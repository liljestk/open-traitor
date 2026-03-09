# ADR-002: Absolute Rules Engine

**Status:** Accepted

## Context

LLM agents are non-deterministic. No matter how well-prompted, they may occasionally recommend trades that violate hard safety constraints — exceeding daily loss limits, overconcentrating in a single position, or trading blacklisted pairs. We need an inviolable safety layer that sits between agent proposals and execution.

## Decision

Implement an `AbsoluteRules` engine that enforces hard trading constraints at the boundary between the RiskManager and Executor. Rules are checked synchronously on every trade proposal.

### Critical Invariant: Sells Are Never Blocked

```python
if not is_buy:  # SELL/EXIT orders
    return True, [], False  # Always allowed, no violations
```

This ensures the system can **always exit positions** even when:
- Daily loss limit is breached
- Maximum trades per day is reached
- The pair is blacklisted
- Emergency stop is triggered

Blocking exits would trap capital in losing positions — the worst possible outcome.

### Rules Enforced (BUY only)

| Rule | Default Bounds | Description |
|------|---------------|-------------|
| `max_single_trade` | $1–$100K | Maximum value of a single buy order |
| `max_daily_spend` | $1–$500K | Cumulative buy spend per UTC day |
| `max_daily_loss` | $0–$100K | Maximum realized loss per UTC day |
| `max_trades_per_day` | 1–1,000 | Rate limit on total trades (buy + sell) |
| `max_portfolio_risk_pct` | Tier-dependent | `quote_amount / portfolio_value` cap |
| `max_cash_per_trade_pct` | Tier-dependent | `quote_amount / cash_available` cap |
| `never_trade_pairs` | Runtime list | Pair blacklist (in-memory + config) |
| Emergency stop | Tier-aware | Floor based on high-water mark |

### Daily Counter Persistence

- Counters are **seeded at startup** from the database (today's trades, UTC midnight cutoff).
- Counters are **exchange-scoped**: separate per exchange to prevent cross-domain bleed.
- If seeding fails, **all new buys are blocked** until successful (fail-safe, not fail-open).
- Only BUY trades increment `_daily_spend`; both BUY and SELL increment the trade count.

### Tier-Aware Emergency Stop

- **MICRO/SMALL accounts**: Dynamic floor = 85% of high-water mark (not a fixed dollar amount that would trivially halt a $10 account).
- **REGULAR/WHALE accounts**: Fixed floor from config.

### Thread Safety

All counter reads/writes are protected by `RLock`. The `check_trade()` method holds the lock for the entire evaluation to prevent race conditions on concurrent approvals.

### Runtime Updates

- `update_param(param, value)`: In-memory only (lost on restart).
- `add_never_trade_pair(pair)`: Runtime blacklist addition.
- Persistence requires writing changes back to `settings.yaml`.

## Consequences

**Benefits:**
- Hard safety floor that no LLM output can bypass.
- Sells always succeed, preventing capital lockup.
- Counter persistence across restarts eliminates "fresh counter" exploits.
- Exchange-scoped counters enforce domain separation (ADR-003).

**Risks:**
- Rules are in-memory with optional persistence; a crash between `update_param()` and config write loses the change.
- Overly conservative rules can prevent profitable trades (intentional trade-off: safety > profit).

**Follow-on:**
- Dashboard settings UI (ADR-009) allows authorized users to adjust parameters at runtime.
- Pre-commit guards (ADR-013) test that rules enforcement code passes invariant checks.
