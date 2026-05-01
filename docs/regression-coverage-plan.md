# Regression Coverage for All Followed Assets — Implementation Plan

**Goal:** Every followed asset (human ∪ AI) on both `coinbase` and `ibkr` profiles
has at least one persisted regression model. New follows get a model on the next
nightly cycle (and ideally within minutes via a one-shot trigger).

## Why this is needed
- `event_price_regressions` for `ibkr` only contains the synthetic `_MACRO_` row
  because there are zero `catalyst_events` rows for individual ibkr symbols
  (e.g. `BIOBV.HE-EUR`).
- Even when catalysts exist, no per-symbol *factor* regression is persisted —
  `market_factors.fit_market_factor_models` is invoked in-memory only.
- We need **two complementary regression families** so every followed asset is
  covered:
  1. **Event regressions** (existing) — fitted only when catalysts exist.
  2. **Factor regressions** (new) — vs. macro factors; works for any asset
     with daily candles.

## Scope (locked)
- Followed set = `db.get_followed_pairs_set(exchange=...)` (human ∪ AI).
- Persistence:
  - Events → existing `event_price_regressions`.
  - Factors → new `factor_regressions` table.
- Both crypto and equity profiles.
- Auto-extension on new follow: orchestrator hook fires a one-shot fit.

## Phases
- [x] **Phase 0** — Plan tracker (this file).
- [ ] **Phase 1** — Catalyst backfill for ibkr (earnings + dividends via yfinance).
- [ ] **Phase 2** — `factor_regressions` schema + DAO (`upsert_factor_regression`,
      `get_factor_regressions`, `count_factor_regressions`).
- [ ] **Phase 3** — `run_factor_regressions_for_profile(db, exchange, symbols, ...)`
      helper in `src/analysis/market_factors.py`.
- [ ] **Phase 4** — Activity `run_regressions_for_followed_assets(profile)` in
      `src/planning/activities.py` that:
      - reads followed set
      - calls `run_event_regressions_for_profile(symbols=followed)`
      - calls `run_factor_regressions_for_profile(symbols=followed)`
- [ ] **Phase 5** — Wire into `EventRegressionWorkflow` (replace existing
      activity call) + add a one-shot trigger from
      `orchestrator.add_followed_pair`.
- [ ] **Phase 6** — Dashboard:
      - extend `src/dashboard/routes/regression.py` with coverage endpoint.
      - frontend `Analytics.tsx` shows `coverage_pct` badge.
- [ ] **Phase 7** — Tests:
      - `tests/test_factor_regression_persistence.py`
      - `tests/test_followed_asset_regression_coverage.py`
- [ ] **Phase 8** — Full code review of every diff.
- [ ] **Phase 9** — `pytest tests/ --timeout=60` green; commit; push; redeploy.
- [ ] **Phase 10** — Tail logs; run SQL probe; verify `BIOBV.HE-EUR` has a row.

## Acceptance criteria (every box must tick before task_complete)
1. `pytest tests/ --timeout=60 -q` shows `0 failed`.
2. After deploy + nightly run (or manual one-shot):
   ```sql
   SELECT exchange, COUNT(DISTINCT symbol) AS modeled,
          (SELECT COUNT(*) FROM followed_pairs fp WHERE fp.exchange = e.exchange) AS followed
   FROM (SELECT DISTINCT exchange FROM followed_pairs) e
   LEFT JOIN factor_regressions fr USING (exchange)
   GROUP BY e.exchange;
   ```
   `modeled >= followed` for every exchange.
3. `SELECT * FROM factor_regressions WHERE symbol='BIOBV.HE-EUR'` returns ≥1 row.
4. `podman logs opentraitor-planning | grep -iE "error|exception"` clean.
5. `podman logs opentraitor-agent-coinbase | grep -iE "error|exception"` clean.
6. Dashboard `/api/regression/coverage` returns `coverage_pct == 100` for both
   profiles after a refit.

## Anti-shortcut rules (enforced)
- No `_MACRO_`-only outcome — every followed symbol must have a row.
- No deferred TODOs left in the codebase.
- Auto-follow hook must fire (verified by adding a test follow and seeing a
  log line within 60s).
- Every acceptance criterion verified with a tool call before claiming done.
