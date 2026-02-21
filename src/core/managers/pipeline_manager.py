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
        strategic_context = orch._get_strategic_context()

        # Run synchronous blocking functions in executor if necessary
        await asyncio.to_thread(orch._maybe_refresh_holdings)

        tracer = get_llm_tracer()
        trace_ctx = tracer.start_trace(
            cycle_id=cycle_id,
            pair=pair,
            metadata={"strategic_context_preview": strategic_context[:200]},
        ) if tracer else None

        # Data fetching (synchronous -> use asyncio.to_thread if we want true non-blocking,
        # but for now we let it block slightly since Coinbase REST is fast)
        _step_t = time.monotonic()
        await orch.rate_limiter.async_wait("coinbase_rest")
        candles = await asyncio.to_thread(
            orch.exchange.get_candles,
            pair,
            granularity=orch.config.get("analysis", {}).get("technical", {}).get(
                "candle_granularity", "ONE_HOUR"
            ),
        )

        if orch.ws_feed:
            price = orch.ws_feed.get_price(pair)
            if price <= 0:
                await orch.rate_limiter.async_wait("coinbase_rest")
                price = await asyncio.to_thread(orch.exchange.get_current_price, pair)
        else:
            await orch.rate_limiter.async_wait("coinbase_rest")
            price = await asyncio.to_thread(orch.exchange.get_current_price, pair)
        _timings["data"] = time.monotonic() - _step_t

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

        mtf_prompt = ""
        try:
            mtf_prompt = await asyncio.to_thread(orch.multi_tf.get_for_prompt, pair)
        except Exception as e:
            logger.debug(f"Multi-TF unavailable: {e}")

        # ─── Sentiment scoring (keyword-based) ───
        sentiment_prompt = ""
        sentiment_data = {}
        try:
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
                # Fetch candles for other pairs in parallel
                granularity = orch.config.get("analysis", {}).get("technical", {}).get(
                    "candle_granularity", "ONE_HOUR"
                )
                other_results = await asyncio.gather(*[
                    asyncio.to_thread(orch.exchange.get_candles, p, granularity=granularity)
                    for p in other_pairs
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
            "cycle_id": cycle_id,
            "stats_db": orch.stats_db,
            "trace_ctx": trace_ctx,
        })

        signal = analysis_result.get("signal", {})
        _timings["analyst"] = time.monotonic() - _step_t
        if "error" in analysis_result:
            logger.warning(f"Analysis failed for {pair}: {analysis_result.get('error')}")
            orch.journal.log_decision("analysis_failed", pair, "none", {"error": analysis_result.get('error')})
            if trace_ctx is not None:
                trace_ctx.finish(metadata={"action": "analysis_failed", "error": analysis_result.get("error", "")})
            return

        confidence = signal.get("confidence", 0)
        notify_threshold = orch.config.get("telegram", {}).get("notify_on_signal_confidence", 0.8)
        if confidence >= notify_threshold and orch.telegram:
            signal_obj = orch.state.signals[-1] if orch.state.signals else None
            if signal_obj:
                orch.telegram.send_signal_notification(signal_obj.to_summary())

        # Stop early if this is strictly a watchlist pair
        is_watchlist_only = pair in getattr(orch, "watchlist_pairs", []) and pair not in orch.pairs
        if is_watchlist_only:
            _timings["analyst"] = time.monotonic() - _step_t
            _total = time.monotonic() - _t0
            _parts = " ".join(f"{k}={v:.1f}s" for k, v in _timings.items())
            logger.info(f"👀 Pipeline {pair}: {_parts} total={_total:.1f}s [watchlist-only]")
            if trace_ctx is not None:
                trace_ctx.finish(metadata={"action": "watchlist_only", "signal": signal.get("action")})
            return

        # Step 3: Strategy Generation
        _step_t = time.monotonic()
        # Apply per-pair confidence adjustment from planning context
        pair_confidence_adj = orch.get_pair_confidence_adjustment(pair)

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
            "sentiment": sentiment_data,
            "strategy_signals": strategy_signals,
            "confidence_adjustment": pair_confidence_adj,
            "cycle_id": cycle_id,
            "stats_db": orch.stats_db,
            "trace_ctx": trace_ctx,
        })

        if strategy_result.get("action") == "hold":
            _timings["strategist"] = time.monotonic() - _step_t
            _total = time.monotonic() - _t0
            _parts = " ".join(f"{k}={v:.1f}s" for k, v in _timings.items())
            logger.info(f"⏱️ Pipeline {pair}: {_parts} total={_total:.1f}s [hold]")
            logger.info(f"📊 {pair}: HOLD — {strategy_result.get('reason', strategy_result.get('reasoning', 'No action'))}")
            orch.journal.log_decision("hold", pair, "hold", {"signal": signal, "reasoning": strategy_result.get('reason', '')})
            if trace_ctx is not None:
                trace_ctx.finish(metadata={"action": "hold", "reason": strategy_result.get("reason", "")})
            return

        strategy_result["current_price"] = price
        _timings["strategist"] = time.monotonic() - _step_t

        # Step 4: Risk Validation
        _step_t = time.monotonic()
        risk_portfolio_value = (
            orch.state.live_portfolio_value if orch.state.live_portfolio_value > 0 else orch.state.portfolio_value
        )
        risk_cash_balance = (
            sum(orch.state.live_cash_balances.values()) if orch.state.live_cash_balances else orch.state.cash_balance
        )
        risk_result = await orch.risk_manager.execute({
            "proposal": strategy_result,
            "portfolio_value": risk_portfolio_value,
            "cash_balance": risk_cash_balance,
            "cycle_id": cycle_id,
            "stats_db": orch.stats_db,
            "trace_ctx": trace_ctx,
            "win_rate": kelly_stats.get("win_rate", 0),
            "avg_win": kelly_stats.get("avg_win", 0),
            "avg_loss": kelly_stats.get("avg_loss", 0),
            "correlation_matrix": correlation_matrix,
        })

        if not risk_result.get("approved"):
            _timings["risk"] = time.monotonic() - _step_t
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
            try:
                stats_trade_id = await asyncio.to_thread(
                    orch.stats_db.record_trade,
                    pair=risk_result.get('pair', pair),
                    action=risk_result.get('action', 'unknown'),
                    price=risk_result.get('price', price),
                    quantity=(
                        exec_result.get('trade', {}).get('filled_quantity')
                        or exec_result.get('trade', {}).get('quantity')
                        or 0
                    ),
                    quote_amount=risk_result.get('quote_amount', risk_result.get('usd_amount', 0)),
                    confidence=risk_result.get('confidence', 0),
                    signal_type=signal.get('signal_type', ''),
                    stop_loss=risk_result.get('stop_loss', 0),
                    take_profit=risk_result.get('take_profit', 0),
                    reasoning=risk_result.get('reasoning', ''),
                )
            except Exception as e:
                logger.debug(f"Failed to record trade in StatsDB: {e}")

            orch.journal.log_trade(
                pair=risk_result.get('pair', pair),
                action=risk_result.get('action', 'unknown'),
                quantity=(
                    exec_result.get('trade', {}).get('filled_quantity')
                    or exec_result.get('trade', {}).get('quantity')
                    or 0
                ),
                price=risk_result.get('price', price),
                quote_amount=risk_result.get('quote_amount', risk_result.get('usd_amount', 0)),
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
                amount=risk_result.get('quote_amount', risk_result.get('usd_amount', 0)),
                price=risk_result.get('price', price),
            )

            if risk_result.get('action') == 'buy':
                filled_qty = (
                    exec_result.get('trade', {}).get('filled_quantity')
                    or exec_result.get('trade', {}).get('quantity')
                    or risk_result.get('quantity', 0)
                )
                orch.trailing_stops.add_stop(
                    pair=risk_result.get('pair', pair),
                    entry_price=risk_result.get('price', price),
                    initial_stop=risk_result.get('stop_loss'),
                    total_quantity=float(filled_qty) if filled_qty else 0.0,
                )

            # ─── FIFO tax tracking ───
            try:
                filled_qty = (
                    exec_result.get('trade', {}).get('filled_quantity')
                    or exec_result.get('trade', {}).get('quantity')
                    or risk_result.get('quantity', 0)
                )
                fill_price = risk_result.get('price', price)
                trade_pair = risk_result.get('pair', pair)
                base_asset = trade_pair.split("-")[0] if "-" in trade_pair else trade_pair
                fee_amount = exec_result.get('trade', {}).get('fee', 0) or 0

                if risk_result.get('action') == 'buy' and float(filled_qty or 0) > 0:
                    orch.fifo_tracker.record_buy(
                        asset=base_asset,
                        quantity=float(filled_qty),
                        cost_per_unit=float(fill_price),
                        fees=float(fee_amount),
                    )
                elif risk_result.get('action') == 'sell' and float(filled_qty or 0) > 0:
                    orch.fifo_tracker.record_sell(
                        asset=base_asset,
                        quantity=float(filled_qty),
                        sale_price_per_unit=float(fill_price),
                        fees=float(fee_amount),
                    )
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
