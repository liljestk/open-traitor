"""Tests for PortfolioRotator — asset ranking, swap proposals, LLM validation, priority."""
import asyncio
import time
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.portfolio_rotator import (
    AssetRanking,
    SwapProposal,
    PortfolioRotator,
)
from src.core.fee_manager import FeeEstimate


# ── AssetRanking ─────────────────────────────────────────────────────────

class TestAssetRanking:
    def test_repr(self):
        r = AssetRanking("BTC-USD", 0.75, 0.8, 2.5, {"x": 1})
        s = repr(r)
        assert "BTC-USD" in s
        assert "+0.75" in s
        assert "0.80" in s

    def test_attributes(self):
        r = AssetRanking("ETH-EUR", -0.3, 0.5, -1.0, {}, "weak")
        assert r.pair == "ETH-EUR"
        assert r.score == -0.3
        assert r.predicted_move_pct == -1.0
        assert r.reasoning == "weak"


# ── SwapProposal ─────────────────────────────────────────────────────────

def _fee(**overrides):
    """Build a FeeEstimate with sensible defaults."""
    defaults = dict(
        sell_fee_pct=0.006, buy_fee_pct=0.006, total_fee_pct=0.012,
        sell_fee_quote=0.25, buy_fee_quote=0.25, total_fee_quote=0.5,
        breakeven_move_pct=1.2, is_profitable=True,
    )
    defaults.update(overrides)
    return FeeEstimate(**defaults)


class TestSwapProposal:
    def test_attributes(self):
        fee = _fee(total_fee_pct=0.005, total_fee_quote=0.5)
        p = SwapProposal(
            sell_pair="BTC-USD",
            buy_pair="ETH-USD",
            usd_amount=500.0,
            sell_score=-0.2,
            buy_score=0.6,
            expected_gain_pct=3.0,
            fee_estimate=fee,
            net_gain_pct=0.025,
            confidence=0.75,
            priority="autonomous",
            reasoning="test",
        )
        assert p.sell_pair == "BTC-USD"
        assert p.buy_pair == "ETH-USD"
        assert p.quote_amount == 500.0  # mapped from usd_amount
        assert p.approved is None
        assert p.executed is False

    def test_with_route(self):
        fee = _fee(total_fee_pct=0, total_fee_quote=0)
        route = MagicMock()
        p = SwapProposal(
            sell_pair="A-USD", buy_pair="B-USD", usd_amount=10,
            sell_score=0, buy_score=0, expected_gain_pct=1,
            fee_estimate=fee, net_gain_pct=0.01,
            confidence=0.5, priority="autonomous", reasoning="r",
            route=route,
        )
        assert p.route is route


# ── PortfolioRotator helpers ─────────────────────────────────────────────

def _make_rotator(**overrides):
    """Create a PortfolioRotator with mock dependencies."""
    config = {
        "rotation": {
            "enabled": True,
            "autonomous_allocation_pct": 0.10,
            "min_score_delta": 0.3,
            "min_confidence": 0.65,
            "high_impact_confidence": 0.80,
            "approval_threshold": 200.0,
            "full_autonomy": True,
            "llm_validation": False,
        },
    }
    config["rotation"].update(overrides.pop("rotation_cfg", {}))
    fee_manager = MagicMock()
    fee_manager.swap_cooldown_seconds = 0
    fee_manager.is_trade_worthwhile.return_value = (True, _fee(
        total_fee_pct=0.005, total_fee_quote=0.5,
    ))
    fee_manager.get_dynamic_min_trade.return_value = 1.0
    high_stakes = MagicMock()
    high_stakes.is_active = False
    multi_tf = overrides.pop("multi_tf", None)
    fear_greed = overrides.pop("fear_greed", None)
    route_finder = overrides.pop("route_finder", None)
    rules = overrides.pop("rules", None)
    return PortfolioRotator(
        config=config,
        coinbase_client=MagicMock(),
        llm_client=MagicMock(),
        fee_manager=fee_manager,
        high_stakes=high_stakes,
        multi_tf=multi_tf,
        fear_greed=fear_greed,
        route_finder=route_finder,
        rules=rules,
    )


