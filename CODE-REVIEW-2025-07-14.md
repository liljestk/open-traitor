# Code Review — auto-traitor (14 July 2025)

Fresh comprehensive review of all key source files after completing all prior review fixes.

---

## CRITICAL

### C1 — `run_until_complete` from non-owning thread crashes Telegram approvals
**Files:** `src/core/managers/telegram_manager.py` ~L812, `src/core/managers/universe_scanner.py` ~L248

`orch._loop.run_until_complete(coro)` is called from threads that don't own the event loop. Since the orchestrator's loop is already running in another thread, this raises `RuntimeError: This event loop is already running`.

```python
# telegram_manager.py, cmd_approve_trade
result = orch._loop.run_until_complete(orch.executor.execute({"approved_trade": approved}))

# Also in cmd_rotate, and universe_scanner run_llm_screener
```

**Impact:** Telegram trade approvals, manual rotations, and LLM screener all crash at runtime. Users cannot approve pending trades via Telegram.

**Fix:** Use `asyncio.run_coroutine_threadsafe`:
```python
future = asyncio.run_coroutine_threadsafe(
    orch.executor.execute({"approved_trade": approved}), orch._loop
)
result = future.result(timeout=60)
```

---

### C2 — `_close_position` state divergence when `close_trade` fails
**File:** `src/agents/executor.py` ~L417

After executing a sell order on the exchange, the executor calls `state.close_trade(trade_id, ...)`. If the trade isn't found (already closed by another path, e.g., trailing stop), `close_trade` returns `None` but the exchange sell was already placed. The subsequent `trade.pnl` check silently skips `record_loss`, creating a state/exchange divergence.

```python
self.state.close_trade(trade.id, close_price, fees)  # might not find it
if trade.pnl and trade.pnl < 0:   # trade.pnl still None → skipped
    self.rules.record_loss(abs(trade.pnl))
```

**Impact:** Exchange has sold the position but internal state still shows it open. PnL and daily loss tracking are wrong.

**Fix:** Check return value and log divergence:
```python
closed = self.state.close_trade(trade.id, close_price, fees)
if not closed:
    logger.error(f"close_trade returned None for {trade.id} — state/exchange divergence")
elif closed.pnl and closed.pnl < 0:
    self.rules.record_loss(abs(closed.pnl))
```

---

## HIGH

### H1 — `get_candles` falls through to mock data on API failure in live mode
**File:** `src/core/coinbase_client.py` ~L234

When the REST client exists but the API call throws, execution falls through to `_mock_candles()`:

```python
def get_candles(self, product_id, granularity="ONE_HOUR", limit=200):
    if self._rest_client:
        try:
            ...
            return candle_list
        except Exception as e:
            logger.error(...)
            # ← Missing return! Falls through to mock data below
    return self._mock_candles(product_id, limit)
```

**Impact:** In live mode during transient API errors, the bot trades on randomly-generated fake candle data.

**Fix:** Add `return []` inside the exception handler.

---

### H2 — `get_portfolio_value` iterates `_paper_balance` without lock
**File:** `src/core/coinbase_client.py` ~L762

```python
def get_portfolio_value(self) -> float:
    if self.paper_mode:
        for currency, amount in self._paper_balance.items():  # ← No lock
```

All mutations of `_paper_balance` are protected by `_paper_balance_lock`, but this read path is not. Concurrent paper trades can raise `RuntimeError: dictionary changed size during iteration`.

**Impact:** Intermittent crash when querying portfolio value during trade execution.

**Fix:** Wrap in `with self._paper_balance_lock:`.

---

### H3 — `_handle_tighten_stop` AttributeError — dict accessed as object
**File:** `src/core/orchestrator.py` ~L1047

`tighten_to_breakeven()` returns a dict, but the caller uses attribute access:

```python
stop = self.trailing_stops.tighten_to_breakeven(pair)
if stop:
    entry = stop.entry_price  # ← AttributeError: dict has no 'entry_price'
```

**Impact:** Every "tighten stop" command from the dashboard silently fails.

**Fix:** `entry = stop["entry_price"]`

---

### H4 — `check_pending_orders` directly mutates trade objects outside state lock
**File:** `src/agents/executor.py` ~L461-L499

Trade fields (`status`, `filled_price`, `filled_quantity`, `fees`) are mutated directly without holding `state._lock`. Concurrent reads see partial updates.

