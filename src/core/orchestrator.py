"""
Orchestrator — Coordinates all agents in a continuous loop.
Manages tasks, handles Telegram commands, and runs autonomously.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from src.agents.market_analyst import MarketAnalystAgent
from src.agents.strategist import StrategistAgent
from src.agents.risk_manager import RiskManagerAgent
from src.agents.executor import ExecutorAgent
from src.agents.settings_advisor import SettingsAdvisorAgent, format_advisor_notification
from src.core.exchange_client import ExchangeClient
from src.core.llm_client import LLMClient
from src.core.rules import AbsoluteRules
from src.core.state import TradingState
from src.core.ws_feed import CoinbaseWebSocketFeed
from src.core.trailing_stop import TrailingStopManager
from src.core.health import check_component_health, update_health, start_health_server
from src.core.fee_manager import FeeManager
from src.core.high_stakes import HighStakesManager
from src.core.market_hours import is_market_open as _is_market_open
from src.core.portfolio_rotator import PortfolioRotator
from src.core.portfolio_scaler import PortfolioScaler
from src.core.route_finder import RouteFinder
from src.analysis.fear_greed import FearGreedIndex
from src.analysis.multi_timeframe import MultiTimeframeAnalyzer
from src.analysis.sentiment import SentimentAnalyzer
from src.strategies import EMACrossoverStrategy, BollingerReversionStrategy, PairsCorrelationMonitor
from src.utils.tax import FIFOTracker
from src.news.aggregator import NewsAggregator
from src.telegram_bot.chat_handler import TelegramChatHandler
from src.utils.logger import get_logger, sanitize_exception
from src.utils.helpers import format_currency, format_percentage
from src.utils.rate_limiter import get_rate_limiter
from src.utils.journal import TradeJournal
from src.utils.audit import AuditLog
from src.utils.stats import StatsDB
from src.utils.tracer import get_llm_tracer
from src.utils.training_data import TrainingDataCollector
from src.utils import settings_manager as sm
from src.utils.rpm_budget import compute_rpm_entity_cap
from src.core.managers.pipeline_manager import PipelineManager
from src.core.managers.state_manager import StateManager
from src.core.managers.telegram_manager import TelegramManager
from src.core.managers.universe_scanner import UniverseScanner
from src.core.managers.holdings_manager import HoldingsManager
from src.core.managers.context_manager import ContextManager
from src.core.managers.event_manager import EventManager
from src.core.managers.dashboard_commands import DashboardCommandManager
from src.core.managers.learning_manager import LearningManager
import asyncio

logger = get_logger("core.orchestrator")


class Task:
    """A user-defined trading task with constraints."""

    def __init__(
        self,
        description: str,
        max_spend: Optional[float] = None,
        pair: Optional[str] = None,
    ):
        self.id = f"task_{uuid.uuid4().hex[:8]}"
        self.description = description
        self.max_spend = max_spend
        self.pair = pair
        self.created_at = datetime.now(timezone.utc)
        self.completed = False
        self.spent = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "max_spend": self.max_spend,
            "pair": self.pair,
            "created_at": self.created_at.isoformat(),
            "completed": self.completed,
            "spent": self.spent,
        }


class Orchestrator:
    """
    Main orchestrator that runs all agents in a continuous loop.
    Handles the complete trading pipeline:
    1. Fetch market data & news
    2. Market analysis (technical + sentiment)
    3. Strategy generation
    4. Risk validation
    5. Trade execution
    6. Position monitoring
    """

    def __init__(
        self,
        config: dict,
        exchange: ExchangeClient,
        llm: LLMClient,
        rules: AbsoluteRules,
        news_aggregator: Optional[NewsAggregator] = None,
        telegram_bot=None,
        redis_client=None,
        ws_feed: Optional[CoinbaseWebSocketFeed] = None,
    ):
        self.config = config
        self.exchange = exchange
        self.llm = llm
        self.rules = rules
        self.news = news_aggregator
        self.telegram = telegram_bot
        self.redis = redis_client
        self.ws_feed = ws_feed
        self.rate_limiter = get_rate_limiter()
        self._dashboard_command_signing_key = (
            os.environ.get("DASHBOARD_COMMAND_SIGNING_KEY", "")
            or os.environ.get("DASHBOARD_API_KEY", "")
        )
        self._dashboard_command_max_age_seconds = int(
            config.get("dashboard", {}).get("command_max_age_seconds", 120)
        )
        # M20 fix: nonce replay protection — track recently used nonces
        self._used_nonces: dict[str, float] = {}  # nonce → monotonic timestamp
        self._nonce_lock = threading.Lock()
        if not self._dashboard_command_signing_key:
            logger.warning(
                "⚠️ DASHBOARD_COMMAND_SIGNING_KEY not configured; unsigned dashboard "
                "commands will be rejected"
            )

        # Persistent event loop for async operations within the sync run_forever() loop.
        # Avoids repeatedly creating/destroying event loops via asyncio.run().
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)  # M5 fix: register as thread-current loop

        # Trading state
        if getattr(exchange, "paper_mode", False):
            initial_balance = 10000.0
        else:
            try:
                initial_balance = exchange.get_portfolio_value()
            except Exception as _e:
                logger.warning(f"⚠️ Could not fetch live portfolio value on startup: {_e} — defaulting to 0")
                initial_balance = 0.0
        self.state = TradingState(initial_balance=initial_balance)
        self.state.is_running = True

        # ─── Set native currency on TradingState from exchange / config ───
        _CURRENCY_SYMBOLS = {"EUR": "€", "GBP": "£", "CHF": "CHF ", "USD": "$", "CAD": "C$", "AUD": "A$", "JPY": "¥"}
        _native = config.get("trading", {}).get("quote_currency", "auto").upper()
        if _native == "AUTO":
            _native = getattr(exchange, "_native_currency", None) or "USD"
        self.state.native_currency = _native
        self.state.currency_symbol = _CURRENCY_SYMBOLS.get(_native, _native + " ")

        # Trading pairs (copy-on-write: always replace the list, never mutate in-place)
        self._pairs_lock = threading.Lock()
        self.pairs: list[str] = list(config.get("trading", {}).get("pairs", ["BTC-USD"]))
        self.watchlist_pairs = config.get("trading", {}).get("watchlist_pairs", [])
        self.all_tracked_pairs = list(set(self.pairs + self.watchlist_pairs))
        self.interval = config.get("trading", {}).get("interval", 120)

        # Tell the health endpoint about our cycle interval
        from src.core.health import _lock as _health_lock
        import src.core.health as _health_mod
        with _health_lock:
            _health_mod._cycle_interval = self.interval

        # Portfolio scaler — adapts limits based on account tier
        self.portfolio_scaler = PortfolioScaler(config)

        # Wire scaler into the rules engine so tier-aware limits are used
        rules.set_portfolio_scaler(self.portfolio_scaler)

        # Initialize agents
        self.market_analyst = MarketAnalystAgent(llm, self.state, config)
        self.strategist = StrategistAgent(llm, self.state, config)
        self.risk_manager = RiskManagerAgent(
            llm, self.state, config, rules,
            portfolio_scaler=self.portfolio_scaler,
        )
        self.executor = ExecutorAgent(llm, self.state, config, exchange, rules, telegram=telegram_bot)
        self.settings_advisor = SettingsAdvisorAgent(
            llm, self.state, config, rules,
            review_interval=config.get("trading", {}).get("settings_review_interval", 10),
        )

        # New analysis components
        self.fear_greed = FearGreedIndex()
        self.multi_tf = MultiTimeframeAnalyzer(config, exchange)
        self.trailing_stops = TrailingStopManager(
            default_trail_pct=config.get("risk", {}).get("trailing_stop_pct", 0.03),
            enable_tiers=config.get("risk", {}).get("enable_tiered_stops", True),
        )

        # Sentiment analysis (keyword-based, no external deps)
        self.sentiment = SentimentAnalyzer(config)

        # Deterministic strategy modules (run alongside LLM strategist)
        self.ema_strategy = EMACrossoverStrategy(config)
        self.bollinger_strategy = BollingerReversionStrategy(config)
        self.pairs_monitor = PairsCorrelationMonitor(config)

        # FIFO cost-basis tracking for tax reporting
        self.fifo_tracker = FIFOTracker()

        # Fee management and high-stakes mode
        self.fee_manager = FeeManager(config)
        self.high_stakes = HighStakesManager(config, audit=None)  # Will set audit below

        # Journal and audit
        self.journal = TradeJournal()
        self.audit = AuditLog()
        self.high_stakes.audit = self.audit  # Connect audit after creation

        # Route finder (optimal swap route discovery) — only for crypto exchanges
        routing_enabled = config.get("routing", {}).get("enabled", True)
        if routing_enabled:
            self.route_finder = RouteFinder(exchange, self.fee_manager, config)
        else:
            self.route_finder = None
            logger.info("🛤️ Route Finder disabled (non-crypto exchange or routing.enabled=false)")

        # Portfolio rotator (autonomous crypto-to-crypto swaps)
        rotation_enabled = config.get("rotation", {}).get("enabled", True)
        if rotation_enabled and self.route_finder is not None:
            self.rotator = PortfolioRotator(
                config=config,
                coinbase_client=exchange,
                llm_client=llm,
                fee_manager=self.fee_manager,
                high_stakes=self.high_stakes,
                multi_tf=self.multi_tf,
                fear_greed=self.fear_greed,
                journal=self.journal,
                audit=self.audit,
                route_finder=self.route_finder,
                rules=rules,
            )
        else:
            self.rotator = None
            if not rotation_enabled:
                logger.info("🔄 Portfolio Rotator disabled (rotation.enabled=false)")
            elif self.route_finder is None:
                logger.info("🔄 Portfolio Rotator disabled (no route finder)")

        # Tasks
        self.active_tasks: list[Task] = []
        
        self.pipeline_manager = PipelineManager(self)
        self.state_manager = StateManager(self)
        self.telegram_manager = TelegramManager(self)
        self.universe_scanner = UniverseScanner(self)
        self.holdings_manager = HoldingsManager(self)
        self.context_manager = ContextManager(self)
        self.event_manager = EventManager(self)
        self.dashboard_commands = DashboardCommandManager(self)
        
        self._pending_approvals: dict[str, dict] = {}
        self._pending_approvals_lock = threading.Lock()
        self.state_manager.load_pending_approvals()

        # ─── Stats Database (persistent analytics) ───
        self.stats_db = StatsDB()

        # ─── Adaptive Learning Engine ───
        self.learning_manager = LearningManager(self)

        # ─── Training Data Collector (for future fine-tuning) ───
        self.training_collector = TrainingDataCollector(config)
        self.executor.training_collector = self.training_collector
        self.executor.stats_db = self.stats_db
        # Hook LLM callback so every prompt/response is captured
        llm_cb = self.training_collector.make_llm_callback()
        if llm_cb:
            self.llm._interaction_callback = llm_cb

        # ─── Live Holdings Sync (Coinbase API → TradingState) ───
        trading_cfg = config.get("trading", {})
        self._holdings_sync_enabled: bool = trading_cfg.get("live_holdings_sync", True)
        self._holdings_refresh_seconds: float = float(trading_cfg.get("holdings_refresh_seconds", 60))
        self._holdings_dust_threshold: float = float(trading_cfg.get("holdings_dust_threshold", 0.01))

        # Initial sync on startup (live mode only)
        _is_coinbase = exchange.__class__.__name__ in ("CoinbaseClient", "CoinbasePaperClient")
        _is_ibkr = exchange.__class__.__name__ == "IBClient"

        # IBKR live: sync balance and portfolio value into TradingState
        if not getattr(exchange, 'paper_mode', False) and _is_ibkr:
            try:
                pv = exchange.get_portfolio_value()
                accs = exchange.get_accounts()
                cash = 0.0
                for acc in accs:
                    cash += acc.get("available_cash", 0.0)
                self.state.live_portfolio_value = pv
                self.state.cash_balance = cash
                self.state.live_cash_balances = {self.state.native_currency: cash}
                self.state._live_snapshot_ts = time.time()
                # Correct initial_balance from live data (same as Coinbase sync_live_holdings)
                if not self.state._initial_balance_synced and pv > 0:
                    self.state.initial_balance = pv
                    self.state.peak_portfolio_value = pv
                    self.state._initial_balance_synced = True
                    self.state.max_drawdown = 0.0
                    self.state.circuit_breaker_triggered = False
                    logger.info(
                        f"📊 Initial balance corrected to live IBKR portfolio: "
                        f"{self.state.currency_symbol}{pv:,.2f} (drawdown reset)"
                    )
                logger.info(
                    f"📡 IBKR live sync: portfolio {self.state.currency_symbol}{pv:,.2f}, "
                    f"cash {self.state.currency_symbol}{cash:,.2f}"
                )
            except Exception as e:
                logger.warning(f"⚠️ IBKR initial portfolio sync failed: {e}")

        if self._holdings_sync_enabled and not getattr(exchange, 'paper_mode', False) and _is_coinbase:
            try:
                snapshot = self.holdings_manager.live_coinbase_snapshot()
                new_externals = self.state.sync_live_holdings(snapshot, dust_threshold=self._holdings_dust_threshold)
                if new_externals:
                    self.holdings_manager._register_external_holdings(new_externals)
                logger.info("📡 Initial live holdings sync complete")

                # One-time strategic context invalidation.
                # Only fires when `invalidate_strategic_context: true` is set in
                # settings.yaml.  After firing, the flag is auto-reset to false
                # so normal restarts preserve learned strategic plans.
                # Set this flag manually (or via Telegram /command) when a major
                # system change means old plans are based on wrong assumptions.
                if trading_cfg.get("invalidate_strategic_context", False):
                    try:
                        sym = self.state.currency_symbol
                        pv = self.state.live_portfolio_value
                        n_assets = len([h for h in self.state.live_holdings if not h.get("is_fiat")])
                        correction_summary = (
                            f"Portfolio tracking corrected to live Coinbase data: "
                            f"{n_assets} crypto assets, total value {sym}{pv:,.2f}. "
                            f"Previous plans may have assumed incorrect portfolio state — "
                            f"weight current holdings and this correction heavily."
                        )
                        self.stats_db.save_strategic_context(
                            horizon="daily",
                            plan_json={
                                "regime": "neutral",
                                "confidence": 0.5,
                                "risk_posture": "conservative",
                                "key_observations": [
                                    "Portfolio tracking was corrected to reflect actual live Coinbase holdings",
                                    f"Real portfolio: {n_assets} crypto holdings, {sym}{pv:,.2f} total value",
                                    "Previous plans may contain incorrect portfolio assumptions — treat with low weight",
                                ],
                                "today_focus": "Re-evaluate all positions with corrected portfolio data",
                                "summary": correction_summary,
                                "portfolio_correction_applied": True,
                            },
                            summary_text=correction_summary,
                        )
                        logger.info("📋 Inserted portfolio correction notice into strategic context")

                        # Auto-reset the flag so it doesn't fire on every restart
                        try:
                            from src.utils.settings_manager import update_section
                            ok, err, _applied = update_section("trading", {"invalidate_strategic_context": False})
                            if ok:
                                logger.info("🔄 Auto-reset invalidate_strategic_context → false")
                            else:
                                logger.warning(f"⚠️ Could not auto-reset config flag: {err}")
                        except Exception as e:
                            logger.warning(f"⚠️ Could not auto-reset config flag: {e}")

                    except Exception as e:
                        logger.warning(f"⚠️ Failed to insert correction notice (non-fatal): {e}")

            except Exception as e:
                logger.warning(f"⚠️ Initial holdings sync failed (non-fatal): {e}")

        # ─── Strategic context cache (refreshed every 60s from DB) ───
        self._strategic_context_str: str = ""
        self._strategic_context_ts: float = 0.0
        self._STRATEGIC_CONTEXT_TTL: float = 60.0
        self._pair_priority_map: dict[str, float] = {}  # pair → confidence adjustment
        self._pair_expected_gains: dict[str, dict] = {}  # pair → {gain_pct, direction, horizon_days, confidence}

        # ─── Universe Tracking (funnel system) ────────────────────────────
        self._pair_universe: list[dict] = []           # full product metadata
        self._pair_universe_ts: float = 0.0
        self._PAIR_UNIVERSE_TTL: float = float(
            trading_cfg.get("pair_universe_refresh_seconds", 1800)
        )
        self._scan_results: dict[str, dict] = {}       # pair → {technicals, strategies, score}
        self._screener_active_pairs: list[str] = []    # pairs selected by LLM screener
        self._screener_cycle_counter: int = 0

        # Restore LLM-followed pairs from DB so a container restart
        # doesn't lose the screener's selection (cold-start fix)
        try:
            _exchange_name = trading_cfg.get("exchange", "coinbase").lower()
            restored = self.stats_db.get_followed_pairs_set(
                followed_by="llm",
                quote_currency=trading_cfg.get("quote_currency"),
                exchange=_exchange_name,
            )
            if restored:
                self._screener_active_pairs = sorted(restored)
                logger.info(
                    f"♻️ Restored {len(restored)} LLM-followed pairs from DB: "
                    f"{self._screener_active_pairs}"
                )
        except Exception as _restore_err:
            logger.warning(f"⚠️ Could not restore LLM-followed pairs: {_restore_err}")

        # Seed human-followed pairs from DB so dashboard watchlist additions
        # survive a restart (they are also kept in self.watchlist_pairs at runtime)
        try:
            human_followed = self.stats_db.get_followed_pairs_set(
                followed_by="human",
                quote_currency=trading_cfg.get("quote_currency"),
                exchange=_exchange_name,
            )
            if human_followed:
                new_wl = list(dict.fromkeys(
                    self.watchlist_pairs + [p for p in sorted(human_followed) if p not in self.watchlist_pairs]
                ))
                self.watchlist_pairs = new_wl
                self.all_tracked_pairs = list(set(self.pairs + self.watchlist_pairs))
                logger.info(
                    f"♻️ Seeded {len(human_followed)} human-followed pairs from DB into pipeline: "
                    f"{sorted(human_followed)}"
                )
                # Update WS subscriptions so seeded pairs get live prices
                # (no-op if WS not yet connected — product_ids updated for first connect)
                if self.ws_feed is not None:
                    self.ws_feed.update_subscriptions(self.all_tracked_pairs)
        except Exception as _seed_err:
            logger.warning(f"⚠️ Could not seed human-followed pairs: {_seed_err}")

        self._SCREENER_INTERVAL: int = int(
            trading_cfg.get("screener_interval_cycles", 5)
        )
        configured_max_pairs = int(trading_cfg.get("max_active_pairs", 5))
        rpm_max, rpm_breakdown = compute_rpm_entity_cap(
            config.get("llm_providers", []),
            int(trading_cfg.get("interval", 120)),
        )
        self._rpm_entity_cap: int = rpm_max
        self._rpm_breakdown: dict = rpm_breakdown
        if configured_max_pairs > rpm_max:
            logger.warning(
                f"⚠️ max_active_pairs clamped from {configured_max_pairs} to {rpm_max} "
                f"— primary provider '{rpm_breakdown.get('provider', '?')}' has "
                f"{rpm_breakdown.get('rpm', '?')} RPM, cycle interval "
                f"{rpm_breakdown.get('interval', '?')}s"
            )
        self._max_active_pairs: int = min(configured_max_pairs, rpm_max)
        self._scan_volume_threshold: float = float(
            trading_cfg.get("scan_volume_threshold", 1000)
        )
        self._scan_movement_threshold_pct: float = float(
            trading_cfg.get("scan_movement_threshold_pct", 1.0)
        )
        self._include_crypto_quotes: bool = bool(
            trading_cfg.get("include_crypto_quotes", True)
        )

        # ─── RPM Budget Startup Banner ────────────────────────────────────
        _prov = rpm_breakdown.get('provider', 'local-only')
        _rpm = rpm_breakdown.get('rpm', 0)
        _headroom = rpm_max - self._max_active_pairs
        _headroom_pct = round(_headroom / rpm_max * 100) if rpm_max > 0 else 100
        logger.info(
            f"📊 Entity tracking: {self._max_active_pairs} active pairs | "
            f"RPM budget: {_prov} @ {_rpm} RPM | "
            f"theoretical max: {rpm_max} entities | "
            f"headroom: {_headroom} ({_headroom_pct}%)"
        )

        # ─── Asyncio helper ───────────────────────────────────────────────

        # ─── Position reconciliation (live mode only) ───
        # Reconcile TradingState.positions against real Coinbase balances every N cycles
        self._reconcile_every: int = config.get("trading", {}).get("reconcile_every_cycles", 10)
        self._reconcile_counter: int = 0

        # ─── Event-driven early pipeline triggers ────────────────────────
        # WS price-move trigger: if a tracked pair's price moves >= _ws_trigger_pct
        # since the last pipeline run, we fire an early pipeline mid-interval.
        self._ws_trigger_lock = threading.Lock()
        self._ws_trigger_pairs: set[str] = set()    # pairs needing an early run
        self._news_trigger_pairs: set[str] = set()  # pairs flagged by breaking news
        self._ws_last_prices: dict[str, float] = {} # prices at last pipeline start
        self._ws_trigger_pct: float = config.get("trading", {}).get("ws_trigger_pct", 0.005)
        self._last_pipeline_ts: dict[str, float] = {}  # pair → epoch of last run
        # Batch scanning: analyse at most N pairs per cycle, rotating by staleness.
        # 0 = disabled (all pairs every cycle). Default: 5.
        self.scan_batch_size: int = config.get("trading", {}).get("scan_batch_size", 5)

        # ─── LLM Chat Handler (conversational Telegram interface) ───
        _exchange_type = config.get("trading", {}).get("exchange", "coinbase").lower()
        self.chat_handler = TelegramChatHandler(
            llm_client=llm,
            rate_limiter=self.rate_limiter,
            exchange_type=_exchange_type,
        )
        self.telegram_manager.register_chat_functions()

        # Connect to Telegram bot
        if self.telegram:
            self.telegram.chat_handler = self.chat_handler
            self.telegram.on_command = self.telegram_manager.handle_telegram_command  # Legacy fallback
            self.chat_handler.set_send_callback(self.telegram.send_message)
            # Connect proactive engine to trading state + stats
            self.chat_handler.set_context_provider(self.telegram_manager.get_trading_context)
            if self.chat_handler._proactive:
                self.chat_handler._proactive.set_stats_db(self.stats_db)
                # Pass live config reference so notification settings take effect immediately
                self.chat_handler._proactive.set_config(self.config)

        # Start health server
        start_health_server(port=config.get("health", {}).get("port", 8080))

        # Register WS price-move detector (runs in WS thread, very lightweight)
        if self.ws_feed:
            self.ws_feed.add_ticker_callback(self.event_manager.on_ws_ticker)

        # Subscribe to Redis news:updates channel so fresh news triggers early pipelines
        self.event_manager.start_news_subscriber()

        _sync_status = '✅ Enabled' if (self._holdings_sync_enabled and not getattr(exchange, 'paper_mode', False)) else '❌ Disabled'
        logger.info("═══════════════════════════════════════════")
        logger.info("  🤖 Orchestrator initialized")
        logger.info(f"  Trading pairs: {self.pairs}")
        logger.info(f"  Interval: {self.interval}s")
        logger.info(f"  WebSocket: {'✅ Enabled' if ws_feed else '❌ Disabled (polling)'}")
        logger.info(f"  Live Holdings Sync: {_sync_status}")
        logger.info(f"  LLM Chat: ✅ Conversational mode")
        logger.info(f"  Fear & Greed: ✅ Enabled")
        logger.info(f"  Multi-Timeframe: ✅ Enabled")
        logger.info(f"  Trailing Stops: ✅ Enabled (tiers: {'ON' if self.trailing_stops.enable_tiers else 'OFF'})")
        logger.info(f"  Portfolio Rotation: ✅ Enabled")
        logger.info(f"  Fee-Aware Trading: ✅ Enabled")
        logger.info(f"  High-Stakes Mode: ✅ Ready")
        logger.info(f"  Audit Log: ✅ Enabled")
        logger.info(f"  Sentiment Analysis: ✅ Enabled")
        logger.info(f"  Strategy Modules: ✅ EMA + Bollinger + Pairs")
        logger.info(f"  Kelly Criterion: ✅ Enabled")
        logger.info(f"  FIFO Tax Tracking: ✅ Enabled")
        logger.info(f"  Universe Scanner: ✅ Enabled (screener every {self._SCREENER_INTERVAL} cycles)")
        logger.info(f"  Mode: {'📝 PAPER' if getattr(exchange, 'paper_mode', False) else '💰 LIVE'}")
        logger.info("═══════════════════════════════════════════")

    # Severely stale data threshold: skip drawdown circuit breaker when
    # live data hasn't refreshed in over an hour (market closed / gateway down).
    _STALE_DRAWDOWN_SKIP_SECS: float = 3600.0
    # Minimum cooldown before auto-recovery can kick in (seconds).
    _CIRCUIT_BREAKER_COOLDOWN_SECS: float = 1800.0  # 30 minutes

    def _check_circuit_breakers(self) -> None:
        """Trip circuit breaker on current drawdown OR daily loss limit breach."""
        if self.state.circuit_breaker_triggered:
            return

        reason = value = msg = None  # type: ignore[assignment]

        # Drawdown check — use *current* drawdown (recoverable) instead of the
        # all-time max (which ratchets up and can never come back down, causing
        # permanent false triggers after any past dip).
        # Skip when live data is severely stale to prevent false triggers.
        max_dd_pct = self.config.get("risk", {}).get("max_drawdown_pct", 0.10)
        stale_secs = time.time() - self.state._live_snapshot_ts if self.state._live_snapshot_ts > 0 else 0
        current_dd = self.state.current_drawdown
        if current_dd >= max_dd_pct:
            if stale_secs > self._STALE_DRAWDOWN_SKIP_SECS:
                logger.debug(
                    f"Skipping drawdown circuit breaker — live data is {stale_secs:.0f}s stale"
                )
            else:
                reason, value = "max_drawdown", current_dd
                msg = f"🛑 CIRCUIT BREAKER: Current drawdown {format_percentage(value)} reached!"

        # Daily loss check (M4 fix: use thread-safe accessor)
        if reason is None and self.rules.daily_loss >= self.rules.max_daily_loss:
            reason, value = "daily_loss", self.rules.daily_loss
            _sym = self.state.currency_symbol
            msg = (
                f"🛑 CIRCUIT BREAKER: Daily loss {format_currency(value, _sym)} "
                f"reached limit {format_currency(self.rules.max_daily_loss, _sym)}!"
            )

        if reason is None:
            return

        self.state.circuit_breaker_triggered = True
        self.state._circuit_breaker_ts = time.time()
        logger.warning(msg)
        self.audit.log_circuit_breaker(reason, value)
        # queue_event("CRITICAL: ...") already sends 🚨 ALERT immediately via ProactiveEngine
        self.chat_handler.queue_event(f"CRITICAL: {msg}", severity="critical")
        self.event_manager.trigger_emergency_replan(f"Circuit breaker: {reason} {value}")

    def _try_circuit_breaker_recovery(self) -> None:
        """Auto-recover from circuit breaker if conditions have improved.

        Requires:
        - Cooldown period elapsed (30 min default)
        - Fresh live data (not stale)
        - Current drawdown below threshold
        - Daily loss below limit
        """
        elapsed = time.time() - self.state._circuit_breaker_ts
        if elapsed < self._CIRCUIT_BREAKER_COOLDOWN_SECS:
            return

        # Need fresh data to make a recovery decision
        stale_secs = time.time() - self.state._live_snapshot_ts if self.state._live_snapshot_ts > 0 else float("inf")
        if stale_secs > self._STALE_DRAWDOWN_SKIP_SECS:
            return  # can't evaluate without recent data

        max_dd_pct = self.config.get("risk", {}).get("max_drawdown_pct", 0.10)
        current_dd = self.state.current_drawdown
        daily_loss_ok = self.rules.daily_loss < self.rules.max_daily_loss

        # Recover only when drawdown has subsided meaningfully (below 80% of threshold)
        # to prevent rapid re-trigger oscillation.
        if current_dd < max_dd_pct * 0.8 and daily_loss_ok:
            self.state.circuit_breaker_triggered = False
            self.state._circuit_breaker_ts = 0.0
            msg = (
                f"✅ Circuit breaker auto-recovered — "
                f"current drawdown {format_percentage(current_dd)} "
                f"is below threshold {format_percentage(max_dd_pct)}"
            )
            logger.info(msg)
            self.chat_handler.queue_event(msg, severity="info")

    def run_forever(self) -> None:
        """Main loop — runs continuously until stopped."""
        logger.info("🚀 Starting main trading loop...")

        if self.telegram:
            _configured_mode = self.config.get("trading", {}).get("mode", "paper")
            _is_paper = getattr(self.exchange, "paper_mode", False)
            if _is_paper:
                _mode_line = "📝 Paper"
            else:
                _mode_line = "💰 Live"
            self.telegram.send_message(
                "🤖 *Auto-Traitor Online*\n\n"
                f"Mode: {_mode_line}\n"
                f"Pairs: {', '.join(self.pairs)}\n"
                f"Cycle: every {self.interval}s\n\n"
                "💬 I'm in conversational mode — just talk to me naturally!\n"
                "Ask me anything: _\"how are we doing?\"_, "
                "_\"let's go high-stakes\"_, _\"show me the portfolio\"_\n\n"
                "Say _\"be quiet\"_ or _\"be chatty\"_ to control my updates."
            )

        cycle_count = 0
        consecutive_errors = 0
        _MAX_CONSECUTIVE_ERRORS = 3

        # Start LLM provider recovery polling on the orchestrator's event loop
        recovery_interval = self.config.get("llm", {}).get("recovery_check_interval", 120)
        self.llm.start_recovery_polling(loop=self._loop, interval=recovery_interval)

        while self.state.is_running:
            if self.state.is_paused:
                logger.debug("Trading paused, waiting...")
                time.sleep(10)
                continue

            if self.state.circuit_breaker_triggered:
                logger.warning("🛑 Circuit breaker active — trading halted")
                self._try_circuit_breaker_recovery()
                time.sleep(60)
                continue

            cycle_count += 1
            logger.info(f"━━━ Cycle #{cycle_count} ━━━━━━━━━━━━━━━━━━━━━━━━")
            _cycle_t0 = time.monotonic()

            try:
                # ─── LLM provider recovery check ─────────────────────
                # Lightweight: detects cooldown expiry, daily token resets,
                # and newly-added API keys — no restart needed.
                if cycle_count % 5 == 1:  # every 5th cycle
                    self.llm.check_provider_recovery()
                    self.llm.rescan_and_reload()

                # ─── Ollama pre-check ─────────────────────────────────
                # Avoid burning minutes on retries if Ollama is down.
                if not self.llm.is_available():
                    _ollama_skip_count = getattr(self, "_ollama_skip_count", 0) + 1
                    self._ollama_skip_count = _ollama_skip_count
                    logger.warning(f"⚠️ Ollama unreachable — skipping cycle (consecutive: {_ollama_skip_count})")
                    if _ollama_skip_count >= 2 and self.telegram:
                        try:
                            self.telegram.send_alert(
                                f"⚠️ Ollama unreachable for {_ollama_skip_count} consecutive cycles — "
                                "LLM pipelines skipped."
                            )
                        except Exception:
                            pass
                    # Still update health so the endpoint stays fresh
                    update_health(status="degraded", cycle_count=cycle_count)
                    # H23 fix: sleep before continuing to avoid CPU-spinning
                    time.sleep(min(30.0, self.interval))
                    continue
                self._ollama_skip_count = 0

                # ─── Portfolio Tier Scaling ────────────────────────────
                # Recompute tier so position limits, pair caps, etc. reflect
                # the current portfolio value (micro → whale auto-adapt).
                _pv = (
                    self.state.live_portfolio_value
                    if self.state.live_portfolio_value > 0
                    else self.state.portfolio_value
                )
                tier = self.portfolio_scaler.update(_pv)
                # Log tier summary once every 20 cycles
                if cycle_count % 20 == 1:
                    logger.info(self.portfolio_scaler.summary())

                # --- Apply tier-scaled limits ---
                # max_active_pairs: clamp by both tier and RPM budget
                tier_max_pairs = tier.max_active_pairs
                rpm_max_pairs = self._rpm_entity_cap
                effective_max_active = min(tier_max_pairs, rpm_max_pairs)
                # Fee manager: update min_gain_after_fees for current tier
                self.fee_manager.min_gain_after_fees_pct = tier.min_gain_after_fees_pct

                # ─── Monitoring Mode ──────────────────────────────────
                # When all capital is tied up in positions with trailing stops
                # and there's not enough cash for a new trade, skip the
                # expensive LLM pipeline and just monitor exits.
                _active_stops = self.trailing_stops.get_active_count()
                _cash = (
                    sum(self.state.live_cash_balances.values())
                    if self.state.live_cash_balances
                    else self.state.cash_balance
                )
                _min_trade = self.fee_manager.get_dynamic_min_trade(_pv)
                _monitoring_mode = (
                    _active_stops > 0
                    and _cash < _min_trade
                    and len(self.state.open_positions) >= tier.max_open_positions
                )

                if _monitoring_mode:
                    # Refresh prices for trailing stop accuracy
                    _stop_pairs = list(self.trailing_stops.get_all_stops().keys())
                    for _sp in _stop_pairs:
                        if self.ws_feed:
                            _ws_p = self.ws_feed.get_price(_sp)
                            if _ws_p > 0:
                                self.state.update_price(_sp, _ws_p)
                        else:
                            try:
                                _rest_p = self.exchange.get_current_price(_sp)
                                if _rest_p > 0:
                                    self.state.update_price(_sp, _rest_p)
                            except Exception:
                                pass
                    sorted_pairs = []  # skip pipeline — no LLM calls
                    logger.info(
                        f"👁️ Monitoring mode — {_active_stops} trailing stop(s) active, "
                        f"cash {format_currency(_cash, self.state.currency_symbol)} "
                        f"< min trade {format_currency(_min_trade, self.state.currency_symbol)}. "
                        f"Skipping LLM pipeline."
                    )
                else:
                    # ─── Full Pipeline Mode ───────────────────────────────
                    # Run pipelines — parallelised across pairs using asyncio
                    # Sort pairs: planning-preferred first, then normal, avoid last
                    priority_map = getattr(self, "_pair_priority_map", {})

                    # ─── Universe Scan + LLM Screener (funnel system) ─────
                    try:
                        self.universe_scanner.refresh_pair_universe()
                        self.universe_scanner.run_universe_scan()

                        self._screener_cycle_counter += 1
                        # Run screener immediately when no pairs are active (cold start)
                        # or on the regular interval
                        no_pairs = not self._screener_active_pairs and not self.pairs
                        if no_pairs or self._screener_cycle_counter >= self._SCREENER_INTERVAL:
                            self._screener_cycle_counter = 0
                            self.universe_scanner.run_llm_screener()
                    except Exception as _uf_err:
                        logger.warning(f"Universe funnel error (non-fatal): {_uf_err}")

                    # Effective base pairs: screener-selected (if any) or configured seed list
                    # Capped by tier-scaled max_active_pairs (micro accounts get fewer pairs)
                    # Read both pairs and watchlist under lock to prevent race with settings advisor
                    with self._pairs_lock:
                        base_pairs = self._screener_active_pairs or list(self.pairs[:effective_max_active])
                        current_watchlist = list(self.watchlist_pairs)
                    base_pairs = base_pairs[:effective_max_active]  # enforce cap
                    
                    # Watchlist pairs bypass the LLM screener cap since they are explicitly requested
                    # Combine base pairs and watchlist pairs, removing duplicates while preserving order
                    effective_pairs = list(dict.fromkeys(base_pairs + current_watchlist))

                    if not effective_pairs:
                        logger.warning(
                            "⚠️ No active pairs to trade this cycle "
                            f"(screener={len(self._screener_active_pairs)}, config={len(self.pairs)}, watchlist={len(current_watchlist)}). "
                            "Waiting for LLM screener to pick pairs..."
                        )

                    sorted_pairs = sorted(
                        effective_pairs,
                        key=lambda p: priority_map.get(p, 0.0),  # negative = preferred → first
                    )

                    # ─── Market hours filter (equity only) ────────────────
                    _asset_class = getattr(self.exchange, "asset_class", "crypto")
                    if _asset_class == "equity":
                        _open_pairs, _closed_pairs = [], []
                        for _p in sorted_pairs:
                            (_open_pairs if _is_market_open(_p, _asset_class) else _closed_pairs).append(_p)
                        if _closed_pairs:
                            logger.info(
                                f"🕐 Market closed — skipping {_closed_pairs} "
                                f"({len(_open_pairs)}/{len(sorted_pairs)} pairs open)"
                            )
                        sorted_pairs = _open_pairs

                    # ─── Batch scan: select the most-urgent/stale N pairs ─────────
                    _all_pairs_count = len(sorted_pairs)
                    if self.scan_batch_size and self.scan_batch_size < _all_pairs_count:
                        _now_mono = time.monotonic()
                        _open_pairs_set = {t.pair for t in self.state.trades if t.is_open}
                        with self._pairs_lock:
                            _watchlist_set = set(self.watchlist_pairs)

                        def _batch_score(p: str) -> float:
                            # Tier: open position (-2) → watchlist (-1) → normal (0)
                            if p in _open_pairs_set:
                                tier = -2.0
                            elif p in _watchlist_set:
                                tier = -1.0
                            else:
                                tier = 0.0
                            # Staleness: 0 (just scanned) → -1.0 (≥3 cycles overdue)
                            age = _now_mono - self._last_pipeline_ts.get(p, 0.0)
                            staleness = min(age / (3 * self.interval), 1.0)
                            return tier - staleness  # most-negative = scan first

                        sorted_pairs = sorted(sorted_pairs, key=_batch_score)[:self.scan_batch_size]
                        logger.info(
                            f"🔄 Batch scan: {len(sorted_pairs)}/{_all_pairs_count} pairs "
                            f"(batch_size={self.scan_batch_size}, "
                            f"with_open={len(_open_pairs_set & set(sorted_pairs))})"
                        )

                    # ─── RPM utilisation log (every 10 cycles) ────────────
                    if cycle_count % 10 == 0:
                        _n_pairs = len(sorted_pairs)
                        _worst_calls = _n_pairs * 2 + self._rpm_breakdown.get('overhead', 2)
                        _budget = self._rpm_breakdown.get('available_per_cycle', 0)
                        logger.info(
                            f"📈 RPM utilisation: {_n_pairs}/{self._max_active_pairs} entities, "
                            f"est. {_worst_calls} calls/cycle (budget: {_budget})"
                        )

                    try:
                        tasks = [self.pipeline_manager.run_pipeline(p) for p in sorted_pairs]
                        # Impose a hard timeout so no single cycle can hang forever.
                        # Budget: ~60s per pair × max_pairs, capped at 10 min.
                        _cycle_deadline = min(60.0 * len(sorted_pairs), 600.0)
                        self._loop.run_until_complete(
                            asyncio.wait_for(
                                asyncio.gather(*tasks, return_exceptions=True),
                                timeout=_cycle_deadline,
                            )
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"⚠️ Cycle #{cycle_count} pipelines timed out after "
                            f"{_cycle_deadline:.0f}s — moving to next cycle"
                        )
                    except Exception as _pe:
                        logger.error(f"Pipeline worker error: {_pe}", exc_info=True)

                    # Record scan timestamps so the next cycle's batch-scorer knows
                    # which pairs are freshest and can deprioritise them.
                    _ts_now = time.monotonic()
                    for _p in sorted_pairs:
                        self._last_pipeline_ts[_p] = _ts_now

                    # ─── Check pending limit orders ───
                    try:
                        self.executor.check_pending_orders()
                    except Exception as _po_err:
                        logger.debug(f"Pending order check error: {_po_err}")

                    # ─── Portfolio Rotation (autonomous swaps) ───
                    if self.rotator is not None:
                        self._run_rotation()

                # Update trailing stops with current prices — execute sells for triggered stops
                triggered = self.trailing_stops.update_prices(
                    self.state.current_prices
                )
                for t in triggered:
                    pair = t["pair"]
                    trigger_price = t.get("trigger_price", 0)
                    entry_price = t.get("entry_price", 0)

                    # ── EXECUTE THE SELL ORDER ────────────────────────────────
                    close_result = self.executor.close_position_by_pair(
                        pair, trigger_price, "trailing_stop"
                    )
                    if close_result:
                        if close_result.get("success", True):
                            # C6 fix: only remove stop AFTER confirmed success
                            self.trailing_stops.remove_stop(pair)
                            pnl = close_result.get("pnl")
                            self.audit.log_trade(
                                pair=pair, action="trailing_stop_exit",
                                amount=close_result.get("close_price", trigger_price) or 0,
                                price=close_result.get("close_price", trigger_price) or 0,
                            )
                            # H3 note: record_trade is handled in executor._close_position
                            if pnl is not None and pnl < 0:
                                self.rules.record_loss(abs(pnl))
                            _sym = self.state.currency_symbol
                            event_msg = (
                                f"Trailing stop executed on {pair} "
                                f"at {format_currency(trigger_price, _sym)}"
                                + (f" — PnL: {format_currency(pnl, _sym)}" if pnl is not None else "")
                            )
                        else:
                            # Sell failed — keep the stop so it retries next cycle
                            logger.error(
                                f"❌ Trailing stop sell FAILED for {pair} — "
                                "stop preserved; will retry next cycle."
                            )
                            event_msg = (
                                f"⚠️ Trailing stop sell FAILED for {pair} "
                                f"at {format_currency(trigger_price, self.state.currency_symbol)} — will retry"
                            )
                    else:
                        # No open trade found — stop is stale; remove it
                        self.trailing_stops.remove_stop(pair)
                        event_msg = (
                            f"Trailing stop triggered on {pair} "
                            f"at {format_currency(trigger_price, self.state.currency_symbol)} (no open position found)"
                        )

                    self.chat_handler.queue_event(event_msg, severity="trade", pair=pair)

                # ─── Tiered partial exits (profit-locking) ───
                tier_exits = self.trailing_stops.get_pending_tier_exits()
                for te in tier_exits:
                    te_pair = te["pair"]
                    te_qty = te["exit_quantity"]
                    te_price = te["trigger_price"]
                    te_pct = te.get("tier_pct", 0) * 100
                    try:
                        sell_result = self.executor.close_position_by_pair(
                            te_pair, te_price, f"tier_exit_{te_pct:.0f}pct",
                            quantity=te_qty,
                        )
                        if sell_result:
                            self.audit.log_trade(
                                pair=te_pair, action=f"tier_exit_{te_pct:.0f}pct",
                                amount=te_qty * te_price,
                                price=te_price,
                            )
                            # H3+M3 note: record_trade is handled in executor._partial_sell
                            # FIFO tracking for tier exit sell
                            try:
                                base_asset = te_pair.split("-")[0] if "-" in te_pair else te_pair
                                self.fifo_tracker.record_sell(
                                    asset=base_asset,
                                    quantity=te_qty,
                                    sale_price_per_unit=te_price,
                                )
                            except Exception as e:
                                logger.debug(f"FIFO record_sell failed: {e}")

                            tier_msg = (
                                f"Tier exit +{te_pct:.0f}% on {te_pair}: "
                                f"sold {te_qty:.6f} at {format_currency(te_price, self.state.currency_symbol)} "
                                f"(PnL: +{te.get('pnl_pct', 0):.1f}%)"
                            )
                            self.chat_handler.queue_event(tier_msg, severity="trade", pair=te_pair)
                    except Exception as e:
                        logger.warning(f"⚠️ Tier exit failed for {te_pair}: {e}")

                # Check stop-losses on all positions
                closed = self.executor.check_stop_losses()
                for c in closed:
                    if not c.get("success", True):
                        # Close attempt failed — exchange rejected or state diverged.
                        # Don't report as "Position closed"; executor will retry next cycle.
                        logger.warning(
                            f"⚠️ Stop-loss close FAILED for {c['pair']} ({c['reason']}) — "
                            "will retry next cycle; no notification sent."
                        )
                        continue
                    pnl = c.get("pnl", 0)
                    emoji = "🎯" if pnl and pnl > 0 else "⚠️"
                    event_msg = (
                        f"Position closed ({c['reason']}): {c['pair']} "
                        f"PnL: {format_currency(pnl or 0, self.state.currency_symbol)}"
                    )
                    self.chat_handler.queue_event(
                        f"{emoji} {event_msg}", severity="trade", pair=c["pair"]
                    )

                # Refresh live holdings before snapshot to prevent stale-data
                # fallback that inflates values from drifted positions.
                self.holdings_manager.maybe_refresh_holdings()

                # Take portfolio snapshot
                self.state.take_portfolio_snapshot()

                # Persist snapshot to stats DB (drives the Analytics dashboard)
                try:
                    exchange_name = self.config.get("trading", {}).get("exchange", "coinbase").lower()
                    if getattr(self.exchange, "paper_mode", False):
                        exchange_name = f"{exchange_name}_paper"
                    _fg_value = getattr(self.fear_greed, "last_value", None)
                    self.stats_db.record_snapshot(
                        portfolio_value=self.state.portfolio_value,
                        cash_balance=self.state.cash_balance,
                        return_pct=self.state.return_pct,
                        total_pnl=self.state.total_pnl,
                        max_drawdown=self.state.max_drawdown,
                        open_positions=dict(self.state.positions),
                        current_prices=dict(self.state.current_prices),
                        fear_greed_value=float(_fg_value) if _fg_value is not None else None,
                        high_stakes_active=getattr(self.state, "high_stakes_active", False),
                        exchange=exchange_name,
                    )
                except Exception as _snap_err:
                    logger.debug(f"Portfolio snapshot persistence failed: {_snap_err}")

                # Check circuit breakers (drawdown + daily loss)
                self._check_circuit_breakers()

                # Save state periodically
                self.state.save_state()

                # ─── Position reconciliation (live mode only) ────────────
                if not getattr(self.exchange, 'paper_mode', False):
                    self._reconcile_counter += 1
                    if self._reconcile_counter >= self._reconcile_every:
                        self._reconcile_counter = 0
                        self.holdings_manager.reconcile_positions()

                # ─── Autonomous Settings Advisor ────────────────────
                if self.settings_advisor.should_run():
                    try:
                        exchange_name = self.config.get("trading", {}).get("exchange", "coinbase").lower()
                        advisor_ctx = {
                            "fear_greed": getattr(self.state, "fear_greed_summary", "unavailable"),
                            "recent_performance": self.context_manager.get_performance_summary(),
                            "market_volatility": getattr(self.state, "volatility_summary", "moderate"),
                            "current_prices": dict(self.state.current_prices),
                            "cycle_id": str(cycle_count),
                            "stats_db": self.stats_db if hasattr(self, "stats_db") else None,
                            "trace_ctx": self.trace_ctx if hasattr(self, "trace_ctx") else None,
                            "scan_results_summary": self.universe_scanner.get_scan_summary(),
                            "universe_size": len(self._pair_universe),
                            "exchange": exchange_name,
                        }
                        advisor_result = self._loop.run_until_complete(
                            self.settings_advisor.execute(advisor_ctx)
                        )

                        if advisor_result and advisor_result.get("changes_applied", 0) > 0:
                            # Push updated sections to runtime config
                            for ch in advisor_result.get("applied", []):
                                sec = ch["section"]
                                sm.push_section_to_runtime(
                                    sec, {ch["field"]: ch["value"]},
                                    self.rules, self.config,
                                )
                            # If pairs were changed, refresh self.pairs
                            changed_fields = {
                                (ch["section"], ch["field"])
                                for ch in advisor_result.get("applied", [])
                            }
                            if ("trading", "pairs") in changed_fields:
                                new_pairs = self.config.get("trading", {}).get("pairs", self.pairs)
                                if isinstance(new_pairs, list) and new_pairs:
                                    with self._pairs_lock:
                                        old_count = len(self.pairs)
                                        self.pairs = list(new_pairs)  # copy-on-write
                                        self.all_tracked_pairs = list(set(self.pairs + self.watchlist_pairs))  # M23 fix
                                    logger.info(
                                        f"🔄 Active pairs updated by settings advisor: "
                                        f"{old_count} → {len(new_pairs)} pairs"
                                    )

                            # Refresh max_active_pairs if changed (e.g. human edit via Telegram/YAML)
                            if ("trading", "max_active_pairs") in changed_fields:
                                new_cap = int(self.config.get("trading", {}).get(
                                    "max_active_pairs", self._max_active_pairs
                                ))
                                clamped = min(new_cap, self._rpm_entity_cap)
                                if clamped != new_cap:
                                    logger.warning(
                                        f"⚠️ max_active_pairs {new_cap} clamped to "
                                        f"{clamped} by RPM guardrail"
                                    )
                                old_cap = self._max_active_pairs
                                self._max_active_pairs = clamped
                                logger.info(
                                    f"🔄 max_active_pairs updated: {old_cap} → {clamped}"
                                )

                            # Refresh interval if changed (affects RPM budget)
                            if ("trading", "interval") in changed_fields:
                                new_interval = int(self.config.get("trading", {}).get(
                                    "interval", self.interval
                                ))
                                self.interval = new_interval
                                with _health_lock:
                                    _health_mod._cycle_interval = new_interval
                                # Recompute RPM budget with new interval
                                rpm_max, rpm_bd = compute_rpm_entity_cap(
                                    self.config.get("llm_providers", []),
                                    new_interval,
                                )
                                self._rpm_entity_cap = rpm_max
                                self._rpm_breakdown = rpm_bd
                                if self._max_active_pairs > rpm_max:
                                    logger.warning(
                                        f"⚠️ After interval change to {new_interval}s, "
                                        f"max_active_pairs clamped from "
                                        f"{self._max_active_pairs} to {rpm_max}"
                                    )
                                    self._max_active_pairs = rpm_max
                                logger.info(
                                    f"🔄 Interval updated to {new_interval}s, "
                                    f"RPM entity cap recalculated: {rpm_max}"
                                )
                            # Notify via Telegram (direct send — infrequent, system-level)
                            notif = format_advisor_notification(advisor_result)
                            if notif and self.telegram:
                                self.telegram.send_alert(notif)
                            self.audit.log_event(
                                "settings_advisor",
                                f"Applied {advisor_result['changes_applied']} autonomous setting change(s)",
                                advisor_result,
                            )
                    except Exception as _sa_err:
                        logger.warning("Settings advisor error (non-fatal)", exc_info=True)

                # ─── Adaptive Learning Engine tick ────────────────────
                try:
                    learning_result = self._loop.run_until_complete(
                        self.learning_manager.tick(cycle_count)
                    )
                    if learning_result and not learning_result.get("skipped"):
                        logger.debug(f"🧠 ALE tick: {list(learning_result.keys())}")
                except Exception as _ale_err:
                    logger.warning("ALE tick error (non-fatal)", exc_info=True)

                # Sync state to Redis
                self.state_manager.sync_to_redis()

                # Publish trailing stop state to Redis for dashboard
                self.dashboard_commands.publish_trailing_stops()

                # Process any HITL commands from dashboard
                self.dashboard_commands.process_commands()

                # Prune stale pending approvals (> 1 hour old)
                self.state_manager.prune_stale_approvals()

                # Update health status
                _cycle_duration_s = time.monotonic() - _cycle_t0
                components = check_component_health(
                    ollama_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
                    redis_client=self.redis,
                )
                update_health(
                    status="ok",
                    cycle_count=cycle_count,
                    components=components,
                    cycle_duration_s=round(_cycle_duration_s, 2),
                )
                logger.info(f"⏱️ Cycle #{cycle_count} completed in {_cycle_duration_s:.1f}s")

                # ─── Slow-cycle Telegram alert ────────────────────────
                _slow_threshold = self.interval * 2
                if _cycle_duration_s > _slow_threshold and self.telegram:
                    try:
                        self.telegram.send_alert(
                            f"⚠️ Slow cycle #{cycle_count}: {_cycle_duration_s:.0f}s "
                            f"(threshold: {_slow_threshold:.0f}s)"
                        )
                    except Exception:
                        pass

            except Exception as e:
                consecutive_errors += 1
                # H10 fix: Use sanitized traceback to prevent credential leakage
                # in logs that may be accessible via dashboard
                sanitized_tb = sanitize_exception(e)
                logger.error(f"Pipeline error: {type(e).__name__}\n{sanitized_tb}")
                if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    alert_msg = (
                        f"🚨 *Auto-Traitor alert*: {consecutive_errors} consecutive pipeline "
                        f"errors — last type: `{type(e).__name__}` (see server logs for details)"
                    )
                    logger.error(alert_msg)
                    if self.telegram:
                        try:
                            self.telegram.send_alert(alert_msg)
                        except Exception:
                            pass
            else:
                consecutive_errors = 0

            logger.info(
                f"💼 Portfolio: {format_currency(self.state.portfolio_value, self.state.currency_symbol)} | "
                f"Return: {format_percentage(self.state.return_pct)} | "
                f"Drawdown: {format_percentage(self.state.max_drawdown)} | "
                f"Trailing stops: {self.trailing_stops.get_active_count()}"
            )

            # ─── Proactive Updates (LLM-generated) ───
            self.telegram_manager.send_proactive_update()

            # ─── Wait for next cycle, waking every 10 s to check early triggers ──
            # WS price-move or news pub/sub events can fire a pipeline mid-interval
            # so the agent reacts within seconds rather than waiting up to 120 s.
            _cycle_t0 = time.monotonic()
            elapsed = 0.0
            while (
                elapsed < self.interval
                and self.state.is_running
                and not self.state.is_paused
                and not self.state.circuit_breaker_triggered
            ):
                time.sleep(min(10.0, self.interval - elapsed))
                elapsed = time.monotonic() - _cycle_t0  # L16 fix: use wall clock

                with self._ws_trigger_lock:
                    # M6 fix: Read all_tracked_pairs under lock to prevent race
                    with self._pairs_lock:
                        tracked = set(self.all_tracked_pairs)
                    early_pairs = (
                        self._ws_trigger_pairs | self._news_trigger_pairs
                    ).intersection(tracked)
                    self._ws_trigger_pairs.clear()
                    self._news_trigger_pairs.clear()

                if not early_pairs:
                    continue

                # Filter early triggers for closed equity markets
                if getattr(self.exchange, "asset_class", "crypto") == "equity":
                    early_pairs = {p for p in early_pairs if _is_market_open(p, "equity")}
                    if not early_pairs:
                        continue

                logger.info(
                    f"⚡ Early-trigger pipeline for {sorted(early_pairs)} "
                    f"({elapsed:.0f}s / {self.interval}s into cycle)"
                )
                try:
                    tasks = [self.pipeline_manager.run_pipeline(ep) for ep in early_pairs]
                    self._loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                except Exception as _ep_err:
                    logger.error(f"Early-trigger pipeline error: {_ep_err}", exc_info=True)
                    
                # Record timestamps for early-triggered pairs so the batch scorer
                # treats them as freshly scanned on the next full cycle.
                _et_now = time.monotonic()
                for _ep in early_pairs:
                    self._last_pipeline_ts[_ep] = _et_now

                # Reset WS baselines so these pairs don’t immediately re-trigger
                if self.ws_feed:
                    with self._ws_trigger_lock:
                        for ep in early_pairs:
                            p_now = self.ws_feed.get_price(ep)
                            if p_now > 0:
                                self._ws_last_prices[ep] = p_now
                break  # restart the full interval after an early trigger

        logger.info("Orchestrator stopped.")
        # Stop LLM recovery poller
        self.llm.stop_recovery_polling()
        # H2 fix: close the asyncio event loop to release resources
        try:
            self._loop.close()
        except Exception:
            pass

    # =========================================================================
    # Portfolio Rotation
    # =========================================================================

    def _run_rotation(self) -> None:
        """Evaluate and execute autonomous portfolio rotations."""
        try:
            # Determine held pairs (pairs with open positions)
            held_pairs = list(self.state.open_positions.keys())
            if not held_pairs:
                logger.debug("No open positions — skipping rotation check")
                return

            # Build broader candidate list from scan results (not just active pairs)
            scan_pairs = list(self._scan_results.keys()) if self._scan_results else []
            all_candidate_pairs = list(set(self.pairs + scan_pairs))

            proposals = self._loop.run_until_complete(self.rotator.evaluate_rotation(
                held_pairs=held_pairs,
                all_pairs=all_candidate_pairs,
                current_prices=self.state.current_prices,
                portfolio_value=self.state.portfolio_value,
                scan_results=self._scan_results,
                open_positions=self.state.open_positions,
                max_open_positions=self.portfolio_scaler.tier.max_open_positions,
            ))

            if not proposals:
                return

            for proposal in proposals:
                if proposal.priority == "autonomous":
                    # Execute autonomously (within allocation, fee-positive)
                    logger.info(
                        f"🔄 Auto-swap: {proposal.sell_pair} → {proposal.buy_pair} "
                        f"({format_currency(proposal.quote_amount, self.state.currency_symbol)}, net +{proposal.net_gain_pct*100:.2f}%)"
                    )
                    result = self.rotator.execute_swap(
                        proposal,
                        portfolio_value=self.state.portfolio_value,
                        cash_balance=self.state.cash_balance,
                    )
                    if result.get("partial") and self.telegram:
                        partial_msg = result.get("alert_message") or (
                            f"⚠️ Rotation partial failure: {proposal.sell_pair}→{proposal.buy_pair}. "
                            f"Details: {result.get('error', 'unknown error')}"
                        )
                        if result.get("reversal"):
                            partial_msg += f"\nReversal result: {result.get('reversal')}"
                        self.telegram.send_alert(partial_msg)
                    if result.get("executed") and self.telegram:
                        route_info = ""
                        if result.get("route_type"):
                            route_info = f"\nRoute: {result['route_type']}"
                            if result.get("bridge_currency"):
                                route_info += f" via {result['bridge_currency']}"
                            route_info += f" ({result.get('n_legs', 2)} legs)"
                        self.telegram.send_trade_notification(
                            f"🔄 *Auto-Swap Executed*\n\n"
                            f"Sold: {proposal.sell_pair}\n"
                            f"Bought: {proposal.buy_pair}\n"
                            f"Amount: {format_currency(proposal.quote_amount, self.state.currency_symbol)}\n"
                            f"Expected net gain: +{proposal.net_gain_pct*100:.2f}%\n"
                            f"Fees: {format_currency(proposal.fee_estimate.total_fee_quote, self.state.currency_symbol)}\n"
                            f"Confidence: {format_percentage(proposal.confidence)}"
                            f"{route_info}"
                        )

                elif proposal.priority in ("high_impact", "critical"):
                    # Escalate to owner via Telegram
                    swap_id = f"swap_{uuid.uuid4().hex[:8]}_{proposal.buy_pair}"
                    self.rotator.add_pending_swap(swap_id, proposal)  # M27 fix
                    with self._pending_approvals_lock:
                        self._pending_approvals[swap_id] = {
                            "_is_swap": True,
                            "_swap_proposal": proposal,
                        }

                    if self.telegram:
                        msg = self.rotator.format_swap_approval_request(proposal)
                        emoji = "🔴" if proposal.priority == "critical" else "🟡"
                        self.telegram.send_message(
                            f"{emoji} {msg}\n\n"
                            f"Reply: /approve {swap_id}\nor: /reject {swap_id}"
                        )
                    else:
                        logger.warning(
                            f"Swap needs approval but Telegram not configured: "
                            f"{proposal.sell_pair} → {proposal.buy_pair}"
                        )

        except Exception as e:
            logger.error(f"Rotation error: {e}", exc_info=True)


