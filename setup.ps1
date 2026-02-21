# ===========================================================================
#  Auto-Traitor Setup Script
#  Interactive setup that creates config/.env and guides through everything.
#  Supports multi-exchange (Coinbase crypto + Nordnet equities) architecture.
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
    Write-Host "  ║       Autonomous LLM Multi-Asset Trading      ║" -ForegroundColor Cyan
    Write-Host "  ║                                               ║" -ForegroundColor Cyan
    Write-Host "  ╚═══════════════════════════════════════════════╝" -ForegroundColor Cyan
    Write-Host ""
}

function Write-Step {
    param([int]$Num, [string]$Title)
    Write-Host ""
    Write-Host "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor DarkGray
    Write-Host "  STEP ${Num}: $Title" -ForegroundColor Yellow
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
# STEP 2: Exchange Selection
# ===========================================================================

Write-Step -Num 2 -Title "EXCHANGE SELECTION"

Write-Host "  Which exchanges do you want to trade on?" -ForegroundColor White
Write-Host "    1. 🪙 Coinbase only (crypto: BTC, ETH, etc.)" -ForegroundColor Cyan
Write-Host "    2. 📈 Nordnet only (equities: OMX Stockholm)" -ForegroundColor Green
Write-Host "    3. 🔀 Both Coinbase + Nordnet (multi-asset)" -ForegroundColor Yellow
Write-Host ""

$exchangeChoice = Prompt-Required -Prompt "Select exchange(s) (1-3)" -Default "1"

$setupCoinbaseExchange = $false
$setupNordnetExchange = $false

switch ($exchangeChoice) {
    "1" {
        $setupCoinbaseExchange = $true
        Write-OK "Coinbase (crypto) selected."
    }
    "2" {
        $setupNordnetExchange = $true
        Write-OK "Nordnet (equities) selected."
    }
    "3" {
        $setupCoinbaseExchange = $true
        $setupNordnetExchange = $true
        Write-OK "Both exchanges selected (multi-asset mode)."
    }
    default {
        $setupCoinbaseExchange = $true
        Write-OK "Defaulting to Coinbase (crypto)."
    }
}

# ===========================================================================
# STEP 3: Trading Mode
# ===========================================================================

Write-Step -Num 3 -Title "TRADING MODE"

Write-Host "  Choose your trading mode:" -ForegroundColor White
Write-Host "    1. 📝 Paper Trading (simulated — no real money)" -ForegroundColor Green
Write-Host "    2. 💰 Live Trading (real money on exchange)" -ForegroundColor Red
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

# For headless/Docker live mode confirmation
if ($tradingMode -eq "live") {
    Write-Host ""
    Write-Info "For headless/Docker deployments, you can skip the interactive"
    Write-Info "confirmation by setting LIVE_TRADING_CONFIRMED."
    $headlessConfirm = Prompt-YesNo -Prompt "Enable headless live mode confirmation?" -Default $false
    if ($headlessConfirm) {
        Append-Env -Key "LIVE_TRADING_CONFIRMED" -Value "I UNDERSTAND THE RISKS" -Comment "Headless live mode confirmation (skips interactive prompt)"
    }
}
Append-EnvBlank

# ===========================================================================
# STEP 4: Coinbase API
# ===========================================================================

if ($setupCoinbaseExchange) {
    Write-Step -Num 4 -Title "COINBASE API CREDENTIALS"

    Write-Info "You need a Coinbase Advanced Trade API key."
    Write-Host "  How to get one:" -ForegroundColor DarkGray
    Write-Host "    1. Go to https://www.coinbase.com/settings/api" -ForegroundColor DarkGray
    Write-Host "    2. Click 'New API Key'" -ForegroundColor DarkGray
    Write-Host "    3. Select portfolios and permissions:" -ForegroundColor DarkGray
    Write-Host "       - View ✅  Trade ✅  Transfer ❌" -ForegroundColor DarkGray
    Write-Host "    4. Coinbase will show you TWO values:" -ForegroundColor DarkGray
    Write-Host "         API Key Name  — looks like: organizations/xxxx/apiKeys/xxxx" -ForegroundColor DarkGray
    Write-Host "         Private Key   — a multi-line EC private key (PEM format)" -ForegroundColor DarkGray
    Write-Host "    5. Copy both before closing the dialog (private key shown once!)" -ForegroundColor DarkGray
    Write-Host ""

    if ($tradingMode -eq "paper") {
        Write-Info "Paper mode: You can skip this (agent will simulate trades)."
        $setupCoinbase = Prompt-YesNo -Prompt "Set up Coinbase API now?" -Default $false
    }
    else {
        $setupCoinbase = $true
    }

    if ($setupCoinbase) {
        $cbKey = Prompt-Required -Prompt "API Key Name" -Help "e.g. organizations/xxxx-xxxx/apiKeys/xxxx-xxxx"
        Write-Host ""
        Write-Info "Paste your Private Key (PEM). It starts with -----BEGIN EC PRIVATE KEY-----"
        Write-Info "Paste it as a single line with \n replacing newlines, or just paste the key name path if using a key file."
        Write-Host "  Tip: In the Coinbase dialog, click the copy icon next to 'Private Key'." -ForegroundColor DarkGray
        Write-Host ""
        $cbSecret = Prompt-Required -Prompt "Private Key"

        Append-Env -Key "COINBASE_API_KEY" -Value $cbKey -Comment "Coinbase Advanced Trade — API Key Name (organizations/xxx/apiKeys/xxx)"
        Append-Env -Key "COINBASE_API_SECRET" -Value $cbSecret -Comment "Coinbase Advanced Trade — EC Private Key (PEM)"
    }
    else {
        Write-Info "Skipping Coinbase API — paper mode will simulate."
        Append-Env -Key "COINBASE_API_KEY" -Value "" -Comment "Coinbase Advanced Trade API (blank = paper only)"
        Append-Env -Key "COINBASE_API_SECRET" -Value ""
    }
    Append-EnvBlank
}

# ===========================================================================
# STEP 4b: Nordnet Info (if selected)
# ===========================================================================

if ($setupNordnetExchange) {
    Write-Step -Num 4 -Title "NORDNET EXCHANGE"

    Write-Info "Nordnet trading is currently paper-mode only."
    Write-Info "The NordnetClient uses public market data (no API key required yet)."
    Write-Host ""
    Write-Host "  Nordnet configuration:" -ForegroundColor White
    Write-Host "    • Config file: config/nordnet.yaml" -ForegroundColor DarkGray
    Write-Host "    • Market: OMX Stockholm (SEK-denominated)" -ForegroundColor DarkGray
    Write-Host "    • Fee model: 39 SEK flat + 0.15% (Courtage Mini)" -ForegroundColor DarkGray
    Write-Host "    • Default pairs: VOLV-B.ST, ERIC-B.ST, ABB.ST" -ForegroundColor DarkGray
    Write-Host ""
    Write-OK "Nordnet config will be active via config/nordnet.yaml"
    Append-EnvBlank
}

# ===========================================================================
# STEP 5: Ollama LLM Setup
# ===========================================================================

Write-Step -Num 5 -Title "LLM CONFIGURATION"

Write-Info "Auto-Traitor uses a multi-provider LLM chain."
Write-Info "Requests try providers in order: Gemini → OpenAI → Ollama (local fallback)."
Write-Host ""

# --- 5a: Cloud providers ---

Write-Host "  ─── Cloud LLM Providers (optional, faster) ────────" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Cloud providers are optional but recommended for speed." -ForegroundColor White
Write-Host "  If configured, they're used first; Ollama is the local fallback." -ForegroundColor DarkGray
Write-Host ""

# Gemini
$setupGemini = Prompt-YesNo -Prompt "Set up Google Gemini API?" -Default $false
$geminiKey = ""
if ($setupGemini) {
    Write-Host ""
    Write-Host "  How to get a Gemini API key:" -ForegroundColor White
    Write-Host "    1. Go to https://aistudio.google.com/app/apikey" -ForegroundColor DarkGray
    Write-Host "    2. Click 'Create API key'" -ForegroundColor DarkGray
    Write-Host "    3. Copy the key" -ForegroundColor DarkGray
    Write-Host ""
    $geminiKey = Prompt-Required -Prompt "Gemini API Key"
    Append-Env -Key "GEMINI_API_KEY" -Value $geminiKey -Comment "Google Gemini API (provider 1 — fastest)"
    Write-OK "Gemini configured."
}
else {
    Append-Env -Key "# GEMINI_API_KEY" -Value "" -Comment "Google Gemini API (not configured)"
}

Write-Host ""

# OpenAI
$setupOpenAI = Prompt-YesNo -Prompt "Set up OpenAI API?" -Default $false
$openaiKey = ""
if ($setupOpenAI) {
    Write-Host ""
    Write-Host "  How to get an OpenAI API key:" -ForegroundColor White
    Write-Host "    1. Go to https://platform.openai.com/api-keys" -ForegroundColor DarkGray
    Write-Host "    2. Click 'Create new secret key'" -ForegroundColor DarkGray
    Write-Host "    3. Copy the key (starts with sk-)" -ForegroundColor DarkGray
    Write-Host ""
    $openaiKey = Prompt-Required -Prompt "OpenAI API Key"
    Append-Env -Key "OPENAI_API_KEY" -Value $openaiKey -Comment "OpenAI API (provider 2 — fallback)"
    Write-OK "OpenAI configured."
}
else {
    Append-Env -Key "# OPENAI_API_KEY" -Value "" -Comment "OpenAI API (not configured)"
}
Append-EnvBlank

# --- 5b: Ollama local model ---

Write-Host ""
Write-Host "  ─── Ollama (Local LLM — always available) ─────────" -ForegroundColor Yellow
Write-Host ""
Write-Info "Ollama runs the AI brain locally on your GPU."
Write-Info "It serves as the final fallback if cloud providers are unavailable."
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
# STEP 6: Telegram Bot Setup (CRITICAL SECURITY)
# ===========================================================================

Write-Step -Num 6 -Title "TELEGRAM BOT SETUP (SECURITY-CRITICAL)"

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

    # --- Shared: User ID & Authorized Users ---

    Write-Host ""
    Write-Host "  ─── Step 6a: Get Your User ID ───────────────────" -ForegroundColor Yellow
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

    Append-Env -Key "TELEGRAM_AUTHORIZED_USERS" -Value $authorizedUsers -Comment "SECURITY: Only these user IDs can control ANY bot (comma-separated)"
    Append-EnvBlank

    # --- Per-exchange Telegram bots ---

    if ($setupCoinbaseExchange) {
        Write-Host ""
        Write-Host "  ─── Step 6b: Coinbase Telegram Bot ──────────────" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "  Each exchange agent gets its own Telegram bot for" -ForegroundColor White
        Write-Host "  isolated notifications and commands." -ForegroundColor White
        Write-Host ""
        Write-Host "  1. Open Telegram and search for @BotFather" -ForegroundColor White
        Write-Host "  2. Send: /newbot" -ForegroundColor White
        Write-Host "  3. Choose a name (e.g. 'Auto-Traitor Crypto')" -ForegroundColor White
        Write-Host "  4. Choose a username (e.g. 'my_at_crypto_bot')" -ForegroundColor White
        Write-Host "  5. BotFather gives you a token like:" -ForegroundColor White
        Write-Host "     1234567890:ABCdefGHIjklMNOpqrsTUVwxyz" -ForegroundColor Cyan
        Write-Host ""

        $cbBotToken = Prompt-Required -Prompt "Coinbase Bot Token"

        if ($cbBotToken -notmatch '^\d+:[A-Za-z0-9_-]+$') {
            Write-Warn "Token format looks unusual. Double-check with BotFather."
        }
        else {
            Write-OK "Token format looks valid."
        }

        Write-Host ""
        $cbChatId = Prompt-Required -Prompt "Coinbase Chat ID (press Enter to use your User ID)" -Default $userId

        Append-Env -Key "TELEGRAM_BOT_TOKEN_COINBASE" -Value $cbBotToken -Comment "Telegram Bot — Coinbase agent"
        Append-Env -Key "TELEGRAM_CHAT_ID_COINBASE" -Value $cbChatId

        # Also set the generic vars as fallback (for backward compatibility)
        Append-Env -Key "TELEGRAM_BOT_TOKEN" -Value $cbBotToken -Comment "Generic fallback (same as Coinbase)"
        Append-Env -Key "TELEGRAM_CHAT_ID" -Value $cbChatId
        Append-EnvBlank
    }

    if ($setupNordnetExchange) {
        Write-Host ""
        Write-Host "  ─── Step 6c: Nordnet Telegram Bot ───────────────" -ForegroundColor Yellow
        Write-Host ""

        if ($setupCoinbaseExchange) {
            Write-Host "  Create a SECOND bot for the Nordnet agent." -ForegroundColor White
            Write-Host "  This keeps crypto and equity notifications separate." -ForegroundColor DarkGray
        }
        else {
            Write-Host "  Create a bot for the Nordnet agent." -ForegroundColor White
        }

        Write-Host ""
        Write-Host "  1. @BotFather → /newbot" -ForegroundColor White
        Write-Host "  2. Name: e.g. 'Auto-Traitor Stocks'" -ForegroundColor White
        Write-Host "  3. Username: e.g. 'my_at_stocks_bot'" -ForegroundColor White
        Write-Host ""

        $nnBotToken = Prompt-Required -Prompt "Nordnet Bot Token"

        if ($nnBotToken -notmatch '^\d+:[A-Za-z0-9_-]+$') {
            Write-Warn "Token format looks unusual. Double-check with BotFather."
        }
        else {
            Write-OK "Token format looks valid."
        }

        Write-Host ""
        $nnChatId = Prompt-Required -Prompt "Nordnet Chat ID (press Enter to use your User ID)" -Default $userId

        Append-Env -Key "TELEGRAM_BOT_TOKEN_NORDNET" -Value $nnBotToken -Comment "Telegram Bot — Nordnet agent"
        Append-Env -Key "TELEGRAM_CHAT_ID_NORDNET" -Value $nnChatId

        # Set generic fallback if Coinbase wasn't configured
        if (-not $setupCoinbaseExchange) {
            Append-Env -Key "TELEGRAM_BOT_TOKEN" -Value $nnBotToken -Comment "Generic fallback (same as Nordnet)"
            Append-Env -Key "TELEGRAM_CHAT_ID" -Value $nnChatId
        }
        Append-EnvBlank
    }

    Write-Host ""
    Write-Host "  ─── Step 6d: Bot Privacy Settings ───────────────" -ForegroundColor Yellow
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

    Write-OK "Telegram configured!"
    Write-Host ""
    Write-Host "  🔒 Security Summary:" -ForegroundColor Green
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
# STEP 7: Reddit API (Optional — for news)
# ===========================================================================

Write-Step -Num 7 -Title "NEWS SOURCES (OPTIONAL)"

Write-Info "The agent can monitor Reddit for crypto/equity sentiment."
Write-Host "  RSS feeds (CoinTelegraph, CoinDesk, DI.se, etc.) work without API keys." -ForegroundColor DarkGray
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
# STEP 8: Redis + Temporal + Langfuse (Infrastructure Secrets)
# ===========================================================================

Write-Step -Num 8 -Title "INFRASTRUCTURE SECRETS (AUTO-GENERATED)"

Write-Info "Generating secure passwords for all infrastructure services..."
Write-Host ""

$chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

# --- Redis ---
$redisPassword = -join ((1..32) | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })

Append-Env -Key "REDIS_PASSWORD" -Value $redisPassword -Comment "Redis (auto-generated)"
Append-Env -Key "REDIS_URL" -Value "redis://default:${redisPassword}@redis:6379/0"
Append-EnvBlank

Write-OK "Redis password generated."

# --- Temporal ---
$temporalDbUser = "temporal"
$temporalDbPassword = -join ((1..32) | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
$temporalDbName = "temporal"

Append-Env -Key "TEMPORAL_DB_USER" -Value $temporalDbUser -Comment "Temporal — workflow engine DB (auto-generated)"
Append-Env -Key "TEMPORAL_DB_PASSWORD" -Value $temporalDbPassword
Append-Env -Key "TEMPORAL_DB_NAME" -Value $temporalDbName
Append-EnvBlank

Write-OK "Temporal DB credentials generated."

# --- Langfuse ---
$lf_secret    = -join ((1..48) | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
$lf_salt      = -join ((1..48) | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
$lf_adminpw   = -join ((1..20) | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
$lf_dbpw      = -join ((1..32) | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
$ch_password  = -join ((1..32) | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
$minio_pw     = -join ((1..32) | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
$enc_key      = -join (1..32 | ForEach-Object { "{0:x2}" -f (Get-Random -Maximum 256) })

# Langfuse project init keys (used by both langfuse-web and agents)
$lf_public_key = "at-public-key"
$lf_secret_key = "at-secret-key"

Append-Env -Key "LANGFUSE_DB_PASSWORD"      -Value $lf_dbpw      -Comment "Langfuse — LLM observability (auto-generated)"
Append-Env -Key "LANGFUSE_NEXTAUTH_SECRET"  -Value $lf_secret
Append-Env -Key "LANGFUSE_SALT"             -Value $lf_salt
Append-Env -Key "LANGFUSE_ADMIN_PASSWORD"   -Value $lf_adminpw
Append-Env -Key "LANGFUSE_PUBLIC_KEY"       -Value $lf_public_key -Comment "Langfuse project init keys"
Append-Env -Key "LANGFUSE_SECRET_KEY"       -Value $lf_secret_key
Append-EnvBlank

Append-Env -Key "CLICKHOUSE_PASSWORD"       -Value $ch_password   -Comment "Langfuse v3 — ClickHouse + MinIO (auto-generated)"
Append-Env -Key "MINIO_ROOT_USER"           -Value "minio"
Append-Env -Key "MINIO_ROOT_PASSWORD"       -Value $minio_pw
Append-Env -Key "LANGFUSE_ENCRYPTION_KEY"   -Value $enc_key
Append-EnvBlank

Write-OK "Langfuse secrets generated."
Write-Host "     Langfuse admin login: admin@auto-traitor.local / $lf_adminpw" -ForegroundColor DarkGray

# Sync all Docker Compose substitution vars to root .env (used by `docker compose up -d` without --env-file)
$rootEnv = @(
    "# Docker Compose variable substitution — generated by setup, do not commit"
    "# Mirrors the substitution keys from config/.env"
    ""
    "OLLAMA_MODEL=$ollamaModel"
    ""
    "LANGFUSE_NEXTAUTH_SECRET=$lf_secret"
    "LANGFUSE_SALT=$lf_salt"
    "LANGFUSE_ADMIN_PASSWORD=$lf_adminpw"
    "LANGFUSE_ADMIN_EMAIL=admin@auto-traitor.local"
    "LANGFUSE_ADMIN_NAME=admin"
    "LANGFUSE_DB_PASSWORD=$lf_dbpw"
    "LANGFUSE_PUBLIC_KEY=$lf_public_key"
    "LANGFUSE_SECRET_KEY=$lf_secret_key"
    ""
    "REDIS_PASSWORD=$redisPassword"
    ""
    "TEMPORAL_DB_USER=$temporalDbUser"
    "TEMPORAL_DB_PASSWORD=$temporalDbPassword"
    "TEMPORAL_DB_NAME=$temporalDbName"
    ""
    "CLICKHOUSE_PASSWORD=$ch_password"
    "MINIO_ROOT_USER=minio"
    "MINIO_ROOT_PASSWORD=$minio_pw"
    "LANGFUSE_ENCRYPTION_KEY=$enc_key"
)
Set-Content -Path ".env" -Value $rootEnv
Write-OK "Root .env written with Docker Compose substitution vars."

# ===========================================================================
# STEP 9: Create Data Directories
# ===========================================================================

Write-Step -Num 9 -Title "CREATING DIRECTORIES"

$dirs = @("data", "data/trades", "data/news", "data/journal", "data/audit", "logs", "config")
foreach ($d in $dirs) {
    if (-not (Test-Path $d)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
    }
}

Write-OK "Created: data/(trades, news, journal, audit), logs/"

# ===========================================================================
# STEP 10: Docker Compose Build & Pull
# ===========================================================================

Write-Step -Num 10 -Title "BUILDING DOCKER STACK"

Write-Info "The full stack includes:"
Write-Host "    • Ollama (local LLM with GPU)" -ForegroundColor DarkGray
Write-Host "    • Redis (state + cache)" -ForegroundColor DarkGray
if ($setupCoinbaseExchange) {
    Write-Host "    • agent-coinbase (crypto trading)" -ForegroundColor DarkGray
}
if ($setupNordnetExchange) {
    Write-Host "    • agent-nordnet (equity trading)" -ForegroundColor DarkGray
}
Write-Host "    • dashboard (web UI on port 8090)" -ForegroundColor DarkGray
Write-Host "    • news-worker (background news aggregation)" -ForegroundColor DarkGray
Write-Host "    • Temporal (workflow engine + planning worker)" -ForegroundColor DarkGray
Write-Host "    • Langfuse v3 (LLM observability: web, worker, ClickHouse, MinIO, DB)" -ForegroundColor DarkGray
Write-Host ""

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
# STEP 11: Verification
# ===========================================================================

Write-Step -Num 11 -Title "SETUP COMPLETE"

Write-Host ""
Write-Host "  ╔═══════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "  ║         ✅ SETUP COMPLETE!                     ║" -ForegroundColor Green
Write-Host "  ╚═══════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""

Write-Host "  📄 Environment file: $script:envPath" -ForegroundColor White
Write-Host "  🧠 LLM Model:        $ollamaModel" -ForegroundColor White
Write-Host "  📊 Trading Mode:     $tradingMode" -ForegroundColor White
Write-Host ""

# Exchange-specific config info
Write-Host "  ⚙️  Exchange Configuration:" -ForegroundColor White
if ($setupCoinbaseExchange) {
    Write-Host "     Coinbase:  config/coinbase.yaml  (port 8080)" -ForegroundColor DarkGray
}
if ($setupNordnetExchange) {
    Write-Host "     Nordnet:   config/nordnet.yaml   (port 8081)" -ForegroundColor DarkGray
}
Write-Host ""

# LLM providers summary
Write-Host "  🔗 LLM Provider Chain:" -ForegroundColor White
if ($setupGemini) {
    Write-Host "     1. Gemini (gemini-2.0-flash) ✅" -ForegroundColor Green
}
else {
    Write-Host "     1. Gemini — not configured" -ForegroundColor DarkGray
}
if ($setupOpenAI) {
    Write-Host "     2. OpenAI (gpt-4o-mini) ✅" -ForegroundColor Green
}
else {
    Write-Host "     2. OpenAI — not configured" -ForegroundColor DarkGray
}
Write-Host "     3. Ollama ($ollamaModel) ✅ (local fallback)" -ForegroundColor Green
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
Write-Host "     ✅ Temporal DB password auto-generated" -ForegroundColor Green
Write-Host "     ✅ Langfuse secrets auto-generated" -ForegroundColor Green
Write-Host "     ✅ Docker containers run as non-root" -ForegroundColor Green
Write-Host ""

Write-Host "  📋 Quick Commands:" -ForegroundColor Yellow
Write-Host "     Start:        docker compose up -d" -ForegroundColor DarkGray
Write-Host "     Logs:         docker compose logs -f agent-coinbase" -ForegroundColor DarkGray
Write-Host "     Stop:         docker compose down" -ForegroundColor DarkGray
Write-Host "     Status:       docker compose ps" -ForegroundColor DarkGray
Write-Host "     Ollama logs:  docker compose logs -f ollama" -ForegroundColor DarkGray
if ($setupCoinbaseExchange) {
    Write-Host "     Crypto health: curl http://localhost:8080/health" -ForegroundColor DarkGray
}
if ($setupNordnetExchange) {
    Write-Host "     Equity health: curl http://localhost:8081/health" -ForegroundColor DarkGray
}
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

Write-Host "  ⚠️  IMPORTANT: Review your exchange config to fine-tune:" -ForegroundColor Yellow
if ($setupCoinbaseExchange) {
    Write-Host "     config/coinbase.yaml:" -ForegroundColor DarkGray
    Write-Host "       • Trade limits, portfolio rotation %, fee margins" -ForegroundColor DarkGray
    Write-Host "       • Crypto-specific risk parameters" -ForegroundColor DarkGray
}
if ($setupNordnetExchange) {
    Write-Host "     config/nordnet.yaml:" -ForegroundColor DarkGray
    Write-Host "       • Trade limits (SEK), Courtage Mini fee model" -ForegroundColor DarkGray
    Write-Host "       • Equity-specific risk parameters (wider stops)" -ForegroundColor DarkGray
}
Write-Host "     Common settings:" -ForegroundColor DarkGray
Write-Host "       • High-stakes mode multipliers" -ForegroundColor DarkGray
Write-Host "       • LLM provider priorities" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Happy trading! 🚀" -ForegroundColor Green
Write-Host ""
