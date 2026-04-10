#!/usr/bin/env bash
# ===========================================================================
#  Auto-Traitor Setup Script (Linux/macOS)
#  Interactive setup that creates config/.env and guides through everything.
#  Supports multi-exchange (Coinbase crypto + IBKR equities) architecture.
# ===========================================================================

set -euo pipefail

ENV_FILE="${1:-config/.env}"
SKIP_DOCKER="${SKIP_DOCKER:-false}"
SKIP_OLLAMA="${SKIP_OLLAMA:-false}"
SKIP_TELEGRAM="${SKIP_TELEGRAM:-false}"
ONLY_ENV="${ONLY_ENV:-false}"

container_runtime=""
compose_cmd=""

# ===========================================================================
# Helpers
# ===========================================================================

banner() {
    echo ""
    echo "  ╔═══════════════════════════════════════════════╗"
    echo "  ║                                               ║"
    echo "  ║          AUTO-TRAITOR  SETUP  WIZARD          ║"
    echo "  ║       Autonomous LLM Multi-Asset Trading      ║"
    echo "  ║                                               ║"
    echo "  ╚═══════════════════════════════════════════════╝"
    echo ""
}

generate_root_env() {
    local traitor_db_pw="$1"
    local langfuse_db_pw="$2"
    local langfuse_secret="$3"
    local langfuse_salt="$4"
    local langfuse_admin_pw="$5"
    local redis_pw="$6"
    local temporal_db_pw="$7"
    local clickhouse_pw="$8"
    local minio_pw="$9"
    local langfuse_enc_key="${10}"
    local dash_signing_key="${11}"
    local dash_session_secret="${12}"

    cat > .env << ROOTENV
# Docker/Podman Compose variable substitution
# Generated: $(date '+%Y-%m-%d %H:%M:%S')

OLLAMA_MODEL=qwen2.5:14b

TRAITOR_DB_PASSWORD=${traitor_db_pw}

LANGFUSE_NEXTAUTH_SECRET=${langfuse_secret}
LANGFUSE_SALT=${langfuse_salt}
LANGFUSE_ADMIN_PASSWORD=${langfuse_admin_pw}
LANGFUSE_ADMIN_EMAIL=admin@auto-traitor.local
LANGFUSE_ADMIN_NAME=admin
LANGFUSE_DB_PASSWORD=${langfuse_db_pw}
LANGFUSE_PUBLIC_KEY=at-public-key
LANGFUSE_SECRET_KEY=at-secret-key

REDIS_PASSWORD=${redis_pw}

TEMPORAL_DB_USER=temporal
TEMPORAL_DB_PASSWORD=${temporal_db_pw}
TEMPORAL_DB_NAME=temporal

CLICKHOUSE_PASSWORD=${clickhouse_pw}
MINIO_ROOT_USER=minio
MINIO_ROOT_PASSWORD=${minio_pw}
LANGFUSE_ENCRYPTION_KEY=${langfuse_enc_key}

# Dashboard security
DASHBOARD_COMMAND_SIGNING_KEY=${dash_signing_key}
DASHBOARD_SESSION_SECRET=${dash_session_secret}
ROOTENV
}

step() {
    echo ""
    echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  STEP $1: $2"
    echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
}

info()  { echo "  ℹ️  $1"; }
ok()    { echo "  ✅ $1"; }
warn()  { echo "  ⚠️  $1"; }
err()   { echo "  ❌ $1"; }

prompt_required() {
    local prompt="$1"
    local default="${2:-}"
    local val=""
    local suffix=""
    [ -n "$default" ] && suffix=" [$default]"
    while true; do
        read -rp "  > ${prompt}${suffix}: " val
        [ -z "$val" ] && [ -n "$default" ] && val="$default"
        [ -n "$val" ] && break
        echo "     This field is required."
    done
    echo "$val"
}

prompt_yesno() {
    local prompt="$1"
    local default="${2:-y}"
    local suffix
    if [ "$default" = "y" ]; then suffix=" [Y/n]"; else suffix=" [y/N]"; fi
    read -rp "  > ${prompt}${suffix}: " val
    val="${val:-$default}"
    case "$val" in
        [Yy]*) return 0 ;;
        *) return 1 ;;
    esac
}

