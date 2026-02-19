# ===========================================================================
#  Auto-Traitor Setup Script
#  Interactive setup that creates config/.env and guides through everything.
# ===========================================================================

param(
    [switch]$SkipDocker,
    [switch]$SkipOllama,
    [switch]$SkipTelegram,
    [string]$EnvFile = "config/.env"
)

$ErrorActionPreference = "Stop"

# ===========================================================================
# Helpers
# ===========================================================================

function Write-Banner {
    Write-Host ""
    Write-Host "  ╔═══════════════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host "  ║                                               ║" -ForegroundColor Cyan
    Write-Host "  ║          AUTO-TRAITOR  SETUP  WIZARD          ║" -ForegroundColor Cyan
    Write-Host "  ║       Autonomous LLM Crypto Trading Agent     ║" -ForegroundColor Cyan
    Write-Host "  ║                                               ║" -ForegroundColor Cyan
    Write-Host "  ╚═══════════════════════════════════════════════╝" -ForegroundColor Cyan
    Write-Host ""
}

function Write-Step {
    param([int]$Num, [string]$Title)
    Write-Host ""
    Write-Host "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor DarkGray
    Write-Host "  STEP $Num: $Title" -ForegroundColor Yellow
    Write-Host "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor DarkGray
    Write-Host ""
}

function Write-Info {
    param([string]$Text)
    Write-Host "  ℹ️  $Text" -ForegroundColor Cyan
}

function Write-OK {
    param([string]$Text)
    Write-Host "  ✅ $Text" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Text)
    Write-Host "  ⚠️  $Text" -ForegroundColor Yellow
}

function Write-Err {
    param([string]$Text)
    Write-Host "  ❌ $Text" -ForegroundColor Red
}

function Prompt-Required {
    param(
        [string]$Prompt,
        [string]$Help = "",
        [string]$Default = "",
        [switch]$Secret
    )
    if ($Help) { Write-Host "     $Help" -ForegroundColor DarkGray }
    $suffix = if ($Default) { " [$Default]" } else { "" }
    while ($true) {
        if ($Secret) {
            $val = Read-Host "  > $Prompt$suffix"
        }
        else {
            $val = Read-Host "  > $Prompt$suffix"
        }
        if ([string]::IsNullOrWhiteSpace($val) -and $Default) {
            return $Default
        }
        if (-not [string]::IsNullOrWhiteSpace($val)) {
            return $val
        }
        Write-Host "     This field is required." -ForegroundColor Red
    }
}

function Prompt-YesNo {
    param([string]$Prompt, [bool]$Default = $true)
    $suffix = if ($Default) { " [Y/n]" } else { " [y/N]" }
    $val = Read-Host "  > $Prompt$suffix"
    if ([string]::IsNullOrWhiteSpace($val)) { return $Default }
    return $val.ToLower().StartsWith("y")
}

function Append-Env {
    param([string]$Key, [string]$Value, [string]$Comment = "")
    if ($Comment) {
        Add-Content -Path $script:envPath -Value "# $Comment"
    }
    Add-Content -Path $script:envPath -Value "$Key=$Value"
}

function Append-EnvBlank {
    Add-Content -Path $script:envPath -Value ""
}

# ===========================================================================
# MAIN
# ===========================================================================

Write-Banner

$script:envPath = $EnvFile

# Create config directory if needed
if (-not (Test-Path "config")) {
    New-Item -ItemType Directory -Path "config" -Force | Out-Null
}

# Backup existing .env
if (Test-Path $script:envPath) {
    $backup = "$($script:envPath).backup.$(Get-Date -Format 'yyyyMMdd_HHmmss')"
    Copy-Item $script:envPath $backup
    Write-Warn "Existing .env backed up to: $backup"
}

# Start fresh
Set-Content -Path $script:envPath -Value "# ==========================================="
Add-Content -Path $script:envPath -Value "# Auto-Traitor Environment Configuration"
Add-Content -Path $script:envPath -Value "# Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Add-Content -Path $script:envPath -Value "# ==========================================="
Append-EnvBlank

# ===========================================================================
# STEP 1: Prerequisites
# ===========================================================================

Write-Step -Num 1 -Title "PREREQUISITE CHECKS"

