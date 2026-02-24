# Auto-Traitor — Solution Intent Document

> **Purpose:** This document defines what the system SHOULD do — the intended behavior, architecture, and design invariants. It is the reference for all implementation work. When the code doesn't match this document, the code is wrong.
>
> **Last updated:** 2026-02-23

---

## 1. What This Is

An autonomous multi-agent trading system that operates two parallel tracks — **Crypto** (Coinbase) and **Equities** (IBKR/Nordnet) — using LLM-powered agents to analyze markets, manage risk, and execute trades with one overriding goal: **make money**.

The system thinks for itself, adapts its own parameters, and optimizes its own behavior. It doesn't need to be told HOW to make money — it figures that out through continuous self-evaluation and adjustment.

---

## 2. Two Parallel Trading Tracks — One Goal

The system operates **two independent trading tracks** that share architecture but run separately:

|                      | **Crypto Track**                                      | **Equity Track**                                        |
| -------------------- | ----------------------------------------------------- | ------------------------------------------------------- |
| **Exchange**         | Coinbase Advanced Trade                               | Interactive Brokers (or Nordnet)                        |
| **Markets**          | Crypto pairs (EUR/EURC quotes)                        | US equities (NYSE/NASDAQ) + Nordic (OMX Stockholm)      |
| **Mode**             | Paper or Live                                         | Paper or Live                                           |
| **Telegram Bot**     | Dedicated bot (own token + chat)                      | Dedicated bot (own token + chat)                        |
| **Dashboard View**   | Crypto-tailored (token quantities, crypto charts)     | Equity-tailored (share counts, equity metrics)          |
| **State & DB**       | Separate StatsDB, separate state file                 | Separate StatsDB, separate state file                   |
| **Config**           | `config/coinbase.yaml`                                | `config/ibkr.yaml` (or `config/nordnet.yaml`)           |

**Both tracks share:** The same agent pipeline, risk framework, LLM provider chain, planning system, and dashboard application. The difference is in the view, the exchange client, the fee model, and asset-class-specific tuning.

**Nordnet** is a disabled alternative to IBKR. If enabled, it replaces IBKR for the equity track (same pipeline, different broker). We don't care which platform handles shares — only that shares get traded.

---

## 3. The Core Trading Loop

Each track runs a perpetual loop (~every 2 minutes). Each cycle:

1. **Discover what to trade** — Scan the exchange for tradeable instruments. Crypto: volume/activity filter → technical screen → LLM screener. Equities: IBKR scanner (TOP_PERC_GAIN, volume) → technical screen → LLM screener.
2. **Analyze each instrument** — Run technical indicators (RSI, MACD, Bollinger, EMA, ADX, volume). Crypto adds: news sentiment, Fear & Greed index. Equities add: IBKR news feed.
3. **Generate signals** — Market Analyst agent combines technical data + context into an LLM call → structured signal (strong_buy → strong_sell, confidence 0–1).
4. **Propose trades** — Strategist agent takes signals + portfolio state → concrete action (buy/sell/hold with amounts, stops, targets).
5. **Validate risk** — Risk Manager agent gates every proposal: Kelly sizing, correlation penalties, ATR stops, portfolio tier limits, AbsoluteRules.
6. **Execute** — Executor places orders (limit for patient entries, market for exits), verifies fills, records everything.
7. **Manage open positions** — Update trailing stops, check tiered partial exits, check stop-losses, run portfolio rotation (crypto) or rebalancing (equities).
8. **Persist and communicate** — Save state, sync to Redis, notify the track's Telegram bot, update dashboard.

Instruments run in parallel within a cycle. The pipeline is sequential per-instrument: Analyst → Strategist → Risk → Executor.

---

## 4. The Agent System

Five agents, each with a single responsibility — but the system's purpose is to **think for itself**:

| Agent                | Job                              | Key Behavior                                                                                                                                                                |
| -------------------- | -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Market Analyst**   | Raw data → Signal                | Combines math indicators + LLM judgment. Degrades to technical-only scoring if LLM unavailable. Asset-class-aware prompts (crypto vs equity language).                      |
| **Strategist**       | Signal → Trade Proposal          | Converts signals into actionable trades. Skips LLM for low-confidence/neutral signals (token savings). Respects planning guidance.                                          |
| **Risk Manager**     | Proposal → Validated Trade       | The gatekeeper. Kelly sizing, correlation penalty, ATR stops, tier limits. **Never blocks sell orders.**                                                                     |
| **Executor**         | Validated Trade → Filled Order   | Places orders, tracks fills with retries, measures slippage. Limit on buys (cheaper fees), market on sells (speed). Handles fractional shares (IBKR).                       |
| **Settings Advisor** | Performance → Self-Tuning        | **The brain of autonomy.** Analyzes its own performance, adjusts parameters to maximize results. Not told HOW to make money — it figures that out.                          |

