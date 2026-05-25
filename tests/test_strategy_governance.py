from __future__ import annotations

from src.core.strategy_governance import build_strategy_policy


def test_strategy_policy_promotes_fee_clearing_multi_horizon_edge():
    policy = build_strategy_policy(
        pair="ASML.AS-EUR",
        exchange="ibkr",
        current_price=100.0,
        config={
            "trading": {"min_hold_minutes": 60},
            "risk": {"take_profit_pct": 0.03},
        },
        market_signal={"signal_type": "buy", "confidence": 0.82},
        strategy_signals={
            "_ensemble": {"action": "buy", "confidence": 0.80, "agreement": 0.75},
        },
        pattern_signal={
            "available": True,
            "direction": "bullish",
            "confidence": 0.70,
            "expected_drift_pct": 0.04,
            "horizon_days": 5,
        },
        kelly_stats={"win_rate": 0.62, "sample_size": 30, "avg_win": 2.0, "avg_loss": 1.0},
        planning_outlook={
            "direction": "bullish",
            "gain_pct": 0.08,
            "confidence": 0.72,
            "horizon_days": 7,
        },
        fee_context={"min_gain_pct": 0.012, "breakeven_pct": 0.010},
    )

    assert policy["posture"] == "trade"
    assert policy["strategy_horizon_days"] == 7
    assert policy["target_gain_pct"] >= 0.08
    assert policy["expected_net_return_pct"] > 0
    assert policy["size_multiplier"] > 1.0
    assert policy["min_hold_minutes"] >= 12 * 60


def test_strategy_policy_blocks_negative_evidence_before_new_buys():
    policy = build_strategy_policy(
        pair="BEER.HE-EUR",
        exchange="ibkr",
        current_price=2.50,
        config={"risk": {"take_profit_pct": 0.02}},
        market_signal={"signal_type": "neutral", "confidence": 0.40},
        pattern_signal={"available": True, "direction": "bearish", "confidence": 0.80},
        regression_factor={
            "applied": True,
            "factor": 0.94,
            "direction": "bearish",
            "model": {"r_squared": 0.20},
        },
        kelly_stats={"win_rate": 0.30, "sample_size": 30, "avg_win": 1.0, "avg_loss": 2.0},
        fee_context={"min_gain_pct": 0.055, "breakeven_pct": 0.045},
    )

    assert policy["posture"] == "block"
    assert policy["size_multiplier"] == 0.0
    assert policy["confidence_adjustment"] >= 0.9
    assert policy["invalidation_reasons"]
