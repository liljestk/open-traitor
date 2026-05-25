"""
Pydantic schemas for LLM JSON responses.

These act as a trust boundary between the non-deterministic LLM output
and the deterministic trading pipeline. Any field the LLM returns that
doesn't conform is either coerced (numeric clamps, enum fallback to
neutral/hold) or causes the whole response to be rejected so the caller
can fall back to a safe default.

P0: Addresses the "Trust LLM JSON without schema validation" code smell
from the system review.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.models.signal import MarketCondition, SignalType
from src.models.trade import TradeAction


_VALID_SENTIMENT = {"bullish", "bearish", "neutral"}


def _coerce_float(v: Any) -> Optional[float]:
    """Best-effort float coercion; returns None for unusable input."""
    if v is None:
        return None
    if isinstance(v, bool):
        # Guard: bool is an int subclass in Python; reject explicitly.
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # Reject NaN / inf — indicator / price fields must be finite.
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


class MarketAnalystResponse(BaseModel):
    """Validated market-analyst LLM response.

    Unknown fields are ignored rather than raising so the contract can
    evolve without breaking downstream parsing.
    """

    model_config = ConfigDict(extra="ignore")

    signal_type: SignalType = SignalType.NEUTRAL
    confidence: float = 0.0
    market_condition: MarketCondition = MarketCondition.UNKNOWN
    sentiment_overall: Optional[str] = None
    sentiment_score: float = 0.0
    key_factors: list[str] = Field(default_factory=list)
    reasoning: str = ""
    suggested_entry: Optional[float] = None
    suggested_stop_loss: Optional[float] = None
    suggested_take_profit: Optional[float] = None

    @field_validator("signal_type", mode="before")
    @classmethod
    def _coerce_signal_type(cls, v: Any) -> Any:
        if isinstance(v, SignalType):
            return v
        if isinstance(v, str):
            try:
                return SignalType(v.strip().lower())
            except ValueError:
                return SignalType.NEUTRAL
        return SignalType.NEUTRAL

    @field_validator("market_condition", mode="before")
    @classmethod
    def _coerce_market_condition(cls, v: Any) -> Any:
        if isinstance(v, MarketCondition):
            return v
        if isinstance(v, str):
            try:
                return MarketCondition(v.strip().lower())
            except ValueError:
                return MarketCondition.UNKNOWN
        return MarketCondition.UNKNOWN

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v: Any) -> float:
        f = _coerce_float(v)
        if f is None:
            return 0.0
        return max(0.0, min(1.0, f))

    @field_validator("sentiment_score", mode="before")
    @classmethod
    def _clamp_sentiment_score(cls, v: Any) -> float:
        f = _coerce_float(v)
        if f is None:
            return 0.0
        return max(-1.0, min(1.0, f))

    @field_validator("sentiment_overall", mode="before")
    @classmethod
    def _normalize_sentiment_overall(cls, v: Any) -> Optional[str]:
        if not isinstance(v, str):
            return None
        s = v.strip().lower()
        return s if s in _VALID_SENTIMENT else None

    @field_validator(
        "suggested_entry", "suggested_stop_loss", "suggested_take_profit",
        mode="before",
    )
    @classmethod
    def _sanitize_price(cls, v: Any) -> Optional[float]:
        f = _coerce_float(v)
        if f is None or f <= 0:
            return None
        return f

    @field_validator("key_factors", mode="before")
    @classmethod
    def _normalize_factors(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        if isinstance(v, (list, tuple)):
            return [str(x) for x in v if x is not None]
        return []

    @field_validator("reasoning", mode="before")
    @classmethod
    def _normalize_reasoning(cls, v: Any) -> str:
        return "" if v is None else str(v)


class StrategistResponse(BaseModel):
    """Validated strategist LLM response.

    Hard constraint: action MUST be one of buy/sell/hold. Anything
    unparseable collapses to "hold" — the safe default.
    """

    model_config = ConfigDict(extra="ignore")

    action: TradeAction = TradeAction.HOLD
    pair: str = ""
    confidence: float = 0.0
    quote_amount: Optional[float] = None
    quantity: Optional[float] = None
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    strategy_horizon_days: Optional[int] = None
    expected_gross_return_pct: Optional[float] = None
    expected_net_return_pct: Optional[float] = None
    exit_policy: str = ""
    reasoning: str = ""
    task_alignment: str = ""

    @field_validator("action", mode="before")
    @classmethod
    def _coerce_action(cls, v: Any) -> Any:
        if isinstance(v, TradeAction):
            return v
        if isinstance(v, str):
            s = v.strip().lower()
            if s in {"buy", "sell", "hold"}:
                return TradeAction(s)
        # Any unknown value collapses to the safe default.
        return TradeAction.HOLD

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v: Any) -> float:
        f = _coerce_float(v)
        if f is None:
            return 0.0
        return max(0.0, min(1.0, f))

    @field_validator(
        "quote_amount", "quantity",
        "stop_loss_price", "take_profit_price",
        mode="before",
    )
    @classmethod
    def _sanitize_positive(cls, v: Any) -> Optional[float]:
        f = _coerce_float(v)
        if f is None or f <= 0:
            return None
        return f

    @field_validator("expected_gross_return_pct", "expected_net_return_pct", mode="before")
    @classmethod
    def _sanitize_return_pct(cls, v: Any) -> Optional[float]:
        f = _coerce_float(v)
        if f is None:
            return None
        if abs(f) > 1.0:
            f /= 100.0
        return max(-1.0, min(1.0, f))

    @field_validator("strategy_horizon_days", mode="before")
    @classmethod
    def _sanitize_horizon(cls, v: Any) -> Optional[int]:
        f = _coerce_float(v)
        if f is None or f <= 0:
            return None
        return max(1, min(365, int(f)))

    @field_validator("pair", "reasoning", "task_alignment", "exit_policy", mode="before")
    @classmethod
    def _normalize_str(cls, v: Any) -> str:
        return "" if v is None else str(v)


def validate_market_analyst(raw: dict) -> tuple[dict, Optional[str]]:
    """Validate a market-analyst LLM response.

    Returns (sanitized_dict, error). On success error is None; on failure
    the caller should fall back (e.g. to technical-only).
    """
    if not isinstance(raw, dict):
        return {}, f"expected dict, got {type(raw).__name__}"
    try:
        model = MarketAnalystResponse(**raw)
    except Exception as e:  # pragma: no cover - defensive
        return {}, f"schema validation failed: {e}"
    return model.model_dump(mode="json"), None


def validate_strategist(raw: dict) -> tuple[dict, Optional[str]]:
    """Validate a strategist LLM response.

    Returns (sanitized_dict, error). On error the caller should treat
    the decision as "hold".
    """
    if not isinstance(raw, dict):
        return {}, f"expected dict, got {type(raw).__name__}"
    try:
        model = StrategistResponse(**raw)
    except Exception as e:  # pragma: no cover - defensive
        return {}, f"schema validation failed: {e}"
    return model.model_dump(mode="json"), None