# Check Docker
$dockerOK = $false
try {
    $dockerVer = docker --version 2>$null
    if ($dockerVer) {
        Write-OK "Docker: $dockerVer"
        $dockerOK = $true
    }
}
catch {}
if (-not $dockerOK) {
    Write-Err "Docker not found!"
    Write-Info "Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
    if (-not $SkipDocker) {
        Write-Host "  Cannot continue without Docker." -ForegroundColor Red
        exit 1
    }
}

# Check Docker Compose
try {
    $composeVer = docker compose version 2>$null
    if ($composeVer) { Write-OK "Docker Compose: $composeVer" }
}
catch {}

# Check NVIDIA GPU
$gpuOK = $false
try {
    $nvidiaSmi = nvidia-smi --query-gpu=name, memory.total --format=csv, noheader 2>$null
    if ($nvidiaSmi) {
        Write-OK "NVIDIA GPU: $nvidiaSmi"
        $gpuOK = $true
    }
}
catch {}
if (-not $gpuOK) {
    Write-Warn "No NVIDIA GPU detected. Ollama will use CPU (much slower)."
}

# Check Python (for local development)
try {
    $pyVer = python --version 2>$null
    if ($pyVer) { Write-OK "Python: $pyVer" }
}
catch {
    Write-Info "Python not found locally (OK — runs in Docker)"
}

# ===========================================================================
# STEP 2: Trading Mode
# ===========================================================================

Write-Step -Num 2 -Title "TRADING MODE"

Write-Host "  Choose your trading mode:" -ForegroundColor White
Write-Host "    1. 📝 Paper Trading (simulated — no real money)" -ForegroundColor Green
Write-Host "    2. 💰 Live Trading (real money on Coinbase)" -ForegroundColor Red
Write-Host ""
$modeChoice = Prompt-Required -Prompt "Select mode (1 or 2)" -Default "1"

if ($modeChoice -eq "2") {
    $tradingMode = "live"
    Write-Host ""
    Write-Warn "LIVE MODE SELECTED — real money will be used!"
    Write-Host "  Make sure you understand the risks." -ForegroundColor Yellow
    $confirm = Prompt-YesNo -Prompt "Confirm LIVE trading mode?" -Default $false
    if (-not $confirm) {
        $tradingMode = "paper"
        Write-OK "Switched to Paper trading mode."
    }
}
else {
    $tradingMode = "paper"
    Write-OK "Paper trading mode selected (safe to experiment)."
}

Append-Env -Key "TRADING_MODE" -Value $tradingMode -Comment "Trading mode: paper or live"
Append-EnvBlank

# ===========================================================================
# STEP 3: Coinbase API
# ===========================================================================

Write-Step -Num 3 -Title "COINBASE API CREDENTIALS"

Write-Info "You need a Coinbase Advanced Trade API key."
Write-Host "  How to get one:" -ForegroundColor DarkGray
Write-Host "    1. Go to https://www.coinbase.com/settings/api" -ForegroundColor DarkGray
Write-Host "    2. Click 'New API Key'" -ForegroundColor DarkGray
Write-Host "    3. Select portfolios and permissions:" -ForegroundColor DarkGray
Write-Host "       - View ✅  Trade ✅  Transfer ❌" -ForegroundColor DarkGray
Write-Host "    4. Copy the API Key and Secret" -ForegroundColor DarkGray
Write-Host ""

if ($tradingMode -eq "paper") {
    Write-Info "Paper mode: You can skip this (agent will simulate trades)."
    $setupCoinbase = Prompt-YesNo -Prompt "Set up Coinbase API now?" -Default $false
}
else {
    $setupCoinbase = $true
}

if ($setupCoinbase) {
    $cbKey = Prompt-Required -Prompt "Coinbase API Key" -Help "Organizations/xxxx-xxxx/apiKeys/xxxx-xxxx"
    $cbSecret = Prompt-Required -Prompt "Coinbase API Secret" -Secret

    Append-Env -Key "COINBASE_API_KEY" -Value $cbKey -Comment "Coinbase Advanced Trade API"
    Append-Env -Key "COINBASE_API_SECRET" -Value $cbSecret
}
else {
    Write-Info "Skipping Coinbase API — paper mode will simulate."
    Append-Env -Key "COINBASE_API_KEY" -Value "" -Comment "Coinbase Advanced Trade API (blank = paper only)"
    Append-Env -Key "COINBASE_API_SECRET" -Value ""
}
Append-EnvBlank

