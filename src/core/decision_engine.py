"""
DecisionEngine — deterministic guardrail layer between the (autonomous) LLM
TraderAgent and the order executor.

The LLM remains an autonomous super-trader: it picks pair, action, and
confidence freely. Every proposal it submits, however, is routed through
this engine, which:

  1.  Validates the proposal envelope (pair / action / confidence).
  2.  Vetoes proposals whose originating signal has *no measured edge*
      in the current regime (consults SignalEdgeLibrary).
  3.  Caps the proposal's notional by the strategy's allocator budget
      (consults CapitalAllocator).
  4.  Honours ensemble agreement — solitary contrarian votes can be
      downsized or vetoed.
  5.  Hands the resulting (possibly amended) proposal off to AbsoluteRules
      for the final unbypassable check.

Result is a ``DecisionVerdict`` containing the final action, the
deterministically-bounded ``quote_amount_max``, and a list of human-
readable reasons. The verdict is *advisory to the executor* — i.e. the
existing ``RiskManagerAgent`` consumes it as one of many sizing inputs,
but no path can amplify a vetoed decision back into a buy.

This module has zero hard dependencies — every collaborator (allocator,
edge library, rules) is optional, so unit tests can wire in fakes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from src.utils.logger import get_logger

logger = get_logger("core.decision_engine")


# --------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------- #

_VALID_ACTIONS: frozenset[str] = frozenset({"buy", "sell", "hold"})


@dataclass
class TradeProposal:
    """A trade idea the LLM (or a deterministic fallback) wants to execute."""

    pair: str
    action: str  # "buy" | "sell" | "hold"
    confidence: float
    strategy: str = "llm_strategist"
    regime: str = "unknown"
    ensemble: Optional[dict] = None  # output of pipeline_manager._compute_ensemble
    pattern_signal: Optional[dict] = None
    quote_amount: Optional[float] = None
    quantity: Optional[float] = None
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    current_price: Optional[float] = None
    reasoning: str = ""

    def to_proposal_dict(self) -> dict:
        out = {
            "pair": self.pair,
            "action": self.action,
            "confidence": float(self.confidence),
            "reasoning": self.reasoning,
        }
        if self.quote_amount is not None:
            out["quote_amount"] = float(self.quote_amount)
        if self.quantity is not None:
            out["quantity"] = float(self.quantity)
        if self.stop_loss_price is not None:
            out["stop_loss_price"] = float(self.stop_loss_price)
        if self.take_profit_price is not None:
            out["take_profit_price"] = float(self.take_profit_price)
        if self.current_price is not None:
            out["current_price"] = float(self.current_price)
        return out


@dataclass
class DecisionVerdict:
    """Result of DecisionEngine.evaluate()."""

    approved: bool
    action: str  # final action — vetoes collapse to "hold"
    proposal: dict  # safe-to-execute proposal dict (compatible with risk_manager)
    quote_amount_max: float = 0.0  # deterministic upper bound for sizing
    reasons: list[str] = field(default_factory=list)
    veto: Optional[str] = None  # which gate vetoed (None when approved)
    edge_stats: Optional[dict] = None
    allocator_weight: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "approved": self.approved,
            "action": self.action,
            "proposal": dict(self.proposal),
            "quote_amount_max": float(self.quote_amount_max),
            "reasons": list(self.reasons),
            "veto": self.veto,
            "edge_stats": self.edge_stats,
            "allocator_weight": self.allocator_weight,
        }


# --------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------- #

class DecisionEngine:
    """Deterministic guardrail layer.

    Consumers:
      - ``TraderAgent`` calls ``evaluate(proposal, ctx)`` to obtain a verdict
        before returning anything to the pipeline.
      - The pipeline can also call ``evaluate`` directly when bypassing the
        LLM (deterministic-only mode / backtests).
    """

    def __init__(
        self,
        *,
        rules=None,                    # AbsoluteRules
        edges=None,                    # SignalEdgeLibrary
        allocator=None,                # CapitalAllocator
        exchange: str = "default",
        min_edge_sharpe: float = 0.10,
        min_edge_samples: int = 30,
        min_agreement: float = 0.50,
        min_confidence: float = 0.50,
        max_total_exposure_pct: float = 0.80,
    ) -> None:
        self.rules = rules
        self.edges = edges
        self.allocator = allocator
        self.exchange = exchange
        self.min_edge_sharpe = float(min_edge_sharpe)
        self.min_edge_samples = int(min_edge_samples)
        self.min_agreement = float(min_agreement)
        self.min_confidence = float(min_confidence)
        self.max_total_exposure_pct = float(max_total_exposure_pct)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def evaluate(
        self,
        proposal: TradeProposal,
        *,
        portfolio_value: float = 0.0,
        cash_balance: float = 0.0,
    ) -> DecisionVerdict:
        """Run all gates. Returns a verdict that downstream callers MUST honour."""

        reasons: list[str] = []

        # ---- 1. Envelope validation ---------------------------------- #
        action = (proposal.action or "hold").lower()
        if action not in _VALID_ACTIONS:
            return self._veto(
                proposal, "invalid_action",
                [f"action '{proposal.action}' not in {sorted(_VALID_ACTIONS)}"],
            )
        if action == "hold":
            return DecisionVerdict(
                approved=True, action="hold",
                proposal=proposal.to_proposal_dict(),
                reasons=["hold proposal accepted as-is"],
            )

        if not (0.0 <= float(proposal.confidence) <= 1.0):
            return self._veto(
                proposal, "invalid_confidence",
                [f"confidence {proposal.confidence!r} outside [0,1]"],
            )
        if proposal.action == "buy" and proposal.confidence < self.min_confidence:
            return self._veto(
                proposal, "low_confidence",
                [
                    f"confidence {proposal.confidence:.2f} below "
                    f"engine minimum {self.min_confidence:.2f}"
                ],
            )

        # ---- 2. Edge-library veto (buy side only) ------------------- #
        edge_dict: Optional[dict] = None
        if self.edges is not None and action == "buy":
            try:
                e = self.edges.edge(proposal.strategy, proposal.regime)
                edge_dict = e.to_dict() if hasattr(e, "to_dict") else None
                # Only veto when we have *enough* samples to make the call.
                # Too few samples → exploration prior wins; we let the trade
                # through but record the reason for observability.
                if (
                    e.n_samples >= self.min_edge_samples
                    and e.sharpe < self.min_edge_sharpe
                ):
                    return self._veto(
                        proposal, "no_edge",
                        [
                            f"strategy '{proposal.strategy}' shows no edge in "
                            f"regime '{proposal.regime}' "
                            f"(sharpe={e.sharpe:.2f} < {self.min_edge_sharpe:.2f}, "
                            f"n={e.n_samples})"
                        ],
                        edge_stats=edge_dict,
                    )
                if e.n_samples < self.min_edge_samples:
                    reasons.append(
                        f"edge prior (n={e.n_samples} < {self.min_edge_samples}) — exploration allowed"
                    )
                else:
                    reasons.append(
                        f"edge ok: sharpe={e.sharpe:.2f}, n={e.n_samples}, "
                        f"win_rate={e.win_rate:.2f}"
                    )
            except Exception as exc:
                # Edge lookup must never crash the path — log and continue.
                logger.debug("edge_lookup_failed strategy=%s err=%s", proposal.strategy, exc)
                reasons.append("edge lookup unavailable — exploration allowed")

        # ---- 3. Ensemble agreement ----------------------------------- #
        ensemble = proposal.ensemble or {}
        ens_action = (ensemble.get("action") or "").lower()
        ens_agreement = float(ensemble.get("agreement", 0.0) or 0.0)
        ens_n = int(ensemble.get("n_strategies", 0) or 0)
        pattern_dir = (
            (proposal.pattern_signal or {}).get("direction", "neutral")
            if isinstance(proposal.pattern_signal, dict) else "neutral"
        )
        pattern_confirms = (
            (action == "buy" and pattern_dir == "bullish")
            or (action == "sell" and pattern_dir == "bearish")
        )
        if (
            action == "buy"
            and ens_n >= 2
            and ens_action and ens_action != action
            and not pattern_confirms
        ):
            # Solitary contrarian buy with no pattern backup — too risky.
            return self._veto(
                proposal, "ensemble_disagreement",
                [
                    f"ensemble says '{ens_action}' (agreement={ens_agreement:.2f}, "
                    f"n={ens_n}), pattern='{pattern_dir}' — buy not confirmed"
                ],
            )
        if action == "buy" and ens_action == action:
            reasons.append(
                f"ensemble confirms buy (agreement={ens_agreement:.2f}, n={ens_n})"
            )
        if pattern_confirms:
            reasons.append(f"pattern engine confirms ({pattern_dir})")

        # ---- 4. Allocator budget ------------------------------------- #
        allocator_w: Optional[float] = None
        budget_cap = portfolio_value * self.max_total_exposure_pct
        if self.allocator is not None:
            try:
                weights = self.allocator.weights() or {}
                if proposal.strategy in weights:
                    allocator_w = float(weights[proposal.strategy])
                    budget_cap = portfolio_value * allocator_w * 1.0
                    reasons.append(
                        f"allocator weight for '{proposal.strategy}' = "
                        f"{allocator_w:.3f} → budget cap "
                        f"{budget_cap:,.2f}"
                    )
            except Exception as exc:
                logger.debug("allocator_lookup_failed err=%s", exc)

        if action == "buy" and budget_cap <= 0 and portfolio_value > 0:
            return self._veto(
                proposal, "no_allocator_budget",
                [
                    "allocator assigns zero (or negative) weight to "
                    f"'{proposal.strategy}'"
                ],
                allocator_weight=allocator_w,
            )

        # ---- 5. AbsoluteRules pass-through --------------------------- #
        # We don't *replace* the risk manager's call here — it does the
        # full-fat check including stop-loss enforcement and daily counters.
        # We do an *advisory* pre-flight so that obvious violations veto
        # the LLM's proposal before it ever touches the executor's queue.
        if self.rules is not None and action in {"buy", "sell"}:
            try:
                from src.models.trade import TradeAction  # local import to avoid cycle

                trade_action = TradeAction.BUY if action == "buy" else TradeAction.SELL
                quote_value = float(proposal.quote_amount or 0.0)
                if quote_value <= 0 and budget_cap > 0:
                    quote_value = budget_cap  # use the cap for the dry-run check
                if quote_value > 0:
                    is_allowed, violations, _needs_appr = self.rules.check_trade(
                        pair=proposal.pair,
                        action=trade_action,
                        quote_value=quote_value,
                        portfolio_value=portfolio_value,
                        cash_balance=cash_balance,
                        has_stop_loss=proposal.stop_loss_price is not None,
                    )
                    if not is_allowed:
                        return self._veto(
                            proposal, "absolute_rules",
                            [str(v) for v in violations] or ["AbsoluteRules veto"],
                            allocator_weight=allocator_w,
                            edge_stats=edge_dict,
                        )
            except Exception as exc:
                logger.debug("rules_preflight_failed err=%s", exc)

        # ---- 6. Approve ---------------------------------------------- #
        out = proposal.to_proposal_dict()
        # Surface deterministic cap so risk_manager can see it as a ceiling.
        if budget_cap > 0:
            out["allocator_budget_cap"] = float(budget_cap)
        if allocator_w is not None:
            out["allocator_weight"] = float(allocator_w)
        return DecisionVerdict(
            approved=True,
            action=action,
            proposal=out,
            quote_amount_max=float(budget_cap),
            reasons=reasons or ["all gates passed"],
            edge_stats=edge_dict,
            allocator_weight=allocator_w,
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _veto(
        self,
        proposal: TradeProposal,
        veto: str,
        reasons: list[str],
        *,
        edge_stats: Optional[dict] = None,
        allocator_weight: Optional[float] = None,
    ) -> DecisionVerdict:
        held = proposal.to_proposal_dict()
        held["action"] = "hold"
        held["reasoning"] = (held.get("reasoning") or "") + (
            f" | DecisionEngine veto[{veto}]: " + "; ".join(reasons)
        )
        return DecisionVerdict(
            approved=False,
            action="hold",
            proposal=held,
            quote_amount_max=0.0,
            reasons=reasons,
            veto=veto,
            edge_stats=edge_stats,
            allocator_weight=allocator_weight,
        )


__all__ = ["DecisionEngine", "TradeProposal", "DecisionVerdict"]
