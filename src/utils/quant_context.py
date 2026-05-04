"""
Quant Analytics → decision context glue.

Builds a per-pair ``quant_context`` dict from the latest rows in the five
QuantAnalytics tables (factor loadings, HAR-RV forecasts, Granger edges,
slippage model, correlation regime). The dict is injected into every
strategist / trader payload and into the risk-manager context, so the
self-learning loop can later attribute decision quality back to each quant
signal.

Pure read; never raises. Returns ``{"available": False}`` when the DB has
no quant rows yet.
"""

from __future__ import annotations

import math
from typing import Any, Optional


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def build_quant_context(
    stats_db: Any,
    exchange: str,
    pair: str,
    *,
    horizon_days: int = 1,
) -> dict[str, Any]:
    """Snapshot the latest quant signals relevant to one pair.

    Shape::

        {
            "available": bool,
            "har_rv": {forecast_vol, realized_vol_daily, model_r_squared, ...} | None,
            "factor_loadings": [{factor, beta, t_stat, r_squared, alpha_annualised, idio_vol}, ...],
            "factor_alpha_annualised": float | None,
            "idio_vol": float | None,
            "granger_leaders": [{leader, lag_hours, f_stat, p_value}, ...],
            "slippage_model": {alpha, beta_size, beta_vol, r_squared, sample_count} | None,
            "correlation_regime": {regime, avg_corr, z_score, n_pairs, recorded_at} | None,
        }
    """
    ctx: dict[str, Any] = {"available": False}
    if stats_db is None or not exchange or not pair:
        return ctx

    # 1) HAR-RV forecast
    try:
        if hasattr(stats_db, "get_har_rv_forecast_for_symbol"):
            har = stats_db.get_har_rv_forecast_for_symbol(
                exchange, pair, horizon_days=int(horizon_days),
            )
            if har:
                ctx["har_rv"] = {
                    "forecast_vol": _safe_float(har.get("forecast_vol")),
                    "realized_vol_daily": _safe_float(har.get("realized_vol_daily")),
                    "realized_vol_weekly": _safe_float(har.get("realized_vol_weekly")),
                    "realized_vol_monthly": _safe_float(har.get("realized_vol_monthly")),
                    "model_r_squared": _safe_float(har.get("model_r_squared")),
                    "sample_count": int(har.get("sample_count") or 0),
                }
    except Exception:
        pass

    # 2) Factor loadings
    try:
        if hasattr(stats_db, "get_market_factor_loadings"):
            rows = stats_db.get_market_factor_loadings(
                exchange, symbol=pair, limit=50,
            )
            if rows:
                fl = [
                    {
                        "factor": r.get("factor"),
                        "beta": _safe_float(r.get("beta")),
                        "t_stat": _safe_float(r.get("t_stat")),
                        "r_squared": _safe_float(r.get("r_squared")),
                        "alpha_annualised": _safe_float(r.get("alpha_annualised")),
                        "idio_vol": _safe_float(r.get("idio_vol")),
                    }
                    for r in rows
                ]
                ctx["factor_loadings"] = fl
                # Per-symbol values are duplicated across factor rows;
                # take the first non-null we see.
                for r in fl:
                    if ctx.get("factor_alpha_annualised") is None and r["alpha_annualised"] is not None:
                        ctx["factor_alpha_annualised"] = r["alpha_annualised"]
                    if ctx.get("idio_vol") is None and r["idio_vol"] is not None:
                        ctx["idio_vol"] = r["idio_vol"]
    except Exception:
        pass

    # 3) Granger leaders for this pair (where ``pair`` is the follower)
    try:
        if hasattr(stats_db, "get_granger_results"):
            rows = stats_db.get_granger_results(
                exchange, follower=pair, max_p_value=0.05, limit=10,
            )
            if rows:
                ctx["granger_leaders"] = [
                    {
                        "leader": r.get("leader"),
                        "lag_hours": int(r.get("lag_hours") or 0),
                        "f_stat": _safe_float(r.get("f_stat")),
                        "p_value": _safe_float(r.get("p_value")),
                    }
                    for r in rows
                ]
    except Exception:
        pass

    # 4) Slippage model (universe-wide, one row per exchange)
    try:
        if hasattr(stats_db, "get_slippage_impact_model"):
            sm = stats_db.get_slippage_impact_model(exchange)
            if sm:
                ctx["slippage_model"] = {
                    "alpha": _safe_float(sm.get("alpha")),
                    "beta_size": _safe_float(sm.get("beta_size")),
                    "beta_vol": _safe_float(sm.get("beta_vol")),
                    "r_squared": _safe_float(sm.get("r_squared")),
                    "sample_count": int(sm.get("sample_count") or 0),
                }
    except Exception:
        pass

    # 5) Correlation regime — latest snapshot
    try:
        if hasattr(stats_db, "get_correlation_regime_events"):
            ev = stats_db.get_correlation_regime_events(exchange, limit=1)
            if ev:
                e = ev[0]
                ts = e.get("computed_at")
                ctx["correlation_regime"] = {
                    "regime": e.get("regime"),
                    "avg_corr": _safe_float(e.get("avg_corr")),
                    "z_score": _safe_float(e.get("z_score")),
                    "n_pairs": int(e.get("n_pairs") or 0),
                    "recorded_at": ts.isoformat() if hasattr(ts, "isoformat") else ts,
                }
    except Exception:
        pass

    ctx["available"] = any(
        ctx.get(k) is not None
        for k in ("har_rv", "factor_loadings", "granger_leaders",
                  "slippage_model", "correlation_regime")
    )
    return ctx


