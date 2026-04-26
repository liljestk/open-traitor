"""Regression-driven trading factor — turns nightly OLS fits into a signal.

The ``EventRegressionWorkflow`` persists per-(exchange, symbol, event_type)
forward-return regressions to ``event_price_regressions``. Historically the
dashboard *displayed* these but the trading loop never read them — the
operator had no way to know whether a "STRONG R²=0.4 after CPI" model
actually moved a single sat in production.

This module bridges that gap: it returns a small, bounded multiplier the
``risk_manager`` can apply to position sizing when a fitted regression
*and* an imminent matching catalyst both exist for the symbol. Honors
the "no hallucinations" directive — every active multiplier is anchored
to a real DB row reference returned in the payload so the dashboard can
show *which* regression and *which* upcoming event drove the change.

Disabled by default behind the ``REGRESSION_RISK_FACTOR_ENABLED`` env
flag; profile YAML can also set ``risk.use_regression_factor`` to opt in.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any

# Bounds — kept tight on purpose: regressions are noisy, the floor/ceiling
# are deliberately closer to 1.0 than the pattern-engine multiplier.
_MIN_FACTOR = 0.9
_MAX_FACTOR = 1.1

# Quality gates — mirror the dashboard's MODERATE threshold so what the
# operator sees in green/amber is what actually fires in production.
_MIN_R_SQUARED = 0.10
_MIN_SAMPLES = 10

# Only act on catalysts within this window — old fits applied to events
# already in the rear-view mirror are noise.
_CATALYST_HORIZON_HOURS = 72


def _is_enabled(risk_config: dict | None) -> bool:
    """Strict opt-in. Env flag wins; profile yaml can also enable."""
    env = os.environ.get("REGRESSION_RISK_FACTOR_ENABLED", "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    if env in {"0", "false", "no", "off"}:
        return False
    return bool((risk_config or {}).get("use_regression_factor", False))


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def build_regression_factor(
    *,
    db: Any,
    exchange: str,
    symbol: str,
    risk_config: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """Return a regression-derived signal payload for ``risk_manager``.

    Shape (always populated even when disabled / no data, so callers can
    log a single uniform structure):

        {
            "available": bool,
            "applied": bool,            # True only when factor != 1.0 and enabled
            "factor": float,            # in [_MIN_FACTOR, _MAX_FACTOR]
            "direction": "bullish"|"bearish"|"neutral",
            "reason": str,
            "model": {symbol, event_type, horizon_days, r_squared, sample_count, mean_forward_return} | None,
            "upcoming_event": {event_type, event_ts} | None,
        }
    """
    payload: dict = {
        "available": False,
        "applied": False,
        "factor": 1.0,
        "direction": "neutral",
        "reason": "disabled",
        "model": None,
        "upcoming_event": None,
    }

    if not _is_enabled(risk_config):
        return payload

    if db is None or not symbol or not exchange:
        payload["reason"] = "missing_inputs"
        return payload

    now = now or datetime.now(timezone.utc)

    # 1) Find an imminent catalyst for this symbol.
    try:
        upcoming = db.get_upcoming_catalysts(
            exchange=exchange,
            horizon_days=max(1, _CATALYST_HORIZON_HOURS // 24),
            symbol=symbol,
        )
    except Exception:
        upcoming = []

    if not upcoming:
        payload["reason"] = "no_upcoming_catalyst"
        return payload

    # Pick the soonest event within the horizon.
    horizon_cutoff = now + timedelta(hours=_CATALYST_HORIZON_HOURS)
    next_ev: dict | None = None
    for ev in upcoming:
        ets = ev.get("event_ts")
        if isinstance(ets, datetime):
            ets_dt = ets if ets.tzinfo else ets.replace(tzinfo=timezone.utc)
        else:
            try:
                ets_dt = datetime.fromisoformat(str(ets).replace("Z", "+00:00"))
            except Exception:
                continue
        if now <= ets_dt <= horizon_cutoff:
            next_ev = {**ev, "event_ts_dt": ets_dt}
            break

    if next_ev is None:
        payload["reason"] = "no_catalyst_in_window"
        return payload

    event_type = next_ev.get("event_type")
    if not event_type:
        payload["reason"] = "missing_event_type"
        return payload

    # 2) Look up the regression model for this (symbol, event_type).
    try:
        rows = db.get_event_regressions(
            exchange=exchange,
            symbol=symbol,
            event_type=event_type,
            order_by="r_squared",
            limit=5,
        )
    except Exception:
        rows = []

    if not rows:
        payload["available"] = True
        payload["reason"] = "no_regression_for_event_type"
        payload["upcoming_event"] = {
            "event_type": event_type,
            "event_ts": next_ev["event_ts_dt"].isoformat(),
        }
        return payload

    model = rows[0]
    r2 = model.get("r_squared")
    n = int(model.get("sample_count") or 0)
    mfr = model.get("mean_forward_return")

    payload["available"] = True
    payload["upcoming_event"] = {
        "event_type": event_type,
        "event_ts": next_ev["event_ts_dt"].isoformat(),
    }
    payload["model"] = {
        "symbol": symbol,
        "event_type": event_type,
        "horizon_days": model.get("horizon_days"),
        "r_squared": r2,
        "sample_count": n,
        "mean_forward_return": mfr,
        "hit_rate": model.get("hit_rate"),
    }

    # 3) Quality gate — refuse to act on weak fits.
    if r2 is None or not isinstance(r2, (int, float)) or not math.isfinite(r2):
        payload["reason"] = "r_squared_invalid"
        return payload
    if r2 < _MIN_R_SQUARED or n < _MIN_SAMPLES:
        payload["reason"] = (
            f"weak_fit (R²={r2:.2f} < {_MIN_R_SQUARED} or N={n} < {_MIN_SAMPLES})"
        )
        return payload

    if mfr is None or not isinstance(mfr, (int, float)) or not math.isfinite(mfr):
        payload["reason"] = "mean_forward_return_unknown"
        return payload

    # 4) Compute direction + magnitude.
    # Map mean forward return to a bounded multiplier:
    #   - sign drives direction (bullish/bearish)
    #   - magnitude proportional to abs(mfr) but capped at the bounds
    #   - confidence dampens by R² (a 5% mfr at R²=0.5 moves more than the
    #     same 5% mfr at R²=0.1)
    confidence = _clip(r2, 0.0, 1.0)
    raw_delta = float(mfr) * confidence  # both fractional
    # Clip to bounds
    factor = _clip(1.0 + raw_delta, _MIN_FACTOR, _MAX_FACTOR)

    if abs(factor - 1.0) < 0.005:
        payload["reason"] = "delta_too_small"
        return payload

    payload["factor"] = factor
    payload["applied"] = True
    payload["direction"] = "bullish" if factor > 1.0 else "bearish"
    payload["reason"] = "ok"
    return payload


__all__ = ["build_regression_factor"]