# ===========================================================================
# STEP 4: Ollama LLM Setup
# ===========================================================================

Write-Step -Num 4 -Title "OLLAMA LLM CONFIGURATION"

Write-Info "Ollama runs the AI brain locally on your GPU."
Write-Host ""
Write-Host "  Available model sizes:" -ForegroundColor White
Write-Host "    1. qwen2.5:7b    — Fast, lower quality     (~4GB VRAM)" -ForegroundColor Green
Write-Host "    2. qwen2.5:14b   — Balanced (recommended)   (~8GB VRAM)" -ForegroundColor Yellow
Write-Host "    3. qwen2.5:32b   — Best quality, slow       (~18GB VRAM)" -ForegroundColor Red
Write-Host "    4. llama3.1:8b   — Good alternative          (~5GB VRAM)" -ForegroundColor Cyan
Write-Host "    5. Custom        — Enter your own model name" -ForegroundColor DarkGray
Write-Host ""

$modelChoice = Prompt-Required -Prompt "Select model (1-5)" -Default "2"

$ollamaModel = switch ($modelChoice) {
    "1" { "qwen2.5:7b" }
    "2" { "qwen2.5:14b" }
    "3" { "qwen2.5:32b" }
    "4" { "llama3.1:8b" }
    "5" { Prompt-Required -Prompt "Enter model name" }
    default { "qwen2.5:14b" }
}

Write-OK "Selected model: $ollamaModel"

Append-Env -Key "OLLAMA_MODEL" -Value $ollamaModel -Comment "Ollama LLM model"
Append-Env -Key "OLLAMA_BASE_URL" -Value "http://ollama:11434" -Comment "Ollama URL (Docker internal)"
Append-EnvBlank

# ===========================================================================
# STEP 5: Telegram Bot Setup (CRITICAL SECURITY)
# ===========================================================================

Write-Step -Num 5 -Title "TELEGRAM BOT SETUP (SECURITY-CRITICAL)"

Write-Host "  The Telegram bot lets you:" -ForegroundColor White
Write-Host "    • Receive trade notifications and alerts" -ForegroundColor DarkGray
Write-Host "    • Approve or reject high-value trades" -ForegroundColor DarkGray
Write-Host "    • Send tasks and commands to the agent" -ForegroundColor DarkGray
Write-Host "    • Enable high-stakes mode for a set duration" -ForegroundColor DarkGray
Write-Host "    • Trigger manual portfolio rotation checks" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  ╔════════════════════════════════════════════════════╗" -ForegroundColor Red
Write-Host "  ║  🔒 SECURITY: Only YOUR Telegram user ID will be  ║" -ForegroundColor Red
Write-Host "  ║  allowed to interact with this bot. No one else   ║" -ForegroundColor Red
Write-Host "  ║  can control your trades, even if they find the   ║" -ForegroundColor Red
Write-Host "  ║  bot. Unauthorized attempts are logged.           ║" -ForegroundColor Red
Write-Host "  ╚════════════════════════════════════════════════════╝" -ForegroundColor Red
Write-Host ""

$setupTelegram = Prompt-YesNo -Prompt "Set up Telegram bot?" -Default $true

