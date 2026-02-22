"""
Training Data Collector — Captures structured data for future model fine-tuning.

Records market snapshots, LLM prompt/response pairs, agent decisions, and
trade outcomes in JSONL files organized by date.  Every write is fire-and-forget
(errors are swallowed with a debug log) so the collector can *never* break
the main trading pipeline.

Directory layout:
    data/{profile}/training/YYYY-MM-DD/
        market_snapshots.jsonl   — full feature vector per pipeline run
        llm_interactions.jsonl   — every LLM prompt + response
        decisions.jsonl          — agent decisions with context
        outcomes.jsonl           — PnL outcomes when trades close

Each line is a self-contained JSON object; the files are safe for concurrent
append (one writer thread at a time via an internal lock).
"""

from __future__ import annotations

import contextvars
import json
import os
import threading
import time
from datetime import datetime, timezone, date as dt_date
from typing import Any, Optional

from src.utils.helpers import get_data_dir
from src.utils.logger import get_logger

logger = get_logger("utils.training_data")

# Sentinel object so callers can skip optional params without None ambiguity
_UNSET = object()


class TrainingDataCollector:
    """
    Non-blocking collector that persists rich training samples to JSONL files.

    Usage:
        collector = TrainingDataCollector(config)

        # In pipeline:
        collector.record_snapshot(cycle_id, pair, features)
        collector.record_llm_interaction(cycle_id, pair, agent, prompt, response, ...)
        collector.record_decision(cycle_id, pair, stage, decision, context)
        collector.record_outcome(trade_id, pair, pnl, metadata)

    All public methods are fully guarded — they never raise.
    """

    def __init__(self, config: dict | None = None):
        self._enabled: bool = False
        self._base_dir: str = ""
        self._lock = threading.Lock()
        # In-memory buffer: optional background flush (future improvement)
        self._buffer: list[tuple[str, dict]] = []
        self._buffer_limit = 0  # 0 = immediate flush (safest default)
        self._flush_lock = threading.Lock()

        if config is None:
            config = {}

        td_cfg = config.get("training_data", {})
        self._enabled = td_cfg.get("enabled", False)

        if not self._enabled:
            logger.info("📦 Training data collector: disabled")
            return

        data_dir = get_data_dir()
        self._base_dir = os.path.join(data_dir, "training")
        os.makedirs(self._base_dir, exist_ok=True)

        self._buffer_limit = td_cfg.get("buffer_size", 0)
        self._include_raw_candles = td_cfg.get("include_raw_candles", False)
        self._include_prompts = td_cfg.get("include_prompts", True)
        self._max_candles = td_cfg.get("max_candles_per_snapshot", 50)

        logger.info(
            f"📦 Training data collector: enabled | dir={self._base_dir} | "
            f"candles={'yes' if self._include_raw_candles else 'no'} | "
            f"prompts={'yes' if self._include_prompts else 'no'}"
        )

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── Public API ────────────────────────────────────────────────────────

    def record_snapshot(
        self,
        cycle_id: str,
        pair: str,
        *,
        price: float = 0.0,
        candles: Any = _UNSET,
        technical: dict | None = None,
        strategy_signals: dict | None = None,
        fear_greed: str = "",
        multi_timeframe: str = "",
        sentiment: dict | None = None,
        correlation_matrix: dict | None = None,
        kelly_stats: dict | None = None,
        portfolio_value: float = 0.0,
        cash_balance: float = 0.0,
        open_positions: dict | None = None,
        recent_outcomes: str = "",
        strategic_context: str = "",
        extra: dict | None = None,
    ) -> None:
        """Record the full feature vector available at pipeline start."""
        if not self._enabled:
            return
        try:
            record: dict[str, Any] = {
                "ts": _now_iso(),
                "cycle_id": cycle_id,
                "pair": pair,
                "price": price,
                "technical": _safe_dict(technical),
                "strategy_signals": _safe_dict(strategy_signals),
                "fear_greed": fear_greed,
                "multi_timeframe": multi_timeframe,
                "sentiment": _safe_dict(sentiment),
                "correlation_matrix": _safe_dict(correlation_matrix),
                "kelly_stats": _safe_dict(kelly_stats),
                "portfolio_value": portfolio_value,
                "cash_balance": cash_balance,
                "open_positions": _safe_dict(open_positions),
                "recent_outcomes": recent_outcomes,
                "strategic_context": strategic_context[:500] if strategic_context else "",
            }

            if self._include_raw_candles and candles is not _UNSET:
                record["candles"] = _truncate_candles(candles, self._max_candles)

            if extra:
                record["extra"] = _safe_dict(extra)

            self._write("market_snapshots", record)
        except Exception as e:
            logger.debug(f"Training snapshot write failed (non-fatal): {e}")

    def record_llm_interaction(
        self,
        cycle_id: str,
        pair: str,
        agent_name: str,
        *,
        system_prompt: str = "",
        user_message: str = "",
        response_text: str = "",
        parsed_response: dict | None = None,
        provider: str = "",
        model: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: float = 0.0,
        temperature: float = 0.0,
        error: str = "",
    ) -> None:
        """Record a single LLM prompt/response pair."""
        if not self._enabled:
            return
        try:
            record: dict[str, Any] = {
                "ts": _now_iso(),
                "cycle_id": cycle_id,
                "pair": pair,
                "agent": agent_name,
                "provider": provider,
                "model": model,
                "temperature": temperature,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "latency_ms": round(latency_ms, 1),
            }

            if self._include_prompts:
                record["system_prompt"] = system_prompt
                record["user_message"] = user_message

            record["response_text"] = response_text
            record["parsed_response"] = _safe_dict(parsed_response)

            if error:
                record["error"] = error

            self._write("llm_interactions", record)
        except Exception as e:
            logger.debug(f"Training LLM interaction write failed (non-fatal): {e}")

    def record_decision(
        self,
        cycle_id: str,
        pair: str,
        stage: str,
        *,
        decision: dict | None = None,
        action: str = "",
        confidence: float = 0.0,
        reasoning: str = "",
        approved: bool | None = None,
        context: dict | None = None,
    ) -> None:
        """Record an agent decision at any pipeline stage.

        Stages: "analysis", "strategy", "risk", "execution", "hold",
                "rejected", "pending_approval"
        """
        if not self._enabled:
            return
        try:
            record: dict[str, Any] = {
                "ts": _now_iso(),
                "cycle_id": cycle_id,
                "pair": pair,
                "stage": stage,
                "action": action,
                "confidence": confidence,
                "reasoning": reasoning[:1000] if reasoning else "",
            }
            if approved is not None:
                record["approved"] = approved
            if decision:
                record["decision"] = _safe_dict(decision)
            if context:
                record["context"] = _safe_dict(context)

            self._write("decisions", record)
        except Exception as e:
            logger.debug(f"Training decision write failed (non-fatal): {e}")

    def record_outcome(
        self,
        trade_id: str,
        pair: str,
        *,
        action: str = "",
        entry_price: float = 0.0,
        exit_price: float = 0.0,
        quantity: float = 0.0,
        pnl: float = 0.0,
        pnl_pct: float = 0.0,
        fees: float = 0.0,
        hold_duration_seconds: float = 0.0,
        exit_reason: str = "",
        cycle_id: str = "",
        extra: dict | None = None,
    ) -> None:
        """Record the realized outcome of a closed trade.

        This is the *label* for supervised learning: given the features
        at decision time, was this a good or bad trade?
        """
        if not self._enabled:
            return
        try:
            record: dict[str, Any] = {
                "ts": _now_iso(),
                "trade_id": trade_id,
                "cycle_id": cycle_id,
                "pair": pair,
                "action": action,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "quantity": quantity,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "fees": fees,
                "hold_duration_seconds": hold_duration_seconds,
                "exit_reason": exit_reason,
            }
            if extra:
                record["extra"] = _safe_dict(extra)

            self._write("outcomes", record)
        except Exception as e:
            logger.debug(f"Training outcome write failed (non-fatal): {e}")

    # ── Internal helpers ──────────────────────────────────────────────────

    def _write(self, category: str, record: dict) -> None:
        """Append *record* as a JSON line to the day-partitioned file."""
        today = dt_date.today().isoformat()
        day_dir = os.path.join(self._base_dir, today)
        os.makedirs(day_dir, exist_ok=True)
        path = os.path.join(day_dir, f"{category}.jsonl")

        line = json.dumps(record, default=str, ensure_ascii=False) + "\n"

        with self._lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)

    def flush(self) -> None:
        """No-op for now (immediate writes). Reserved for future buffering."""
        pass

    def make_llm_callback(self):
        """Return a callback function for LLMClient._interaction_callback.

        The callback captures the current pipeline context (cycle_id, pair)
        from thread-local storage set via ``set_pipeline_context``.
        """
        if not self._enabled:
            return None

        collector = self

        def _on_llm_interaction(
            agent_name: str = "",
            system_prompt: str = "",
            user_message: str = "",
            response_text: str = "",
            provider: str = "",
            model: str = "",
            prompt_tokens: int = 0,
            completion_tokens: int = 0,
            latency_ms: float = 0.0,
            temperature: float = 0.0,
        ) -> None:
            ctx = _pipeline_context.get()
            collector.record_llm_interaction(
                ctx.get("cycle_id", ""),
                ctx.get("pair", ""),
                agent_name,
                system_prompt=system_prompt,
                user_message=user_message,
                response_text=response_text,
                provider=provider,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                temperature=temperature,
            )

        return _on_llm_interaction

    def set_pipeline_context(self, cycle_id: str, pair: str) -> None:
        """Set the current pipeline context for LLM callback routing."""
        _pipeline_context.set({"cycle_id": cycle_id, "pair": pair})

    def clear_pipeline_context(self) -> None:
        """Clear the pipeline context after a pipeline run."""
        _pipeline_context.set({})


# ── Module-level helpers ─────────────────────────────────────────────────

_pipeline_context: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "training_pipeline_ctx", default={}
)

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_dict(d: Any) -> dict:
    """Return *d* if it's a dict, otherwise empty dict. Never raises."""
    if isinstance(d, dict):
        return d
    return {}


def _truncate_candles(candles: Any, max_rows: int) -> list:
    """Keep only the most recent *max_rows* candles (as dicts or lists)."""
    try:
        if not candles:
            return []
        if isinstance(candles, list):
            return candles[-max_rows:]
        # numpy / pandas-like — convert last N rows
        return list(candles[-max_rows:])
    except Exception:
        return []