append_env() {
    local key="$1" value="$2" comment="${3:-}"
    [ -n "$comment" ] && echo "# $comment" >> "$ENV_FILE"
    echo "${key}=${value}" >> "$ENV_FILE"
}

append_env_blank() {
    echo "" >> "$ENV_FILE"
}

generate_password() {
    local length="${1:-32}"
    LC_ALL=C openssl rand -base64 "$((length + 12))" | tr -dc 'A-Za-z0-9' | head -c "$length" || true
}

generate_hex_key() {
    local length="${1:-32}"
    openssl rand -hex "$length" || true
}

# ===========================================================================
# MAIN
# ===========================================================================

banner

# Create config directory if needed
mkdir -p config

if [ "$ONLY_ENV" != "true" ]; then
    # Backup existing config/.env
    if [ -f "$ENV_FILE" ]; then
        backup="${ENV_FILE}.backup.$(date +%Y%m%d_%H%M%S)"
        cp "$ENV_FILE" "$backup"
        warn "Existing .env backed up to: $backup"
    fi

    # Start fresh
    cat > "$ENV_FILE" << EOF
# ===========================================
# Auto-Traitor Environment Configuration
# Generated: $(date '+%Y-%m-%d %H:%M:%S')
# ===========================================
EOF
    append_env_blank
fi

# ===========================================================================
# STEP 1: Prerequisites
# ===========================================================================

step 1 "PREREQUISITE CHECKS"

docker_ok=false
if command -v docker &>/dev/null && docker compose version &>/dev/null; then
    container_runtime="docker"
    compose_cmd="docker compose"
    docker_ok=true
    ok "Docker: $(docker --version)"
    ok "Docker Compose: $(docker compose version)"
elif command -v podman &>/dev/null && podman compose version &>/dev/null; then
    container_runtime="podman"
    compose_cmd="podman compose"
    docker_ok=true
    ok "Podman: $(podman --version)"
    ok "Podman Compose: $(podman compose version)"
elif command -v podman-compose &>/dev/null; then
    container_runtime="podman"
    compose_cmd="podman-compose"
    docker_ok=true
    ok "Podman: $(podman --version 2>/dev/null || echo 'installed')"
    ok "podman-compose: $(podman-compose --version)"
else
    err "No supported container runtime found!"
    info "Install Docker: https://docs.docker.com/engine/install/"
    info "or Podman: https://podman.io/docs/installation"
    if [ "$SKIP_DOCKER" != "true" ]; then
        echo "  Cannot continue without Docker or Podman."
        exit 1
    fi
fi

if command -v nvidia-smi &>/dev/null; then
    ok "NVIDIA GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo 'detected')"
else
    warn "No NVIDIA GPU detected. Ollama will use CPU (much slower)."
fi

if command -v python3 &>/dev/null; then
    ok "Python: $(python3 --version)"
elif command -v python &>/dev/null; then
    ok "Python: $(python --version)"
else
    info "Python not found locally (OK — runs in Docker)"
fi

# ===========================================================================
# STEP 1.5: Generate Compose Secrets
# ===========================================================================

step "1.5" "GENERATING INFRASTRUCTURE SECRETS"

info "Creating root .env for ${container_runtime^^} Compose variable substitution..."

# Generate random secrets
traitor_db_password=$(generate_password 32)
langfuse_db_password=$(generate_password 32)
langfuse_nextauth_secret=$(generate_password 48)
langfuse_salt=$(generate_password 48)
langfuse_admin_password=$(generate_password 20)
redis_password=$(generate_password 32)
temporal_db_password=$(generate_password 32)
clickhouse_password=$(generate_password 32)
minio_password=$(generate_password 32)
langfuse_encryption_key=$(generate_hex_key 32)
dash_signing_key=$(generate_hex_key 32)
dash_session_secret=$(generate_hex_key 32)

# Generate actual root .env
generate_root_env "$traitor_db_password" "$langfuse_db_password" "$langfuse_nextauth_secret" "$langfuse_salt" "$langfuse_admin_password" "$redis_password" "$temporal_db_password" "$clickhouse_password" "$minio_password" "$langfuse_encryption_key" "$dash_signing_key" "$dash_session_secret"

ok "Root .env created with auto-generated secrets."

