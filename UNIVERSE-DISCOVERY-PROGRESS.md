# Autonomous Full-Universe Discovery + LLM-Efficient Scanning + Crypto-to-Crypto

## Status: ✅ COMPLETE — Ready for Deploy

---

## Architecture

4-stage funnel that discovers ALL Coinbase pairs with pure math (zero LLM cost), uses ONE LLM call to screen the top candidates, and only runs full 4-agent LLM pipelines for the selected 3-5 pairs.

```
Stage 1 — Universe Discovery (1 API call, 0 LLM)
    ↓ ~600+ products
Stage 2 — Technical Screen (~30-50 API calls for candles, 0 LLM)
    ↓ filter by volume + movement → fetch candles → TechnicalAnalyzer + strategies → composite score
Stage 3 — LLM Screener (0 API calls, 1 LLM call)
    ↓ compact table → single LLM picks top N
Stage 4 — Active Pipeline (N pairs × 4 LLM agents)
    ↓ only 3-5 pairs run MarketAnalyst → Strategist → RiskManager → Executor
```

---

## Files Modified

### ✅ `config/settings.yaml`
- Added: `include_crypto_quotes`, `pair_universe_refresh_seconds`, `max_active_pairs`, `scan_volume_threshold`, `scan_movement_threshold_pct`, `screener_interval_cycles`

### ✅ `src/utils/settings_manager.py`
- Extended `_TRADING_SCHEMA` with new fields
- Added autonomous field guards for `max_active_pairs`, `include_crypto_quotes`, `scan_volume_threshold`, `scan_movement_threshold_pct`, `screener_interval_cycles`
- Blocked `pair_universe_refresh_seconds` from autonomous changes

### ✅ `src/core/coinbase_client.py`
- Added `_product_cache` with 10-minute TTL for efficient universe refresh
- Added `discover_all_pairs_detailed()` — returns full product metadata including volume, price change, crypto-to-crypto pairs
- Added `find_direct_pair(base, quote)` — finds direct crypto-to-crypto pairs (e.g. ETH-BTC) for single-order swaps

### ✅ `src/utils/stats.py`
- Added `scan_results` table to SQLite schema
- Added `save_scan_results()` — persists scan data with universe size, top movers, summary text
- Added `get_latest_scan_results()` — retrieves latest scan for planning/dashboard injection

### ✅ `src/core/ws_feed.py`
- Added `update_subscriptions(new_product_ids)` — dynamically changes WebSocket subscriptions when screener selects new pairs

### ✅ `src/core/orchestrator.py`
- Added universe tracking instance variables (pair_universe, scan_results, screener state)
- Modified `run_forever()` main loop: funnel runs before pipelines each cycle
- Added `_refresh_pair_universe()` — cached product catalog refresh
- Added `_run_universe_scan()` — technical screen with composite scoring (RSI, ADX, MACD, volume, EMA, Bollinger)
- Added `_run_llm_screener()` — single LLM call to select top-N pairs from scan table
- Added `_get_scan_summary()` — human-readable summary for advisor/planning injection
- Updated `_run_rotation()` — passes scan results to rotator for ranking boost
- Injected scan data into settings advisor context

### ✅ `src/agents/settings_advisor.py`
- Replaced PAIR MANAGEMENT prompt section with universe-aware prompts
- Added `{scan_summary}` template variable to system prompt
- Injected scan summary + universe size into user message

### ✅ `src/core/portfolio_rotator.py`
- Added `scan_results` parameter to `evaluate_rotation()` — boosts rankings with scan data (20% weight blend)
- Added direct pair routing in `execute_swap()` — checks `find_direct_pair()` before fiat routing, saves one leg of fees

### ✅ `src/planning/activities.py`
- Added `fetch_pair_universe` activity — queries Coinbase product catalog for planning LLM
- Added `fetch_universe_scan_summary` activity — retrieves latest scan from StatsDB

### ✅ `src/planning/workflows.py`
- Wired `fetch_universe_scan_summary` into DailyPlanWorkflow
- Wired `fetch_pair_universe` + `fetch_universe_scan_summary` into WeeklyReviewWorkflow

### ✅ `src/planning/worker.py`
- Registered both new activities in the Temporal worker

---

## New Settings

| Field | Default | Description |
|---|---|---|
| `include_crypto_quotes` | `true` | Include crypto-to-crypto pairs (e.g. ETH-BTC) in universe |
| `pair_universe_refresh_seconds` | `1800` | How often to refresh full product catalog (30min) |
| `max_active_pairs` | `5` | Max pairs for LLM screener to select |
| `scan_volume_threshold` | `1000` | Min 24h volume to enter technical screen |
| `scan_movement_threshold_pct` | `1.0` | Min absolute 24h % move to enter screen |
| `screener_interval_cycles` | `5` | LLM screener runs every N cycles |

---

## LLM Cost Model

| Component | LLM Calls | API Calls |
|---|---|---|
| Universe refresh | 0 | 1 (products list) |
| Technical screen | 0 | ~30-50 (candles) |
| LLM screener | 1 | 0 |
| Active pipelines | N × 4 | N × candles |
| **Total per cycle** | **1 + (N × 4)** | **~35-55** |

At N=5 active pairs: 21 LLM calls/cycle (vs scanning 600+ pairs individually = 2400+ calls).
