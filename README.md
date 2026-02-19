# 🤖 Auto-Traitor: Autonomous LLM Crypto Trading Agent

An autonomous, LLM-powered cryptocurrency trading agent that runs **entirely locally** using **Ollama** and interfaces with the **Coinbase Advanced Trade API**. Communicates via **Telegram** for notifications, approvals, and receiving commands. Features **autonomous portfolio rotation** (crypto-to-crypto swaps), **fee-aware trading**, and a **time-limited high-stakes mode**.

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    DOCKER COMPOSE                        │
│                                                         │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │   Ollama     │  │    Redis     │  │  News Worker  │  │
│  │  (Local LLM) │  │  (State &    │  │  (Background) │  │
│  │  GPU Accel.  │  │   Cache)     │  │  Reddit/RSS   │  │
│  └──────┬───────┘  └──────┬───────┘  └───────┬───────┘  │
│         │                 │                  │          │
│  ┌──────┴─────────────────┴──────────────────┴───────┐  │
│  │              TRADING AGENT (Daemon)                │  │
│  │                                                   │  │
│  │  ┌──────────────┐  ┌──────────────┐               │  │
│  │  │   WebSocket  │  │   Telegram   │               │  │
│  │  │  (Real-time  │  │    Bot       │──── 📱 You    │  │
│  │  │   Prices)    │  │  (🔒 Strict) │               │  │
│  │  └──────┬───────┘  └──────┬───────┘               │  │
│  │         │                 │                       │  │
│  │  ┌──────┴─────────────────┴────────────────────┐  │  │
│  │  │           ORCHESTRATOR                       │  │  │
│  │  │                                              │  │  │
│  │  │  Market Analyst → Strategist → Risk Mgr     │  │  │
│  │  │      │                            │          │  │  │
│  │  │  Technical     ┌──────────────┐   Rules      │  │  │
│  │  │  + Sentiment   │  Absolute    │   Engine     │  │  │
│  │  │  + Fear&Greed  │   Rules      │     │        │  │  │
│  │  │  + Multi-TF    │ (NEVER BREAK)│     ▼        │  │  │
│  │  │               └──────────────┘  Executor     │  │  │
│  │  │                                    │         │  │  │
│  │  │  ┌──────────────┐  ┌────────────┐  │         │  │  │
│  │  │  │ Fee Manager  │  │ Portfolio  │  │         │  │  │
│  │  │  │ (Loss Prev.) │  │ Rotator   │  │         │  │  │
│  │  │  └──────────────┘  └────────────┘  │         │  │  │
│  │  │                                    │         │  │  │
│  │  └────────────────────────────────────┼─────────┘  │  │
│  │                                       │            │  │
│  │                              ┌────────┴─────────┐  │  │
│  │                              │   Coinbase API   │  │  │
│  │                              │   (REST + WS)    │  │  │
│  │                              └──────────────────┘  │  │
│  └────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

## ✨ Key Features

- **🧠 Local LLM (Ollama)** — All AI runs on YOUR machine. No data leaves your network.
- **🔒 Absolute Rules** — Hard limits that can NEVER be broken (max spend, daily loss, etc.)
- **📱 Telegram Bot** — Full 2-way communication: alerts, approvals, tasks, high-stakes mode
- **🔄 Portfolio Rotation** — Autonomous crypto-to-crypto swaps based on relative strength
- **💰 Fee-Aware Trading** — Only trades when expected gain exceeds fees × safety margin
- **⚡ High-Stakes Mode** — Owner-activated time-limited elevated trading via Telegram
- **📰 News Aggregation** — Reddit, RSS (CoinTelegraph, CoinDesk), CoinGecko trending
- **📡 WebSocket Feed** — Real-time prices via Coinbase WebSocket (low latency)
- **📊 Multi-Timeframe Analysis** — 15m, 1h, 4h, 1d confluence scoring
- **😱 Fear & Greed Index** — Sentiment analysis from alternative.me
- **🎯 Trailing Stop-Loss** — Dynamic stops that lock in profits as price moves
- **📋 Trade Journal** — Every decision logged (JSONL + CSV)
- **🔐 Audit Log** — Hash-chained tamper-evident log of all critical operations
- **❤️ Health Check** — HTTP endpoints for Docker HEALTHCHECK and Prometheus
- **🛡️ Security** — Non-root Docker, strict Telegram auth, prompt injection protection
- **📝 Paper Trading** — Test safely with simulated money before going live
- **🐳 Docker Compose** — One command to deploy everything

