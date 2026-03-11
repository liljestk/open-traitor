<p align="center">
  <img src="dashboard/frontend/public/logo.png" alt="OpenTraitor" width="180" />
</p>

<h1 align="center">OpenTraitor</h1>
<p align="center"><strong>Autonomous Multi-Asset LLM Trading System</strong></p>

<p align="center">
An autonomous, LLM-powered trading system supporting <strong>cryptocurrency</strong> (Coinbase) and <strong>equities</strong> (Interactive Brokers). Uses a multi-provider LLM fallback chain (OpenRouter → Groq → Ollama) with optional GPU-accelerated local inference. Runs on <strong>Windows</strong>, <strong>macOS</strong> (Apple Silicon), and <strong>Linux</strong>. Features a multi-agent pipeline, <strong>strict domain separation</strong> between asset classes, <strong>Temporal-orchestrated planning</strong>, a <strong>real-time dashboard</strong>, <strong>backtesting with walk-forward optimization</strong>, and <strong>conversational Telegram control</strong>.
</p>

> [!WARNING]
> **This platform autonomously trades crypto and stocks with real money.** It is experimental, unproven, and **you will likely lose more money than you gain**. If you choose to try it, **create dedicated Coinbase and Interactive Brokers accounts solely for this system** — never connect it to accounts holding funds you can't afford to lose. Use at your own risk. See [Disclaimer](#%EF%B8%8F-disclaimer).

> **Full architecture docs:** [`docs/high-level-architecture.md`](docs/high-level-architecture.md) &nbsp;|&nbsp; **Decision log:** [`docs/ADR/`](docs/ADR/)

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          DOCKER COMPOSE (17 services)               │
│                                                                     │
│  ┌───────────┐ ┌───────────┐ ┌────────────┐ ┌───────────────────┐  │
│  │  Ollama   │ │   Redis   │ │ PostgreSQL │ │    Langfuse v3    │  │
│  │ (Local    │ │ (Cache &  │ │ (Trading   │ │  (LLM Traces)    │  │
│  │  LLM)     │ │  State)   │ │  Stats DB) │ │  +ClickHouse     │  │
│  └─────┬─────┘ └─────┬─────┘ └─────┬──────┘ │  +MinIO          │  │
│        │              │             │        └────────┬──────────┘  │
│  ┌─────┴──────────────┴─────────────┴─────────────────┴──────────┐  │
│  │                     TRADING AGENTS                             │  │
│  │                                                                │  │
│  │  ┌─────────────────┐       ┌─────────────────┐                │  │
│  │  │ agent-coinbase  │       │   agent-ibkr    │                │  │
│  │  │ (Crypto)        │       │   (Equities)    │                │  │
│  │  │ ════════════    │       │   ════════════  │                │  │
│  │  │ Domain-isolated │       │ Domain-isolated │                │  │
│  │  └────────┬────────┘       └────────┬────────┘                │  │
│  │           │      SHARED PIPELINE     │                        │  │
│  │  ┌────────┴──────────────────────────┴─────────────────────┐  │  │
│  │  │               ORCHESTRATOR                               │  │  │
│  │  │                                                          │  │  │
│  │  │  Market Analyst → Strategist → Risk Mgr → Executor      │  │  │
│  │  │       │                           │                      │  │  │
│  │  │  Technical    ┌──────────────┐    AbsoluteRules          │  │  │
│  │  │  +Sentiment   │  Absolute    │    +Kelly/ATR             │  │  │
│  │  │  +Fear&Greed  │   Rules      │    +Correlation           │  │  │
│  │  │  +Multi-TF    │ (NEVER BREAK)│         │                 │  │  │
│  │  │  +Indicators  └──────────────┘    Fee Manager            │  │  │
│  │  │                                        │                 │  │  │
│  │  │  ┌──────────────┐ ┌────────────────┐   │                 │  │  │
│  │  │  │  Portfolio   │ │ Trailing Stop  │   │                 │  │  │
│  │  │  │  Rotator +   │ │   Manager      │   │                 │  │  │
│  │  │  │  Rot. Exec.  │ └────────────────┘   │                 │  │  │
│  │  │  └──────────────┘                      │                 │  │  │
│  │  │  ┌──────────────┐ ┌────────────────┐   │                 │  │  │
│  │  │  │  Context     │ │  Holdings      │   │                 │  │  │
│  │  │  │  Manager     │ │  Manager       │   │                 │  │  │
│  │  │  └──────────────┘ └────────────────┘   │                 │  │  │
│  │  └────────────────────────────────────────┼─────────────────┘  │  │
│  │                                           │                    │  │
│  │  ┌──────────┐  ┌────────────┐   ┌────────┴──────────┐        │  │
│  │  │ Telegram │  │  WebSocket │   │  Exchange APIs    │        │  │
│  │  │ Bot 📱   │  │  Feeds     │   │ Coinbase / IBKR   │        │  │
│  │  │ +Persona │  └────────────┘   └───────────────────┘        │  │
│  │  │ +Tools   │                                                 │  │
│  │  └──────────┘                                                 │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────┐    │
│  │  Dashboard   │  │ News Worker  │  │  Temporal + Worker     │    │
│  │  (FastAPI +  │  │ (Reddit/RSS/ │  │  (Planning Workflows)  │    │
│  │   Vue 3 SPA) │  │  IBKR News)  │  │  Daily/Weekly/Monthly  │    │
│  │  :8090       │  │              │  │  :7233 / :8233         │    │
│  └──────────────┘  └──────────────┘  └────────────────────────┘    │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                META-LEARNING & OPTIMIZATION                  │   │
│  │  Signal Scorecard • Confidence Calibrator • Ensemble Opt.   │   │
│  │  Prompt Evolver • Auto WFO • LLM Optimizer • QC Filter      │   │
│  │  Backtesting Engine • Walk-Forward Optimization              │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

