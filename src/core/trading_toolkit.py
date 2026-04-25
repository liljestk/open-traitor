"""
TradingToolkit — structured façade the autonomous LLM TraderAgent uses
to query deterministic state and submit proposals.

The toolkit is *the only* surface through which the LLM can affect the
trading system. Every method returns a JSON-safe dict (so it can be
embedded in a prompt, sent through a tool-call API, or logged). The
``propose_trade`` method is the only side-effecting method; it routes
through ``DecisionEngine.evaluate`` so the LLM cannot bypass guardrails.

This file is deliberately stateless — it holds references, not state —
so a TradingToolkit can be reconstructed every pipeline cycle without
allocating new persistent state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from src.core.decision_engine import DecisionEngine, DecisionVerdict, TradeProposal
from src.utils.logger import get_logger

logger = get_logger("core.trading_toolkit")


@dataclass
class ToolkitContext:
    """Per-cycle context the toolkit reads from. Cheap to construct."""
    pair: str
    exchange: str
    regime: str = "unknown"
    current_price: float = 0.0
    portfolio_value: float = 0.0
    cash_balance: float = 0.0
    open_positions: dict = None  # {pair: qty}
    market_signal: dict = None   # MarketAnalystAgent output
    strategy_signals: dict = None  # ema/bollinger/etc + _ensemble
    pattern_signal: dict = None  # PatternAgent output
    sentiment: dict = None
    news_headlines: str = ""
    fee_context: dict = None
    kelly_stats: dict = None
    recent_outcomes: str = ""
    strategic_context: str = ""

    def __post_init__(self) -> None:
        if self.open_positions is None:
            self.open_positions = {}
        if self.market_signal is None:
            self.market_signal = {}
        if self.strategy_signals is None:
            self.strategy_signals = {}
        if self.pattern_signal is None:
            self.pattern_signal = {"available": False}
        if self.sentiment is None:
            self.sentiment = {}
        if self.fee_context is None:
            self.fee_context = {}
        if self.kelly_stats is None:
            self.kelly_stats = {}


class TradingToolkit:
    """Tools available to the LLM TraderAgent.

    Every tool returns a dict. The ``propose_trade`` action also returns
    a dict — but additionally invokes the deterministic DecisionEngine to
    veto/cap unsafe proposals.
    """

    def __init__(
        self,
        ctx: ToolkitContext,
        *,
        decision_engine: DecisionEngine,
        edges=None,
        allocator=None,
    ) -> None:
        self.ctx = ctx
        self.decision_engine = decision_engine
        self.edges = edges
        self.allocator = allocator

    # ---------------------------------------------------------------- #
    # Read tools
    # ---------------------------------------------------------------- #

    def get_market_snapshot(self) -> dict:
        c = self.ctx
        return {
            "pair": c.pair,
            "exchange": c.exchange,
            "regime": c.regime,
            "current_price": c.current_price,
            "signal_type": c.market_signal.get("signal_type", "neutral"),
            "signal_confidence": c.market_signal.get("confidence", 0.0),
            "market_condition": c.market_signal.get("market_condition", "unknown"),
            "reasoning": (c.market_signal.get("reasoning") or "")[:300],
            "sentiment_label": c.sentiment.get("sentiment_label", "neutral"),
            "sentiment_score": c.sentiment.get("sentiment_score", 0.0),
            "news_excerpt": c.news_headlines[:500],
        }

    def get_strategy_signals(self) -> dict:
        """Per-strategy signal contributions plus the ensemble verdict."""
        signals = self.ctx.strategy_signals or {}
        out = {
            "ensemble": signals.get("_ensemble"),
            "contributors": {
                name: {
                    "action": v.get("action"),
                    "confidence": v.get("confidence"),
                    "regime": v.get("market_regime"),
                }
                for name, v in signals.items()
                if not name.startswith("_") and isinstance(v, dict)
            },
        }
        return out

    def get_pattern_signal(self) -> dict:
        """Catalyst Pattern Engine output (deterministic, no-LLM)."""
        return dict(self.ctx.pattern_signal or {})

    def get_edge_stats(
        self,
        strategy: Optional[str] = None,
        regime: Optional[str] = None,
    ) -> dict:
        """Look up SignalEdgeLibrary stats for the active regime.

        - With ``strategy``: returns a single edge dict.
        - Without: returns *all* registered edges for the regime.
        """
        if self.edges is None:
            return {"available": False, "reason": "edges unavailable"}
        regime = regime or self.ctx.regime
        try:
            if strategy:
                e = self.edges.edge(strategy, regime)
                return {
                    "available": True,
                    "strategy": strategy,
                    "regime": regime,
                    "edge": e.to_dict() if hasattr(e, "to_dict") else None,
                }
            edges = self.edges.all_edges(regime) or []
            return {
                "available": True,
                "regime": regime,
                "edges": [e.to_dict() for e in edges if hasattr(e, "to_dict")],
            }
        except Exception as exc:
            logger.debug("toolkit.get_edge_stats failed: %s", exc)
            return {"available": False, "reason": str(exc)}

    def get_allocator_weights(self) -> dict:
        """Current capital-allocator weights (per strategy)."""
        if self.allocator is None:
            return {"available": False, "reason": "allocator unavailable"}
        try:
            return {"available": True, "weights": dict(self.allocator.weights() or {})}
        except Exception as exc:
            return {"available": False, "reason": str(exc)}

    def get_portfolio_state(self) -> dict:
        c = self.ctx
        return {
            "portfolio_value": c.portfolio_value,
            "cash_balance": c.cash_balance,
            "open_positions": dict(c.open_positions or {}),
            "kelly_win_rate": c.kelly_stats.get("win_rate", 0.0),
            "kelly_sample_size": c.kelly_stats.get("sample_size", 0),
            "fee_round_trip_pct": c.fee_context.get("round_trip_fee_pct", 0.0),
            "fee_min_gain_pct": c.fee_context.get("min_gain_pct", 0.0),
            "recent_outcomes_excerpt": (c.recent_outcomes or "")[:600],
            "strategic_context_excerpt": (c.strategic_context or "")[:600],
        }

    # ---------------------------------------------------------------- #
    # Action — the only side-effecting tool
    # ---------------------------------------------------------------- #

    def propose_trade(
        self,
        *,
        pair: Optional[str] = None,
        action: str = "hold",
        confidence: float = 0.0,
        strategy: str = "llm_strategist",
        quote_amount: Optional[float] = None,
        quantity: Optional[float] = None,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        reasoning: str = "",
    ) -> dict:
        """Submit a proposal — runs through DecisionEngine for guardrail check.

        Returns the verdict as a dict (so the LLM can introspect rejections
        and try again, e.g. by widening stops or proposing a smaller size).
        """
        c = self.ctx
        proposal = TradeProposal(
            pair=pair or c.pair,
            action=action,
            confidence=float(confidence or 0.0),
            strategy=strategy,
            regime=c.regime,
            ensemble=c.strategy_signals.get("_ensemble") if c.strategy_signals else None,
            pattern_signal=c.pattern_signal,
            quote_amount=quote_amount,
            quantity=quantity,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            current_price=c.current_price,
            reasoning=reasoning,
        )
        verdict = self.decision_engine.evaluate(
            proposal,
            portfolio_value=c.portfolio_value,
            cash_balance=c.cash_balance,
        )
        return verdict.to_dict()

    # ---------------------------------------------------------------- #
    # Self-description (for prompt context, dashboards, audits)
    # ---------------------------------------------------------------- #

    @staticmethod
    def get_tool_schemas() -> list[dict]:
        """OpenAI-compatible JSON tool schemas. Optional — the default
        TraderAgent uses a single chat_json call rather than tool-loop;
        but this is here so future providers (or audit dashboards) have a
        canonical contract.
        """
        return [
            {
                "name": "get_market_snapshot",
                "description": "Current price, regime, signal type, sentiment.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "get_strategy_signals",
                "description": "Per-strategy actions/confidence + ensemble verdict.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "get_pattern_signal",
                "description": "Catalyst Pattern Engine direction/expected_drift.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "get_edge_stats",
                "description": "Edge sharpe/win_rate/sample size for a strategy in current regime.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "strategy": {"type": "string"},
                        "regime": {"type": "string"},
                    },
                },
            },
            {
                "name": "get_allocator_weights",
                "description": "Current capital-allocator weights per strategy.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "get_portfolio_state",
                "description": "Portfolio value, cash, open positions, fee context.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "propose_trade",
                "description": (
                    "Submit a trade proposal. The proposal is run through "
                    "the deterministic DecisionEngine which may veto or cap it."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["action", "confidence"],
                    "properties": {
                        "pair": {"type": "string"},
                        "action": {"type": "string", "enum": ["buy", "sell", "hold"]},
                        "confidence": {"type": "number"},
                        "strategy": {"type": "string"},
                        "quote_amount": {"type": "number"},
                        "quantity": {"type": "number"},
                        "stop_loss_price": {"type": "number"},
                        "take_profit_price": {"type": "number"},
                        "reasoning": {"type": "string"},
                    },
                },
            },
        ]


__all__ = ["TradingToolkit", "ToolkitContext"]
