"""Tests for PortfolioOptimizer and CapitalAllocator (Phase 4)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from src.core.capital_allocator import CapitalAllocator
from src.core.portfolio_optimizer import PortfolioOptimizer


# --------------------------------------------------------------------- #
# PortfolioOptimizer
# --------------------------------------------------------------------- #

class TestPortfolioOptimizer:
    def setup_method(self):
        self.opt = PortfolioOptimizer(
            risk_aversion=5.0, max_weight=0.5, min_weight=0.01,
        )

    def test_invalid_args(self):
        with pytest.raises(ValueError):
            PortfolioOptimizer(risk_aversion=0)
        with pytest.raises(ValueError):
            PortfolioOptimizer(max_weight=0)
        with pytest.raises(ValueError):
            PortfolioOptimizer(max_weight=0.3, min_weight=0.5)

    def test_empty_assets(self):
        out = self.opt.optimize(np.zeros((10, 0)), [])
        assert out.weights == ()

    def test_short_history_falls_back_equal(self):
        R = np.zeros((3, 3))
        out = self.opt.optimize(R, ["A", "B", "C"])
        # Equal weights, sum to 1.
        assert sum(out.weights) == pytest.approx(1.0, abs=1e-6)
        assert max(out.weights) - min(out.weights) < 1e-6

    def test_overweights_high_sharpe_asset(self):
        rng = np.random.default_rng(0)
        T = 200
        # Asset A: high mean, low vol; B: zero mean, high vol; C: zero mean, low vol.
        a = rng.normal(0.005, 0.005, T)
        b = rng.normal(0.000, 0.020, T)
        c = rng.normal(0.000, 0.005, T)
        R = np.column_stack([a, b, c])
        out = self.opt.optimize(R, ["A", "B", "C"])
        d = out.as_dict()
        assert d["A"] > d["B"]
        assert d["A"] > d["C"]
        assert sum(out.weights) == pytest.approx(1.0, abs=1e-4)

    def test_max_weight_cap_enforced(self):
        opt = PortfolioOptimizer(risk_aversion=1.0, max_weight=0.4, min_weight=0.0)
        rng = np.random.default_rng(1)
        # One dominant asset would otherwise grab >40 %.
        T = 300
        good = rng.normal(0.01, 0.005, T)
        weak = rng.normal(0.0, 0.01, T)
        R = np.column_stack([good, weak, weak, weak])
        out = opt.optimize(R, ["A", "B", "C", "D"])
        assert max(out.weights) <= 0.4 + 1e-6
        assert sum(out.weights) == pytest.approx(1.0, abs=1e-4)

    def test_returns_portfolio_metrics(self):
        rng = np.random.default_rng(2)
        R = rng.normal(0.001, 0.01, (250, 4))
        out = self.opt.optimize(R, [f"P{i}" for i in range(4)])
        assert out.expected_vol >= 0
        assert out.cvar_95 <= 0  # CVaR of losses → non-positive

    def test_dust_dropped(self):
        opt = PortfolioOptimizer(risk_aversion=20.0, max_weight=0.6, min_weight=0.05)
        rng = np.random.default_rng(3)
        # Two great assets, one clearly worse.
        good1 = rng.normal(0.005, 0.005, 200)
        good2 = rng.normal(0.005, 0.005, 200)
        bad = rng.normal(-0.02, 0.005, 200)
        R = np.column_stack([good1, good2, bad])
        out = opt.optimize(R, ["A", "B", "C"])
        d = out.as_dict()
        # Bad asset should be the smallest, and either dust-dropped (0)
        # or strictly below the good ones.
        assert d["C"] < d["A"] and d["C"] < d["B"]


# --------------------------------------------------------------------- #
# CapitalAllocator
# --------------------------------------------------------------------- #

class TestCapitalAllocator:
    def test_invalid_args(self):
        with pytest.raises(ValueError):
            CapitalAllocator(eta=0)
        with pytest.raises(ValueError):
            CapitalAllocator(min_weight=0.5, max_weight=0.4)

    def test_register_uniform(self):
        ca = CapitalAllocator(eta=0.1, min_weight=0.0, max_weight=1.0)
        ca.register(["a", "b", "c", "d"])
        w = ca.weights()
        assert all(abs(v - 0.25) < 1e-9 for v in w.values())
        assert sum(w.values()) == pytest.approx(1.0)

    def test_winners_gain_weight(self):
        ca = CapitalAllocator(eta=0.5, min_weight=0.05, max_weight=0.6)
        ca.register(["winner", "loser", "neutral"])
        for _ in range(20):
            ca.update({"winner": 0.02, "loser": -0.02, "neutral": 0.0})
        w = ca.weights()
        assert w["winner"] > w["neutral"] > w["loser"]
        assert sum(w.values()) == pytest.approx(1.0, abs=1e-6)
        assert all(self._floor_ok(v, 0.05) for v in w.values())
        assert all(v <= 0.6 + 1e-6 for v in w.values())

    def _floor_ok(self, v, floor):
        return v >= floor - 1e-6

    def test_loser_keeps_floor(self):
        ca = CapitalAllocator(eta=1.0, min_weight=0.05, max_weight=0.7)
        ca.register(["a", "b"])
        # Crush 'a' for many rounds.
        for _ in range(100):
            ca.update({"a": -0.1, "b": 0.1})
        w = ca.weights()
        assert w["a"] >= 0.05 - 1e-6  # floor preserved → recoverable
        assert w["b"] <= 0.7 + 1e-6

    def test_persistence_roundtrip(self, tmp_path):
        path = tmp_path / "alloc.json"
        ca = CapitalAllocator(state_path=str(path))
        ca.register(["a", "b", "c"])
        ca.update({"a": 0.05, "b": -0.05, "c": 0.0})
        # Re-load.
        ca2 = CapitalAllocator(state_path=str(path))
        for k in ["a", "b", "c"]:
            assert ca2.weights()[k] == pytest.approx(ca.weights()[k])

    def test_audit_log(self, tmp_path):
        ca = CapitalAllocator(audit_path=str(tmp_path / "audit.jsonl"))
        ca.register(["a", "b"])
        ca.update({"a": 0.01, "b": -0.01})
        ca.update({"a": 0.0, "b": 0.02})
        lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2

    def test_new_strategy_auto_registers(self):
        ca = CapitalAllocator(eta=0.1)
        ca.register(["a", "b"])
        ca.update({"a": 0.0, "b": 0.0, "c": 0.05})
        assert "c" in ca.weights()
        assert sum(ca.weights().values()) == pytest.approx(1.0, abs=1e-6)

    def test_reset(self):
        ca = CapitalAllocator()
        ca.register(["a", "b"])
        ca.update({"a": 0.5, "b": -0.5})
        ca.reset(["a", "b", "c"])
        w = ca.weights()
        assert all(abs(v - 1/3) < 1e-9 for v in w.values())

    def test_extreme_pnl_doesnt_blow_up(self):
        ca = CapitalAllocator(eta=10.0)  # extreme learning rate
        ca.register(["a", "b"])
        ca.update({"a": 1e6, "b": -1e6})  # extreme PnL
        w = ca.weights()
        assert all(np.isfinite(v) for v in w.values())
        assert sum(w.values()) == pytest.approx(1.0, abs=1e-6)

    def test_no_strategies_no_op(self):
        ca = CapitalAllocator()
        out = ca.update({})
        assert out == {}