**Impact:** Race condition: a reader sees `status=FILLED` but stale `filled_price`.

**Fix:** Add a `state.update_trade_fill(trade_id, ...)` method that performs updates under the lock.

---

### H5 — `_partial_sell` doesn't update `state.positions`
**File:** `src/agents/executor.py` ~L306-L340

After a partial sell, `state.update_partial_fill` updates `trade.filled_quantity` but NOT `state.positions`. The position still shows the original full quantity until the trade is fully closed.

**Impact:** Incorrect position size tracking → wrong risk calculations, potentially blocking new buys.

**Fix:** Either record a partial sell trade via `state.add_trade()` or have `update_partial_fill` deduct from positions.

---

### H6 — `update_llm_providers` TOCTOU race condition
**File:** `src/utils/settings_manager.py` ~L1076-L1085

`load_settings()` and `save_settings()` are called as separate lock acquisitions. Another writer can interleave, and its changes are silently overwritten.

**Impact:** Concurrent settings updates (dashboard + Telegram) cause lost writes.

**Fix:** Wrap full load→modify→save in a single `with _lock:` scope (like `update_section` already does).

---

### H7 — WebSocket unauthenticated when `DASHBOARD_API_KEY` is unset
**File:** `src/dashboard/server.py` ~L1039-L1070

HTTP middleware restricts `/api/*` to localhost when no API key is set, but the WebSocket at `/ws/live` is not covered. If bound to `0.0.0.0`, anyone can subscribe to live events.

**Impact:** Network clients can see LLM span events, cycle IDs, pair names, model info without auth.

**Fix:** Add localhost check in WS handler when no API key is configured.

---

### H8 — `send_message` / `request_approval` create new `Bot` per call — connection leak
**File:** `src/telegram_bot/bot.py` ~L327-L407

Every outbound message creates a fresh `Bot(token=...)` instance (which internally creates a new `httpx.AsyncClient`), sends one request, and never closes the session.

**Impact:** Thousands of unclosed HTTP connections over time → file descriptor exhaustion.

**Fix:** Reuse `self._app.bot` when available, or keep a single reusable `Bot` instance.

---

### H9 — CORS wildcard check too narrow
**File:** `src/dashboard/server.py` ~L238-L247

The check `if _cors_origins == ["*"]` only fires when the list is exactly `["*"]`. Setting `"*, http://evil.com"` bypasses the warning but Starlette matches ALL origins when `"*"` appears anywhere in the list.

**Impact:** Malicious webpages can make cross-origin API calls to the dashboard.

**Fix:** `if "*" in _cors_origins:` instead of `== ["*"]`.

---

### H10 — Settings confirmation flow doesn't validate values
**File:** `src/dashboard/server.py` ~L1318-L1340

Step 1 stores `field_names` (keys only, no values). Step 2 only validates the section matches. An attacker can request confirmation for safe values, then submit dangerous values with the same token.

**Impact:** Two-step confirmation can be bypassed by swapping values between steps.

**Fix:** Store and validate a hash of the updates payload.

---

### H11 — WebSocket subscription signature uses different product order than request
**File:** `src/core/ws_feed.py` ~L326-L345

The HMAC signature is computed over `sorted(to_add)`, but the request `product_ids` contains unsorted set iteration. If Coinbase verifies against request order, subscriptions silently fail.

**Impact:** Stale price subscriptions after subscription updates.

**Fix:** Use the same sorted list for both the request body and signature.

---

### H12 — `/health/components` reads state without lock
**File:** `src/core/health.py` ~L110-L113

The `/health` endpoint correctly acquires `_lock`, but `/health/components` does not.

**Impact:** Health check consumers get corrupted data under concurrent updates.

**Fix:** Acquire `_lock` and return a copy.

---

### H13 — `_compute_correlation_penalty` uses substring match
**File:** `src/agents/risk_manager.py` ~L105-L127

`if open_base in existing_pair_key` does substring matching: "SOL" matches "SOLO-USD", "BTC" matches "WBTC-USD".

**Impact:** False correlation penalties → incorrect position sizing.

**Fix:** Use exact key matching or split on `-` for proper asset comparison.

---

## MEDIUM

### M1 — `_technical_only_signal` has inverted RSI scoring
**File:** `src/agents/market_analyst.py` ~L273-L282

