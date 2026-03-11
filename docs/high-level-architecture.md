# High-Level Architecture

## Overview

OpenTraitor is an autonomous, multi-asset trading system powered by LLM-driven decision-making. It operates as a Docker Compose stack of 12+ services, trading both cryptocurrency (via Coinbase) and US/EU equities (via Interactive Brokers) through domain-isolated agent instances.

The system follows four core design principles:

1. **Safety First** — Absolute rules engine enforces hard limits that no agent, LLM, or user command can override.
2. **Domain Separation** — Crypto and equity trading are fully isolated at every layer (DB queries, Redis keys, dashboard UI, news feeds).
3. **Multi-Agent Pipeline** — Four specialized LLM agents process each trade sequentially: analysis → strategy → risk → execution.
4. **Graceful Degradation** — Multi-provider LLM fallback chain, optional infrastructure (Redis, Langfuse, Temporal), and paper trading mode.

---

## System Diagram

```
                            ┌─────────────────┐
                            │   Telegram Bot   │ ◄── Owner (mobile)
                            │  (Conversational │
                            │   LLM Agent)     │
                            └────────┬─────────┘
                                     │
┌────────────────────────────────────┼────────────────────────────────────┐
│                           ORCHESTRATOR                                  │
│                                    │                                    │
│  ┌─────────────┐   ┌──────────────┴──────────────┐   ┌──────────────┐ │
│  │  Universe    │   │     AGENT PIPELINE           │   │  Portfolio   │ │
│  │  Scanner     │   │                              │   │  Rotator    │ │
│  │  (Pair       │   │  ┌────────────────────────┐  │   │  (Crypto    │ │
│  │  Discovery)  │   │  │ 1. Market Analyst      │  │   │   Swaps)    │ │
│  │             │   │  │    • Technical (RSI,    │  │   │             │ │
│  │             │   │  │      MACD, BB, EMA)     │  │   │  Route      │ │
│  │             │   │  │    • Sentiment          │  │   │  Finder     │ │
│  │             │   │  │    • Fear & Greed       │  │   │  (Swap      │ │
│  │             │   │  │    • Multi-Timeframe    │  │   │   Paths)    │ │
│  │             │   │  ├────────────────────────┤  │   │             │ │
│  │             │   │  │ 2. Strategist           │  │   └──────────────┘ │
│  │             │   │  │    • LLM trade proposal │  │                    │
│  │             │   │  │    • Strategy ensemble  │  │   ┌──────────────┐ │
│  │             │   │  │    • Task alignment     │  │   │  Trailing    │ │
│  │             │   │  ├────────────────────────┤  │   │  Stop Mgr    │ │
│  │             │   │  │ 3. Risk Manager         │  │   │  (Dynamic    │ │
│  │             │   │  │    • AbsoluteRules      │  │   │   Exits)     │ │
│  └─────────────┘   │  │    • Kelly Criterion    │  │   │             │ │
│                     │  │    • ATR volatility     │  │   └──────────────┘ │
│  ┌─────────────┐   │  │    • Correlation adj.   │  │                    │
│  │  Settings   │   │  ├────────────────────────┤  │   ┌──────────────┐ │
│  │  Advisor    │   │  │ 4. Executor             │  │   │  Fee         │ │
│  │  (Auto-     │   │  │    • Market/limit order │  │   │  Manager     │ │
│  │  Tuning)    │   │  │    • Fee check          │  │   │  (Breakeven  │ │
│  │             │   │  │    • Order tracking     │  │   │   Guard)     │ │
│  └─────────────┘   │  └────────────────────────┘  │   └──────────────┘ │
│                     │                              │                    │
│                     └──────────────────────────────┘                    │
│                                                                        │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │                     ABSOLUTE RULES ENGINE                        │  │
│  │  max_single_trade • max_daily_spend • max_daily_loss             │  │
│  │  max_trades_per_day • always_use_stop_loss • never_trade_pairs   │  │
│  │  require_approval_above • emergency_stop_portfolio               │  │
│  │  ─── CANNOT BE OVERRIDDEN BY ANY AGENT, LLM, OR COMMAND ───     │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
         │                    │                    │
         ▼                    ▼                    ▼
 ┌───────────────┐  ┌─────────────────┐  ┌─────────────────┐
 │ Coinbase API  │  │ IB Gateway/TWS  │  │  Yahoo Finance  │
 │ REST + WS     │  │ (Equities)      │  │  (IBKR Paper)   │
 └───────────────┘  └─────────────────┘  └─────────────────┘
```

---

## Infrastructure Stack

