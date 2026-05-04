"""Unit tests for ``src.analysis.regression_factor.build_regression_factor``.

Covers the three functional contracts:

1. **Disabled by default** — without the env flag or yaml opt-in, the
   factor is always 1.0 (observational mode preserved).
2. **Quality gate** — a fitted model with R² < 0.10 OR N < 10 must
   produce a no-op even when enabled.
3. **Real impact** — enabled + strong fit + imminent matching catalyst
   produces a bounded multiplier in the documented direction.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from src.analysis.regression_factor import build_regression_factor


class _StubDB:
    """Minimal DB stub matching the two methods the factor uses."""

    def __init__(self, *, upcoming=None, regressions=None):
        self._upcoming = upcoming or []
        self._regressions = regressions or []

    def get_upcoming_catalysts(self, *, exchange, horizon_days, symbol):  # noqa: ARG002
        return list(self._upcoming)

    def get_event_regressions(
        self,
        *,
        exchange,  # noqa: ARG002
        symbol=None,  # noqa: ARG002
        event_type=None,  # noqa: ARG002
        order_by="r_squared",  # noqa: ARG002
        limit=5,  # noqa: ARG002
    ):
        return list(self._regressions)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Ensure env flag is unset between tests so default behaviour is exercised."""
    monkeypatch.delenv("REGRESSION_RISK_FACTOR_ENABLED", raising=False)


# ---------------------------------------------------------------------------
# Contract 1 — disabled by default
# ---------------------------------------------------------------------------

def test_disabled_by_default_no_op():
    """No env flag, no yaml → must be a 1.0 no-op even with perfect inputs."""
    soon = datetime.now(timezone.utc) + timedelta(hours=12)
    db = _StubDB(
        upcoming=[{"event_type": "CPI", "event_ts": soon}],
        regressions=[{
            "symbol": "BTC-USD", "event_type": "CPI", "horizon_days": 5,
            "sample_count": 50, "r_squared": 0.45,
            "mean_forward_return": 0.02, "hit_rate": 0.7,
        }],
    )
    out = build_regression_factor(db=db, exchange="coinbase", symbol="BTC-USD")
    assert out["applied"] is False
    assert out["factor"] == 1.0
    assert out["reason"] == "disabled"


def test_env_flag_enables(monkeypatch):
    """Setting the env flag is sufficient — yaml opt-in not required."""
    monkeypatch.setenv("REGRESSION_RISK_FACTOR_ENABLED", "1")
    soon = datetime.now(timezone.utc) + timedelta(hours=12)
    db = _StubDB(
        upcoming=[{"event_type": "CPI", "event_ts": soon}],
        regressions=[{
            "symbol": "BTC-USD", "event_type": "CPI", "horizon_days": 5,
            "sample_count": 50, "r_squared": 0.45,
            "mean_forward_return": 0.02, "hit_rate": 0.7,
        }],
    )
    out = build_regression_factor(db=db, exchange="coinbase", symbol="BTC-USD")
    assert out["applied"] is True
    assert 0.9 <= out["factor"] <= 1.1
    assert out["direction"] == "bullish"


def test_yaml_opt_in_enables():
    """The risk_config dict alone (no env) should be enough to enable."""
    soon = datetime.now(timezone.utc) + timedelta(hours=12)
    db = _StubDB(
        upcoming=[{"event_type": "CPI", "event_ts": soon}],
        regressions=[{
            "symbol": "BTC-USD", "event_type": "CPI", "horizon_days": 5,
            "sample_count": 50, "r_squared": 0.45,
            "mean_forward_return": 0.02, "hit_rate": 0.7,
        }],
    )
    out = build_regression_factor(
        db=db, exchange="coinbase", symbol="BTC-USD",
        risk_config={"use_regression_factor": True},
    )
    assert out["applied"] is True


# ---------------------------------------------------------------------------
# Contract 2 — quality gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "r2,n,reason_substring",
    [
        (0.05, 50, "weak_fit"),
        (0.45, 5, "weak_fit"),
        (None, 50, "r_squared_invalid"),
        (float("nan"), 50, "r_squared_invalid"),
    ],
)
def test_weak_fit_does_not_apply(monkeypatch, r2, n, reason_substring):
    """Below-threshold fits are visible but never affect sizing."""
    monkeypatch.setenv("REGRESSION_RISK_FACTOR_ENABLED", "1")
    soon = datetime.now(timezone.utc) + timedelta(hours=12)
    db = _StubDB(
        upcoming=[{"event_type": "CPI", "event_ts": soon}],
        regressions=[{
            "symbol": "BTC-USD", "event_type": "CPI", "horizon_days": 5,
            "sample_count": n, "r_squared": r2,
            "mean_forward_return": 0.02, "hit_rate": 0.7,
        }],
    )
    out = build_regression_factor(db=db, exchange="coinbase", symbol="BTC-USD")
    assert out["applied"] is False
    assert out["factor"] == 1.0
    assert reason_substring in out["reason"]
    # Diagnostic data still surfaced — operator sees the model in the UI.
    assert out["model"] is not None


