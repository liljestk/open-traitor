import asyncio
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from src.utils.logger import get_logger
from src.utils.helpers import format_currency, format_percentage
from src.utils.tracer import get_llm_tracer
from src.utils import llm_optimizer

logger = get_logger("core.pipeline")


def _read_sentiment_score(exchange: str) -> float | None:
    """Read the latest news_bias.json's ``sentiment_mean`` (∈[-1, +1]) for
    pattern-engine sentiment fusion. Returns ``None`` on any error so the
    fusion step is skipped gracefully."""
    try:
        from pathlib import Path
        profile = (
            os.environ.get("AUTO_TRAITOR_PROFILE") or exchange or ""
        ).lower()
        if not profile:
            return None
        path = Path("data") / profile / "news_bias.json"
        if not path.exists():
            return None
        # Stale guard: ignore older than 6h.
        if time.time() - path.stat().st_mtime > 6 * 3600:
            return None
        data = json.loads(path.read_text())
        v = data.get("sentiment_mean")
        if v is None:
            return None
        return max(-1.0, min(1.0, float(v)))
    except Exception:
        return None


def _candle_returns(candles: list, lookback: int = 30) -> list[float]:
    """Extract close-to-close pct returns from the last `lookback+1` candles.

    Returns [] when candles are missing or malformed so vol_target falls back
    to a neutral multiplier of 1.0 instead of zeroing the trade.
    """
    if not candles or len(candles) < 2:
        return []
    try:
        closes = []
        for c in candles[-(lookback + 1):]:
            if isinstance(c, dict):
                px = c.get("close") or c.get("c")
            else:
                # tuple/list-like (ts, o, h, l, c, v)
                px = c[4] if len(c) > 4 else None
            if px is None:
                continue
            closes.append(float(px))
        if len(closes) < 2:
            return []
        return [
            (closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes))
            if closes[i - 1] > 0
        ]
    except Exception:
        return []


def _build_accuracy_ctx(
    pair_accuracy: dict | None, weighted_acc: dict
) -> dict | None:
    """Merge raw pair accuracy with signal-strength-weighted accuracy for LLM context."""
    base = dict(pair_accuracy) if pair_accuracy else {}
    if weighted_acc:
        base["weighted_accuracy_pct"] = weighted_acc.get("weighted_accuracy_pct")
        base["weighted_sample_count"] = weighted_acc.get("weighted_total")
        base["accuracy_by_signal_type"] = weighted_acc.get("by_type")
    return base or None