## ✨ Key Features

### Trading Engine
- **🧠 Multi-Provider LLM** — OpenRouter → Groq → Ollama fallback chain with tier-aware routing and RPM budget management
- **🔒 Absolute Rules** — Hard limits that can NEVER be broken (max spend, daily loss, etc.)
- **📊 Multi-Agent Pipeline** — Market Analyst → Strategist → Risk Manager → Executor
- **🎯 Position Sizing** — Kelly Criterion, ATR volatility adjustment, correlation penalties
- **💰 Fee-Aware Trading** — Only trades when expected gain exceeds fees × safety margin
- **🎯 Trailing Stop-Loss** — Dynamic stops that lock in profits as price moves
- **🔄 Portfolio Rotation** — Autonomous crypto-to-crypto swaps with dedicated rotation executor and route finder
- **⚡ High-Stakes Mode** — Owner-activated time-limited elevated trading via Telegram
- **📝 Paper Trading** — Full simulation with realistic fee modeling before going live
- **🕐 Market Hours** — Exchange-aware scheduling (equity market sessions, crypto 24/7)

### Analysis & Intelligence
- **📡 WebSocket Feed** — Real-time prices via Coinbase WebSocket (low latency)
- **📊 Multi-Timeframe Analysis** — 1h, 4h, 1d, 1w confluence scoring
- **📈 Strategy Ensemble** — EMA Crossover + Bollinger Reversion with adaptive weighting
- **😱 Fear & Greed Index** — Crypto sentiment from alternative.me
- **📰 News Aggregation** — Reddit, RSS (CoinTelegraph, CoinDesk), IBKR news, ticker-specific matching
- **🧪 Adaptive Learning Engine** — Tracks prediction accuracy, adjusts strategy weights via signal scorecard
- **📐 Technical Indicators** — RSI, MACD, Bollinger Bands, EMA, custom indicator library
- **🔬 Pairs Monitor** — Correlation divergence detection between assets

### Meta-Learning & Optimization
- **🎯 Signal Scorecard** — Scores agent predictions against actual price movements
- **📊 Confidence Calibrator** — Calibrates prediction confidence using historical accuracy
- **⚖️ Ensemble Optimizer** — Optimizes strategy weights based on rolling performance
- **🧬 Prompt Evolver** — LLM-driven meta-learning that evolves agent prompts from prediction patterns
- **🔄 Auto Walk-Forward Optimization** — Production parameter tuning with automatic promotion and rollback
- **🧠 LLM Optimizer** — Tunes LLM parameters (temperature, model selection) based on outcome data
- **🔍 Quality Control Filter** — Filters low-quality signals before execution
- **📈 Backtesting Engine** — Full backtest framework with cost sensitivity analysis
- **📊 Walk-Forward Optimization** — Out-of-sample validation for strategy parameters
- **🎓 Fine-Tuning Pipeline** — Captures training data for LLM fine-tuning

