# Code Review Fix Tracker

Full code review completed 2026-02-21. Re-reviewed 2026-02-21 (second pass).
Findings organized by priority.

---

## CRITICAL (NEW — found in second review)

- [ ] **C6 — Trailing stop removed even when sell fails** — `src/core/orchestrator.py:528`
  `remove_stop(pair)` is called *before* checking `close_result.get("success")`. If the sell order fails, the stop is gone and will never retry. Position left unprotected. Log message says "will retry next cycle" but that's false.

- [ ] **C7 — `fee_pct` undefined → `NameError` in `estimate_swap_fees`** — `src/core/fee_manager.py:206`
  `FeeEstimate(sell_fee_pct=fee_pct, buy_fee_pct=fee_pct, ...)` references `fee_pct` which is never assigned in this method. **All swap/rotation fee estimation is broken** — any call to `estimate_swap_fees` or `is_trade_worthwhile(..., is_swap=True)` crashes.

- [ ] **C8 — `_SETTINGS_PATH` undefined → `NameError` on import** — `src/utils/settings_manager.py:1084`
  `def get_llm_providers(path: str = _SETTINGS_PATH)` — `_SETTINGS_PATH` is never defined in this module. Python evaluates default args at definition time, so this will raise `NameError` when the module is imported. Imported by `server.py`.

- [ ] **C9 — Telegram tool-calling path has NO action allowlist** — `src/telegram_bot/chat_handler.py:1298-1370`
  C5 only secured the text-fallback `ACTION:` parser. The **preferred** native tool-calling path (`_smart_response_with_tools`) executes *any* registered handler — `emergency_stop`, `enable_highstakes`, `update_rule`, `pause_trading`, etc. Prompt injection via the tool-calling path completely bypasses C5's allowlist.

- [ ] **C10 — `universe_scanner` calls non-existent `llm.generate()`** — `src/core/managers/universe_scanner.py:265`
  `LLMClient` has no `generate()` method (only `chat`, `chat_json`, `chat_with_tools` — all async). `run_llm_screener` is synchronous. **Every screener invocation crashes with `AttributeError`.**

---

## CRITICAL (previously fixed — confirmed still fixed)

- [x] **C1 — Dashboard auth bypass via `Sec-Fetch-Site`** — `src/dashboard/server.py`
  ✅ Removed. `/api/*` now requires explicit API key.

- [x] **C2 — Unauthenticated Redis dashboard commands** — `src/core/orchestrator.py`
  ✅ HMAC-signed payloads with timestamp validation.

- [x] **C3 — Fiat-routed swap partial failure, no recovery** — `src/core/portfolio_rotator.py`
  ✅ Buy-leg failure reversal with audit logging.

- [x] **C4 — Credential injection via unprotected API endpoint** — `src/dashboard/server.py`
  ✅ Two-step confirmation token flow.

- [x] **C5 — LLM prompt injection via `ACTION:` text parsing** — `src/telegram_bot/chat_handler.py`
  ✅ Text-fallback path secured. **But see C9 — tool-calling path is NOT secured.**

---

## HIGH — New findings

- [ ] **H22 — `asyncio.gather` without `return_exceptions=True`** — `src/core/orchestrator.py:500,794`
  One pair pipeline failure aborts ALL other pair pipelines in the same cycle. Both the main pipeline gather and the early-trigger gather have this problem.

- [ ] **H23 — Ollama-down path spins with no sleep** — `src/core/orchestrator.py:471`
  When `self.llm.is_available()` returns False, `continue` skips the entire cycle body including the inter-cycle sleep. The loop will hammer the Ollama check at full speed, burning CPU.

- [ ] **H24 — Telegram `_add_pair`/`_remove_pair` bypass `_pairs_lock`** — `src/core/managers/telegram_manager.py:333,344`
  Direct `orch.pairs.append()`/`.remove()` without acquiring `_pairs_lock`. H8 added the lock and a copy-on-write contract, but Telegram callbacks don't honour it. Data race.

- [ ] **H25 — `chat_with_tools` iterates providers without lock** — `src/core/llm_client.py:441`
  H14 secured `chat()` with provider-chain lock snapshots, but `chat_with_tools` iterates `self._providers` directly without snapshotting under `_providers_lock`. Same race condition H14 was meant to fix.

- [ ] **H26 — Risk manager crashes on sell orders** — `src/agents/risk_manager.py:330`
  `f"SL: {stop_loss:,.2f} | TP: {take_profit:,.2f}"` — for sell orders, `stop_loss`/`take_profit` are `None` (the ensure-SL/TP blocks only run `if action == "buy"`). Formatting `None:,.2f` raises `TypeError`. Every sell trade crashes the risk manager.

