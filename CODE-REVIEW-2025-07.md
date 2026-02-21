# Code Review — auto-traitor (July 2025)

Comprehensive review of 21 key source files across core, agents, dashboard, utils, and telegram_bot.

---

## CRITICAL

### C1 — Paper limit orders: balance mutation without lock
**Files:** `src/core/coinbase_client.py` lines ~1150–1200 (`_paper_limit_buy`) and ~1230–1295 (`_paper_limit_sell`)

Both `_paper_market_buy` and `_paper_market_sell` correctly wrap balance checks and mutations inside `self._paper_balance_lock`. However, `_paper_limit_buy` and `_paper_limit_sell` perform the **same operations completely outside the lock** when the order fills immediately.

```python
# _paper_limit_buy — fills at market, NO LOCK:
quote_bal = self._paper_balance.get(quote_currency, 0)   # ← unsynchronised read
if quote_bal < quote_amount + fee:
    return {...}
self._paper_balance[quote_currency] = quote_bal - quote_amount - fee  # ← unsynchronised write
self._paper_balance[base_currency] = ...                               # ← unsynchronised write
```

Compare with the correctly locked `_paper_market_buy`:
```python
with self._paper_balance_lock:
    quote_bal = self._paper_balance.get(quote_currency, 0)
    ...
```

**Impact:** Two concurrent limit fills can both pass the balance check but each deduct the full amount, resulting in **negative paper balance** — cascading incorrect trade decisions downstream.

**Fix:** Wrap the fill-path balance check + mutation in `with self._paper_balance_lock:`, matching the market order pattern. Also wrap the resting-order append (currently also unprotected).

---

### C2 — Telegram settings guard is always bypassed
**File:** `src/core/managers/telegram_manager.py` line ~258

```python
def _update_settings(p: dict) -> dict:
    ...
    if not sm.is_telegram_allowed(section):
        return {"ok": False, "error": "Section blocked..."}
```

`settings_manager.is_telegram_allowed()` returns a **non-empty string** (`"safe"`, `"semi_safe"`, or `"blocked"`). Since `not "blocked"` evaluates to `False` in Python, the guard **never triggers**. Every section — including infrastructure sections explicitly placed in `TELEGRAM_BLOCKED_SECTIONS` (`llm`, `logging`, `health`, `dashboard`, `analysis`, `journal`, `audit`) — can be modified via Telegram.

**Impact:** Any Telegram user (within the authorized users list) can modify LLM model configuration, logging parameters, dashboard settings, and analysis parameters that should require Dashboard-only access.

**Fix:**
```python
if sm.is_telegram_allowed(section) == "blocked":
    return {"ok": False, "error": f"Section '{section}' is blocked ..."}
```

Or even stricter, only allow `"safe"` and `"semi_safe"`:
```python
tier = sm.is_telegram_allowed(section)
if tier not in ("safe", "semi_safe"):
    return {"ok": False, "error": ...}
```

---

## HIGH

### H1 — LLMProvider fields mutated without lock in hot paths
**File:** `src/core/llm_client.py` lines ~207–245

`_is_provider_available()` and `_record_call()` read and mutate mutable fields on `LLMProvider` objects (`daily_tokens`, `daily_date`, `rpm_timestamps`, `cooldown_until`) **without holding `_providers_lock`**. The lock only protects the provider list snapshot.

```python
# chat() -- takes lock ONLY for list copy:
with self._providers_lock:
    providers = list(self._providers)

for provider in providers:
    if not self._is_provider_available(provider):   # ← mutates provider.daily_date, rpm_timestamps
        continue
    ...
    self._record_call(provider, total)               # ← mutates provider.daily_tokens, rpm_timestamps
```

If `chat()` and `chat_with_tools()` are called concurrently (e.g., from the pipeline + Telegram chat), the same `LLMProvider` object gets mutated from multiple threads simultaneously.

**Impact:** RPM counters, daily token budgets, and cooldown timers can become incorrect under concurrency. Worst case: a provider bypasses its rate limit and gets banned by the upstream API.

**Fix:** Either protect each `LLMProvider` with its own lock, or hold `_providers_lock` for the duration of each call attempt (or use atomic counters).

---

### H2 — Orchestrator asyncio event loop never closed
**File:** `src/core/orchestrator.py` line 138

```python
self._loop = asyncio.new_event_loop()
```

No `__del__`, `cleanup()`, or `finally` block ever calls `self._loop.close()`. While the process typically runs indefinitely and the loop dies with it, any restart/reload scenario (test harness, hot-reload) leaks the event loop's resources (file descriptors, thread pool, etc.).

**Fix:** Add cleanup in `run_forever`'s exit path:
```python
finally:
    self._loop.close()
```

---

