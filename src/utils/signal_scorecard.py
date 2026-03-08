"""
Signal Scorecard — Scores past predictions against actual outcomes.

Runs after each pipeline cycle to backfill scores for recently-matured
predictions (≥ horizon hours old, still unscored).  Computes per-agent,
per-pair, per-market-condition rolling accuracy and factor attribution.

New DB table: ``signal_scores``
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from src.utils.logger import get_logger

logger = get_logger("utils.signal_scorecard")

# Horizons at which predictions are scored (hours)
SCORE_HORIZONS: list[int] = [1, 4, 24]

# Maximum predictions to backfill per call (avoid long-running queries)
_MAX_BACKFILL_BATCH = 200


class SignalScorecard:
    """Scores agent predictions against actual price movements.

    Lifecycle:
        scorecard = SignalScorecard(stats_db)
        scorecard.backfill_scores()                    # run every cycle
        acc = scorecard.get_rolling_accuracy("BTC-USD") # query anytime
        attr = scorecard.get_factor_attribution()       # weekly
    """

    def __init__(self, stats_db):
        self._db = stats_db

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @staticmethod
    def create_table_sql() -> str:
        return """
        CREATE TABLE IF NOT EXISTS signal_scores (
            id SERIAL PRIMARY KEY,
            reasoning_id INTEGER NOT NULL,
            agent_name TEXT NOT NULL,
            pair TEXT NOT NULL,
            exchange TEXT NOT NULL DEFAULT 'coinbase',
            prediction_ts TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            raw_confidence REAL NOT NULL DEFAULT 0,
            calibrated_confidence REAL DEFAULT NULL,
            market_condition TEXT DEFAULT '',
            horizon_hours INTEGER NOT NULL,
            price_at_signal REAL NOT NULL,
            price_at_horizon REAL,
            predicted_direction TEXT NOT NULL,
            actual_direction TEXT,
            is_correct BOOLEAN DEFAULT NULL,
            magnitude_error REAL DEFAULT NULL,
            key_factors TEXT DEFAULT '[]',
            scored_at TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
            prompt_supplement_version TEXT DEFAULT NULL
        )
        """

    @staticmethod
    def create_indexes_sql() -> list[str]:
        return [
            "CREATE INDEX IF NOT EXISTS idx_scores_pair ON signal_scores(pair)",
            "CREATE INDEX IF NOT EXISTS idx_scores_scored_at ON signal_scores(scored_at)",
            "CREATE INDEX IF NOT EXISTS idx_scores_agent ON signal_scores(agent_name, market_condition)",
            "CREATE INDEX IF NOT EXISTS idx_scores_reasoning ON signal_scores(reasoning_id, horizon_hours)",
        ]

    # ------------------------------------------------------------------
    # Backfill — score matured predictions
    # ------------------------------------------------------------------

    def backfill_scores(self, max_batch: int = _MAX_BACKFILL_BATCH) -> int:
        """Score recently-matured predictions that haven't been scored yet.

        Returns the number of new scores inserted.
        """
        scored = 0
        try:
            scored = self._backfill_horizon(max_batch)
        except Exception as e:
            logger.warning(f"Signal scorecard backfill error: {e}")
        return scored

    def _backfill_horizon(self, max_batch: int) -> int:
        """Find unscored predictions whose longest horizon has elapsed, score all horizons."""
        now = datetime.now(timezone.utc)
        # Only score predictions that are at least max-horizon old
        max_horizon = max(SCORE_HORIZONS)
        cutoff = (now - timedelta(hours=max_horizon)).isoformat()
        # Don't go too far back — limit to 7 days
        floor = (now - timedelta(days=7)).isoformat()

        with self._db._get_conn() as conn:
            # Find unscored market_analyst predictions
            unscored = conn.execute(
                """
                SELECT ar.id, ar.ts, ar.pair, ar.signal_type, ar.confidence,
                       ar.reasoning_json, ar.exchange
                FROM agent_reasoning ar
                WHERE ar.agent_name = 'market_analyst'
                  AND ar.ts <= %s
                  AND ar.ts >= %s
                  AND ar.signal_type NOT IN ('neutral', '')
                  AND NOT EXISTS (
                      SELECT 1 FROM signal_scores ss
                      WHERE ss.reasoning_id = ar.id AND ss.horizon_hours = %s
                  )
                ORDER BY ar.ts DESC
                LIMIT %s
                """,
                (cutoff, floor, max_horizon, max_batch),
            ).fetchall()

            if not unscored:
                return 0

            # Build price timeline from portfolio snapshots
            earliest_ts = min(r["ts"] for r in unscored)
            price_snapshots = conn.execute(
                """
                SELECT ts, current_prices
                FROM portfolio_snapshots
                WHERE ts >= %s
                  AND current_prices IS NOT NULL AND current_prices != '{}'
                ORDER BY ts ASC
                """,
                (earliest_ts,),
            ).fetchall()

            price_timeline = []
            for snap in price_snapshots:
                try:
                    prices = json.loads(snap["current_prices"] or "{}")
                    if prices:
                        price_timeline.append((snap["ts"], prices))
                except (json.JSONDecodeError, TypeError):
                    continue

            if not price_timeline:
                return 0

            inserted = 0
            for pred in unscored:
                rows = self._score_prediction(pred, price_timeline)
                for row in rows:
                    try:
                        conn.execute(
                            """
                            INSERT INTO signal_scores
                                (reasoning_id, agent_name, pair, exchange,
                                 prediction_ts, signal_type, raw_confidence,
                                 market_condition, horizon_hours,
                                 price_at_signal, price_at_horizon,
                                 predicted_direction, actual_direction,
                                 is_correct, magnitude_error, key_factors)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            """,
                            (
                                row["reasoning_id"], row["agent_name"], row["pair"],
                                row["exchange"], row["prediction_ts"], row["signal_type"],
                                row["raw_confidence"], row["market_condition"],
                                row["horizon_hours"], row["price_at_signal"],
                                row["price_at_horizon"], row["predicted_direction"],
                                row["actual_direction"], row["is_correct"],
                                row["magnitude_error"], json.dumps(row["key_factors"]),
                            ),
                        )
                        inserted += 1
                    except Exception as e:
                        logger.debug(f"Score insert failed: {e}")

            conn.commit()
            if inserted:
                logger.info(f"📊 Signal scorecard: scored {inserted} predictions")
            return inserted

    def _score_prediction(
        self, pred: dict, price_timeline: list[tuple[str, dict]]
    ) -> list[dict]:
        """Score a single prediction at all horizons."""
        signal = pred["signal_type"]
        is_bullish = signal in ("strong_buy", "buy", "weak_buy")
        is_bearish = signal in ("strong_sell", "sell", "weak_sell")
        if not is_bullish and not is_bearish:
            return []

        predicted_direction = "bullish" if is_bullish else "bearish"
        pair = pred["pair"]
        pred_ts = pred["ts"]

        # Parse reasoning for price and metadata
        try:
            reasoning = json.loads(pred["reasoning_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            reasoning = {}

        # Get price at signal time
        price_at_signal = self._extract_price(reasoning, pair, pred_ts, price_timeline)
        if not price_at_signal:
            return []

        market_condition = reasoning.get("market_condition", "")
        key_factors = reasoning.get("key_factors", [])
        if isinstance(key_factors, str):
            try:
                key_factors = json.loads(key_factors)
            except Exception:
                key_factors = [key_factors]

        rows = []
        for horizon in SCORE_HORIZONS:
            target_ts = self._ts_plus_hours(pred_ts, horizon)
            price_at_horizon = self._find_price(pair, target_ts, price_timeline)

            if price_at_horizon is None:
                # Can't score this horizon yet
                continue

            actual_move = (price_at_horizon - price_at_signal) / price_at_signal
            actual_direction = "bullish" if actual_move > 0 else "bearish" if actual_move < 0 else "flat"
            is_correct = predicted_direction == actual_direction

            rows.append({
                "reasoning_id": pred["id"],
                "agent_name": "market_analyst",
                "pair": pair,
                "exchange": pred.get("exchange", "coinbase"),
                "prediction_ts": pred_ts,
                "signal_type": signal,
                "raw_confidence": pred["confidence"] or 0.0,
                "market_condition": market_condition,
                "horizon_hours": horizon,
                "price_at_signal": price_at_signal,
                "price_at_horizon": price_at_horizon,
                "predicted_direction": predicted_direction,
                "actual_direction": actual_direction,
                "is_correct": is_correct,
                "magnitude_error": abs(actual_move),
                "key_factors": key_factors[:10],  # cap for storage
            })

        return rows

    # ------------------------------------------------------------------
    # Queries — rolling accuracy
    # ------------------------------------------------------------------

    def get_rolling_accuracy(
        self,
        pair: str | None = None,
        agent_name: str = "market_analyst",
        window_days: int = 30,
        horizon_hours: int = 24,
    ) -> dict[str, Any]:
        """Get rolling accuracy stats for a pair or globally.

        Returns:
            {total, correct, accuracy_pct, by_condition: {cond: {total, correct, accuracy_pct}}}
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        pair_frag = " AND pair = %s" if pair else ""
        pair_params = [pair] if pair else []

        with self._db._get_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT market_condition, is_correct, COUNT(*) as cnt
                FROM signal_scores
                WHERE agent_name = %s
                  AND horizon_hours = %s
                  AND scored_at >= %s
                  AND is_correct IS NOT NULL
                  {pair_frag}
                GROUP BY market_condition, is_correct
                """,
                (agent_name, horizon_hours, cutoff, *pair_params),
            ).fetchall()

        total = 0
        correct = 0
        by_condition: dict[str, dict] = defaultdict(lambda: {"total": 0, "correct": 0})

        for row in rows:
            cnt = row["cnt"]
            cond = row["market_condition"] or "unknown"
            total += cnt
            by_condition[cond]["total"] += cnt
            if row["is_correct"]:
                correct += cnt
                by_condition[cond]["correct"] += cnt

        for cond_data in by_condition.values():
            cond_data["accuracy_pct"] = (
                round(cond_data["correct"] / cond_data["total"] * 100, 1)
                if cond_data["total"] > 0 else None
            )

        return {
            "total": total,
            "correct": correct,
            "accuracy_pct": round(correct / total * 100, 1) if total > 0 else None,
            "by_condition": dict(by_condition),
            "window_days": window_days,
            "horizon_hours": horizon_hours,
            "pair": pair,
        }

    def get_multi_window_accuracy(
        self, pair: str | None = None, horizon_hours: int = 24
    ) -> dict[str, Any]:
        """Get accuracy across 7d / 30d / 90d windows."""
        return {
            f"{w}d": self.get_rolling_accuracy(pair, window_days=w, horizon_hours=horizon_hours)
            for w in (7, 30, 90)
        }

    # ------------------------------------------------------------------
    # Factor Attribution
    # ------------------------------------------------------------------

    def get_factor_attribution(
        self, window_days: int = 30, horizon_hours: int = 24, min_occurrences: int = 5
    ) -> list[dict]:
        """Identify which reasoning factors correlate with correct predictions.

        Returns a list of {factor, total, correct, accuracy_pct, lift_vs_baseline}
        sorted by lift descending.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()

        with self._db._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT key_factors, is_correct
                FROM signal_scores
                WHERE horizon_hours = %s
                  AND scored_at >= %s
                  AND is_correct IS NOT NULL
                  AND key_factors != '[]'
                """,
                (horizon_hours, cutoff),
            ).fetchall()

        if not rows:
            return []

        # Compute baseline accuracy
        total_all = len(rows)
        correct_all = sum(1 for r in rows if r["is_correct"])
        baseline = correct_all / total_all if total_all > 0 else 0.5

        # Count per-factor accuracy
        factor_stats: dict[str, dict] = defaultdict(lambda: {"total": 0, "correct": 0})
        for row in rows:
            try:
                factors = json.loads(row["key_factors"]) if isinstance(row["key_factors"], str) else row["key_factors"]
            except (json.JSONDecodeError, TypeError):
                continue
            for factor in factors:
                if not isinstance(factor, str):
                    continue
                factor_lower = factor.strip().lower()[:100]
                factor_stats[factor_lower]["total"] += 1
                if row["is_correct"]:
                    factor_stats[factor_lower]["correct"] += 1

        results = []
        for factor, stats in factor_stats.items():
            if stats["total"] < min_occurrences:
                continue
            acc = stats["correct"] / stats["total"]
            results.append({
                "factor": factor,
                "total": stats["total"],
                "correct": stats["correct"],
                "accuracy_pct": round(acc * 100, 1),
                "lift_vs_baseline": round((acc - baseline) * 100, 1),
            })

        results.sort(key=lambda x: x["lift_vs_baseline"], reverse=True)
        return results

    def get_regime_accuracy(
        self, window_days: int = 30, horizon_hours: int = 24
    ) -> dict[str, dict]:
        """Get accuracy broken down by market_condition (regime)."""
        acc = self.get_rolling_accuracy(window_days=window_days, horizon_hours=horizon_hours)
        return acc.get("by_condition", {})

    # ------------------------------------------------------------------
    # Signal-strength-weighted accuracy
    # ------------------------------------------------------------------

    # How much each signal type contributes to weighted accuracy.
    # strong signals count more — being wrong on a strong signal is a bigger
    # mark against quality than being wrong on a weak signal.
    SIGNAL_WEIGHTS: dict[str, float] = {
        "strong_buy":  2.0,
        "strong_sell": 2.0,
        "buy":         1.0,
        "sell":        1.0,
        "weak_buy":    0.5,
        "weak_sell":   0.5,
    }

    def get_accuracy_by_signal_type(
        self,
        pair: str | None = None,
        window_days: int = 30,
        horizon_hours: int = 24,
        min_samples: int = 5,
    ) -> dict[str, dict]:
        """Raw (unweighted) accuracy broken down by signal type.

        Returns a dict keyed by signal_type.  Types with fewer than
        *min_samples* scored predictions are excluded.

        Example::

            {
                "strong_buy": {"total": 24, "correct": 19, "win_rate": 0.792},
                "buy":        {"total": 11, "correct":  7, "win_rate": 0.636},
            }
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        pair_frag = " AND pair = %s" if pair else ""
        pair_params = [pair] if pair else []

        with self._db._get_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT signal_type, is_correct, COUNT(*) AS cnt
                FROM signal_scores
                WHERE horizon_hours = %s
                  AND scored_at >= %s
                  AND is_correct IS NOT NULL
                  {pair_frag}
                GROUP BY signal_type, is_correct
                """,
                (horizon_hours, cutoff, *pair_params),
            ).fetchall()

        stats: dict[str, dict] = defaultdict(lambda: {"total": 0, "correct": 0})
        for row in rows:
            st = row["signal_type"] or "unknown"
            stats[st]["total"] += row["cnt"]
            if row["is_correct"]:
                stats[st]["correct"] += row["cnt"]

        result = {}
        for st, s in stats.items():
            if s["total"] < min_samples:
                continue
            result[st] = {
                "total": s["total"],
                "correct": s["correct"],
                "win_rate": round(s["correct"] / s["total"], 4),
            }
        return result

    def get_weighted_accuracy(
        self,
        pair: str | None = None,
        window_days: int = 30,
        horizon_hours: int = 24,
        min_weighted_samples: float = 3.0,
    ) -> dict[str, Any]:
        """Accuracy score weighted by signal conviction.

        A ``strong_buy`` being wrong incurs a 2× penalty; a ``weak_buy``
        being wrong incurs only a 0.5× penalty (see :attr:`SIGNAL_WEIGHTS`).

        weighted_accuracy = Σ(weight_i × is_correct_i) / Σ(weight_i)

        Returns a dict with::

            weighted_accuracy_pct   – None if insufficient data
            weighted_total          – sum of weights across all predictions
            weighted_correct        – sum of weights for correct predictions
            raw_accuracy_pct        – simple correct / total (no weighting)
            sample_count            – number of raw predictions
            by_type                 – per-signal-type weighted scores

        Types absent from :attr:`SIGNAL_WEIGHTS` use weight 1.0.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        pair_frag = " AND pair = %s" if pair else ""
        pair_params = [pair] if pair else []

        with self._db._get_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT signal_type, is_correct, COUNT(*) AS cnt
                FROM signal_scores
                WHERE horizon_hours = %s
                  AND scored_at >= %s
                  AND is_correct IS NOT NULL
                  {pair_frag}
                GROUP BY signal_type, is_correct
                """,
                (horizon_hours, cutoff, *pair_params),
            ).fetchall()

        # Accumulate per signal_type
        by_type_raw: dict[str, dict] = defaultdict(lambda: {"total": 0, "correct": 0})
        for row in rows:
            st = row["signal_type"] or "unknown"
            by_type_raw[st]["total"] += row["cnt"]
            if row["is_correct"]:
                by_type_raw[st]["correct"] += row["cnt"]

        # Weighted totals
        w_total = 0.0
        w_correct = 0.0
        raw_total = 0
        raw_correct = 0

        by_type: dict[str, dict] = {}
        for st, s in by_type_raw.items():
            w = self.SIGNAL_WEIGHTS.get(st, 1.0)
            wt = s["total"] * w
            wc = s["correct"] * w
            w_total += wt
            w_correct += wc
            raw_total += s["total"]
            raw_correct += s["correct"]
            by_type[st] = {
                "total": s["total"],
                "correct": s["correct"],
                "weight": w,
                "weighted_total": round(wt, 2),
                "weighted_correct": round(wc, 2),
                "weighted_accuracy_pct": (
                    round(wc / wt * 100, 1) if wt > 0 else None
                ),
            }

        if w_total < min_weighted_samples:
            return {
                "weighted_accuracy_pct": None,
                "weighted_total": round(w_total, 2),
                "weighted_correct": round(w_correct, 2),
                "raw_accuracy_pct": None,
                "sample_count": raw_total,
                "by_type": by_type,
            }

        return {
            "weighted_accuracy_pct": round(w_correct / w_total * 100, 1),
            "weighted_total": round(w_total, 2),
            "weighted_correct": round(w_correct, 2),
            "raw_accuracy_pct": (
                round(raw_correct / raw_total * 100, 1) if raw_total > 0 else None
            ),
            "sample_count": raw_total,
            "by_type": by_type,
        }

    # ------------------------------------------------------------------
    # Confidence calibration data (for CalibrationEngine)
    # ------------------------------------------------------------------

    def get_calibration_data(
        self, window_days: int = 90, horizon_hours: int = 24
    ) -> list[tuple[float, bool]]:
        """Return (raw_confidence, is_correct) pairs for calibration training.

        Returns list of (confidence, was_correct) tuples.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        with self._db._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT raw_confidence, is_correct
                FROM signal_scores
                WHERE horizon_hours = %s
                  AND scored_at >= %s
                  AND is_correct IS NOT NULL
                  AND raw_confidence > 0
                """,
                (horizon_hours, cutoff),
            ).fetchall()
        return [(float(r["raw_confidence"]), bool(r["is_correct"])) for r in rows]

    # ------------------------------------------------------------------
    # Strategy-level accuracy (for ensemble weight optimization)
    # ------------------------------------------------------------------

    def get_strategy_accuracy(
        self, window_days: int = 14, horizon_hours: int = 4, exchange: str = "",
    ) -> dict[str, dict]:
        """Get accuracy stats per deterministic strategy.

        Looks for decisions.jsonl-style reasoning in agent_reasoning where
        agent_name = 'strategist' and extracts which strategy signals
        contributed to winning vs losing trades.

        Falls back to trade-level directional accuracy per strategy.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        _exch_frag = ""
        _exch_params: tuple = ()
        if exchange:
            _exch_frag = " AND (t.exchange = %s OR t.exchange = %s)"
            _exch_params = (exchange, f"{exchange}_paper")

        with self._db._get_conn() as conn:
            # Get trades with their associated reasoning
            rows = conn.execute(
                f"""
                SELECT t.pair, t.action, t.pnl, ar.reasoning_json
                FROM trades t
                JOIN agent_reasoning ar ON ar.trade_id = t.id AND ar.agent_name = 'strategist'
                WHERE t.ts >= %s AND t.pnl IS NOT NULL{_exch_frag}
                """,
                (cutoff, *_exch_params),
            ).fetchall()

        strategy_stats: dict[str, dict] = defaultdict(
            lambda: {"total": 0, "correct": 0, "total_pnl": 0.0}
        )

        for row in rows:
            try:
                reasoning = json.loads(row["reasoning_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue

            # Check which strategies contributed
            strat_signals = reasoning.get("strategy_signals", {})
            pnl = row["pnl"] or 0
            is_win = pnl > 0

            for strat_name, sig in strat_signals.items():
                if strat_name.startswith("_"):
                    continue
                strategy_stats[strat_name]["total"] += 1
                strategy_stats[strat_name]["total_pnl"] += pnl
                if is_win:
                    strategy_stats[strat_name]["correct"] += 1

        result = {}
        for strat, stats in strategy_stats.items():
            result[strat] = {
                "total": stats["total"],
                "correct": stats["correct"],
                "win_rate": round(stats["correct"] / stats["total"] * 100, 1) if stats["total"] > 0 else None,
                "total_pnl": round(stats["total_pnl"], 2),
                "avg_pnl": round(stats["total_pnl"] / stats["total"], 2) if stats["total"] > 0 else 0,
            }

        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_price(
        reasoning: dict, pair: str, pred_ts: str, price_timeline: list[tuple[str, dict]]
    ) -> float | None:
        """Extract price at signal time from reasoning or price timeline."""
        for key in ("suggested_entry", "current_price"):
            val = reasoning.get(key)
            if val is not None:
                try:
                    p = float(str(val).strip().translate(str.maketrans("", "", "€$£¥₹₩₪₫₦₨ ")))
                    if p > 0:
                        return p
                except (ValueError, TypeError):
                    pass
        return SignalScorecard._find_price(pair, pred_ts, price_timeline)

    @staticmethod
    def _find_price(
        pair: str, target_ts: str, price_timeline: list[tuple[str, dict]]
    ) -> float | None:
        """Find closest price for pair at or after target_ts."""
        for ts, prices in price_timeline:
            if ts >= target_ts:
                for key in [pair, pair.replace("-", "/"), pair.replace("/", "-")]:
                    if key in prices:
                        try:
                            return float(prices[key])
                        except (ValueError, TypeError):
                            pass
                return None
        return None

    @staticmethod
    def _ts_plus_hours(ts_str: str, hours: int) -> str:
        """Add hours to an ISO timestamp string."""
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return (dt + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
        except Exception:
            return ts_str
