import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from src.utils.logger import get_logger
from src.utils.helpers import format_currency, format_percentage
from src.utils.tracer import get_llm_tracer

logger = get_logger("core.pipeline")

class PipelineManager:
    """Manages the execution of the main trading pipeline across all pairs."""

    # Weights for each strategy in ensemble scoring
    _STRATEGY_WEIGHTS: dict[str, float] = {
        "ema_crossover": 0.55,        # trend-following weight
        "bollinger_reversion": 0.45,   # mean-reversion weight
    }
    
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self._candle_cache: dict[str, tuple[float, list]] = {}
        self._candle_cache_lock = asyncio.Lock()

    def _compute_ensemble(self, strategy_signals: dict) -> dict | None:
        """Compute a weighted ensemble from individual strategy signals.

        Returns a dict with:
          action: majority action (buy/sell/hold)
          confidence: weighted average confidence
          agreement: fraction of strategies that agree on the action
          n_strategies: how many strategies contributed
          breakdown: per-strategy summary
        """
        if not strategy_signals:
            return None

        # Filter out internal keys (like _ensemble itself)
        signals = {
            k: v for k, v in strategy_signals.items()
            if not k.startswith("_") and isinstance(v, dict)
        }
        if not signals:
            return None

        action_scores: dict[str, float] = {}  # action → total weighted confidence
        total_weight = 0.0
        breakdown: list[dict] = []

        for name, sig in signals.items():
            action = sig.get("action", "hold")
            conf = sig.get("confidence", 0.0)
            weight = self._STRATEGY_WEIGHTS.get(name, 0.3)

            weighted_conf = conf * weight
            action_scores[action] = action_scores.get(action, 0) + weighted_conf
            total_weight += weight

            breakdown.append({
                "strategy": name,
                "action": action,
                "confidence": round(conf, 3),
                "weight": weight,
            })

        if total_weight == 0:
            return None

        # Majority action = highest weighted confidence
        majority_action = max(action_scores, key=action_scores.get)
        raw_confidence = action_scores[majority_action] / total_weight

        # Agreement bonus: if all strategies agree, boost confidence slightly
        n_strategies = len(signals)
        agreeing = sum(1 for s in signals.values() if s.get("action") == majority_action)
        agreement = agreeing / n_strategies if n_strategies else 0.0

        # Conflicting strategies (buy vs sell) penalize confidence
        has_buy = any(s.get("action") == "buy" for s in signals.values())
        has_sell = any(s.get("action") == "sell" for s in signals.values())
        conflict_penalty = 0.15 if (has_buy and has_sell) else 0.0

        ensemble_confidence = max(0.0, min(1.0, raw_confidence + (0.05 if agreement == 1.0 else 0.0) - conflict_penalty))

        return {
            "action": majority_action,
            "confidence": round(ensemble_confidence, 3),
            "agreement": round(agreement, 3),
            "conflict": has_buy and has_sell,
            "n_strategies": n_strategies,
            "breakdown": breakdown,
        }

    async def run_pipeline(self, pair: str) -> None:
        """Run the full analysis → strategy → risk → execute pipeline for a pair asynchronously."""
        # Unpack dependencies from orchestrator for brevity
        orch = self.orchestrator
        _t0 = time.monotonic()  # wall-clock start
        _timings: dict[str, float] = {}  # step → seconds
        
        logger.info(f"🔍 Analyzing {pair}...")

        if orch.ws_feed:
            with orch._ws_trigger_lock:
                ws_now = orch.ws_feed.get_price(pair)
                if ws_now > 0:
                    orch._ws_last_prices[pair] = ws_now

        cycle_id = str(uuid.uuid4())
        strategic_context = orch.context_manager.get_strategic_context()
        exchange_name = orch.config.get("trading", {}).get("exchange", "coinbase").lower()

        # Set training data pipeline context so LLM callback knows cycle_id/pair
        tc = getattr(orch, "training_collector", None)
        if tc and tc.enabled:
            tc.set_pipeline_context(cycle_id, pair)

        # Run synchronous blocking functions in executor if necessary
        await asyncio.to_thread(orch.holdings_manager.maybe_refresh_holdings)

        tracer = get_llm_tracer()
        trace_ctx = tracer.start_trace(
            cycle_id=cycle_id,
            pair=pair,
            metadata={"strategic_context_preview": strategic_context[:200]},
        ) if tracer else None

        # Data fetching (synchronous -> use asyncio.to_thread if we want true non-blocking,
        # but for now we let it block slightly since Coinbase REST is fast)
        _step_t = time.monotonic()
        granularity = orch.config.get("analysis", {}).get("technical", {}).get(
            "candle_granularity", "ONE_HOUR"
        )
        
        async with self._candle_cache_lock:
            cached = self._candle_cache.get(pair)
            if cached and (time.monotonic() - cached[0]) < min(60.0, orch.interval * 0.9):
                candles = list(cached[1])
            else:
                await orch.rate_limiter.async_wait(orch.exchange.rate_limit_key)
                try:
                    candles = await asyncio.to_thread(
                        orch.exchange.get_candles,
                        pair,
                        granularity=granularity,
                    )
                    self._candle_cache[pair] = (time.monotonic(), list(candles))
                except Exception as e:
                    logger.warning(f"⚠️ Skipping pipeline for {pair}: get_candles failed: {e}")
                    return

        if orch.ws_feed:
            price = orch.ws_feed.get_price(pair)
            if price <= 0:
                await orch.rate_limiter.async_wait(orch.exchange.rate_limit_key)
                price = await asyncio.to_thread(orch.exchange.get_current_price, pair)
        else:
            await orch.rate_limiter.async_wait(orch.exchange.rate_limit_key)
            price = await asyncio.to_thread(orch.exchange.get_current_price, pair)
        _timings["data"] = time.monotonic() - _step_t
        logger.info(f"📊 {pair}: candles={len(candles) if candles else 0}, price={price:.6g}")

        if price <= 0:
            logger.warning(
                f"⚠️ Skipping pipeline for {pair}: price is {price} "
                "(both WebSocket and REST returned an invalid value)"
            )
            return

        orch.state.update_price(pair, price)

        news_headlines = "No news available."
        if orch.news:
            news_headlines = await asyncio.to_thread(
                orch.news.get_headlines,
                orch.config.get("news", {}).get("articles_for_analysis", 15)
            )
        elif orch.redis:
            try:
                cached = orch.redis.get("news:latest")
                if cached:
                    articles = json.loads(cached)
                    news_headlines = "\n".join(
                        f"- [{a.get('source', '?')}] {a.get('title', '')}"
                        for a in articles[:15]
                    )
            except Exception:
                pass

        fg_prompt = ""
        try:
            fg_prompt = await asyncio.to_thread(orch.fear_greed.get_for_prompt)
        except Exception as e:
            logger.debug(f"Fear & Greed unavailable: {e}")

        logger.info(f"📊 {pair}: Fear & Greed done, starting multi-TF...")
        mtf_prompt = ""
        try:
            mtf_prompt = await asyncio.wait_for(
                asyncio.to_thread(orch.multi_tf.get_for_prompt, pair),
                timeout=120,  # 2-minute hard cap for multi-TF analysis
            )
            logger.info(f"📊 {pair}: multi-TF complete")
        except asyncio.TimeoutError:
            logger.warning(f"Multi-TF timed out after 120s for {pair}")
        except Exception as e:
            logger.warning(f"Multi-TF unavailable for {pair}: {e}")

        # ─── Sentiment scoring (keyword-based) ───
        sentiment_prompt = ""
        sentiment_data = {}
        try:
            if orch.sentiment:
                news_items = []
                if orch.redis:
                    cached = orch.redis.get("news:latest")
                    if cached:
                        news_items = json.loads(cached)
                sentiment_data = orch.sentiment.score_for_pair(pair, news_items)
                if sentiment_data.get("total_articles", 0) > 0:
                    sentiment_prompt = (
                        f"Sentiment ({pair}): {sentiment_data.get('sentiment_label', 'neutral')} "
                        f"(score={sentiment_data.get('sentiment_score', 0):.2f}, "
                        f"n={sentiment_data.get('total_articles', 0)})"
                    )
        except Exception as e:
            logger.debug(f"Sentiment analysis unavailable: {e}")

        # ─── Deterministic strategy signals ───
        # Strategies need TechnicalAnalyzer output, not raw candles.
        # We run the same analyzer the market_analyst uses.
        tech_analysis = {}
        try:
            tech_analysis = orch.market_analyst.technical.analyze(candles)
        except Exception as e:
            logger.debug(f"Technical analysis for strategies unavailable: {e}")

        strategy_signals = {}
        if tech_analysis and "error" not in tech_analysis:
            try:
                ema_signal = orch.ema_strategy.generate_signal(pair, candles, tech_analysis)
                if ema_signal and ema_signal.is_actionable:
                    strategy_signals["ema_crossover"] = ema_signal.to_dict()
            except Exception as e:
                logger.debug(f"EMA strategy unavailable: {e}")
            try:
                boll_signal = orch.bollinger_strategy.generate_signal(pair, candles, tech_analysis)
                if boll_signal and boll_signal.is_actionable:
                    strategy_signals["bollinger_reversion"] = boll_signal.to_dict()
            except Exception as e:
                logger.debug(f"Bollinger strategy unavailable: {e}")

        # ─── Strategy ensemble scoring ───
        # Combine individual strategy signals into a weighted consensus.
        # The ensemble score gives the LLM a clear aggregate to work with,
        # while individual signals are still passed for detailed reasoning.
        ensemble = self._compute_ensemble(strategy_signals)
        if ensemble:
            strategy_signals["_ensemble"] = ensemble

        # ─── Pairs correlation (for risk sizing) ───
        _step_t = time.monotonic()
        correlation_matrix = {}
        try:
            all_candles = {pair: candles}
            other_pairs = [p for p in orch.pairs if p != pair]
            if other_pairs:
                # Fetch candles for other pairs with concurrency cap (Cycle-3 fix)
                granularity = orch.config.get("analysis", {}).get("technical", {}).get(
                    "candle_granularity", "ONE_HOUR"
                )
                _sem = asyncio.Semaphore(3)  # max 3 concurrent API calls

                async def _fetch_with_sem(p):
                    async with self._candle_cache_lock:
                        cached = self._candle_cache.get(p)
                        if cached and (time.monotonic() - cached[0]) < min(60.0, orch.interval * 0.9):
                            return list(cached[1])
                    async with _sem:
                        await orch.rate_limiter.async_wait(orch.exchange.rate_limit_key)
                        res = await asyncio.to_thread(orch.exchange.get_candles, p, granularity=granularity)
                        async with self._candle_cache_lock:
                            self._candle_cache[p] = (time.monotonic(), list(res))
                        return res

                other_results = await asyncio.gather(*[
                    _fetch_with_sem(p) for p in other_pairs
                ], return_exceptions=True)
                for p, result in zip(other_pairs, other_results):
                    if isinstance(result, Exception):
                        logger.debug(f"Correlation candle fetch failed for {p}: {result}")
                    else:
                        all_candles[p] = result
            correlation_matrix = orch.pairs_monitor.get_correlation_matrix(all_candles)
        except Exception as e:
            logger.debug(f"Pairs correlation unavailable: {e}")
        _timings["correlation"] = time.monotonic() - _step_t

        # ─── Kelly Criterion stats (from StatsDB) ───
        kelly_stats = {"win_rate": 0, "avg_win": 0, "avg_loss": 0, "sample_size": 0}
        try:
            kelly_stats = await asyncio.to_thread(orch.stats_db.get_win_loss_stats)
        except Exception as e:
            logger.debug(f"Kelly stats unavailable: {e}")

        recent_outcomes = ""
        try:
            recent_outcomes = await asyncio.to_thread(
                orch.stats_db.get_recent_outcomes,
                pair, n=10, currency_symbol=orch.state.currency_symbol
            )
        except Exception as e:
            logger.debug(f"Failed to load recent outcomes: {e}")

        # ─── Training Data: record market snapshot ───
        if tc and tc.enabled:
            try:
                tc.record_snapshot(
                    cycle_id, pair,
                    price=price,
                    candles=candles,
                    technical=tech_analysis,
                    strategy_signals=strategy_signals,
                    fear_greed=fg_prompt,
                    multi_timeframe=mtf_prompt,
                    sentiment=sentiment_data,
                    correlation_matrix=correlation_matrix,
                    kelly_stats=kelly_stats,
                    portfolio_value=(
                        orch.state.live_portfolio_value
                        if orch.state.live_portfolio_value > 0
                        else orch.state.portfolio_value
                    ),
                    cash_balance=(
                        sum(orch.state.live_cash_balances.values())
                        if orch.state.live_cash_balances
                        else orch.state.cash_balance
                    ),
                    open_positions=orch.state.open_positions,
                    recent_outcomes=recent_outcomes,
                    strategic_context=strategic_context,
                )
            except Exception:
                pass  # never break pipeline

        # Compute portfolio metrics once for all downstream agents
        _portfolio_value = (
            orch.state.live_portfolio_value if orch.state.live_portfolio_value > 0 else orch.state.portfolio_value
        )
        _cash_balance = (
            sum(orch.state.live_cash_balances.values()) if orch.state.live_cash_balances else orch.state.cash_balance
        )

        # Step 2: Market Analysis
        _step_t = time.monotonic()
        analysis_result = await orch.market_analyst.execute({
            "pair": pair,
            "candles": candles,
            "news_headlines": news_headlines,
            "fear_greed": fg_prompt,
            "multi_timeframe": mtf_prompt,
            "sentiment": sentiment_prompt,
            "strategy_signals": strategy_signals,
            "strategic_context": strategic_context,
            "currency_symbol": orch.state.currency_symbol,
            "native_currency": orch.state.native_currency,
            "portfolio_value": _portfolio_value,
            "cash_balance": _cash_balance,
            "cycle_id": cycle_id,
            "stats_db": orch.stats_db,
            "trace_ctx": trace_ctx,
            "exchange": exchange_name,
        })

        signal = analysis_result.get("signal", {})
        _timings["analyst"] = time.monotonic() - _step_t

        # ─── Training Data: record analysis decision ───
        if tc and tc.enabled:
            try:
                tc.record_decision(
                    cycle_id, pair, "analysis",
                    decision=signal,
                    action=signal.get("signal_type", ""),
                    confidence=signal.get("confidence", 0),
                    reasoning=signal.get("reasoning", ""),
                    context={"llm_analysis": analysis_result.get("llm_analysis", "")},
                )
            except Exception:
                pass

        if "error" in analysis_result:
            logger.warning(f"Analysis failed for {pair}: {analysis_result.get('error')}")
            orch.journal.log_decision("analysis_failed", pair, "none", {"error": analysis_result.get('error')})
            if trace_ctx is not None:
                trace_ctx.finish(metadata={"action": "analysis_failed", "error": analysis_result.get("error", "")})
            return

        confidence = signal.get("confidence", 0)
        notify_threshold = orch.config.get("telegram", {}).get("notify_on_signal_confidence", 0.8)
        if confidence >= notify_threshold and orch.telegram:
            # Cycle-3 fix: find the signal for THIS pair instead of signals[-1]
            # which races with concurrent pipelines via asyncio.gather.
            signal_obj = next(
                (s for s in reversed(orch.state.signals) if s.pair == pair), None
            )
            if signal_obj:
                orch.telegram.send_signal_notification(signal_obj.to_summary())

        # Step 3: Strategy Generation
        _step_t = time.monotonic()
        # Apply per-pair confidence adjustment from planning context
        pair_confidence_adj = orch.context_manager.get_pair_confidence_adjustment(pair)

        # Build fee context so the LLM knows about trading costs
        _rt_fee = orch.fee_manager.trade_fee_pct * 2  # round-trip
        _be_fee = _rt_fee * orch.fee_manager.fee_safety_margin
        fee_context = {
            "round_trip_fee_pct": _rt_fee,
            "breakeven_pct": _be_fee,
            "min_gain_pct": orch.fee_manager.min_gain_after_fees_pct + _rt_fee,
        }

        strategy_result = await orch.strategist.execute({
            "signal": signal,
            "active_tasks": [t.to_dict() for t in orch.active_tasks if not t.completed],
            "current_balance": orch.exchange.balance if hasattr(orch.exchange, 'balance') else {},
            "open_positions": orch.state.open_positions,
            "recent_trades": [t.to_summary() for t in orch.state.recent_trades],
            "recent_outcomes": recent_outcomes,
            "strategic_context": strategic_context,
            "live_holdings_summary": orch.state.holdings_summary,
            "native_currency": orch.state.native_currency,
            "currency_symbol": orch.state.currency_symbol,
            "portfolio_value": _portfolio_value,
            "cash_balance": _cash_balance,
            "sentiment": sentiment_data,
            "strategy_signals": strategy_signals,
            "confidence_adjustment": pair_confidence_adj,
            "fee_context": fee_context,
            "cycle_id": cycle_id,
            "stats_db": orch.stats_db,
            "trace_ctx": trace_ctx,
            "exchange": exchange_name,
        })

        if strategy_result.get("action") == "hold":
            _timings["strategist"] = time.monotonic() - _step_t

            # ─── Training Data: record hold decision ───
            if tc and tc.enabled:
                try:
                    tc.record_decision(
                        cycle_id, pair, "hold",
                        decision=strategy_result,
                        action="hold",
                        confidence=strategy_result.get("confidence", 0),
                        reasoning=strategy_result.get("reason", strategy_result.get("reasoning", "")),
                    )
                except Exception:
                    pass

            _total = time.monotonic() - _t0
            _parts = " ".join(f"{k}={v:.1f}s" for k, v in _timings.items())
            logger.info(f"⏱️ Pipeline {pair}: {_parts} total={_total:.1f}s [hold]")
            logger.info(f"📊 {pair}: HOLD — {strategy_result.get('reason', strategy_result.get('reasoning', 'No action'))}")
            orch.journal.log_decision("hold", pair, "hold", {"signal": signal, "reasoning": strategy_result.get('reason', '')})
            if trace_ctx is not None:
                trace_ctx.finish(metadata={"action": "hold", "reason": strategy_result.get("reason", "")})
            return

        strategy_result["current_price"] = price

        # Guard: ensure the strategist didn't propose a trade on a different pair
        proposed_pair = strategy_result.get("pair", pair)
        if proposed_pair != pair:
            logger.warning(
                f"⚠️ Strategist proposed trade on {proposed_pair} but pipeline is for {pair} — "
                f"correcting to {pair}"
            )
            strategy_result["pair"] = pair

        _timings["strategist"] = time.monotonic() - _step_t

        # Step 4: Risk Validation
        _step_t = time.monotonic()
        risk_result = await orch.risk_manager.execute({
            "proposal": strategy_result,
            "portfolio_value": _portfolio_value,
            "cash_balance": _cash_balance,
            "cycle_id": cycle_id,
            "stats_db": orch.stats_db,
            "trace_ctx": trace_ctx,
            "win_rate": kelly_stats.get("win_rate", 0),
            "avg_win": kelly_stats.get("avg_win", 0),
            "avg_loss": kelly_stats.get("avg_loss", 0),
            "correlation_matrix": correlation_matrix,
            "atr": tech_analysis.get("atr") if tech_analysis else None,
            "exchange": exchange_name,
        })

        if not risk_result.get("approved"):
            _timings["risk"] = time.monotonic() - _step_t

            # ─── Training Data: record rejected decision ───
            if tc and tc.enabled:
                try:
                    tc.record_decision(
                        cycle_id, pair, "rejected",
                        decision=risk_result,
                        action=strategy_result.get("action", "unknown"),
                        confidence=strategy_result.get("confidence", 0),
                        approved=False,
                        reasoning=risk_result.get("reason", ""),
                        context={"proposal": strategy_result},
                    )
                except Exception:
                    pass

            _total = time.monotonic() - _t0
            _parts = " ".join(f"{k}={v:.1f}s" for k, v in _timings.items())
            logger.info(f"⏱️ Pipeline {pair}: {_parts} total={_total:.1f}s [rejected]")
            logger.info(f"🚫 {pair}: Trade rejected — {risk_result.get('reason', 'Unknown')}")
            orch.journal.log_decision("trade_rejected", pair, strategy_result.get("action", "unknown"), {
                "reason": risk_result.get("reason", "Unknown"),
                "proposal": strategy_result,
            })
            orch.audit.log_rule_check("risk_validation", passed=False, details=risk_result.get('reason', 'Unknown'))
            if trace_ctx is not None:
                trace_ctx.finish(metadata={"action": "rejected", "reason": risk_result.get("reason", "")})
            return

        # Step 5: Handle approval or execute
        _timings["risk"] = time.monotonic() - _step_t

        # ─── Step 5a: Fee Viability Gate ─────────────────────────────
        # Ensure the trade is actually profitable after fees.
        # Applies to BOTH buys and sells of bot-tracked positions.
        _trade_action = risk_result.get("action")
        if risk_result.get("approved") and _trade_action in ("buy", "sell"):
            trade_amount = float(risk_result.get("quote_amount", 0))
            trade_price = risk_result.get("price", 0)

            if _trade_action == "buy":
                tp = risk_result.get("take_profit")
                # Estimate expected gain from the take-profit target
                if tp and trade_price and trade_price > 0:
                    expected_gain_pct = (float(tp) - trade_price) / trade_price
                else:
                    # Fallback: use tier's take_profit_pct as expected gain
                    expected_gain_pct = getattr(
                        getattr(orch, "portfolio_scaler", None), "tier", None
                    )
                    if expected_gain_pct:
                        expected_gain_pct = expected_gain_pct.take_profit_pct
                    else:
                        expected_gain_pct = orch.config.get("risk", {}).get("take_profit_pct", 0.06)

                # ─── Plan-based TP override ──────────────────────────────────
                _plan_min_conf = orch.config.get("planning", {}).get(
                    "plan_tp_min_confidence", 0.65
                )
                _plan_outlook = orch.context_manager.get_pair_expected_gain(pair)
                if _plan_outlook:
                    _plan_gain = _plan_outlook["gain_pct"]
                    _plan_conf = _plan_outlook["confidence"]
                    _plan_horizon = _plan_outlook["horizon_days"]
                    if _plan_gain > expected_gain_pct and _plan_conf >= _plan_min_conf:
                        _plan_tp = trade_price * (1 + _plan_gain)
                        logger.info(
                            f"📋 {pair}: Plan-based TP override — "
                            f"gain {_plan_gain:.1%} (conf={_plan_conf:.0%}, {_plan_horizon}d) "
                            f"replaces TP-based {expected_gain_pct:.1%} | "
                            f"new TP={_plan_tp:.4f}"
                        )
                        risk_result["take_profit"] = _plan_tp
                        expected_gain_pct = _plan_gain

            elif _trade_action == "sell" and trade_price > 0:
                # For sell orders: check if gain from entry covers fees.
                # Only applies to bot-tracked positions (we know the entry price).
                from src.models.trade import TradeAction
                entry_trade = next(
                    (t for t in reversed(orch.state.recent_trades)
                     if t.pair == pair and t.action == TradeAction.BUY),
                    None
                )
                if entry_trade and entry_trade.price and entry_trade.price > 0:
                    expected_gain_pct = (trade_price - entry_trade.price) / entry_trade.price
                else:
                    # Pre-existing holding (not bot-bought) — skip fee gate
                    # since we don't know the cost basis
                    expected_gain_pct = None

            if expected_gain_pct is not None:
                worthwhile, fee_est = orch.fee_manager.is_trade_worthwhile(
                    quote_amount=trade_amount,
                    expected_gain_pct=expected_gain_pct,
                    is_swap=False,
                    portfolio_value=_portfolio_value,
                )
                if not worthwhile:
                    # ─── Auto-bump: try increasing amount to minimum viable ───
                    bumped = False
                    if _trade_action == "buy":
                        min_viable = orch.fee_manager.get_dynamic_min_trade(_portfolio_value)
                        bumped_amount = max(trade_amount, min_viable)

                        # Cap at available cash and risk-manager position limits
                        _rm_max_pct = orch.risk_manager.risk_config.get("max_position_pct", 0.05)
                        if orch.risk_manager.scaler and _portfolio_value > 0:
                            _rm_max_pct = max(_rm_max_pct, orch.risk_manager.scaler.tier.max_position_pct)
                        _max_position = _portfolio_value * _rm_max_pct
                        bumped_amount = min(bumped_amount, _cash_balance, _max_position)

                        if bumped_amount > trade_amount:
                            worthwhile, fee_est = orch.fee_manager.is_trade_worthwhile(
                                quote_amount=bumped_amount,
                                expected_gain_pct=expected_gain_pct,
                                is_swap=False,
                                portfolio_value=_portfolio_value,
                            )
                            if worthwhile:
                                logger.info(
                                    f"📈 {pair}: Fee gate auto-bumped amount "
                                    f"{trade_amount:.2f} → {bumped_amount:.2f} "
                                    f"(min_viable={min_viable:.2f}, "
                                    f"cash={_cash_balance:.2f}, "
                                    f"max_pos={_max_position:.2f})"
                                )
                                risk_result["quote_amount"] = bumped_amount
                                if trade_price > 0:
                                    risk_result["quantity"] = bumped_amount / trade_price
                                trade_amount = bumped_amount
                                bumped = True

                    if not bumped and not worthwhile:
                        logger.info(
                            f"💸 {pair}: {_trade_action.upper()} NOT worthwhile after fees "
                            f"(amount={trade_amount:.2f}, expected={expected_gain_pct*100:.1f}%, "
                            f"breakeven={fee_est.breakeven_move_pct*100:.1f}%)"
                        )
                        orch.journal.log_decision(
                            "fee_gate_reject", pair, _trade_action,
                            {"reason": "Fees would eat expected gains",
                             "trade_amount": trade_amount,
                             "breakeven_pct": fee_est.breakeven_move_pct,
                             "expected_gain_pct": expected_gain_pct},
                        )
                        if trace_ctx is not None:
                            trace_ctx.finish(metadata={"action": "fee_gate_reject"})
                        return

        if risk_result.get("needs_approval"):
            trade_desc = (
                f"{risk_result['action'].upper()} {risk_result['pair']}\n"
                f"Amount: {format_currency(risk_result['quote_amount'])}\n"
                f"Price: {format_currency(risk_result['price'])}\n"
                f"Stop-Loss: {format_currency(risk_result.get('stop_loss', 0))}\n"
                f"Take-Profit: {format_currency(risk_result.get('take_profit', 0))}\n"
                f"Confidence: {format_percentage(risk_result.get('confidence', 0))}"
            )
            trade_id = f"pending_{uuid.uuid4().hex[:8]}"
            with orch._pending_approvals_lock:
                risk_result["_queued_at"] = datetime.now(timezone.utc).isoformat()
                orch._pending_approvals[trade_id] = risk_result

            if orch.telegram:
                orch.telegram.request_approval(trade_desc, trade_id)
            if trace_ctx is not None:
                trace_ctx.finish(metadata={"action": "pending_approval", "trade_id": trade_id})
            return

        # Step 6: Execute Trade
        _step_t = time.monotonic()
        exec_result = await orch.executor.execute({
            "approved_trade": risk_result,
        })

        if exec_result.get("executed"):
            # Persist trade to StatsDB
            # Use the ACTUAL filled price/quantity from the executor (exchange-
            # reported) so the stats DB reflects what really happened, not the
            # pre-trade estimate from the risk manager.
            stats_trade_id = None
            _exec_trade = exec_result.get('trade', {})
            _filled_price = (
                _exec_trade.get('filled_price')
                or risk_result.get('price', price)
            )
            _filled_qty = (
                _exec_trade.get('filled_quantity')
                or _exec_trade.get('quantity')
                or 0
            )
            _fee = _exec_trade.get('fees', 0) or 0
            # For quote_amount: use actual fill values when available
            _quote_amount = risk_result.get('quote_amount', risk_result.get('usd_amount', 0))
            if _filled_price and _filled_qty and risk_result.get('action') == 'sell':
                # For sells the quote_amount is the actual proceeds
                _quote_amount = float(_filled_price) * float(_filled_qty)
            try:
                stats_trade_id = await asyncio.to_thread(
                    orch.stats_db.record_trade,
                    pair=risk_result.get('pair', pair),
                    action=risk_result.get('action', 'unknown'),
                    price=float(_filled_price),
                    quantity=float(_filled_qty),
                    quote_amount=float(_quote_amount),
                    confidence=risk_result.get('confidence', 0),
                    signal_type=signal.get('signal_type', ''),
                    stop_loss=risk_result.get('stop_loss', 0),
                    take_profit=risk_result.get('take_profit', 0),
                    reasoning=risk_result.get('reasoning', ''),
                    fee_quote=float(_fee),
                    exchange=exchange_name,
                )
            except Exception as e:
                logger.debug(f"Failed to record trade in StatsDB: {e}")

            # Link all agent reasoning rows for this cycle to the trade
            if stats_trade_id and cycle_id:
                try:
                    await asyncio.to_thread(
                        orch.stats_db.backfill_reasoning_trade_id,
                        cycle_id,
                        stats_trade_id,
                    )
                except Exception as e:
                    logger.debug(f"Failed to backfill reasoning trade_id: {e}")

            orch.journal.log_trade(
                pair=risk_result.get('pair', pair),
                action=risk_result.get('action', 'unknown'),
                quantity=float(_filled_qty),
                price=float(_filled_price),
                quote_amount=float(_quote_amount),
                confidence=risk_result.get('confidence', 0),
                signal_type=signal.get('signal_type', ''),
                stop_loss=risk_result.get('stop_loss', 0),
                take_profit=risk_result.get('take_profit', 0),
                reasoning=risk_result.get('reasoning', ''),
                fear_greed=orch.fear_greed.last_value or 0,
                rsi=signal.get('rsi', 0),
                macd_signal=signal.get('macd_signal', ''),
            )
            orch.audit.log_trade(
                pair=risk_result.get('pair', pair),
                action=risk_result.get('action', 'unknown'),
                amount=float(_quote_amount),
                price=float(_filled_price),
            )

            if risk_result.get('action') == 'buy':
                orch.trailing_stops.add_stop(
                    pair=risk_result.get('pair', pair),
                    entry_price=float(_filled_price),
                    initial_stop=risk_result.get('stop_loss'),
                    total_quantity=float(_filled_qty) if _filled_qty else 0.0,
                )

            # ─── FIFO tax tracking ───
            try:
                trade_pair = risk_result.get('pair', pair)
                base_asset = trade_pair.split("-")[0] if "-" in trade_pair else trade_pair

                if risk_result.get('action') == 'buy' and float(_filled_qty or 0) > 0:
                    orch.fifo_tracker.record_buy(
                        asset=base_asset,
                        quantity=float(_filled_qty),
                        cost_per_unit=float(_filled_price),
                        fees=float(_fee),
                    )
                elif risk_result.get('action') == 'sell' and float(_filled_qty or 0) > 0:
                    disposals = orch.fifo_tracker.record_sell(
                        asset=base_asset,
                        quantity=float(_filled_qty),
                        price_per_unit=float(_filled_price),
                        fees=float(_fee),
                    )
                    # Back-fill realized PNL into the StatsDB trade row so that
                    # analytics queries (pnl IS NOT NULL) can include this trade.
                    # Only update when we had real FIFO lots (cost_basis_per_unit > 0).
                    # Skips pre-existing holdings where cost basis is unknown.
                    if stats_trade_id and disposals:
                        valid = [d for d in disposals if d.cost_basis_per_unit > 0]
                        if valid:
                            realized_pnl = sum(d.realized_pnl for d in valid)
                            total_fees = sum(d.fees for d in valid)
                            try:
                                orch.stats_db.update_trade_pnl(
                                    stats_trade_id, realized_pnl, fee_quote=total_fees
                                )
                            except Exception as _upd_err:
                                logger.debug(f"PNL back-fill failed (non-fatal): {_upd_err}")
            except Exception as e:
                logger.debug(f"FIFO tracking failed (non-fatal): {e}")

            trade_event = (
                f"{'BUY' if risk_result['action'] == 'buy' else 'SELL'} "
                f"{risk_result['pair']} — "
                f"{format_currency(risk_result.get('quote_amount', risk_result.get('usd_amount', 0)))} "
                f"at {format_currency(risk_result['price'])} "
                f"(confidence: {format_percentage(risk_result.get('confidence', 0))})"
            )
            orch.chat_handler.queue_event(f"Trade executed: {trade_event}")

            if orch.telegram and orch.config.get("telegram", {}).get("notify_on_trade", True):
                orch.telegram.send_trade_notification(trade_event)

            # ─── Training Data: record execution decision ───
            if tc and tc.enabled:
                try:
                    tc.record_decision(
                        cycle_id, pair, "execution",
                        decision=exec_result,
                        action=risk_result.get("action", "unknown"),
                        confidence=risk_result.get("confidence", 0),
                        approved=True,
                        reasoning=risk_result.get("reasoning", ""),
                        context={
                            "signal": signal,
                            "strategy": strategy_result,
                            "risk": risk_result,
                            "slippage_pct": exec_result.get("slippage_pct", 0),
                            "order_type": exec_result.get("order_type", ""),
                        },
                    )
                except Exception:
                    pass

            _timings["exec"] = time.monotonic() - _step_t
            _total = time.monotonic() - _t0
            _parts = " ".join(f"{k}={v:.1f}s" for k, v in _timings.items())
            logger.info(f"⏱️ Pipeline {pair}: {_parts} total={_total:.1f}s [executed]")

            if trace_ctx is not None:
                try:
                    trace_ctx.finish(metadata={
                        "trade_executed": True,
                        "action": risk_result.get("action"),
                        "quote_amount": risk_result.get("quote_amount"),
                        "confidence": risk_result.get("confidence"),
                    })
                except Exception:
                    pass

        else:
            # Execution failed — log details for debugging
            exec_error = exec_result.get("error", exec_result.get("reason", "unknown"))
            logger.warning(
                f"⚠️ Trade execution FAILED for {pair}: {exec_error} | "
                f"action={risk_result.get('action')} amount={risk_result.get('quote_amount')}"
            )
            _timings["exec"] = time.monotonic() - _step_t
            _total = time.monotonic() - _t0
            _parts = " ".join(f"{k}={v:.1f}s" for k, v in _timings.items())
            logger.info(f"⏱️ Pipeline {pair}: {_parts} total={_total:.1f}s [NOT executed]")

            if trace_ctx is not None:
                try:
                    trace_ctx.finish(metadata={
                        "trade_executed": False,
                        "exec_error": str(exec_error),
                    })
                except Exception:
                    pass