## 🚀 Quick Start

### Prerequisites
- Docker Desktop with NVIDIA Container Toolkit
- NVIDIA GPU (configured for RTX 5080 16GB)
- Telegram account

### Setup
```powershell
# Run the interactive setup wizard
.\setup.ps1
```

The setup wizard will:
1. ✅ Validate Docker, NVIDIA drivers, and prerequisites
2. ⚙️ Choose trading mode (paper or live)
3. 🔑 Set up Coinbase API credentials
4. 🧠 Select optimal LLM model for your GPU
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
- **Non-root Docker containers** — Agent runs as unprivileged user
- **Read-only config mount** — Config can't be modified at runtime
- **Prompt injection protection** — User input sanitized before LLM
- **HMAC verification** — API request signing
- **Credential masking** — Secrets hidden in logs
- **Redis hardening** — Dangerous commands disabled
- **No-new-privileges** — Container can't escalate privileges
- **Rate limiting** — Prevents API abuse and bans
- **Hash-chained audit log** — Tamper-evident record of all operations
- **High-stakes audit** — Every activation/deactivation logged with full context

## 📊 Project Structure
```
auto-traitor/
├── config/
│   ├── .env                # Environment config (created by setup.ps1)
│   ├── settings.yaml       # Trading, risk, fees, rotation config
│   └── Modelfile           # Custom Ollama model definition
├── src/
│   ├── agents/             # Multi-agent LLM system
│   │   ├── market_analyst.py # Technical + sentiment analysis
│   │   ├── strategist.py   # Strategy generation
│   │   ├── risk_manager.py # Risk validation + rules check
│   │   └── executor.py     # Trade execution on Coinbase
│   ├── analysis/           # Market analysis
│   │   ├── technical.py    # RSI, MACD, Bollinger, EMA
│   │   ├── fear_greed.py   # Fear & Greed Index integration
│   │   └── multi_timeframe.py # Multi-TF confluence scoring
│   ├── backtesting/        # Backtesting engine
│   │   └── engine.py       # Historical replay simulation
│   ├── core/               # Core engine
│   │   ├── coinbase_client.py # Coinbase REST API wrapper
│   │   ├── ws_feed.py      # Coinbase WebSocket (real-time)
│   │   ├── llm_client.py   # Ollama LLM client
│   │   ├── orchestrator.py # Main pipeline coordinator
│   │   ├── portfolio_rotator.py # Autonomous crypto swaps
│   │   ├── fee_manager.py  # Fee-aware trading logic
│   │   ├── high_stakes.py  # Time-limited elevated mode
│   │   ├── trailing_stop.py # Trailing stop-loss manager
│   │   ├── health.py       # HTTP health check server
│   │   ├── rules.py        # Absolute rules engine
│   │   └── state.py        # Shared trading state
│   ├── news/               # News aggregation
│   │   ├── aggregator.py   # Reddit, RSS, CoinGecko
│   │   └── worker.py       # Background news daemon
│   ├── telegram_bot/       # Telegram integration
│   │   ├── bot.py          # LLM-powered bot interface
│   │   └── chat_handler.py # Conversational engine + memory
│   ├── utils/              # Utilities
│   │   ├── journal.py      # Trade journal (JSONL + CSV)
│   │   ├── audit.py        # Hash-chained audit log
│   │   ├── rate_limiter.py # API rate limiting
│   │   ├── security.py     # Input sanitization, HMAC
│   │   └── logger.py       # Rich logging
│   └── main.py             # Entry point
├── docker-compose.yml      # Full stack deployment
├── Dockerfile              # Agent container
├── setup.ps1               # Interactive setup wizard
└── requirements.txt        # Python dependencies
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
8. **Approval** (if needed) — Telegram approval for trades above threshold
9. **Execution** — Coinbase market order with fee tracking
10. **Portfolio Rotation** — Evaluate and execute crypto-to-crypto swaps
11. **Trailing Stops** — Update dynamic stops with current prices
12. **Monitor** — Stop-loss and take-profit checking on all open positions
13. **Journal + Audit** — Log every decision for analysis and accountability

## ⚠️ Disclaimer

This software is for **educational and research purposes**. Cryptocurrency trading involves significant risk. Always start with paper trading and never invest more than you can afford to lose. The authors are not responsible for any financial losses.
