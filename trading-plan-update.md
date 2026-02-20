# Trading Engine Update: Dynamic Holdings Awareness

## Implementation Status

| File | Status | Notes |
|---|---|---|
| `config/settings.yaml` | ✅ DONE | `live_holdings_sync`, `holdings_refresh_seconds`, `holdings_dust_threshold` added |
| `src/core/state.py` | ✅ DONE | New fields, `sync_live_holdings()`, `holdings_summary` property, `import time` |
| `src/agents/strategist.py` | ✅ DONE | System prompt + `_build_strategy_prompt` updated, all `$`/`USD` hardcodes removed |
| `src/agents/risk_manager.py` | ✅ DONE | Sell quantity preservation added |
| `src/agents/market_analyst.py` | ✅ DONE | Dynamic `currency_symbol` in analysis prompt (was hardcoded `$`) |
| `src/core/orchestrator.py` | ✅ DONE | Startup sync, `_maybe_refresh_holdings()`, strategist + risk manager + market analyst context updated, stale strategic context invalidation |
| `src/planning/activities.py` | ✅ DONE | Dynamic `currency_symbol` in planning prompts, `portfolio_correction_applied` support |
| Syntax verification | ✅ DONE | All 6 Python files compile cleanly with `py_compile` |
| Integration testing | ⬜ TODO | Start bot in live mode and verify with Langfuse traces |

---

## Problem

The trading pipeline currently **does not know what you actually hold**. Here's what's broken:

| What should happen | What actually happens |
|---|---|
| Bot sees your 21 crypto holdings (AUCTION, SAND, XCN, etc.) | `positions = {}` — empty, bot thinks you hold nothing |
| Bot knows you have €0.55 EUR + €0.00 USDC + €0.00 EURC | Shows `Cash (USD): $0.00` — wrong currency, wrong amount |
| Risk manager uses real portfolio value (~€1.62) | Uses stale internal number that drifts from reality |
| Bot can sell pre-existing holdings when analysis warrants it | Can only sell positions it opened itself |
| Holdings update frequently during rapid trading | Only reconciles every ~20 minutes (and only for bot-tracked positions) |
| LLM sees correct currency symbols (€83,000 for BTC-EUR) | Shows `$83,000` even when trading EUR pairs |
| Strategic plans reflect actual portfolio | Plans generated assuming $10,000 USD phantom portfolio |

**The data already exists** — `_live_coinbase_snapshot()` fetches all real Coinbase holdings with proper EUR conversion. It's just only wired to Telegram chat responses, not the trading pipeline.

---

## Solution Overview

Thread the existing snapshot data through the pipeline with a configurable refresh rate (~60s default). No hardcoded currencies — works for any Coinbase account worldwide. On first start, invalidate stale strategic context generated under wrong assumptions.

---

## Detailed Changes

### File 1: `config/settings.yaml` — New config options ✅

```yaml
trading:
  live_holdings_sync: true                # Master toggle (false = legacy behavior)
  holdings_refresh_seconds: 60            # TTL for API calls
  holdings_dust_threshold: 0.01           # Minimum native-currency value in LLM prompts
```

### File 2: `src/core/state.py` — Store live holdings on shared state ✅

**New fields** added to `TradingState.__init__()`:
- `live_holdings`, `live_cash_balances`, `live_portfolio_value`
- `native_currency`, `currency_symbol`
- `_live_snapshot_ts`, `_initial_balance_synced`
- `positions_meta` — origin tracking (`"external"` vs `"bot"`)

**New method**: `sync_live_holdings(snapshot, dust_threshold)`
- Additive-only merge (only adds holdings NOT already tracked)
- Tags new positions with `origin="external"`
- Updates `cash_balance` from live fiat totals
- On first sync, corrects `initial_balance` to real portfolio value

**New property**: `holdings_summary`
- Formatted text for LLM prompts with dust filtering and 50-item cap

### File 3: `src/core/orchestrator.py` — Refresh + invalidate ✅

**On startup** (`__init__`):
- Initial `sync_live_holdings()` call
- **Inserts correction notice** into `strategic_context` table — immediately supersedes stale plans generated under wrong portfolio assumptions

**New method**: `_maybe_refresh_holdings()`
- TTL-cached, graceful degradation

