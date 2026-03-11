"""OpenTraitor Main Entry Point - Autonomous LLM Trading Agent.

Usage:
    python -m src.main --mode daemon     # Run as background daemon
    python -m src.main --mode paper      # Run in paper trading mode
    python -m src.main --mode live       # Run with real money (⚠️)
"""

from __future__ import annotations

import argparse
import os
import sys
import signal
import threading
import time

import uvicorn
import yaml
from dotenv import load_dotenv

from src.core.coinbase_client import CoinbaseClient
from src.core.exchange_client import ExchangeClient
from src.core.llm_client import LLMClient, build_providers
from src.core.orchestrator import Orchestrator
from src.core.rules import AbsoluteRules
from src.core.ws_feed import CoinbaseWebSocketFeed
from src.news.aggregator import NewsAggregator
from src.utils.logger import setup_logger, get_logger
from src.utils.security import validate_env_credentials
from src.utils.tracer import LLMTracer, get_llm_tracer


def load_config() -> dict:
    """Load configuration from settings.yaml."""
    config_path = os.environ.get("AUTO_TRAITOR_CONFIG", os.path.join("config", "settings.yaml"))
    if not os.path.exists(config_path):
        print(f"❌ Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, "r") as f:
        return yaml.safe_load(f) or {}


def print_banner(mode: str, exchange_type: str = "") -> None:
    """Print a beautiful startup banner."""
    # Determine agent label based on exchange type
    exchange_labels = {
        "coinbase": "Crypto",
        "ibkr": "Equities",
    }
    agent_label = exchange_labels.get(exchange_type.lower(), "Trading")
    banner = f"""
    ╔═══════════════════════════════════════════════════════╗
    ║                                                       ║
    ║   █████╗ ██╗   ██╗████████╗ ██████╗                   ║
    ║  ██╔══██╗██║   ██║╚══██╔══╝██╔═══██╗                  ║
    ║  ███████║██║   ██║   ██║   ██║   ██║                  ║
    ║  ██╔══██║██║   ██║   ██║   ██║   ██║                  ║
    ║  ██║  ██║╚██████╔╝   ██║   ╚██████╔╝                  ║
    ║  ╚═╝  ╚═╝ ╚═════╝    ╚═╝    ╚═════╝                   ║
    ║                                                       ║
    ║  ████████╗██████╗  █████╗ ██╗████████╗ ██████╗ ██████╗ ║
    ║  ╚══██╔══╝██╔══██╗██╔══██╗██║╚══██╔══╝██╔═══██╗██╔══██╗║
    ║     ██║   ██████╔╝███████║██║   ██║   ██║   ██║██████╔╝║
    ║     ██║   ██╔══██╗██╔══██║██║   ██║   ██║   ██║██╔══██╗║
    ║     ██║   ██║  ██║██║  ██║██║   ██║   ╚██████╔╝██║  ██║║
    ║     ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝   ╚═╝    ╚═════╝ ╚═╝  ╚═╝║
    ║                                                       ║
    ║   🤖 Autonomous LLM {agent_label} Trading Agent{' ' * (14 - len(agent_label))}║
    ║   📡 Powered by Ollama (Local LLM)                    ║
    ║                                                       ║
    ╚═══════════════════════════════════════════════════════╝
    """
    print(banner)
    mode_display = {"paper": "📝 PAPER TRADING", "live": "💰 LIVE TRADING", "daemon": "🔄 DAEMON MODE"}
    print(f"    Mode: {mode_display.get(mode, mode)}")
    print()


def main():
    # Parse arguments
    parser = argparse.ArgumentParser(description="OpenTraitor Trading Agent")
    parser.add_argument(
        "--mode",
        choices=["paper", "live", "daemon"],
        default="paper",
        help="Trading mode",
    )
    parser.add_argument(
        "--config",
        default="config/settings.yaml",
        help="Path to the configuration file (determines profile)",
    )
    args = parser.parse_args()

    # Determine profile name and set env vars
    config_name = os.path.splitext(os.path.basename(args.config))[0]
    profile = config_name.replace("settings_", "") if config_name.startswith("settings_") else config_name
    os.environ["AUTO_TRAITOR_PROFILE"] = profile
    os.environ["AUTO_TRAITOR_CONFIG"] = args.config

    # Load environment
    load_dotenv(os.path.join("config", ".env"))

    # Load config
    config = load_config()

    # Override mode from args or env
    mode = os.environ.get("TRADING_MODE", args.mode)
    if mode == "daemon":
        mode = config.get("trading", {}).get("mode", "paper")

    paper_mode = mode != "live"

    # Setup logging
    log_config = config.get("logging", {})
    setup_logger(
        log_level=log_config.get("level", "INFO"),
        log_dir=log_config.get("directory", "logs"),
        file_enabled=log_config.get("file_enabled", True),
    )

    logger = get_logger("main")
    exchange_type = config.get("trading", {}).get("exchange", "coinbase").lower()
    print_banner(mode, exchange_type=exchange_type)

    # Safety confirmation for live mode
    if not paper_mode:
        logger.warning("⚠️ ⚠️ ⚠️  LIVE TRADING MODE — REAL MONEY AT RISK  ⚠️ ⚠️ ⚠️")
        # Allow headless/Docker deployments to confirm via environment variable
        if os.environ.get("LIVE_TRADING_CONFIRMED", "").strip() == "I UNDERSTAND THE RISKS":
            logger.warning("Live mode confirmed via LIVE_TRADING_CONFIRMED environment variable.")
        else:
            try:
                confirm = input("Type 'I UNDERSTAND THE RISKS' to continue: ")
            except EOFError:
                print("Aborting: no interactive terminal and LIVE_TRADING_CONFIRMED not set.")
                sys.exit(1)
            if confirm != "I UNDERSTAND THE RISKS":
                print("Aborting.")
                sys.exit(0)

    # =========================================================================
    # Initialize Components
    # =========================================================================

    # Redis
    redis_client = None
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        try:
            import redis
            redis_client = redis.Redis.from_url(redis_url)
            redis_client.ping()
            logger.info("✅ Redis connected")
        except Exception as e:
            logger.warning(f"⚠️ Redis not available: {e}")

    # LLM Tracer (Langfuse)
    dash_config = config.get("dashboard", {})
    if dash_config.get("langfuse_enabled", True):
        _langfuse_pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        _langfuse_sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
        if not _langfuse_pk or not _langfuse_sk:
            logger.info("ℹ️ Langfuse keys not set — LLM tracing disabled")
        else:
            try:
                LLMTracer.init(
                    public_key=_langfuse_pk,
                    secret_key=_langfuse_sk,
                    host=os.environ.get(
                        "LANGFUSE_HOST",
                        dash_config.get("langfuse_host", "http://localhost:3000"),
                    ),
                    redis_client=redis_client,
                )
                logger.info("✅ LLM Tracer (Langfuse) initialised")
            except Exception as e:
                logger.warning(f"⚠️ LLM Tracer init failed: {e} — tracing disabled")

    # LLM — multi-provider chain (Gemini → OpenAI → Ollama)
    llm_config = config.get("llm", {})
    ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    fallback_model = os.environ.get("OLLAMA_MODEL", llm_config.get("model", "llama3.1:8b"))

    providers_config = llm_config.get("providers", [])
    if providers_config:
        logger.info("🔗 Building LLM provider chain...")
        providers = build_providers(
            providers_config,
            fallback_base_url=ollama_url,
            fallback_model=fallback_model,
            fallback_timeout=llm_config.get("timeout", 60),
            fallback_max_retries=llm_config.get("max_retries", 3),
        )
    else:
        providers = None  # backward compat: single Ollama

    llm = LLMClient(
        base_url=ollama_url,
        model=fallback_model,
        temperature=llm_config.get("temperature", 0.2),
        max_tokens=llm_config.get("max_tokens", 2000),
        max_retries=llm_config.get("max_retries", 3),
        timeout=llm_config.get("timeout", 60),
        persona=llm_config.get("persona", ""),
        providers=providers,
    )

    # Store providers config for recovery polling / rescan
    if providers_config:
        llm.update_providers_config(
            providers_config,
            fallback_base_url=ollama_url,
            fallback_model=fallback_model,
            fallback_timeout=llm_config.get("timeout", 60),
            fallback_max_retries=llm_config.get("max_retries", 3),
        )

    # Wait for at least one LLM provider to be ready
    has_cloud = any(not p.is_local for p in llm._providers)
    if has_cloud:
        # Cloud providers available — just check Ollama status without blocking
        for p in llm._providers:
            if p.is_local:
                try:
                    import requests as _req
                    _url = str(p.client.base_url).rstrip("/").removesuffix("/v1")
                    _resp = _req.get(f"{_url}/api/tags", timeout=5)
                    if _resp.status_code == 200:
                        logger.info("✅ Ollama is ready (fallback)")
                    else:
                        logger.info("ℹ️ Ollama not ready yet (cloud providers are primary)")
                except Exception:
                    logger.info("ℹ️ Ollama not ready yet (cloud providers are primary)")
    else:
        # Ollama-only — wait for it
        logger.info("⏳ Waiting for Ollama to be ready...")
        for attempt in range(30):
            if llm.is_available():
                logger.info("✅ Ollama is ready!")
                break
            logger.debug(f"Ollama not ready yet (attempt {attempt + 1}/30)...")
            time.sleep(5)
        else:
            logger.warning("⚠️ Ollama not responding — will retry during operation")

    # Exchange Client Selection
    exchange_type = config.get("trading", {}).get("exchange", "coinbase").lower()
    
    if exchange_type == "ibkr":
        # C5 fix: Import IBClient BEFORE try block to ensure it's available in except handler
        from src.core.ib_client import IBClient

        try:
            exchange: ExchangeClient = IBClient(
                paper_mode=paper_mode,
                paper_slippage_pct=config.get("trading", {}).get("paper_slippage_pct", 0.0003),
                ib_host=os.environ.get("IBKR_HOST", "127.0.0.1"),
                ib_port=int(os.environ.get("IBKR_PORT", "4002")),
                ib_client_id=int(os.environ.get("IBKR_CLIENT_ID", "1")),
            )
        except Exception as e:
            if not paper_mode:
                logger.critical(
                    f"❌ IBKR client init failed in LIVE mode: {e}. "
                    "Refusing to silently fall back to paper. Fix the connection or switch config to paper mode."
                )
                sys.exit(1)
            logger.warning(f"⚠️ Could not initialise IBKR client in paper mode: {e}")
            raise
    else:
        exchange: ExchangeClient = CoinbaseClient(
            api_key=os.environ.get("COINBASE_API_KEY"),
            api_secret=os.environ.get("COINBASE_API_SECRET"),
            paper_mode=paper_mode,
            paper_slippage_pct=config.get("trading", {}).get("paper_slippage_pct", 0.0005),
        )

    # -------------------------------------------------------------------------
    # API health-check & dynamic currency / pair adaption
    # -------------------------------------------------------------------------
    conn = exchange.check_connection()
    if conn["ok"]:
        logger.info(f"✅ Exchange API ({exchange.__class__.__name__}): {conn['message']}")
        if conn.get("non_zero_accounts") is not None:
            logger.info(
                f"   Accounts with balance: {conn['non_zero_accounts']} / "
                f"{conn.get('total_accounts', '?')}"
            )
    else:
        err = conn.get("error", "unknown error")
        logger.error(f"❌ Exchange API connection failed: {err}")
        if not paper_mode:
            logger.critical(
                "Cannot run in LIVE mode without Exchange API access. "
                "Refusing to silently fall back to paper — fix the connection or switch config to paper mode."
            )
            sys.exit(1)
        else:
            logger.warning("Continuing in paper mode — using mock market data.")

    # Auto-detect the account's native fiat currency (e.g. EUR for EU accounts)
    # and rewrite the configured trading pairs accordingly so the bot actually
    # trades against the currency sitting in the wallet (EUR, GBP, ...).
    quote_currency_setting = config.get("trading", {}).get("quote_currency", "auto").upper()
    native_currency: str = "USD"
    
    if quote_currency_setting != "AUTO":
        native_currency = quote_currency_setting
    elif hasattr(exchange, "detect_native_currency"):
        native_currency = exchange.detect_native_currency()

    # Pair discovery: "all" = discover every tradable pair on Coinbase for the
    # configured quote currencies; "configured" = use only settings.yaml pairs
    pair_discovery = config.get("trading", {}).get("pair_discovery", "configured").lower()
    quote_currencies = config.get("trading", {}).get(
        "quote_currencies", [native_currency]
    )

    if hasattr(exchange, "discover_all_pairs"):
        if pair_discovery == "all":
            # Discover ALL tradable pairs on Exchange for our quote currencies
            abs_rules_cfg = config.get("absolute_rules", {})
            never_trade = set(abs_rules_cfg.get("never_trade_pairs", []))
            only_trade = set(abs_rules_cfg.get("only_trade_pairs", []))
            discovered = exchange.discover_all_pairs(
                quote_currencies=quote_currencies,
                never_trade=never_trade if never_trade else None,
                only_trade=only_trade if only_trade else None,
            )
            if discovered:
                logger.info(
                    f"🔍 Full pair discovery: found {len(discovered)} tradable pairs "
                    f"for quote currencies {quote_currencies}"
                )
                config.setdefault("trading", {})["pairs"] = discovered
            else:
                logger.warning(
                    "⚠️ Full pair discovery returned 0 pairs — falling back to configured pairs"
                )
        else:
            # Legacy mode: expand configured pairs via asset-based discovery
            raw_pairs: list[str] = list(config.get("trading", {}).get("pairs", ["BTC-USD"]))
            adapted_pairs = exchange.adapt_pairs_to_account(raw_pairs, native_currency) if hasattr(exchange, "adapt_pairs_to_account") else raw_pairs
            if set(adapted_pairs) != set(raw_pairs):
                logger.info(
                    f"🌍 Trading pairs dynamically expanded: "
                    f"{raw_pairs} → {adapted_pairs}"
                )
                config.setdefault("trading", {})["pairs"] = adapted_pairs
            else:
                logger.info(f"✓ Trading pairs unchanged: {adapted_pairs}")

    # Seed known pairs so live-mode discover_all_pairs_detailed() has a
    # baseline universe even when the IB Scanner is unavailable.
    resolved_pairs = config.get("trading", {}).get("pairs", [])
    if hasattr(exchange, "seed_known_pairs") and resolved_pairs:
        exchange.seed_known_pairs(list(resolved_pairs))

    # Paper mode: initialise paper balance in the account's native currency
    # so P&L figures are denominated correctly (e.g. EUR not USD).
    if getattr(exchange, "paper_mode", False) and native_currency != "USD" and hasattr(exchange, "_paper_balance"):
        # M6 fix: acquire the paper balance lock for thread safety
        _balance_lock = getattr(exchange, "_paper_balance_lock", None)
        if _balance_lock:
            with _balance_lock:
                initial_paper = exchange._paper_balance.pop("USD", 10_000.0)
                exchange._paper_balance[native_currency] = initial_paper
        else:
            initial_paper = exchange._paper_balance.pop("USD", 10_000.0)
            exchange._paper_balance[native_currency] = initial_paper
        logger.info(f"📝 Paper balance: {initial_paper:,.2f} {native_currency}")

    # Absolute Rules — scoped to this profile's exchange to prevent cross-domain counter bleed
    _exchange_id = config.get("trading", {}).get("exchange", "coinbase").lower()
    rules = AbsoluteRules(config.get("absolute_rules", {}), exchange=_exchange_id)
    rules.seed_daily_counters()  # Seed today's counters from DB (survives restarts)

    # News Aggregator
    news_config = config.get("news", {})
    news_aggregator = NewsAggregator(
        config=news_config,
        redis_client=redis_client,
        reddit_client_id=os.environ.get("REDDIT_CLIENT_ID", ""),
        reddit_client_secret=os.environ.get("REDDIT_CLIENT_SECRET", ""),
        reddit_user_agent=os.environ.get("REDDIT_USER_AGENT", "opentraitor-bot/0.1"),
        profile=profile,
        exchange_client=exchange,
    )

    # Validate credentials
    logger.info("🔐 Validating credentials...")
    cred_status = validate_env_credentials()

    # Telegram Bot
    telegram_bot = None
    telegram_config = config.get("telegram", {})

    # Resolve token and chat_id from config-specified env var names
    # (allows per-exchange Telegram bots: TELEGRAM_BOT_TOKEN_COINBASE, etc.)
    _token_env = telegram_config.get("bot_token_env", "TELEGRAM_BOT_TOKEN")
    _chat_env = telegram_config.get("chat_id_env", "TELEGRAM_CHAT_ID")
    _auth_env = telegram_config.get("authorized_users_env", "TELEGRAM_AUTHORIZED_USERS")

    telegram_token = telegram_config.get("bot_token") or os.environ.get(_token_env) or os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = telegram_config.get("chat_id") or os.environ.get(_chat_env) or os.environ.get("TELEGRAM_CHAT_ID")
    
    if telegram_token and telegram_chat_id:
        from src.telegram_bot.bot import TelegramBot

        # SECURITY: TELEGRAM_AUTHORIZED_USERS is REQUIRED.
        authorized_raw = telegram_config.get("authorized_users") or os.environ.get(_auth_env) or os.environ.get("TELEGRAM_AUTHORIZED_USERS", "")
        if not authorized_raw:
            logger.error(
                "❌ Telegram authorized_users is not set! "
                "This is REQUIRED for security. Set it in settings.yaml or env vars."
            )
            sys.exit(1)

        if isinstance(authorized_raw, list):
            authorized_list = [str(u).strip() for u in authorized_raw if str(u).strip()]
        else:
            authorized_list = [u.strip() for u in str(authorized_raw).split(",") if u.strip()]
            
        logger.info(f"🔒 Telegram authorized users: {authorized_list}")

        # Detect if another agent is already polling with this token (via Redis)
        _bot_mode = telegram_config.get("mode", "controller")
        if _bot_mode == "controller" and redis_client:
            import hashlib as _hashlib
            _token_hash = _hashlib.sha256(telegram_token.encode()).hexdigest()[:16]
            _lock_key = f"telegram:poller:{_token_hash}"
            try:
                # Try to acquire the poller lock (60s TTL, renewed by the polling thread)
                _acquired = redis_client.set(_lock_key, profile, nx=True, ex=120)
                if not _acquired:
                    _holder = redis_client.get(_lock_key)
                    _holder_str = _holder.decode() if isinstance(_holder, bytes) else str(_holder)
                    logger.warning(
                        f"⚠️ Telegram bot token already polled by '{_holder_str}' — "
                        f"switching to REPORTING mode (outbound only) for '{profile}'"
                    )
                    _bot_mode = "reporting"
                else:
                    logger.info(f"🔒 Acquired Telegram poller lock for profile '{profile}'")
            except Exception as e:
                logger.warning(f"Redis Telegram lock check failed: {e} — proceeding as controller")

        # Determine exchange display name for Telegram messages
        _exchange_name = config.get("trading", {}).get("exchange", profile or "opentraitor").upper()
        _currency = config.get("trading", {}).get("quote_currency", "EUR")

        telegram_bot = TelegramBot(
            bot_token=telegram_token,
            chat_id=telegram_chat_id,
            authorized_users=authorized_list,
            mode=_bot_mode,
            exchange_name=_exchange_name,
        )
        telegram_bot.start()
        logger.info(f"📱 Telegram bot started (mode={_bot_mode}, exchange={_exchange_name})")
        # Give the polling thread a moment to connect, then send startup ping
        time.sleep(2)
        telegram_bot.send_message(
            f"👋 *OpenTraitor [{_exchange_name}] is online!*\n\n"
            f"Mode: `{mode.upper()}` | Currency: `{_currency}`\n"
            f"Profile: `{profile}`\n"
            f"Telegram mode: `{_bot_mode}`\n"
            f"Ready and listening. 🚀"
        )
    else:
        logger.warning("⚠️ Telegram not configured — running without notifications")

    # WebSocket Feed (real-time prices)
    ws_feed = None
    pairs = config.get("trading", {}).get("pairs", ["BTC-USD"])
    watchlist_pairs = config.get("trading", {}).get("watchlist_pairs", [])
    all_pairs_to_track = list(set(list(pairs) + list(watchlist_pairs)))
    if not paper_mode or os.environ.get("COINBASE_API_KEY"):
        try:
            ws_feed = CoinbaseWebSocketFeed(
                product_ids=all_pairs_to_track,
                api_key=os.environ.get("COINBASE_API_KEY"),
                api_secret=os.environ.get("COINBASE_API_SECRET"),
            )
            ws_feed.start()
            logger.info("📡 WebSocket feed started for real-time prices")
        except Exception as e:
            logger.warning(f"⚠️ WebSocket feed failed: {e} — using REST polling")
    else:
        logger.info("📡 WebSocket skipped (paper mode, no API key)")

    # =========================================================================
    # Create Orchestrator & Start
    # =========================================================================

    orchestrator = Orchestrator(
        config=config,
        exchange=exchange,
        llm=llm,
        rules=rules,
        news_aggregator=news_aggregator,
        telegram_bot=telegram_bot,
        redis_client=redis_client,
        ws_feed=ws_feed,
    )


    # Handle graceful shutdown
    def shutdown_handler(signum, frame):
        logger.info("🛑 Shutdown signal received...")
        orchestrator.state.is_running = False
        # Flush pending Langfuse events before exit
        _tracer = get_llm_tracer()
        if _tracer:
            _tracer.flush()
        if telegram_bot:
            telegram_bot.send_message("🛑 *OpenTraitor shutting down...*")

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Fetch initial news
    logger.info("📰 Fetching initial news...")
    try:
        news_aggregator.fetch_all()
    except Exception as e:
        logger.warning(f"Initial news fetch failed: {e}")

    # Start the main loop
    orchestrator.run_forever()

    # Cleanup
    logger.info("Saving final state...")
    orchestrator.state.save_state()
    if ws_feed:
        ws_feed.stop()
    logger.info("👋 OpenTraitor shut down cleanly.")


if __name__ == "__main__":
    main()