# Exit early if only generating .env
if [ "$ONLY_ENV" = "true" ]; then
    ok "ONLY_ENV mode: Setup complete!"
    echo ""
    echo "  Root .env generated at: .env"
    echo "  You can now run: $compose_cmd up -d"
    exit 0
fi

# ===========================================================================
# STEP 2: Exchange Selection
# ===========================================================================

step 2 "EXCHANGE SELECTION"

echo "  Which exchanges do you want to trade on?"
echo "    1. Coinbase (crypto: BTC, ETH, etc.)"
echo "    2. Interactive Brokers (equities: US/EU markets)"
echo "    3. Both Coinbase + Interactive Brokers"
echo ""
echo "  Note: You can enable one or both exchanges."
echo ""

exchange_choice=$(prompt_required "Select exchange setup (1-3)" "1")

setup_coinbase=false
setup_ibkr=false

case "$exchange_choice" in
    1) setup_coinbase=true; ok "Coinbase (crypto) selected." ;;
    2) setup_ibkr=true; ok "Interactive Brokers (equities) selected." ;;
    3)
        setup_coinbase=true
        setup_ibkr=true
        ok "Both exchanges selected: Coinbase + Interactive Brokers."
        ;;
    *) setup_coinbase=true; ok "Defaulting to Coinbase (crypto)." ;;
esac

# ===========================================================================
# STEP 3: Trading Mode
# ===========================================================================

step 3 "TRADING MODE"

echo "  Choose your trading mode:"
echo "    1. Paper Trading (simulated — no real money)"
echo "    2. Live Trading (real money on exchange)"
echo ""

mode_choice=$(prompt_required "Select mode (1 or 2)" "1")

trading_mode="paper"
if [ "$mode_choice" = "2" ]; then
    echo ""
    warn "LIVE MODE SELECTED — real money will be used!"
    echo "  Make sure you understand the risks."
    if prompt_yesno "Confirm LIVE trading mode?" "n"; then
        trading_mode="live"
    else
        trading_mode="paper"
        ok "Switched to Paper trading mode."
    fi
else
    ok "Paper trading mode selected (safe to experiment)."
fi

append_env "TRADING_MODE" "$trading_mode" "Trading mode: paper or live"

if [ "$trading_mode" = "live" ]; then
    echo ""
    info "For headless/Docker deployments, you can skip the interactive"
    info "confirmation by setting LIVE_TRADING_CONFIRMED."
    if prompt_yesno "Enable headless live mode confirmation?" "n"; then
        append_env "LIVE_TRADING_CONFIRMED" "I UNDERSTAND THE RISKS" "Headless live mode confirmation"
    fi
fi
append_env_blank

# ===========================================================================
# STEP 4: Coinbase API
# ===========================================================================

if [ "$setup_coinbase" = "true" ]; then
    step 4 "COINBASE API CREDENTIALS"

    info "You need a Coinbase Advanced Trade API key."
    echo "  How to get one:"
    echo "    1. Go to https://www.coinbase.com/settings/api"
    echo "    2. Click 'New API Key'"
    echo "    3. Select permissions: View + Trade (NOT Transfer)"
    echo "    4. Copy the API Key Name and Private Key"
    echo ""

    setup_cb=true
    if [ "$trading_mode" = "paper" ]; then
        info "Paper mode: You can skip this (agent will simulate trades)."
        if ! prompt_yesno "Set up Coinbase API now?" "n"; then
            setup_cb=false
        fi
    fi

    if [ "$setup_cb" = "true" ]; then
        cb_key=$(prompt_required "API Key Name")
        echo ""
        info "Paste your Private Key (PEM) as a single line with \\n replacing newlines."
        cb_secret=$(prompt_required "Private Key")
        append_env "COINBASE_API_KEY" "$cb_key" "Coinbase Advanced Trade — API Key Name"
        append_env "COINBASE_API_SECRET" "$cb_secret" "Coinbase Advanced Trade — EC Private Key"
    else
        info "Skipping Coinbase API — paper mode will simulate."
        append_env "COINBASE_API_KEY" "" "Coinbase Advanced Trade API (blank = paper only)"
        append_env "COINBASE_API_SECRET" ""
    fi
    append_env_blank
fi

# ===========================================================================
# STEP 4b: Interactive Brokers
# ===========================================================================

