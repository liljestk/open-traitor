# Code Review Fix Tracker

Full code review completed 2026-02-21. Findings organized by priority.

---

## CRITICAL

- [ ] **C1 — Dashboard auth bypass via `Sec-Fetch-Site`** — `src/dashboard/server.py:245`
  Remove `Sec-Fetch-Site` trust. Require API key header for all `/api/*` and `/ws/*` requests. For browser SPA, store key in cookie or have frontend include it in `X-API-Key`.

- [ ] **C2 — Unauthenticated Redis dashboard commands** — `src/core/orchestrator.py:884`
  Add HMAC signature or signed-token validation on `dashboard:commands_queue` messages. Reject unsigned payloads.

- [ ] **C3 — Fiat-routed swap partial failure, no recovery** — `src/core/portfolio_rotator.py:788`
  Add sell-reversal recovery for fiat-routed swaps (parity with bridged swap error handling). Notify Telegram on partial failure.

- [ ] **C4 — Credential injection via unprotected API endpoint** — `src/dashboard/server.py:1257`
  Add secondary confirmation (re-prompt or Telegram approval) before `PUT /api/settings/api-keys`. Use atomic file writes for `.env`.

- [ ] **C5 — LLM prompt injection via `ACTION:` text parsing** — `src/telegram_bot/chat_handler.py:1432`
  Restrict which tools the text-fallback `ACTION:` parser can invoke, or remove fallback entirely. Sanitize LLM output before parsing.

---

## HIGH — Security

- [ ] **H1 — Redis bound to 0.0.0.0** — `docker-compose.yml:59`
  Change `6380:6379` → `127.0.0.1:6380:6379`.

- [ ] **H2 — Dashboard bound to 0.0.0.0** — `docker-compose.yml:142`
  Change `8090:8090` → `127.0.0.1:8090:8090`.

- [ ] **H3 — Health/metrics bound to 0.0.0.0** — `docker-compose.yml:103`
  Change `8080:8080` → `127.0.0.1:8080:8080`.

- [ ] **H4 — API key in WebSocket query string** — `src/dashboard/server.py:227`
  Remove `?api_key=` fallback. Use a short-lived token exchange or first-message auth instead.

- [ ] **H5 — Settings mutation with no MFA/audit** — `src/dashboard/server.py:1092`
  Add confirmation step and audit trail for `PUT /api/settings`.

- [ ] **H6 — No bounds on rule parameter updates** — `src/core/rules.py:385`
  Add min/max validation in `update_param` for safety-critical fields (`emergency_stop_portfolio`, `max_daily_loss`, etc.).

- [ ] **H7 — LLM agent can modify blacklist unrestricted** — `src/utils/settings_manager.py:302`
  Add guardrails on autonomous `never_trade_pairs`/`only_trade_pairs` modifications (e.g., LLM can only add, not clear).

## HIGH — Concurrency

- [ ] **H8 — `self.pairs` modified without lock** — `src/core/orchestrator.py:636`
  Protect `self.pairs` reads/writes with a lock, or use copy-on-write pattern.

- [ ] **H9 — `_handle_tighten_stop` bypasses TrailingStopManager lock** — `src/core/orchestrator.py:939`
  Acquire `TrailingStopManager._lock` before modifying `stop.stop_price`, or add a `tighten_stop()` method to the manager.

- [ ] **H10 — Paper mode `_paper_balance` no locking** — `src/core/coinbase_client.py:85`
  Add `threading.Lock` around all `_paper_balance` reads and mutations.

- [ ] **H11 — `_product_cache` is class-level mutable** — `src/core/coinbase_client.py:510`
  Move to instance level. Add a lock around `_refresh_product_cache`.

- [ ] **H12 — Rotator `_last_swap_times`/`pending_swaps` unlocked** — `src/core/portfolio_rotator.py:158`
  Add a lock protecting both dicts.

- [ ] **H13 — `TrailingStop` internal state not thread-safe** — `src/core/trailing_stop.py:87`
  Add per-stop lock or ensure all mutations go through the manager's lock.

- [ ] **H14 — `reload_providers()` hot-swap race** — `src/core/llm_client.py:585`
  Add threading lock around `reload_providers()` and `chat()` provider iteration.

- [ ] **H15 — WS `update_subscriptions` doesn't hold lock** — `src/core/ws_feed.py:241`
  Acquire `self._lock` before reading `self.product_ids` and calling `ws.send()`.

- [ ] **H16 — Settings TOCTOU (load then save)** — `src/utils/settings_manager.py:621`
  Hold lock across the entire load-modify-save cycle in `update_section`.

## HIGH — Bugs / State

- [ ] **H17 — Wrong attribute in `_handle_pause_pair`** — `src/core/orchestrator.py:959`
  Fix `self.rules._never_trade` → `self.rules.never_trade_pairs`.

- [ ] **H18 — `auto_approve_up_to_usd` AttributeError** — `src/core/high_stakes.py:260`
  Fix `self.hs_config.auto_approve_up_to_usd` → `self.hs_config.auto_approve_up_to`.

