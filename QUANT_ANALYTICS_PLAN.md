# Quantitative Analytics Implementation Plan

Adds five high-value, gap-filling quantitative capabilities to the
auto-traitor pipeline. All implementations are **pure numpy / stdlib**
(no new heavy deps such as scipy, statsmodels, arch) and slot into the
existing profile-isolated, exchange-scoped data model.

## Scope

| # | Module | Purpose | DB Table |
|---|--------|---------|----------|
| 1 | `src/analysis/market_factors.py` | Multi-factor regression: each tradeable symbol's daily return regressed on SPX, VIX, DXY, BTC factor returns. Outputs per-factor beta, t-stat, residual alpha, idiosyncratic vol. | `market_factor_loadings` |
| 2 | `src/analysis/har_rv.py` | HAR-RV (Heterogeneous Autoregressive Realized Volatility) one-step-ahead forecast from daily/weekly/monthly RV components. | `har_rv_forecasts` |
| 3 | `src/analysis/granger.py` | Granger causality F-test for lead-lag pair validation. | `granger_causality` |
| 4 | `src/analysis/slippage_model.py` | OLS regression of historical fill slippage on (size/ADV, realized vol). Forward-predicts impact for upcoming orders. | `slippage_impact_models` |
| 5 | `src/analysis/correlation_regime.py` | Avg pairwise correlation tracker with rolling-z breakdown detector ("crisis correlation" alarm). | `correlation_regime_events` |

## Integration

| Layer | Change |
|-------|--------|
| **DB** | New mixin `src/utils/stats_quant.py` (`QuantAnalyticsMixin`); wired into `StatsDB` in `src/utils/stats.py`. |
| **Manager** | New `QuantAnalyticsManager` runs nightly (or every N pipeline cycles) inside the planning worker. Fetches universe candles once and cascades into all five analyzers. |
| **Risk** | Optional vol-target enhancement: when `RISK_USE_HAR_RV=1`, `vol_target_multiplier` consumes the HAR-RV forecast instead of trailing realized vol. Backward compatible. |
| **Dashboard backend** | `src/dashboard/routes/quant_analytics.py` exposes 5 read endpoints under `/api/quant/*`. Profile-scoped. |
| **Dashboard frontend** | `dashboard/frontend/src/pages/QuantAnalytics.tsx` (5 tabs). API helpers added to `api.ts`, route to `App.tsx`, nav link in `Layout.tsx`. All `useQuery` calls include `profile` per Domain Separation rule. |
| **Tests** | One unit-test file per analysis module + manager test + dashboard route test. |

## Domain Separation Compliance

- All SQL queries filter by `exchange`.
- All Redis keys (none introduced here) would be profile-prefixed.
- Frontend `useQuery` keys all include `profile`.
- No mixing of crypto + equity data in any view.

## Status Tracker

- [x] Plan markdown
- [x] `market_factors.py`
- [x] `har_rv.py`
- [x] `granger.py`
- [x] `slippage_model.py`
- [x] `correlation_regime.py`
- [x] `stats_quant.py` mixin + wiring
- [x] `quant_analytics_manager.py` + orchestrator hook
- [x] Dashboard route + register
- [x] Frontend page + API + route + nav
- [x] Tests
- [x] Full test suite green
- [x] Code review
- [x] Deploy + monitor logs