- [ ] **H27 — Executor orphaned exchange orders on `add_trade` ValueError** — `src/agents/executor.py:170`
  H20 added `ValueError` on insufficient balance. The broad `except Exception` at L126 catches it, but the exchange order is already placed/filled. The trade becomes untracked — divergent state. For limit orders (L170), the resting order is orphaned forever.

- [ ] **H28 — Dashboard default-open when `DASHBOARD_API_KEY` unset** — `src/dashboard/server.py:228`
  When the env var is unset (the common default), ALL REST endpoints including settings mutation, api-key updates, and trade commands are fully open with zero authentication. The confirmation flow is a speed bump, not security.

---

## HIGH — Security (previously fixed — confirmed)

- [x] **H1** — Redis bound to `127.0.0.1` ✅
- [x] **H2** — Dashboard bound to `127.0.0.1` ✅
- [x] **H3** — Health bound to `127.0.0.1` ✅
- [x] **H4** — WebSocket subprotocol auth ✅
- [x] **H5** — Settings mutation confirmation flow ✅
- [x] **H6** — Rule parameter bounds ✅
- [x] **H7** — Autonomous append-only lists ✅

## HIGH — Concurrency (previously fixed — confirmed)

- [x] **H8** — `_pairs_lock` with copy-on-write ✅ (but see H24 — Telegram ignores it)
- [x] **H9** — `tighten_to_breakeven()` routed through manager ✅
- [x] **H10** — Paper balance locking ✅
- [x] **H11** — Product cache instance-level with lock ✅
- [x] **H12** — Rotator `_last_swap_times` with `_state_lock` ✅
- [x] **H13** — Dict snapshots from `TrailingStopManager` ✅
- [x] **H14** — Provider-chain lock for `chat()` ✅ (but see H25 — `chat_with_tools` missed)
- [x] **H15** — WS `update_subscriptions` snapshots under lock ✅
- [x] **H16** — Settings `update_section` under `_lock` ✅

## HIGH — Bugs / State (previously fixed — confirmed)

- [x] **H17** — Wrong attribute in `_handle_pause_pair` ✅
- [x] **H18** — `auto_approve_up_to` config ✅
- [x] **H19** — Rotation swaps record daily counters ✅
- [x] **H20** — Positions/cash cannot go negative ✅ (but see H27 — callers not ready)
- [x] **H21** — `_verify_fill` async with `await asyncio.sleep()` ✅

---

## MEDIUM — Open

- [x] **M1 — CORS defaults to `["*"]`** — ✅ Fixed. Defaults to localhost origins. `allow_methods`/`allow_headers` still wildcard (low risk).
- [ ] **M2 — Executive summary reveals DB profile names** — `src/dashboard/server.py:672`
- [ ] **M3 — Raw exceptions in order results** — `src/core/coinbase_client.py:810`
- [ ] **M4 — Exception class name leaked to Telegram** — `src/core/orchestrator.py:692`
- [x] **M5 — Prompt injection regex bypassed via Unicode** — ✅ Fixed (NFKC + zero-width strip). Cyrillic homoglyphs still bypass — see M22.
- [x] **M6 — `setattr` on rules from YAML field names** — ✅ Fixed. `_RULES_SETTABLE_ATTRS` allowlist.
- [ ] **M7 — `sync_live_holdings` overwrites cash unconditionally** — `src/core/state.py:124`
- [ ] **M8 — `seed_daily_counters` hardcoded DB path** — `src/core/rules.py:83`
- [x] **M9 — `_FIAT_RATE_CACHE` no thread sync** — ✅ Fixed. `_FIAT_RATE_LOCK` added.
- [ ] **M10 — Emergency replan cooldown race** — `src/core/managers/event_manager.py:106`
  Lock scope too narrow — covers only cooldown check, not the DB write + cache invalidation. If DB write fails, cooldown is set but no plan is saved.
- [ ] **M11 — `_partial_sell` reads trade without lock** — `src/agents/executor.py:271`
- [ ] **M12 — Executor accesses `state._lock` directly** — `src/agents/executor.py:295,553`
- [ ] **M13 — No CSRF protection on mutations** — `dashboard/frontend/src/api.ts`
- [x] **M14 — WebSocket sends no auth token** — ✅ Fixed via subprotocol auth.
- [x] **M15 — LLM provider config disclosure** — ✅ Fixed. `_SAFE_PROVIDER_FIELDS` allowlist.
  ⚠️ `provider_status()` live data is still passed unfiltered — may leak base URLs.