if [ "$setup_ibkr" = "true" ]; then
    step 4 "INTERACTIVE BROKERS EXCHANGE"

    info "Interactive Brokers trading is currently paper-mode only."
    info "Live trading requires IB Gateway / TWS running locally."
    echo ""

    ibkr_host=$(prompt_required "IB Gateway/TWS host" "127.0.0.1")
    ibkr_port=$(prompt_required "IB Gateway/TWS port (4001=live, 4002=paper)" "4002")
    ibkr_client_id=$(prompt_required "IB client ID" "1")

    echo ""
    echo "  Base currency for your IB account?"
    echo "    1. USD  2. EUR  3. GBP  4. CHF"
    echo ""
    curr_choice=$(prompt_required "Select currency (1-4)" "1")
    case "$curr_choice" in
        1) ibkr_currency="USD" ;; 2) ibkr_currency="EUR" ;;
        3) ibkr_currency="GBP" ;; 4) ibkr_currency="CHF" ;;
        *) ibkr_currency="USD" ;;
    esac

    append_env "IBKR_HOST" "$ibkr_host" "Interactive Brokers — Gateway/TWS connection"
    append_env "IBKR_PORT" "$ibkr_port"
    append_env "IBKR_CLIENT_ID" "$ibkr_client_id"
    append_env "IBKR_CURRENCY" "$ibkr_currency" "IB base currency"
    append_env_blank

    ok "Interactive Brokers config will be active via config/ibkr.yaml"
    append_env_blank
fi

# ===========================================================================
# STEP 5: LLM Configuration
# ===========================================================================

step 5 "LLM CONFIGURATION"

info "Auto-Traitor uses a multi-provider LLM chain."
info "Requests try providers in order: Gemini → OpenRouter → OpenAI → Ollama (local fallback)."
echo ""

# --- Cloud providers ---

echo "  ─── Cloud LLM Providers (optional, faster) ────────"
echo ""

gemini_key=""
if prompt_yesno "Set up Google Gemini API?" "n"; then
    echo ""
    echo "  Get a key at: https://aistudio.google.com/app/apikey"
    echo ""
    gemini_key=$(prompt_required "Gemini API Key")
    append_env "GEMINI_API_KEY" "$gemini_key" "Google Gemini API (provider 1)"
    ok "Gemini configured."
else
    append_env "# GEMINI_API_KEY" "" "Google Gemini API (not configured)"
fi
echo ""

if prompt_yesno "Set up OpenRouter API?" "n"; then
    echo ""
    echo "  Get a key at: https://openrouter.ai/keys"
    echo ""
    openrouter_key=$(prompt_required "OpenRouter API Key")
    append_env "OPENROUTER_API_KEY" "$openrouter_key" "OpenRouter API (provider 2)"
    ok "OpenRouter configured."
else
    append_env "# OPENROUTER_API_KEY" "" "OpenRouter API (not configured)"
fi
echo ""

openai_key=""
if prompt_yesno "Set up OpenAI API?" "n"; then
    echo ""
    echo "  Get a key at: https://platform.openai.com/api-keys"
    echo ""
    openai_key=$(prompt_required "OpenAI API Key")
    append_env "OPENAI_API_KEY" "$openai_key" "OpenAI API (provider 3)"
    ok "OpenAI configured."
else
    append_env "# OPENAI_API_KEY" "" "OpenAI API (not configured)"
fi
append_env_blank

# --- Ollama local model ---

echo ""
echo "  ─── Ollama (Local LLM — always available) ─────────"
echo ""
echo "  Available model sizes:"
echo "    1. qwen2.5:7b    — Fast, lower quality     (~4GB VRAM)"
echo "    2. qwen2.5:14b   — Balanced (recommended)   (~8GB VRAM)"
echo "    3. qwen2.5:32b   — Best quality, slow       (~18GB VRAM)"
echo "    4. llama3.1:8b   — Good alternative          (~5GB VRAM)"
echo "    5. Custom        — Enter your own model name"
echo ""

model_choice=$(prompt_required "Select model (1-5)" "2")

case "$model_choice" in
    1) ollama_model="qwen2.5:7b" ;;
    2) ollama_model="qwen2.5:14b" ;;
    3) ollama_model="qwen2.5:32b" ;;
    4) ollama_model="llama3.1:8b" ;;
    5) ollama_model=$(prompt_required "Enter model name") ;;
    *) ollama_model="qwen2.5:14b" ;;