def test_no_upcoming_catalyst_no_op(monkeypatch):
    """Even with a strong fit, an absent catalyst means nothing to act on."""
    monkeypatch.setenv("REGRESSION_RISK_FACTOR_ENABLED", "1")
    db = _StubDB(upcoming=[], regressions=[])
    out = build_regression_factor(db=db, exchange="coinbase", symbol="BTC-USD")
    assert out["applied"] is False
    assert out["reason"] == "no_upcoming_catalyst"


def test_event_outside_window_no_op(monkeypatch):
    """A catalyst 30 days out is past the 72h window — no-op."""
    monkeypatch.setenv("REGRESSION_RISK_FACTOR_ENABLED", "1")
    far = datetime.now(timezone.utc) + timedelta(days=30)
    db = _StubDB(
        upcoming=[{"event_type": "CPI", "event_ts": far}],
        regressions=[{
            "symbol": "BTC-USD", "event_type": "CPI", "horizon_days": 5,
            "sample_count": 50, "r_squared": 0.45,
            "mean_forward_return": 0.02, "hit_rate": 0.7,
        }],
    )
    out = build_regression_factor(db=db, exchange="coinbase", symbol="BTC-USD")
    assert out["applied"] is False
    # The far-future event is also outside the upcoming horizon (<=3 days)
    # so no_upcoming_catalyst is acceptable; if a future-tightened helper
    # surfaces it, no_catalyst_in_window is also a valid reason.
    assert out["reason"] in {"no_upcoming_catalyst", "no_catalyst_in_window"}


def test_regression_event_type_mismatch(monkeypatch):
    """Imminent catalyst exists but no regression for that event type."""
    monkeypatch.setenv("REGRESSION_RISK_FACTOR_ENABLED", "1")
    soon = datetime.now(timezone.utc) + timedelta(hours=12)
    db = _StubDB(
        upcoming=[{"event_type": "EARNINGS", "event_ts": soon}],
        regressions=[],
    )
    out = build_regression_factor(db=db, exchange="coinbase", symbol="BTC-USD")
    assert out["applied"] is False
    assert out["reason"] == "no_regression_for_event_type"


# ---------------------------------------------------------------------------
# Contract 3 — real impact, bounded
# ---------------------------------------------------------------------------

def test_bullish_factor_in_bounds(monkeypatch):
    """Strong fit + positive mean fwd return → bullish bounded multiplier."""
    monkeypatch.setenv("REGRESSION_RISK_FACTOR_ENABLED", "1")
    soon = datetime.now(timezone.utc) + timedelta(hours=12)
    db = _StubDB(
        upcoming=[{"event_type": "CPI", "event_ts": soon}],
        regressions=[{
            "symbol": "BTC-USD", "event_type": "CPI", "horizon_days": 5,
            "sample_count": 50, "r_squared": 0.45,
            "mean_forward_return": 0.04, "hit_rate": 0.65,
        }],
    )
    out = build_regression_factor(db=db, exchange="coinbase", symbol="BTC-USD")
    assert out["applied"] is True
    assert out["direction"] == "bullish"
    assert 1.0 < out["factor"] <= 1.1
    assert out["model"]["r_squared"] == 0.45


def test_bearish_factor_in_bounds(monkeypatch):
    """Strong fit + negative mean fwd return → bearish bounded multiplier."""
    monkeypatch.setenv("REGRESSION_RISK_FACTOR_ENABLED", "1")
    soon = datetime.now(timezone.utc) + timedelta(hours=12)
    db = _StubDB(
        upcoming=[{"event_type": "CPI", "event_ts": soon}],
        regressions=[{
            "symbol": "BTC-USD", "event_type": "CPI", "horizon_days": 5,
            "sample_count": 50, "r_squared": 0.45,
            "mean_forward_return": -0.06, "hit_rate": 0.40,
        }],
    )
    out = build_regression_factor(db=db, exchange="coinbase", symbol="BTC-USD")
    assert out["applied"] is True
    assert out["direction"] == "bearish"
    assert 0.9 <= out["factor"] < 1.0


def test_factor_clipped_to_max(monkeypatch):
    """Even an extreme mfr*R² product is hard-capped at 1.10 / 0.90."""
    monkeypatch.setenv("REGRESSION_RISK_FACTOR_ENABLED", "1")
    soon = datetime.now(timezone.utc) + timedelta(hours=12)
    db = _StubDB(
        upcoming=[{"event_type": "CPI", "event_ts": soon}],
        regressions=[{
            "symbol": "BTC-USD", "event_type": "CPI", "horizon_days": 5,
            "sample_count": 500, "r_squared": 0.95,
            "mean_forward_return": 5.0,  # absurd: 500% expected fwd
            "hit_rate": 1.0,
        }],
    )
    out = build_regression_factor(db=db, exchange="coinbase", symbol="BTC-USD")
    assert out["applied"] is True
    assert out["factor"] == pytest.approx(1.1, rel=1e-6)