class TestDeterminePriority:
    def test_full_autonomy_always_autonomous(self):
        rotator = _make_rotator()
        assert rotator._determine_priority(0.9, 1000, 0.8) == "autonomous"
        assert rotator._determine_priority(0.3, 10, 0.1) == "autonomous"

    def test_no_autonomy_critical(self):
        rotator = _make_rotator(rotation_cfg={"full_autonomy": False})
        # > 2x approval_threshold (200) = critical
        assert rotator._determine_priority(0.5, 500, 0.5) == "critical"

    def test_no_autonomy_high_impact_amount(self):
        rotator = _make_rotator(rotation_cfg={"full_autonomy": False})
        # > approval_threshold (200) but < 2x
        assert rotator._determine_priority(0.5, 250, 0.3) == "high_impact"

    def test_no_autonomy_high_impact_confidence(self):
        rotator = _make_rotator(rotation_cfg={"full_autonomy": False})
        # High confidence + high delta → high_impact
        assert rotator._determine_priority(0.85, 50, 0.6) == "high_impact"

    def test_no_autonomy_autonomous(self):
        rotator = _make_rotator(rotation_cfg={"full_autonomy": False})
        # Below threshold, sufficient confidence
        assert rotator._determine_priority(0.70, 100, 0.3) == "autonomous"

    def test_no_autonomy_low_confidence(self):
        rotator = _make_rotator(rotation_cfg={"full_autonomy": False})
        # Below min_confidence → ask
        assert rotator._determine_priority(0.3, 50, 0.3) == "high_impact"


# ── Thread-safe pending swaps ────────────────────────────────────────────

class TestPendingSwaps:
    def test_add_and_get(self):
        rotator = _make_rotator()
        fee = _fee(total_fee_pct=0, total_fee_quote=0)
        proposal = SwapProposal(
            "A-USD", "B-USD", 100, 0, 0, 1, fee, 0.01, 0.7, "autonomous", "r"
        )
        rotator.add_pending_swap("s1", proposal)
        pending = rotator.get_pending_swaps()
        assert "s1" in pending

    def test_pop(self):
        rotator = _make_rotator()
        fee = _fee(total_fee_pct=0, total_fee_quote=0)
        proposal = SwapProposal(
            "A-USD", "B-USD", 100, 0, 0, 1, fee, 0.01, 0.7, "autonomous", "r"
        )
        rotator.add_pending_swap("s1", proposal)
        popped = rotator.pop_pending_swap("s1")
        assert popped is proposal
        assert rotator.pop_pending_swap("s1") is None


# ── _rank_assets ─────────────────────────────────────────────────────────

class TestRankAssets:
    def test_no_multi_tf(self):
        rotator = _make_rotator()
        rankings = rotator._rank_assets(["BTC-USD", "ETH-USD"], {"BTC-USD": 50000, "ETH-USD": 3000})
        # Without multi_tf, confluence_score=0, all scores=0
        assert len(rankings) == 2
        assert all(r.score == 0 for r in rankings)

    def test_with_multi_tf(self):
        multi_tf = MagicMock()
        multi_tf.analyze.side_effect = lambda pair: {
            "BTC-USD": {"confluence_score": 0.8, "aligned": True, "summary": "bullish"},
            "ETH-USD": {"confluence_score": -0.3, "aligned": False, "summary": "bearish"},
        }[pair]
        rotator = _make_rotator(multi_tf=multi_tf)
        rankings = rotator._rank_assets(["BTC-USD", "ETH-USD"], {})
        btc = next(r for r in rankings if r.pair == "BTC-USD")
        eth = next(r for r in rankings if r.pair == "ETH-USD")
        assert btc.score > eth.score
        assert btc.predicted_move_pct > 0
        assert eth.predicted_move_pct < 0

    def test_with_fear_greed_extreme_fear(self):
        multi_tf = MagicMock()
        multi_tf.analyze.return_value = {"confluence_score": 0.5, "aligned": True, "summary": ""}
        fear_greed = MagicMock()
        fear_greed.fetch.return_value = {"value": 15}  # extreme fear
        rotator = _make_rotator(multi_tf=multi_tf, fear_greed=fear_greed)
        rankings = rotator._rank_assets(["BTC-USD"], {})
        # Extreme fear + bullish → amplified predicted_move
        assert rankings[0].predicted_move_pct > 0.5 * 3.0  # baseline without boost

    def test_ranking_error_skipped(self):
        multi_tf = MagicMock()
        multi_tf.analyze.side_effect = Exception("API error")
        rotator = _make_rotator(multi_tf=multi_tf)
        rankings = rotator._rank_assets(["BTC-USD"], {})
        assert len(rankings) == 0  # error → skipped