### H3 — Settings YAML mutation via string replace is fragile
**File:** `src/core/orchestrator.py` `__init__` (within the `invalidate_strategic_context` block)

The orchestrator reads the raw YAML file as a string and uses `yaml_text.replace("invalidate_strategic_context: true", ...)` to flip a flag. This approach:

1. Breaks if the YAML uses different quoting, indentation, or comment nearby.
2. Could match in an unexpected location (e.g., inside a comment or a differently scoped key).
3. Bypasses the `settings_manager.save_settings()` atomic-write and validation path.

**Fix:** Use `settings_manager.update_section()` or at minimum load → modify → save through the YAML library.

---

### H4 — Dashboard `_profile_db_cache` grows without bound
**File:** `src/dashboard/server.py` `_require_db()` (line ~305)

Profile databases are cached in `_profile_db_cache` on first access but **never evicted**. In a multi-profile deployment or if profiles are dynamically added, this cache and its associated open SQLite connections grow indefinitely.

**Fix:** Add an LRU eviction policy (e.g., `functools.lru_cache` or a manual size cap with `close()` on eviction).

---

### H5 — WebSocket `update_subscriptions` uses stale `ws` reference
**File:** `src/core/ws_feed.py` `update_subscriptions()` (line ~300)

```python
with self._lock:
    ...
    ws = self._ws
    is_running = self._running

# Lock released — ws could be set to None by stop() here

if not ws or not is_running:
    return

ws.send(json.dumps(unsub))  # ← potential use-after-close
```

If `stop()` is called between releasing the lock and calling `ws.send()`, the WebSocket object may already be closed.

**Impact:** `WebSocketConnectionClosedException` or silently dropped messages.

**Fix:** Wrap the send calls in a try/except or hold the lock for the entire operation (sends are IO-bound but safe to do under a short-lived lock).

---

### H6 — `rules.py` accessing shared set outside lock
**File:** `src/core/rules.py` `add_never_trade_pair()` / `remove_never_trade_pair()` (lines ~445, ~455)

```python
def add_never_trade_pair(self, pair: str) -> dict:
    pair = pair.upper().strip()
    with self._lock:
        self.never_trade_pairs.add(pair)
    # Lock released ↑
    return {"ok": True, "blacklisted": pair, "all": sorted(self.never_trade_pairs)}
    #                                                       ↑ iterated outside lock
```

`sorted()` iterates the set after the lock is released. If another thread modifies the set concurrently, this can raise `RuntimeError: Set changed size during iteration`.

**Fix:** Move the `sorted()` call inside the `with self._lock:` block, or return a copy.

---

## MEDIUM

### M1 — Portfolio rotator bridge reversal failure has no recovery path
**Files:** `src/core/portfolio_rotator.py` `_attempt_bridge_reversal()`, `_attempt_fiat_reversal()`

When a two-leg bridged swap fails on leg 2, the code attempts a reversal. If the reversal also fails, the stuck bridge currency is logged and an alert is sent, but:

- No automatic retry or escalation mechanism exists.
- No pending task is queued for the next cycle.
- The stuck balance can drift silently.

**Suggestion:** Record partial failures to a persistent recovery queue that the orchestrator checks each cycle.

---

### M2 — StatsDB thread-local connections vs. FastAPI threadpool
**File:** `src/utils/stats.py` `_get_conn()` / `src/dashboard/server.py`

`StatsDB` uses `threading.local()` for per-thread SQLite connections. FastAPI's sync endpoints run on a threadpool where the same thread may serve different requests. The dashboard partially works around this with `_fresh_conn()`, but several endpoints bypass it:

- `get_stats_summary` (line ~487) opens its own `sqlite3.connect(db._db_path, ...)`.
- `get_portfolio_exposure` (line ~1610) does the same.
- `get_strategic` (line ~820) does the same.

This inconsistency means some endpoints benefit from `_fresh_conn`'s clean isolation while others may encounter stale thread-local connections or share connections across requests.

**Fix:** Standardize all dashboard endpoints to use `_fresh_conn()` or create a FastAPI dependency that yields a fresh connection per request.

---

### M3 — Inconsistent SQLite connection management pattern
**File:** `src/dashboard/server.py`

Some endpoints open connections manually and close in `finally`, others use `_fresh_conn()`, and others use `Depends(_get_profile_db)` which returns the `StatsDB` instance (and uses its thread-local connection). This triple pattern complicates maintenance and creates inconsistent connection lifecycle behavior.

---

### M4 — `validate_field` silently accepts unknown fields
**File:** `src/utils/settings_manager.py` `validate_field()` (line ~685)

```python
field_schema = schema_dict.get(key)
if field_schema is None:
    return True, "", value  # unknown fields pass through
```

Typos in field names (e.g., `max_singel_trade`) are silently accepted and persisted, with no validation applied.

