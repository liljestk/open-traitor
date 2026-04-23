"""Tests for the Signal Edge Library."""

from __future__ import annotations

import math
import time

import pytest

from src.analysis.signal_edge_library import (
    EdgeStats,
    InMemorySignalEdgeStore,
    SignalEdgeLibrary,
    SignalSample,
    _compute_edge,
)


# --------------------------------------------------------------------- #
# Pure helper: _compute_edge
# --------------------------------------------------------------------- #

class TestComputeEdge:
    def _sample(self, *, direction="long", forward=0.01, ts=None):
        return SignalSample(
            signal_name="x",
            regime="trending_up",
            direction=direction,
            score=1.0 if direction == "long" else -1.0 if direction == "short" else 0.0,
            forward_return=forward,
            pair="BTC-USD",
            exchange="coinbase",
            timestamp=ts if ts is not None else time.time(),
        )

    def test_empty_returns_zero_edge(self):
        e = _compute_edge("x", "trending_up", "coinbase", [])
        assert e.n_samples == 0 and e.sharpe == 0.0 and e.win_rate == 0.0

    def test_long_signal_with_positive_returns(self):
        s = [self._sample(direction="long", forward=0.01) for _ in range(10)]
        e = _compute_edge("x", "trending_up", "coinbase", s)
        assert e.n_samples == 10
        assert e.win_rate == 1.0
        assert e.avg_return == pytest.approx(0.01)
        # std=0 → sharpe defined as 0 by convention
        assert e.sharpe == 0.0

    def test_short_signal_rewarded_for_down_moves(self):
        # All forward returns -1%, direction short → adjusted = +1%
        s = [self._sample(direction="short", forward=-0.01) for _ in range(5)]
        e = _compute_edge("x", "trending_down", "coinbase", s)
        assert e.win_rate == 1.0
        assert e.avg_return == pytest.approx(0.01)

    def test_flat_signal_contributes_zero(self):
        s = [self._sample(direction="flat", forward=0.05) for _ in range(4)]
        e = _compute_edge("x", "chop", "coinbase", s)
        assert e.avg_return == 0.0
        assert e.win_rate == 0.0

    def test_mixed_returns_produce_finite_sharpe(self):
        # Alternating wins and losses, slight positive bias.
        returns = [0.02, -0.01, 0.015, -0.005, 0.01, -0.002, 0.012, -0.008]
        s = [self._sample(direction="long", forward=r) for r in returns]
        e = _compute_edge("x", "trending_up", "coinbase", s)
        assert e.n_samples == len(returns)
        assert math.isfinite(e.sharpe)
        assert e.sharpe > 0  # positive mean → positive sharpe
        assert 0.0 < e.win_rate < 1.0


# --------------------------------------------------------------------- #
# InMemorySignalEdgeStore
# --------------------------------------------------------------------- #

class TestInMemoryStore:
    def setup_method(self):
        self.store = InMemorySignalEdgeStore()
        self.now = time.time()

    def _add(self, name, regime, direction, forward, exchange="coinbase",
             ts_offset=0.0):
        self.store.add_sample(SignalSample(
            signal_name=name, regime=regime, direction=direction,
            score=1.0 if direction == "long" else -1.0,
            forward_return=forward, pair="BTC-USD",
            exchange=exchange, timestamp=self.now + ts_offset,
        ))

    def test_get_edge_filters_by_regime(self):
        self._add("ema", "trending_up", "long", 0.02)
        self._add("ema", "trending_up", "long", 0.01)
        self._add("ema", "chop", "long", -0.05)
        e = self.store.get_edge("ema", "trending_up", "coinbase", now_ts=self.now)
        assert e.n_samples == 2
        assert e.avg_return > 0

    def test_get_edge_respects_lookback(self):
        # Old sample (90 days ago) should be excluded by 30-day lookback.
        self._add("ema", "trending_up", "long", 1.0, ts_offset=-90 * 86400)
        self._add("ema", "trending_up", "long", 0.01)
        e = self.store.get_edge(
            "ema", "trending_up", "coinbase", lookback_days=30, now_ts=self.now
        )
        assert e.n_samples == 1
        assert e.avg_return == pytest.approx(0.01)

    def test_domain_separation(self):
        self._add("ema", "trending_up", "long", 0.05, exchange="coinbase")
        self._add("ema", "trending_up", "long", -0.99, exchange="ibkr")
        e_cb = self.store.get_edge("ema", "trending_up", "coinbase", now_ts=self.now)
        e_ib = self.store.get_edge("ema", "trending_up", "ibkr", now_ts=self.now)
        assert e_cb.n_samples == 1 and e_cb.avg_return == pytest.approx(0.05)
        assert e_ib.n_samples == 1 and e_ib.avg_return == pytest.approx(-0.99)

    def test_all_edges_groups_by_signal(self):
        self._add("ema", "trending_up", "long", 0.02)
        self._add("rsi", "trending_up", "short", -0.01)  # short rewarded
        edges = self.store.all_edges("trending_up", "coinbase", now_ts=self.now)
        names = {e.signal_name for e in edges}
        assert names == {"ema", "rsi"}

    def test_list_signals_per_exchange(self):
        self._add("a", "chop", "long", 0.0, exchange="coinbase")
        self._add("b", "chop", "long", 0.0, exchange="ibkr")
        assert self.store.list_signals("coinbase") == ["a"]
        assert self.store.list_signals("ibkr") == ["b"]