# ── _apply_llm_decisions ─────────────────────────────────────────────────

class TestApplyLlmDecisions:
    def _make_proposal(self, sell, buy):
        fee = _fee(total_fee_pct=0, total_fee_quote=0)
        return SwapProposal(sell, buy, 100, 0, 1, 2.0, fee, 0.02, 0.7, "autonomous", "r")

    def test_all_approved(self):
        rotator = _make_rotator()
        proposals = [self._make_proposal("BTC-USD", "ETH-USD")]
        llm = {"decisions": [
            {"sell_pair": "BTC-USD", "buy_pair": "ETH-USD", "action": "approve", "reasoning": "ok"},
        ]}
        result = rotator._apply_llm_decisions(proposals, llm)
        assert len(result) == 1

    def test_vetoed(self):
        rotator = _make_rotator()
        proposals = [self._make_proposal("BTC-USD", "ETH-USD")]
        llm = {"decisions": [
            {"sell_pair": "BTC-USD", "buy_pair": "ETH-USD", "action": "veto", "reasoning": "bad idea"},
        ]}
        result = rotator._apply_llm_decisions(proposals, llm)
        assert len(result) == 0

    def test_no_decision_keeps_proposal(self):
        rotator = _make_rotator()
        proposals = [self._make_proposal("BTC-USD", "ETH-USD")]
        llm = {"decisions": []}  # no decisions
        result = rotator._apply_llm_decisions(proposals, llm)
        assert len(result) == 1

    def test_mixed_decisions(self):
        rotator = _make_rotator()
        proposals = [
            self._make_proposal("BTC-USD", "ETH-USD"),
            self._make_proposal("SOL-USD", "AVAX-USD"),
        ]
        llm = {"decisions": [
            {"sell_pair": "BTC-USD", "buy_pair": "ETH-USD", "action": "approve", "reasoning": "ok"},
            {"sell_pair": "SOL-USD", "buy_pair": "AVAX-USD", "action": "veto", "reasoning": "risky"},
        ]}
        result = rotator._apply_llm_decisions(proposals, llm)
        assert len(result) == 1
        assert result[0].sell_pair == "BTC-USD"


# ── evaluate_rotation (async) ────────────────────────────────────────────

