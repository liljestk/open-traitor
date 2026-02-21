# Implementation Progress — Code Review Fixes + Dashboard Makeover

**Branch:** `feature/multi-asset-trading`  
**Started:** 2025-02-21

---

## Part A: Critical/High Bug Fixes (from Code Review)

### 1. Fix `latest_signals` AttributeError (CRITICAL)
- **File:** `src/agents/risk_manager.py` line 188, `src/core/managers/pipeline_manager.py`
- **Bug:** `self.state.latest_signals.get(pair)` — no `latest_signals` attribute on `TradingState`
- **Fix:** Pass ATR through pipeline context dict; read `context.get("atr")` in risk_manager
- **Status:** ✅ Complete

### 2. Fix `cmd_approve_trade` sync/async bug (CRITICAL)
- **File:** `src/core/managers/telegram_manager.py` line 787
- **Bug:** `orch.executor.execute()` is async but called synchronously — approved trades silently fail
- **Fix:** Wrapped in `asyncio.run()` 
- **Status:** ✅ Complete

### 3. Complete `ExchangeClient` ABC (HIGH)
- **File:** `src/core/exchange_client.py`
- **Bug:** ~8 methods called throughout codebase are missing from the ABC
- **Fix:** Added 9 methods with concrete default implementations (balance, detect_native_currency, adapt_pairs_to_account, discover_all_pairs, discover_all_pairs_detailed, get_portfolio_value, reconcile_positions, get_open_orders)
- **Status:** ✅ Complete

### 4. Fix orphaned `_currency_to_usd` in CoinbaseClient (CRITICAL)
- **File:** `src/core/coinbase_client.py` line 637+
- **Bug:** Method body orphaned inside `find_direct_pair` after `return None` — no `def` statement
- **Fix:** Added proper `def _currency_to_usd(self, currency, amount)` method definition
- **Status:** ✅ Complete

### 5. Fix `asyncio.run()` pattern in Orchestrator (HIGH)
- **File:** `src/core/orchestrator.py` — 4 occurrences
- **Bug:** Creates/destroys event loops per call from sync `run_forever()`
- **Fix:** Persistent `self._loop = asyncio.new_event_loop()` in __init__, replaced all 4 `asyncio.run()` with `self._loop.run_until_complete()`
- **Status:** ✅ Complete

### 6. Remove dead delegation stubs in Orchestrator (MEDIUM)
- **File:** `src/core/orchestrator.py` lines 773-981
- **Bug:** 31 pure delegation stubs (~210 lines, 21% of file)
- **Fix:** Updated 3 callers in pipeline_manager.py to use managers directly, then removed all stubs
- **Status:** ✅ Complete

---

## Part B: Dashboard Makeover (from dashboard-makeover.md)

### Phase 1: Core Polish — ✅ Complete
- Enhanced `StatCard` with outer glow, gradient background, better typography hierarchy
- Expanded `index.css` theme with full gray palette for consistent skeleton/component colors

### Phase 2: Empty States & Skeleton Loaders — ✅ Complete
- Created `Skeleton.tsx` — reusable skeleton components: `SkeletonTable`, `SkeletonStatCards`, `SkeletonCards`, `SkeletonLogEntries`, `SkeletonBlock`
- Created `EmptyState.tsx` — illustrated SVG empty states with 6 icon variants (chart, trades, logs, live, planning, search)
- Applied to all pages: CycleExplorer, TradesLog, SystemLogs, PlanningAudit, LiveMonitor

### Phase 3: Dashboard Features — ✅ Complete
- **Profile switcher** in sidebar — dropdown with Default/Crypto (EUR)/Nordnet Shares (SEK)
- **Dynamic currency** — `useCurrencyFormatter()` + `useCurrencySymbol()` hooks in store.ts replace all hardcoded € symbols
- **Page transitions** — `PageTransition` wrapper using framer-motion, applied to all 8 pages
- Updated `CycleExplorer`, `TradesLog` to use dynamic `fmtCurrency` from store instead of hardcoded EUR

### Deferred (Needs Backend Work)
- TradingView charts integration
- Equity curve / analytics hub  
- Risk heatmaps / VaR visualization
- HITL intervention buttons (Liquidate, Tighten Stop-Loss)
- Trade anatomy deep-dive view
- News aggregation view
- Watchlist / asset monitor
- Density controls (compact/comfortable toggle)

---

## Change Log

| Change | Files Modified |
|--------|----------------|
| Fix ATR context passing to risk_manager | `risk_manager.py`, `pipeline_manager.py` |
| Fix async approve_trade | `telegram_manager.py` |
| Expand ExchangeClient ABC | `exchange_client.py` |
| Fix orphaned _currency_to_usd | `coinbase_client.py` |
| Persistent event loop for orchestrator | `orchestrator.py` |
| Remove 31 dead delegation stubs | `orchestrator.py`, `pipeline_manager.py` |
| Create Skeleton components | `components/Skeleton.tsx` (new) |
| Create EmptyState component | `components/EmptyState.tsx` (new) |
| Enhance StatCard with glow/gradient | `components/StatCard.tsx` |
| Add profile switcher to sidebar | `components/Layout.tsx` |
| Add dynamic currency support | `store.ts` |
| Create PageTransition wrapper | `components/PageTransition.tsx` (new) |
| Apply skeletons/empty states/transitions | All 8 page components |
| Expand theme gray palette | `index.css` |
