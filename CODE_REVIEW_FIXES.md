# Code Review Fix Tracker

Review date: 2026-03-01
Branch: feature/multi-asset-trading

Legend: ✅ Fixed | 🔧 In Progress | ⏳ Pending | ➡️ Deferred

---

## CRITICAL

| ID | File | Issue | Status | Commit/Notes |
|----|------|-------|--------|--------------|
| CRIT-1 | `src/dashboard/routes/llm_analytics.py` ~L31 | `bucket_fmt` interpolated directly into SQL f-string | ✅ | Allowlist frozenset + assert in `_bucket_format` |
| CRIT-2 | `src/utils/stats.py` ~L310 | Table/column names interpolated into DDL ALTER TABLE via f-string | ✅ | `_MIGRATION_ALLOWLIST` frozenset checked before every ALTER TABLE |
| CRIT-3 | `src/dashboard/deps.py` ~L211 | Empty `DASHBOARD_COMMAND_SIGNING_KEY` silently accepted — HMAC forgeable | ✅ | Startup warning; rerun endpoint rejects if key empty |
| CRIT-4 | `src/utils/stats.py` ~L367 | Non-`DuplicateColumn` migration errors swallowed silently | ✅ | `logger.warning(...)` on unexpected errors |
| CRIT-5 | `src/dashboard/routes/planning.py` ~L152 | `POST /api/temporal/rerun/{id}` has no per-endpoint auth | ✅ | Explicit HMAC key check; returns 503 if key not configured |

## HIGH

| ID | File | Issue | Status | Commit/Notes |
|----|------|-------|--------|--------------|
| HIGH-1 | `src/dashboard/deps.py` L38 | `ws_connections` list is a mutable global with no lock | ➡️ | All access is within single asyncio event loop — cooperative scheduling makes this safe in practice; deferred |
| HIGH-2 | `src/agents/executor.py` ~L426 | Race condition: duplicate sell orders possible (lock released before exchange call) | ✅ | `_closing_trades` set + `_closing_trades_lock` guards `_close_position` |
| HIGH-3 | `src/utils/stats.py` ~L275 | Missing `CREATE INDEX` on `agent_reasoning.ts` — full table scans | ✅ | `idx_reasoning_ts` added; also added `idx_events_exchange` |
| HIGH-4 | `src/dashboard/server.py` ~L273 | API key middleware short-circuits when key not set — all `/api/` open | ➡️ | Warning already logged; dangerous mutation endpoints (rerun) now have per-endpoint auth (CRIT-5). Full enforcement deferred — would break LAN dev setups |
| HIGH-5 | `src/dashboard/deps.py` ~L147 | `_pending_confirmations_lock` shared with rate-limiter dict — coupling + misnamed function | ✅ | `_confirmation_attempts_lock` separate; `prune_expired_rate_entries` renamed |
| HIGH-6 | `src/utils/stats_simulated.py` ~L55 | `close_simulated_trade` updates return dict before commit succeeds | ➡️ | Re-read code: `row.update()` is already after `conn.commit()` — not a bug in normal paths; deferred |
| HIGH-7 | `src/telegram_bot/proactive.py` ~L276 | Full portfolio state sent to cloud LLMs without sanitization | ✅ | `_sanitize_ctx_for_llm()` strips credential-name fields; all financial/trading data kept for LLM quality |
| HIGH-8 | `src/utils/stats_trades.py` ~L180 | `get_events` filters on `exchange` column that doesn't exist in schema | ✅ | Column added to `events` table schema + migration; `record_event` accepts `exchange` param |
| HIGH-9 | `src/core/state.py` ~L733 | `save_state` only persists last 100 trades — open positions lost on restart | ✅ | All open trades outside the last-100 window also persisted |

## MEDIUM