class TestEvaluateRotation:
    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_disabled(self):
        rotator = _make_rotator(rotation_cfg={"enabled": False})
        result = self._run(rotator.evaluate_rotation([], [], {}, 1000))
        assert result == []

    def test_at_position_limit(self):
        rotator = _make_rotator()
        result = self._run(rotator.evaluate_rotation(
            held_pairs=["BTC-USD", "ETH-USD"],
            all_pairs=["BTC-USD", "ETH-USD", "SOL-USD"],
            current_prices={"BTC-USD": 50000, "ETH-USD": 3000, "SOL-USD": 100},
            portfolio_value=1000,
            max_open_positions=2,
        ))
        assert result == []

    def test_not_enough_assets(self):
        multi_tf = MagicMock()
        multi_tf.analyze.return_value = {"confluence_score": 0.5, "aligned": True, "summary": ""}
        rotator = _make_rotator(multi_tf=multi_tf)
        result = self._run(rotator.evaluate_rotation(
            held_pairs=["BTC-USD"],
            all_pairs=["BTC-USD"],
            current_prices={"BTC-USD": 50000},
            portfolio_value=1000,
        ))
        assert result == []

    def test_proposals_generated(self):
        multi_tf = MagicMock()
        multi_tf.analyze.side_effect = lambda pair: {
            "BTC-USD": {"confluence_score": -0.5, "aligned": True, "summary": "bearish"},
            "ETH-USD": {"confluence_score": 0.6, "aligned": True, "summary": "bullish"},
        }[pair]
        rotator = _make_rotator(multi_tf=multi_tf)
        result = self._run(rotator.evaluate_rotation(
            held_pairs=["BTC-USD"],
            all_pairs=["BTC-USD", "ETH-USD"],
            current_prices={"BTC-USD": 50000, "ETH-USD": 3000},
            portfolio_value=10000,
            open_positions={"BTC-USD": 0.1},
            max_open_positions=5,
        ))
        # BTC score=-0.5, ETH score=0.6, delta=1.1 > min_score_delta=0.3
        assert len(result) >= 1
        assert result[0].sell_pair == "BTC-USD"
        assert result[0].buy_pair == "ETH-USD"

    def test_cooldown_blocks_swap(self):
        multi_tf = MagicMock()
        multi_tf.analyze.side_effect = lambda pair: {
            "BTC-USD": {"confluence_score": -0.5, "aligned": True, "summary": ""},
            "ETH-USD": {"confluence_score": 0.6, "aligned": True, "summary": ""},
        }[pair]
        rotator = _make_rotator(multi_tf=multi_tf)
        rotator.fee_manager.swap_cooldown_seconds = 999999
        rotator._set_last_swap_times("BTC-USD")
        result = self._run(rotator.evaluate_rotation(
            held_pairs=["BTC-USD"],
            all_pairs=["BTC-USD", "ETH-USD"],
            current_prices={"BTC-USD": 50000, "ETH-USD": 3000},
            portfolio_value=10000,
            open_positions={"BTC-USD": 0.1},
            max_open_positions=5,
        ))
        assert result == []

    def test_with_route_finder(self):
        multi_tf = MagicMock()
        multi_tf.analyze.side_effect = lambda pair: {
            "BTC-USD": {"confluence_score": -0.5, "aligned": True, "summary": ""},
            "ETH-USD": {"confluence_score": 0.6, "aligned": True, "summary": ""},
        }[pair]
        route_finder = MagicMock()
        mock_route = MagicMock()
        mock_route.n_legs = 1
        mock_route.route_type = "direct"
        mock_route.bridge_currency = None
        route_finder.find_routes.return_value = [mock_route]
        rotator = _make_rotator(multi_tf=multi_tf, route_finder=route_finder)
        result = self._run(rotator.evaluate_rotation(
            held_pairs=["BTC-USD"],
            all_pairs=["BTC-USD", "ETH-USD"],
            current_prices={"BTC-USD": 50000, "ETH-USD": 3000},
            portfolio_value=10000,
            open_positions={"BTC-USD": 0.1},
            max_open_positions=5,
        ))
        assert len(result) >= 1
        assert result[0].route is mock_route


# ── get_rotation_summary ─────────────────────────────────────────────────

class TestGetRotationSummary:
    def test_no_proposals(self):
        rotator = _make_rotator()
        summary = rotator.get_rotation_summary([])
        assert "No profitable swaps" in summary

    def test_with_proposals(self):
        rotator = _make_rotator()
        fee = _fee(total_fee_pct=0.005, total_fee_quote=0.5)
        proposal = SwapProposal(
            "BTC-USD", "ETH-USD", 500.0, -0.2, 0.6, 3.0, fee, 0.025, 0.75, "autonomous", "test"
        )
        summary = rotator.get_rotation_summary([proposal])
        assert "Rotation Analysis" in summary


# ── _record_rotation_leg ─────────────────────────────────────────────────

class TestRecordRotationLeg:
    def test_records_when_rules_set(self):
        rules = MagicMock()
        rotator = _make_rotator(rules=rules)
        rotator._record_rotation_leg(100.0, "sell", "sell_leg")
        rules.record_trade.assert_called_once_with(100.0, action="sell")

    def test_noop_when_no_rules(self):
        rotator = _make_rotator()
        rotator.rules = None
        # Should not raise
        rotator._record_rotation_leg(100.0, "sell", "sell_leg")

    def test_error_swallowed(self):
        rules = MagicMock()
        rules.record_trade.side_effect = Exception("boom")
        rotator = _make_rotator(rules=rules)
        # Should not raise
        rotator._record_rotation_leg(100.0, "sell", "sell_leg")