### Operations & Observability
- **📱 Telegram Bot** — Conversational LLM-powered control with persona system, tool-use, and proactive alerts
- **🖥️ Real-Time Dashboard** — Vue 3 SPA with 15+ pages: analytics, cycle explorer, predictions, risk exposure, LLM analytics, and more
- **📅 Temporal Planning** — Daily/weekly/monthly strategic planning workflows with dedicated worker
- **🔭 Langfuse Tracing** — Full LLM observability (prompts, tokens, costs, latency) via self-hosted Langfuse v3 + ClickHouse + MinIO
- **📋 Trade Journal** — Every decision logged (JSONL + CSV)
- **🔐 Audit Log** — Hash-chained tamper-evident record of all critical operations
- **❤️ Health Check** — HTTP endpoints for Docker HEALTHCHECK
- **🐳 Docker Compose** — One command to deploy the full 17-service stack

### Security & Isolation
- **🏛️ Domain Separation** — Crypto and equity data never mix (SQL, Redis keys, UI)
- **🛡️ Strict Auth** — Telegram allowlist, dashboard 2FA + TOTP, HMAC request signing
- **🔐 Container Hardening** — Read-only FS, no-new-privileges, non-root execution, resource limits
- **🧱 Pre-Commit Guards** — Domain separation + security tests block broken commits

## 🚀 Quick Start

### Prerequisites
- Docker Desktop (Windows/Mac/Linux)
- **Optional:** NVIDIA GPU + Container Toolkit for GPU-accelerated Ollama (on Mac, Ollama uses Metal natively)
- Telegram account

> **No GPU?** The system works fine with cloud-only LLM providers (OpenRouter, Groq). Set those API keys and Ollama becomes optional.

### Setup
```powershell
# Windows
.\setup.ps1

# macOS / Linux
./setup.sh
```

The setup wizard will:
1. ✅ Validate Docker and prerequisites
2. ⚙️ Choose trading mode (paper or live)
3. 🔑 Set up Coinbase API credentials
4. 🧠 Select optimal LLM model for your hardware
5. 🔒 **Create Telegram bot with strict user authorization**
6. 📰 Optionally set up Reddit API for news
7. 🔐 Generate Redis password automatically
8. 📁 Create required data directories
9. 🐳 Build & start Docker stack and pull LLM model
10. ✅ Verify everything works

**The entire `config/.env` file is created interactively — no template copying needed.**

## 📋 Configuration

### Environment Variables (`config/.env`)
| Variable | Required | Description |
|----------|----------|-------------|
| `COINBASE_API_KEY` | For live | Coinbase Advanced Trade API key |
| `COINBASE_API_SECRET` | For live | Coinbase API secret |
| `TELEGRAM_BOT_TOKEN` | Recommended | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Recommended | Where to send messages (your user ID) |
| `TELEGRAM_AUTHORIZED_USERS` | **REQUIRED** ⚠️ | Comma-separated **numeric user IDs** that can control the bot |
| `OLLAMA_MODEL` | No | LLM model (default: qwen2.5:14b) |
| `REDDIT_CLIENT_ID` | No | Reddit API client ID |
| `REDDIT_CLIENT_SECRET` | No | Reddit API secret |

### 🔒 Telegram Security

**The bot ONLY responds to users listed in `TELEGRAM_AUTHORIZED_USERS`.**

- This env var is **REQUIRED** — the agent **refuses to start** without it
- Must contain **numeric Telegram user IDs** (not usernames!)
- Get your ID: message **@userinfobot** on Telegram
- Unauthorized access attempts are **logged with full details**
- The bot gives **no information** to unauthorized users
- **Recommendation**: disable "Allow Groups" in BotFather settings

```env
# ✅ Correct — numeric user IDs
TELEGRAM_AUTHORIZED_USERS=123456789,987654321

# ❌ WRONG — usernames don't work
TELEGRAM_AUTHORIZED_USERS=@myusername
```

