# ADR-011: Tier-Based Portfolio Scaling

**Status:** Accepted

## Context

A €10 account and a €100K account have fundamentally different risk profiles. A 5% position on €10 is €0.50 (below minimum trade size); the same 5% on €100K is €5,000 (reasonable). Static percentage-based limits either prevent small accounts from trading at all or allow large accounts to take outsized positions.

## Decision

Implement a **5-tier portfolio scaler** that dynamically adjusts position sizing, risk limits, and fee thresholds based on current account value. Configuration values in `coinbase.yaml` represent the MEDIUM tier baseline; other tiers scale from there.

### Tier Brackets

| Tier | Portfolio Size | max_position_pct | max_cash/trade | max_open_pos | take_profit | stop_loss | min_gain_after_fees |
|------|----------------|------------------|----------------|--------------|-------------|-----------|---------------------|
| **MICRO** | < €50 | 40% | 50% | 2 | 5% | 3% | Tier-adjusted |
| **SMALL** | €50–€500 | 25% | 35% | 3 | 7% | 5% | Tier-adjusted |
| **MEDIUM** | €500–€5K | 15% | 25% | 5 | 6% | 4.5% | Config baseline |
| **LARGE** | €5K–€50K | 8% | 25% | 8 | 6% | 4.5% | Config baseline |
| **WHALE** | > €50K | 3% | 15% | 10 | 5% | 4% | Tier-adjusted |

### Design Philosophy

- **MEDIUM is the reference tier** — config values in `coinbase.yaml` and `ibkr.yaml` are MEDIUM defaults.
- **Smaller accounts get loosened constraints**: wider TP/SL, higher concentration allowed. A micro account needs more aggressive leverage to make trading viable above fee thresholds.
- **Larger accounts get tightened constraints**: lower position risk, broader diversification, tighter concentration limits. Risk management scales with capital.
- **Never increase risk for large accounts**: WHALE has the strictest `max_position_pct` (3%) and broadest diversification requirement.

### Tier Selection

```python
def update(self, portfolio_value: float) -> Tier:
    with self._lock:
        old_tier = self._tier
        self._portfolio_value = portfolio_value
        for upper, tier in _TIERS:
            if portfolio_value < upper:
                self._tier = tier
                break
        else:
            self._tier = _TIERS[-1][1]  # WHALE
        
        if self._tier.name != old_tier.name:
            logger.info(f"📊 Tier changed: {old_tier.name} → {self._tier.name}")
        return self._tier
```

### Thread Safety

Tier updates are protected by `RLock`. The `update()` method is called once per cycle with the current portfolio value. Tier transitions are logged for transparency.

### Integration Points

- **AbsoluteRules** (ADR-002): Emergency stop floor is tier-aware (MICRO/SMALL use percentage of high-water mark, not fixed dollar amounts).
- **RiskManager** (ADR-001): Position sizing caps come from the active tier.
- **FeeManager** (ADR-008): `min_gain_after_fees_pct` threshold adjusts per tier.

### Tier Data Structure

```python
@dataclass(frozen=True)
class Tier:
    name: str
    max_position_pct: float
    max_cash_per_trade_pct: float
    max_portfolio_risk_pct: float
    max_active_pairs: int
    max_open_positions: int
    min_gain_after_fees_pct: float
    take_profit_pct: float
    stop_loss_pct: float
```

## Consequences

**Benefits:**
- Small accounts can actually trade (wider limits above fee floors).
- Large accounts are protected from oversized positions.
- Automatic tier transitions as the portfolio grows or shrinks — no manual config changes needed.
- Single source of truth for all position/risk limits per tier.

**Risks:**
- Tier boundaries create discontinuities (e.g., €499 → €500 shifts from MICRO to SMALL). Mitigated by gradual parameter differences between adjacent tiers.
- Portfolio value fluctuations near tier boundaries could cause frequent tier switching. Mitigated by logging and the fact that adjacent tiers have similar parameters.

**Trade-offs:**
- Fixed tier brackets may not suit all users. Custom tiers are not currently supported, keeping the system simple.
- MICRO tier's aggressive limits (40% max position) may seem risky, but it's necessary for sub-€50 accounts to place trades above minimum size and fee thresholds.