esac

ok "Selected model: $ollama_model"

append_env "OLLAMA_MODEL" "$ollama_model" "Ollama LLM model"
append_env "OLLAMA_BASE_URL" "http://ollama:11434" "Ollama URL (Docker internal)"
append_env_blank

# ===========================================================================
# STEP 6: Telegram Bot Setup
# ===========================================================================

step 6 "TELEGRAM BOT SETUP (SECURITY-CRITICAL)"

echo "  The Telegram bot lets you:"
echo "    • Receive trade notifications and alerts"
echo "    • Approve or reject high-value trades"
echo "    • Send tasks and commands to the agent"
echo ""
echo "  ╔════════════════════════════════════════════════════╗"
echo "  ║  SECURITY: Only YOUR Telegram user ID will be     ║"
echo "  ║  allowed to interact with this bot. Unauthorized  ║"
echo "  ║  attempts are logged.                             ║"
echo "  ╚════════════════════════════════════════════════════╝"
echo ""

if [ "$SKIP_TELEGRAM" != "true" ] && prompt_yesno "Set up Telegram bot?" "y"; then

    echo ""
    echo "  How to get your User ID:"
    echo "    1. Open Telegram"
    echo "    2. Search for @userinfobot"
    echo "    3. Send it any message"
    echo "    4. It replies with your numeric ID"
    echo ""

    user_id=$(prompt_required "Your Telegram User ID (numeric)")
    while ! echo "$user_id" | grep -qE '^[0-9]+$'; do
        err "User ID must be numeric (e.g. 123456789)."
        user_id=$(prompt_required "Your Telegram User ID (numeric)")
    done
    ok "User ID: $user_id"

    echo ""
    authorized_users="$user_id"
    if prompt_yesno "Add additional authorized users?" "n"; then
        info "Enter additional user IDs separated by commas."
        read -rp "  > Additional user IDs: " extra
        [ -n "$extra" ] && authorized_users="${user_id},${extra}"
    fi

    append_env "TELEGRAM_AUTHORIZED_USERS" "$authorized_users" "SECURITY: Only these user IDs can control ANY bot"
    append_env_blank

    if [ "$setup_coinbase" = "true" ]; then
        echo ""
        echo "  ─── Coinbase Telegram Bot ───────────────────"
        echo ""
        echo "  1. Open Telegram → @BotFather → /newbot"
        echo "  2. Copy the bot token"
        echo ""

        cb_bot_token=$(prompt_required "Coinbase Bot Token")
        cb_chat_id=$(prompt_required "Coinbase Chat ID (Enter to use User ID)" "$user_id")

        append_env "TELEGRAM_BOT_TOKEN_COINBASE" "$cb_bot_token" "Telegram Bot — Coinbase agent"
        append_env "TELEGRAM_CHAT_ID_COINBASE" "$cb_chat_id"
        append_env "TELEGRAM_BOT_TOKEN" "$cb_bot_token" "Generic fallback (same as Coinbase)"
        append_env "TELEGRAM_CHAT_ID" "$cb_chat_id"
        append_env_blank
    fi

    if [ "$setup_ibkr" = "true" ]; then
        echo ""
        echo "  ─── IBKR Telegram Bot ──────────────────────"
        echo ""

        ib_bot_token=$(prompt_required "IBKR Bot Token")
        ib_chat_id=$(prompt_required "IBKR Chat ID (Enter to use User ID)" "$user_id")

        append_env "TELEGRAM_BOT_TOKEN_IBKR" "$ib_bot_token" "Telegram Bot — IBKR agent"
        append_env "TELEGRAM_CHAT_ID_IBKR" "$ib_chat_id"

        if [ "$setup_coinbase" != "true" ]; then
            append_env "TELEGRAM_BOT_TOKEN" "$ib_bot_token" "Generic fallback (same as IBKR)"
            append_env "TELEGRAM_CHAT_ID" "$ib_chat_id"
        fi
        append_env_blank
    fi

    ok "Telegram configured!"