### Recommended Models (16GB VRAM)
| Model | VRAM | Speed | Quality | Best For |
|-------|------|-------|---------|----------|
| **qwen2.5:14b** ⭐ | ~10GB | Fast | Great | Overall best |
| llama3.1:8b | ~5GB | V.Fast | Good | Speed priority |
| mistral:7b | ~5GB | V.Fast | Good | Balanced |
| deepseek-r1:14b | ~10GB | Med | Great | Deep reasoning |

### Key Settings (`config/settings.yaml`)
| Setting | Default | Description |
|---------|---------|-------------|
| `rotation.autonomous_allocation_pct` | 10% | % of portfolio for auto-swaps |
| `rotation.min_score_delta` | 0.30 | Min strength difference to trigger swap |
| `fees.trade_fee_pct` | 0.6% | Coinbase taker fee |
| `fees.safety_margin` | 1.5x | Required gain vs fees multiple |
| `high_stakes.trade_size_multiplier` | 2.5x | Max trade boost in HS mode |
| `high_stakes.auto_approve_up_to_usd` | $500 | Auto-approve limit in HS mode |

## 💬 Conversational Telegram Interface

The Telegram bot is **NOT a traditional command bot** — it's an **LLM agent** that uses Telegram to communicate with you naturally.

### Natural Language Examples
```
You: "how are we doing?"
Bot: 📊 Portfolio's at $10,234 — up 2.3% today! We've got 3 open
     positions. BTC is looking strong, ETH is sideways. Thinking
     about rotating some SOL into AVAX...

You: "let's go high stakes for the rest of the day"
Bot: ⚡ High-stakes mode ON until midnight UTC! Trade sizes bumped
     to 2.5x, I'll auto-approve up to $500. Remember — absolute
     rules still apply. Let's make some moves 🚀

You: "be quiet for a while"
Bot: 🤫 Going quiet. I'll only ping you for trades and critical alerts.

You: "buy BTC if it drops below 90k, max $500"
Bot: 📝 Task created! I'll watch BTC and buy up to $500 if it
     hits $90,000. Currently at $94,200 — I'll keep you posted.
```

### Verbosity Control
| Say this | Bot switches to |
|----------|----------------|
| _"be quiet"_ / _"tone it down"_ | **Quiet** — trades and alerts only |
| _"be silent"_ / _"shut up"_ | **Silent** — critical emergencies only |
| _"talk to me more"_ / _"be chatty"_ | **Chatty** — frequent updates, market color |
| _"give me everything"_ / _"verbose"_ | **Verbose** — full play-by-play |
| _"back to normal"_ | **Normal** — balanced updates |

### Proactive Updates
The bot **proactively keeps you informed** based on your verbosity setting:
- 📊 Portfolio snapshots and daily plans
- 📈 Interesting price movements and signals
- 🔄 Rotation proposals and swap analysis
- ⚡ High-stakes mode countdowns
- 🚨 Critical alerts (always sent, even in silent mode)

### Slash Command Shortcuts
Slash commands still work as **quick shortcuts** — they route through the LLM too:
| Command | Shortcut for |
|---------|-------------|
| `/status` | "How are we doing?" |
| `/positions` | "Show me open positions" |
| `/highstakes 4h` | "Go high-stakes for 4 hours" |
| `/quiet` | "Be quiet" |
| `/chatty` | "Be more talkative" |
| `/task <desc>` | Direct task creation |
| `/pause` / `/resume` / `/stop` | Trading control |

## 🔄 Portfolio Rotation

The agent can **autonomously swap between cryptocurrencies** when relative strength analysis indicates one asset is weakening while another is strengthening.

### How It Works
1. Each cycle, **all tracked assets are ranked** by multi-timeframe confluence score
2. If a held asset scores significantly lower than an alternative → propose swap
3. **Fee check**: swap only happens if `expected_gain > total_fees × 1.5`
4. Small, high-confidence swaps → **auto-execute** (within allocation %)
5. Large or uncertain swaps → **ask owner via Telegram** for approval

### Fee Protection
A swap costs **two trades** (~1.2% total fees). The agent:
- Calculates exact fee impact before every trade
- Requires expected gain to exceed `fees × safety_margin`
- Enforces cooldown between swaps (prevents churn)
- Logs all fee calculations in the journal

