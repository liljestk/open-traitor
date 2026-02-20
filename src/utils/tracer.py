"""
LLM Tracer — Unified observability for every LLM call made by the trading agents.

Architecture:
  - Langfuse (self-hosted) is the primary trace store. Each trading cycle creates one
    Langfuse trace; each agent LLM call within that cycle creates a generation span.
  - Redis pub/sub (channel: llm:events) is used for real-time WebSocket streaming to
    the dashboard. If Redis is unavailable the tracer still works (Langfuse-only).
  - All methods degrade gracefully — if Langfuse is down, tracing is a no-op and
    trading continues without interruption.

Usage (in an agent):
    trace_ctx = tracer.start_trace(cycle_id, pair, metadata={...})
    span = trace_ctx.start_span("market_analyst", input_data={...}, model="llama3.1:8b")
    # ... pass span to LLMClient.chat_json(span=span) ...
    # LLMClient calls span.finish(...) after the API call
    # Read back: span.trace_id, span.span_id, span.prompt_tokens, etc.
"""

from __future__ import annotations

import json
import os
import time
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from src.utils.logger import get_logger

logger = get_logger("utils.tracer")

# Redis pub/sub channel for live dashboard streaming
_REDIS_CHANNEL = "llm:events"


# ─── Span context (returned per LLM call) ─────────────────────────────────────

@dataclass
class SpanContext:
    """
    Represents a single LLM generation span inside a trace.
    Created by TraceContext.start_span(); finished by LLMClient after the API call.
    After .finish() the metrics fields are populated and can be read by the agent to
    persist into StatsDB alongside the reasoning JSON.
    """
    trace_id: str
    agent_name: str
    model: str
    input_data: dict

    # Populated by .finish()
    span_id: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    output: Any = None

    # Internal Langfuse generation object
    _generation: Any = field(default=None, repr=False)
    # Redis client reference for pub/sub
    _redis: Any = field(default=None, repr=False)
    _pair: str = field(default="", repr=False)
    _cycle_id: str = field(default="", repr=False)
    _start_time: float = field(default_factory=time.time, repr=False)

    def finish(
        self,
        output: Any,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: Optional[float] = None,
    ) -> None:
        """Called by LLMClient after the API call completes."""
        self.output = output
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.latency_ms = latency_ms if latency_ms is not None else (time.time() - self._start_time) * 1000

        # Finish Langfuse generation (v3 SDK: update then end)
        if self._generation is not None:
            try:
                truncated_output = str(output)[:2000] if not isinstance(output, str) else output[:2000]
                self._generation.update(
                    output=truncated_output,
                    usage_details={
                        "input": prompt_tokens,
                        "output": completion_tokens,
                    },
                )
                self._generation.end()
                self.span_id = getattr(self._generation, "id", "") or ""
            except Exception as e:
                logger.debug(f"Langfuse generation.end failed: {e}")

        # Publish to Redis for live dashboard streaming
        if self._redis is not None:
            try:
                event = {
                    "type": "span_finished",
                    "cycle_id": self._cycle_id,
                    "pair": self._pair,
                    "agent_name": self.agent_name,
                    "model": self.model,
                    "trace_id": self.trace_id,
                    "span_id": self.span_id,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "latency_ms": round(self.latency_ms, 1),
                    "ts": time.time(),
                }
                self._redis.publish(_REDIS_CHANNEL, json.dumps(event))
            except Exception as e:
                logger.debug(f"Redis publish failed: {e}")


# ─── Trace context (one per trading cycle) ────────────────────────────────────

class TraceContext:
    """
    Represents a single trading cycle trace in Langfuse.
    Create once per cycle via LLMTracer.start_trace(), then call start_span() for each
    agent LLM call within the cycle.
    """

    def __init__(
        self,
        cycle_id: str,
        pair: str,
        trace: Any,  # Langfuse span object (v3 SDK) or None
        redis_client: Any,
        metadata: Optional[dict] = None,
        session_id: Optional[str] = None,
    ):
        self._cycle_id = cycle_id
        self._pair = pair
        self._trace = trace
        self._redis = redis_client
        self._metadata = metadata or {}
        self._session_id = session_id
        self._spans: list[SpanContext] = []

        # Publish trace-started event
        if redis_client is not None:
            try:
                redis_client.publish(_REDIS_CHANNEL, json.dumps({
                    "type": "trace_started",
                    "cycle_id": cycle_id,
                    "pair": pair,
                    "ts": time.time(),
                    "metadata": metadata or {},
                }))
            except Exception:
                pass

    @property
    def trace_id(self) -> str:
        return self._cycle_id

    def start_span(
        self,
        agent_name: str,
        input_data: dict,
        model: str = "",
    ) -> SpanContext:
        """
        Start a new generation span for one agent LLM call.
        The returned SpanContext must be passed to LLMClient.chat() as the `span` argument.
        LLMClient will call span.finish() after the API response.
        """
        generation = None
        if self._trace is not None:
            try:
                generation = self._trace.start_observation(
                    as_type="generation",
                    name=agent_name,
                    model=model,
                    input=[
                        {"role": "system", "content": input_data.get("system", "")[:2000]},
                        {"role": "user", "content": input_data.get("user", "")[:2000]},
                    ],
                    metadata={"cycle_id": self._cycle_id, "pair": self._pair},
                )
            except Exception as e:
                logger.debug(f"Langfuse generation start failed: {e}")

        span = SpanContext(
            trace_id=self._cycle_id,
            agent_name=agent_name,
            model=model,
            input_data=input_data,
            _generation=generation,
            _redis=self._redis,
            _pair=self._pair,
            _cycle_id=self._cycle_id,
            _start_time=time.time(),
        )
        self._spans.append(span)
        return span

    def finish(self, metadata: Optional[dict] = None) -> None:
        """Optionally annotate the trace with final metadata (e.g., trade outcome)."""
        if self._trace is not None:
            try:
                if metadata:
                    self._trace.update(metadata=metadata)
                self._trace.end()
            except Exception as e:
                logger.debug(f"Langfuse trace span finish failed: {e}")
            # Flush to ensure this trace's events are sent to Langfuse
            tracer = LLMTracer._instance
            if tracer and tracer._langfuse is not None:
                try:
                    tracer._langfuse.flush()
                except Exception:
                    pass

        if self._redis is not None:
            try:
                self._redis.publish(_REDIS_CHANNEL, json.dumps({
                    "type": "trace_finished",
                    "cycle_id": self._cycle_id,
                    "pair": self._pair,
                    "ts": time.time(),
                    "metadata": metadata or {},
                }))
            except Exception:
                pass