**Pipeline wiring**:
- Market analyst gets `currency_symbol`
- Strategist gets `live_holdings_summary`, `currency_symbol`, `native_currency`
- Risk manager gets `live_portfolio_value`, live cash balances

### File 4: `src/agents/strategist.py` — LLM sees real holdings ✅

- System prompt instructs LLM it can sell pre-existing holdings
- All hardcoded `$`/`USD` → dynamic `{sym}`/`{native_currency}`
- New `{holdings_section}` in prompt with full Coinbase holdings

### File 5: `src/agents/market_analyst.py` — Currency-aware analysis ✅

- All `$` in the analysis prompt → dynamic `{sym}`
- Accepts `currency_symbol` from context

### File 6: `src/agents/risk_manager.py` — Preserve sell quantities ✅

- When `action == "sell"` and `quantity > 0`, preserves strategist-specified quantity

### File 7: `src/planning/activities.py` — Currency-aware planning ✅

- All `$` in `call_planning_llm` user_message → dynamic `{sym}`
- `fetch_portfolio_history` detects native currency from `settings.yaml`
- Returns `currency_symbol` and `native_currency` in review data
- Supports `portfolio_correction_applied` flag — tells LLM to weight recent data more heavily

---

## Stale Context Handling

### What was contaminated
The planning layer (Daily/Weekly/Monthly workflows) generated regime assessments, pair preferences, and risk posture based on:
- A phantom $10,000 USD initial balance
- Zero detected positions
- Wrong currency in all prompts

### How we fix it
1. **Immediate**: On startup, a correction notice is inserted into `strategic_context` with `horizon='daily'`. This becomes the latest daily plan and is immediately fed to agents on the next cycle. It tells them portfolio tracking was corrected and to re-evaluate.

2. **Next cron run**: When the next DailyPlanWorkflow fires (midnight UTC), it will use corrected currency symbols and include the `portfolio_correction_applied` note in the prompt to the planning LLM.

3. **Natural decay**: After 1-2 planning cycles, the correction notice naturally ages out as new plans (built on correct data) supersede it.

---

## Design Principles

- **Zero hardcoded currencies** — all agents, planners, and prompts use runtime-detected symbols
- **Additive-only merge** — prevents race conditions with in-flight trades
- **Graceful degradation** — API failures don't block trading
- **Paper mode unaffected** — all live-sync code is gated
- **Origin tracking** — `positions_meta` prevents inflated PnL stats
- **Strategic context correction** — stale plans invalidated on first boot

---

## Edge Cases & Guardrails

| Edge Case | Handling |
|---|---|
| Snapshot API fails | Keep stale data, log warning, retry next cycle |
| Bot buy not yet settled on Coinbase | Additive merge doesn't overwrite |
| Concurrent `/approve` + sync | `_lock` serializes access |
| Dust holdings bloating prompt | `holdings_dust_threshold` + hard €0.01 minimum |
| 300+ holdings | Hard cap at 50 in `holdings_summary` |
| Selling external holding | Tagged `origin="external"` in `positions_meta` |
| `initial_balance` = $10k vs actual €1.62 | First sync corrects it |
| Kill-switch needed | `live_holdings_sync: false` reverts instantly |
| Stale strategic plans | Correction notice inserted on startup |
| Planning worker uses wrong currency | Fixed — detects from settings.yaml |

---

## How to Verify

### Happy Path
1. Startup log: `📡 Live holdings synced: 21 assets, total=€1.62`
2. Startup log: `📋 Inserted portfolio correction notice into strategic context`
3. Strategist prompt (Langfuse): contains "ACTUAL COINBASE HOLDINGS" with EUR values
4. Market analyst prompt (Langfuse): prices show `€` not `$`
5. Refresh: ~once per minute, not every pipeline call
6. Risk limits: `portfolio_value` ≈ €1.62, not $0 or $10,000
7. Paper mode: unchanged, no API calls

### Error & Edge Cases
8. API failure: bot continues with stale data
9. Concurrent approve + sync: positions not corrupted
10. Dust filtering: sub-threshold holdings omitted from prompt
11. Return-% after sync: based on real portfolio, not $10k
12. Kill-switch: `live_holdings_sync: false` → legacy behavior
13. Planning LLM: next daily plan uses `€` and shows correction note