---

## 5. Autonomous Self-Optimization — The Core Differentiator

The system's defining characteristic: **it thinks for itself and adapts**.

### Settings Advisor

Runs every N cycles and:

- Analyzes recent performance (win rate, P&L, drawdown, Sharpe, trade frequency)
- Evaluates market regime (trending, ranging, volatile, calm)
- Considers what's been working and what hasn't
- **Decides its own optimization profile** — it might prioritize capital preservation in a crash, aggressive growth in a bull run, or risk-adjusted returns in choppy markets
- Proposes parameter changes within guardrailed bounds
- Changes are applied immediately, persisted, and audited

**What it can adjust:**

- Confidence thresholds (how picky to be about entries)
- Stop-loss and take-profit distances
- Trailing stop percentages
- Position sizing parameters
- Cycle interval (trade faster or slower)
- Number of active pairs/instruments
- Strategy weights (more trend-following vs. mean-reversion)
- Risk tolerance levels

**What it cannot touch (guardrails):**

- Mode (paper/live) — human decision
- Trading enabled/disabled — human decision
- Fee model — factual, not tunable
- AbsoluteRules hard limits — safety floor

### Strategic Planning (Temporal Workflows)

Three scheduled workflows provide multi-horizon self-reflection:

| Workflow             | Frequency       | Lookback  | Purpose                                                          |
| -------------------- | --------------- | --------- | ---------------------------------------------------------------- |
| **Daily Plan**       | Midnight UTC    | 7 days    | Tactical: what to focus on today based on recent results         |
| **Weekly Review**    | Monday midnight | 30 days   | Strategic: evaluate weekly performance, adjust themes            |
| **Monthly Review**   | 1st of month    | 90d + YTD | Macro: regime assessment, allocation targets, long-term direction|

Plans are soft guidance that influence the agents' behavior — preferred instruments get easier entry, flagged instruments get harder entry.

### What "Make Money" Means Operationally

The system doesn't have a fixed optimization profile. Instead:

- The **Settings Advisor** analyzes its own results and decides whether to be aggressive, conservative, or balanced
- The **Planning workflows** review performance over 7/30/90 day windows and set strategic direction
- In a bull run: increase position sizes, lower confidence thresholds, trade more frequently
- In a downturn: tighten stops, raise confidence requirements, reduce exposure
- In choppy markets: favor mean-reversion, reduce trade frequency, increase selectivity
- **The measure of success is cumulative P&L over time** — the system picks the path to get there

---

## 6. Risk Management — The Safety Stack

Seven independent layers ensure the system can be aggressive in pursuit of profit while never blowing up:

### Layer 1: AbsoluteRules

Hard limits that NO code path can override:

- Max per-trade cap (configurable per track)
- Daily spend and daily loss caps
- Human approval threshold (configurable — set very high for full autonomy, low for supervision)
- Max trades/day, min time between trades
- Every position must have a stop-loss (max distance configurable)
- Pair/instrument blacklist + whitelist
- Emergency portfolio floor — all trading halts below threshold
- Counters auto-reset daily, survive process restarts via DB seeding

### Layer 2: Risk Manager Agent

- Kelly Criterion position sizing (half-Kelly from historical win rate + W/L ratio)
- Correlation penalty (50% size cut if new position highly correlated with existing)
- ATR-based stops (stop-loss at 2× ATR, take-profit at 3× ATR)
- Portfolio tier limits (max position %, max positions per tier)

### Layer 3: Fee Manager

- Trades must be profitable after 1.5× round-trip fees or they're blocked
- Crypto: percentage-based (taker 0.6%, maker 0.4%)
- Equities: per-share ($0.0035/share, $0.35 min at IBKR) or flat+pct (Nordnet SEK 39 + 0.15%)

### Layer 4: Trailing Stops

- Dynamic stops that follow price up, trigger on reversal
- Default: 3% trail (crypto), 5% trail (equities)
- Managed per-position with thread-safe state

### Layer 5: Tiered Partial Exits

- Lock in gains incrementally: sell 33% at +3%, another 33% at +6%, rest rides the trail
- Prevents "watched a winner turn into a loser" scenarios

### Layer 6: Circuit Breaker

- All trading halts if max drawdown or daily loss limits breached
- Automatic, cannot be overridden by agents

