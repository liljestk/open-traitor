"""
Auto-Traitor Main Entry Point — Autonomous LLM Crypto Trading Agent.

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
from src.core.llm_client import LLMClient
from src.core.orchestrator import Orchestrator
from src.core.rules import AbsoluteRules
from src.core.ws_feed import CoinbaseWebSocketFeed
from src.news.aggregator import NewsAggregator
from src.utils.logger import setup_logger, get_logger
from src.utils.security import validate_env_credentials
from src.utils.tracer import LLMTracer, get_llm_tracer


def load_config() -> dict:
    """Load configuration from settings.yaml."""
    config_path = os.path.join("config", "settings.yaml")
    if not os.path.exists(config_path):
        print(f"❌ Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def print_banner(mode: str) -> None:
    """Print a beautiful startup banner."""
    banner = """
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
    ║   🤖 Autonomous LLM Crypto Trading Agent              ║
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
    parser = argparse.ArgumentParser(description="Auto-Traitor Trading Agent")
    parser.add_argument(
        "--mode",
        choices=["paper", "live", "daemon"],
        default="paper",
        help="Trading mode",
    )
    args = parser.parse_args()

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
    print_banner(mode)

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

    # Ollama LLM
    llm_config = config.get("llm", {})
    ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    model = os.environ.get("OLLAMA_MODEL", llm_config.get("model", "llama3.1:8b"))

    llm = LLMClient(
        base_url=ollama_url,
        model=model,
        temperature=llm_config.get("temperature", 0.2),
        max_tokens=llm_config.get("max_tokens", 2000),
        max_retries=llm_config.get("max_retries", 3),
        timeout=llm_config.get("timeout", 60),
        persona=llm_config.get("persona", ""),
    )

    # Wait for Ollama to be ready
    logger.info("⏳ Waiting for Ollama to be ready...")
    for attempt in range(30):
        if llm.is_available():
            logger.info("✅ Ollama is ready!")
            break
        logger.debug(f"Ollama not ready yet (attempt {attempt + 1}/30)...")
        time.sleep(5)
    else:
        logger.warning("⚠️ Ollama not responding — will retry during operation")

    # Coinbase Client
    coinbase = CoinbaseClient(
        api_key=os.environ.get("COINBASE_API_KEY"),
        api_secret=os.environ.get("COINBASE_API_SECRET"),
        paper_mode=paper_mode,
        paper_slippage_pct=config.get("trading", {}).get("paper_slippage_pct", 0.0005),
    )

    # Absolute Rules
    rules = AbsoluteRules(config.get("absolute_rules", {}))
    rules.seed_daily_counters()  # Seed today's counters from DB (survives restarts)

    # News Aggregator
    news_config = config.get("news", {})
    news_aggregator = NewsAggregator(
        config=news_config,
        redis_client=redis_client,
        reddit_client_id=os.environ.get("REDDIT_CLIENT_ID", ""),
        reddit_client_secret=os.environ.get("REDDIT_CLIENT_SECRET", ""),
        reddit_user_agent=os.environ.get("REDDIT_USER_AGENT", "auto-traitor-bot/0.1"),
    )

    # Validate credentials
    logger.info("🔐 Validating credentials...")
    cred_status = validate_env_credentials()

    # Telegram Bot
    telegram_bot = None
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if telegram_token and telegram_chat_id:
        from src.telegram_bot.bot import TelegramBot

        # SECURITY: TELEGRAM_AUTHORIZED_USERS is REQUIRED.
        # We do NOT fall back to chat_id because chat_id could be a group,
        # which would let ANY group member control the bot.
        authorized_raw = os.environ.get("TELEGRAM_AUTHORIZED_USERS", "")
        if not authorized_raw.strip():
            logger.error(
                "❌ TELEGRAM_AUTHORIZED_USERS is not set! "
                "This is REQUIRED for security. "
                "Set it to your numeric Telegram user ID. "
                "Message @userinfobot on Telegram to get your ID."
            )
            sys.exit(1)

        authorized_list = [u.strip() for u in authorized_raw.split(",") if u.strip()]
        logger.info(f"🔒 Telegram authorized users: {authorized_list}")

        telegram_bot = TelegramBot(
            bot_token=telegram_token,
            chat_id=telegram_chat_id,
            authorized_users=authorized_list,
        )
        telegram_bot.start()
        logger.info("📱 Telegram bot started")
        # Give the polling thread a moment to connect, then send startup ping
        time.sleep(2)
        telegram_bot.send_message(
            f"👋 *Auto-Traitor is online!*\n\n"
            f"Mode: `{mode.upper()}`\n"
            f"Ready and listening. 🚀"
        )
    else:
        logger.warning("⚠️ Telegram not configured — running without notifications")

    # WebSocket Feed (real-time prices)
    ws_feed = None
    pairs = config.get("trading", {}).get("pairs", ["BTC-USD"])
    if not paper_mode or os.environ.get("COINBASE_API_KEY"):
        try:
            ws_feed = CoinbaseWebSocketFeed(
                product_ids=pairs,
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
        coinbase=coinbase,
        llm=llm,
        rules=rules,
        news_aggregator=news_aggregator,
        telegram_bot=telegram_bot,
        redis_client=redis_client,
        ws_feed=ws_feed,
    )

    # Dashboard server
    if dash_config.get("enabled", True):
        from src.dashboard.server import create_app
        dash_app = create_app(
            stats_db=orchestrator.stats_db,
            redis_client=redis_client,
            temporal_client=None,   # Temporal client wired if available
            config=config,
        )
        dash_port = int(dash_config.get("port", 8090))

        def _run_dashboard():
            uvicorn.run(
                dash_app,
                host="0.0.0.0",
                port=dash_port,
                log_level="warning",
                access_log=False,
            )

        dashboard_thread = threading.Thread(
            target=_run_dashboard,
            name="dashboard",
            daemon=True,
        )
        dashboard_thread.start()
        logger.info(f"📊 Dashboard started on http://0.0.0.0:{dash_port}")

    # Handle graceful shutdown
    def shutdown_handler(signum, frame):
        logger.info("🛑 Shutdown signal received...")
        orchestrator.state.is_running = False
        if telegram_bot:
            telegram_bot.send_message("🛑 *Auto-Traitor shutting down...*")

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
    logger.info("👋 Auto-Traitor shut down cleanly.")


if __name__ == "__main__":
    main()
