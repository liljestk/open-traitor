# ADR-006: Temporal for Multi-Horizon Planning

**Status:** Accepted

## Context

The fast trading loop (sub-second to seconds per cycle) handles real-time decisions, but strategic planning requires longer analysis windows — reviewing past performance, assessing market regimes, and adjusting pair allocations. Running planning logic inside the trading loop would block execution and conflate fast-path decisions with slow-path strategy.

## Decision

Use **Temporal** to run multi-horizon planning workflows on independent schedules, decoupled from the fast trading loop. Plans are persisted to the database and consumed as **soft context** by trading agents.

### Workflows

| Workflow | Schedule | Horizon | Purpose |
|----------|----------|---------|---------|
| `DailyPlanWorkflow` | 00:00 UTC daily | 7 days | Evaluate yesterday's performance, set today's pair focus and risk posture |
| `WeeklyReviewWorkflow` | 00:00 UTC Monday | 30 days | Broader performance trends, strategy weight adjustments |
| `MonthlyReviewWorkflow` | 00:00 UTC 1st | 90 days + YTD | Long-term regime assessment, allocation rebalancing |

### Activity Chain (shared across workflows)

```
evaluate_previous_plan
  → fetch_trade_history (filtered by date range & exchange)
  → fetch_portfolio_history (balance evolution)
  → fetch_pair_universe (monitored pairs)
  → call_planning_llm (single LLM call with all history as context)
  → write_strategic_context (persist to strategic_context table)
  → write_daily_plan
```

### Worker Architecture

A standalone Temporal worker process polls for scheduled workflow instances:

```python
Worker(
    client,
    task_queue="planning",
    workflows=[DailyPlanWorkflow, WeeklyReviewWorkflow, MonthlyReviewWorkflow],
    activities=[evaluate_previous_plan, fetch_trade_history, ...],
)
```

### Fast-Loop Integration

The orchestrator reads the latest `strategic_context` each cycle and injects the plan summary as **soft prompt context** into agent LLM calls. Plans inform but do not override real-time decisions:

- Agents see regime assessments ("high volatility regime detected") as context.
- Agents retain full autonomy to deviate based on live market data.
- No hard constraint is ever set by a planning output.

### Profile Awareness

All activities filter by `exchange` to maintain domain separation (ADR-003). A Coinbase planning workflow only sees crypto trades and portfolio history.

## Consequences

**Benefits:**
- Strategic planning runs independently; fast loop is never blocked.
- Multi-horizon analysis captures patterns at different time scales.
- Plans are persisted with timestamps, enabling historical strategy auditing.
- Temporal handles retries, timeouts, and workflow versioning.

**Risks:**
- Temporal becomes an infrastructure dependency (mitigated: planning failure doesn't halt trading).
- Stale plans: if Temporal is down, agents use the last available plan (graceful degradation).
- LLM-generated plans are non-deterministic; consecutive runs may produce different assessments.

**Trade-offs:**
- Soft context means plans can be ignored by agents if live data contradicts them. This is intentional — real-time data should always win.
- Running a separate Temporal worker process adds operational complexity.