### Layer 7: Emergency Stop

- Portfolio floor (configurable); all trading suspended below threshold
- Disabled for MICRO/SMALL tiers where the floor would exceed portfolio value

**Critical invariant:** Sell/exit orders are NEVER blocked by risk rules. The system can always get out.

---

## 7. Portfolio Management

### Tier-Based Scaling

Adjusts automatically as portfolio grows:

| Tier     | Size     | Max Per Position | Max Positions | Behavior                    |
| -------- | -------- | ---------------- | ------------- | --------------------------- |
| MICRO    | <€50     | 40%              | 2             | Aggressive concentration    |
| SMALL    | <€500    | 25%              | 3             | Moderate concentration      |
| MEDIUM   | <€5K     | 15%              | 5             | Balanced                    |
| LARGE    | <€50K    | 8%               | 8             | Diversified                 |
| WHALE    | >€50K    | 3%               | 10            | Conservative diversification|

### Crypto: Portfolio Rotation

- Autonomous crypto-to-crypto swaps
- Ranks holdings by relative strength
- Routes through optimal path (direct, bridged via EUR/USDC/BTC, or fiat-routed)
- Validates via LLM + AbsoluteRules before executing
- Operates within configurable allocation % (default 10%)

### Equities: Sector Rebalancing

- Rebalances across sectors/themes rather than pair-swapping
- Uses IBKR scanner data for relative strength ranking

---

## 8. Supported Exchanges

| Exchange                    | Asset Class           | Live Trading | Status                                           |
| --------------------------- | --------------------- | ------------ | ------------------------------------------------ |
| **Coinbase Advanced Trade** | Crypto                | Yes          | Fully implemented, production-tested             |
| **Interactive Brokers**     | US + EU Equities      | Yes          | Live code paths exist (`ib_insync`), needs testing |
| **Nordnet**                 | Nordic Equities (OMX) | Disabled     | Alternative to IBKR — replaces it if enabled     |

All implement a common abstract interface (`ExchangeClient`). Only one equity broker is active at a time.

---

## 9. LLM Provider Chain

Multi-provider fallback — the system never stops thinking:

Each provider has:

- Per-provider RPM limits with sliding window
- Daily token budgets
- Cooldown timers with recovery polling
- Automatic re-enable when provider comes back

If ALL providers fail, agents degrade to scoring-based signals with zero LLM cost. The system keeps trading regardless.

---

## 10. Human Interfaces

### Two Telegram Bots (One Per Track)

| Feature              | Crypto Bot                              | Equity Bot                              |
| -------------------- | --------------------------------------- | --------------------------------------- |
| **Token**            | `TELEGRAM_BOT_TOKEN` (Coinbase)         | `TELEGRAM_BOT_TOKEN_IBKR`              |
| **Chat**             | `TELEGRAM_CHAT_ID` (Coinbase)           | `TELEGRAM_CHAT_ID_IBKR`                |
| **Language/Context** | Crypto pairs, token quantities          | Ticker symbols, share counts            |
| **News**             | Reddit + RSS + Fear & Greed             | IBKR news feed                          |

Both provide:

- LLM-powered conversational interface ("sharp pro trader" personality)
- Fast path: pattern-matched instant replies (<100ms) for status, balance, prices, positions
- Smart path: LLM + tool-calling for complex queries
- Proactive alerts: large price moves, stop triggers, trade executions, milestones
- Trade approvals (if enabled): inline keyboard for trades above threshold
- High-Stakes mode: time-limited elevated permissions (2.5× size, lower thresholds, auto-expires)
- Commands: `/status`, `/positions`, `/trades`, `/balance`, `/highstakes`, `/approve`, `/reject`, `/settings`, `/performance`, `/news`

### Dashboard (Single App, Two Views)

One dashboard application with profile-based views:

**Crypto view:** Token quantities, crypto pair charts, portfolio rotation info, Fear & Greed index, crypto news sentiment

**Equity view:** Share counts, ticker symbols, sector allocation, equity-specific metrics, IBKR news

**Shared features:**

- Equity curve and portfolio value tracking
- Trade history with full details
- Agent reasoning traces per cycle (what each agent thought and why)
- Strategic plan viewer (Temporal workflow outputs)
- HITL commands: liquidate, tighten stop, pause instrument (HMAC-signed with nonce replay protection)
- Real-time updates via WebSocket + Redis pub/sub
- Profile switching in the UI

---

## 11. Autonomy Model (Configurable)

The default posture is **autonomous with configurable human gates**:

| Setting              | Autonomous Mode                              | Supervised Mode                             |
| -------------------- | -------------------------------------------- | ------------------------------------------- |
| Trade execution      | Auto-execute all                             | Require approval above threshold            |
| Parameter tuning     | Settings Advisor self-adjusts                | Settings Advisor proposes, human approves   |
| Portfolio rotation   | Auto-rotate                                  | Suggest swaps, await approval               |
| High-stakes          | System can self-activate based on conditions | Human-activated only                        |

The human can always intervene via Telegram or Dashboard regardless of autonomy level. The approval threshold (`require_approval_above`) controls the gate — set it to €999,999 for full autonomy, or €50 for tight supervision.

---

## 12. News & Sentiment

### Crypto Track

- Reddit (5 crypto subs) + RSS (4 crypto news sites), polled every 5 min
- Fear & Greed Index (alternative.me), 10-min cache, 7-day trend
- Sentiment scoring [-1.0, 1.0] injected into agent prompts
- Breaking news triggers early pipeline wake via Redis pub/sub

### Equity Track

- IBKR native news feed (built-in API)
- Fear & Greed disabled (crypto-specific index)
- Equity-specific sentiment analysis (earnings, macro indicators — to be built)

---

## 13. Universe Discovery & Instrument Selection

### 3-Stage Funnel
Stage 1: Universe Refresh (periodic)
├── Crypto: exchange.discover_all_pairs() filtered by quote currencies
├── Equities: IBKR ScannerSubscription (TOP_PERC_GAIN on STK.US.MAJOR)
└── Pre-filter by 24h volume + price activity

Stage 2: Technical Screen (pure math, zero LLM cost)
├── RSI, ADX, MACD, Volume profile, EMA alignment, Bollinger position
├── Score and rank all candidates
└── Output: technically-scored shortlist

Stage 3: LLM Screener (top-N selection)
├── LLM evaluates macro factors, narrative, catalysts
├── Selects final top-N instruments for active trading
└── Output: active trading list for this cycle


### Discovery Modes

| Mode         | Behavior                                                        |
| ------------ | --------------------------------------------------------------- |
| `all`        | Discover all instruments, run through full funnel               |
| `manual`     | Trade only explicitly listed instruments                        |
| `quote_only` | Trade all pairs for configured quote currencies without screening |

---

## 14. Strategies

### EMA Crossover (55% ensemble weight)

- **Type:** Trend-following
- **Buy:** EMA-50 crosses above EMA-200 + ADX > 25 + above-average volume
- **Sell:** EMA-50 crosses below EMA-200 + same confirmations
- **Stops:** ATR-based (2× ATR stop-loss, 3× ATR take-profit)

### Bollinger Band Reversion (45% ensemble weight)

- **Type:** Mean-reversion
- **Buy:** Price at lower Bollinger Band + RSI < 30
- **Sell:** Price at upper Bollinger Band + RSI > 70
- **Disabled when:** ADX > 25 (trending market — mean reversion fails in trends)
- **Confirmation:** Stochastic RSI + OBV divergence

### Ensemble Scoring

The pipeline runs both strategies, then combines:
ensemble_score = 0.55 × ema_signal + 0.45 × bollinger_signal

This blended score feeds the Market Analyst alongside raw indicator data.

### Pairs Correlation Monitor (Informational)

- Monitors rolling correlations between instruments
- Z-score divergence detection
- Feeds correlation data to Risk Manager for penalty calculation

---

## 15. Position Lifecycle

### Opening

1. Universe scan selects instrument for pipeline
2. Technical indicators computed (no LLM cost)
3. Strategy ensemble produces combined signal
4. Market Analyst (LLM) generates Signal (type, confidence, entry/SL/TP)
5. Strategist (LLM) proposes Trade (action, amount, stops, targets)
6. Risk Manager (LLM) validates + sizes (Kelly, correlation, ATR, tier limits, AbsoluteRules)
7. Human approval if configured and above threshold
8. Fee Manager validates profitability after fees
9. Executor places order (limit for patient buys, market for urgent/sells)
10. Fill verification with exponential backoff
11. State + DB + Journal + Audit recording
12. Trailing stop initialized
13. Telegram notification sent

### Managing

Each cycle for every open position:

- Price updated (WebSocket real-time + REST cycle refresh)
- Trailing stop updated (follows price up)
- Tiered partial exits checked (+3%, +6%)
- Stop-loss checked (hard exit)
- Portfolio rotation/rebalance candidates evaluated
- Circuit breaker checked

### Closing (7 Mechanisms)

