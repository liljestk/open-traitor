"""
Decision-engine backtest harness.

Replays historical candles through the deterministic decision path
(``DecisionEngine`` + a stubbed deterministic trader) so that the new
autonomous architecture can be validated *without* a live LLM transport.

This is intentionally minimalist: it does not aim to replace the
existing ``BacktestEngine`` (which simulates fee / slippage / fills).
Its purpose is to confirm:

  1.  The DecisionEngine veto/approval logic behaves consistently.
  2.  Edge samples accumulated via ``SignalEdgeLibrary`` actually flip
      the gating decision once enough samples cross the threshold.
  3.  Allocator caps shrink position size as expected.

Used by ``tests/test_decision_engine_backtest.py`` and as a sanity tool
for ops (``python -m src.backtesting.decision_engine_backtest``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from src.analysis.signal_edge_library import (
    InMemorySignalEdgeStore,
    SignalEdgeLibrary,
)
from src.core.capital_allocator import CapitalAllocator
from src.core.decision_engine import DecisionEngine, TradeProposal


# --------------------------------------------------------------------- #
# Result container
# --------------------------------------------------------------------- #

@dataclass
class DecisionBacktestResult:
    n_bars: int = 0
    n_proposals: int = 0
    n_approved: int = 0
    n_vetoed: int = 0
    veto_breakdown: dict[str, int] = field(default_factory=dict)
    cumulative_return: float = 0.0
    final_edge_sharpe: float = 0.0
    final_edge_n: int = 0
    sized_caps: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "n_bars": self.n_bars,
            "n_proposals": self.n_proposals,
            "n_approved": self.n_approved,
            "n_vetoed": self.n_vetoed,
            "veto_breakdown": dict(self.veto_breakdown),
            "cumulative_return": round(self.cumulative_return, 6),
            "final_edge_sharpe": round(self.final_edge_sharpe, 4),
            "final_edge_n": self.final_edge_n,
            "avg_sized_cap": (
                round(sum(self.sized_caps) / len(self.sized_caps), 4)
                if self.sized_caps else 0.0
            ),
        }


# --------------------------------------------------------------------- #
# Default deterministic responder — stand-in for the LLM
# --------------------------------------------------------------------- #

def _default_responder(closes: list[float], regime: str) -> Optional[dict]:
    """Tiny rule-based "trader": buy on positive 5-bar return, else hold."""
    if len(closes) < 6:
        return None
    r5 = (closes[-1] - closes[-6]) / max(closes[-6], 1e-9)
    if r5 > 0.005:
        return {
            "action": "buy",
            "confidence": min(1.0, 0.55 + r5 * 5),
            "reasoning": f"deterministic 5-bar return = {r5:.4f}",
        }
    if r5 < -0.005:
        return {
            "action": "sell",
            "confidence": min(1.0, 0.55 + (-r5) * 5),
            "reasoning": f"deterministic 5-bar return = {r5:.4f}",
        }
    return None


# --------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------- #

def run_decision_backtest(
    candles: list[dict],
    *,
    pair: str = "BTC-USD",
    exchange: str = "test",
    strategy: str = "deterministic",
    regime: str = "trending",
    portfolio_value: float = 10_000.0,
    cash_balance: float = 10_000.0,
    forward_return_horizon: int = 5,
    decision_engine: Optional[DecisionEngine] = None,
    edges: Optional[SignalEdgeLibrary] = None,
    allocator: Optional[CapitalAllocator] = None,
    responder=_default_responder,
) -> DecisionBacktestResult:
    """Replay candles through the deterministic decision path.

    For every bar (after a 10-bar warmup):
      * Ask the responder for a hypothetical proposal.
      * Run it through the DecisionEngine.
      * If approved, record the realized forward_return into the edge store.
      * Update the allocator with the realized PnL once a trade closes.
    """
    result = DecisionBacktestResult(n_bars=len(candles))
    if not candles:
        return result

    if edges is None:
        edges = SignalEdgeLibrary(store=InMemorySignalEdgeStore(), exchange=exchange)
    if allocator is None:
        allocator = CapitalAllocator()
        allocator.register([strategy])
    if decision_engine is None:
        decision_engine = DecisionEngine(
            edges=edges,
            allocator=allocator,
            exchange=exchange,
            min_edge_sharpe=0.10,
            min_edge_samples=30,
        )

    closes = [float(c.get("close", c.get("c", 0)) or 0) for c in candles]

    for i in range(10, len(closes) - forward_return_horizon):
        proposal_dict = responder(closes[: i + 1], regime)
        if not proposal_dict:
            continue
        result.n_proposals += 1
        action = proposal_dict.get("action", "hold")
        proposal = TradeProposal(
            pair=pair,
            action=action,
            confidence=float(proposal_dict.get("confidence", 0.5) or 0.5),
            strategy=strategy,
            regime=regime,
            current_price=closes[i],
            reasoning=str(proposal_dict.get("reasoning", "")),
        )
        verdict = decision_engine.evaluate(
            proposal,
            portfolio_value=portfolio_value,
            cash_balance=cash_balance,
        )
        if verdict.approved and verdict.action != "hold":
            result.n_approved += 1
            if verdict.quote_amount_max:
                result.sized_caps.append(verdict.quote_amount_max / max(portfolio_value, 1.0))
            # Realized forward return.
            fwd = (
                closes[i + forward_return_horizon] - closes[i]
            ) / max(closes[i], 1e-9)
            score = 1.0 if action == "buy" else -1.0
            edges.record_sample(
                signal_name=strategy,
                regime=regime,
                score=score,
                forward_return=fwd,
                pair=pair,
            )
            # Allocator update with the period PnL.
            try:
                allocator.update({strategy: fwd if action == "buy" else -fwd})
            except Exception:
                pass
            result.cumulative_return += fwd if action == "buy" else -fwd
        else:
            result.n_vetoed += 1
            v = verdict.veto or "unknown"
            result.veto_breakdown[v] = result.veto_breakdown.get(v, 0) + 1

    final_edge = edges.edge(strategy, regime)
    result.final_edge_sharpe = float(final_edge.sharpe)
    result.final_edge_n = int(final_edge.n_samples)
    return result


__all__ = ["run_decision_backtest", "DecisionBacktestResult"]
