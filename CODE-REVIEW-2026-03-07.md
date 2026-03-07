# Code Review — 2026-03-07

**Scope:** Full codebase review across all modules (~180 files, ~50k LoC).  
**Prior review status:** 82/98 issues from previous reviews fixed. This review identifies **new** findings only.

---

## Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 12 |
| HIGH     | 38 |
| MEDIUM   | 55 |
| LOW      | 20 |
| **Total** | **125** |

---

## CRITICAL Issues

### CRIT-1: Division-by-zero cascade in technical indicators
**File:** [src/analysis/technical.py](src/analysis/technical.py#L69)  
**Impact:** RSI, ADX, Stochastic RSI, and VWAP all use `.replace(0, np.nan)` *after* division, not before. When `avg_loss`, `plus_di + minus_di`, or cumulative volume are zero, the division produces `inf` first, then `.replace()` is a no-op because the value is `inf` not `0`. Downstream strategies receive inf/NaN values.  
**Fix:** Move `.replace(0, np.nan)` to the denominator *before* division:
```python
denom = avg_loss.replace(0, np.nan)
rs = avg_gain / denom
```
Apply the same pattern to ADX (L142), Stochastic RSI (L129), and VWAP (L167).

### CRIT-2: Race condition on `_pairs_lock` inconsistency in orchestrator
**File:** [src/core/orchestrator.py](src/core/orchestrator.py#L752)  
**Impact:** `self.pairs[:effective_max_active]` accessed without `_pairs_lock` while settings advisor can mutate `self.pairs` from another thread. Causes inconsistent pair lists mid-cycle, missed or duplicated pipelines.  
**Fix:** Acquire `_pairs_lock` when reading `self.pairs` in the main cycle.

### CRIT-3: Stale position tracking — additive-only merge in state
**File:** [src/core/state.py](src/core/state.py#L133)  
**Impact:** Positions sold externally (via Coinbase UI) remain in `self.positions` forever because the merge only *adds* new holdings. The bot believes it still owns the position and can't re-buy or correct the discrepancy.  
**Fix:** If a holding exists in `self.positions` but Coinbase reports 0 quantity and there's no matching `positions_meta` entry, remove it from `self.positions`.

### CRIT-4: Race condition on `_initial_balance_synced` flag
**File:** [src/core/state.py](src/core/state.py#L170)  
**Impact:** Two threads calling `sync_live_holdings()` can both pass the `not self._initial_balance_synced` check, both updating `initial_balance` to potentially different values. Corrupts return/drawdown calculations.  
**Fix:** Protect with a dedicated lock or use `threading.Event` for one-shot initialization.

### CRIT-5: `_trim_closed_trades()` destroys FIFO lot tracking
**File:** [src/core/state.py](src/core/state.py#L258)  
**Impact:** Oldest closed trades are deleted to save memory. If FIFO cost-basis calculations reference those deleted trades, tax/PnL becomes incorrect.  
**Fix:** Archive trimmed trades to a separate list or DB table instead of deleting them.

### CRIT-6: Floating-point precision loss in fee calculations
**File:** [src/core/fee_manager.py](src/core/fee_manager.py#L233)  
**Impact:** Multi-leg fee calculation repeatedly subtracts floats from `remaining`, accumulating rounding error. After several legs, the fee estimate drifts enough that marginally-profitable trades are misclassified.  
**Fix:** Use `decimal.Decimal` for fee arithmetic.

### CRIT-7: Algorithmic complexity bomb in LLM JSON extraction
**File:** [src/core/llm_client.py](src/core/llm_client.py#L510)  
**Impact:** `_extract_json()` tries up to 10 opening braces with nested loops up to `len(text)`, creating O(n²) worst-case on malformed LLM output. A very long garbled response can hang the agent for seconds.  
**Fix:** Limit search to first 5KB: `for i, ch in enumerate(text[:5000])`.

### CRIT-8: Thread leak in multi-timeframe `ThreadPoolExecutor`
**File:** [src/analysis/multi_timeframe.py](src/analysis/multi_timeframe.py#L95)  
**Impact:** `pool.shutdown(wait=False)` after timeout does not cancel running futures. Workers blocked on slow API calls continue in background indefinitely, accumulating threads and memory.  
**Fix:** Cancel all pending futures before shutdown; use context manager with proper cleanup.

### CRIT-9: Fee gate division by zero when entry_price is 0
**File:** [src/core/managers/pipeline_manager.py](src/core/managers/pipeline_manager.py#L533)  
**Impact:** `expected_gain_pct = (trade_price - entry_price) / entry_price` — if `entry_price` is 0 from stale data, produces inf. The fee gate comparison becomes indeterminate, potentially allowing unprofitable trades through.  
**Fix:** Validate `entry_price > 0` before the calculation; skip execution on invalid prices.

### CRIT-10: `async_acquire()` blocks the event loop
**File:** [src/utils/rate_limiter.py](src/utils/rate_limiter.py#L83)  
**Impact:** Marked `async` but uses `threading.Lock` and `time.monotonic()` — blocking calls that stall the entire asyncio event loop, defeating all concurrency in pipeline processing.  
**Fix:** Use `asyncio.Lock` and `await asyncio.sleep()` instead.

### CRIT-11: Session FIFO eviction enables DoS
**File:** [src/dashboard/auth.py](src/dashboard/auth.py#L63)  
**Impact:** In-memory session store with max 100 sessions uses FIFO eviction. An attacker can create sessions rapidly to evict legitimate user sessions, effectively performing a denial-of-service.  
**Fix:** Use LRU eviction; notify/alert when capacity is hit; consider Redis-backed session store.

### CRIT-12: Incomplete SSRF protection in RSS feed fetcher
**File:** [src/news/aggregator.py](src/news/aggregator.py#L393)  
**Impact:** Only checks `http://`/`https://` scheme but allows `localhost`, `127.0.0.1`, `169.254.169.254` (AWS metadata), and private IPs. An RSS feed URL pointing to internal services could leak internal data.  
**Fix:** Resolve hostname to IP and reject private/loopback/link-local addresses:
```python
import ipaddress
ip = ipaddress.ip_address(socket.gethostbyname(urlparse(url).hostname))
if ip.is_private or ip.is_loopback or ip.is_link_local:
    raise ValueError(f"SSRF: refusing to connect to {ip}")
```

---

## HIGH Issues

### H-1: Unprotected concurrent access to `_pending_approvals`
**File:** [src/core/orchestrator.py](src/core/orchestrator.py#L621)  
Read paths exist without acquiring `_pending_approvals_lock`. Dict can be mutated while iterated.

### H-2: Nonce dict grows unbounded
**File:** [src/core/orchestrator.py](src/core/orchestrator.py#L560)  
`_used_nonces` dict is cleaned up during validation calls but grows without bound between calls. Memory leak for long-running instances.

### H-3: No timeout + cancellation for asyncio pipeline tasks
**File:** [src/core/orchestrator.py](src/core/orchestrator.py#L889)  
After `asyncio.wait_for` timeout, hanging tasks continue in background. Next cycle spawns duplicates, causing resource starvation.

### H-4: Swallowed exceptions in `gather(return_exceptions=True)`
**File:** [src/core/orchestrator.py](src/core/orchestrator.py#L895)  
Results of `asyncio.gather(..., return_exceptions=True)` are never inspected for exception entries. All pipeline failures are silently swallowed.

### H-5: Portfolio HWM never resets on account degradation
**File:** [src/core/rules.py](src/core/rules.py#L75)  
Emergency stop threshold derived from HWM stays at peak forever. After deposits + withdrawals, emergency stop becomes ineffective.

### H-6: DB cursor leak in stats module
**File:** [src/utils/stats.py](src/utils/stats.py#L77)  
`_ConnProxy.execute()` creates new cursor per call but never closes cursors explicitly. Memory bloat on PostgreSQL over 100k+ queries.

### H-7: WebSocket ticker callback race condition
**File:** [src/core/ws_feed.py](src/core/ws_feed.py#L139)  
Price updated under lock but callbacks invoked outside lock. Between release and callback, price can change; callbacks may see stale data.

### H-8: WebSocket `update_subscriptions` TOCTOU
**File:** [src/core/ws_feed.py](src/core/ws_feed.py#L270)  
Product IDs captured under lock but `ws.send()` happens after lock release. Socket could close between; subscription update silently fails.

### H-9: Base agent counters not thread-safe
**File:** [src/agents/base_agent.py](src/agents/base_agent.py#L54)  
`_last_run`, `_run_count`, `_error_count` mutated without locks during concurrent agent execution.

### H-10: Executor stop-loss iteration on mutable collection
**File:** [src/agents/executor.py](src/agents/executor.py#L358)  
`check_stop_losses()` iterates `state.get_open_trades()` which can be modified concurrently. Can crash with `StopIteration` or skip stop-losses.

### H-11: Coinbase client `_backoff_until` not protected by lock
**File:** [src/core/coinbase_client.py](src/core/coinbase_client.py#L93)  
Thread race: one thread sets cooldown while another checks it, leading to simultaneous retries past rate limits.

### H-12: Division by zero in ADX calculation
**File:** [src/analysis/technical.py](src/analysis/technical.py#L142)  
When both DI values are 0, division produces inf before `.replace(0, np.nan)` can act. ADX becomes inf downstream.

### H-13: Empty DataFrame `.iloc[-1]` crash
**File:** [src/analysis/technical.py](src/analysis/technical.py#L36)  
If candles conversion returns DataFrame with <30 rows, callers use `.iloc[-1]` which crashes on empty frames.

### H-14: VWAP undefined (NaN) not caught
**File:** [src/analysis/technical.py](src/analysis/technical.py#L167)  
Cumulative volume of 0 makes VWAP NaN. NaN comparisons silently fail; VWAP signal becomes "unknown" without logging.

### H-15: Stochastic RSI division by zero
**File:** [src/analysis/technical.py](src/analysis/technical.py#L129)  
When `rsi_high == rsi_low`, denominator is 0 before `.replace()`. Produces inf, not NaN, breaking downstream.

### H-16: Multi-TF `_candle_cache` race condition
**File:** [src/analysis/multi_timeframe.py](src/analysis/multi_timeframe.py#L59)  
Cache lock held for reads but async methods modify cache concurrently without consistent locking.

### H-17: Multi-TF no error propagation from failed timeframes
**File:** [src/analysis/multi_timeframe.py](src/analysis/multi_timeframe.py#L81)  
If all timeframes fail, result still returns with 0.0 confluence. Caller doesn't know every TF failed.

### H-18: Sentiment double-counting via keyword replacement
**File:** [src/analysis/sentiment.py](src/analysis/sentiment.py#L111)  
Text replacement for bullish keywords leaves sub-phrases that match other entries. Sentiment scores inflated.

### H-19: Fear & Greed fallback uses stale last_value
**File:** [src/analysis/fear_greed.py](src/analysis/fear_greed.py#L81)  
On fetch failure, returns previous value which could be hours/days old. LLM fed stale data but doesn't know its age.

### H-20: Misaligned return vectors in pairs correlation
**File:** [src/strategies/pairs_monitor.py](src/strategies/pairs_monitor.py#L182)  
When zero-price candles are skipped from one series, return vectors become misaligned. Pearson correlation computed on wrong index pairs.

### H-21: O(n²) correlation matrix computation
**File:** [src/strategies/pairs_monitor.py](src/strategies/pairs_monitor.py#L241)  
Computes correlation(A,B) and correlation(B,A) separately. Double work for symmetric matrix.

### H-22: EMA strategy ADX asymmetric confidence penalty
**File:** [src/strategies/ema_crossover.py](src/strategies/ema_crossover.py#L160)  
Bullish crossovers get ADX penalty but the system is biased long in ranging markets. In bear markets with weak ADX, keeps buying false breakouts.

### H-23: Bollinger OBV divergence signals not freshness-checked
**File:** [src/strategies/bollinger_reversion.py](src/strategies/bollinger_reversion.py#L111)  
OBV divergence from stale candles consumed as if fresh. Could enter after reversal already happened.

### H-24: LLM screener JSON parsing fragile
**File:** [src/core/managers/universe_scanner.py](src/core/managers/universe_scanner.py#L301)  
Non-greedy regex `\[.*?\]` matches first JSON array in LLM output. If response contains multiple arrays, picks wrong one.

### H-25: FIFO tracker records buy without confirming execution
**File:** [src/core/managers/pipeline_manager.py](src/core/managers/pipeline_manager.py#L681)  
FIFO buy lots recorded before execution confirmation. If execution fails, orphaned lots inflate cost basis.

### H-26: Slippage values not validated post-execution
**File:** [src/core/managers/pipeline_manager.py](src/core/managers/pipeline_manager.py#L732)  
Slippage > 50% recorded without bounds check. Training data corrupted with impossible values.

### H-27: Stale approval auto-pruned without notification
**File:** [src/core/managers/state_manager.py](src/core/managers/state_manager.py#L119)  
Approvals older than 1 hour silently deleted. User who queued a trade and stepped away loses the approval with no alert.

### H-28: Planning activities — no transaction isolation
**File:** [src/planning/activities.py](src/planning/activities.py#L148)  
Concurrent reads and writes during planning vs. orchestrator can see inconsistent trade data.

### H-29: Context manager assumes StatsDB exists
**File:** [src/core/managers/context_manager.py](src/core/managers/context_manager.py#L233)  
`orch.stats_db.get_performance_summary()` crashes with AttributeError if `stats_db` is None (dev mode).

### H-30: Trailing stop `pending_tier_exits` list not thread-safe
**File:** [src/core/trailing_stop.py](src/core/trailing_stop.py#L62)  
Appended to and read from different threads without any lock protection. Can lose tier exit signals.

### H-31: Trailing stop division by zero if entry_price is 0
**File:** [src/core/trailing_stop.py](src/core/trailing_stop.py#L167)  
`pnl_pct = (current_price - self.entry_price) / self.entry_price * 100` — ZeroDivisionError on corrupted data.

### H-32: Equity calendar concurrent fetch (TOCTOU)
**File:** [src/core/managers/pipeline_manager.py](src/core/managers/pipeline_manager.py#L161)  
Two async tasks both see TTL expired, both fetch equity calendar, wasting API calls.

### H-33: IBKR client ID collision across dashboard instances
**File:** [src/dashboard/server.py](src/dashboard/server.py#L95)  
All dashboard instances use `ib_client_id + 10`, causing IB Gateway session conflicts.

### H-34: Telegram bot `_get_send_loop()` not thread-safe
**File:** [src/telegram_bot/bot.py](src/telegram_bot/bot.py#L345)  
Multiple threads can create duplicate event loops on first call — memory leak.

### H-35: Chat handler no timeout on LLM calls
**File:** [src/telegram_bot/chat_handler.py](src/telegram_bot/chat_handler.py#L150)  
If LLM provider hangs, message handler blocks indefinitely, preventing all other messages.

### H-36: Chat handler conversation memory never expires
**File:** [src/telegram_bot/chat_handler.py](src/telegram_bot/chat_handler.py#L70)  
30 messages kept indefinitely. Could contain stale data or sensitive information. No TTL.

### H-37: Settings confirmation rate dict eviction race
**File:** [src/dashboard/routes/settings.py](src/dashboard/routes/settings.py#L72)  
Between checking dict size and deleting stale entries, another thread can add more. Dict exceeds cap.

### H-38: Settings `_RULES_SETTABLE_ATTRS` hardcoded allowlist
**File:** [src/utils/settings_manager.py](src/utils/settings_manager.py#L493)  
New `AbsoluteRules` attributes must be manually added. If forgotten, hot-reload silently fails for that attribute. No automated sync test.

---

## MEDIUM Issues

### M-1: LiveHoldings sync not protected against concurrent position updates
[src/core/orchestrator.py](src/core/orchestrator.py#L730) — Holdings refresh without state locks; trade between fetch and sync causes divergence.

### M-2: Batch scoring accesses `_last_pipeline_ts` without lock
[src/core/orchestrator.py](src/core/orchestrator.py#L1082) — Dict read without lock during concurrent writes.

### M-3: Settings advisor may update `self.pairs` without triggering WebSocket resubscription
[src/core/orchestrator.py](src/core/orchestrator.py#L847) — New pairs not subscribed; old pairs still subscribed.

### M-4: Daily counter race in rules engine
[src/core/rules.py](src/core/rules.py#L218) — Between reset and increment, another thread may pass check.

### M-5: Approval threshold doesn't scale with portfolio size
[src/core/rules.py](src/core/rules.py#L317) — Fixed `require_approval_above` becomes annoying on large accounts.

### M-6: `_last_trade_time` type not validated
[src/core/rules.py](src/core/rules.py#L289) — Used in datetime comparison without type guard.

### M-7: DB connection leaked if `psycopg2.connect()` fails
[src/core/rules.py](src/core/rules.py#L103) — Connection created before try/finally block.

### M-8: Dust threshold inconsistency between state and rules
[src/core/state.py](src/core/state.py#L143) — Rules may count dust positions against limits.

### M-9: `portfolio_history` unbounded growth potential
[src/core/state.py](src/core/state.py#L197) — Bounded at 10k, trimmed to 5k, but should use `deque(maxlen=5000)`.

### M-10: `update_partial_fill()` doesn't validate remaining >= 0
[src/core/state.py](src/core/state.py#L349) — Negative `remaining_quantity` corrupts positions.

### M-11: Tier exit fractions not validated to sum <= 1.0
[src/core/trailing_stop.py](src/core/trailing_stop.py#L82) — Exit fractions that exceed 1.0 produce negative remaining.

### M-12: Equity fee model loses min_fee for zero-amount trades
[src/core/fee_manager.py](src/core/fee_manager.py#L278) — `return min(fee, quote_amount * max_fee_pct)` when amount is 0 yields 0, losing min_fee.

### M-13: Swap leg count hardcoded to 2
[src/core/fee_manager.py](src/core/fee_manager.py#L259) — Crypto-to-crypto via fiat bridge is 3 legs.

### M-14: High stakes duration parsing incomplete error handling
[src/core/high_stakes.py](src/core/high_stakes.py#L94) — "1h30m" silently returns None without feedback.

### M-15: High stakes audit log leaks configuration multipliers
[src/core/high_stakes.py](src/core/high_stakes.py#L136) — Trade sizing multipliers exposed in audit trail.

### M-16: Silent data loss on empty candle DataFrames
[src/analysis/technical.py](src/analysis/technical.py#L42) — `pd.to_numeric(errors="coerce")` silently converts invalid values to NaN; 100% NaN column is undetected.

### M-17: ATR can be zero, causing downstream division by zero
[src/analysis/technical.py](src/analysis/technical.py#L102) — Rolling mean of zero TR values.

### M-18: OBV direction detection misses flat price movements
[src/analysis/technical.py](src/analysis/technical.py#L152) — `np.sign()` returns 0 when delta=0, ignoring volume.

### M-19: Signal interpretation thresholds hardcoded
[src/analysis/technical.py](src/analysis/technical.py#L308) — RSI 30/70, MACD, BB all hardcoded; no config override.

### M-20: 24h price change uses index offset, not timestamp
[src/analysis/technical.py](src/analysis/technical.py#L286) — Assumes candle at index -24 is exactly 24 hours ago.

### M-21: Multi-TF volume ratio multiplier amplifies noise
[src/analysis/multi_timeframe.py](src/analysis/multi_timeframe.py#L173) — Multiplicative scaling amplifies contradictory signals.

### M-22: Multi-TF aggregated bar low can be 0
[src/analysis/multi_timeframe.py](src/analysis/multi_timeframe.py#L149) — If all lows are 0, bar has bogus low value.

### M-23: Sentiment confidence over-simplification
[src/analysis/sentiment.py](src/analysis/sentiment.py#L144) — 5 matches = 100% confidence regardless of keyword weight.

### M-24: Sentiment recency weighting assumes newest-first input
[src/analysis/sentiment.py](src/analysis/sentiment.py#L239) — Unsorted input gives wrong recency weights.

### M-25: Fear & Greed global cache not thread-safe
[src/analysis/fear_greed.py](src/analysis/fear_greed.py#L19) — Module-level dict accessed without lock.

### M-26: Bollinger ATR-based stop-loss can go negative
[src/strategies/bollinger_reversion.py](src/strategies/bollinger_reversion.py#L148) — Volatile market + large ATR multiplier.

### M-27: Bollinger entry_price = current_price assumption wrong
[src/strategies/bollinger_reversion.py](src/strategies/bollinger_reversion.py#L171) — By execution time, price may have moved.

### M-28: Bollinger take-profit can be smaller than fee breakeven
[src/strategies/bollinger_reversion.py](src/strategies/bollinger_reversion.py#L153) — Mean reversion target too close in tight BB.

### M-29: EMA strategy candle count check too strict
[src/strategies/ema_crossover.py](src/strategies/ema_crossover.py#L129) — Requires 202 candles for 200-period EMA; misses valid setups.

### M-30: Pairs monitor z_threshold not validated
[src/strategies/pairs_monitor.py](src/strategies/pairs_monitor.py#L220) — Config value of 0 triggers hyperactive signals.

### M-31: Pairs monitor min_correlation filter too rigid
[src/strategies/pairs_monitor.py](src/strategies/pairs_monitor.py#L224) — Misses decorrelation events.

### M-32: Context manager no warning when no strategic plans exist
[src/core/managers/context_manager.py](src/core/managers/context_manager.py#L43) — System trades without plan guidance silently.

### M-33: Accuracy cache TTL too long (6 hours)
[src/core/managers/context_manager.py](src/core/managers/context_manager.py#L88) — Stale accuracy data scales plan confidence incorrectly.

### M-34: Context manager ignores bearish predictions
[src/core/managers/context_manager.py](src/core/managers/context_manager.py#L106) — System can't capitalize on clear downtrends.

### M-35: Universe scanner `run_universe_scan()` blocks event loop
[src/core/managers/universe_scanner.py](src/core/managers/universe_scanner.py#L215) — Synchronous function with `time.sleep()` called from async context.

### M-36: LLM screener hallucinated pairs logged at debug, not warning
[src/core/managers/universe_scanner.py](src/core/managers/universe_scanner.py#L328) — LLM hallucinations go unnoticed.

### M-37: Settings manager file write failure doesn't prevent memory update
[src/core/managers/universe_scanner.py](src/core/managers/universe_scanner.py#L356) — Memory updated after failed persist; lost on restart.

### M-38: Training collector unbounded snapshot rate
[src/core/managers/pipeline_manager.py](src/core/managers/pipeline_manager.py#L294) — No sampling; 50 pairs × 48 cycles/day = 2400 rows/day.

### M-39: Pipeline cache uses threading.Lock in async code
[src/core/managers/pipeline_manager.py](src/core/managers/pipeline_manager.py#L72) — Blocks event loop when held.

### M-40: Swap approval minimum amount not validated
[src/core/managers/state_manager.py](src/core/managers/state_manager.py#L62) — $0.01 swap passes validation but fails on exchange.

### M-41: Redis approval storage not idempotent
[src/core/managers/state_manager.py](src/core/managers/state_manager.py#L76) — Duplicate approvals in Redis on restarts.

### M-42: Homoglyph translation table incomplete
[src/utils/security.py](src/utils/security.py#L30) — Many Cyrillic/Greek lookalikes unmapped; prompt injection bypass possible.

### M-43: Credential format not validated
[src/utils/security.py](src/utils/security.py#L161) — 3-char "abc" passes validation; fails at first API call.

### M-44: Stats pool atexit doesn't drain active queries
[src/utils/stats.py](src/utils/stats.py#L62) — Active queries aborted on shutdown; data loss.

### M-45: Stats migration `col_type` not fully validated
[src/utils/stats.py](src/utils/stats.py#L267) — Interpolated col_type could inject SQL if sourced from untrusted input.

### M-46: Audit chain corruption recovery silent
[src/utils/audit.py](src/utils/audit.py#L59) — Corrupted chain silently restarted; no CRITICAL alert.

### M-47: Audit chain reorderable
[src/utils/audit.py](src/utils/audit.py#L102) — Hash doesn't include independent sequence number; entries can be reordered.

### M-48: Audit verify_chain loads entire file into memory
[src/utils/audit.py](src/utils/audit.py#L209) — OOM on large audit logs (10M+ entries).

### M-49: Settings manager autonomous list validation TOCTOU
[src/utils/settings_manager.py](src/utils/settings_manager.py#L302) — Comparison uses cached list; LLM can exploit timing.

### M-50: Journal file write errors silently lost
[src/utils/journal.py](src/utils/journal.py#L78) — Failed writes lose trading decisions without propagation.

### M-51: WebSocket callback list grows unbounded
[src/core/ws_feed.py](src/core/ws_feed.py#L43) — No `remove_ticker_callback()`; memory leak.

### M-52: WebSocket exponential backoff never alerts on permanent failure
[src/core/ws_feed.py](src/core/ws_feed.py#L248) — Retries at 60s forever; user doesn't know connection is dead.

### M-53: Paper trading balances use float — precision loss
[src/core/coinbase_paper.py](src/core/coinbase_paper.py#L47) — After 100+ trades, accumulated rounding errors.

### M-54: Paper limit orders have no expiration
[src/core/coinbase_paper.py](src/core/coinbase_paper.py#L207) — Resting orders stay forever; unrealistic vs. live markets.

### M-55: RSS ticker regex too permissive
[src/news/aggregator.py](src/news/aggregator.py#L220) — `[A-Z]{2,5}` matches random uppercase words in headlines.

---

## LOW Issues

### L-1: Duplicate `_ollama_skip_count` initialization
[src/core/orchestrator.py](src/core/orchestrator.py#L569) — Fragile dynamic attribute; should be in `__init__`.

### L-2: Portfolio scaler tier not logged at startup
[src/core/portfolio_scaler.py](src/core/portfolio_scaler.py#L127) — User doesn't see initial tier.

### L-3: Portfolio scaler disabled flag not honored in all paths
[src/core/portfolio_scaler.py](src/core/portfolio_scaler.py#L143) — Default tier values used instead of config values.

### L-4: LLM callback exceptions silently swallowed
[src/core/llm_client.py](src/core/llm_client.py#L486) — Training data collection errors unnoticed.

### L-5: Market analyst magic numbers without comments
[src/agents/market_analyst.py](src/agents/market_analyst.py#L286) — `confidence = min(abs(score) * 0.15, 0.75)` unexplained.

### L-6: Risk manager `max_position_pct` not validated > 0
[src/agents/risk_manager.py](src/agents/risk_manager.py#L293) — Config of 0 causes zero position sizes.

### L-7: LLM provider skip logging too low visibility
[src/core/llm_providers.py](src/core/llm_providers.py#L242) — Provider skipped at INFO; should be WARNING.

### L-8: Market analyst enum fallback not logged
[src/agents/market_analyst.py](src/agents/market_analyst.py#L216) — Invalid signal_type silently becomes NEUTRAL.

### L-9: Settings advisor empty change set still logs "applied"
[src/agents/settings_advisor.py](src/agents/settings_advisor.py#L241) — Noisy log for no-op.

### L-10: Signal model confidence field unbounded
[src/models/signal.py](src/models/signal.py#L73) — Values > 1.0 accepted; downstream multiplications break.

### L-11: Signal model RSI field unvalidated
[src/models/signal.py](src/models/signal.py#L42) — RSI=500 accepted without complaint.

### L-12: Trade model PnL doesn't handle partial fills
[src/models/trade.py](src/models/trade.py#L54) — Assumes exact fill; no multi-fill support.

### L-13: Helpers `format_currency` no type check
[src/utils/helpers.py](src/utils/helpers.py#L36) — String input crashes with TypeError.

### L-14: Rate limiter O(n) timestamp cleanup per call
[src/utils/rate_limiter.py](src/utils/rate_limiter.py#L69) — Use deque instead of list for O(1) cleanup.

### L-15: Journal `get_recent_decisions` loads entire file
[src/utils/journal.py](src/utils/journal.py#L164) — `readlines()` on 1GB file causes OOM. Read backwards instead.

### L-16: Paper trading slippage model unrealistic
[src/core/coinbase_paper.py](src/core/coinbase_paper.py#L82) — Uses mid + slippage instead of bid/ask spread.

### L-17: Paper mock candle volatility unrealistic
[src/core/coinbase_paper.py](src/core/coinbase_paper.py#L295) — 0.5%/candle implies ~40%/day.

### L-18: Discovery `detect_native_currency` assumes complete account list
[src/core/coinbase_discovery.py](src/core/coinbase_discovery.py#L100) — Pagination may be incomplete.

### L-19: Discovery unparseable price silently set to 0
[src/core/coinbase_discovery.py](src/core/coinbase_discovery.py#L336) — Pairs with price=0 included in results.

### L-20: Audit fsync on every write — performance bottleneck
[src/utils/audit.py](src/utils/audit.py#L148) — 100+ fsyncs/min blocks calling thread.

---

## Cross-Cutting Patterns

### 1. Inconsistent Locking Strategy
Multiple modules independently implement their own locking (executor, coinbase_client, base_agent, state, orchestrator). There's no shared `ThreadSafeState` wrapper or lock hierarchy documentation. This makes deadlock analysis impossible and race conditions pervasive.

**Recommendation:** Document a lock ordering convention. Consider a shared lock manager that tracks acquisition order and detects potential deadlocks in debug mode.

### 2. Division-by-Zero in Indicator Calculations
The `.replace(0, np.nan)` pattern is used consistently but incorrectly across technical.py — it's applied *after* division instead of *before*. This single pattern accounts for 5 findings (CRIT-1, H-12, H-14, H-15, M-17).

**Recommendation:** Create a safe division helper: `safe_div(a, b) = a / b.replace(0, np.nan)` and use it everywhere.

### 3. async/sync Boundary Violations
Several modules mix threading locks with async code (`rate_limiter.async_acquire`, `pipeline_manager` cache lock, `universe_scanner.run_universe_scan`). This blocks the event loop and defeats concurrency.

**Recommendation:** Audit all async code paths and replace `threading.Lock` with `asyncio.Lock` where appropriate. Use `asyncio.to_thread()` for unavoidably synchronous calls.

### 4. Silent Failure Epidemic
Broad `except Exception: pass/continue` blocks throughout the codebase swallow errors that should at minimum be logged. This makes debugging extremely difficult and can mask security-relevant failures.

**Recommendation:** Adopt a policy: no bare `except Exception: pass`. At minimum, log at debug level. For security-sensitive paths, log at warning/error.

### 5. Unbounded Memory Growth
Multiple dicts and lists grow without bound: `_used_nonces`, `_last_prices`, `_extra_ticker_callbacks`, `_unauthorized_attempts`, `portfolio_history`, `_confirmation_attempts`. Individual caps exist but are inconsistently applied.

**Recommendation:** Replace unbounded collections with `collections.deque(maxlen=N)` or TTL-based caches. Add memory usage monitoring.

---

## Priority Fix Order

1. **Immediate (CRITICAL, security impact):**
   - CRIT-1: Technical indicator division-by-zero cascade
   - CRIT-10: `async_acquire()` blocking event loop
   - CRIT-11: Session eviction DoS
   - CRIT-12: SSRF in RSS fetcher

2. **Urgent (CRITICAL/HIGH, data integrity):**
   - CRIT-3: Stale position tracking
   - CRIT-4: Initial balance race condition
   - CRIT-5: FIFO lot tracking destruction
   - CRIT-6: Fee calculation precision
   - CRIT-9: Fee gate division by zero
   - H-4: Swallowed pipeline exceptions

3. **Important (HIGH, operational reliability):**
   - CRIT-2: Orchestrator pairs_lock inconsistency
   - CRIT-7: LLM JSON extraction complexity bomb
   - CRIT-8: Thread leak in multi-TF
   - H-3: Pipeline task timeout/cancellation
   - H-7/H-8: WebSocket race conditions
   - H-10: Stop-loss iteration race
   - H-20: Pairs correlation misalignment
   - H-35: LLM call timeout in Telegram

4. **Planned (MEDIUM/HIGH, quality & maintainability):**
   - All remaining HIGH items
   - Cross-cutting pattern fixes (safe division helper, async/sync boundary audit)

---

*Review performed: 2026-03-07 | Reviewer: automated full-codebase analysis*
