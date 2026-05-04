"""
Quant Analytics Model Card.

Self-describing, versioned, exportable JSON+Markdown bundle that:

* Documents every quant analyzer (purpose, method, window, current fit).
* Surfaces the latest values from each of the five quant tables.
* Ships ``limitations`` and ``usage_guidance`` so a human reader can
  reason about *when* each signal is and isn't trustworthy.
* Includes ``attribution`` (hit rate / avg outcome per quant feature
  bucket) so the self-learning loop's progress is visible.

The card is consumed by the dashboard ``/api/quant/model-card`` route
and exported as ``data/<exchange>/quant_model_card.json`` whenever the
attribution subsystem runs.

Pure read; never raises.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Optional

MODEL_CARD_VERSION = "1.0"


# ─── Helpers ────────────────────────────────────────────────────────────


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _isoformat(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def _safe_call(fn, default):
    try:
        return fn()
    except Exception:
        return default


# ─── Section builders ───────────────────────────────────────────────────


def _section_factor_loadings(stats_db, exchange: str) -> dict[str, Any]:
    rows = _safe_call(
        lambda: stats_db.get_market_factor_loadings(exchange, limit=2000),
        [],
    )
    cleaned: list[dict[str, Any]] = []
    factor_counts: dict[str, int] = {}
    r2_vals: list[float] = []
    for r in rows:
        f = r.get("factor")
        if f:
            factor_counts[f] = factor_counts.get(f, 0) + 1
        r2 = _safe_float(r.get("r_squared"))
        if r2 is not None:
            r2_vals.append(r2)
        cleaned.append({
            "symbol": r.get("symbol"),
            "factor": f,
            "beta": _safe_float(r.get("beta")),
            "t_stat": _safe_float(r.get("t_stat")),
            "r_squared": r2,
            "alpha_annualised": _safe_float(r.get("alpha_annualised")),
            "idio_vol": _safe_float(r.get("idio_vol")),
            "sample_count": int(r.get("sample_count") or 0),
            "computed_at": _isoformat(r.get("computed_at")),
        })
    top_loadings = sorted(
        [r for r in cleaned if r["t_stat"] is not None],
        key=lambda r: abs(r["t_stat"] or 0.0),
        reverse=True,
    )[:10]
    most_significant_factor = (
        max(factor_counts.items(), key=lambda kv: kv[1])[0]
        if factor_counts else None
    )
    return {
        "description": (
            "Multi-factor regression of each tradeable symbol's daily return "
            "against macro + market factors (default: SPX, VIX, DXY, BTC). "
            "Reports per-factor beta, t-stat, residual α, idiosyncratic vol."
        ),
        "method": "OLS per (symbol, factor) on overlapping daily returns",
        "summary": {
            "rows": len(cleaned),
            "factors_seen": list(factor_counts.keys()),
            "factor_loading_counts": factor_counts,
            "avg_r_squared": (
                sum(r2_vals) / len(r2_vals) if r2_vals else None
            ),
            "most_common_factor": most_significant_factor,
        },
        "top_loadings": top_loadings,
    }


def _section_har_rv(stats_db, exchange: str) -> dict[str, Any]:
    rows = _safe_call(
        lambda: stats_db.get_har_rv_forecasts(exchange, limit=2000),
        [],
    )
    cleaned = []
    fvs: list[float] = []
    r2_vals: list[float] = []
    for r in rows:
        fv = _safe_float(r.get("forecast_vol"))
        if fv is not None:
            fvs.append(fv)
        r2 = _safe_float(r.get("model_r_squared"))
        if r2 is not None:
            r2_vals.append(r2)
        cleaned.append({
            "symbol": r.get("symbol"),
            "horizon_days": int(r.get("horizon_days") or 1),
            "forecast_vol": fv,
            "realized_vol_daily": _safe_float(r.get("realized_vol_daily")),
            "realized_vol_weekly": _safe_float(r.get("realized_vol_weekly")),
            "realized_vol_monthly": _safe_float(r.get("realized_vol_monthly")),
            "model_r_squared": r2,
            "sample_count": int(r.get("sample_count") or 0),
            "computed_at": _isoformat(r.get("computed_at")),
        })
    fvs_sorted = sorted(fvs)
    median_fv = (
        fvs_sorted[len(fvs_sorted) // 2] if fvs_sorted else None
    )
    return {
        "description": (
            "HAR-RV (Heterogeneous Autoregressive Realized Volatility) "
            "one-step-ahead forecast from daily / weekly / monthly RV components."
        ),
        "method": (
            "OLS: RV_t+1 ~ α + β_d·RV_d + β_w·RV_w + β_m·RV_m, "
            "evaluated per symbol on rolling hourly bars."
        ),
        "summary": {
            "rows": len(cleaned),
            "median_forecast_vol": median_fv,
            "avg_model_r_squared": (
                sum(r2_vals) / len(r2_vals) if r2_vals else None
            ),
        },
        "forecasts": cleaned[:50],  # cap response size
    }


def _section_granger(stats_db, exchange: str) -> dict[str, Any]:
    rows = _safe_call(
        lambda: stats_db.get_granger_results(
            exchange, max_p_value=0.05, limit=500,
        ),
        [],
    )
    leader_counts: dict[str, int] = {}
    follower_counts: dict[str, int] = {}
    cleaned = []
    for r in rows:
        ld, fw = r.get("leader"), r.get("follower")
        if ld:
            leader_counts[ld] = leader_counts.get(ld, 0) + 1
        if fw:
            follower_counts[fw] = follower_counts.get(fw, 0) + 1
        cleaned.append({
            "leader": ld,
            "follower": fw,
            "lag_hours": int(r.get("lag_hours") or 0),
            "f_stat": _safe_float(r.get("f_stat")),
            "p_value": _safe_float(r.get("p_value")),
            "sample_count": int(r.get("sample_count") or 0),
            "computed_at": _isoformat(r.get("computed_at")),
        })

    def _top(d: dict[str, int]) -> Optional[str]:
        if not d:
            return None
        return max(d.items(), key=lambda kv: kv[1])[0]

    return {
        "description": (
            "Granger causality F-test for lead-lag pair validation. "
            "Reports significant (leader → follower) edges only."
        ),
        "method": (
            "Restricted vs unrestricted VAR; F-test on Δ-RSS over "
            "lags ∈ {1, 2, 4, 12, 24}h, significance p ≤ 0.05."
        ),
        "summary": {
            "significant_edges": len(cleaned),
            "most_common_leader": _top(leader_counts),
            "most_common_follower": _top(follower_counts),
        },
        "edges": cleaned[:100],
    }


def _section_slippage(stats_db, exchange: str) -> dict[str, Any]:
    row = _safe_call(
        lambda: stats_db.get_slippage_impact_model(exchange),
        None,
    )
    return {
        "description": (
            "Linear slippage impact regression. Predicts realised fill "
            "slippage (bps) from order size relative to ADV and trailing "
            "realised vol. Used to flag edge-eroding trades pre-execution."
        ),
        "method": (
            "OLS: slippage_bps ~ α + β_size · (notional / ADV) + β_vol · realised_vol"
        ),
        "model": (
            {
                "alpha": _safe_float(row.get("alpha")),
                "beta_size": _safe_float(row.get("beta_size")),
                "beta_vol": _safe_float(row.get("beta_vol")),
                "r_squared": _safe_float(row.get("r_squared")),
                "sample_count": int(row.get("sample_count") or 0),
                "fitted_at": _isoformat(row.get("computed_at")),
            }
            if row else None
        ),
    }


def _section_correlation_regime(stats_db, exchange: str) -> dict[str, Any]:
    events = _safe_call(
        lambda: stats_db.get_correlation_regime_events(exchange, limit=200),
        [],
    )
    cleaned = []
    for e in events:
        cleaned.append({
            "regime": e.get("regime"),
            "avg_corr": _safe_float(e.get("avg_corr")),
            "z_score": _safe_float(e.get("z_score")),
            "n_pairs": int(e.get("n_pairs") or 0),
            "history_n": int(e.get("history_n") or 0),
            "recorded_at": _isoformat(e.get("computed_at")),
        })
    latest = cleaned[0] if cleaned else None
    # Regime histogram across the recent window
    regime_counts: dict[str, int] = {}
    for c in cleaned:
        r = c.get("regime") or "unknown"
        regime_counts[r] = regime_counts.get(r, 0) + 1
    return {
        "description": (
            "Universe-wide avg pairwise correlation tracker with rolling "
            "z-score regime detection (normal / elevated / breakdown)."
        ),
        "method": (
            "Pearson |ρ| averaged over all pairs within the rolling-window "
            "matrix, z-scored against the trailing history (default n=60)."
        ),
        "summary": {
            "events": len(cleaned),
            "regime_counts": regime_counts,
        },
        "latest": latest,
        "recent_events": cleaned[:50],
    }


# ─── Public API ─────────────────────────────────────────────────────────


def build_model_card(
    stats_db: Any,
    exchange: str,
    *,
    universe: Optional[list[str]] = None,
    attribution: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Assemble the full model card. ``stats_db`` may be ``None`` for tests.

    ``attribution`` is the latest output of
    :func:`src.utils.quant_attribution.attribute_quant_signals` and is
    embedded verbatim. Pass ``None`` to omit.
    """
    exchange = (exchange or "").lower()
    now = datetime.now(timezone.utc).isoformat()
    card: dict[str, Any] = {
        "model_card_version": MODEL_CARD_VERSION,
        "exchange": exchange,
        "generated_at": now,
        "universe": list(universe or []),
        "sections": {},
        "limitations": [
            "Factor loadings: 252-day window; idiosyncratic vol may be unstable for illiquid alts.",
            "HAR-RV: single-step-ahead only; multi-step requires iteration and accumulates error.",
            "Granger: assumes linear lagged dependence; contemporaneous and non-linear effects not captured.",
            "Slippage model: fitted from recent fills only; may not generalise during regime shifts.",
            "Correlation regime: z-score baseline drifts as new events arrive; sensitive to shocks.",
        ],
        "usage_guidance": [
            "factor_loadings: prefer symbols with high |residual α| AND low idio_vol (high IR).",
            "har_rv: feed into vol_target multiplier (set RISK_USE_HAR_RV=1) for forward-looking sizing.",
            "granger: amplify entries when a strong leader (low p-value) confirms direction; size down otherwise.",
            "slippage_model: at the executor, choose LIMIT vs MARKET and abort if predicted bps eats expected edge.",
            "correlation_regime: when regime=breakdown (z>2σ), reduce gross exposure and downweight trend strategies.",
        ],
    }
    if stats_db is not None and exchange:
        card["sections"]["factor_loadings"] = _section_factor_loadings(stats_db, exchange)
        card["sections"]["har_rv"] = _section_har_rv(stats_db, exchange)
        card["sections"]["granger"] = _section_granger(stats_db, exchange)
        card["sections"]["slippage_model"] = _section_slippage(stats_db, exchange)
        card["sections"]["correlation_regime"] = _section_correlation_regime(stats_db, exchange)
    if attribution:
        card["attribution"] = attribution
    return card


