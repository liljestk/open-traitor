"""Tests for Phase-3 microstructure engines."""

from __future__ import annotations

import pytest

from src.core.microstructure import (
    FundingBasisMonitor,
    FundingSnapshot,
    OrderBookImbalance,
    OrderBookSnapshot,
    TriangleQuote,
    TriangularArbDetector,
)


# --------------------------------------------------------------------- #
# OrderBookImbalance
# --------------------------------------------------------------------- #

class TestOrderBookImbalance:
    def setup_method(self):
        self.engine = OrderBookImbalance(
            depth_levels=5, min_score_emit=0.15, max_spread_bps=50.0,
        )

    def _book(self, bids, asks):
        return OrderBookSnapshot(
            pair="BTC-USD",
            bids=bids, asks=asks,
            timestamp=1.0, exchange="coinbase",
        )

    def test_empty_book(self):
        assert self.engine.evaluate(self._book([], [])) is None

    def test_crossed_book_rejected(self):
        snap = self._book([(101, 1)], [(100, 1)])  # bid > ask
        assert self.engine.evaluate(snap) is None

    def test_wide_spread_rejected(self):
        # Spread = 100 bps, max_spread_bps=50 → reject.
        snap = self._book([(99, 10)], [(100, 10)])
        assert self.engine.evaluate(snap) is None

    def test_balanced_book_below_floor(self):
        bids = [(99.99, 10), (99.98, 10), (99.97, 10), (99.96, 10), (99.95, 10)]
        asks = [(100.01, 10), (100.02, 10), (100.03, 10), (100.04, 10), (100.05, 10)]
        assert self.engine.evaluate(self._book(bids, asks)) is None

    def test_bid_dominant_emits_long(self):
        bids = [(99.99, 100), (99.98, 100), (99.97, 100), (99.96, 100), (99.95, 100)]
        asks = [(100.01, 5), (100.02, 5), (100.03, 5), (100.04, 5), (100.05, 5)]
        sig = self.engine.evaluate(self._book(bids, asks))
        assert sig is not None
        assert sig.score > 0
        assert sig.weighted_obi > 0
        assert sig.mid == pytest.approx(100.0, abs=0.01)

    def test_ask_dominant_emits_short(self):
        bids = [(99.99, 5)] * 5
        asks = [(100.01, 100)] * 5
        sig = self.engine.evaluate(self._book(bids, asks))
        assert sig is not None and sig.score < 0

    def test_depth_weighting_favours_top(self):
        # Top of book balanced; deep levels skewed → weighted OBI close to 0.
        bids = [(99.99, 10)] + [(99.98, 100)] * 4
        asks = [(100.01, 10)] + [(100.02, 100)] * 4
        sig = self.engine.evaluate(self._book(bids, asks))
        # Should not emit (near-zero weighted OBI).
        assert sig is None or abs(sig.score) < 0.5


# --------------------------------------------------------------------- #
# TriangularArbDetector
# --------------------------------------------------------------------- #

class TestTriangularArb:
    def setup_method(self):
        self.det = TriangularArbDetector(
            fee_bps_per_leg=2.0, slippage_bps_per_leg=1.0, min_edge_bps=5.0,
        )

    def test_no_arb_with_consistent_prices(self):
        # B/A = 2, C/B = 3 → C/A = 6 (consistent, no edge).
        ab = TriangleQuote("A-B", bid=2.0, ask=2.0)
        bc = TriangleQuote("B-C", bid=3.0, ask=3.0)
        ac = TriangleQuote("A-C", bid=6.0, ask=6.0)
        assert self.det.evaluate(ab, bc, ac) is None

    def test_forward_arb_detected(self):
        # Make A/C wildly mispriced: ask too low → going forward profits.
        ab = TriangleQuote("A-B", bid=2.0, ask=2.0)
        bc = TriangleQuote("B-C", bid=3.0, ask=3.0)
        # Forward path produces ~6 C, then divides by ask(A/C) to get A.
        # Lower ac.ask → more A back → bigger profit.
        ac = TriangleQuote("A-C", bid=5.5, ask=5.5)
        opp = self.det.evaluate(ab, bc, ac)
        assert opp is not None and opp.direction == "forward" and opp.edge_bps > 5

    def test_reverse_arb_detected(self):
        # Reverse path: A→C (sell A @ ac.bid=high) → C→B (buy B @ bc.ask=low) → B→A (buy A @ ab.ask=low)
        ab = TriangleQuote("A-B", bid=2.0, ask=2.0)
        bc = TriangleQuote("B-C", bid=3.0, ask=3.0)
        ac = TriangleQuote("A-C", bid=6.5, ask=6.5)  # A/C overpriced
        opp = self.det.evaluate(ab, bc, ac)
        assert opp is not None and opp.direction == "reverse" and opp.edge_bps > 5

    def test_bad_quote_rejected(self):
        ab = TriangleQuote("A-B", bid=0, ask=2.0)
        bc = TriangleQuote("B-C", bid=3.0, ask=3.0)
        ac = TriangleQuote("A-C", bid=6.0, ask=6.0)
        assert self.det.evaluate(ab, bc, ac) is None

    def test_small_edge_below_threshold(self):
        # Tiny mispricing < min_edge_bps → no opportunity.
        ab = TriangleQuote("A-B", bid=2.0, ask=2.0)
        bc = TriangleQuote("B-C", bid=3.0, ask=3.0)
        ac = TriangleQuote("A-C", bid=5.999, ask=6.001)  # ~1.7 bps edge
        assert self.det.evaluate(ab, bc, ac) is None


# --------------------------------------------------------------------- #
# FundingBasisMonitor
# --------------------------------------------------------------------- #

class TestFundingBasisMonitor:
    def setup_method(self):
        self.eng = FundingBasisMonitor(
            basis_bps_full=50.0, funding_full=0.001,
            basis_weight=0.5, min_score_emit=0.15,
        )

    def test_invalid_weight(self):
        with pytest.raises(ValueError):
            FundingBasisMonitor(basis_weight=0.0)
        with pytest.raises(ValueError):
            FundingBasisMonitor(basis_weight=1.0)

    def test_zero_prices(self):
        snap = FundingSnapshot("X", spot_price=0, perp_price=100, funding_rate=0.0)
        assert self.eng.evaluate(snap) is None

    def test_long_crowded_emits_short(self):
        # Perp 1% above spot, funding +30bps → both sides push longs paying.
        snap = FundingSnapshot(
            "BTC-PERP", spot_price=100.0, perp_price=101.0,
            funding_rate=0.003,
        )
        sig = self.eng.evaluate(snap)
        assert sig is not None
        assert sig.score < 0
        assert sig.crowded_side == "long"

    def test_short_crowded_emits_long(self):
        snap = FundingSnapshot(
            "BTC-PERP", spot_price=100.0, perp_price=99.0,
            funding_rate=-0.003,
        )
        sig = self.eng.evaluate(snap)
        assert sig is not None and sig.score > 0 and sig.crowded_side == "short"

    def test_neutral_below_floor(self):
        snap = FundingSnapshot(
            "BTC-PERP", spot_price=100.0, perp_price=100.05,
            funding_rate=0.00005,
        )
        # Tiny basis & tiny funding → score below min_score_emit.
        assert self.eng.evaluate(snap) is None

    def test_score_saturates(self):
        snap = FundingSnapshot(
            "BTC-PERP", spot_price=100.0, perp_price=200.0,  # 10000 bps basis
            funding_rate=0.05,
        )
        sig = self.eng.evaluate(snap)
        assert sig is not None and sig.score == pytest.approx(-1.0)