- [x] **M16 — TOCTOU in `get_status`** — ✅ Fixed. Full method under `self._lock`.
- [x] **M17 — Simulated trade PnL always computed as long** — ✅ Fixed. Direction-aware.
- [ ] **M18 — Fee rates hardcoded to Coinbase** — `src/core/fee_manager.py:37`
- [ ] **M19 — RouteFinder crypto-specific** — `src/core/route_finder.py:91`
- [ ] **M20 — Signed dashboard commands no replay protection** — `src/core/orchestrator.py`
- [ ] **M21 — `ValueError` from `add_trade` may crash orchestrator** — `src/core/state.py:280`
  Verified: broad `except Exception` in executor catches it, but recovery is incorrect (see H27).

## MEDIUM — New findings

- [ ] **M22 — Cyrillic/Greek homoglyphs bypass prompt injection regex** — `src/utils/security.py:33`
  NFKC does not map cross-script lookalikes (Cyrillic 'а' ≠ Latin 'a'). Attacker can write "ignоre previоus instructiоns" and bypass all patterns.

- [ ] **M23 — `all_tracked_pairs` never updated at runtime** — `src/core/orchestrator.py:162`
  Set once in `__init__` but never refreshed when `self.pairs` changes (via settings advisor, Telegram, or `_handle_pause_pair`).

- [ ] **M24 — `_pending_confirmations` dict has no lock** — `src/dashboard/server.py:75`
  Dashboard confirmation tokens are stored in a plain dict. FastAPI sync endpoints run in a threadpool — concurrent confirmation requests can race.

- [ ] **M25 — WS `update_subscriptions` only updates `ticker` channel** — `src/core/ws_feed.py:325`
  `_on_open` subscribes to both `ticker` and `market_trades`, but `update_subscriptions` only manages the `ticker` channel. Dynamic pair changes leave `market_trades` stale.

- [ ] **M26 — `apply_preset` TOCTOU** — `src/utils/settings_manager.py:808`
  Loads settings outside the lock, modifies in memory, then `save_settings` acquires lock internally. Concurrent `update_section` writes are silently overwritten.

- [ ] **M27 — Rotator `pending_swaps` not under `_state_lock`** — `src/core/portfolio_rotator.py:169`
  H12 added `_state_lock` for `_last_swap_times` but missed `pending_swaps`.

- [ ] **M28 — Fiat-routed reversal double-counts daily spend** — `src/core/portfolio_rotator.py:760`
  Original sell leg and reversal buy each call `_record_rotation_leg`, recording 2x spend for a net-zero position change.

- [ ] **M29 — `high_stakes.get_effective_limits` can LOWER `require_approval_above`** — `src/core/high_stakes.py:249`
  Unconditionally replaces base `require_approval_above` with `auto_approve_up_to`. If base is $1000 and HS is $500, high-stakes mode *reduces* the ceiling.

- [ ] **M30 — `holdings_manager` `dict(accounts)` crashes on list input** — `src/core/managers/holdings_manager.py:83`
  If `get_accounts()` returns `list[dict]` (per ABC), `dict(accounts)` raises. Only works with Coinbase SDK response objects.

- [ ] **M31 — `telegram_manager` uses `asyncio.run()` in async context** — `src/core/managers/telegram_manager.py:774,852`
  `cmd_approve_trade` and `cmd_rotate` call `asyncio.run()`. If called from within the Telegram bot's async handler, raises `RuntimeError: This event loop is already running`.

- [ ] **M32 — Market analyst format crash on missing indicators** — `src/agents/market_analyst.py:148`
  `f"{indicators.get('rsi', 'N/A'):.1f}"` — if RSI is absent, returns string `'N/A'` which fails `:.1f` format. `ValueError` or `TypeError`.

- [ ] **M33 — Strategist crash on string tasks** — `src/agents/strategist.py:166`
  `t.get('description', t)` — if `active_tasks` contains plain strings, `.get()` on a `str` raises `AttributeError`.

---

## LOW — Open

