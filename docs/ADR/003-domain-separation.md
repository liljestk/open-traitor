# ADR-003: Domain Separation (Crypto vs Equities)

**Status:** Accepted

## Context

OpenTraitor supports two fundamentally different asset classes — crypto (Coinbase) and equities (IBKR). These domains have different pricing models, fee structures, market hours, and regulatory considerations. Mixing data between them would corrupt ML models, produce nonsensical portfolio metrics, and violate trading logic assumptions.

## Decision

Enforce strict domain separation across **three layers**: database queries, Redis cache keys, and frontend cache invalidation.

### Layer 1: SQL Filtering

All SQL queries for trades, reasoning samples, portfolio snapshots, and daily summaries **must** filter by `exchange` when a profile is active:

```sql
-- Correct: filtered by exchange
SELECT * FROM trades WHERE exchange = 'coinbase' AND ts > ?

-- Wrong: returns mixed crypto + equity data
SELECT * FROM trades WHERE ts > ?
```

The `AbsoluteRules.seed_daily_counters()` method filters trades by `exchange` before summing daily spend/loss/count, ensuring crypto trading limits don't affect equity counters and vice versa.

### Layer 2: Redis Key Prefixing

All Redis keys are prefixed with the profile/exchange name:

```
coinbase:trailing_stops:state
ibkr:trailing_stops:state
coinbase:portfolio:latest
ibkr:portfolio:latest
```

The news aggregator checks `news:{profile}:latest` before falling back to `news:latest`.

### Layer 3: Frontend Cache (React Query)

Every `useQuery({ queryKey: [...] })` call in dashboard pages **must** include `profile` in its `queryKey` array:

```typescript
// Correct: cache is profile-scoped
const profile = useLiveStore((s) => s.profile);
useQuery({ queryKey: ["trades", profile], ... });

// Wrong: cached data leaks across profile switches
useQuery({ queryKey: ["trades"], ... });
```

**Exempt pages/keys** (truly profile-independent): `Settings.tsx`, `LLMProviders.tsx`, keys like `"settings"`, `"presets"`, `"auth-status"`, `"llm-providers"`.

### Enforcement

- A **static test** (`TestFrontendQueryKeysIncludeProfile`) scans all `.tsx` files in `dashboard/frontend/src/pages/` and verifies the `profile` inclusion rule.
- This test runs in the **pre-commit hook** (ADR-013), blocking any commit that violates domain separation.
- SQL and Redis rules are enforced via code review and integration tests.

## Consequences

**Benefits:**
- Impossible to accidentally display crypto P&L on the equity dashboard or vice versa.
- Redis cache is naturally isolated; switching profiles doesn't serve stale data.
- Static test catches frontend violations at commit time, not in production.

**Risks:**
- Runtime cost of `exchange` filtering in every DB query — negligible with proper indexes on `(exchange, ts)`.
- Developers must remember to include exchange/profile parameters. Static tests catch frontend issues; backend requires discipline.

**Trade-offs:**
- No "All Assets" combined view by design. Cross-domain analysis requires explicit, separate queries.
- Backward compatibility: when `exchange=""`, no filter is applied for legacy single-profile deployments.