```
┌──────────────────────────────────────────────────────────────┐
│                    Docker Compose Services                    │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  TRADING                    STORAGE                          │
│  ├─ agent-coinbase          ├─ traitor-db (PostgreSQL 16)    │
│  ├─ agent-ibkr (profile)    ├─ redis (7-alpine)             │
│  └─ news-worker             └─ ollama (GPU, RTX 5080)       │
│                                                              │
│  DASHBOARD                  OBSERVABILITY                    │
│  └─ dashboard (FastAPI      ├─ langfuse-web (v3)             │
│     + Vue 3 SPA, :8090)     ├─ langfuse-worker               │
│                              ├─ langfuse-db (PostgreSQL)     │
│  PLANNING                   ├─ clickhouse (OLAP traces)      │
│  ├─ temporal (auto-setup)   └─ minio (S3 blob store)        │
│  └─ temporal-worker                                          │
│     temporal-db (PostgreSQL)                                 │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

| Service | Port | Purpose |
|---------|------|---------|
| `ollama` | 11434 | GPU-accelerated local LLM inference |
| `redis` | 6380 | State sync, caching, pub/sub messaging |
| `traitor-db` | 5432 (internal) | Trading stats, trades, portfolio snapshots |
| `agent-coinbase` | 8080 | Crypto trading agent + health check |
| `agent-ibkr` | 8082 | Equity trading agent (optional profile) |
| `dashboard` | 8090 | Web UI + REST API |
| `news-worker` | — | Background news aggregation daemon |
| `temporal` | 7233 | Workflow engine gRPC endpoint |
| `langfuse-web` | 3000 | LLM trace observability UI |
| `clickhouse` | 8123/9000 | OLAP store for Langfuse traces |
| `minio` | 9090/9091 | S3-compatible blob store for Langfuse |

---

## Trading Pipeline (Per Cycle)

Each cycle runs every ~120 seconds (crypto) or ~300 seconds (equities):

```
1. FETCH          Candles (REST) + real-time prices (WebSocket)
                  + news articles (Redis cache) + Fear & Greed Index
       │
2. ANALYZE        Technical indicators (RSI, MACD, BB, EMA)
                  + multi-timeframe alignment (1h, 4h, 1d, 1w)
                  + sentiment scoring + strategic context (Temporal plans)
       │
3. STRATEGIZE     LLM generates trade proposal (buy/sell/hold)
                  + deterministic strategy ensemble (EMA + Bollinger)
                  + adaptive learning weight adjustment
       │
4. RISK CHECK     AbsoluteRules validation (hard limits)
                  + Kelly Criterion position sizing (half-Kelly)
                  + ATR volatility adjustment
                  + correlation penalty for correlated holdings
                  + signal-strength multiplier + tier-based scaling
       │
5. FEE CHECK      Expected gain > fees × safety_margin (1.5×)
       │
6. APPROVAL       Auto-approve below threshold, Telegram above
       │
7. EXECUTE        Market or limit order via exchange API
       │
8. POST-TRADE     Trailing stop setup + journal + audit log
                  + portfolio rotation check + stop-loss monitoring
                  + prediction tracking for calibration
