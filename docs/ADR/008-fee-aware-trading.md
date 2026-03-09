# ADR-008: Fee-Aware Trading with Safety Margins

**Status:** Accepted

## Context

Exchange fees (maker/taker, flat commissions, per-share costs) can silently erode profits. A 0.5% expected gain on a trade with 0.6% round-trip fees is a net loss. Without fee awareness, the system would execute trades that appear profitable but consistently lose money — "death by a thousand cuts."

## Decision

Implement a **pluggable fee model** system with a configurable **safety margin multiplier** that ensures trades are only executed when expected gains meaningfully exceed total fees.

### Fee Models

Three pluggable models cover the supported exchanges:

| Model | Exchange | Formula | Example |
|-------|----------|---------|---------|
| `CryptoPercentageFeeModel` | Coinbase | `fee = quote × pct` (taker: 0.6%, maker: 0.4%) | €1000 buy → €6 taker fee |
| `EquityFlatPlusPctFeeModel` | Nordic brokers | `fee = max(flat_min, quote × pct)` (€39 + 0.15%) | €10K buy → €39 + €15 = €54 |
| `EquityPerShareFeeModel` | IBKR | `fee = max($0.35, shares × $0.0035)` | 1000 shares → $3.50 |

### Safety Margin Logic

```python
fee_safety_margin = 1.5  # default: require 1.5× fees as expected gain

# For a round-trip (buy + sell):
total_fee = buy_fee + sell_fee
breakeven = total_fee * fee_safety_margin

# Trade executes only if:
expected_gain > breakeven AND gain_after_fees > min_gain_after_fees_pct
```

**Example:**
- Swap €1,000: sell fee = €3, buy fee = €3, total = €6
- Breakeven with 1.5× margin: €6 × 1.5 = €9 gain required
- Expected gain 1.5% (€15) → **EXECUTE** (€15 > €9)
- Expected gain 0.5% (€5) → **REJECT** (€5 < €9, net loss of €4)

### Dynamic Minimum Trade Size

```python
min_from_floor = 1.0    # absolute minimum (EUR)
min_from_pct = portfolio_value × 0.01  # 1% of portfolio

min_trade = max(min_from_floor, min_from_pct)
```

- €6.80 account: `max(1.0, 0.068)` = €1.00
- €50K account: `max(1.0, 500)` = €500.00

This prevents dust trades on small accounts and ensures meaningful position sizes on large accounts.

### Integration with Pipeline

The Strategist agent receives fee context when formulating proposals. Fee checks happen before the proposal reaches AbsoluteRules, preventing fee-losing trades from consuming daily trade/spend quotas.

## Consequences

**Benefits:**
- Eliminates trades that appear profitable but lose money after fees.
- Safety margin provides buffer for slippage and price movement during execution.
- Dynamic minimum trade scales with account size, preventing dust accumulation.
- Pluggable models support new exchanges without core logic changes.

**Risks:**
- Conservative margin (1.5×) rejects some marginally profitable trades. This is intentional — in high-frequency cycling, accumulating small losses is more dangerous than missing small gains.
- Fee percentages change when exchange volume tiers change; config must be updated manually.

**Trade-offs:**
- The system errs on the side of not trading. A 1.5× safety margin means a trade needs to be 50% more profitable than its fees to execute, which is deliberately conservative.
- Per-share fee models require share count estimation, which adds complexity for equity trading.