```python
elif rsi_signal == "bearish":
    score += 1     # bearish → score UP? Inverted
elif rsi_signal == "bullish":
    score -= 1     # bullish → score DOWN? Also inverted
```

**Impact:** Fallback (no-LLM) signals are inverted for bearish/bullish RSI.

**Fix:** Swap the signs: bearish → `score -= 1`, bullish → `score += 1`.

---

### M2 — `reverse_trade_booking` can produce negative positions
**File:** `src/core/state.py` ~L338

Reversing a buy deducts from positions without ensuring the result stays ≥ 0. If the position was partially sold by another path, the position goes negative.

**Impact:** Corrupt portfolio value calculations and spurious sell signals.

**Fix:** `max(0.0, ...)` guard with a warning log.

---

### M3 — `load_pending_approvals` replaces dict without lock
**File:** `src/core/managers/state_manager.py` ~L68

`orch._pending_approvals = validated` is assigned outside `_pending_approvals_lock`. Concurrent `cmd_approve_trade` could pop from the stale reference.

**Impact:** Approval appears lost after state reload.

**Fix:** Wrap assignment in `with orch._pending_approvals_lock:`.

---

### M4 — `save_scan_results` receives wrong type for `top_movers`
**File:** `src/core/managers/universe_scanner.py` ~L186-L190

`top_movers_str` is a string, but `save_scan_results` expects a list. It gets serialized as `"\"BTC-USD=0.5, ...\""` instead of a JSON array.

**Impact:** Dashboard consumers get garbled top movers data.

**Fix:** Pass a list of dicts: `[{"pair": p, "score": d["composite_score"]} ...]`.

---

### M5 — Settings updates set runtime before persisting
**File:** `src/core/managers/telegram_manager.py` ~L367-L399

In `_update_trading_param`, the value is applied to the runtime config before `sm.update_section()` validates and persists. If validation fails, runtime has a value that disk does not.

**Impact:** Runtime config drifts from disk. Restart silently reverts the change.

**Fix:** Validate and persist first, apply to runtime only on success.

---

### M6 — `_resolve_contextual_yes` uses literal match for regex pattern
**File:** `src/telegram_bot/chat_handler.py` ~L1122

`"high.?stakes"` uses regex metacharacters but is matched with Python's `in` operator (literal substring). It never matches "high stakes" or "high-stakes".

**Impact:** Contextual "yes" resolution for high-stakes status always fails.

**Fix:** Use `re.search` or replace with multiple literal keywords.

---

### M7 — `_schedule_is_due` crashes on malformed cron expression
**File:** `src/telegram_bot/chat_handler.py` ~L897-L910

`int(cron[:-1])` raises `ValueError` if the prefix isn't numeric. The exception skips all remaining schedules for that tick cycle.

**Impact:** One malformed schedule blocks all scheduled reports.

**Fix:** Wrap parse in try/except with sensible default.

---

### M8 — `live_coinbase_snapshot` unbounded API calls without rate limiting
**File:** `src/core/managers/holdings_manager.py` ~L151-L162

Per-pair price fetches in a loop with no `rate_limiter.wait("coinbase_rest")`.

**Impact:** Coinbase 429 errors that cascade into the main trading pipeline.

**Fix:** Acquire rate limiter before each REST call.

---

### M9 — `_handle_callback` has no Markdown parse fallback
**File:** `src/telegram_bot/bot.py` ~L250

Unlike `_send_reply` which retries as plain text, the callback handler uses `parse_mode="Markdown"` with no fallback. LLM responses with unmatched `*` or `_` cause silent failure.

**Impact:** Approve/reject callback buttons fail silently on invalid Markdown.

**Fix:** Wrap in try/except, retry without parse_mode.

---

### M10 — `send_trade_command` doesn't validate pair format
**File:** `src/dashboard/server.py` ~L1855-L1886

The `pair` path parameter is pushed to Redis with no format validation.

**Impact:** Arbitrary strings (injection payloads) reach the orchestrator via Redis.

**Fix:** Validate against `parse_pair()` before pushing.

---

### M11 — `get_rate_limiter()` singleton TOCTOU race
**File:** `src/utils/rate_limiter.py` ~L140-L145

Two threads can each create a `RateLimiter`, splitting call history.

