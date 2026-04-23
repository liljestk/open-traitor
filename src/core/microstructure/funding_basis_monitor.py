"""
Funding / basis monitor.

For perpetual futures (or cash-and-carry on dated futures), the
basis (perp_price − spot_price) and the funding rate together encode
crowding sentiment:

  • Persistently positive basis + positive funding → longs paying
    shorts heavily; market "long-crowded" → mean-reversion / fade.
  • Persistently negative basis + negative funding → shorts paying
    longs; market "short-crowded" → squeeze / long bias.

Inputs are :class:`FundingSnapshot` (single observation; the engine
is stateless per call). Score in [-1, 1]: +ve means lean long
(short-crowded), -ve means lean short (long-crowded).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class FundingSnapshot:
    pair: str               # e.g. "BTC-USD-PERP" or "BTC-USD"
    spot_price: float
    perp_price: float
    funding_rate: float     # period funding (e.g. 8h funding) as decimal (0.0001 == 1bp)
    exchange: str = "coinbase"
    timestamp: float = 0.0


@dataclass(frozen=True)
class FundingSignal:
    pair: str
    score: float            # [-1, 1]
    basis_bps: float
    funding_rate: float
    crowded_side: str       # "long" or "short"
    exchange: str
    timestamp: float


class FundingBasisMonitor:
    """Pure-logic crowding detector."""

    def __init__(
        self,
        *,
        basis_bps_full: float = 50.0,        # 50 bps basis → score saturates
        funding_full: float = 0.001,         # 10 bps funding per period → saturates
        basis_weight: float = 0.5,
        min_score_emit: float = 0.20,
    ) -> None:
        if not (0.0 < basis_weight < 1.0):
            raise ValueError("basis_weight must be in (0, 1)")
        self.basis_bps_full = float(basis_bps_full)
        self.funding_full = float(funding_full)
        self.basis_weight = float(basis_weight)
        self.funding_weight = 1.0 - self.basis_weight
        self.min_score_emit = float(min_score_emit)

    # ------------------------------------------------------------------ #

    def evaluate(self, snap: FundingSnapshot) -> Optional[FundingSignal]:
        if snap.spot_price <= 0 or snap.perp_price <= 0:
            return None
        basis_bps = (snap.perp_price - snap.spot_price) / snap.spot_price * 10_000.0

        # Normalise components into [-1, 1] (saturate at *_full).
        basis_norm = max(-1.0, min(1.0, basis_bps / max(1e-9, self.basis_bps_full)))
        funding_norm = max(-1.0, min(1.0, snap.funding_rate / max(1e-9, self.funding_full)))

        # Crowded long → positive basis & funding → fade → negative score.
        crowding = self.basis_weight * basis_norm + self.funding_weight * funding_norm
        score = -crowding  # invert: long-crowded → short bias

        if abs(score) < self.min_score_emit:
            return None

        crowded_side = "long" if crowding > 0 else "short"
        return FundingSignal(
            pair=snap.pair,
            score=float(score),
            basis_bps=round(basis_bps, 4),
            funding_rate=snap.funding_rate,
            crowded_side=crowded_side,
            exchange=snap.exchange,
            timestamp=snap.timestamp,
        )


__all__ = ["FundingBasisMonitor", "FundingSnapshot", "FundingSignal"]