def test_iso_string_event_ts_supported(monkeypatch):
    """upcoming_catalysts may surface event_ts as ISO string — both must work."""
    monkeypatch.setenv("REGRESSION_RISK_FACTOR_ENABLED", "1")
    soon_iso = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    db = _StubDB(
        upcoming=[{"event_type": "CPI", "event_ts": soon_iso}],
        regressions=[{
            "symbol": "BTC-USD", "event_type": "CPI", "horizon_days": 5,
            "sample_count": 50, "r_squared": 0.45,
            "mean_forward_return": 0.02, "hit_rate": 0.7,
        }],
    )
    out = build_regression_factor(db=db, exchange="coinbase", symbol="BTC-USD")
    assert out["applied"] is True


# ---------------------------------------------------------------------------
# Contract 4 — auto mode (system flips factor on based on model performance)
# ---------------------------------------------------------------------------

def _strong_model_db(*, hit_rate=0.7, n=50, r2=0.45, mfr=0.02):
    soon = datetime.now(timezone.utc) + timedelta(hours=12)
    return _StubDB(
        upcoming=[{"event_type": "CPI", "event_ts": soon}],
        regressions=[{
            "symbol": "BTC-USD", "event_type": "CPI", "horizon_days": 5,
            "sample_count": n, "r_squared": r2,
            "mean_forward_return": mfr, "hit_rate": hit_rate,
        }],
    )


def test_auto_mode_promotes_proven_model(monkeypatch):
    """auto mode + model that has demonstrated directional accuracy → applied."""
    monkeypatch.setenv("REGRESSION_RISK_FACTOR_MODE", "auto")
    out = build_regression_factor(
        db=_strong_model_db(hit_rate=0.62, n=40, r2=0.20, mfr=0.05),
        exchange="coinbase", symbol="BTC-USD",
    )
    assert out["mode"] == "auto"
    assert out["applied"] is True
    assert out["reason"] == "ok:auto"
    assert out["auto_gate"]["passed"] is True


@pytest.mark.parametrize(
    "hit_rate,n,r2,reason_token",
    [
        (0.51, 50, 0.20, "hit_rate"),     # below 0.55 directional
        (0.65, 20, 0.20, "N "),            # below 30 samples
        (0.65, 50, 0.12, "R\u00b2"),       # below 0.15 r²
    ],
)
def test_auto_mode_rejects_unproven_model(monkeypatch, hit_rate, n, r2, reason_token):
    """auto mode must not move size on a model that hasn't proven itself."""
    monkeypatch.setenv("REGRESSION_RISK_FACTOR_MODE", "auto")
    out = build_regression_factor(
        db=_strong_model_db(hit_rate=hit_rate, n=n, r2=r2),
        exchange="coinbase", symbol="BTC-USD",
    )
    assert out["mode"] == "auto"
    assert out["applied"] is False
    assert out["factor"] == 1.0
    assert reason_token in out["reason"]
    assert out["auto_gate"]["passed"] is False


def test_auto_mode_yaml_setting(monkeypatch):
    """Profile yaml ``risk.regression_factor_mode: auto`` enables auto mode."""
    monkeypatch.delenv("REGRESSION_RISK_FACTOR_MODE", raising=False)
    out = build_regression_factor(
        db=_strong_model_db(hit_rate=0.62, n=40, r2=0.20, mfr=0.05),
        exchange="coinbase", symbol="BTC-USD",
        risk_config={"regression_factor_mode": "auto"},
    )
    assert out["mode"] == "auto"
    assert out["applied"] is True


def test_off_mode_kills_auto_yaml(monkeypatch):
    """Env ``off`` is a kill-switch — overrides any yaml auto/on setting."""
    monkeypatch.setenv("REGRESSION_RISK_FACTOR_MODE", "off")
    out = build_regression_factor(
        db=_strong_model_db(),
        exchange="coinbase", symbol="BTC-USD",
        risk_config={"regression_factor_mode": "auto"},
    )
    assert out["mode"] == "off"
    assert out["applied"] is False
    assert out["reason"] == "disabled"


def test_manual_on_mode_skips_auto_bar(monkeypatch):
    """Manual ``on`` honours operator override even when hit_rate is borderline."""
    monkeypatch.setenv("REGRESSION_RISK_FACTOR_MODE", "on")
    # Hit rate 0.51 would fail auto bar; manual override accepts it.
    out = build_regression_factor(
        db=_strong_model_db(hit_rate=0.51, n=50, r2=0.45),
        exchange="coinbase", symbol="BTC-USD",
    )
    assert out["mode"] == "on"
    assert out["applied"] is True
    assert out["reason"] == "ok"
