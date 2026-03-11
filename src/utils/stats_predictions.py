from __future__ import annotations

import bisect
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from src.utils.qc_filter import qc_where


# -- Module-level helpers (extracted from nested definitions) ----------------

_CURRENCY_STRIP = str.maketrans("", "", "€$£¥₹₩₪₫₦₨ ")


def _safe_float(val) -> float | None:
    """Convert val to float, stripping leading currency symbols if needed."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        if isinstance(val, str):
            try:
                return float(val.strip().translate(_CURRENCY_STRIP))
            except (ValueError, TypeError):
                pass
    return None


def _find_price(
    pair: str, target_ts: str, price_timeline: list[tuple[str, dict]],
    _ts_index: list[str] | None = None,
) -> float | None:
    """Find the closest price for a pair at or after *target_ts*.

    Uses binary search on the pre-built *_ts_index* (list of timestamps) when
    provided, falling back to linear scan otherwise.

    Returns *None* if the price appears stale (unchanged for 2+ consecutive
    hourly snapshots), which indicates the market was closed.
    """
    _STALE_LOOKBACK = 2
    if _ts_index is not None:
        idx = bisect.bisect_left(_ts_index, target_ts)
    else:
        idx = next((i for i, (ts, _) in enumerate(price_timeline) if ts >= target_ts), None)
        if idx is None:
            return None

    if idx >= len(price_timeline):
        return None

    ts, prices = price_timeline[idx]
    for key in [pair, pair.replace("-", "/"), pair.replace("/", "-")]:
        if key in prices:
            val = prices[key]
            if not val:
                return None
            current_price = _safe_float(val)
            if current_price is None:
                return None
            stale_streak = 0
            for lookback in range(1, _STALE_LOOKBACK + 1):
                prev_idx = idx - lookback
                if prev_idx < 0:
                    break
                prev_prices = price_timeline[prev_idx][1]
                prev_val = prev_prices.get(key)
                if prev_val and _safe_float(prev_val) == current_price:
                    stale_streak += 1
                else:
                    break
            if stale_streak >= _STALE_LOOKBACK:
                return None
            return current_price
    return None


def _find_price_in_history(
    target_ts: str, price_history: list[dict]
) -> float | None:
    """Find the first price entry at or after *target_ts* in a price-history list."""
    for ph in price_history:
        if ph["ts"] >= target_ts:
            if ph.get("stale"):
                return None  # market was closed -- don't use this price
            return ph["price"]
    return None


def _ts_plus_hours(ts_str: str, hours: int) -> str:
    """Add *hours* to an ISO timestamp string."""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return (dt + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    except Exception:
        return ts_str


# -- Mixin -------------------------------------------------------------------


class PredictionsMixin:
    """Mixin supplying prediction-accuracy and pair-tracking queries.

    Expects the host class to provide ``_get_conn()`` as a context manager
    yielding a connection proxy with ``.execute()`` / ``.commit()`` methods.
    """

    # --- Prediction Accuracy ------------------------------------------------

    def get_prediction_accuracy(self, days: int = 30, quote_currency: str | list[str] | None = None, exchange: str | None = None) -> dict:
        """
        Compute signal prediction accuracy by comparing market_analyst signals
        with actual price movements over subsequent hours.

        Uses the current_prices stored in portfolio_snapshots to determine what
        actually happened after each prediction.

        If *quote_currency* is given (e.g. "EUR" or ["EUR", "USD"]), only pairs
        ending in those currency suffixes are included.
        """
        with self._get_conn() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

            # 1. Get all market_analyst predictions with signal details
            qc_frag, qc_params = qc_where(quote_currency)
            exch_frag = " AND ar.exchange = %s" if exchange else ""
            exch_params = [exchange] if exchange else []
            predictions = conn.execute(
                """SELECT
                    ar.ts, ar.pair, ar.signal_type, ar.confidence,
                    ar.reasoning_json, ar.cycle_id
                   FROM agent_reasoning ar
                   WHERE ar.agent_name = 'market_analyst'
                     AND ar.ts >= %s""" + qc_frag + exch_frag + """
                   ORDER BY ar.ts ASC""",
                (cutoff, *qc_params, *exch_params),
            ).fetchall()

            if not predictions:
                return {
                    "predictions": [],
                    "per_pair": {},
                    "overall": {
                        "total": 0, "correct_24h": 0, "evaluated_24h": 0,
                        "correct_1h": 0, "evaluated_1h": 0,
                        "accuracy_24h_pct": None, "accuracy_1h_pct": None,
                    },
                    "by_signal_type": {},
                    "confidence_calibration": [],
                    "daily_accuracy": [],
                }

            # 2. Build a price lookup from portfolio snapshots (current_prices JSON)
            snap_exch_frag = " AND exchange = %s" if exchange else ""
            snap_exch_params = [exchange] if exchange else []
            snapshots = conn.execute(
                """SELECT ts, current_prices
                   FROM portfolio_snapshots
                   WHERE ts >= %s AND current_prices IS NOT NULL AND current_prices != '{}'"""
                + snap_exch_frag + """
                   ORDER BY ts ASC""",
                (cutoff, *snap_exch_params),
            ).fetchall()

        # Parse into list of (ts, prices_dict) -- sample every ~5 min
        price_timeline: list[tuple[str, dict]] = []
        for snap in snapshots:
            try:
                prices = json.loads(snap["current_prices"] or "{}")
                if prices:
                    price_timeline.append((snap["ts"], prices))
            except (json.JSONDecodeError, TypeError):
                continue

        # Build sorted timestamp index for O(log n) binary search
        ts_index: list[str] = [ts for ts, _ in price_timeline]

        # 3. Evaluate each prediction
        results: list[dict] = []
        for pred in predictions:
            try:
                reasoning = json.loads(pred["reasoning_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                reasoning = {}

            signal = pred["signal_type"] or "neutral"
            confidence = pred["confidence"] or 0.0
            pair = pred["pair"]
            pred_ts = pred["ts"]

            # Price at prediction time
            price_at_signal = None
            for key in ["suggested_entry", "current_price"]:
                val = reasoning.get(key)
                parsed = _safe_float(val)
                if parsed and parsed > 0:
                    price_at_signal = parsed
                    break
            if not price_at_signal:
                price_at_signal = _find_price(pair, pred_ts, price_timeline, ts_index)

            if not price_at_signal:
                continue

            # Direction the bot predicted
            is_bullish = signal in ("strong_buy", "buy", "weak_buy")
            is_bearish = signal in ("strong_sell", "sell", "weak_sell")
            if not is_bullish and not is_bearish:
                continue  # neutral predictions can't be evaluated

            # Check outcome at multiple horizons
            horizons = {"1h": 1, "4h": 4, "24h": 24, "7d": 168}
            outcomes: dict[str, dict | None] = {}
            for label, hours in horizons.items():
                future_ts = _ts_plus_hours(pred_ts, hours)
                actual_price = _find_price(pair, future_ts, price_timeline, ts_index)
                if actual_price and actual_price > 0:
                    pct_change = (actual_price - price_at_signal) / price_at_signal * 100
                    price_went_up = actual_price > price_at_signal
                    correct = (is_bullish and price_went_up) or (is_bearish and not price_went_up)
                    outcomes[label] = {
                        "actual_price": round(actual_price, 6),
                        "pct_change": round(pct_change, 4),
                        "correct": correct,
                    }
                else:
                    outcomes[label] = None

            results.append({
                "ts": pred_ts,
                "pair": pair,
                "signal_type": signal,
                "confidence": round(confidence, 3),
                "entry_price": round(price_at_signal, 6),
                "suggested_tp": reasoning.get("suggested_take_profit"),
                "suggested_sl": reasoning.get("suggested_stop_loss"),
                "outcomes": outcomes,
            })

        # 4. Aggregate per-pair accuracy
        per_pair: dict[str, dict] = {}
        for r in results:
            p = r["pair"]
            if p not in per_pair:
                per_pair[p] = {"total": 0, "correct_24h": 0, "correct_1h": 0, "evaluated_24h": 0, "evaluated_1h": 0}
            per_pair[p]["total"] += 1
            if r["outcomes"].get("24h"):
                per_pair[p]["evaluated_24h"] += 1
                if r["outcomes"]["24h"]["correct"]:
                    per_pair[p]["correct_24h"] += 1
            if r["outcomes"].get("1h"):
                per_pair[p]["evaluated_1h"] += 1
                if r["outcomes"]["1h"]["correct"]:
                    per_pair[p]["correct_1h"] += 1
        for p in per_pair:
            s = per_pair[p]
            s["accuracy_24h_pct"] = round(s["correct_24h"] / s["evaluated_24h"] * 100, 1) if s["evaluated_24h"] else None
            s["accuracy_1h_pct"] = round(s["correct_1h"] / s["evaluated_1h"] * 100, 1) if s["evaluated_1h"] else None

        # 5. Overall accuracy
        overall = {"total": len(results), "correct_24h": 0, "evaluated_24h": 0, "correct_1h": 0, "evaluated_1h": 0}
        for r in results:
            if r["outcomes"].get("24h"):
                overall["evaluated_24h"] += 1
                if r["outcomes"]["24h"]["correct"]:
                    overall["correct_24h"] += 1
            if r["outcomes"].get("1h"):
                overall["evaluated_1h"] += 1
                if r["outcomes"]["1h"]["correct"]:
                    overall["correct_1h"] += 1
        overall["accuracy_24h_pct"] = round(overall["correct_24h"] / overall["evaluated_24h"] * 100, 1) if overall["evaluated_24h"] else None
        overall["accuracy_1h_pct"] = round(overall["correct_1h"] / overall["evaluated_1h"] * 100, 1) if overall["evaluated_1h"] else None

        # 6. By signal type
        by_signal: dict[str, dict] = {}
        for r in results:
            st = r["signal_type"]
            if st not in by_signal:
                by_signal[st] = {"total": 0, "correct_24h": 0, "evaluated_24h": 0}
            by_signal[st]["total"] += 1
            if r["outcomes"].get("24h"):
                by_signal[st]["evaluated_24h"] += 1
                if r["outcomes"]["24h"]["correct"]:
                    by_signal[st]["correct_24h"] += 1
        # Signal weight constants (mirrors SignalScorecard.SIGNAL_WEIGHTS)
        _SIGNAL_WEIGHTS = {
            "strong_buy": 2.0, "strong_sell": 2.0,
            "buy": 1.0, "sell": 1.0,
            "weak_buy": 0.5, "weak_sell": 0.5,
            "neutral": 0.0,
        }
        for st in by_signal:
            s = by_signal[st]
            s["accuracy_pct"] = round(s["correct_24h"] / s["evaluated_24h"] * 100, 1) if s["evaluated_24h"] else None
            s["weight"] = _SIGNAL_WEIGHTS.get(st, 1.0)

        # 7. Confidence calibration buckets (0-20%, 20-40%, ..., 80-100%)
        buckets: dict[str, dict] = {}
        for r in results:
            bucket_idx = min(int(r['confidence'] * 100 // 20), 4)  # clamp to 0-4
            bucket = f"{bucket_idx * 20}-{bucket_idx * 20 + 20}%"
            if bucket not in buckets:
                buckets[bucket] = {"confidence_range": bucket, "total": 0, "correct": 0, "evaluated": 0}
            buckets[bucket]["total"] += 1
            if r["outcomes"].get("24h"):
                buckets[bucket]["evaluated"] += 1
                if r["outcomes"]["24h"]["correct"]:
                    buckets[bucket]["correct"] += 1
        calibration = []
        for b in sorted(buckets.values(), key=lambda x: x["confidence_range"]):
            b["accuracy_pct"] = round(b["correct"] / b["evaluated"] * 100, 1) if b["evaluated"] else None
            calibration.append(b)

        # 8. Daily accuracy time-series
        daily: dict[str, dict] = defaultdict(lambda: {"date": "", "total": 0, "correct": 0, "evaluated": 0})
        for r in results:
            date = r["ts"][:10]
            daily[date]["date"] = date
            daily[date]["total"] += 1
            if r["outcomes"].get("24h"):
                daily[date]["evaluated"] += 1
                if r["outcomes"]["24h"]["correct"]:
                    daily[date]["correct"] += 1
        daily_list = []
        for d in sorted(daily.values(), key=lambda x: x["date"]):
            d["accuracy_pct"] = round(d["correct"] / d["evaluated"] * 100, 1) if d["evaluated"] else None
            daily_list.append(d)

        return {
            "predictions": results[-200:],  # last 200 for detail view
            "per_pair": per_pair,
            "overall": overall,
            "by_signal_type": by_signal,
            "confidence_calibration": calibration,
            "daily_accuracy": daily_list,
        }

    def get_pair_prediction_history(self, pair: str, days: int = 30, exchange: str | None = None) -> dict:
        """Return price time-series with prediction markers for a single pair.

        Used by the Prediction Overlay chart. Returns:
          - price_history: [{ts, price}] from portfolio snapshots
          - predictions: [{ts, signal_type, confidence, entry_price, suggested_tp,
                          suggested_sl, is_bullish, outcomes}]
        """
        with self._get_conn() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

            # 1. Price history from portfolio snapshots
            snap_exch_frag = " AND exchange = %s" if exchange else ""
            snap_exch_params = [exchange] if exchange else []
            snapshots = conn.execute(
                """SELECT ts, current_prices
                   FROM portfolio_snapshots
                   WHERE ts >= %s AND current_prices IS NOT NULL AND current_prices != '{}'"""
                + snap_exch_frag + """
                   ORDER BY ts ASC""",
                (cutoff, *snap_exch_params),
            ).fetchall()

            # 2. Prediction markers
            pair_upper = pair.upper()
            pred_exch_frag = " AND exchange = %s" if exchange else ""
            pred_exch_params = [exchange] if exchange else []
            predictions_raw = conn.execute(
                """SELECT ts, signal_type, confidence, reasoning_json
                   FROM agent_reasoning
                   WHERE agent_name = 'market_analyst'
                     AND UPPER(pair) = %s
                     AND ts >= %s""" + pred_exch_frag + """
                   ORDER BY ts ASC""",
                (pair_upper, cutoff, *pred_exch_params),
            ).fetchall()

        price_history: list[dict] = []
        seen_hours: set[str] = set()  # deduplicate to ~hourly
        _prev_price: float | None = None
        _stale_count = 0
        _STALE_THRESHOLD = 2  # consecutive same-price entries to flag as stale
        for snap in snapshots:
            try:
                prices = json.loads(snap["current_prices"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            price = None
            for key in [pair_upper, pair_upper.replace("-", "/"), pair_upper.replace("/", "-")]:
                if key in prices and prices[key]:
                    price = float(prices[key])
                    break
            if price and price > 0:
                hour_key = snap["ts"][:13]  # YYYY-MM-DDTHH
                if hour_key not in seen_hours:
                    seen_hours.add(hour_key)
                    # Detect stale prices (market closed / no fresh data)
                    is_stale = False
                    if _prev_price is not None and price == _prev_price:
                        _stale_count += 1
                        if _stale_count >= _STALE_THRESHOLD:
                            is_stale = True
                    else:
                        _stale_count = 0
                    _prev_price = price
                    # Drop consecutive stale entries (keep only the 1st to
                    # mark the gap).  This eliminates the flat after-hours /
                    # weekend sections that compress the real price action.
                    if is_stale and _stale_count > _STALE_THRESHOLD:
                        continue
                    entry = {"ts": snap["ts"], "price": round(price, 8)}
                    if is_stale:
                        entry["stale"] = True
                    price_history.append(entry)

        predictions: list[dict] = []
        for pred in predictions_raw:
            try:
                reasoning = json.loads(pred["reasoning_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                reasoning = {}

            signal = pred["signal_type"] or "neutral"
            confidence = pred["confidence"] or 0.0

            entry_price = None
            for key in ["suggested_entry", "current_price"]:
                val = reasoning.get(key)
                parsed = _safe_float(val)
                if parsed and parsed > 0:
                    entry_price = parsed
                    break

            # Try to find price from snapshot if not in reasoning
            if not entry_price:
                entry_price = _find_price_in_history(pred["ts"], price_history)

            if not entry_price:
                continue

            is_bullish = signal in ("strong_buy", "buy", "weak_buy")
            is_bearish = signal in ("strong_sell", "sell", "weak_sell")

            # Find outcome prices at various horizons
            pred_ts = pred["ts"]
            outcomes: dict[str, dict | None] = {}
            for label, hours in {"1h": 1, "4h": 4, "24h": 24, "7d": 168}.items():
                future_ts = _ts_plus_hours(pred_ts, hours)
                actual_price = _find_price_in_history(future_ts, price_history)
                if actual_price and actual_price > 0:
                    pct_change = (actual_price - entry_price) / entry_price * 100
                    price_went_up = actual_price > entry_price
                    correct = (is_bullish and price_went_up) or (is_bearish and not price_went_up)
                    outcomes[label] = {
                        "actual_price": round(actual_price, 8),
                        "pct_change": round(pct_change, 4),
                        "correct": correct,
                    }
                else:
                    outcomes[label] = None

            predictions.append({
                "ts": pred_ts,
                "signal_type": signal,
                "confidence": round(confidence, 3),
                "entry_price": round(entry_price, 8),
                "suggested_tp": reasoning.get("suggested_take_profit"),
                "suggested_sl": reasoning.get("suggested_stop_loss"),
                "is_bullish": is_bullish,
                "outcomes": outcomes,
            })

        return {
            "pair": pair_upper,
            "price_history": price_history,
            "predictions": predictions,
            "total_predictions": len(predictions),
        }

    def get_pair_accuracy_context(
        self, pair: str, days: int = 30, min_samples: int = 5
    ) -> dict | None:
        """Return a concise accuracy summary for a single pair, for LLM prompt injection.

        Evaluates 24h and 1h directional accuracy over the last *days* days, plus a
        trend comparison (last 7 days vs. the prior period). Returns None if fewer
        than *min_samples* evaluated predictions exist (not enough data to be useful).
        """
        with self._get_conn() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            week_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            pair_upper = pair.upper()

            rows = conn.execute(
                """SELECT ar.ts, ar.signal_type, ar.reasoning_json
                   FROM agent_reasoning ar
                   WHERE ar.agent_name = 'market_analyst'
                     AND UPPER(ar.pair) = %s
                     AND ar.ts >= %s
                   ORDER BY ar.ts ASC""",
                (pair_upper, cutoff),
            ).fetchall()

            if not rows:
                return None

            # Build price timeline from portfolio snapshots
            snapshots = conn.execute(
                """SELECT ts, current_prices
                   FROM portfolio_snapshots
                   WHERE ts >= %s AND current_prices IS NOT NULL AND current_prices != '{}'
                   ORDER BY ts ASC""",
                (cutoff,),
            ).fetchall()

        price_timeline: list[tuple[str, dict]] = []
        for snap in snapshots:
            try:
                prices = json.loads(snap["current_prices"] or "{}")
                if prices:
                    price_timeline.append((snap["ts"], prices))
            except (json.JSONDecodeError, TypeError):
                continue

        ts_index: list[str] = [ts for ts, _ in price_timeline]

        results = []
        for pred in rows:
            try:
                reasoning = json.loads(pred["reasoning_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                reasoning = {}

            signal = pred["signal_type"] or "neutral"
            pred_ts = pred["ts"]
            is_bullish = signal in ("strong_buy", "buy", "weak_buy")
            is_bearish = signal in ("strong_sell", "sell", "weak_sell")
            if not is_bullish and not is_bearish:
                continue

            price_at_signal = None
            for key in ["suggested_entry", "current_price"]:
                val = reasoning.get(key)
                parsed = _safe_float(val)
                if parsed and parsed > 0:
                    price_at_signal = parsed
                    break
            if not price_at_signal:
                price_at_signal = _find_price(pair_upper, pred_ts, price_timeline, ts_index)
            if not price_at_signal:
                continue

            rec: dict = {"ts": pred_ts, "is_recent": pred_ts >= week_cutoff}
            for label, hours in [("1h", 1), ("24h", 24)]:
                future_ts = _ts_plus_hours(pred_ts, hours)
                actual = _find_price(pair_upper, future_ts, price_timeline, ts_index)
                if actual and actual > 0:
                    went_up = actual > price_at_signal
                    correct = (is_bullish and went_up) or (is_bearish and not went_up)
                    rec[f"correct_{label}"] = int(correct)
                    rec[f"evaluated_{label}"] = 1
                else:
                    rec[f"correct_{label}"] = 0
                    rec[f"evaluated_{label}"] = 0
            results.append(rec)

        total_eval_24h = sum(r["evaluated_24h"] for r in results)
        if total_eval_24h < min_samples:
            return None

        total_correct_24h = sum(r["correct_24h"] for r in results)
        total_eval_1h = sum(r["evaluated_1h"] for r in results)
        total_correct_1h = sum(r["correct_1h"] for r in results)

        accuracy_24h = round(total_correct_24h / total_eval_24h * 100, 1) if total_eval_24h else None
        accuracy_1h = round(total_correct_1h / total_eval_1h * 100, 1) if total_eval_1h else None

        # Trend: last 7 days vs prior period
        recent = [r for r in results if r["is_recent"]]
        older = [r for r in results if not r["is_recent"]]
        recent_eval = sum(r["evaluated_24h"] for r in recent)
        recent_correct = sum(r["correct_24h"] for r in recent)
        older_eval = sum(r["evaluated_24h"] for r in older)
        older_correct = sum(r["correct_24h"] for r in older)
        recent_acc = round(recent_correct / recent_eval * 100, 1) if recent_eval >= 3 else None
        older_acc = round(older_correct / older_eval * 100, 1) if older_eval >= 3 else None

        if recent_acc is not None and older_acc is not None:
            delta = recent_acc - older_acc
            trend = "improving" if delta > 8 else ("degrading" if delta < -8 else "stable")
        else:
            trend = "insufficient_data"

        return {
            "pair": pair_upper,
            "total": len(results),
            "evaluated_24h": total_eval_24h,
            "accuracy_24h_pct": accuracy_24h,
            "accuracy_1h_pct": accuracy_1h,
            "recent_accuracy_24h_pct": recent_acc,
            "older_accuracy_24h_pct": older_acc,
            "trend": trend,
        }

    def get_weighted_pair_accuracy(
        self, pair: str, days: int = 30, horizon_hours: int = 24
    ) -> dict:
        """Weighted prediction accuracy for a pair, delegating to SignalScorecard.

        Strong signals (strong_buy/sell) carry 2× weight; weak signals 0.5×.
        Returns SignalScorecard.get_weighted_accuracy result, or empty dict on error.
        """
        from src.utils.signal_scorecard import SignalScorecard
        try:
            sc = SignalScorecard(self)
            return sc.get_weighted_accuracy(pair=pair, window_days=days, horizon_hours=horizon_hours)
        except Exception:
            return {}

    def get_tracked_pairs(self, quote_currency: str | list[str] | None = None, exchange: str | None = None) -> dict:
        """Return pairs tracked by AI and/or humans, grouped by asset class.

        AI-tracked: pairs with agent_reasoning entries from the last 7 days.
        Human-tracked: pairs in the pair_follows table with followed_by='human'.

        If *quote_currency* is given (e.g. "EUR" or ["EUR", "USD"]),
        only pairs ending in those currency suffixes are returned.
        """
        with self._get_conn() as conn:
            # Get AI-tracked pairs with prediction counts from last 7 days
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            qc_frag, qc_params = qc_where(quote_currency)
            exch_frag = " AND exchange = %s" if exchange else ""
            exch_params = [exchange] if exchange else []
            rows = conn.execute(
                """SELECT pair, COUNT(*) as prediction_count,
                          MAX(ts) as last_predicted,
                          STRING_AGG(DISTINCT signal_type, ',') as signal_types
                   FROM agent_reasoning
                   WHERE agent_name = 'market_analyst' AND ts >= %s""" + qc_frag + exch_frag + """
                   GROUP BY pair
                   ORDER BY prediction_count DESC""",
                (cutoff, *qc_params, *exch_params),
            ).fetchall()

            # Build a map of AI-tracked pairs
            ai_pairs: dict[str, dict] = {}
            for r in rows:
                pair = r["pair"]
                raw_signals = [s.strip().replace("-", "_") for s in (r["signal_types"] or "").split(",") if s.strip()]
                ai_pairs[pair.upper()] = {
                    "pair": pair,
                    "prediction_count": r["prediction_count"],
                    "last_predicted": r["last_predicted"],
                    "signal_types": sorted(set(raw_signals)),
                    "source": "ai",
                }

            # Get human-followed pairs from pair_follows table
            human_sql = "SELECT DISTINCT pair, ts FROM pair_follows WHERE followed_by = 'human'"
            human_params: list = []
            if qc_frag:
                human_sql += qc_frag
                human_params.extend(qc_params)
            if exchange:
                try:
                    exch_human_rows = conn.execute(
                        human_sql + " AND exchange = %s", [*human_params, exchange]
                    ).fetchall()
                    human_rows = exch_human_rows
                except Exception:
                    conn.rollback()
                    human_rows = conn.execute(human_sql, human_params).fetchall()
            else:
                try:
                    human_rows = conn.execute(human_sql, human_params).fetchall()
                except Exception:
                    human_rows = []  # table may not exist in very old DBs

        # Merge: AI pairs take priority, human-only pairs get added with source="human"
        for hr in human_rows:
            pair_upper = hr["pair"].upper()
            if pair_upper in ai_pairs:
                # Pair is both AI and human tracked
                ai_pairs[pair_upper]["source"] = "both"
            else:
                ai_pairs[pair_upper] = {
                    "pair": hr["pair"],
                    "prediction_count": 0,
                    "last_predicted": hr["ts"] if hr["ts"] else None,
                    "signal_types": [],
                    "source": "human",
                }

        # Classify pairs into crypto/equity.
        equity_suffixes = {"-SEK", "-NOK", "-DKK"}

        crypto_pairs = []
        equity_pairs = []
        for item in sorted(ai_pairs.values(), key=lambda x: x["prediction_count"], reverse=True):
            pair = item["pair"]
            base = pair.rsplit("-", 1)[0] if "-" in pair else pair
            is_equity = "." in base or any(pair.upper().endswith(s) for s in equity_suffixes)
            if is_equity:
                equity_pairs.append(item)
            else:
                crypto_pairs.append(item)

        return {
            "crypto": crypto_pairs,
            "equity": equity_pairs,
            "total_pairs": len(ai_pairs),
        }
