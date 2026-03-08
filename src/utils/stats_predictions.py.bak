from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from src.utils.qc_filter import qc_where


# ── Module-level helpers (extracted from nested definitions) ───────────


def _find_price(
    pair: str, target_ts: str, price_timeline: list[tuple[str, dict]]
) -> float | None:
    """Find the closest price for a pair at or after *target_ts*.

    Returns *None* if the price appears stale (unchanged for 2+ consecutive
    hourly snapshots), which indicates the market was closed.
    """
    _STALE_LOOKBACK = 2  # how many prior entries to check for staleness
    for idx, (ts, prices) in enumerate(price_timeline):
        if ts >= target_ts:
            # Try exact pair, then common variants
            for key in [pair, pair.replace("-", "/"), pair.replace("/", "-")]:
                if key in prices:
                    val = prices[key]
                    if not val:
                        return None
                    current_price = float(val)
                    # Check if price is stale (same value in prior entries)
                    stale_streak = 0
                    for lookback in range(1, _STALE_LOOKBACK + 1):
                        prev_idx = idx - lookback
                        if prev_idx < 0:
                            break
                        prev_prices = price_timeline[prev_idx][1]
                        prev_val = prev_prices.get(key)
                        if prev_val and float(prev_val) == current_price:
                            stale_streak += 1
                        else:
                            break
                    if stale_streak >= _STALE_LOOKBACK:
                        return None  # market closed — stale price
                    return current_price
            return None
    return None


def _find_price_in_history(
    target_ts: str, price_history: list[dict]
) -> float | None:
    """Find the first price entry at or after *target_ts* in a price-history list."""
    for ph in price_history:
        if ph["ts"] >= target_ts:
            if ph.get("stale"):
                return None  # market was closed — don't use this price
            return ph["price"]
    return None