def predict_slippage_bps(
    slippage_model: Optional[dict[str, Any]],
    *,
    notional: float,
    adv: float,
    realised_vol: float,
) -> Optional[float]:
    """Apply the fitted OLS to a hypothetical fill.

    Returns predicted slippage in basis points or ``None`` if the model
    is missing required coefficients.
    """
    if not slippage_model:
        return None
    alpha = slippage_model.get("alpha")
    bs = slippage_model.get("beta_size")
    bv = slippage_model.get("beta_vol")
    if alpha is None or bs is None or bv is None or adv <= 0:
        return None
    try:
        return float(alpha) + float(bs) * (float(notional) / float(adv)) + float(bv) * float(realised_vol)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def format_decision_explanation(
    quant_ctx: dict[str, Any],
    *,
    action: str = "",
) -> list[str]:
    """Render quant signals as one-liner human-readable explanations.

    Used by the dashboard, audit log and the strategist's reasoning trace
    so humans (and the LLM) can see *why* the position was sized the way
    it was.
    """
    out: list[str] = []
    if not quant_ctx or not quant_ctx.get("available"):
        return out

    har = quant_ctx.get("har_rv") or {}
    fv = har.get("forecast_vol")
    if fv is not None:
        rd = har.get("realized_vol_daily") or 0.0
        rel = (fv / rd) if rd else 1.0
        verdict = "elevated" if rel > 1.20 else ("calm" if rel < 0.85 else "stable")
        out.append(
            f"HAR-RV forecast vol={fv:.2%} ({verdict} vs trailing realized {rd:.2%})"
        )

    cr = quant_ctx.get("correlation_regime") or {}
    regime = cr.get("regime")
    if regime:
        z = cr.get("z_score")
        avg = cr.get("avg_corr")
        z_str = f"z={z:+.2f}" if z is not None else "z=?"
        avg_str = f"avg={avg:.2f}" if avg is not None else ""
        out.append(f"Correlation regime: {regime} ({z_str} {avg_str})".strip())

    leaders = quant_ctx.get("granger_leaders") or []
    if leaders:
        top = leaders[0]
        out.append(
            f"Granger leader: {top.get('leader')} →lag={top.get('lag_hours')}h "
            f"(p={top.get('p_value'):.3f})"
        )

    alpha = quant_ctx.get("factor_alpha_annualised")
    idio = quant_ctx.get("idio_vol")
    if alpha is not None:
        if idio and idio > 0:
            ir = alpha / idio
            out.append(
                f"Factor model: α={alpha:+.2%}/yr, idio_vol={idio:.2%}, IR≈{ir:+.2f}"
            )
        else:
            out.append(f"Factor model: α={alpha:+.2%}/yr")

    sm = quant_ctx.get("slippage_model") or {}
    if sm.get("alpha") is not None:
        out.append(
            f"Slippage model fitted (n={sm.get('sample_count', 0)}, "
            f"R²={(sm.get('r_squared') or 0):.2f})"
        )

    return out


__all__ = [
    "build_quant_context",
    "predict_slippage_bps",
    "format_decision_explanation",
]