```

---

## Multi-Agent System

Five specialized agents, each inheriting from `BaseAgent`:

| Agent | Priority | Input | Output |
|-------|----------|-------|--------|
| **Market Analyst** | high | Candles, news, Fear & Greed, multi-TF context | `Signal` (type, confidence, market condition, entry/stop/TP) |
| **Strategist** | high | Signal, portfolio, tasks, recent outcomes, context | Trade proposal (action, pair, amount, reasoning) |
| **Risk Manager** | high | Trade proposal, holdings, portfolio metrics | Approved trade (adjusted size) or rejection |
| **Executor** | normal | Approved trade | Filled order with fees, PnL |
| **Settings Advisor** | low | Volatility, performance, pair quality, portfolio metrics | Parameter change proposals with guardrails |

All agents use the **LLM fallback chain**: OpenRouter → Groq → Ollama (local). High-priority agents always attempt cloud providers first; low-priority agents prefer local when on free-tier plans.

---

## Data Architecture

### PostgreSQL (Primary Store)

```
trades               Portfolio snapshots, daily P&L, reasoning samples,
portfolio_snapshots   prediction tracking, strategic context, events,
strategic_context     simulated trades — all filtered by exchange column
daily_summaries       for domain separation (WHERE exchange IN (%s, %s))
events
prediction_tracking
reasoning_samples
simulated_trades
```

### Redis (Ephemeral State)

```
{profile}:trailing_stops:state     Trailing stop positions
{profile}:dashboard:commands_queue  Pending dashboard commands
{profile}:news:latest              Cached news articles
{profile}:news:watched_tickers     Dynamic ticker match list
sessions:*                         Dashboard login sessions
rate_limit:*                       API rate limit counters
```

All Redis keys are prefix-scoped by profile (`coinbase:` or `ibkr:`).

### File System (Warm-Start & Audit)

```
data/{profile}/trading_state.json   Thread-safe state snapshot
data/{profile}/journal/             Trade journal (JSONL + CSV)
data/{profile}/audit/               Hash-chained audit log
data/{profile}/training/            LLM fine-tuning data
data/{profile}/finetuning/          Processed training sets
logs/                               Structured application logs
```

---

## Domain Separation

Crypto (Coinbase) and equity (IBKR) trading run as independent agent instances sharing the same codebase but never sharing data:

| Layer | Mechanism |
|-------|-----------|
| **Process** | Separate Docker containers (`agent-coinbase`, `agent-ibkr`) |
| **Config** | Separate YAML files (`coinbase.yaml`, `ibkr.yaml`) |
| **SQL** | All queries filter `WHERE exchange IN (profile, profile_paper)` |
| **Redis** | All keys prefix-scoped: `{profile}:key_name` |
| **Dashboard** | Profile toggle (Crypto/Equities), all `useQuery` keys include `profile` |
| **News** | Separate ticker watch lists per profile |
| **Planning** | Strategic context fetched and stored per profile |
| **Tests** | `test_domain_separation.py` enforces all rules; pre-commit hook blocks violations |

---

## Security Model

| Control | Implementation |
|---------|---------------|
| **Telegram Auth** | Numeric user ID allowlist (`TELEGRAM_AUTHORIZED_USERS`); agent refuses to start without it |
| **Dashboard Auth** | bcrypt password + optional TOTP 2FA + backup codes |
| **Request Signing** | HMAC-SHA256 with nonce replay protection for dashboard commands |
| **Rate Limiting** | Token-bucket per tier (free/paid) + per-provider RPM limits |
| **Container Hardening** | Read-only FS, `no-new-privileges`, non-root, resource limits |
| **Input Sanitization** | HTML escape + length limits on all user input before LLM |
| **Secrets** | All via environment variables; never hardcoded; Redis dangerous commands disabled |
| **Audit Trail** | Hash-chained tamper-evident log of all critical operations |
| **Pre-Commit** | `test_domain_separation.py` + `test_security.py` block broken commits |

---

## LLM Provider Chain

```
Request → OpenRouter (llama-3.3-70b, free tier, 20 RPM)
              │ rate-limited / error
              ▼
          Groq (llama-3.3-70b, free tier, 30 RPM)
              │ rate-limited / error
              ▼
          Ollama (llama3.1:8b, local GPU, unlimited)
```

**Tier-aware routing:**
- **Free tier:** Only high-priority calls (strategist, risk_manager, portfolio_rotator, telegram) try cloud; low-priority (settings_advisor) goes straight to local.
- **Paid tier:** All calls try cloud first.
- **Cooldown:** On rate-limit, provider enters 60–120s cooldown; recovery polling re-enables.
- **Token budgets:** Per-provider daily token limits with day-rollover detection.

---

## Planning System (Temporal)

Three Temporal workflows run on schedule:

| Workflow | Schedule | Scope |
|----------|----------|-------|
| `DailyPlanWorkflow` | 00:00 UTC daily | Evaluate yesterday, set today's focus areas, regime assessment |
| `WeeklyReviewWorkflow` | 00:00 UTC Monday | 7-day retrospective, correlation analysis, strategic directions |
| `MonthlyReviewWorkflow` | 00:00 UTC 1st | Full month retrospective, strategy rebalancing |

Plans are written to `strategic_context` in PostgreSQL and injected as soft-prompt context into agents each cycle. Agents adapt to regime without being locked into decisions.

---

## Observability

| Tool | Purpose |
|------|---------|
| **Langfuse v3** | Full LLM observability — prompts, responses, token counts, latency, costs |
| **Health Endpoints** | `/health` on each agent (8080/8082) — uptime, last cycle, rules status |
| **Trade Journal** | JSONL + CSV record of every decision (idea → signal → execution → outcome) |
| **Audit Log** | Hash-chained tamper-evident record of trades, approvals, parameter changes |
| **Prediction Tracking** | Signal accuracy by type, pair, and regime — feeds adaptive learning |
| **Telegram Proactive Alerts** | Real-time notifications for fills, milestones, violations, failovers |