| ID | File | Issue | Status | Commit/Notes |
|----|------|-------|--------|--------------|
| MED-1 | `src/dashboard/deps.py` L217 | `sign_dashboard_command` is dead code — duplicate HMAC impl also in `dashboard_commands.py` | ✅ | Removed from deps.py |
| MED-2 | `src/dashboard/deps.py` L333 | `from datetime import datetime, timezone` at bottom of file | ✅ | Moved to top-level imports |
| MED-3 | `src/utils/stats_portfolio.py` L128 | Silent fallback returns all-exchange data when `exchange` column missing | ✅ | `logger.warning(...)` on fallback |
| MED-4 | `src/dashboard/routes/llm_analytics.py` L179 | Percentile math wrong: `int(n * 0.50)` overshoots | ✅ | Changed to `int((n-1) * p)` |
| MED-5 | `src/utils/stats_reasoning.py` L121 | `GROUP BY t.id` produces duplicate rows for multi-trade cycles | ⏳ | Needs query refactor — deferred |
| MED-6 | `src/telegram_bot/proactive.py` L119 | PnL regex fails for `"PnL: $-50.00"` — sign after currency symbol | ✅ | Named-group regex handles all orderings |
| MED-7 | `src/planning/activities.py` L65 | `open("config/settings.yaml")` uses relative path — breaks in Docker/systemd | ✅ | Uses `__file__`-relative path |
| MED-8 | `src/planning/activities.py` L629 | `plan_json.pop()` mutates caller's input dict | ✅ | `plan_json = dict(plan_json)` copy before pop |
| MED-9 | `src/utils/stats_reasoning.py` L143 | Exchange filter not applied when pair filter is active | ⏳ | Deferred — low impact |
| MED-10 | `src/utils/stats.py` L122 | `_migrate_db` called on same connection as `_init_db` — partial migration un-rollbackable | ✅ | `_migrate_db()` now gets its own `_get_conn()` context |
| MED-11 | `src/telegram_bot/proactive.py` L356 | `$0.00 - $0.00` displayed when no portfolio data | ✅ | Only shows range when `samples > 0` |
| MED-12 | `src/telegram_bot/proactive.py` L438 | Malformed cron expressions silently fall back to hourly | ✅ | `logger.warning(...)` on unrecognised/malformed cron |

## LOW

| ID | File | Issue | Status | Commit/Notes |
|----|------|-------|--------|--------------|
| LOW-1 | `src/utils/stats_trades.py` | `avg_win`/`avg_loss` = 0 returned without flag for degenerate Kelly inputs | ⏳ | |
| LOW-2 | `src/utils/stats_predictions.py` L51 | Stale-price detection breaks on alternating price patterns | ⏳ | |
| LOW-3 | `src/utils/stats_portfolio.py` L217 | `cleanup_bad_snapshots` deletes across all exchanges without filter | ⏳ | |
| LOW-4 | `src/dashboard/routes/planning.py` L79 | `_ALLOWED_WORKFLOW_TYPES` defined inside handler on every request | ✅ | Moved to module level |
| LOW-5 | `src/utils/stats_predictions.py` L641 | `BTC-SEK` misclassified as equity due to `-SEK` suffix heuristic | ⏳ | |
| LOW-6 | `src/telegram_bot/proactive.py` L333 | LLM failures logged at DEBUG; retry storm until window closes | ✅ | `logger.warning(...)` for morning/evening failures; `_last_morning` set before LLM call (already was) |
| LOW-7 | `src/core/state.py` L733 | *(see HIGH-9)* | ✅ | Covered by HIGH-9 fix |
| LOW-8 | `src/telegram_bot/proactive.py` L475 | Events drained before LLM call — lost on LLM failure | ✅ | Snapshot without draining; drain only after successful send |
| LOW-9 | `src/utils/stats_reasoning.py` L143 | *(see MED-9)* | ⏳ | Covered by MED-9 fix (deferred) |
| LOW-10 | `src/utils/stats_trades.py` L233 | `WHERE is_active = 1` integer on PG boolean column | ✅ | Changed to `!= 0` — works for both INTEGER and BOOLEAN |
| LOW-11 | `src/core/orchestrator.py` L11 | `import re` — verify usage in orchestrator body | ➡️ | Pre-existing; not our change to make |
| LOW-12 | `src/planning/activities.py` L629 | *(see MED-8)* | ✅ | Covered by MED-8 fix |
