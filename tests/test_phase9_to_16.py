"""Phase 9-16 tests: substrate wiring, vol-target, news reflex, drills,
WFO scheduler, cross-asset regime, macro_regime route."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.core.quant_substrate import QuantSubstrate
from src.core.vol_target import (
    realised_vol, vol_target_multiplier, vol_targeted_notional,
)
from src.core.drills import SlippageSimulator, OutageDrill
from src.news.reflex import NewsReflex, score_articles
from src.analysis.cross_asset_regime import macro_view


# --------------------------------------------------------------------- #
# Phase 9 — QuantSubstrate
# --------------------------------------------------------------------- #

class TestQuantSubstrate:
    def test_creates_per_profile_artefacts(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        s = QuantSubstrate(profile="coinbase")
        assert s.allocator is not None
        assert s.healing is not None
        assert s.edges is not None
        # paths are profile-scoped
        assert "coinbase" in str(s.allocator.state_path)
        assert "coinbase" in str(s.healing.audit_path)

    def test_record_pnl_and_update_allocator(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        s = QuantSubstrate(profile="ibkr")
        s.register_strategies(["ema", "boll"])
        s.record_strategy_pnl("ema", 0.005)
        s.record_strategy_pnl("ema", -0.002)
        weights = s.update_allocator({"ema": 0.005, "boll": -0.001})
        assert "ema" in weights and "boll" in weights
        assert abs(sum(weights.values()) - 1.0) < 1e-6
        # File written
        path = Path("data/ibkr/allocator_state.json")
        assert path.exists()

    def test_signal_sample(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        s = QuantSubstrate(profile="coinbase")
        s.record_signal_sample(
            signal_name="sig", regime="ranging", score=0.4,
            forward_return=0.01, pair="BTC-USD",
        )
        edges = s.edges.all_edges("ranging", lookback_days=30)
        assert isinstance(edges, list)

    def test_isolation_between_profiles(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        a = QuantSubstrate(profile="coinbase")
        b = QuantSubstrate(profile="ibkr")
        assert a.allocator.state_path != b.allocator.state_path

    def test_snapshot(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        s = QuantSubstrate(profile="coinbase")
        s.register_strategies(["x"])
        snap = s.snapshot()
        assert snap["profile"] == "coinbase"
        assert "weights" in snap


# --------------------------------------------------------------------- #
# Phase 13 — vol target
# --------------------------------------------------------------------- #

class TestVolTarget:
    def test_realised_vol_insufficient(self):
        assert realised_vol([0.01, 0.02]) is None

    def test_realised_vol_basic(self):
        v = realised_vol([0.01, -0.01, 0.02, -0.02, 0.0])
        assert v is not None and v > 0

    def test_vol_target_multiplier_neutral_when_empty(self):
        assert vol_target_multiplier([]) == 1.0

    def test_vol_target_multiplier_bounded(self):
        # Tiny realised vol → multiplier capped at ceiling
        m = vol_target_multiplier([0.0001] * 30, target_vol=1.0)
        assert m <= 3.0
        # Huge realised vol → floored
        m2 = vol_target_multiplier([0.5, -0.5] * 20, target_vol=0.001)
        assert m2 >= 0.10

    def test_vol_targeted_notional_min_with_kelly(self):
        sized = vol_targeted_notional(
            base_notional=1000.0,
            returns=[0.01, -0.01, 0.005, -0.003, 0.002],
            target_vol=0.01,
            kelly_notional=200.0,
        )
        assert sized <= 200.0


# --------------------------------------------------------------------- #
# Phase 14 — News reflex
# --------------------------------------------------------------------- #

class TestNewsReflex:
    def test_score_empty(self):
        out = score_articles([])
        assert out["bias"] == 1.0 and out["sample_size"] == 0

    def test_score_bullish(self):
        arts = [
            {"sentiment": "bullish", "relevance": 0.9},
            {"sentiment": "bullish", "relevance": 0.8},
            {"sentiment": "neutral", "relevance": 0.5},
        ]
        out = score_articles(arts)
        assert out["bias"] > 1.0
        assert out["high_impact_count"] == 2
        assert out["sentiment_mean"] > 0

    def test_score_bearish_bounded(self):
        arts = [{"sentiment": "bearish", "relevance": 1.0}] * 50
        out = score_articles(arts)
        # Even saturated bearish must stay above the floor
        assert 0.5 <= out["bias"] < 1.0

    def test_evaluate_and_persist(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Stage filesystem fallback file
        fs = tmp_path / "data" / "news" / "coinbase_latest.json"
        fs.parent.mkdir(parents=True)
        fs.write_text(json.dumps([
            {"sentiment": "bullish", "relevance": 0.9},
            {"sentiment": "neutral", "relevance": 0.4},
        ]))
        reflex = NewsReflex(profile="coinbase")
        rec = reflex.evaluate_and_persist()
        assert rec["sample_size"] == 2
        # Persisted
        out = tmp_path / "data" / "coinbase" / "news_bias.json"
        assert out.exists()
        # Round-trip via current_bias
        assert abs(reflex.current_bias() - rec["bias"]) < 1e-9


# --------------------------------------------------------------------- #
# Phase 15 — drills
# --------------------------------------------------------------------- #

class TestSlippageSimulator:
    def test_scenarios_increasing(self):
        sim = SlippageSimulator(seed=7)
        light = sim.simulate(n=200, scenario="light").mean_bps
        normal = sim.simulate(n=200, scenario="normal").mean_bps
        severe = sim.simulate(n=200, scenario="severe").mean_bps
        assert light < normal < severe

    def test_expected_fill_price_buy_above_mid(self):
        sim = SlippageSimulator(seed=1)
        fill = sim.expected_fill_price(100.0, "BUY", "normal")
        assert fill >= 100.0

    def test_expected_fill_price_sell_below_mid(self):
        sim = SlippageSimulator(seed=1)
        fill = sim.expected_fill_price(100.0, "SELL", "normal")
        assert fill <= 100.0

    def test_unknown_scenario_raises(self):
        with pytest.raises(ValueError):
            SlippageSimulator().simulate(scenario="apocalyptic")


class _FakeRules:
    kill_switch_engaged = False

    def engage_kill_switch(self, reason: str):
        self.kill_switch_engaged = True
        self._reason = reason


class TestOutageDrill:
    def test_disconnect_engages_kill_switch(self):
        rules = _FakeRules()
        result = OutageDrill(rules).simulate_exchange_disconnect()
        assert result.halted is True
        assert rules.kill_switch_engaged is True

    def test_feed_stale_threshold(self):
        rules = _FakeRules()
        d = OutageDrill(rules)
        assert d.simulate_feed_stale(last_tick_age_s=600).halted is True
        assert d.simulate_feed_stale(last_tick_age_s=10).halted is False


# --------------------------------------------------------------------- #
# Phase 12 — Cross-asset regime
# --------------------------------------------------------------------- #

class TestCrossAssetRegime:
    def test_macro_view_unknown_when_no_data(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        v = macro_view()
        assert v["consensus"]["regime"] == "UNKNOWN"

    def test_macro_view_risk_on(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        for p in ("coinbase", "ibkr"):
            d = tmp_path / "data" / p
            d.mkdir(parents=True)
            (d / "regime_snapshot.json").write_text(json.dumps({
                "regime": "TRENDING_UP", "confidence": 0.7,
            }))
        v = macro_view()
        assert v["consensus"]["regime"] == "RISK_ON"

    def test_macro_view_risk_off(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        for p in ("coinbase", "ibkr"):
            d = tmp_path / "data" / p
            d.mkdir(parents=True)
            (d / "regime_snapshot.json").write_text(json.dumps({
                "regime": "HIGH_VOL", "confidence": 0.8,
            }))
        v = macro_view()
        assert v["consensus"]["regime"] == "RISK_OFF"

    def test_macro_view_mixed(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data" / "coinbase").mkdir(parents=True)
        (tmp_path / "data" / "ibkr").mkdir(parents=True)
        (tmp_path / "data" / "coinbase" / "regime_snapshot.json").write_text(
            json.dumps({"regime": "TRENDING_UP", "confidence": 0.7})
        )
        (tmp_path / "data" / "ibkr" / "regime_snapshot.json").write_text(
            json.dumps({"regime": "HIGH_VOL", "confidence": 0.8})
        )
        v = macro_view()
        assert v["consensus"]["regime"] == "MIXED"


# --------------------------------------------------------------------- #
# Phase 10 — WFO scheduler
# --------------------------------------------------------------------- #

class TestWFOScheduler:
    def test_run_wfo_writes_promotions(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Stage a tiny PnL log so the incumbent has data
        path = tmp_path / "data" / "coinbase" / "audit"
        path.mkdir(parents=True)
        log = path / "capital_allocator.jsonl"
        for _ in range(20):
            log.write_text(log.read_text() if log.exists() else "" + "")
        # Run the activity directly (no Temporal worker needed)
        import asyncio
        from src.planning.wfo_workflow import run_wfo_for_strategies
        result = asyncio.new_event_loop().run_until_complete(
            run_wfo_for_strategies(profile="coinbase")
        )
        assert result["evaluated"] >= 1
        promotions = (path / "wfo_promotions.jsonl")
        assert promotions.exists()
        rows = [json.loads(l) for l in promotions.read_text().splitlines() if l.strip()]
        assert all("strategy" in r and "promoted" in r for r in rows)


# --------------------------------------------------------------------- #
# Phase 12 — macro_regime route registers
# --------------------------------------------------------------------- #

class TestMacroRegimeRoute:
    def test_route_registered(self):
        from src.dashboard.routes.quant_observability import router
        paths = {r.path for r in router.routes}
        assert "/api/quant/macro_regime" in paths