### High-Stakes Mode
When you're confident about a market move, temporarily elevate limits:
```
You: /highstakes 4h
Bot: ⚡ HIGH-STAKES MODE ACTIVATED
     Duration: 4h
     Trade size: 2.5x normal
     Swap allocation: 2x normal
     Min confidence: 0.50
     Auto-approve up to: $500
     ⚠️ Absolute rules still enforced.
```

## 🔒 Security

- **Strict Telegram auth** — Only numeric user IDs in allowlist; **agent refuses to start without it**
- **Unauthorized attempt logging** — Full user details logged on every rejected request
- **Non-root Docker containers** — Agent runs as unprivileged `trader` user
- **Read-only root filesystem** — Only `config/` and `data/` are writable bind-mounts
- **Prompt injection protection** — User input sanitized before LLM
- **HMAC verification** — API request signing for dashboard
- **Credential masking** — Secrets hidden in logs
- **Redis hardening** — Password-protected, destructive commands disabled, memory-bounded with LRU eviction
- **No-new-privileges** — Container can't escalate privileges
- **Resource limits** — Memory and CPU caps on all containers prevent resource exhaustion
- **Local-only ports** — Unauthenticated services (Ollama, Redis, Temporal) bound to `127.0.0.1`
- **Rate limiting** — Token-bucket rate limiting prevents API abuse
- **Hash-chained audit log** — Tamper-evident record of all operations
- **High-stakes audit** — Every activation/deactivation logged with full context
- **Dashboard 2FA** — TOTP-based two-factor authentication

