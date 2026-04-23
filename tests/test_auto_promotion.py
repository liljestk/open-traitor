"""Tests for WFOAutoPromoter (Phase 2d auto-promotion)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.backtesting.auto_promotion import (
    PromotionConfig,
    WFOAutoPromoter,
)
from src.backtesting.walk_forward import WFOResult, WFOWindowResult


def _wfo(
    *,
    windows=5,
    avg_wfe=0.8,
    avg_oos_sharpe=1.0,
    avg_oos_return=0.05,
    combined=0.20,
    params=None,
):
    if params is None:
        params = {"position_size_pct": 0.10, "stop_pct": 0.04}
    return WFOResult(
        total_windows=windows,
        avg_wfe=avg_wfe,
        avg_oos_return=avg_oos_return,
        avg_oos_sharpe=avg_oos_sharpe,
        combined_oos_return=combined,
        is_robust=avg_wfe >= 0.5,
        windows=[
            WFOWindowResult(
                window_index=i, is_start="", is_end="", oos_start="", oos_end="",
                best_params={"a": 1}, is_return=0.05, is_sharpe=1.0,
                oos_return=avg_oos_return, oos_sharpe=avg_oos_sharpe, wfe=avg_wfe,
            )
            for i in range(windows)
        ],
        param_grid={},
        best_overall_params=params,
    )


@pytest.fixture
def promoter(tmp_path: Path) -> WFOAutoPromoter:
    return WFOAutoPromoter(
        config_root=str(tmp_path / "config" / "strategies"),
        audit_root=str(tmp_path / "data"),
    )


# --------------------------------------------------------------------- #

class TestHardGates:
    def test_min_windows_gate(self, promoter):
        d = promoter.evaluate("zscore_mean_reversion", _wfo(windows=2))
        assert not d.promoted and "windows" in d.reason

    def test_wfe_gate(self, promoter):
        d = promoter.evaluate("zscore_mean_reversion", _wfo(avg_wfe=0.4))
        assert not d.promoted and "wfe" in d.reason

    def test_sharpe_gate(self, promoter):
        d = promoter.evaluate("zscore_mean_reversion", _wfo(avg_oos_sharpe=0.1))
        assert not d.promoted and "sharpe" in d.reason

    def test_combined_return_gate(self, promoter):
        d = promoter.evaluate("zscore_mean_reversion", _wfo(combined=-0.05))
        assert not d.promoted and "combined" in d.reason

    def test_empty_params_blocked(self, promoter):
        d = promoter.evaluate("z", _wfo(params={}))
        assert not d.promoted and "empty" in d.reason


# --------------------------------------------------------------------- #

class TestPromotion:
    def test_first_promotion_writes_file(self, promoter, tmp_path):
        d = promoter.evaluate("z", _wfo(), exchange="coinbase", pair="BTC-USD")
        assert d.promoted
        live = tmp_path / "config" / "strategies" / "z.live.yaml"
        assert live.exists()
        data = yaml.safe_load(live.read_text())
        assert data["strategy"] == "z"
        assert data["exchange"] == "coinbase"
        assert data["pair"] == "BTC-USD"
        assert data["params"]["position_size_pct"] == 0.10
        assert data["metrics"]["oos_sharpe"] == 1.0

    def test_promotion_writes_audit_line(self, promoter, tmp_path):
        promoter.evaluate("z", _wfo(), exchange="coinbase")
        audit = tmp_path / "data" / "coinbase" / "audit" / "wfo_promotions.jsonl"
        assert audit.exists()
        lines = audit.read_text().strip().splitlines()
        assert len(lines) == 1
        rec = yaml.safe_load(lines[0])
        assert rec["strategy"] == "z"

    def test_no_regression_blocks_worse_candidate(self, promoter):
        promoter.evaluate("z", _wfo(avg_oos_sharpe=1.5))
        # Worse candidate.
        d = promoter.evaluate("z", _wfo(avg_oos_sharpe=1.0))
        assert not d.promoted and "no_improvement" in d.reason
        assert d.prev_oos_sharpe == pytest.approx(1.5)

    def test_force_overrides_no_regression(self, promoter):
        promoter.evaluate("z", _wfo(avg_oos_sharpe=1.5))
        d = promoter.evaluate("z", _wfo(avg_oos_sharpe=1.0), force=True)
        assert d.promoted

    def test_better_candidate_promotes_and_snapshots_prev(self, promoter, tmp_path):
        promoter.evaluate(
            "z",
            _wfo(avg_oos_sharpe=1.0, params={"x": 1}),
        )
        d = promoter.evaluate(
            "z",
            _wfo(avg_oos_sharpe=2.0, params={"x": 2}),
        )
        assert d.promoted
        live = tmp_path / "config" / "strategies" / "z.live.yaml"
        prev = tmp_path / "config" / "strategies" / "z.live.prev.yaml"
        assert live.exists() and prev.exists()
        assert yaml.safe_load(live.read_text())["params"]["x"] == 2
        assert yaml.safe_load(prev.read_text())["params"]["x"] == 1


# --------------------------------------------------------------------- #

class TestLoadAndRollback:
    def test_load_live_params_returns_none_when_missing(self, promoter):
        assert promoter.load_live_params("nope") is None

    def test_load_live_params_returns_dict(self, promoter):
        promoter.evaluate("z", _wfo(params={"a": 7}))
        assert promoter.load_live_params("z") == {"a": 7}

    def test_rollback_no_prev(self, promoter):
        promoter.evaluate("z", _wfo())
        assert promoter.rollback("z") is False

    def test_rollback_restores_previous(self, promoter):
        promoter.evaluate("z", _wfo(avg_oos_sharpe=1.0, params={"v": 1}))
        promoter.evaluate("z", _wfo(avg_oos_sharpe=2.0, params={"v": 2}))
        assert promoter.load_live_params("z") == {"v": 2}
        assert promoter.rollback("z") is True
        assert promoter.load_live_params("z") == {"v": 1}


# --------------------------------------------------------------------- #

class TestCustomGate:
    def test_strict_gate_blocks(self, tmp_path):
        p = WFOAutoPromoter(
            config_root=str(tmp_path / "c"),
            audit_root=str(tmp_path / "d"),
            gate=PromotionConfig(min_wfe=0.95, min_oos_sharpe=0.0, min_windows=1),
        )
        d = p.evaluate("z", _wfo(avg_wfe=0.8))
        assert not d.promoted and "wfe" in d.reason
