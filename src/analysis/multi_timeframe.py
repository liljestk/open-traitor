"""
Multi-Timeframe Analysis — Checks signal alignment across timeframes.
Dramatically reduces false signals by requiring confluence.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

from src.analysis.technical import TechnicalAnalyzer
from src.utils.logger import get_logger
from src.utils.rate_limiter import get_rate_limiter

logger = get_logger("analysis.multi_tf")


# Timeframes in order of increasing significance
# NOTE: Coinbase Advanced Trade API does not support FOUR_HOUR granularity.
# We fetch TWO_HOUR candles (2x the requested count) and aggregate pairs
# into 4h candles inside _fetch_timeframe().
TIMEFRAMES = [
    {"name": "15m", "granularity": "FIFTEEN_MINUTE", "weight": 0.15, "candles": 100},
    {"name": "1h", "granularity": "ONE_HOUR", "weight": 0.35, "candles": 200},
    {"name": "4h", "granularity": "TWO_HOUR", "weight": 0.30, "candles": 200, "aggregate": 2},
    {"name": "1d", "granularity": "ONE_DAY", "weight": 0.20, "candles": 60},
]


class MultiTimeframeAnalyzer:
    """
    Analyzes a trading pair across multiple timeframes and produces
    a confluence score. Trades are only taken when timeframes align.
    """

    # Cache TTL per granularity (seconds) — avoids redundant API calls
    _CACHE_TTL = {
        "FIFTEEN_MINUTE": 120,
        "ONE_HOUR": 300,
        "TWO_HOUR": 600,
        "SIX_HOUR": 900,
        "ONE_DAY": 1800,
    }

    def __init__(self, config: dict, coinbase_client=None):
        self.config = config
        self.exchange = coinbase_client  # may be CoinbaseClient or IBClient
        self.technical = TechnicalAnalyzer(config.get("analysis", {}).get("technical", {}))
        self.rate_limiter = get_rate_limiter()
        self._candle_cache: dict[str, tuple[float, list[dict]]] = {}  # key -> (timestamp, candles)
        self._cache_lock = __import__('threading').Lock()  # guard concurrent cache writes

    def _fetch_timeframe(
        self, pair: str, tf: dict
    ) -> tuple[str, dict, float]:
        """Fetch and analyse a single timeframe.

        Returns (tf_name, result_dict, weighted_score_contribution).
        Thread-safe: rate-limiter and cache access are both protected internally.

        When the timeframe definition includes an ``aggregate`` key (e.g.
        ``"aggregate": 2``), the raw candles are merged in groups of N to
        synthesise a higher timeframe (e.g. 2×TWO_HOUR → 4h candles).
        """
        _rl_key = self.exchange.rate_limit_key if self.exchange else "coinbase_rest"
        self.rate_limiter.wait(_rl_key)
        cache_key = f"{pair}:{tf['granularity']}"
        ttl = self._CACHE_TTL.get(tf["granularity"], 300)
        now = time.time()

        with self._cache_lock:
            cached = self._candle_cache.get(cache_key)

        if cached and (now - cached[0]) < ttl:
            candles = cached[1]
        else:
            candles = self.exchange.get_candles(
                product_id=pair,
                granularity=tf["granularity"],
                limit=tf["candles"],
            )
            with self._cache_lock:
                self._candle_cache[cache_key] = (now, candles)

        # Aggregate smaller candles into larger bars when requested
        agg_factor = tf.get("aggregate", 1)
        if agg_factor > 1 and candles:
            candles = self._aggregate_candles(candles, agg_factor)

        analysis = self.technical.analyze(candles)

        if "error" in analysis:
            return tf["name"], {"error": analysis["error"]}, 0.0

        tf_score = self._score_timeframe(analysis)
        result = {
            "score": tf_score,
            "signal": self._score_to_signal(tf_score),
            "rsi": analysis["indicators"].get("rsi"),
            "macd_signal": analysis["indicators"].get("macd_signal"),
            "ema_signal": analysis["indicators"].get("ema_signal"),
            "bb_signal": analysis["indicators"].get("bb_signal"),
            "price": analysis["current_price"],
        }
        return tf["name"], result, tf_score * tf["weight"]

    def analyze(self, pair: str) -> dict:
        """
        Analyze a pair across all timeframes.

        Returns:
            {
                "confluence_score": -1.0 to 1.0,
                "confluence_signal": "strong_buy"|"buy"|"neutral"|"sell"|"strong_sell",
                "timeframes": {
                    "15m": {...},
                    "1h": {...},
                    ...
                },
                "aligned": bool,  # True if all TFs agree
                "summary": str,
            }
        """
        if not self.exchange:
            return {"error": "Exchange client not available", "confluence_score": 0}

        tf_results = {}
        weighted_score = 0.0

        # Fetch and analyse all timeframes concurrently.
        # Each worker rate-limits itself so Coinbase quotas are respected,
        # but the *wait* times overlap instead of stacking sequentially.
        # Timeout per-future: prevents indefinite hangs when upstream
        # data sources (e.g. IB Gateway) stop responding.
        _FUTURE_TIMEOUT = 45  # seconds per timeframe fetch

        # IMPORTANT: Do NOT use `with ThreadPoolExecutor(...)` here.
        # If as_completed() raises TimeoutError, __exit__ calls shutdown(wait=True)
        # which blocks forever if workers are stuck on blocking I/O (e.g. IB Gateway).
        pool = ThreadPoolExecutor(max_workers=len(TIMEFRAMES), thread_name_prefix="mtf")
        try:
            futures = {pool.submit(self._fetch_timeframe, pair, tf): tf for tf in TIMEFRAMES}
            try:
                for future in as_completed(futures, timeout=_FUTURE_TIMEOUT * len(TIMEFRAMES)):
                    tf_def = futures[future]
                    try:
                        name, result, score_contrib = future.result(timeout=_FUTURE_TIMEOUT)
                        tf_results[name] = result
                        weighted_score += score_contrib
                    except TimeoutError:
                        name = tf_def["name"]
                        logger.warning(f"Multi-TF {name} timed out after {_FUTURE_TIMEOUT}s")
                        tf_results[name] = {"error": "timeout"}
                    except Exception as e:
                        name = tf_def["name"]
                        logger.warning(f"Multi-TF analysis failed for {name}: {e}")
                        tf_results[name] = {"error": str(e)}
            except TimeoutError:
                logger.warning(f"Multi-TF as_completed timed out for {pair} — skipping remaining timeframes")
                for future, tf_def in futures.items():
                    if tf_def["name"] not in tf_results:
                        tf_results[tf_def["name"]] = {"error": "timeout"}
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        # Determine confluence
        valid_scores = [
            r["score"] for r in tf_results.values() if isinstance(r.get("score"), (int, float))
        ]

        aligned = False
        if len(valid_scores) >= 3:
            # All scores have same sign = alignment
            all_bullish = all(s > 0.1 for s in valid_scores)
            all_bearish = all(s < -0.1 for s in valid_scores)
            aligned = all_bullish or all_bearish

        confluence_signal = self._score_to_signal(weighted_score)

        # Build summary
        summary_parts = []
        for tf_name, result in tf_results.items():
            if "score" in result:
                summary_parts.append(f"{tf_name}: {result['signal']} ({result['score']:+.2f})")
            else:
                summary_parts.append(f"{tf_name}: N/A")

        summary = (
            f"Multi-TF: {confluence_signal} (score: {weighted_score:+.2f}) | "
            f"Aligned: {'✅ YES' if aligned else '❌ NO'} | "
            + " | ".join(summary_parts)
        )

        logger.info(f"📊 {pair} {summary}")

        return {
            "confluence_score": weighted_score,
            "confluence_signal": confluence_signal,
            "timeframes": tf_results,
            "aligned": aligned,
            "summary": summary,
        }

    @staticmethod
    def _aggregate_candles(candles: list[dict], factor: int) -> list[dict]:
        """Merge *factor* consecutive candles into one higher-TF bar.

        Expects candles sorted oldest-first (ascending timestamp).  Each
        output bar has the open of the first sub-bar, close of the last,
        high = max(highs), low = min(lows), volume = sum(volumes).
        """
        if factor <= 1 or not candles:
            return candles

        # Ensure oldest-first ordering (Coinbase returns newest-first)
        # Detect by comparing first and last timestamps
        try:
            first_ts = float(candles[0].get("start", 0))
            last_ts = float(candles[-1].get("start", 0))
            if first_ts > last_ts:
                candles = list(reversed(candles))
        except (ValueError, TypeError):
            pass

        aggregated: list[dict] = []
        for i in range(0, len(candles) - factor + 1, factor):
            group = candles[i : i + factor]
            try:
                agg = {
                    "start": group[0].get("start", ""),
                    "open": group[0].get("open", "0"),
                    "close": group[-1].get("close", "0"),
                    "high": str(max(float(c.get("high", 0)) for c in group)),
                    "low": str(min((float(c.get("low", 0)) for c in group if float(c.get("low", 0)) > 0), default=0)),
                    "volume": str(sum(float(c.get("volume", 0)) for c in group)),
                }
                aggregated.append(agg)
            except (ValueError, TypeError):
                continue

        return aggregated

    def _score_timeframe(self, analysis: dict) -> float:
        """
        Score a single timeframe analysis from -1.0 (strong sell) to 1.0 (strong buy).
        """
        indicators = analysis.get("indicators", {})
        score = 0.0

        # RSI scoring
        rsi = indicators.get("rsi", 50)
        if rsi < 25:
            score += 0.3   # Very oversold = buy
        elif rsi < 35:
            score += 0.15
        elif rsi > 75:
            score -= 0.3   # Very overbought = sell
        elif rsi > 65:
            score -= 0.15

        # MACD scoring
        macd_signal = indicators.get("macd_signal", "neutral")
        macd_map = {
            "bullish_crossover": 0.25,
            "bullish": 0.1,
            "bearish_crossover": -0.25,
            "bearish": -0.1,
        }
        score += macd_map.get(macd_signal, 0)

        # EMA scoring
        ema_signal = indicators.get("ema_signal", "neutral")
        if "bullish" in str(ema_signal).lower():
            score += 0.15
        elif "bearish" in str(ema_signal).lower():
            score -= 0.15

        # Bollinger Band scoring
        bb_signal = indicators.get("bb_signal", "neutral")
        if bb_signal == "oversold":
            score += 0.2
        elif bb_signal == "overbought":
            score -= 0.2

        # Volume confirmation
        volume_ratio = indicators.get("volume_ratio", 1.0)
        if volume_ratio > 1.5:
            score *= 1.2  # Volume confirms move
        elif volume_ratio < 0.5:
            score *= 0.8  # Low volume = less conviction

        return max(-1.0, min(1.0, score))

    def _score_to_signal(self, score: float) -> str:
        """Convert a numerical score to a signal string."""
        if score >= 0.5:
            return "strong_buy"
        elif score >= 0.2:
            return "buy"
        elif score >= 0.05:
            return "weak_buy"
        elif score <= -0.5:
            return "strong_sell"
        elif score <= -0.2:
            return "sell"
        elif score <= -0.05:
            return "weak_sell"
        return "neutral"

    def get_for_prompt(self, pair: str) -> str:
        """Get a formatted multi-TF summary for LLM consumption."""
        result = self.analyze(pair)
        if "error" in result:
            return f"Multi-timeframe analysis unavailable: {result['error']}"

        lines = [
            f"Multi-Timeframe Confluence: {result['confluence_signal'].replace('_', ' ').upper()} "
            f"(score: {result['confluence_score']:+.2f})",
            f"Timeframe Alignment: {'YES — all timeframes agree' if result['aligned'] else 'NO — mixed signals'}",
        ]
        for tf_name, tf_data in result["timeframes"].items():
            if "score" in tf_data:
                rsi_val = tf_data.get('rsi')
                rsi_str = f"{rsi_val:.0f}" if rsi_val is not None else "N/A"
                lines.append(
                    f"  {tf_name}: {tf_data['signal']} | RSI: {rsi_str} | "
                    f"MACD: {tf_data.get('macd_signal', 'N/A')} | EMA: {tf_data.get('ema_signal', 'N/A')}"
                )
        return "\n".join(lines)