class PipelineManager:
    """Manages the execution of the main trading pipeline across all pairs."""

    # Weights for each strategy in ensemble scoring
    _STRATEGY_WEIGHTS: dict[str, float] = {
        "ema_crossover": 0.45,        # trend-following weight
        "bollinger_reversion": 0.35,   # mean-reversion weight
        "pattern_engine": 0.20,        # catalyst pattern engine (event-driven)
    }

    # Equity calendar TTL: refresh earnings/dividend data at most every 4 hours
    _EQUITY_CALENDAR_TTL: float = 4 * 3600

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self._candle_cache: dict[str, tuple[float, list]] = {}
        # C3 fix: Use threading.Lock instead of asyncio.Lock because this cache
        # is accessed from both async code and from asyncio.to_thread() workers
        # (threadpool). An asyncio.Lock cannot be acquired from a thread.
        self._candle_cache_lock = threading.Lock()
        # Equity event calendar cache (earnings + dividends for all watchlist pairs)
        self._equity_calendar: dict = {}
        self._equity_calendar_ts: float = 0.0

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

        # ── Dynamic weights from ALE ensemble optimizer ───────────────
        dynamic_weights = self._get_dynamic_weights()

        action_scores: dict[str, float] = {}  # action → total weighted confidence
        total_weight = 0.0
        breakdown: list[dict] = []

        for name, sig in signals.items():
            action = sig.get("action", "hold")
            conf = sig.get("confidence", 0.0)
            weight = dynamic_weights.get(name, self._STRATEGY_WEIGHTS.get(name, 0.3))

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

    def _get_dynamic_weights(self) -> dict[str, float]:
        """Fetch learned strategy weights from ALE ensemble optimizer.

        Falls back to static ``_STRATEGY_WEIGHTS`` on any error.
        Phase 9: blends in ``QuantSubstrate.capital_allocator`` weights —
        gives self-learning capital flow influence over the ensemble.
        """
        # Phase 1 (smarts): Thompson-sampling bandit overlay. Strict opt-in
        # via top-level config ``smarts.use_bandit`` (default off so the
        # rollout is gradual). Falls back to existing weights on any error.
        bandit_weights: dict[str, float] | None = None
        try:
            cfg = getattr(self.orchestrator, "config", {}) or {}
            # Strict isinstance guard so MagicMock test doubles (whose
            # ``.get(...)`` returns truthy MagicMocks) don't accidentally
            # enable the bandit overlay.
            smarts_cfg = cfg.get("smarts", {}) if isinstance(cfg, dict) else {}
            use_bandit = (
                isinstance(smarts_cfg, dict)
                and bool(smarts_cfg.get("use_bandit", False)) is True
                and isinstance(smarts_cfg.get("use_bandit"), (bool, int))
            )
            if use_bandit:
                from src.utils.bandit import StrategyBandit
                exch = cfg.get("trading", {}).get("exchange", "coinbase")
                regime = (
                    getattr(self.orchestrator.state, "market_regime", "unknown")
                    or "unknown"
                )
                strats = list(self._STRATEGY_WEIGHTS.keys())
                bandit_weights = StrategyBandit(
                    self.orchestrator.stats_db, exchange=exch,
                ).sample_weights(regime, strats)
        except Exception:
            bandit_weights = None
        try:
            lm = getattr(self.orchestrator, "learning_manager", None)
            if lm and lm.ensemble:
                # Detect current regime from latest state
                regime = getattr(self.orchestrator.state, "market_regime", "unknown")
                base = lm.ensemble.get_weights(market_regime=regime)
            else:
                base = dict(self._STRATEGY_WEIGHTS)
        except Exception:
            base = dict(self._STRATEGY_WEIGHTS)

        # Phase 9 blend: multiplicative overlay of allocator weights, capped
        # so a single hot strategy can't run away from the ensemble.
        try:
            quant = getattr(self.orchestrator, "quant", None)
            if quant and getattr(quant, "allocator", None):
                alloc_w = quant.allocator.weights() or {}
                if alloc_w:
                    blended: dict[str, float] = {}
                    for name, w in base.items():
                        # Allocator weight averages around 1/N; normalise to 1.0 baseline.
                        n = max(len(alloc_w), 1)
                        scale = alloc_w.get(name, 1.0 / n) * n
                        # Clamp scale to [0.5, 2.0] to bound drift
                        scale = max(0.5, min(2.0, scale))
                        blended[name] = w * scale
                    base = blended
        except Exception:
            pass

        # Phase E1: macro-regime overlay. When the cross-asset regime snapshot
        # says risk_off, downweight trend-following and upweight mean-reversion;
        # opposite on risk_on. Bounded so a single overlay can never zero a
        # strategy. Reads data/<profile>/regime_snapshot.json (already written
        # per cycle by the orchestrator). Strict opt-in via
        # trading.use_regime_overlay (default on).
        try:
            cfg_use = True
            try:
                cfg_use = bool(
                    self.orchestrator.config.get("trading", {})
                    .get("use_regime_overlay", True)
                )
            except Exception:
                cfg_use = True
            if cfg_use:
                regime = self._read_macro_regime()
                tilts = self._REGIME_STRATEGY_TILTS.get(regime)
                if tilts:
                    tilted: dict[str, float] = {}
                    for name, w in base.items():
                        # Per-strategy tilt clamped to [0.6, 1.4] inside the
                        # tilt table; safe to multiply directly.
                        tilted[name] = w * float(tilts.get(name, 1.0))
                    base = tilted
        except Exception:
            pass

        # Phase 1 (smarts): apply Thompson-sampled bandit weights as a
        # multiplicative overlay [0.5, 1.5] so existing weights are not
        # overridden — only re-weighted by recent performance.
        if bandit_weights:
            try:
                n = max(len(bandit_weights), 1)
                blended2: dict[str, float] = {}
                for name, w in base.items():
                    bw = bandit_weights.get(name, 1.0 / n)
                    scale = max(0.5, min(1.5, bw * n))
                    blended2[name] = w * scale
                base = blended2
            except Exception:
                pass

        return base

    # Macro-regime → per-strategy multiplier table. Keys must be a subset of
    # _STRATEGY_WEIGHTS. Values are bounded to [0.6, 1.4]. Unknown regimes
    # fall through to the neutral base weights.
    _REGIME_STRATEGY_TILTS: dict[str, dict[str, float]] = {
        "risk_off": {
            "ema_crossover": 0.7,        # trend-following struggles in chop
            "bollinger_reversion": 1.3,  # mean-reversion shines
        },
        "risk_on": {
            "ema_crossover": 1.3,        # let momentum run
            "bollinger_reversion": 0.7,  # reversion gets run over
        },
        "neutral": {
            "ema_crossover": 1.0,
            "bollinger_reversion": 1.0,
        },
    }

    def _read_macro_regime(self) -> str:
        """Read the macro regime from the per-profile snapshot file.

        Returns ``"unknown"`` when the snapshot is missing or malformed so
        callers fall back to neutral weights.
        """
        try:
            from pathlib import Path as _P
            profile = (
                getattr(self.orchestrator, "profile", None)
                or getattr(self.orchestrator.state, "profile", None)
                or "default"
            )
            p = _P("data") / str(profile).lower() / "regime_snapshot.json"
            if not p.exists():
                return "unknown"
            data = json.loads(p.read_text())
            return str(
                data.get("regime") or data.get("macro_regime") or "unknown"
            ).lower()
        except Exception:
            return "unknown"

    def _get_equity_event_str(self, exchange_name: str, pair: str) -> str:
        """Return a formatted equity event string for injection into agent prompts.

        Fetches the earnings / dividend calendar for the full watchlist (cached
        4 h) and returns a short text block for the given pair.  Returns "" for
        crypto profiles (exchange != "ibkr").
        """
        if exchange_name != "ibkr":
            return ""

        now = time.monotonic()
        if now - self._equity_calendar_ts > self._EQUITY_CALENDAR_TTL or not self._equity_calendar:
            try:
                from src.core.equity_feed import (
                    get_earnings_calendar,
                    get_dividend_calendar,
                    pair_to_yahoo,
                )
                orch = self.orchestrator
                tickers = [pair_to_yahoo(p) for p in orch.pairs]
                self._equity_calendar = {
                    "earnings": get_earnings_calendar(tickers),
                    "dividends": get_dividend_calendar(tickers),
                }
                self._equity_calendar_ts = now
                logger.debug(
                    f"Equity calendar refreshed: "
                    f"{len(self._equity_calendar['earnings'])} earnings, "
                    f"{len(self._equity_calendar['dividends'])} dividends"
                )
            except Exception as e:
                logger.debug(f"Equity calendar refresh failed (non-fatal): {e}")
                return ""

        try:
            from src.core.equity_feed import pair_to_yahoo
            ticker = pair_to_yahoo(pair)
            lines: list[str] = []

            earnings_info = self._equity_calendar.get("earnings", {}).get(ticker)
            if earnings_info:
                days = earnings_info["days_away"]
                date = earnings_info["earnings_date"]
                eps = (
                    f", est. EPS {earnings_info['eps_estimate']:.2f}"
                    if earnings_info.get("eps_estimate") is not None
                    else ""
                )
                urgency = "WARNING: " if days <= 3 else ("CAUTION: " if days <= 7 else "")
                lines.append(
                    f"{urgency}Earnings report in {days} day(s) ({date}{eps}). "
                    + ("Avoid new long positions — gap risk is HIGH." if days <= 3
                       else "Reduce position size or hold cash ahead of report." if days <= 7
                       else "Consider sizing down before earnings.")
                )

            div_info = self._equity_calendar.get("dividends", {}).get(ticker)
            if div_info:
                days = div_info["days_away"]
                date = div_info["ex_div_date"]
                annual = div_info.get("annual_dividend")
                yld = div_info.get("yield_pct")
                detail = ""
                if annual is not None and yld is not None:
                    detail = f" (div {annual:.2f}/yr, {yld:.1f}% yield)"
                lines.append(
                    f"Ex-dividend date in {days} day(s) ({date}{detail}). "
                    "Price typically drops by the dividend amount on ex-div date."
                )

            if lines:
                return "EQUITY EVENT RISK:\n" + "\n".join(f"- {l}" for l in lines)
        except Exception as e:
            logger.debug(f"Equity event string build failed for {pair}: {e}")
        return ""

    def _calibrate_confidence(self, raw_confidence: float, pair: str) -> float:
        """Run raw LLM confidence through the ALE calibrator.

        Returns calibrated value, or raw_confidence on any error.
        """
        try:
            lm = getattr(self.orchestrator, "learning_manager", None)
            if lm and lm.calibrator:
                return lm.calibrator.calibrate(raw_confidence, pair)
        except Exception:
            pass
        return raw_confidence

    def _persist_executor_drop(
        self,
        *,
        cycle_id: str,
        pair: str,
        exchange: str,
        stage: str,
        reason: str,
        risk_result: dict,
        details: dict | None = None,
    ) -> None:
        """Persist an ``executor`` reasoning span when a risk-approved trade
        is dropped between RiskManager and the actual exchange call (fee
        gate, pending Telegram approval, exec_failed).

        Without this, the dashboard sees ``[market_analyst, trader,
        risk_manager(approved)]`` with no trade row and can only render the
        generic "Risk manager approved but trade was not recorded." Now it
        can surface the real blocking stage and reason. Best-effort: never
        raises into the trading loop.
        """
        orch = self.orchestrator
        stats_db = getattr(orch, "stats_db", None)
        if not stats_db or not cycle_id:
            return
        try:
            payload = {
                "approved": False,
                "stage": stage,
                "reason": reason,
                "action": risk_result.get("action"),
                "pair": pair,
                "quote_amount": risk_result.get("quote_amount"),
                "confidence": risk_result.get("confidence"),
            }
            if details:
                payload["details"] = {
                    k: v for k, v in details.items()
                    if isinstance(v, (str, int, float, bool))
                }
            stats_db.save_reasoning(
                cycle_id=cycle_id,
                pair=pair,
                agent_name="executor",
                reasoning_json=payload,
                signal_type=risk_result.get("action") or "hold",
                confidence=float(risk_result.get("confidence") or 0),
                exchange=exchange,
            )
        except Exception as _e:  # pragma: no cover — diagnostics only
            logger.debug(f"Failed to persist executor drop span: {_e}")

    async def run_pipeline(self, pair: str) -> None:
        """Run the full analysis → strategy → risk → execute pipeline for a pair asynchronously."""
        # Unpack dependencies from orchestrator for brevity
        orch = self.orchestrator
        # Refresh cycle watchdog heartbeat: per-pair work means the cycle is
        # making progress even if the total wall-clock exceeds a single
        # watchdog window (e.g. screener + many pairs + slow LLM provider).
        try:
            orch.bump_heartbeat()
        except Exception:
            pass
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

        # For equity profiles, append per-pair event risk (earnings / ex-div) to
        # strategic_context so both the analyst and strategist see it.
        # Wrapped in to_thread because the 4-hourly cache refresh makes sync HTTP calls.
        _equity_event_str = await asyncio.to_thread(self._get_equity_event_str, exchange_name, pair)
        _effective_strategic_ctx = (
            (strategic_context + "\n\n" + _equity_event_str).strip()
            if _equity_event_str else strategic_context
        )

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
            exchange=exchange_name,
        ) if tracer else None

        # Data fetching (synchronous -> use asyncio.to_thread if we want true non-blocking,
        # but for now we let it block slightly since Coinbase REST is fast)
        _step_t = time.monotonic()
        granularity = orch.config.get("analysis", {}).get("technical", {}).get(
            "candle_granularity", "ONE_HOUR"
        )
        
        # C3 fix: Use threading.Lock with regular `with` block
        with self._candle_cache_lock:
            cached = self._candle_cache.get(pair)
            if cached and (time.monotonic() - cached[0]) < min(60.0, orch.interval * 0.9):
                candles = list(cached[1])
            else:
                cached = None  # mark as needing fetch
        
        if cached is None:
            await orch.rate_limiter.async_wait(orch.exchange.rate_limit_key)
            try:
                candles = await asyncio.to_thread(
                    orch.exchange.get_candles,
                    pair,
                    granularity=granularity,
                )
                with self._candle_cache_lock:
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

        # Articles count is tunable via the LLM optimizer (hot-reloaded, 30s cache)
        _articles_n = llm_optimizer.get(
            "articles_for_analysis",
            orch.config.get("news", {}).get("articles_for_analysis", 15),
        )
        news_headlines = "No news available."
        if orch.news:
            news_headlines = await asyncio.to_thread(
                orch.news.get_headlines, _articles_n
            )
        elif orch.redis:
            try:
                _news_profile = orch.config.get("trading", {}).get("exchange", "coinbase").lower()
                cached = orch.redis.get(f"news:{_news_profile}:latest") or orch.redis.get("news:latest")
                if cached:
                    articles = json.loads(cached)
                    news_headlines = "\n".join(
                        f"- [{a.get('source', '?')}] {a.get('title', '')}"
                        for a in articles[:_articles_n]
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
                    _news_profile = orch.config.get("trading", {}).get("exchange", "coinbase").lower()
                    cached = orch.redis.get(f"news:{_news_profile}:latest") or orch.redis.get("news:latest")
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
                    # C3 fix: Use threading.Lock with regular `with` block
                    with self._candle_cache_lock:
                        cached = self._candle_cache.get(p)
                        if cached and (time.monotonic() - cached[0]) < min(60.0, orch.interval * 0.9):
                            return list(cached[1])
                    async with _sem:
                        await orch.rate_limiter.async_wait(orch.exchange.rate_limit_key)
                        res = await asyncio.to_thread(orch.exchange.get_candles, p, granularity=granularity)
                        with self._candle_cache_lock:
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
            kelly_stats = await asyncio.to_thread(orch.stats_db.get_win_loss_stats, exchange=exchange_name)
        except Exception as e:
            logger.debug(f"Kelly stats unavailable: {e}")

        # Supplement with backtest data when live sample is too small
        if kelly_stats.get("sample_size", 0) < 20:
            try:
                bt_stats = await asyncio.to_thread(
                    orch.stats_db.get_backtest_kelly_stats, pair, exchange=exchange_name
                )
                if bt_stats.get("sample_size", 0) > 0 and bt_stats.get("win_rate", 0) > 0:
                    live_n = kelly_stats.get("sample_size", 0)
                    bt_n = bt_stats["sample_size"]
                    if live_n == 0:
                        # No live data — use backtest entirely
                        kelly_stats = bt_stats
                    else:
                        # Blend: weight live data more heavily (2x weight per trade)
                        live_w = live_n * 2
                        bt_w = bt_n
                        total_w = live_w + bt_w
                        kelly_stats = {
                            "win_rate": (kelly_stats["win_rate"] * live_w + bt_stats["win_rate"] * bt_w) / total_w,
                            "avg_win": (kelly_stats["avg_win"] * live_w + bt_stats["avg_win"] * bt_w) / total_w,
                            "avg_loss": (kelly_stats["avg_loss"] * live_w + bt_stats["avg_loss"] * bt_w) / total_w,
                            "sample_size": live_n,
                            "backtest_supplemented": True,
                        }
                    logger.info(
                        f"Kelly supplemented with backtest data for {pair}: "
                        f"live_n={live_n}, bt_n={bt_n}, blended_wr={kelly_stats['win_rate']:.2f}"
                    )
            except Exception as e:
                logger.debug(f"Backtest Kelly supplement unavailable for {pair}: {e}")

        recent_outcomes = ""
        try:
            _outcomes_n = llm_optimizer.get("recent_outcomes_n", 10)
            recent_outcomes = await asyncio.to_thread(
                orch.stats_db.get_recent_outcomes,
                pair, n=_outcomes_n, currency_symbol=orch.state.currency_symbol,
                exchange=orch.config.get("trading", {}).get("exchange", "").lower(),
            )
        except Exception as e:
            logger.debug(f"Failed to load recent outcomes: {e}")

        pair_accuracy = None
        try:
            pair_accuracy = await asyncio.to_thread(
                orch.stats_db.get_pair_accuracy_context, pair
            )
        except Exception as e:
            logger.debug(f"Prediction accuracy context unavailable for {pair}: {e}")

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

        # ALE: Inject learned prompt supplements
        _learned_context = ""
        try:
            lm = getattr(orch, "learning_manager", None)
            if lm and lm.prompt_evolver:
                _learned_context = lm.prompt_evolver.format_supplements("market_analyst")
        except Exception:
            pass

        analysis_result = await orch.market_analyst.execute({
            "pair": pair,
            "candles": candles,
            "news_headlines": news_headlines,
            "fear_greed": fg_prompt,
            "multi_timeframe": mtf_prompt,
            "sentiment": sentiment_prompt,
            "strategy_signals": strategy_signals,
            "strategic_context": _effective_strategic_ctx,
            "currency_symbol": orch.state.currency_symbol,
            "native_currency": orch.state.native_currency,
            "portfolio_value": _portfolio_value,
            "cash_balance": _cash_balance,
            "cycle_id": cycle_id,
            "stats_db": orch.stats_db,
            "trace_ctx": trace_ctx,
            "exchange": exchange_name,
            "learned_context": _learned_context,
        })

        signal = analysis_result.get("signal", {})
        _timings["analyst"] = time.monotonic() - _step_t

        # ─── ALE: Calibrate raw LLM confidence ───
        raw_conf = signal.get("confidence", 0)
        calibrated_conf = self._calibrate_confidence(raw_conf, pair)
        if calibrated_conf != raw_conf:
            signal["raw_confidence"] = raw_conf
            signal["confidence"] = calibrated_conf
            logger.debug(f"📐 {pair} confidence calibrated: {raw_conf:.3f} → {calibrated_conf:.3f}")

        # ─── Signal-type context (for weighted accuracy + risk sizing) ───
        signal_type = signal.get("signal_type", "neutral")
        weighted_acc: dict = {}
        signal_type_win_rate: float | None = None
        try:
            weighted_acc = await asyncio.to_thread(
                orch.stats_db.get_weighted_pair_accuracy, pair
            )
            by_type = weighted_acc.get("by_type", {})
            st_data = by_type.get(signal_type, {})
            wt_pct = st_data.get("weighted_accuracy_pct")
            if wt_pct is not None:
                signal_type_win_rate = wt_pct / 100.0
        except Exception as _e:
            logger.debug(f"Weighted accuracy unavailable for {pair}: {_e}")

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
        # Read telegram config fresh from disk so dashboard saves take effect
        # immediately without requiring a restart.
        from src.utils.settings_manager import load_settings as _load_cfg
        tg_cfg = _load_cfg().get("telegram", {})
        notify_threshold = tg_cfg.get("notify_on_signal_confidence", 0.65)
        if tg_cfg.get("notify_on_signal", True) and confidence >= notify_threshold and orch.telegram:
            # Cycle-3 fix: find the signal for THIS pair instead of signals[-1]
            # which races with concurrent pipelines via asyncio.gather.
            signal_obj = next(
                (s for s in reversed(orch.state.signals) if s.pair == pair), None
            )
            if signal_obj:
                orch.telegram.send_signal_notification(signal_obj.to_summary())

        # Step 2.5: Catalyst Pattern Engine (deterministic, no-LLM).
        # Surfaces an upcoming-event-driven pattern_signal for the strategist
        # and risk manager. Always runs but no-ops when no catalyst is in
        # range or when there are too few historical analogs.
        pattern_signal: dict = {"available": False, "reason": "not_run"}
        try:
            _pattern_sentiment = _read_sentiment_score(exchange_name)
            pattern_result = await orch.pattern_agent.execute({
                "pair": pair,
                "exchange": exchange_name,
                "stats_db": orch.stats_db,
                "cycle_id": cycle_id,
                "sentiment_score": _pattern_sentiment,
            })
            pattern_signal = pattern_result.get("pattern_signal", pattern_signal)
        except Exception as _pat_e:
            logger.debug(f"PatternAgent failed for {pair}: {_pat_e}")

        # Step 2.55: Cross-Asset Engine — reactive (driver events about to
        # fire on correlated symbols) + proactive (cluster-mate catalyst
        # density). Always best-effort; failures NEVER halt the pipeline.
        cross_asset_signal: dict = {"available": False, "reason": "not_run"}
        try:
            cross_asset_result = await orch.cross_asset_agent.execute({
                "pair": pair,
                "exchange": exchange_name,
                "stats_db": orch.stats_db,
                "cycle_id": cycle_id,
            })
            cross_asset_signal = cross_asset_result.get(
                "cross_asset_signal", cross_asset_signal,
            )
        except Exception as _ca_e:
            logger.debug(f"CrossAssetAgent failed for {pair}: {_ca_e}")

        # Step 2.56: Smarts decision-time priors — best-effort, all isolated.
        # ``news_knn_prior``: pgvector kNN over news_articles → forward-drift
        # ``lead_lag``: significant leaders for this follower
        # ``upcoming_events``: ≤72h forward catalysts
        # ``onchain_now``: latest BTC dominance / stablecoin supply snapshot
        smarts_prior: dict = {"available": False, "reason": "not_run"}
        try:
            from src.utils.news_knn import news_knn_prior
            from datetime import datetime as _dt, timedelta as _td
            # Grab a small sample of headlines for this pair as the kNN query.
            try:
                _articles = orch.stats_db.get_news_for_pair(
                    pair=pair, exchange=exchange_name, limit=5,
                ) if hasattr(orch.stats_db, "get_news_for_pair") else []
            except Exception:
                _articles = []
            _query = " ".join(
                (a.get("title") or "")[:200] for a in (_articles or [])[:5]
            ).strip() or pair
            smarts_prior = news_knn_prior(
                orch.stats_db,
                exchange=exchange_name,
                pair=pair,
                query_text=_query,
                horizon_hours=24,
                k=10,
            )
        except Exception as _kp_e:
            logger.debug(f"news_knn_prior failed for {pair}: {_kp_e}")

        lead_lag_signals: list[dict] = []
        try:
            if hasattr(orch.stats_db, "get_lead_lag_for"):
                lead_lag_signals = orch.stats_db.get_lead_lag_for(
                    exchange_name, pair, min_abs_t=2.0,
                )[:5]
        except Exception as _ll_e:
            logger.debug(f"lead_lag fetch failed for {pair}: {_ll_e}")

        upcoming_events: list[dict] = []
        try:
            if hasattr(orch.stats_db, "get_upcoming_events"):
                upcoming_events = orch.stats_db.get_upcoming_events(
                    exchange_name, symbol=pair, within_hours=72, min_importance=2,
                )
        except Exception as _ue_e:
            logger.debug(f"upcoming_events fetch failed for {pair}: {_ue_e}")

        onchain_now: dict = {"available": False}
        try:
            if hasattr(orch.stats_db, "get_recent_onchain"):
                _btc_dom = orch.stats_db.get_recent_onchain(
                    exchange_name, "BTC", "market_cap_dominance_pct", limit=2,
                )
                _usdt = orch.stats_db.get_recent_onchain(
                    exchange_name, "USDT", "supply_usd", limit=2,
                )
                if _btc_dom or _usdt:
                    onchain_now = {
                        "available": True,
                        "btc_dominance_pct": (
                            _btc_dom[0].get("value") if _btc_dom else None
                        ),
                        "usdt_supply_usd": (
                            _usdt[0].get("value") if _usdt else None
                        ),
                    }
        except Exception as _oc_e:
            logger.debug(f"onchain fetch failed: {_oc_e}")

        # Step 2.6: Regression factor — turns nightly OLS fits into a real
        # bounded sizing multiplier (formerly observational only). Strict
        # opt-in via REGRESSION_RISK_FACTOR_ENABLED env or per-profile
        # ``risk.use_regression_factor: true``. The payload is shaped so
        # ``risk_manager`` can apply it without re-querying the DB.
        regression_factor: dict = {
            "available": False, "applied": False, "factor": 1.0,
            "direction": "neutral", "reason": "not_run",
            "model": None, "upcoming_event": None,
        }
        try:
            from src.analysis.regression_factor import build_regression_factor
            _risk_cfg = (orch.config or {}).get("risk", {}) if hasattr(orch, "config") else {}
            regression_factor = build_regression_factor(
                db=orch.stats_db,
                exchange=exchange_name,
                symbol=pair,
                risk_config=_risk_cfg,
            )
            if regression_factor.get("applied"):
                logger.info(
                    f"[regression_factor] {pair} factor={regression_factor['factor']:.3f} "
                    f"({regression_factor['direction']}) — model R²="
                    f"{(regression_factor.get('model') or {}).get('r_squared'):.2f}"
                )
        except Exception as _rf_e:
            logger.debug(f"regression_factor build failed for {pair}: {_rf_e}")

        # Promote pattern_engine to a first-class ensemble contributor so the
        # ensemble vote (and downstream DecisionEngine agreement gate) factor
        # in catalyst-pattern direction.
        if isinstance(pattern_signal, dict) and pattern_signal.get("available"):
            _direction = pattern_signal.get("direction", "neutral")
            _action = "buy" if _direction == "bullish" else (
                "sell" if _direction == "bearish" else "hold"
            )
            strategy_signals["pattern_engine"] = {
                "action": _action,
                "confidence": float(pattern_signal.get("confidence", 0.0) or 0.0),
                "market_regime": pattern_signal.get("regime", "unknown"),
                "reasoning": (
                    f"catalyst pattern {_direction} "
                    f"(matches={pattern_signal.get('n_matches', 0)})"
                ),
            }
            # Re-run ensemble with the pattern member included.
            _ens2 = self._compute_ensemble({
                k: v for k, v in strategy_signals.items() if not k.startswith("_")
            })
            if _ens2:
                strategy_signals["_ensemble"] = _ens2

        # Step 2.7: Quant Analytics context — snapshot the latest rows from
        # all five quant tables (factor loadings, HAR-RV, Granger, slippage
        # model, correlation regime) for this pair. Injected into both the
        # strategist payload and the trader payload so the LLM can reason
        # over them, and persisted into ``quant_decision_snapshots`` so the
        # learning loop can later attribute realised PnL back to the quant
        # signals that were active at decision time.
        quant_context: dict = {"available": False}
        try:
            from src.utils.quant_context import (
                build_quant_context,
                format_decision_explanation,
            )
            quant_context = build_quant_context(
                orch.stats_db, exchange_name, pair,
            )
            if quant_context.get("available"):
                quant_context["explanations"] = format_decision_explanation(
                    quant_context,
                )
        except Exception as _qc_e:
            logger.debug(f"quant_context build failed for {pair}: {_qc_e}")

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

        # Build strategist payload up front but defer the call. When the
        # TraderAgent is enabled and succeeds, its output overwrites
        # strategy_result anyway — so invoking the strategist eagerly is pure
        # waste (an LLM round-trip, prompt build, reasoning-log write, and
        # Langfuse span whose result is immediately discarded). We invoke the
        # strategist lazily, only when there is no TraderAgent or it failed.
        _strategist_inputs = {
            "signal": signal,
            "active_tasks": [t.to_dict() for t in orch.active_tasks if not t.completed],
            "current_balance": orch.exchange.balance if hasattr(orch.exchange, 'balance') else {},
            "open_positions": orch.state.open_positions,
            "recent_trades": [t.to_summary() for t in orch.state.recent_trades],
            "recent_outcomes": recent_outcomes,
            "strategic_context": _effective_strategic_ctx,
            "live_holdings_summary": orch.state.holdings_summary,
            "native_currency": orch.state.native_currency,
            "currency_symbol": orch.state.currency_symbol,
            "portfolio_value": _portfolio_value,
            "cash_balance": _cash_balance,
            "sentiment": sentiment_data,
            "strategy_signals": strategy_signals,
            "confidence_adjustment": pair_confidence_adj,
            "prediction_accuracy": _build_accuracy_ctx(pair_accuracy, weighted_acc),
            "fee_context": fee_context,
            "cycle_id": cycle_id,
            "stats_db": orch.stats_db,
            "trace_ctx": trace_ctx,
            "exchange": exchange_name,
            "pattern_signal": pattern_signal,
            "regression_factor": regression_factor,
            "cross_asset_signal": cross_asset_signal,
            "news_knn_prior": smarts_prior,
            "lead_lag_signals": lead_lag_signals,
            "upcoming_events": upcoming_events,
            "onchain_now": onchain_now,
            "quant_context": quant_context,
            "har_rv_forecast": (
                (quant_context.get("har_rv") or {}).get("forecast_vol")
                if isinstance(quant_context, dict) else None
            ),
        }

        strategy_result: dict | None = None

        # ─── TraderAgent: autonomous LLM operator over deterministic toolkit ─────
        # Live decision-maker. Routes every proposal through the deterministic
        # DecisionEngine (edge library, allocator, rules). When this succeeds
        # the strategist is short-circuited entirely.
        trader = getattr(orch, "trader", None)
        if trader is not None:
            try:
                _regime = (signal.get("market_condition") or "unknown")
                trader_result = await trader.execute({
                    "pair": pair,
                    "exchange": exchange_name,
                    "regime": _regime,
                    "current_price": price,
                    "market_signal": signal,
                    "strategy_signals": strategy_signals,
                    "pattern_signal": pattern_signal,
                    "regression_factor": regression_factor,
                    "cross_asset_signal": cross_asset_signal,
                    "news_knn_prior": smarts_prior,
                    "lead_lag_signals": lead_lag_signals,
                    "upcoming_events": upcoming_events,
                    "onchain_now": onchain_now,
                    "quant_context": quant_context,
                    "har_rv_forecast": (
                        (quant_context.get("har_rv") or {}).get("forecast_vol")
                        if isinstance(quant_context, dict) else None
                    ),
                    "sentiment": sentiment_data,
                    "news_headlines": news_headlines,
                    "fee_context": fee_context,
                    "kelly_stats": kelly_stats,
                    "recent_outcomes": recent_outcomes,
                    "strategic_context": _effective_strategic_ctx,
                    "portfolio_value": _portfolio_value,
                    "cash_balance": _cash_balance,
                    "open_positions": orch.state.open_positions,
                    "edges": getattr(orch.quant, "edges", None) if orch.quant else None,
                    "allocator": getattr(orch.quant, "allocator", None) if orch.quant else None,
                    "cycle_id": cycle_id,
                    "stats_db": orch.stats_db,
                    "trace_ctx": trace_ctx,
                })
                if trader_result and not trader_result.get("error"):
                    strategy_result = trader_result
                    logger.info(
                        f"🧠 TraderAgent decision for {pair}: "
                        f"{trader_result.get('action', '?').upper()} "
                        f"(verdict approved="
                        f"{trader_result.get('decision_engine_verdict', {}).get('approved')})"
                    )
            except Exception as _trader_e:
                logger.warning(
                    f"TraderAgent failed for {pair}: {_trader_e} — falling back to strategist"
                )

        # Strategist fallback: only invoke when TraderAgent is absent or did
        # not return a usable decision. Prevents the wasted LLM call that
        # previously ran on every cycle just to be discarded.
        if strategy_result is None:
            strategy_result = await orch.strategist.execute(_strategist_inputs)

        # Persist a point-in-time snapshot of the quant signals that
        # informed this decision so the self-learning loop can attribute
        # realised PnL back to each quant feature later. Best-effort.
        try:
            if (
                isinstance(quant_context, dict)
                and quant_context.get("available")
                and hasattr(orch.stats_db, "insert_quant_decision_snapshot")
            ):
                import json as _json
                _har = quant_context.get("har_rv") or {}
                _cr = quant_context.get("correlation_regime") or {}
                _granger = quant_context.get("granger_leaders") or []
                orch.stats_db.insert_quant_decision_snapshot({
                    "exchange": exchange_name,
                    "cycle_id": cycle_id,
                    "pair": pair,
                    "har_rv_forecast": _har.get("forecast_vol"),
                    "har_rv_realized": _har.get("realized_vol_daily"),
                    "factor_alpha": quant_context.get("factor_alpha_annualised"),
                    "idio_vol": quant_context.get("idio_vol"),
                    "granger_leader_count": len(_granger),
                    "slippage_bps_pred": None,  # filled in by executor when available
                    "corr_regime": _cr.get("regime"),
                    "corr_z_score": _cr.get("z_score"),
                    "action": (strategy_result or {}).get("action"),
                    "confidence": (strategy_result or {}).get("confidence"),
                    "quant_context_json": _json.dumps(quant_context, default=str),
                })
        except Exception as _qs_e:
            logger.debug(f"quant_decision_snapshot persist failed: {_qs_e}")

        # Surface quant explanations in the decision output so the
        # downstream audit log + dashboard reasoning view can show the
        # "why" alongside the action.
        try:
            if isinstance(strategy_result, dict) and isinstance(quant_context, dict):
                _exps = quant_context.get("explanations") or []
                if _exps:
                    strategy_result.setdefault("quant_explanations", list(_exps))
        except Exception:
            pass

        # Phase 8: shadow strategist — fire-and-forget variant logging.
        # Never blocks the live decision and never executes a trade.
        try:
            shadow = getattr(orch, "shadow_strategist", None)
            if shadow is not None and getattr(shadow, "variants", None):
                asyncio.create_task(shadow.shadow(
                    cycle_id=cycle_id,
                    pair=pair,
                    live_action=strategy_result.get("action"),
                    live_confidence=strategy_result.get("confidence"),
                    context=_strategist_inputs,
                ))
        except Exception as _shd_e:
            logger.debug(f"shadow strategist dispatch failed: {_shd_e}")

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
            # Signal strength context for position sizing
            "signal_type": signal_type,
            "signal_type_win_rate": signal_type_win_rate,
            # Phase 13: vol-target sizing — pass the recent close-to-close
            # returns so RiskManager can compute target_vol / realised_vol.
            "recent_returns": _candle_returns(candles, lookback=30),
            # Catalyst Pattern Engine signal — used as an advisory size
            # multiplier (never overrides AbsoluteRules).
            "pattern_signal": pattern_signal,
            # Regression factor — bounded sizing multiplier sourced from
            # the nightly OLS event-price fits (no-op when disabled or
            # no imminent matching catalyst). See analysis/regression_factor.py.
            "regression_factor": regression_factor,
            # Cross-asset signal — reactive driver-event drift estimate
            # plus proactive cluster-mate catalyst density. Advisory only.
            "cross_asset_signal": cross_asset_signal,
            # Phase 6/9: per-profile quant substrate so the risk manager
            # can honour the capital-allocator's strategy budget when the
            # proposal didn't already carry one.
            "quant": getattr(orch, "quant", None),
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
            _proposed_action = strategy_result.get("action", "unknown")
            _reject_reason = risk_result.get("reason", "Unknown")
            logger.info(f"⏱️ Pipeline {pair}: {_parts} total={_total:.1f}s [rejected]")
            if _proposed_action == "buy":
                # Promote BUY rejections to a structured WARN so they're easy
                # to grep and feed the per-cycle BUY_DROPPED summary.
                logger.warning(
                    f"🚫 BUY_DROPPED [risk] {pair}: {_reject_reason} | "
                    f"confidence={strategy_result.get('confidence', 0):.2f}"
                )
                try:
                    orch.record_buy_drop(
                        pair, "risk", _reject_reason,
                        amount=strategy_result.get("quote_amount") or strategy_result.get("usd_amount"),
                        confidence=strategy_result.get("confidence"),
                        details={"violations": ",".join(risk_result.get("violations", []) or [])},
                    )
                except Exception:
                    pass
            else:
                logger.info(f"🚫 {pair}: Trade rejected — {_reject_reason}")
            orch.journal.log_decision("trade_rejected", pair, _proposed_action, {
                "reason": _reject_reason,
                "proposal": strategy_result,
            })
            orch.audit.log_rule_check("risk_validation", passed=False, details=_reject_reason)
            if trace_ctx is not None:
                trace_ctx.finish(metadata={"action": "rejected", "reason": _reject_reason})
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
                # Use maker fee tier for the gate when execution is
                # configured maker-only (limit orders). Halves the assumed
                # round-trip cost (0.40% × 2 vs 0.60% × 2 on Coinbase),
                # which matches what we actually pay.
                _exec_cfg = orch.config.get("execution", {})
                _is_maker = bool(
                    _exec_cfg.get("maker_only", False)
                    or _exec_cfg.get("use_limit_orders", False)
                )
                worthwhile, fee_est = orch.fee_manager.is_trade_worthwhile(
                    quote_amount=trade_amount,
                    expected_gain_pct=expected_gain_pct,
                    is_swap=False,
                    portfolio_value=_portfolio_value,
                    is_maker=_is_maker,
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
                                is_maker=_is_maker,
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
                        _fg_reason = (
                            f"Fees > expected gain (expected={expected_gain_pct*100:.2f}%, "
                            f"breakeven={fee_est.breakeven_move_pct*100:.2f}%)"
                        )
                        if _trade_action == "buy":
                            logger.warning(
                                f"🚫 BUY_DROPPED [fee_gate] {pair}: {_fg_reason} | "
                                f"amount={trade_amount:.2f}"
                            )
                            try:
                                orch.record_buy_drop(
                                    pair, "fee_gate", _fg_reason,
                                    amount=trade_amount,
                                    confidence=risk_result.get("confidence"),
                                    details={
                                        "expected_gain_pct": expected_gain_pct,
                                        "breakeven_pct": fee_est.breakeven_move_pct,
                                    },
                                )
                            except Exception:
                                pass
                        else:
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
                        self._persist_executor_drop(
                            cycle_id=cycle_id,
                            pair=pair,
                            exchange=exchange_name,
                            stage="fee_gate",
                            reason=_fg_reason,
                            risk_result=risk_result,
                            details={
                                "trade_amount": float(trade_amount),
                                "expected_gain_pct": float(expected_gain_pct),
                                "breakeven_pct": float(fee_est.breakeven_move_pct),
                            },
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
            if risk_result.get("action") == "buy":
                logger.warning(
                    f"🚫 BUY_DROPPED [pending_approval] {pair}: "
                    f"Awaiting Telegram approval (trade_id={trade_id})"
                )
                try:
                    orch.record_buy_drop(
                        pair, "pending_approval",
                        f"Awaiting Telegram approval (trade_id={trade_id})",
                        amount=risk_result.get("quote_amount"),
                        confidence=risk_result.get("confidence"),
                    )
                except Exception:
                    pass
            self._persist_executor_drop(
                cycle_id=cycle_id,
                pair=pair,
                exchange=exchange_name,
                stage="pending_approval",
                reason=f"Awaiting Telegram approval (trade_id={trade_id})",
                risk_result=risk_result,
                details={"pending_trade_id": trade_id},
            )
            if trace_ctx is not None:
                trace_ctx.finish(metadata={"action": "pending_approval", "trade_id": trade_id})
            return

        # Step 6: Execute Trade
        _step_t = time.monotonic()
        exec_result = await orch.executor.execute({
            "approved_trade": risk_result,
            "cross_asset_signal": cross_asset_signal,
            "news_knn_prior": smarts_prior,
            "lead_lag_signals": lead_lag_signals,
            "upcoming_events": upcoming_events,
            "cycle_id": cycle_id,
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
                # Compute deterministic entry_score from ensemble for feedback loop
                _entry_score = None
                _ens = strategy_signals.get("_ensemble")
                if _ens and isinstance(_ens, dict):
                    _entry_score = _ens.get("confidence")

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
                    entry_score=_entry_score,
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
            # severity="trade" triggers an instant Telegram message via ProactiveEngine
            # (no second send_trade_notification needed — that was a duplicate).
            orch.chat_handler.queue_event(
                f"Trade executed: {trade_event}", severity="trade", pair=pair
            )

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
            if risk_result.get("action") == "buy":
                logger.warning(
                    f"🚫 BUY_DROPPED [exec_failed] {pair}: {exec_error}"
                )
                try:
                    orch.record_buy_drop(
                        pair, "exec_failed", str(exec_error),
                        amount=risk_result.get("quote_amount"),
                        confidence=risk_result.get("confidence"),
                    )
                except Exception:
                    pass
            self._persist_executor_drop(
                cycle_id=cycle_id,
                pair=pair,
                exchange=exchange_name,
                stage="exec_failed",
                reason=str(exec_error),
                risk_result=risk_result,
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