- [ ] **L1 — Masked secrets at INFO level** — `src/utils/security.py:99`
- [ ] **L2 — Unbounded `_unauthorized_attempts` dict** — `src/telegram_bot/bot.py:149`
- [x] **L3 — `verify_chain` reads without lock** — ✅ Fixed. Lock held during full read.
- [x] **L4 — Audit log not fsync'd** — ✅ Fixed. `f.flush()` + `os.fsync()`.
- [x] **L5 — `_get_last_hash` O(n) file read** — ✅ Fixed. Reads last 4096 bytes.
- [x] **L6 — Thread-local SQLite connections never closed** — ✅ Fixed. `close()` + tracking list.
- [ ] **L7 — `to_summary` not atomic snapshot** — `src/core/state.py:429`
- [x] **L8 — `remaining_quantity` can go negative** — ✅ Fixed. `max(0.0, ...)` guard.
- [ ] **L9 — Paper buy fee float rounding** — `src/core/coinbase_client.py:1028`
- [ ] **L10 — `_running` flag no memory barrier** — `src/telegram_bot/bot.py:120`
- [ ] **L11 — Hardcoded profile→currency mapping** — `dashboard/frontend/src/store.ts:8`
- [ ] **L12 — Hardcoded Temporal Postgres password** — `docker-compose.yml:413`
- [ ] **L13 — Redis subscriber no reconnect** — `src/dashboard/server.py:870`
- [ ] **L14 — Paper order append outside balance lock** — `src/core/coinbase_client.py:1060`

## LOW — New findings

- [ ] **L15 — Duplicate imports in orchestrator** — `src/core/orchestrator.py:59,26`
  `check_component_health, update_health` imported twice; `coinbase_client` symbols imported but unused.
- [ ] **L16 — `elapsed` counter drifts** — `src/core/orchestrator.py:788`
  Inter-cycle sleep increments `elapsed` by 10.0 regardless of actual elapsed time. Pipelines/stop-checks aren't counted, so cycles can over-sleep.
- [ ] **L17 — `_get_sequence` at init is still O(n)** — `src/utils/audit.py:87`
  `sum(1 for _ in f)` reads entire audit log. Slow startup on large logs.
- [ ] **L18 — `StatsDB.close()` never called automatically** — `src/utils/stats.py`
  No `__del__`, `__exit__`, or `atexit`. Thread pool connections leak.
- [ ] **L19 — Journal JSONL+CSV writes in separate lock scopes** — `src/utils/journal.py:95`
  Crash between them leaves inconsistent state.
- [ ] **L20 — Bridge reversals don't call `_record_rotation_leg`** — `src/core/portfolio_rotator.py:700`
  Unlike fiat reversals, bridge reversals aren't tracked in AbsoluteRules counters. Inconsistent.
- [ ] **L21 — `_schema_summary` cached forever** — `src/agents/settings_advisor.py:127`
  If field guards change at runtime, stale cache is served.
- [ ] **L22 — WS subprotocol not echoed** — `src/dashboard/server.py:1030`
  Server calls `websocket.accept()` without `subprotocol=` after auth via subprotocol. Browsers may reject per RFC 6455.

---

## Do not fix yet — Cross-Asset Interference (Share vs Crypto)

- [ ] **X1 — Single `cash_balance` for all exchanges** — `src/core/state.py`
- [ ] **X2 — Daily counters global, not per-exchange** — `src/core/rules.py`
- [ ] **X3 — Fee manager crypto-only** — `src/core/fee_manager.py:37`
- [ ] **X4 — RouteFinder crypto-only** — `src/core/route_finder.py`
- [ ] **X5 — Simulated trades assume `BASE-QUOTE`** — `src/dashboard/server.py:537`

---

## Summary

| Severity | Total | Fixed | Open |
|----------|-------|-------|------|
| Critical | 10 | 5 | **5** |
| High | 28 | 21 | **7** |
| Medium | 33 | 9 | **24** |
| Low | 22 | 5 | **17** |
| Cross-asset | 5 | 0 | 5 |
| **Total** | **98** | **40** | **58** |

### Priority order for open items

**Must fix (crashes / data corruption / security bypass):**
C6, C7, C8, C9, C10, H22, H23, H26, H27

**Should fix (security hardening / concurrency):**
H24, H25, H28, M20, M22, M24

**Fix when convenient:**
Everything else.

## Progress Log

- 2026-02-21 Batch 1 complete: C1, H10, H17, H18.
- 2026-02-21 Batch 2 complete: H1, H2, H3, H21.
- 2026-02-21 Batch 3 complete: H11, H16.
- 2026-02-21 Batch 4 complete: H20.
- 2026-02-21 Batch 5 complete: H9.
- 2026-02-21 Batch 6 complete: H14.
- 2026-02-21 Batch 7 complete: H12, H15, H19.
- 2026-02-21 Batch 8 complete: C2, H6.
- 2026-02-21 Batch 9 complete: C3.
- 2026-02-21 Post-fix review: H4 regressed (browser WS broken), added M20, M21, L14.
- 2026-02-21 Second full review: M1, M5, M6, M9, M14, M15, M16, M17, L3, L4, L5, L6, L8 confirmed fixed. Added C6–C10, H22–H28, M22–M33, L15–L22 (32 new findings).