def _ts_plus_hours(ts_str: str, hours: int) -> str:
    """Add *hours* to an ISO timestamp string."""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return (dt + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    except Exception:
        return ts_str


# ── Mixin ──────────────────────────────────────────────────────────────


class PredictionsMixin:
    """Mixin supplying prediction-accuracy and pair-tracking queries.

    Expects the host class to provide ``_get_conn() -> sqlite3.Connection``.
    """

    # ─── Prediction Accuracy ───────────────────────────────────────────────

    def get_prediction_accuracy(self, days: int = 30, quote_currency: str | list[str] | None = None) -> dict:
        """
        Compute signal prediction accuracy by comparing market_analyst signals
        with actual price movements over subsequent hours.

        Uses the current_prices stored in portfolio_snapshots to determine what
        actually happened after each prediction.

        If *quote_currency* is given (e.g. "EUR" or ["EUR", "USD"]), only pairs
        ending in those currency suffixes are included.
        """
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # 1. Get all market_analyst predictions with signal details
        qc_frag, qc_params = qc_where(quote_currency)
        predictions = conn.execute(
            """SELECT
                ar.ts, ar.pair, ar.signal_type, ar.confidence,
                ar.reasoning_json, ar.cycle_id
               FROM agent_reasoning ar
               WHERE ar.agent_name = 'market_analyst'
                 AND ar.ts >= ?""" + qc_frag + """
               ORDER BY ar.ts ASC""",
            (cutoff, *qc_params),
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
        snapshots = conn.execute(
            """SELECT ts, current_prices
               FROM portfolio_snapshots
               WHERE ts >= ? AND current_prices IS NOT NULL AND current_prices != '{}'
               ORDER BY ts ASC""",
            (cutoff,),
        ).fetchall()

        # Parse into list of (ts, prices_dict) — sample every ~5 min
        price_timeline: list[tuple[str, dict]] = []
        for snap in snapshots:
            try:
                prices = json.loads(snap["current_prices"] or "{}")
                if prices:
                    price_timeline.append((snap["ts"], prices))
            except (json.JSONDecodeError, TypeError):
                continue

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
                if val and float(val) > 0:
                    price_at_signal = float(val)
                    break
            if not price_at_signal:
                price_at_signal = _find_price(pair, pred_ts, price_timeline)

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
                actual_price = _find_price(pair, future_ts, price_timeline)
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
        for st in by_signal:
            s = by_signal[st]
            s["accuracy_pct"] = round(s["correct_24h"] / s["evaluated_24h"] * 100, 1) if s["evaluated_24h"] else None

        # 7. Confidence calibration buckets (0-20%, 20-40%, …, 80-100%)
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

    def get_pair_prediction_history(self, pair: str, days: int = 30) -> dict:
        """Return price time-series with prediction markers for a single pair.

        Used by the Prediction Overlay chart. Returns:
          - price_history: [{ts, price}] from portfolio snapshots
          - predictions: [{ts, signal_type, confidence, entry_price, suggested_tp,
                          suggested_sl, is_bullish, outcomes}]
        """
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # 1. Price history from portfolio snapshots
        snapshots = conn.execute(
            """SELECT ts, current_prices
               FROM portfolio_snapshots
               WHERE ts >= ? AND current_prices IS NOT NULL AND current_prices != '{}'
               ORDER BY ts ASC""",
            (cutoff,),
        ).fetchall()

        price_history: list[dict] = []
        pair_upper = pair.upper()
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
                    entry = {"ts": snap["ts"], "price": round(price, 8)}
                    if is_stale:
                        entry["stale"] = True
                    price_history.append(entry)

        # 2. Prediction markers
        predictions_raw = conn.execute(
            """SELECT ts, signal_type, confidence, reasoning_json
               FROM agent_reasoning
               WHERE agent_name = 'market_analyst'
                 AND UPPER(pair) = ?
                 AND ts >= ?
               ORDER BY ts ASC""",
            (pair_upper, cutoff),
        ).fetchall()

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
                if val and float(val) > 0:
                    entry_price = float(val)
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
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        week_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        pair_upper = pair.upper()

        rows = conn.execute(
            """SELECT ar.ts, ar.signal_type, ar.reasoning_json
               FROM agent_reasoning ar
               WHERE ar.agent_name = 'market_analyst'
                 AND UPPER(ar.pair) = ?
                 AND ar.ts >= ?
               ORDER BY ar.ts ASC""",
            (pair_upper, cutoff),
        ).fetchall()

        if not rows:
            return None

        # Build price timeline from portfolio snapshots (same approach as full accuracy calc)
        snapshots = conn.execute(
            """SELECT ts, current_prices
               FROM portfolio_snapshots
               WHERE ts >= ? AND current_prices IS NOT NULL AND current_prices != '{}'
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
                if val and float(val) > 0:
                    price_at_signal = float(val)
                    break
            if not price_at_signal:
                price_at_signal = _find_price(pair_upper, pred_ts, price_timeline)
            if not price_at_signal:
                continue

            rec: dict = {"ts": pred_ts, "is_recent": pred_ts >= week_cutoff}
            for label, hours in [("1h", 1), ("24h", 24)]:
                future_ts = _ts_plus_hours(pred_ts, hours)
                actual = _find_price(pair_upper, future_ts, price_timeline)
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

    def get_tracked_pairs(self, quote_currency: str | list[str] | None = None) -> dict:
        """Return pairs tracked by AI and/or humans, grouped by asset class.

        AI-tracked: pairs with agent_reasoning entries from the last 7 days.
        Human-tracked: pairs in the pair_follows table with followed_by='human'.

        If *quote_currency* is given (e.g. "EUR" or ["EUR", "USD"]),
        only pairs ending in those currency suffixes are returned.
        """
        conn = self._get_conn()

        # Get AI-tracked pairs with prediction counts from last 7 days
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        qc_frag, qc_params = qc_where(quote_currency)
        rows = conn.execute(
            """SELECT pair, COUNT(*) as prediction_count,
                      MAX(ts) as last_predicted,
                      GROUP_CONCAT(DISTINCT signal_type) as signal_types
               FROM agent_reasoning
               WHERE agent_name = 'market_analyst' AND ts >= ?""" + qc_frag + """
               GROUP BY pair
               ORDER BY prediction_count DESC""",
            (cutoff, *qc_params),
        ).fetchall()

        # Build a map of AI-tracked pairs
        ai_pairs: dict[str, dict] = {}
        for r in rows:
            pair = r["pair"]
            ai_pairs[pair.upper()] = {
                "pair": pair,
                "prediction_count": r["prediction_count"],
                "last_predicted": r["last_predicted"],
                "signal_types": (r["signal_types"] or "").split(","),
                "source": "ai",
            }

        # Get human-followed pairs from pair_follows table
        human_sql = "SELECT DISTINCT pair, ts FROM pair_follows WHERE followed_by = 'human'"
        human_params: list = []
        if qc_frag:
            human_sql += qc_frag
            human_params.extend(qc_params)
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
        # IBKR equity pairs embed an exchange suffix in the base symbol using a
        # dot (e.g. ASML.AS-EUR, FSECURE.HE-EUR).  Crypto pairs never contain a
        # dot (BTC-EUR, ETH-USD).  Nordic equities traded with SEK/NOK/DKK also
        # count as equity regardless of dot presence.
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
