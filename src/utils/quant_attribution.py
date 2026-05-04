"""
Quant Analytics → self-learning attribution.

Joins persisted decision-time quant snapshots (``quant_decision_snapshots``)
with realised trade outcomes (``trades``) and signal-scorecard rows
(``signal_scores``) to compute hit-rate / avg PnL per quant-feature bucket.

Outputs feed:

* The ``LearningManager`` ``quant_attribution`` subsystem (cadenced daily).
  Recent results are persisted into the ``learning_runs`` table and
  surfaced in the dashboard, so an operator can SEE the system getting
  better at deploying these signals.
* The ``QuantAnalyticsModelCard`` ``attribution`` block (exportable).
* Adaptive multipliers on confidence / strategy weights via simple,
  conservative rules in :func:`derive_learning_adjustments`.

Pure read; never raises.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from src.utils.logger import get_logger

logger = get_logger("quant.attribution")


# ─── Helpers ────────────────────────────────────────────────────────────


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _bucket_har_rv(forecast: Optional[float], realized: Optional[float]) -> str:
    if forecast is None or realized is None or realized <= 0:
        return "unknown"
    rel = forecast / realized
    if rel > 1.20:
        return "elevated"
    if rel < 0.85:
        return "calm"
    return "stable"


def _bucket_alpha(alpha: Optional[float]) -> str:
    if alpha is None:
        return "unknown"
    if alpha > 0.05:
        return "positive"
    if alpha < -0.05:
        return "negative"
    return "neutral"


def _bucket_granger(n: Optional[int]) -> str:
    if n is None:
        return "unknown"
    if n >= 3:
        return "strong"
    if n >= 1:
        return "weak"
    return "none"


# ─── Joining ────────────────────────────────────────────────────────────


def _fetch_recent_outcomes(
    stats_db: Any, exchange: str, *, lookback_days: int = 30, limit: int = 5000,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Return a {(cycle_id, pair) → outcome} index from signal_scores."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    if stats_db is None:
        return out
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(lookback_days))).isoformat()
    sql = (
        "SELECT ss.pair, ss.is_correct, "
        "       (ss.price_at_horizon - ss.price_at_signal) / "
        "         NULLIF(ss.price_at_signal, 0) AS pnl_pct, "
        "       ar.cycle_id "
        "FROM signal_scores ss "
        "LEFT JOIN agent_reasoning ar ON ar.id = ss.reasoning_id "
        "WHERE ss.exchange = %s AND ss.scored_at >= %s "
        "  AND ss.is_correct IS NOT NULL "
        "ORDER BY ss.scored_at DESC LIMIT %s"
    )
    try:
        with stats_db._get_conn() as conn:
            rows = conn.execute(sql, (exchange, cutoff, int(limit))).fetchall()
    except Exception as e:
        logger.debug(f"attribution outcomes query failed: {e}")
        return out
    for r in rows:
        cid = (r.get("cycle_id") or "").strip() if isinstance(r, dict) else ""
        pair = r.get("pair") if isinstance(r, dict) else ""
        if not cid or not pair:
            continue
        out.setdefault((cid, pair), {
            "is_correct": bool(r.get("is_correct")),
            "pnl_pct": _safe_float(r.get("pnl_pct")),
        })
    return out


def attribute_quant_signals(
    stats_db: Any, exchange: str, *, lookback_days: int = 30,
) -> dict[str, Any]:
    """Compute hit-rate / avg PnL per quant-feature bucket.

    Output shape::

        {
            "lookback_days": int,
            "decisions_total": int,
            "outcomes_total": int,
            "by_bucket": {
                "har_rv_regime": {"calm": {"n": 12, "hit_rate": 0.62, "avg_pnl_pct": 0.018}, ...},
                "correlation_regime": {"normal": {...}, "breakdown": {...}},
                "factor_alpha": {"positive": {...}, ...},
                "granger_leaders": {"strong": {...}, ...},
            },
            "feature_signal_strength": float,   # IR-style, -1..+1
        }
    """
    summary: dict[str, Any] = {
        "lookback_days": int(lookback_days),
        "decisions_total": 0,
        "outcomes_total": 0,
        "by_bucket": {
            "har_rv_regime": {},
            "correlation_regime": {},
            "factor_alpha": {},
            "granger_leaders": {},
        },
        "feature_signal_strength": 0.0,
    }
    if stats_db is None or not exchange:
        return summary

    since = datetime.now(timezone.utc) - timedelta(days=int(lookback_days))
    try:
        snaps = stats_db.get_quant_decision_snapshots(
            exchange, since=since, limit=10_000,
        )
    except Exception as e:
        logger.debug(f"snapshot fetch failed: {e}")
        snaps = []
    summary["decisions_total"] = len(snaps)

    outcomes = _fetch_recent_outcomes(
        stats_db, exchange, lookback_days=lookback_days,
    )
    summary["outcomes_total"] = len(outcomes)

    # Aggregate
    agg: dict[str, dict[str, list[tuple[bool, Optional[float]]]]] = {
        "har_rv_regime": {},
        "correlation_regime": {},
        "factor_alpha": {},
        "granger_leaders": {},
    }
    for s in snaps:
        cid = (s.get("cycle_id") or "").strip()
        pair = s.get("pair")
        if not cid or not pair:
            continue
        outcome = outcomes.get((cid, pair))
        if not outcome:
            continue
        is_correct = outcome["is_correct"]
        pnl = outcome["pnl_pct"]
        # Buckets
        b_har = _bucket_har_rv(
            _safe_float(s.get("har_rv_forecast")),
            _safe_float(s.get("har_rv_realized")),
        )
        b_corr = (s.get("corr_regime") or "unknown") or "unknown"
        b_alpha = _bucket_alpha(_safe_float(s.get("factor_alpha")))
        b_gr = _bucket_granger(s.get("granger_leader_count"))
        for dim, bucket in (
            ("har_rv_regime", b_har),
            ("correlation_regime", b_corr),
            ("factor_alpha", b_alpha),
            ("granger_leaders", b_gr),
        ):
            agg[dim].setdefault(bucket, []).append((is_correct, pnl))

    # Reduce
    overall_pnls: list[float] = []
    for dim, buckets in agg.items():
        for bucket, items in buckets.items():
            n = len(items)
            hits = sum(1 for c, _ in items if c)
            pnls = [p for _, p in items if p is not None]
            avg_pnl = sum(pnls) / len(pnls) if pnls else None
            overall_pnls.extend(pnls)
            summary["by_bucket"][dim][bucket] = {
                "n": n,
                "hit_rate": (hits / n) if n else None,
                "avg_pnl_pct": avg_pnl,
            }

    # Feature signal strength: IR-style of a hypothetical "trust-the-model"
    # portfolio (long when bucket avg_pnl > 0). Bounded to [-1, 1].
    if overall_pnls:
        mu = sum(overall_pnls) / len(overall_pnls)
        var = sum((p - mu) ** 2 for p in overall_pnls) / max(len(overall_pnls), 1)
        sd = math.sqrt(var) if var > 0 else 0.0
        ir = (mu / sd) if sd > 1e-9 else 0.0
        summary["feature_signal_strength"] = max(-1.0, min(1.0, ir))

    return summary


# ─── Adaptive overlay (consumed by the learning loop) ──────────────────


def derive_learning_adjustments(
    attribution: dict[str, Any],
) -> dict[str, Any]:
    """Translate attribution stats into conservative, bounded multipliers.

    Returns a dict of small multiplicative adjustments that callers can
    apply to confidence or strategy weights, e.g.::

        {
            "confidence_multiplier_by_regime": {"breakdown": 0.85, ...},
            "har_rv_action": "size_down" | "size_up" | "neutral",
            "trust_score": 0.0..1.0,   # 0 = ignore quant, 1 = follow blindly
            "rationale": [str, ...],
        }

    Rules are intentionally simple and bounded (max ±20%):
      * Bucket needs ≥ 10 outcomes before influencing live behaviour.
      * Adjustment scales linearly with avg_pnl_pct, capped at ±20%.
      * Confidence boost requires hit_rate ≥ 0.55.
    """
    rules: dict[str, Any] = {
        "confidence_multiplier_by_regime": {},
        "har_rv_action": "neutral",
        "trust_score": 0.5,
        "rationale": [],
    }
    by_bucket = (attribution or {}).get("by_bucket") or {}
    rationale = rules["rationale"]

    # Correlation regime
    cr = by_bucket.get("correlation_regime") or {}
    for regime, st in cr.items():
        n = st.get("n") or 0
        if n < 10:
            continue
        hr = st.get("hit_rate") or 0.0
        avg_pnl = st.get("avg_pnl_pct") or 0.0
        # Map avg_pnl ∈ [-2%, +2%] → multiplier ∈ [0.80, 1.20]
        mult = 1.0 + max(-0.20, min(0.20, avg_pnl * 10.0))
        if hr < 0.45:
            mult = min(mult, 0.95)
        rules["confidence_multiplier_by_regime"][regime] = round(mult, 3)
        rationale.append(
            f"corr_regime={regime}: n={n}, hr={hr:.2%}, "
            f"avg_pnl={avg_pnl:+.2%} → ×{mult:.2f}"
        )

    # HAR-RV regime hint
    hr_buckets = by_bucket.get("har_rv_regime") or {}
    el = hr_buckets.get("elevated", {})
    cm = hr_buckets.get("calm", {})
    if (el.get("n") or 0) >= 10 and (el.get("avg_pnl_pct") or 0) < -0.005:
        rules["har_rv_action"] = "size_down"
        rationale.append("HAR-RV elevated regime delivered negative avg PnL → size_down")
    elif (cm.get("n") or 0) >= 10 and (cm.get("avg_pnl_pct") or 0) > 0.005:
        rules["har_rv_action"] = "size_up"
        rationale.append("HAR-RV calm regime delivered positive avg PnL → size_up")

    # Trust score: positive feature_signal_strength → higher trust
    fss = (attribution or {}).get("feature_signal_strength") or 0.0
    rules["trust_score"] = max(0.0, min(1.0, 0.5 + 0.5 * fss))

    return rules


__all__ = [
    "attribute_quant_signals",
    "derive_learning_adjustments",
]