def format_markdown(card: dict[str, Any]) -> str:
    """Human-readable Markdown rendering of the model card."""
    lines: list[str] = []
    exchange = card.get("exchange", "?")
    lines.append(f"# Quant Analytics Model Card — {exchange}")
    lines.append("")
    lines.append(f"- **Version:** {card.get('model_card_version')}")
    lines.append(f"- **Generated at:** {card.get('generated_at')}")
    universe = card.get("universe") or []
    if universe:
        lines.append(f"- **Universe ({len(universe)}):** {', '.join(universe[:20])}"
                     + (" …" if len(universe) > 20 else ""))
    lines.append("")
    sections = card.get("sections", {}) or {}

    def _section_block(title: str, body: dict[str, Any]) -> None:
        lines.append(f"## {title}")
        if body.get("description"):
            lines.append(body["description"])
        if body.get("method"):
            lines.append("")
            lines.append(f"**Method.** {body['method']}")
        summary = body.get("summary")
        if summary:
            lines.append("")
            lines.append("**Summary.**")
            for k, v in summary.items():
                lines.append(f"- `{k}`: {v}")
        lines.append("")

    if "factor_loadings" in sections:
        _section_block("1. Multi-Factor Regression", sections["factor_loadings"])
        top = sections["factor_loadings"].get("top_loadings") or []
        if top:
            lines.append("**Top loadings (|t-stat|).**")
            lines.append("")
            lines.append("| Symbol | Factor | β | t-stat | R² |")
            lines.append("|---|---|---:|---:|---:|")
            for r in top[:10]:
                lines.append(
                    f"| {r.get('symbol','')} | {r.get('factor','')} | "
                    f"{_fmt(r.get('beta'))} | {_fmt(r.get('t_stat'))} | "
                    f"{_fmt(r.get('r_squared'))} |"
                )
            lines.append("")
    if "har_rv" in sections:
        _section_block("2. HAR-RV Volatility Forecast", sections["har_rv"])
    if "granger" in sections:
        _section_block("3. Granger Causality Lead-Lag", sections["granger"])
    if "slippage_model" in sections:
        _section_block("4. Slippage Impact Model", sections["slippage_model"])
        m = sections["slippage_model"].get("model")
        if m:
            lines.append("**Coefficients.**")
            for k in ("alpha", "beta_size", "beta_vol", "r_squared", "sample_count"):
                lines.append(f"- `{k}`: {m.get(k)}")
            lines.append("")
    if "correlation_regime" in sections:
        _section_block("5. Correlation Regime", sections["correlation_regime"])
        latest = sections["correlation_regime"].get("latest")
        if latest:
            lines.append("**Latest snapshot.**")
            for k in ("regime", "avg_corr", "z_score", "n_pairs", "recorded_at"):
                lines.append(f"- `{k}`: {latest.get(k)}")
            lines.append("")

    attribution = card.get("attribution")
    if attribution:
        lines.append("## Self-Learning Attribution")
        lines.append("")
        lines.append(
            "Per-feature hit-rate and avg PnL of decisions taken under each "
            "quant signal bucket. Powers the learning loop's adjustments to "
            "strategy weights and confidence multipliers."
        )
        lines.append("")
        for bucket, stats in (attribution.get("by_bucket") or {}).items():
            lines.append(f"### {bucket}")
            for sub, st in (stats or {}).items():
                lines.append(
                    f"- **{sub}**: n={st.get('n', 0)}, "
                    f"hit_rate={_pct(st.get('hit_rate'))}, "
                    f"avg_pnl_pct={_fmt(st.get('avg_pnl_pct'))}"
                )
            lines.append("")

    lines.append("## Limitations")
    for lim in card.get("limitations") or []:
        lines.append(f"- {lim}")
    lines.append("")
    lines.append("## Usage Guidance")
    for g in card.get("usage_guidance") or []:
        lines.append(f"- {g}")
    lines.append("")
    return "\n".join(lines)


def _fmt(v: Any) -> str:
    f = _safe_float(v)
    if f is None:
        return "—"
    if abs(f) < 1e-3 or abs(f) >= 1e4:
        return f"{f:.3e}"
    return f"{f:.4g}"


def _pct(v: Any) -> str:
    f = _safe_float(v)
    if f is None:
        return "—"
    return f"{f * 100:.1f}%"


def export_to_disk(card: dict[str, Any], path: str) -> None:
    """Persist the JSON form to ``path`` (parents must exist)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(card, fh, indent=2, default=str)


__all__ = [
    "MODEL_CARD_VERSION",
    "build_model_card",
    "format_markdown",
    "export_to_disk",
]
