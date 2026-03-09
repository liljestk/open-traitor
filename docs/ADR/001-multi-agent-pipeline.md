# ADR-001: Multi-Agent LLM Pipeline

**Status:** Accepted

## Context

Autonomous trading requires multiple specialized capabilities — market analysis, strategy formulation, risk assessment, and order execution — each with distinct domain knowledge and failure modes. A monolithic LLM prompt combining all responsibilities would be fragile, hard to debug, and impossible to improve incrementally.

We need an architecture where each concern is handled by a dedicated agent, producing typed outputs that flow through a pipeline with clear contracts.

## Decision

Adopt a **multi-agent pipeline** orchestrated by a central `Orchestrator` that runs each trading pair through a sequential agent chain per cycle:

```
Orchestrator.run_forever()
  └─ for each pair (parallel):
       1. MarketAnalyst.run(candles, news)       → Signal
       2. Strategist.run(signal, holdings)       → Proposal
       3. RiskManager.run(proposal, portfolio)   → ApprovedTrade
       4. Executor.run(approved_trade)           → ExecutionResult
       5. TrailingStops.update_prices()          → triggered exits
```

### Agent Responsibilities

| Agent | Input | Output | Key Logic |
|-------|-------|--------|-----------|
| **MarketAnalyst** | Candles, news, technical indicators | Signal (`strong_buy` → `strong_sell`) + confidence | LLM-powered analysis with confidence gate (`min_signal_confidence` default 0.65) |
| **Strategist** | Signal, holdings, fees, strategic context | Trade proposal (pair, side, size, TP/SL) | Fee-aware proposal generation; respects planning context as soft input |
| **RiskManager** | Proposal, portfolio state, historical stats | Approved trade with sized position | Kelly Criterion (half-Kelly), ATR volatility adjustment, correlation penalty, signal-strength multiplier |
| **Executor** | Approved trade | Execution result | Limit vs market order selection; limit offset of 0.1% for maker rebates |

### Key Design Choices

- **Signal-driven sizing**: Confidence and signal strength translate directly to capital allocation via multipliers (`strong_buy=1.0×`, `buy=0.8×`, `weak_buy=0.6×`).
- **Monitoring mode**: When the portfolio is full and cash is insufficient for the minimum trade, the orchestrator skips the expensive LLM pipeline entirely and only refreshes prices for trailing stops.
- **Base agent contract**: All agents extend `BaseAgent`, providing consistent lifecycle (`run()`, structured input/output, reasoning persistence).
- **Ensemble calibration**: Per-pair confidence calibration from historical signal accuracy.

### Position Sizing Stack (RiskManager)

1. Kelly Criterion from historical win rate (half-Kelly for safety)
2. ATR-based volatility adjustment (2× for stop-loss, 3× for take-profit)
3. Correlation penalty (up to 50% reduction when ρ ≥ 0.7)
4. Signal strength multiplier
5. Floor for strong signals (prevents near-zero allocations)
6. Cap by `max_position_pct` from tier config

## Consequences

**Benefits:**
- Each agent can be improved, tested, or replaced independently.
- Reasoning is persisted per-agent, enabling fine-tuning data collection and observability.
- Pipeline short-circuits early (monitoring mode) to save API costs.
- Position sizing is multi-layered, preventing oversized bets.

**Risks:**
- Sequential pipeline adds latency per pair (~3–8s with cloud LLM, ~1s with local Ollama).
- Agent contracts must remain stable; schema changes require coordinated updates.
- LLM non-determinism means identical inputs can produce different signals across cycles.

**Mitigations:**
- Pairs run in parallel; latency is per-pair, not cumulative.
- Confidence gates and AbsoluteRules provide hard stops regardless of LLM output.
- Langfuse tracing (ADR-012) captures every agent call for post-hoc analysis.