if ($setupTelegram -and -not $SkipTelegram) {

    Write-Host ""
    Write-Host "  ─── Step 5a: Create the Bot ─────────────────────" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  1. Open Telegram and search for @BotFather" -ForegroundColor White
    Write-Host "  2. Send: /newbot" -ForegroundColor White
    Write-Host "  3. Choose a name (e.g. 'My Crypto Traitor')" -ForegroundColor White
    Write-Host "  4. Choose a username (e.g. 'my_autotraitor_bot')" -ForegroundColor White
    Write-Host "  5. BotFather gives you a token like:" -ForegroundColor White
    Write-Host "     1234567890:ABCdefGHIjklMNOpqrsTUVwxyz" -ForegroundColor Cyan
    Write-Host ""

    $botToken = Prompt-Required -Prompt "Paste your Bot Token"

    # Validate token format
    if ($botToken -notmatch '^\d+:[A-Za-z0-9_-]+$') {
        Write-Warn "Token format looks unusual. Double-check with BotFather."
    }
    else {
        Write-OK "Token format looks valid."
    }

    Write-Host ""
    Write-Host "  ─── Step 5b: Get Your User ID ───────────────────" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  🔒 THIS IS THE MOST IMPORTANT SECURITY STEP." -ForegroundColor Red
    Write-Host ""
    Write-Host "  Your numeric Telegram User ID is how the bot" -ForegroundColor White
    Write-Host "  knows that YOU are the owner. Without this," -ForegroundColor White
    Write-Host "  the bot will refuse to start." -ForegroundColor White
    Write-Host ""
    Write-Host "  How to get your User ID:" -ForegroundColor Yellow
    Write-Host "    1. Open Telegram" -ForegroundColor White
    Write-Host "    2. Search for @userinfobot" -ForegroundColor White
    Write-Host "    3. Send it any message" -ForegroundColor White
    Write-Host "    4. It replies with your numeric ID (e.g. 123456789)" -ForegroundColor White
    Write-Host ""
    Write-Host "  ⚠️  This is NOT the same as your chat ID!" -ForegroundColor Yellow
    Write-Host "     User ID = who you ARE" -ForegroundColor DarkGray
    Write-Host "     Chat ID = where messages go" -ForegroundColor DarkGray
    Write-Host ""

    $userId = Prompt-Required -Prompt "Your Telegram User ID (numeric)"

    # Validate it's numeric
    while ($userId -notmatch '^\d+$') {
        Write-Err "User ID must be numeric (e.g. 123456789)."
        Write-Info "Message @userinfobot on Telegram to get your ID."
        $userId = Prompt-Required -Prompt "Your Telegram User ID (numeric)"
    }
    Write-OK "User ID: $userId"

    # Ask about additional authorized users
    Write-Host ""
    $addMore = Prompt-YesNo -Prompt "Add additional authorized users?" -Default $false
    $authorizedUsers = $userId

    if ($addMore) {
        Write-Info "Enter additional user IDs separated by commas."
        Write-Info "Each user needs to get their ID from @userinfobot."
        $extra = Read-Host "  > Additional user IDs (comma-separated)"
        if (-not [string]::IsNullOrWhiteSpace($extra)) {
            $authorizedUsers = "$userId,$extra"
        }
    }

    Write-Host ""
    Write-Host "  ─── Step 5c: Get Chat ID ────────────────────────" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  The Chat ID tells the bot WHERE to send messages." -ForegroundColor White
    Write-Host "  For a direct chat with the bot, use your User ID." -ForegroundColor White
    Write-Host "  For a group chat, use the group's chat ID." -ForegroundColor White
    Write-Host ""
    Write-Host "  For direct messages, your Chat ID = User ID" -ForegroundColor DarkGray
    Write-Host ""

    $chatId = Prompt-Required -Prompt "Chat ID (press Enter to use your User ID)" -Default $userId

    Write-Host ""
    Write-Host "  ─── Step 5d: Bot Privacy Settings ───────────────" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  RECOMMENDED: Disable bot joining groups:" -ForegroundColor White
    Write-Host "    1. Go back to @BotFather" -ForegroundColor White
    Write-Host "    2. Send: /mybots" -ForegroundColor White
    Write-Host "    3. Select your bot → Bot Settings" -ForegroundColor White
    Write-Host "    4. Allow Groups → Turn OFF" -ForegroundColor White
    Write-Host ""
    Write-Host "  This prevents anyone from adding your bot to" -ForegroundColor DarkGray
    Write-Host "  a group chat where others might interact with it." -ForegroundColor DarkGray
    Write-Host ""

    Read-Host "  Press Enter when done (or skip)"

    # Write Telegram config
    Append-Env -Key "TELEGRAM_BOT_TOKEN" -Value $botToken -Comment "Telegram Bot"
    Append-Env -Key "TELEGRAM_CHAT_ID" -Value $chatId
    Append-Env -Key "TELEGRAM_AUTHORIZED_USERS" -Value $authorizedUsers -Comment "SECURITY: Only these user IDs can control the bot (comma-separated)"
    Append-EnvBlank

    Write-OK "Telegram configured!"
    Write-Host ""
    Write-Host "  🔒 Security Summary:" -ForegroundColor Green
    Write-Host "     • Bot Token: $($botToken.Substring(0, 10))..." -ForegroundColor DarkGray
    Write-Host "     • Chat ID: $chatId" -ForegroundColor DarkGray
    Write-Host "     • Authorized Users: $authorizedUsers" -ForegroundColor DarkGray
    Write-Host "     • ONLY these users can send commands" -ForegroundColor DarkGray
    Write-Host "     • Unauthorized attempts are logged with full details" -ForegroundColor DarkGray
    Write-Host "     • Bot startup FAILS if authorized users list is empty" -ForegroundColor DarkGray

}
else {
    Write-Info "Skipping Telegram. The agent will run without notifications."
    Write-Warn "You won't be able to approve trades or use /highstakes mode."
    Append-Env -Key "# TELEGRAM_BOT_TOKEN" -Value "" -Comment "Telegram (not configured)"
    Append-Env -Key "# TELEGRAM_CHAT_ID" -Value ""
    Append-Env -Key "# TELEGRAM_AUTHORIZED_USERS" -Value ""
    Append-EnvBlank
}