else
    info "Skipping Telegram. The agent will run without notifications."
    warn "You won't be able to approve trades or use /highstakes mode."
    append_env "# TELEGRAM_BOT_TOKEN" "" "Telegram (not configured)"
    append_env "# TELEGRAM_CHAT_ID" ""
    append_env "# TELEGRAM_AUTHORIZED_USERS" ""
    append_env_blank
fi

# ===========================================================================
# STEP 7: Reddit API (Optional)
# ===========================================================================

step 7 "NEWS SOURCES (OPTIONAL)"

info "The agent can monitor Reddit for crypto/equity sentiment."
echo "  RSS feeds work without API keys."
echo ""

if prompt_yesno "Set up Reddit API for news?" "n"; then
    echo ""
    echo "  Get credentials at: https://www.reddit.com/prefs/apps"
    echo ""
    reddit_id=$(prompt_required "Reddit Client ID")
    reddit_secret=$(prompt_required "Reddit Client Secret")
    reddit_agent=$(prompt_required "Reddit User Agent" "auto-traitor/1.0")

    append_env "REDDIT_CLIENT_ID" "$reddit_id" "Reddit API (for news)"
    append_env "REDDIT_CLIENT_SECRET" "$reddit_secret"
    append_env "REDDIT_USER_AGENT" "$reddit_agent"
else
    info "Skipping Reddit — RSS feeds will still provide news."
    append_env "# REDDIT_CLIENT_ID" "" "Reddit API (not configured)"
    append_env "# REDDIT_CLIENT_SECRET" ""
fi
append_env_blank

# ===========================================================================
# STEP 8: Infrastructure Secrets (Auto-generated)
# ===========================================================================

step 8 "INFRASTRUCTURE SECRETS (AUTO-GENERATED)"

info "Generating secure passwords for all infrastructure services..."
echo ""

# Redis
redis_password=$(generate_password 32)
append_env "REDIS_PASSWORD" "$redis_password" "Redis (auto-generated)"
append_env "REDIS_URL" "redis://default:${redis_password}@redis:6379/0"
append_env_blank
ok "Redis password generated."

# Trading Stats DB
traitor_db_password=$(generate_password 32)
append_env "TRAITOR_DB_PASSWORD" "$traitor_db_password" "Trading stats PostgreSQL (auto-generated)"
append_env_blank
ok "Trading DB password generated."

# Temporal
temporal_db_user="temporal"
temporal_db_password=$(generate_password 32)
temporal_db_name="temporal"
append_env "TEMPORAL_DB_USER" "$temporal_db_user" "Temporal — workflow engine DB (auto-generated)"
append_env "TEMPORAL_DB_PASSWORD" "$temporal_db_password"
append_env "TEMPORAL_DB_NAME" "$temporal_db_name"
append_env_blank
ok "Temporal DB credentials generated."

# Langfuse
lf_secret=$(generate_password 48)
lf_salt=$(generate_password 48)
lf_adminpw=$(generate_password 20)
lf_dbpw=$(generate_password 32)
ch_password=$(generate_password 32)
minio_pw=$(generate_password 32)
enc_key=$(generate_hex_key 32)

lf_public_key="at-public-key"
lf_secret_key="at-secret-key"

append_env "LANGFUSE_DB_PASSWORD" "$lf_dbpw" "Langfuse — LLM observability (auto-generated)"
append_env "LANGFUSE_NEXTAUTH_SECRET" "$lf_secret"
append_env "LANGFUSE_SALT" "$lf_salt"
append_env "LANGFUSE_ADMIN_PASSWORD" "$lf_adminpw"
append_env "LANGFUSE_PUBLIC_KEY" "$lf_public_key" "Langfuse project init keys"
append_env "LANGFUSE_SECRET_KEY" "$lf_secret_key"
append_env_blank

append_env "CLICKHOUSE_PASSWORD" "$ch_password" "Langfuse v3 — ClickHouse + MinIO (auto-generated)"
append_env "MINIO_ROOT_USER" "minio"
append_env "MINIO_ROOT_PASSWORD" "$minio_pw"
append_env "LANGFUSE_ENCRYPTION_KEY" "$enc_key"
append_env_blank

ok "Langfuse secrets generated."
echo "     Langfuse admin login: admin@auto-traitor.local / $lf_adminpw"