**Suggestion:** At minimum log a warning; optionally, reject unknown fields in strict mode.

---

### M5 — Dashboard SPA catch-all serves HTML for invalid API paths
**File:** `src/dashboard/server.py` (line ~1937)

```python
@app.get("/{full_path:path}", include_in_schema=False)
def serve_spa(full_path: str):
    return FileResponse(str(index))
```

When the static directory exists, any unmatched path (including `/api/nonexistent`) returns `index.html` with a 200 status. API consumers expecting JSON 404 errors will instead receive HTML.

**Fix:** Add a guard: `if full_path.startswith("api/"): raise HTTPException(404)`.

---

### M6 — `_check_big_result` regex doesn't handle all PnL formats
**File:** `src/telegram_bot/chat_handler.py` `ProactiveEngine._check_big_result()` (line ~575)

```python
pnl_match = _re.search(r'PnL:\s*\$?([-+]?[\d,]+\.?\d*)', event)
```

This regex handles `PnL: $50.00` and `PnL: -50.00` but not `PnL: -$50.00` (sign before currency symbol) or currency symbols other than `$` (the bot supports EUR via `currency_symbol`).

---

### M7 — No rate limiting on confirmation token generation
**File:** `src/dashboard/server.py` settings and API key update endpoints

The confirmation token flow generates a new `secrets.token_urlsafe(32)` per request without any rate limiting. An attacker with API key access could flood the `pending_confirmations` dict with unbounded entries (they're pruned on expiry, but within the 120s TTL window, memory grows linearly with request rate).

---

## LOW

### L1 — `import uuid` repeated inside every order method
**File:** `src/core/coinbase_client.py`

The `uuid` module is imported at the top of `market_order_buy`, `market_order_sell`, `limit_order_buy`, `limit_order_sell`, and every paper trading method. Move to a single top-level import.

---

### L2 — Dead legacy methods in chat_handler
**File:** `src/telegram_bot/chat_handler.py` lines ~1580–1592

```python
def generate_proactive_update(self, trading_context: dict) -> Optional[str]:
    """Legacy compat — proactive engine handles this now."""
    ...
    return None

def generate_daily_plan(self, trading_context: dict) -> Optional[str]:
    """Legacy compat — proactive engine handles daily plans."""
    return None
```

These methods are dead code. Consider removing them or marking them `@deprecated`.

---

### L3 — `_check_big_result` imports `re` as `_re` inside method body
**File:** `src/telegram_bot/chat_handler.py` line ~570

```python
def _check_big_result(self, event: str) -> None:
    import re as _re
```

`re` is already imported at the module level. The local reimport is unnecessary.

---

### L4 — `_paper_limit_buy` resting order doesn't hold balance in escrow
**File:** `src/core/coinbase_client.py` ~line 1165

When a paper limit buy rests as OPEN, no balance is reserved. A subsequent market buy could consume the funds, causing the limit order to fail silently when it eventually "fills" during `check_pending_orders`. This mimics exchange behavior to some degree, but differs from real Coinbase which reserves funds on limit order placement.

---

### L5 — Dashboard endpoint error responses are inconsistent
**Files:** `src/dashboard/server.py` various endpoints

Some endpoints raise `HTTPException` on failure (proper), while others return empty dicts:
```python
# get_news — returns empty on error:
return {"articles": [], "count": 0, "source": "error"}

# get_trailing_stops — same:
return {"stops": {}, "source": "error"}
```

Consumers need to check a `source` field rather than HTTP status codes.

---

### L6 — `ProactiveEngine._run_loop_thread` starts a new asyncio event loop
**File:** `src/telegram_bot/chat_handler.py` line ~613

```python
def _run_loop_thread(self) -> None:
    import asyncio
    asyncio.run(self._run_loop())
```

This is fine for a background thread, but `asyncio` is already imported at module level — the local import is redundant.

---

### L7 — `_mock_candles` uses random walk without seed
**File:** `src/core/coinbase_client.py` `_mock_candles()` line ~1340

Mock candles use `random.gauss()` without seed, producing non-reproducible data across test runs. Consider accepting an optional seed parameter for deterministic testing.

---

## Summary

| Severity | Count | Key Themes |
|----------|-------|------------|
| **CRITICAL** | 2 | Race condition in paper trading balance, Telegram security bypass |
| **HIGH** | 6 | Thread safety in LLM providers, resource leaks, fragile config mutation |
| **MEDIUM** | 7 | DB connection patterns, input validation, error handling |
| **LOW** | 7 | Import hygiene, dead code, minor inconsistencies |

**Top priority fixes:** C1 (paper balance race) and C2 (Telegram guard bypass) should be addressed immediately — C2 is a straightforward one-line fix; C1 requires wrapping ~20 lines in a `with` block.