## 📊 Project Structure
```
opentraitor/
├── config/
│   ├── .env                  # Environment secrets (created by setup.ps1)
│   ├── coinbase.yaml         # Crypto profile config
│   ├── ibkr.yaml             # Equity profile config
│   ├── settings.yaml         # Default/template config
│   └── Modelfile             # Custom Ollama model definition
├── docs/
│   ├── high-level-architecture.md  # System architecture overview
│   └── ADR/                  # 14 Architecture Decision Records
├── src/
│   ├── agents/               # Multi-agent LLM system
│   │   ├── base_agent.py     # Abstract agent interface
│   │   ├── market_analyst.py # Technical + sentiment analysis
│   │   ├── strategist.py     # Trade strategy generation
│   │   ├── risk_manager.py   # Risk validation + position sizing
│   │   ├── executor.py       # Order routing + execution
│   │   └── settings_advisor.py # Autonomous parameter tuning
│   ├── analysis/             # Market analysis
│   │   ├── technical.py      # LLM-driven technical analysis
│   │   ├── indicators.py     # RSI, MACD, Bollinger, EMA library
│   │   ├── sentiment.py      # Keyword-based sentiment scoring
│   │   ├── fear_greed.py     # Fear & Greed Index integration
│   │   └── multi_timeframe.py # Multi-TF confluence scoring
│   ├── backtesting/          # Strategy validation
│   │   ├── engine.py         # Backtest engine + cost sensitivity
│   │   └── walk_forward.py   # Walk-forward optimization framework
│   ├── core/                 # Core engine
│   │   ├── orchestrator.py   # Main pipeline coordinator
│   │   ├── managers/         # Sub-managers (see below)
│   │   ├── coinbase_client.py    # Coinbase REST API
│   │   ├── coinbase_paper.py     # Coinbase paper trading sim
│   │   ├── coinbase_currency.py  # Currency pair metadata
│   │   ├── coinbase_discovery.py # Pair discovery + scanning
│   │   ├── exchange_client.py    # Abstract exchange interface
│   │   ├── equity_feed.py        # Equity market data feed
│   │   ├── ib_client.py          # Interactive Brokers connector
│   │   ├── ws_feed.py            # WebSocket price feed
│   │   ├── llm_client.py         # Multi-provider LLM with fallback
│   │   ├── llm_providers.py      # Provider definitions + routing
│   │   ├── rules.py              # Absolute rules engine
│   │   ├── state.py              # Thread-safe shared trading state
│   │   ├── fee_manager.py        # Fee-aware trading logic
│   │   ├── portfolio_rotator.py  # Autonomous crypto swap analysis
│   │   ├── rotation_executor.py  # Swap execution + failure recovery
│   │   ├── portfolio_scaler.py   # Tier-based limit scaling
│   │   ├── trailing_stop.py      # Dynamic trailing stop-loss
│   │   ├── route_finder.py       # Crypto swap path discovery
│   │   ├── high_stakes.py        # Time-limited elevated mode
│   │   ├── paper_trading.py      # Paper trading framework
│   │   ├── market_hours.py       # Exchange session awareness
│   │   └── health.py             # HTTP health check server
│   │   managers/
│   │   ├── pipeline_manager.py   # Trading pipeline lifecycle
│   │   ├── state_manager.py      # State persistence + recovery
│   │   ├── telegram_manager.py   # Telegram bot lifecycle
│   │   ├── universe_scanner.py   # Dynamic pair discovery
│   │   ├── context_manager.py    # Strategic context loading
│   │   ├── holdings_manager.py   # Portfolio holdings tracking
│   │   ├── learning_manager.py   # Adaptive learning lifecycle
│   │   ├── event_manager.py      # Event dispatching
│   │   └── dashboard_commands.py # Dashboard command handling
│   ├── dashboard/            # Web dashboard backend
│   │   ├── server.py         # FastAPI app + middleware
│   │   ├── auth.py           # Session auth + 2FA (TOTP)
│   │   ├── deps.py           # Profile routing + dependencies
│   │   └── routes/           # 16 route modules (see Dashboard)
│   ├── models/               # Pydantic data models
│   │   ├── trade.py          # Trade, TradeAction, TradeStatus
│   │   └── signal.py         # Signal, SignalType, MarketCondition
│   ├── news/                 # News aggregation
│   │   ├── aggregator.py     # Reddit, RSS, IBKR, ticker matching
│   │   └── worker.py         # Background news daemon
│   ├── planning/             # Temporal workflow orchestration
│   │   ├── workflows.py      # Daily/weekly/monthly plan workflows
│   │   ├── activities.py     # Side-effectful activity implementations
│   │   └── worker.py         # Temporal worker process
│   ├── strategies/           # Deterministic strategy ensemble
│   │   ├── base.py           # Abstract strategy + StrategySignal
│   │   ├── ema_crossover.py  # Trend-following EMA strategy
│   │   ├── bollinger_reversion.py # Mean-reversion strategy
│   │   └── pairs_monitor.py  # Correlation divergence detector
│   ├── telegram_bot/         # Telegram integration
│   │   ├── bot.py            # LLM-powered bot interface
│   │   ├── chat_handler.py   # Conversational engine
│   │   ├── fast_path.py      # Low-latency critical commands
│   │   ├── formatters.py     # Mobile-friendly output formatting
│   │   ├── proactive.py      # Autonomous event alerts
│   │   ├── persona.py        # Bot personality + verbosity config
│   │   └── tools.py          # Structured LLM tool definitions
│   ├── utils/                # Utilities & meta-learning
│   │   ├── journal.py        # Trade journal (JSONL + CSV)
│   │   ├── audit.py          # Hash-chained audit log
│   │   ├── security.py       # Input sanitization, HMAC
│   │   ├── settings_manager.py # Runtime config management
│   │   ├── tax.py            # FIFO cost-basis tax tracking
│   │   ├── training_data.py  # LLM fine-tuning data capture
│   │   ├── finetuning_pipeline.py # Fine-tuning orchestration
│   │   ├── rate_limiter.py   # Token-bucket rate limiting
│   │   ├── rpm_budget.py     # LLM requests-per-minute budgeting
│   │   ├── signal_scorecard.py   # Prediction accuracy scoring
│   │   ├── confidence_calibrator.py # Confidence calibration
│   │   ├── ensemble_optimizer.py # Strategy weight optimization
│   │   ├── prompt_evolver.py # Meta-learning prompt evolution
│   │   ├── auto_wfo.py       # Auto walk-forward optimization
│   │   ├── llm_optimizer.py  # LLM parameter tuning
│   │   ├── qc_filter.py      # Signal quality control
│   │   ├── stats.py          # Stats DB base + query engine
│   │   ├── stats_trades.py   # Trade statistics
│   │   ├── stats_portfolio.py # Portfolio snapshots
│   │   ├── stats_predictions.py # Prediction tracking
│   │   ├── stats_reasoning.py # LLM reasoning samples
│   │   ├── stats_simulated.py # Simulated trade stats
│   │   ├── tracer.py         # Langfuse trace integration
│   │   ├── helpers.py        # General utility functions
│   │   ├── pair_format.py    # Trading pair formatting
│   │   └── logger.py         # Structured colored logging
│   └── main.py               # Entry point (paper/live/daemon modes)
├── dashboard/frontend/       # Vue 3 + TypeScript SPA (15+ pages)
├── tests/                    # 30+ test modules
├── scripts/                  # Migration, inspection, watchdog scripts
├── docker-compose.yml        # Full 17-service stack
├── Dockerfile                # Trading agent container
├── Dockerfile.dashboard      # Dashboard container
├── setup.ps1                 # Interactive setup wizard (Windows)
├── setup.sh                  # Interactive setup wizard (Linux/Mac)
└── requirements.txt          # Python dependencies
```

