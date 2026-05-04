"""Unit tests for the QuantAnalytics → self-learning glue.

Covers the three pure-function modules that wire the QuantAnalytics
outputs into agent context, the model card export and the learning
attribution loop.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.utils.quant_context import (
    build_quant_context,
    format_decision_explanation,
    predict_slippage_bps,
)
from src.utils.quant_model_card import (
    MODEL_CARD_VERSION,
    build_model_card,
    format_markdown,
)
from src.utils.quant_attribution import (
    attribute_quant_signals,
    derive_learning_adjustments,
)


# ───────────────────────── quant_context ──────────────────────────────


def _stub_db_with_full_quant():
    """Mock stats_db returning canned quant rows for all five tables."""
    db = MagicMock()
    db.get_har_rv_forecast_for_symbol.return_value = {
        "forecast_vol": 0.025,
        "realized_vol_daily": 0.020,
        "realized_vol_weekly": 0.022,
        "realized_vol_monthly": 0.024,
        "model_r_squared": 0.42,
        "sample_count": 180,
    }
    db.get_market_factor_loadings.return_value = [
        {
            "symbol": "BTC-EUR", "factor": "BTC-USD",
            "beta": 0.95, "t_stat": 18.2, "r_squared": 0.85,
            "alpha_annualised": 0.06, "idio_vol": 0.40,
            "sample_count": 252, "computed_at": datetime.now(timezone.utc),
        },
        {
            "symbol": "BTC-EUR", "factor": "^GSPC",
            "beta": 0.10, "t_stat": 1.5, "r_squared": 0.10,
            "alpha_annualised": 0.06, "idio_vol": 0.40,
            "sample_count": 252, "computed_at": datetime.now(timezone.utc),
        },
    ]
    db.get_granger_results.return_value = [
        {
            "leader": "BTC-USD", "follower": "BTC-EUR",
            "lag_hours": 1, "f_stat": 8.5, "p_value": 0.003,
            "sample_count": 720, "computed_at": datetime.now(timezone.utc),
        },
    ]
    db.get_slippage_impact_model.return_value = {
        "alpha": 2.5, "beta_size": 45.0, "beta_vol": 850.0,
        "r_squared": 0.38, "sample_count": 142,
        "computed_at": datetime.now(timezone.utc),
    }
    db.get_correlation_regime_events.return_value = [
        {
            "regime": "elevated", "avg_corr": 0.52, "z_score": 1.8,
            "n_pairs": 45, "history_n": 60,
            "computed_at": datetime.now(timezone.utc),
        },
    ]
    return db


def test_build_quant_context_full_dict():
    ctx = build_quant_context(_stub_db_with_full_quant(), "coinbase", "BTC-EUR")
    assert ctx["available"] is True
    assert ctx["har_rv"]["forecast_vol"] == 0.025
    assert ctx["factor_alpha_annualised"] == 0.06
    assert ctx["idio_vol"] == 0.40
    assert ctx["granger_leaders"][0]["leader"] == "BTC-USD"
    assert ctx["slippage_model"]["beta_size"] == 45.0
    assert ctx["correlation_regime"]["regime"] == "elevated"


def test_build_quant_context_handles_missing_db():
    assert build_quant_context(None, "coinbase", "BTC-EUR")["available"] is False
    assert build_quant_context(MagicMock(), "", "BTC-EUR")["available"] is False
    assert build_quant_context(MagicMock(), "coinbase", "")["available"] is False


def test_build_quant_context_swallows_exceptions():
    db = MagicMock()
    db.get_har_rv_forecast_for_symbol.side_effect = RuntimeError("boom")
    db.get_market_factor_loadings.return_value = []
    db.get_granger_results.return_value = []
    db.get_slippage_impact_model.return_value = None
    db.get_correlation_regime_events.return_value = []
    ctx = build_quant_context(db, "coinbase", "BTC-EUR")
    assert ctx["available"] is False


def test_predict_slippage_bps():
    model = {"alpha": 2.0, "beta_size": 50.0, "beta_vol": 1000.0}
    assert predict_slippage_bps(
        model, notional=100.0, adv=10_000.0, realised_vol=0.02,
    ) == pytest.approx(2.0 + 50.0 * 0.01 + 1000.0 * 0.02)
    assert predict_slippage_bps(None, notional=1, adv=1, realised_vol=0) is None
    assert predict_slippage_bps(
        {"alpha": 1.0}, notional=1, adv=1, realised_vol=0,
    ) is None
    assert predict_slippage_bps(
        model, notional=1, adv=0, realised_vol=0,
    ) is None


def test_format_decision_explanation_renders_lines():
    ctx = build_quant_context(_stub_db_with_full_quant(), "coinbase", "BTC-EUR")
    lines = format_decision_explanation(ctx, action="buy")
    # Each of the five signals contributed at least one explanation line.
    assert any("HAR-RV" in ln for ln in lines)
    assert any("Correlation regime" in ln for ln in lines)
    assert any("Granger" in ln for ln in lines)
    assert any("Factor" in ln for ln in lines)
    assert any("Slippage" in ln for ln in lines)


def test_format_decision_explanation_empty_when_unavailable():
    assert format_decision_explanation({"available": False}) == []
    assert format_decision_explanation({}) == []


# ───────────────────────── quant_model_card ───────────────────────────


def test_build_model_card_basic_shape():
    card = build_model_card(None, "coinbase")
    assert card["model_card_version"] == MODEL_CARD_VERSION
    assert card["exchange"] == "coinbase"
    assert "limitations" in card and len(card["limitations"]) >= 5
    assert "usage_guidance" in card and len(card["usage_guidance"]) >= 5
    # No DB → no sections
    assert card["sections"] == {}


def test_build_model_card_with_db_populates_sections():
    db = MagicMock()
    db.get_market_factor_loadings.return_value = [
        {
            "symbol": "BTC-EUR", "factor": "BTC-USD",
            "beta": 0.9, "t_stat": 12.5, "r_squared": 0.82,
            "alpha_annualised": 0.04, "idio_vol": 0.38,
            "sample_count": 252, "computed_at": datetime.now(timezone.utc),
        },
    ]
    db.get_har_rv_forecasts.return_value = [
        {
            "symbol": "ETH-EUR", "horizon_days": 1,
            "forecast_vol": 0.03, "realized_vol_daily": 0.025,
            "realized_vol_weekly": 0.027, "realized_vol_monthly": 0.029,
            "model_r_squared": 0.30, "sample_count": 200,
            "computed_at": datetime.now(timezone.utc),
        },
    ]
    db.get_granger_results.return_value = [
        {
            "leader": "BTC", "follower": "ETH",
            "lag_hours": 1, "f_stat": 10.0, "p_value": 0.001,
            "sample_count": 500, "computed_at": datetime.now(timezone.utc),
        },
    ]
    db.get_slippage_impact_model.return_value = {
        "alpha": 1.0, "beta_size": 40.0, "beta_vol": 700.0,
        "r_squared": 0.4, "sample_count": 100,
        "computed_at": datetime.now(timezone.utc),
    }
    db.get_correlation_regime_events.return_value = [
        {
            "regime": "normal", "avg_corr": 0.30, "z_score": -0.2,
            "n_pairs": 45, "history_n": 60,
            "computed_at": datetime.now(timezone.utc),
        },
    ]
    card = build_model_card(db, "coinbase", universe=["BTC-EUR", "ETH-EUR"])
    sec = card["sections"]
    assert sec["factor_loadings"]["summary"]["rows"] == 1
    assert sec["factor_loadings"]["top_loadings"][0]["symbol"] == "BTC-EUR"
    assert sec["har_rv"]["summary"]["rows"] == 1
    assert sec["granger"]["summary"]["significant_edges"] == 1
    assert sec["slippage_model"]["model"]["beta_size"] == 40.0
    assert sec["correlation_regime"]["latest"]["regime"] == "normal"


def test_format_markdown_renders_all_sections():
    card = build_model_card(None, "coinbase")
    md = format_markdown(card)
    assert "# Quant Analytics Model Card" in md
    assert "## Limitations" in md
    assert "## Usage Guidance" in md


def test_format_markdown_with_attribution_block():
    card = build_model_card(None, "coinbase")
    card["attribution"] = {
        "by_bucket": {
            "correlation_regime": {
                "normal": {"n": 20, "hit_rate": 0.6, "avg_pnl_pct": 0.012},
                "breakdown": {"n": 5, "hit_rate": 0.3, "avg_pnl_pct": -0.02},
            },
        },
    }
    md = format_markdown(card)
    assert "Self-Learning Attribution" in md
    assert "correlation_regime" in md
    assert "normal" in md and "breakdown" in md


# ───────────────────────── quant_attribution ──────────────────────────


def _fake_conn(rows):
    """Build a MagicMock conn whose .execute(...).fetchall() returns rows."""
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    conn = MagicMock()
    conn.execute.return_value = cursor
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False
    return cm


def test_attribute_quant_signals_aggregates_buckets():
    db = MagicMock()
    db.get_quant_decision_snapshots.return_value = [
        {
            "cycle_id": "c1", "pair": "BTC-EUR",
            "har_rv_forecast": 0.03, "har_rv_realized": 0.02,
            "factor_alpha": 0.10, "idio_vol": 0.4,
            "granger_leader_count": 2,
            "corr_regime": "elevated", "corr_z_score": 1.5,
            "action": "buy", "confidence": 0.7,
        },
        {
            "cycle_id": "c2", "pair": "ETH-EUR",
            "har_rv_forecast": 0.018, "har_rv_realized": 0.022,
            "factor_alpha": -0.10, "idio_vol": 0.5,
            "granger_leader_count": 0,
            "corr_regime": "normal", "corr_z_score": -0.1,
            "action": "buy", "confidence": 0.65,
        },
        {
            "cycle_id": "c3", "pair": "BTC-EUR",
            "har_rv_forecast": 0.025, "har_rv_realized": 0.025,
            "factor_alpha": 0.0, "idio_vol": 0.3,
            "granger_leader_count": 1,
            "corr_regime": "normal", "corr_z_score": 0.2,
            "action": "buy", "confidence": 0.6,
        },
    ]
    # Mock _get_conn for the outcomes join
    db._get_conn.return_value = _fake_conn([
        {"cycle_id": "c1", "pair": "BTC-EUR", "is_correct": False, "pnl_pct": -0.015},
        {"cycle_id": "c2", "pair": "ETH-EUR", "is_correct": True,  "pnl_pct":  0.020},
        {"cycle_id": "c3", "pair": "BTC-EUR", "is_correct": True,  "pnl_pct":  0.005},
    ])
    out = attribute_quant_signals(db, "coinbase", lookback_days=30)
    assert out["decisions_total"] == 3
    assert out["outcomes_total"] == 3
    bb = out["by_bucket"]
    # har_rv: c1 elevated (forecast/realized=1.5), c2 calm (0.82), c3 stable (1.0)
    assert "elevated" in bb["har_rv_regime"]
    assert "calm" in bb["har_rv_regime"]
    assert "stable" in bb["har_rv_regime"]
    assert bb["har_rv_regime"]["elevated"]["n"] == 1
    assert bb["correlation_regime"]["normal"]["n"] == 2
    assert bb["correlation_regime"]["elevated"]["n"] == 1
    assert bb["factor_alpha"]["positive"]["n"] == 1
    assert bb["factor_alpha"]["negative"]["n"] == 1
    assert bb["granger_leaders"]["weak"]["n"] == 2  # counts 1 and 2 → "weak"
    assert bb["granger_leaders"]["none"]["n"] == 1
    # feature_signal_strength bounded
    assert -1.0 <= out["feature_signal_strength"] <= 1.0


def test_attribute_quant_signals_empty_when_no_db():
    out = attribute_quant_signals(None, "coinbase")
    assert out["decisions_total"] == 0
    assert out["outcomes_total"] == 0


def test_derive_learning_adjustments_bounded_and_threshold():
    # Below n=10 threshold → no adjustment for that regime
    a = derive_learning_adjustments({
        "by_bucket": {
            "correlation_regime": {
                "normal": {"n": 5, "hit_rate": 0.9, "avg_pnl_pct": 0.05},
            },
        },
        "feature_signal_strength": 0.0,
    })
    assert a["confidence_multiplier_by_regime"] == {}

    # Above threshold with strong positive PnL → boost capped at +20%
    a = derive_learning_adjustments({
        "by_bucket": {
            "correlation_regime": {
                "normal": {"n": 50, "hit_rate": 0.7, "avg_pnl_pct": 0.10},
            },
            "har_rv_regime": {
                "elevated": {"n": 20, "hit_rate": 0.3, "avg_pnl_pct": -0.02},
                "calm": {"n": 20, "hit_rate": 0.7, "avg_pnl_pct": 0.015},
            },
        },
        "feature_signal_strength": 0.5,
    })
    assert a["confidence_multiplier_by_regime"]["normal"] <= 1.20
    assert a["confidence_multiplier_by_regime"]["normal"] >= 1.00
    assert a["har_rv_action"] in ("size_down", "size_up")
    assert 0.0 <= a["trust_score"] <= 1.0
    assert a["rationale"]


def test_derive_learning_adjustments_handles_empty_input():
    a = derive_learning_adjustments({})
    assert a["confidence_multiplier_by_regime"] == {}
    assert a["har_rv_action"] == "neutral"
    assert a["trust_score"] == 0.5
