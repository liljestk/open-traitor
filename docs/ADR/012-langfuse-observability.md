# ADR-012: Self-Hosted Langfuse for LLM Observability

**Status:** Accepted

## Context

Every trading cycle makes multiple LLM calls across agents (MarketAnalyst, Strategist, RiskManager). Without structured observability, diagnosing why a trade was made — or why a good opportunity was missed — requires manual log parsing. We need per-cycle, per-agent tracing with token usage, latency, and input/output capture.

## Decision

Integrate **Langfuse v3** (self-hosted or cloud) for unified LLM observability, with **Redis pub/sub** for real-time dashboard streaming and **graceful degradation** when Langfuse is unavailable.

### Trace Architecture

```
Trace (per trading cycle)
  └─ Generation (per agent call)
      ├─ input: [system_prompt, user_message]  (truncated to 2000 chars)
      ├─ output: LLM response text
      ├─ usage: {input_tokens, output_tokens}
      ├─ latency_ms
      └─ metadata: {cycle_id, pair, exchange, model}
```

### Span Lifecycle

**1. Start trace** (per cycle):
```python
trace_ctx = tracer.start_trace(
    cycle_id="cx_abc123",
    pair="BTC-USD",
    metadata={"exchange": "coinbase", "mode": "live"},
    session_id="auto-traitor-2026-03-08"  # daily grouping
)
```

**2. Start span** (per agent):
```python
span = trace_ctx.start_span(
    agent_name="market_analyst",
    input_data={"system": "...", "user": "..."},
    model="llama3.1:8b"
)
```

**3. Finish span** (after LLM response):
```python
span.finish(
    output="...",
    prompt_tokens=150,
    completion_tokens=100,
    latency_ms=2340.0,
    model="llama3.1:8b"
)
```

### Redis Pub/Sub (Real-Time Streaming)

On span completion, events are published to the `llm:events` Redis channel:

```json
{
    "type": "span_finished",
    "cycle_id": "cx_abc123",
    "pair": "BTC-USD",
    "agent_name": "market_analyst",
    "model": "llama3.1:8b",
    "trace_id": "abc123...",
    "span_id": "xyz789...",
    "prompt_tokens": 150,
    "completion_tokens": 100,
    "latency_ms": 2340.0,
    "ts": "2026-03-08T14:30:45Z"
}
```

The dashboard WebSocket server subscribes to this channel for live LLM activity visualization.

### Graceful Degradation

- If Langfuse is unavailable or keys are missing, all tracing methods become **no-ops**.
- Trading continues unaffected — tracing failures never block the pipeline.
- All tracing methods wrap operations in try/except to prevent exceptions from propagating.

### Session Grouping

`session_id` defaults to `auto-traitor-{today}` for daily trace aggregation in the Langfuse UI, enabling day-by-day analysis of agent behavior and token consumption.

### Initialization

```python
LLMTracer.init(
    public_key="pk-...",
    secret_key="sk-...",
    host="http://langfuse:3000",
    redis_client=redis_client  # optional
)
```

`LLMTracer` is a singleton — initialized once at startup, shared across all agents and cycles.

## Consequences

**Benefits:**
- Full audit trail: every LLM call is captured with inputs, outputs, tokens, and latency.
- Session grouping enables day-over-day comparison of agent behavior.
- Redis pub/sub provides live dashboard visibility without polling.
- Graceful degradation means Langfuse is optional — not a hard dependency.

**Risks:**
- Input truncation (2000 chars) may lose context details. Acceptable trade-off for storage efficiency.
- Redis pub/sub is fire-and-forget; if no subscriber is listening, events are lost (acceptable for live views).
- Self-hosted Langfuse requires infrastructure maintenance.

**Follow-on:**
- Langfuse traces feed into the ALE's PromptEvolver (ADR-010) for analyzing prediction patterns.
- Agent reasoning persistence (for fine-tuning) is separate from Langfuse traces but covers similar data.