# --------------------------------------------------------------------- #
# SignalEdgeLibrary
# --------------------------------------------------------------------- #

class TestSignalEdgeLibrary:
    def setup_method(self):
        self.store = InMemorySignalEdgeStore()
        self.lib = SignalEdgeLibrary(store=self.store, exchange="coinbase")

    # ---- Registration & compute ----

    def test_register_and_compute(self):
        self.lib.register("always_long", lambda candles: 0.7)
        assert "always_long" in self.lib.registered()
        assert self.lib.compute("always_long", []) == pytest.approx(0.7)

    def test_compute_clips_to_range(self):
        self.lib.register("over", lambda c: 5.0)
        self.lib.register("under", lambda c: -10.0)
        assert self.lib.compute("over", []) == 1.0
        assert self.lib.compute("under", []) == -1.0

    def test_compute_handles_exceptions(self):
        def boom(c):
            raise RuntimeError("fail")
        self.lib.register("boom", boom)
        assert self.lib.compute("boom", []) == 0.0

    def test_compute_handles_non_finite(self):
        self.lib.register("nan", lambda c: float("nan"))
        self.lib.register("inf", lambda c: float("inf"))
        assert self.lib.compute("nan", []) == 0.0
        assert self.lib.compute("inf", []) == 0.0

    def test_compute_unknown_raises(self):
        with pytest.raises(KeyError):
            self.lib.compute("missing", [])

    def test_register_invalid(self):
        with pytest.raises(ValueError):
            self.lib.register("", lambda c: 0.0)
        with pytest.raises(ValueError):
            self.lib.register("ok", "not_callable")  # type: ignore[arg-type]

    # ---- Sample recording ----

    def test_record_sample_infers_direction(self):
        self.lib.register("x", lambda c: 0.5)
        s = self.lib.record_sample(
            signal_name="x", regime="chop", score=0.5,
            forward_return=0.02, pair="BTC-USD",
        )
        assert s.direction == "long"
        s2 = self.lib.record_sample(
            signal_name="x", regime="chop", score=-0.5,
            forward_return=0.02, pair="BTC-USD",
        )
        assert s2.direction == "short"
        s3 = self.lib.record_sample(
            signal_name="x", regime="chop", score=0.0,
            forward_return=0.02, pair="BTC-USD",
        )
        assert s3.direction == "flat"

    def test_record_sample_rejects_non_finite(self):
        with pytest.raises(ValueError):
            self.lib.record_sample(
                signal_name="x", regime="chop", score=float("nan"),
                forward_return=0.0, pair="BTC-USD",
            )

    # ---- Dynamic weighting ----

    def test_weights_equal_when_no_history(self):
        self.lib.register("a", lambda c: 0.0)
        self.lib.register("b", lambda c: 0.0)
        w = self.lib.weights("trending_up")
        assert pytest.approx(w["a"] + w["b"], abs=1e-9) == 1.0
        assert w["a"] == pytest.approx(w["b"])  # equal-weight fallback

    def test_weights_concentrate_on_winner(self):
        self.lib.register("good", lambda c: 1.0)
        self.lib.register("bad", lambda c: -1.0)
        # Inject 50 samples each: good consistently rewarded, bad punished.
        for i in range(50):
            self.lib.record_sample(
                signal_name="good", regime="trending_up",
                score=1.0, forward_return=0.01,
                pair="BTC-USD",
            )
            self.lib.record_sample(
                signal_name="bad", regime="trending_up",
                score=1.0, forward_return=-0.01,  # long but lost
                pair="BTC-USD",
            )
        # Add some variance so sharpe is finite.
        for i in range(50):
            self.lib.record_sample(
                signal_name="good", regime="trending_up",
                score=1.0, forward_return=0.01 + 0.002 * (i % 3 - 1),
                pair="BTC-USD",
            )
            self.lib.record_sample(
                signal_name="bad", regime="trending_up",
                score=1.0, forward_return=-0.01 + 0.002 * (i % 3 - 1),
                pair="BTC-USD",
            )
        w = self.lib.weights("trending_up", min_samples=10)
        assert w["good"] > w["bad"]
        # bad has negative sharpe → clipped to 0 → gets 0 weight
        assert w["bad"] == 0.0
        assert w["good"] == pytest.approx(1.0)

    def test_weights_explore_under_min_samples(self):
        self.lib.register("seasoned", lambda c: 1.0)
        self.lib.register("new", lambda c: 1.0)
        # Seasoned has 100 mediocre samples → sharpe positive but small.
        for i in range(100):
            self.lib.record_sample(
                signal_name="seasoned", regime="chop",
                score=1.0, forward_return=0.001 + 0.01 * (i % 3 - 1),
                pair="BTC-USD",
            )
        # `new` has zero samples → falls back to prior weight, still > 0.
        w = self.lib.weights("chop", min_samples=30)
        assert w["new"] > 0
        assert w["seasoned"] > 0

    def test_combined_score_in_range(self):
        self.lib.register("up", lambda c: 0.8)
        self.lib.register("down", lambda c: -0.6)
        c = self.lib.combined_score([], regime="chop")
        assert -1.0 <= c <= 1.0

    def test_combined_score_uses_weights(self):
        # Force `winner` to dominate weights via training; combined
        # score should track its sign.
        self.lib.register("winner", lambda c: 1.0)
        self.lib.register("loser", lambda c: -1.0)
        for _ in range(100):
            self.lib.record_sample(
                signal_name="winner", regime="trending_up",
                score=1.0, forward_return=0.01,
                pair="BTC-USD",
            )
            self.lib.record_sample(
                signal_name="loser", regime="trending_up",
                score=-1.0, forward_return=0.01,  # short but moved up → loss
                pair="BTC-USD",
            )
        # Add variance so std > 0.
        for i in range(50):
            self.lib.record_sample(
                signal_name="winner", regime="trending_up",
                score=1.0, forward_return=0.01 + 0.003 * ((i % 3) - 1),
                pair="BTC-USD",
            )
            self.lib.record_sample(
                signal_name="loser", regime="trending_up",
                score=-1.0, forward_return=0.01 + 0.003 * ((i % 3) - 1),
                pair="BTC-USD",
            )
        c = self.lib.combined_score([], regime="trending_up", min_samples=10)
        assert c > 0  # winner dominates → bullish bias

    def test_exchange_scoping(self):
        cb_lib = SignalEdgeLibrary(exchange="coinbase")
        ib_lib = SignalEdgeLibrary(store=cb_lib.store, exchange="ibkr")
        cb_lib.register("x", lambda c: 1.0)
        ib_lib.register("x", lambda c: 1.0)
        cb_lib.record_sample(
            signal_name="x", regime="chop",
            score=1.0, forward_return=0.05, pair="BTC-USD",
        )
        # IBKR library reads from same store but sees zero samples.
        e_cb = cb_lib.edge("x", "chop")
        e_ib = ib_lib.edge("x", "chop")
        assert e_cb.n_samples == 1
        assert e_ib.n_samples == 0


# --------------------------------------------------------------------- #
# EdgeStats invariants
# --------------------------------------------------------------------- #

def test_edge_is_actionable_threshold():
    e_low_n = EdgeStats(
        signal_name="x", regime="chop", exchange="coinbase",
        n_samples=10, win_rate=0.6, avg_return=0.01, sharpe=0.5,
    )
    e_actionable = EdgeStats(
        signal_name="x", regime="chop", exchange="coinbase",
        n_samples=50, win_rate=0.55, avg_return=0.005, sharpe=0.8,
    )
    e_negative = EdgeStats(
        signal_name="x", regime="chop", exchange="coinbase",
        n_samples=100, win_rate=0.4, avg_return=-0.001, sharpe=-0.3,
    )
    assert not e_low_n.is_actionable
    assert e_actionable.is_actionable
    assert not e_negative.is_actionable
