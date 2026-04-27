"""Tests for src/utils/cvar.py — pure-function risk helpers."""
from src.utils.cvar import cvar_for_regime, shrunk_kelly, regime_stop_multiplier


def test_cvar_basic():
    rs = [-0.05, -0.04, -0.03, -0.02, -0.01] + [0.01] * 50
    res = cvar_for_regime(rs, alpha=0.05)
    assert res is not None
    assert res["n"] == 55
    assert res["cvar"] <= res["var"] <= 0.0


def test_cvar_too_few():
    assert cvar_for_regime([0.01] * 10) is None


def test_shrunk_kelly_zero_when_negative_edge():
    # avg_win < avg_loss with win_rate 0.5 → negative Kelly → 0
    assert shrunk_kelly(0.5, 0.005, 0.01, 100) == 0.0


def test_shrunk_kelly_positive_clamped():
    f = shrunk_kelly(0.6, 0.02, 0.01, 1000, kelly_fraction=0.5, kelly_cap=0.25)
    assert 0 < f <= 0.25


def test_shrunk_kelly_shrinkage_pulls_toward_zero():
    big = shrunk_kelly(0.6, 0.02, 0.01, 10_000, shrinkage_n=50.0)
    small = shrunk_kelly(0.6, 0.02, 0.01, 5, shrinkage_n=50.0)
    assert small < big


def test_regime_stop_multiplier_known():
    assert regime_stop_multiplier("HIGH_VOL") == 1.3
    assert regime_stop_multiplier("chop") == 0.75
    assert regime_stop_multiplier("trending_up") == 1.0


def test_regime_stop_multiplier_corr_spike_clamped():
    v = regime_stop_multiplier("HIGH_VOL", correlation_spike=True)
    assert 0.5 <= v <= 1.5
    # 1.3 + 0.1 = 1.4
    assert abs(v - 1.4) < 1e-9


def test_regime_stop_multiplier_unknown_defaults_to_1():
    assert regime_stop_multiplier("totally_unknown") == 1.0