# ===========================================================================
# STEP 6: Reddit API (Optional — for news)
# ===========================================================================

Write-Step -Num 6 -Title "NEWS SOURCES (OPTIONAL)"

Write-Info "The agent can monitor Reddit for crypto sentiment."
Write-Host "  RSS feeds (CoinTelegraph, CoinDesk, etc.) work without API keys." -ForegroundColor DarkGray
Write-Host ""

$setupReddit = Prompt-YesNo -Prompt "Set up Reddit API for news?" -Default $false

if ($setupReddit) {
    Write-Host ""
    Write-Host "  How to get Reddit API credentials:" -ForegroundColor White
    Write-Host "    1. Go to https://www.reddit.com/prefs/apps" -ForegroundColor DarkGray
    Write-Host "    2. Click 'create another app...'" -ForegroundColor DarkGray
    Write-Host "    3. Select 'script' type" -ForegroundColor DarkGray
    Write-Host "    4. Use any redirect URI (e.g. http://localhost)" -ForegroundColor DarkGray
    Write-Host ""

    $redditId = Prompt-Required -Prompt "Reddit Client ID"
    $redditSecret = Prompt-Required -Prompt "Reddit Client Secret" -Secret
    $redditAgent = Prompt-Required -Prompt "Reddit User Agent" -Default "auto-traitor/1.0"

    Append-Env -Key "REDDIT_CLIENT_ID" -Value $redditId -Comment "Reddit API (for news)"
    Append-Env -Key "REDDIT_CLIENT_SECRET" -Value $redditSecret
    Append-Env -Key "REDDIT_USER_AGENT" -Value $redditAgent
}
else {
    Write-Info "Skipping Reddit — RSS feeds will still provide news."
    Append-Env -Key "# REDDIT_CLIENT_ID" -Value "" -Comment "Reddit API (not configured)"
    Append-Env -Key "# REDDIT_CLIENT_SECRET" -Value ""
}
Append-EnvBlank

# ===========================================================================
# STEP 7: Redis Password
# ===========================================================================

Write-Step -Num 7 -Title "REDIS CONFIGURATION"

Write-Info "Redis stores state, caches data, and handles inter-service comms."
Write-Info "Generating a random Redis password for security..."

# Generate random password
$chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
$redisPassword = -join ((1..32) | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })

Append-Env -Key "REDIS_PASSWORD" -Value $redisPassword -Comment "Redis (auto-generated)"
Append-Env -Key "REDIS_URL" -Value "redis://default:${redisPassword}@redis:6379/0"
Append-EnvBlank

Write-OK "Redis password generated."

# ===========================================================================
# STEP 7b: Langfuse secrets (for docker-compose interpolation)
# These are written to a root .env that Docker Compose reads automatically
# when resolving ${VAR} references in docker-compose.yml.
# ===========================================================================