| Mechanism             | Trigger                                     | Order Type    |
| --------------------- | ------------------------------------------- | ------------- |
| **Trailing Stop**     | Price reverses beyond trail distance         | Market order  |
| **Tiered Partial**    | Position reaches +3%, +6% thresholds        | Market order  |
| **Stop-Loss**         | Price drops below stop-loss level            | Market order  |
| **Take-Profit**       | Price reaches take-profit target             | Market order  |
| **Agent Sell Signal** | Pipeline generates sell recommendation       | Market order  |
| **Manual Liquidation**| Telegram or dashboard HITL command           | Market order  |
| **Circuit Breaker**   | Max drawdown or daily loss exceeded          | All positions |

---

## 16. Data & Persistence

| Store                   | What                                              | Why                                 |
| ----------------------- | ------------------------------------------------- | ----------------------------------- |
| **SQLite (StatsDB)**    | Trades, snapshots, reasoning, events, plans       | Permanent analytics and history     |
| **JSON state files**    | Runtime state (positions, cash, signals)          | Warm-start on restart               |
| **Redis**               | State sync, approvals, news cache, commands       | Inter-process communication         |

Each track gets its own StatsDB (`stats_coinbase.db`, `stats_ibkr.db`) and state file (`data/coinbase/trading_state.json`, `data/ibkr/trading_state.json`).

---

## 17. Graceful Degradation

The core trading loop has **one hard dependency: exchange connectivity**. Everything else is optional:

| Component Down     | Impact                                        |
| ------------------ | --------------------------------------------- |
| All LLM providers  | Technical-only signals (scoring-based)         |
| Redis              | Trading continues, dashboard/sync disabled     |
| Temporal           | Trading continues, no strategic planning       |
| News aggregator    | Trading continues, no sentiment context        |
| Telegram           | Trading continues, no notifications            |
| Dashboard          | Trading continues, no web monitoring           |

---

## 18. Infrastructure
┌──────────────────────┐ ┌──────────────────────┐
│ Crypto Trading Bot │ │ Equity Trading Bot │
│ (main.py --profile │ │ (main.py --profile │
│ coinbase) │ │ ibkr) │
│ │ │ │
│ Orchestrator │ │ Orchestrator │
│ All Agents │ │ All Agents │
│ Crypto Telegram Bot │ │ Equity Telegram Bot │
│ News (Reddit+RSS) │ │ News (IBKR feed) │
│ WebSocket Feed │ │ │
└──────────┬───────────┘ └──────────┬────────────┘
│ │
└──────── Redis ◄─────────┘
│
┌─────────────┼─────────────┐
│ │ │
┌────▼────┐ ┌─────▼─────┐ ┌───▼───────────┐
│Dashboard│ │ Planning │ │ Ollama │
│(FastAPI)│ │ Worker │ │ (local LLM) │
│ │ │ (Temporal)│ │ │
└─────────┘ └───────────┘ └───────────────┘


Two bot processes (one per track), one shared dashboard, one planning worker, shared Redis and Ollama.

---

## 19. Design Invariants (Must Always Hold)

1. AbsoluteRules cannot be bypassed by any code path
2. Sell/exit orders are never blocked by risk rules
3. Every position has a stop-loss
4. Fees are accounted for before every trade
5. Daily counters survive process restarts
6. State is persisted every cycle (crash recovery)
7. All LLM calls are traced (Langfuse)
8. All decisions are audited (reasoning stored in DB)
9. Crypto and Equity tracks are fully isolated (separate state, DB, Telegram, dashboard view)
10. The system degrades gracefully — never crashes because an optional service is down
11. The system optimizes itself — Settings Advisor and Planning continuously adapt toward profitability
12. Nordnet and IBKR are interchangeable for the equity track — the system doesn't care which broker handles shares

---

## 20. Technical Stack

| Component      | Technology                                            |
| -------------- | ----------------------------------------------------- |
| Language       | Python 3.11+ (async)                                 |
| Exchange APIs  | Coinbase REST+WS, `ib_insync` (IBKR), Nordnet REST   |
| LLM            | OpenRouter, Gemini, OpenAI, Ollama (multi-fallback)   |
| Orchestration  | Temporal.io (planning workflows)                      |
| Dashboard      | FastAPI (REST+WS) + React/TypeScript/Vite/TailwindCSS |
| Messaging      | python-telegram-bot                                   |
| Cache/Pub-Sub  | Redis                                                 |
| Database       | SQLite (per-profile StatsDB)                          |
| Observability  | Langfuse (LLM tracing), structured logging            |
| Deployment     | Docker Compose                                        |