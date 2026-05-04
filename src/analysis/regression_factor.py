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

Three modes (resolved from env or profile yaml):

* ``off``  — default. Factor is always 1.0 (model is fitted nightly but
  not applied to sizing). Existing behaviour, no surprises.
* ``on``   — manual override. Factor moves size whenever the model
  clears the standard quality gate (R² ≥ 0.10, N ≥ 10).
* ``auto`` — system-driven. Same as ``on`` *plus* a per-symbol
  promotion bar: the regression must have demonstrated directional
  accuracy (hit_rate ≥ 0.55, N ≥ 30, R² ≥ 0.15) before the factor
  goes live for that symbol. This is what flips the regression "on"
  automatically based on its own measured performance.

Resolution: ``REGRESSION_RISK_FACTOR_MODE`` env (off/on/auto) →
``REGRESSION_RISK_FACTOR_ENABLED`` env (legacy bool) →
``risk.regression_factor_mode`` yaml → ``risk.use_regression_factor``
yaml (legacy bool) → default ``off``.
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

# Auto-mode promotion bar — strictly tougher than the always-on quality
# gate so a model has to *prove itself* on out-of-sample directional
# accuracy before the factor starts moving size autonomously.
_AUTO_MIN_HIT_RATE = 0.55   # >50% directional accuracy on forward returns
_AUTO_MIN_SAMPLES = 30      # 3× the manual-mode floor
_AUTO_MIN_R_SQUARED = 0.15  # 1.5× the manual-mode floor


def _resolve_mode(risk_config: dict | None) -> str:
    """Return tri-state mode: ``on`` | ``off`` | ``auto``.

    Resolution order (first match wins):
      1. ``REGRESSION_RISK_FACTOR_MODE`` env (off/on/auto)
      2. ``REGRESSION_RISK_FACTOR_ENABLED`` env (legacy bool)
      3. ``risk.regression_factor_mode`` yaml (off/on/auto)
      4. ``risk.use_regression_factor`` yaml (legacy bool)
      5. default → ``off`` (safe; no behaviour change for unconfigured profiles)
    """
    mode_env = os.environ.get("REGRESSION_RISK_FACTOR_MODE", "").strip().lower()
    if mode_env in {"off", "on", "auto"}:
        return mode_env

    bool_env = os.environ.get("REGRESSION_RISK_FACTOR_ENABLED", "").strip().lower()
    if bool_env in {"1", "true", "yes", "on"}:
        return "on"
    if bool_env in {"0", "false", "no", "off"}:
        return "off"

    cfg = risk_config or {}
    mode_yaml = str(cfg.get("regression_factor_mode", "")).strip().lower()
    if mode_yaml in {"off", "on", "auto"}:
        return mode_yaml

    if bool(cfg.get("use_regression_factor", False)):
        return "on"
    return "off"


def _is_enabled(risk_config: dict | None) -> bool:
    """Back-compat shim — returns True when mode is ``on``.

    ``auto`` mode is treated as enabled at the *gate* level (we proceed to
    fetch the model) but the final ``applied`` decision additionally
    requires the model to clear the auto-promotion bar — see the
    ``_meets_auto_bar`` check inside :func:`build_regression_factor`.
    """
    return _resolve_mode(risk_config) in {"on", "auto"}


def _meets_auto_bar(model: dict) -> tuple[bool, str]:
    """Per-symbol auto-promotion check.

    Returns ``(passed, reason)``. The reason is surfaced in the payload
    so the dashboard / logs can show *why* a fitted model is or isn't
    being trusted to move size yet.
    """
    hit = model.get("hit_rate")
    n = int(model.get("sample_count") or 0)
    r2 = model.get("r_squared")
    if not isinstance(hit, (int, float)) or not math.isfinite(float(hit)):
        return False, "auto_gate:hit_rate_unknown"
    if hit < _AUTO_MIN_HIT_RATE:
        return False, f"auto_gate:hit_rate {hit:.2f} < {_AUTO_MIN_HIT_RATE}"
    if n < _AUTO_MIN_SAMPLES:
        return False, f"auto_gate:N {n} < {_AUTO_MIN_SAMPLES}"
    if not isinstance(r2, (int, float)) or not math.isfinite(float(r2)):
        return False, "auto_gate:r_squared_invalid"
    if r2 < _AUTO_MIN_R_SQUARED:
        return False, f"auto_gate:R² {r2:.2f} < {_AUTO_MIN_R_SQUARED}"
    return True, "auto_gate:passed"


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

    mode = _resolve_mode(risk_config)
    payload["mode"] = mode
    if mode == "off":
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

    # Auto mode: model has cleared the always-on quality gate above, but
    # to *autonomously* move size we additionally require proven
    # directional accuracy. Run before computing the multiplier so the
    # rejection reason reflects the gate, not a downstream "too small"
    # heuristic. Manual ``on`` mode skips this bar — the operator has
    # explicitly accepted the weaker quality threshold.
    if mode == "auto":
        ok, reason = _meets_auto_bar(model)
        payload["auto_gate"] = {"passed": ok, "detail": reason}
        if not ok:
            payload["reason"] = reason
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
    payload["reason"] = "ok" if mode == "on" else "ok:auto"
    return payload


__all__ = ["build_regression_factor"]