**Impact:** Rate limits not enforced for initial concurrent calls.

**Fix:** Use a lock or initialize at module level.

---

### M12 — Executor slippage calculation wrong for sell direction
**File:** `src/agents/executor.py` ~L218-L223

The return dict computes slippage without direction correction applied in the logging block.

**Impact:** Downstream consumers get incorrect slippage data for sell orders.

---

### M13 — Mutable default `config: dict = {}` in `set_globals()` / `create_app()`
**File:** `src/dashboard/server.py` ~L103, ~L131

Classic Python mutable default trap. Mutations persist across calls.

**Fix:** `config: dict = None` with `config = config or {}`.

---

## LOW

### L1 — `_mock_product` uses unseeded global RNG
**File:** `src/core/coinbase_client.py` ~L1328

`_mock_candles` was fixed to use seeded RNG, but `_mock_product` still uses global `random.uniform`.

---

### L2 — `balance` property reads `_paper_balance` without lock
**File:** `src/core/coinbase_client.py` ~L1369

`.copy()` under CPython GIL is mostly safe, but technically unprotected.

---

### L3 — `_handle_liquidate` uses potentially zero cached price
**File:** `src/core/orchestrator.py` ~L1027

If the pair isn't in `current_prices` (e.g., after restart), price defaults to 0.

---

### L4 — Nonce replay dict only pruned on next command arrival
**File:** `src/core/orchestrator.py` ~L128

If no commands arrive after a burst, stale entries persist. Minimal memory impact.

---

### L5 — `sanitize_input` rebuilds NFKC map and homoglyph table on every call
**File:** `src/utils/security.py` ~L34-L53

Module-level constants would avoid per-call overhead.

---

### L6 — `_RULES_SETTABLE_ATTRS` frozenset rebuilt on every loop iteration
**File:** `src/utils/settings_manager.py` ~L867-L874

Should be module-scope constant.

---

### L7 — `journal.get_stats` reads file without lock
**File:** `src/utils/journal.py` ~L135-L185

Concurrent writes could produce partial line reads. Silent `JSONDecodeError` skip mitigates crash risk.

---

### L8 — `get_recent_decisions` loads entire file
**File:** `src/utils/journal.py` ~L200-L218

A tail-read approach would be more efficient for long-running bots.

---

### L9 — Emergency replan spawns thread before checking cooldown
**File:** `src/core/managers/event_manager.py` ~L72-L78

During volatile markets, hundreds of threads are spawned just to return immediately.

**Fix:** Check cooldown before spawning.

---

### L10 — StatsDB thread-local connections leak when threads die
**File:** `src/utils/stats.py` ~L63-L73

References in `_connections` keep connections alive past thread termination.

---

### L11 — `context.matches` wrong attribute for `CommandHandler`
**File:** `src/telegram_bot/bot.py` ~L200

Produces log entries like `"Context: /None"` instead of the actual command.

---

### L12 — Fiat rate cache thundering herd on TTL expiry
**File:** `src/core/coinbase_client.py` ~L64

Multiple concurrent callers all fetch when cache expires.

---

---

## Summary

| Severity | Count | Key Themes |
|----------|-------|------------|
| Critical | 2 | Thread safety in Telegram commands, state/exchange divergence |
| High | 13 | Mock data fallthrough, lock discipline, auth gaps, connection leaks |
| Medium | 13 | Inverted signals, type mismatches, validation gaps, race conditions |
| Low | 12 | Performance, minor races, cosmetic issues |

### Prioritized Fix Order

1. **C1** — `run_until_complete` → `run_coroutine_threadsafe` (Telegram approvals broken)
2. **C2** — Check `close_trade` return value (state/exchange divergence)
3. **H1** — Add `return []` in `get_candles` exception handler (live mode mock data)
4. **H2** — Lock `get_portfolio_value` paper balance iteration
5. **H3** — Fix dict→attribute access in `_handle_tighten_stop`
6. **H4-H5** — Trade field mutation and position tracking under lock
7. **H6** — Settings `update_llm_providers` atomicity
8. **H7-H10** — Dashboard auth/CORS/confirmation fixes
9. **H11-H13** — WS signature, health lock, correlation matching
10. **M1-M13** — Signal logic, validation, rate limiting
11. **L1-L12** — Performance and cosmetic fixes