- [ ] **H19 — Rotation swaps bypass daily counters** — `src/core/portfolio_rotator.py`
  Call `rules.record_trade()` after each successful rotation swap leg.

- [ ] **H20 — Positions/cash can go negative** — `src/core/state.py:289`
  Add guard in `add_trade`: reject sells exceeding position size, reject buys exceeding cash.

- [ ] **H21 — `time.sleep()` blocks async event loop** — `src/agents/executor.py:205`
  Replace with `await asyncio.sleep()` in an async wrapper.

---

## MEDIUM

- [ ] **M1 — CORS defaults to `["*"]`** — `src/dashboard/server.py:192`
- [ ] **M2 — Executive summary reveals DB profile names** — `src/dashboard/server.py:672`
- [ ] **M3 — Raw exceptions in order results** — `src/core/coinbase_client.py:810`
- [ ] **M4 — Exception class name leaked to Telegram** — `src/core/orchestrator.py:692`
- [ ] **M5 — Prompt injection regex bypassed via Unicode** — `src/utils/security.py:30`
- [ ] **M6 — `setattr` on rules from YAML field names** — `src/utils/settings_manager.py:806`
- [ ] **M7 — `sync_live_holdings` overwrites cash unconditionally** — `src/core/state.py:124`
- [ ] **M8 — `seed_daily_counters` hardcoded DB path** — `src/core/rules.py:83`
- [ ] **M9 — `_FIAT_RATE_CACHE` no thread sync** — `src/core/coinbase_client.py:40`
- [ ] **M10 — Emergency replan cooldown race** — `src/core/managers/event_manager.py:118`
- [ ] **M11 — `_partial_sell` reads trade without lock** — `src/agents/executor.py:282`
- [ ] **M12 — Executor accesses `state._lock` directly** — `src/agents/executor.py:469`
- [ ] **M13 — No CSRF protection on mutations** — `dashboard/frontend/src/api.ts:14`
- [ ] **M14 — WebSocket sends no auth token** — `dashboard/frontend/src/api.ts:565`
- [ ] **M15 — LLM provider config disclosure** — `src/dashboard/server.py:1186`
- [ ] **M16 — TOCTOU in `get_status`** — `src/core/high_stakes.py:222`
- [ ] **M17 — Simulated trade PnL always computed as long** — `src/dashboard/server.py:600`
- [ ] **M18 — Fee rates hardcoded to Coinbase** — `src/core/fee_manager.py:37`
- [ ] **M19 — RouteFinder crypto-specific** — `src/core/route_finder.py:91`

---

## LOW

- [ ] **L1 — Masked secrets at INFO level** — `src/utils/security.py:99`
- [ ] **L2 — Unbounded `_unauthorized_attempts` dict** — `src/telegram_bot/bot.py:149`
- [ ] **L3 — `verify_chain` reads without lock** — `src/utils/audit.py:121`
- [ ] **L4 — Audit log not fsync'd** — `src/utils/audit.py:105`
- [ ] **L5 — `_get_last_hash` O(n) file read** — `src/utils/audit.py:55`
- [ ] **L6 — Thread-local SQLite connections never closed** — `src/utils/stats.py:45`
- [ ] **L7 — `to_summary` not atomic snapshot** — `src/core/state.py:429`
- [ ] **L8 — `remaining_quantity` can go negative** — `src/core/trailing_stop.py:122`
- [ ] **L9 — Paper buy fee float rounding** — `src/core/coinbase_client.py:1028`
- [ ] **L10 — `_running` flag no memory barrier** — `src/telegram_bot/bot.py:120`
- [ ] **L11 — Hardcoded profile→currency mapping** — `dashboard/frontend/src/store.ts:8`
- [ ] **L12 — Hardcoded Temporal Postgres password** — `docker-compose.yml:413`
- [ ] **L13 — Redis subscriber no reconnect** — `src/dashboard/server.py:870`

---

## Cross-Asset Interference (Share vs Crypto)

- [ ] **X1 — Single `cash_balance` for all exchanges** — `src/core/state.py`
  Introduce per-exchange cash balances to prevent cross-brokerage interference (EUR crypto vs SEK shares).

- [ ] **X2 — Daily counters global, not per-exchange** — `src/core/rules.py`
  Split `_daily_spend` and `_daily_trade_count` per exchange so one doesn't block the other.

- [ ] **X3 — Fee manager crypto-only** — `src/core/fee_manager.py:37`
  Add asset-class dispatch for fee calculation.

- [ ] **X4 — RouteFinder crypto-only** — `src/core/route_finder.py`
  Add stock ticker awareness or skip routing for non-crypto pairs.

- [ ] **X5 — Simulated trades assume `BASE-QUOTE`** — `src/dashboard/server.py:537`
  Handle stock ticker formats (e.g., `AAPL.ST`).

---

## Summary

| Severity | Count |
|----------|-------|
| Critical | 5 |
| High | 21 |
| Medium | 19 |
| Low | 13 |
| Cross-asset | 5 |
| **Total** | **63** |