$lf_secret    = -join ((1..48) | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
$lf_salt      = -join ((1..48) | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
$lf_adminpw   = -join ((1..20) | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
$lf_dbpw      = -join ((1..32) | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })

# Root-level .env for Docker Compose variable substitution
$rootEnv = ".env"
Set-Content -Path $rootEnv -Value "# Docker Compose variable substitution (Langfuse + Postgres)"
Add-Content -Path $rootEnv -Value "# Generated by setup.ps1 — do not commit (already in .gitignore)"
Add-Content -Path $rootEnv -Value ""
Add-Content -Path $rootEnv -Value "LANGFUSE_NEXTAUTH_SECRET=$lf_secret"
Add-Content -Path $rootEnv -Value "LANGFUSE_SALT=$lf_salt"
Add-Content -Path $rootEnv -Value "LANGFUSE_ADMIN_PASSWORD=$lf_adminpw"
Add-Content -Path $rootEnv -Value "LANGFUSE_DB_PASSWORD=$lf_dbpw"

Write-OK "Langfuse secrets generated (root .env)."
Write-Host "     Langfuse admin login: admin@auto-traitor.local / $lf_adminpw" -ForegroundColor DarkGray

# ===========================================================================
# STEP 8: Create Data Directories
# ===========================================================================

Write-Step -Num 8 -Title "CREATING DIRECTORIES"

$dirs = @("data", "data/trades", "data/news", "data/journal", "data/audit", "logs", "config")
foreach ($d in $dirs) {
    if (-not (Test-Path $d)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
    }
}

Write-OK "Created: data/(trades, news, journal, audit), logs/"

# ===========================================================================
# STEP 9: Docker Compose Build & Pull
# ===========================================================================

Write-Step -Num 9 -Title "BUILDING DOCKER STACK"

$startDockerDefault = $dockerOK
if (-not $dockerOK) {
    Write-Warn "Docker is not available in this environment. Skipping Docker startup by default."
}

$startDocker = Prompt-YesNo -Prompt "Build and start the Docker stack now?" -Default $startDockerDefault

if ($startDocker) {
    if (-not $dockerOK) {
        Write-Err "Docker startup requested, but Docker is not available."
        Write-Info "Install Docker Desktop, then run: docker compose up -d"
        $startDocker = $false
    }
}

if ($startDocker) {
    Write-Info "Pulling Docker images..."
    docker compose pull

    Write-Info "Building agent container..."
    docker compose build --no-cache

    Write-Info "Starting services..."
    docker compose up -d

    if ($SkipOllama) {
        Write-Info "-SkipOllama set: skipping Ollama readiness/model pull/custom model creation."
    }
    else {
        Write-Host ""
        Write-Info "Waiting for Ollama to be ready..."
        $attempts = 0
        $ollamaReady = $false
        while ($attempts -lt 30 -and -not $ollamaReady) {
            $attempts++
            try {
                $resp = Invoke-WebRequest -Uri "http://localhost:11434/api/tags" -TimeoutSec 3 -ErrorAction SilentlyContinue
                if ($resp.StatusCode -eq 200) {
                    $ollamaReady = $true
                    Write-OK "Ollama is ready!"
                }
            }
            catch {
                Write-Host "." -NoNewline
                Start-Sleep -Seconds 2
            }
        }

        if (-not $ollamaReady) {
            Write-Warn "Ollama not ready yet. It may still be starting."
            Write-Info "Check: docker compose logs ollama"
        }

        # Pull the selected model
        Write-Host ""
        Write-Info "Pulling model: $ollamaModel (this may take several minutes)..."
        docker compose exec ollama ollama pull $ollamaModel

        # Create custom Modelfile if it exists
        if (Test-Path "config/Modelfile") {
            Write-Info "Creating custom Auto-Traitor model..."
            # Copy Modelfile into the container and create model
            docker compose cp config/Modelfile ollama:/tmp/Modelfile
            docker compose exec ollama ollama create auto-traitor -f /tmp/Modelfile
            Write-OK "Custom model 'auto-traitor' created!"
        }
    }

    Write-Host ""
    Write-Info "Checking service status..."
    docker compose ps

}
else {
    Write-Info "Skipping Docker startup."
    Write-Info "Run 'docker compose up -d' when ready."
}

# ===========================================================================
# STEP 10: Verification
# ===========================================================================

Write-Step -Num 10 -Title "SETUP COMPLETE"

Write-Host ""
Write-Host "  ╔═══════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "  ║         ✅ SETUP COMPLETE!                     ║" -ForegroundColor Green
Write-Host "  ╚═══════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""

Write-Host "  📄 Environment file: $script:envPath" -ForegroundColor White
Write-Host "  ⚙️  Settings file:    config/settings.yaml" -ForegroundColor White
Write-Host "  🧠 LLM Model:        $ollamaModel" -ForegroundColor White
Write-Host "  📊 Trading Mode:     $tradingMode" -ForegroundColor White
Write-Host ""

Write-Host "  🔒 Security Checklist:" -ForegroundColor Yellow
Write-Host "     ✅ .env file created with credentials" -ForegroundColor Green
Write-Host "     ✅ .env is in .gitignore (never committed)" -ForegroundColor Green
if ($setupTelegram) {
    Write-Host "     ✅ Telegram bot restricted to User ID: $authorizedUsers" -ForegroundColor Green
    Write-Host "     ✅ Unauthorized access attempts will be logged" -ForegroundColor Green
    Write-Host "     ✅ Bot will REFUSE to start without authorized users" -ForegroundColor Green
}
else {
    Write-Host "     ⚠️  Telegram not configured (no trade approval)" -ForegroundColor Yellow
}
Write-Host "     ✅ Redis password auto-generated" -ForegroundColor Green
Write-Host "     ✅ Docker containers run as non-root" -ForegroundColor Green
Write-Host ""

Write-Host "  📋 Quick Commands:" -ForegroundColor Yellow
Write-Host "     Start:        docker compose up -d" -ForegroundColor DarkGray
Write-Host "     Logs:         docker compose logs -f agent" -ForegroundColor DarkGray
Write-Host "     Stop:         docker compose down" -ForegroundColor DarkGray
Write-Host "     Status:       docker compose ps" -ForegroundColor DarkGray
Write-Host "     Ollama logs:  docker compose logs -f ollama" -ForegroundColor DarkGray
Write-Host "     Health:       curl http://localhost:8080/health" -ForegroundColor DarkGray
Write-Host "     Metrics:      curl http://localhost:8080/metrics" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  🌐 Web UIs (once stack is running):" -ForegroundColor Yellow
Write-Host "     Dashboard:    http://localhost:8090" -ForegroundColor DarkGray
Write-Host "     Langfuse:     http://localhost:3000  ($('admin@auto-traitor.local') / $lf_adminpw)" -ForegroundColor DarkGray
Write-Host "     Temporal UI:  http://localhost:8233" -ForegroundColor DarkGray
Write-Host ""

if ($setupTelegram) {
    Write-Host "  📱 Telegram Commands:" -ForegroundColor Yellow
    Write-Host "     /status        — Portfolio overview" -ForegroundColor DarkGray
    Write-Host "     /positions     — Open positions" -ForegroundColor DarkGray
    Write-Host "     /trades        — Recent trades" -ForegroundColor DarkGray
    Write-Host "     /rotate        — Force rotation check" -ForegroundColor DarkGray
    Write-Host "     /swaps         — View pending swaps" -ForegroundColor DarkGray
    Write-Host "     /fees          — Fee configuration" -ForegroundColor DarkGray
    Write-Host "     /highstakes 4h — Enable high-stakes for 4 hours" -ForegroundColor DarkGray
    Write-Host "     /pause / /resume / /stop" -ForegroundColor DarkGray
    Write-Host ""
}

Write-Host "  ⚠️  IMPORTANT: Review config/settings.yaml to fine-tune:" -ForegroundColor Yellow
Write-Host "     • Trade limits (max_single_trade_usd, etc.)" -ForegroundColor DarkGray
Write-Host "     • Portfolio rotation allocation %" -ForegroundColor DarkGray
Write-Host "     • Fee safety margins" -ForegroundColor DarkGray
Write-Host "     • High-stakes mode multipliers" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Happy trading! 🚀" -ForegroundColor Green
Write-Host ""