# ─── Null no-op context (used when Langfuse is disabled/unavailable) ──────────

class _NullSpanContext:
    """No-op span — all attribute accesses return safe defaults."""
    trace_id = ""
    span_id = ""
    agent_name = ""
    model = ""
    prompt_tokens = 0
    completion_tokens = 0
    latency_ms = 0.0
    output = None

    def finish(self, output: Any = None, prompt_tokens: int = 0,
               completion_tokens: int = 0, latency_ms: Optional[float] = None) -> None:
        pass


class _NullTraceContext:
    trace_id = ""

    def start_span(self, agent_name: str, input_data: dict, model: str = "") -> _NullSpanContext:
        return _NullSpanContext()

    def finish(self, metadata: Optional[dict] = None) -> None:
        pass


# ─── LLMTracer singleton ──────────────────────────────────────────────────────

class LLMTracer:
    """
    Singleton gateway to Langfuse tracing.
    Initialise once in main.py (or on first call to get_llm_tracer()) and
    then obtain from anywhere via get_llm_tracer().
    """

    _instance: Optional["LLMTracer"] = None
    _lock = threading.Lock()

    def __init__(
        self,
        public_key: str,
        secret_key: str,
        host: str = "http://localhost:3000",
        redis_client: Any = None,
        enabled: bool = True,
    ):
        self._enabled = enabled
        self._redis = redis_client
        self._langfuse: Any = None

        if not enabled:
            logger.info("🔍 LLM Tracer disabled — running in no-op mode")
            return

        try:
            from langfuse import Langfuse  # type: ignore[import]
            self._langfuse = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                host=host,
            )
            # Verify connectivity and credentials
            if self._langfuse.auth_check():
                logger.info(f"✅ LLM Tracer initialized — Langfuse at {host}")
            else:
                logger.warning(f"⚠️ Langfuse auth_check failed — keys may be wrong or server unreachable at {host}")
                self._langfuse = None
        except ImportError:
            logger.warning("⚠️ langfuse package not installed — tracing disabled. Run: pip install langfuse")
        except Exception as e:
            logger.warning(f"⚠️ Langfuse init failed ({e}) — tracing degraded to Redis-only")

    # ── Factory / singleton ────────────────────────────────────────────────

    @classmethod
    def init(
        cls,
        public_key: str,
        secret_key: str,
        host: str = "http://localhost:3000",
        redis_client: Any = None,
        enabled: bool = True,
    ) -> "LLMTracer":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(
                    public_key=public_key,
                    secret_key=secret_key,
                    host=host,
                    redis_client=redis_client,
                    enabled=enabled,
                )
        return cls._instance

    # ── Public API ─────────────────────────────────────────────────────────

    def start_trace(
        self,
        cycle_id: str,
        pair: str,
        metadata: Optional[dict] = None,
        session_id: Optional[str] = None,
    ) -> TraceContext:
        """
        Start a new trace for one complete trading cycle.
        Returns a TraceContext used to create per-agent spans.

        session_id groups related traces in Langfuse (e.g. all cycles for a day).
        Defaults to ``auto-traitor-{today}`` so every daily run is one session.
        """
        if not self._enabled:
            return _NullTraceContext()  # type: ignore[return-value]

        resolved_session_id = session_id or f"auto-traitor-{date.today().isoformat()}"

        langfuse_span = None
        if self._langfuse is not None:
            try:
                # Langfuse SDK v3: create a root span and set trace-level attrs
                langfuse_span = self._langfuse.start_span(
                    name=f"trading-cycle-{pair}",
                    metadata={"pair": pair, "cycle_id": cycle_id, **(metadata or {})},
                )
                langfuse_span.update_trace(
                    name=f"trading-cycle-{pair}",
                    session_id=resolved_session_id,
                    tags=[pair, "trading-cycle"],
                    metadata={"pair": pair, **(metadata or {})},
                )
            except Exception as e:
                logger.debug(f"Langfuse trace creation failed: {e}")

        return TraceContext(
            cycle_id=cycle_id,
            pair=pair,
            trace=langfuse_span,
            redis_client=self._redis,
            metadata=metadata,
            session_id=resolved_session_id,
        )

    def flush(self) -> None:
        """Flush any pending Langfuse events (call on shutdown)."""
        if self._langfuse is not None:
            try:
                self._langfuse.flush()
            except Exception:
                pass


# ─── Module-level singleton accessor ─────────────────────────────────────────

def get_llm_tracer() -> Optional[LLMTracer]:
    """Return the singleton LLMTracer, or None if not yet initialised."""
    return LLMTracer._instance