## 🔄 Trading Pipeline

Each cycle (default: every 120 seconds):

1. **Fetch Market Data** — Candles from REST, prices from WebSocket
2. **Fear & Greed Index** — Sentiment context from alternative.me
3. **Multi-Timeframe Analysis** — 15m/1h/4h/1d confluence scoring
4. **Market Analysis** — Technical indicators + sentiment + F&G + multi-TF via LLM
5. **Strategy Generation** — LLM considers signals, tasks, portfolio, and recent trades
6. **Risk Validation** — Absolute rules check, position sizing, stop-loss enforcement
7. **Fee Check** — Ensure expected gain exceeds trading fees
8. **Quality Control** — QC filter validates signal quality before execution
9. **Approval** (if needed) — Telegram approval for trades above threshold
10. **Execution** — Market order with fee tracking
11. **Portfolio Rotation** — Evaluate and execute crypto-to-crypto swaps
12. **Trailing Stops** — Update dynamic stops with current prices
13. **Monitor** — Stop-loss and take-profit checking on all open positions
14. **Learning** — Signal scorecard, confidence calibration, ensemble weight updates
15. **Journal + Audit** — Log every decision for analysis and accountability

## 🖥️ Dashboard Pages

The Vue 3 SPA dashboard at `:8090` provides 15+ pages:

| Page | Description |
|------|-------------|
| **Live Monitor** | Real-time portfolio overview with WebSocket updates |
| **Trades Log** | Complete trade history with filtering and search |
| **Analytics** | Performance charts, P&L analysis, win rates |
| **Cycle Explorer** | Inspect individual trading cycles and agent reasoning |
| **Cycle Playback** | Step-by-step replay of past trading decisions |
| **Predictions** | Prediction accuracy tracking and calibration |
| **Risk Exposure** | Current risk metrics and position sizing |
| **LLM Analytics** | Token usage, costs, latency, provider distribution |
| **News Feed** | Aggregated news from Reddit, RSS, and IBKR |
| **Planning Audit** | Temporal workflow history and plan outcomes |
| **Simulated Trades** | Paper trading results and simulation analysis |
| **System Logs** | Real-time structured log viewer |
| **Watchlist** | Asset watchlist with alerts |
| **Settings** | Runtime configuration management |
| **Setup Wizard** | Guided initial configuration |

## ⚠️ Disclaimer

**This software is experimental and unproven. You will probably lose money.**

OpenTraitor autonomously executes real trades — both **cryptocurrency** (via Coinbase) and **equities / stocks** (via Interactive Brokers) — using LLM-driven decision-making. While the system includes safeguards (absolute rules, fee checks, risk management), autonomous AI trading is inherently risky and the strategies have no guaranteed edge.

**Before connecting any exchange account:**

1. **Start with paper trading** — run in `--mode paper` until you fully understand the system's behavior.
2. **Create dedicated accounts** — set up a **separate Coinbase account** and/or a **separate Interactive Brokers account** exclusively for OpenTraitor. Never connect your primary brokerage or crypto accounts.
3. **Fund only what you can lose** — deposit only an amount you are 100% comfortable losing entirely.
4. **Monitor actively** — even in autonomous mode, review the dashboard and Telegram alerts regularly.

The authors and contributors are **not financial advisors**, make **no guarantees** about trading performance, and accept **no responsibility** for any financial losses incurred through the use of this software. Past performance (including backtests) does not predict future results.

**Use entirely at your own risk.**