# Dashboard session secret (CSRF + session derivation)
dash_session_secret=$(openssl rand -hex 32)
append_env "DASHBOARD_SESSION_SECRET" "$dash_session_secret" "Dashboard session secret (auto-generated)"

# Dashboard command signing key (HMAC for trade commands via Redis)
dash_cmd_key=$(openssl rand -hex 32)
append_env "DASHBOARD_COMMAND_SIGNING_KEY" "$dash_cmd_key"
append_env_blank

# ===========================================================================
# STEP 9: Create Data Directories
# ===========================================================================

step 9 "CREATING DIRECTORIES"

for d in data data/trades data/news data/journal data/audit logs config; do
    mkdir -p "$d"
done

ok "Created: data/(trades, news, journal, audit), logs/"

# ===========================================================================
# STEP 10: Docker Compose Build & Pull
# ===========================================================================

step 10 "BUILDING CONTAINER STACK"

info "The full stack includes:"
echo "    • Ollama (local LLM with GPU)"
echo "    • Redis (state + cache)"
[ "$setup_coinbase" = "true" ] && echo "    • agent-coinbase (crypto trading)"
[ "$setup_ibkr" = "true" ] && echo "    • agent-ibkr (equity trading)"
echo "    • dashboard (web UI on port 8090)"
echo "    • news-worker (background news aggregation)"
echo "    • Temporal (workflow engine + planning worker)"
echo "    • Langfuse v3 (LLM observability)"
echo ""

# ===========================================================================
# STEP 11: Complete
# ===========================================================================

step 11 "SETUP COMPLETE"

echo ""
echo "  ╔═══════════════════════════════════════════════╗"
echo "  ║         ✅ SETUP COMPLETE!                     ║"
echo "  ╚═══════════════════════════════════════════════╝"
echo ""

echo "  Environment file: $ENV_FILE"
echo "  LLM Model:        $ollama_model"
echo "  Trading Mode:     $trading_mode"
echo ""

echo "  Quick Commands:"
echo "     Start:        $compose_cmd up -d"
echo "     Logs:         $compose_cmd logs -f agent-coinbase"
echo "     Stop:         $compose_cmd down"
echo "     Status:       $compose_cmd ps"
echo ""
echo "  Web UIs (once stack is running):"
echo "     Dashboard:    http://localhost:8090"
echo "     Langfuse:     http://localhost:3000"
echo "     Temporal UI:  http://localhost:8233"
echo ""

start_docker=false
if [ "$docker_ok" = "true" ] && prompt_yesno "Start the ${container_runtime^^} stack now?" "y"; then
    start_docker=true
elif [ "$docker_ok" != "true" ]; then
    warn "Container runtime is not available. Skipping stack startup."
fi

if [ "$start_docker" = "true" ]; then
    info "Pulling images..."
    $compose_cmd pull

    info "Building agent container..."
    $compose_cmd build --no-cache

    info "Starting services..."
    $compose_cmd up -d

    if [ "$SKIP_OLLAMA" != "true" ]; then
        echo ""
        info "Waiting for Ollama to be ready..."
        attempts=0
        ollama_ready=false
        while [ "$attempts" -lt 30 ] && [ "$ollama_ready" = "false" ]; do
            attempts=$((attempts + 1))
            if curl -sf "http://localhost:11434/api/tags" >/dev/null 2>&1; then
                ollama_ready=true
                ok "Ollama is ready!"
            else
                printf "."
                sleep 2
            fi
        done

        if [ "$ollama_ready" = "false" ]; then
            warn "Ollama not ready yet. It may still be starting."
            info "Check: $compose_cmd logs ollama"
        fi

        echo ""
        info "Pulling model: $ollama_model (this may take several minutes)..."
        $compose_cmd exec ollama ollama pull "$ollama_model"

        if [ -f "config/Modelfile" ]; then
            info "Creating custom Auto-Traitor model..."
            $compose_cmd cp config/Modelfile ollama:/tmp/Modelfile
            $compose_cmd exec ollama ollama create auto-traitor -f /tmp/Modelfile
            ok "Custom model 'auto-traitor' created!"
        fi
    fi

    echo ""
    info "Checking service status..."
    $compose_cmd ps
else
    info "Skipping stack startup."
    info "Run '$compose_cmd up -d' when ready."
fi
