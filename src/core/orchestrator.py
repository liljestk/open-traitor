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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

from src.agents.market_analyst import MarketAnalystAgent
from src.agents.strategist import StrategistAgent
from src.agents.risk_manager import RiskManagerAgent
from src.agents.executor import ExecutorAgent
from src.agents.settings_advisor import SettingsAdvisorAgent, format_advisor_notification
from src.core.exchange_client import ExchangeClient
from src.core.coinbase_client import _KNOWN_FIAT, _USD_EQUIVALENTS, _EUR_EQUIVALENTS, _ALL_STABLECOINS, _KNOWN_QUOTES, _get_fiat_rate_usd
from src.core.llm_client import LLMClient
from src.core.rules import AbsoluteRules
from src.core.state import TradingState
from src.core.ws_feed import CoinbaseWebSocketFeed
from src.core.trailing_stop import TrailingStopManager
from src.core.health import update_health, check_component_health, start_health_server
from src.core.fee_manager import FeeManager
from src.core.high_stakes import HighStakesManager
from src.core.portfolio_rotator import PortfolioRotator
from src.core.route_finder import RouteFinder
from src.analysis.fear_greed import FearGreedIndex
from src.analysis.multi_timeframe import MultiTimeframeAnalyzer
from src.analysis.sentiment import SentimentAnalyzer
from src.analysis.technical import TechnicalAnalyzer
from src.strategies import EMACrossoverStrategy, BollingerReversionStrategy, PairsCorrelationMonitor
from src.utils.tax import FIFOTracker
from src.news.aggregator import NewsAggregator
from src.telegram_bot.chat_handler import TelegramChatHandler
from src.utils.logger import get_logger
from src.utils.helpers import format_currency, format_percentage
from src.utils.rate_limiter import get_rate_limiter
from src.utils.security import sanitize_input
from src.utils.journal import TradeJournal
from src.utils.audit import AuditLog
from src.utils.stats import StatsDB
from src.utils.tracer import get_llm_tracer
from src.utils import settings_manager as sm
from src.core.managers.pipeline_manager import PipelineManager
from src.core.managers.state_manager import StateManager
from src.core.health import check_component_health, update_health
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

        # Trading state
        if getattr(exchange, "paper_mode", False):
            initial_balance = 10000.0
        else:
            try:
                initial_balance = exchange.get_portfolio_value()
            except Exception as _e:
                logger.warning(f"⚠️ Could not fetch live portfolio value on startup: {_e} — defaulting to $0")
                initial_balance = 0.0
        self.state = TradingState(initial_balance=initial_balance)
        self.state.is_running = True

        # Trading pairs
        self.pairs = config.get("trading", {}).get("pairs", ["BTC-USD"])
        self.watchlist_pairs = config.get("trading", {}).get("watchlist_pairs", [])
        self.all_tracked_pairs = list(set(self.pairs + self.watchlist_pairs))
        self.interval = config.get("trading", {}).get("interval", 120)

        # Initialize agents
        self.market_analyst = MarketAnalystAgent(llm, self.state, config)
        self.strategist = StrategistAgent(llm, self.state, config)
        self.risk_manager = RiskManagerAgent(llm, self.state, config, rules)
        self.executor = ExecutorAgent(llm, self.state, config, exchange, rules)
        self.settings_advisor = SettingsAdvisorAgent(
            llm, self.state, config, rules,
            review_interval=config.get("trading", {}).get("settings_review_interval", 10),
        )

        # New analysis components
        self.fear_greed = FearGreedIndex()
        self.multi_tf = MultiTimeframeAnalyzer(config, exchange)
        self.trailing_stops = TrailingStopManager(
            default_trail_pct=config.get("risk", {}).get("trailing_stop_pct", 0.03),
            enable_tiers=config.get("risk", {}).get("enable_tiered_stops", False),
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

        # Route finder (optimal swap route discovery)
        self.route_finder = RouteFinder(exchange, self.fee_manager, config)

        # Portfolio rotator (autonomous crypto-to-crypto swaps)
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

        # Tasks
        self.active_tasks: list[Task] = []
        
        self.pipeline_manager = PipelineManager(self)
        self.state_manager = StateManager(self)
        
        self._pending_approvals: dict[str, dict] = {}
        self._pending_approvals_lock = threading.Lock()
        self.state_manager.load_pending_approvals()

        # ─── Stats Database (persistent analytics) ───
        self.stats_db = StatsDB()

        # ─── Live Holdings Sync (Coinbase API → TradingState) ───
        trading_cfg = config.get("trading", {})
        self._holdings_sync_enabled: bool = trading_cfg.get("live_holdings_sync", True)
        self._holdings_refresh_seconds: float = float(trading_cfg.get("holdings_refresh_seconds", 60))
        self._holdings_dust_threshold: float = float(trading_cfg.get("holdings_dust_threshold", 0.01))

        # Initial sync on startup (live mode only)
        if self._holdings_sync_enabled and not getattr(exchange, 'paper_mode', False):
            try:
                snapshot = self._live_coinbase_snapshot()
                self.state.sync_live_holdings(snapshot, dust_threshold=self._holdings_dust_threshold)
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
                            import yaml
                            settings_path = os.path.join("config", "settings.yaml")
                            with open(settings_path, "r", encoding="utf-8") as f:
                                raw = f.read()
                            raw = raw.replace(
                                "invalidate_strategic_context: true",
                                "invalidate_strategic_context: false",
                            )
                            with open(settings_path, "w", encoding="utf-8") as f:
                                f.write(raw)
                            logger.info("🔄 Auto-reset invalidate_strategic_context → false")
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

        # ─── Universe Tracking (funnel system) ────────────────────────────
        self._pair_universe: list[dict] = []           # full product metadata
        self._pair_universe_ts: float = 0.0
        self._PAIR_UNIVERSE_TTL: float = float(
            trading_cfg.get("pair_universe_refresh_seconds", 1800)
        )
        self._scan_results: dict[str, dict] = {}       # pair → {technicals, strategies, score}
        self._screener_active_pairs: list[str] = []    # pairs selected by LLM screener
        self._screener_cycle_counter: int = 0
        self._SCREENER_INTERVAL: int = int(
            trading_cfg.get("screener_interval_cycles", 5)
        )
        self._max_active_pairs: int = int(
            trading_cfg.get("max_active_pairs", 5)
        )
        self._scan_volume_threshold: float = float(
            trading_cfg.get("scan_volume_threshold", 1000)
        )
        self._scan_movement_threshold_pct: float = float(
            trading_cfg.get("scan_movement_threshold_pct", 1.0)
        )
        self._include_crypto_quotes: bool = bool(
            trading_cfg.get("include_crypto_quotes", True)
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

        # ─── LLM Chat Handler (conversational Telegram interface) ───
        self.chat_handler = TelegramChatHandler(
            llm_client=llm,
            rate_limiter=self.rate_limiter,
        )
        self._register_chat_functions()

        # Connect to Telegram bot
        if self.telegram:
            self.telegram.chat_handler = self.chat_handler
            self.telegram.on_command = self._handle_telegram_command  # Legacy fallback
            self.chat_handler.set_send_callback(self.telegram.send_message)
            # Connect proactive engine to trading state + stats
            self.chat_handler.set_context_provider(self._get_trading_context)
            if self.chat_handler._proactive:
                self.chat_handler._proactive.set_stats_db(self.stats_db)

        # Start health server
        start_health_server(port=config.get("health", {}).get("port", 8080))

        # Register WS price-move detector (runs in WS thread, very lightweight)
        if self.ws_feed:
            self.ws_feed.add_ticker_callback(self._on_ws_ticker)

        # Subscribe to Redis news:updates channel so fresh news triggers early pipelines
        self._start_news_subscriber()

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

    def run_forever(self) -> None:
        """Main loop — runs continuously until stopped."""
        logger.info("🚀 Starting main trading loop...")

        if self.telegram:
            self.telegram.send_message(
                "🤖 *Auto-Traitor Online*\n\n"
                f"Mode: {'📝 Paper' if getattr(self.exchange, 'paper_mode', False) else '💰 Live'}\n"
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

        while self.state.is_running:
            if self.state.is_paused:
                logger.debug("Trading paused, waiting...")
                time.sleep(10)
                continue

            if self.state.circuit_breaker_triggered:
                logger.warning("🛑 Circuit breaker active — trading halted")
                time.sleep(60)
                continue

            cycle_count += 1
            logger.info(f"━━━ Cycle #{cycle_count} ━━━━━━━━━━━━━━━━━━━━━━━━")
            _cycle_t0 = time.monotonic()

            try:
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
                    continue
                self._ollama_skip_count = 0

                # Run pipelines — parallelised across pairs using asyncio
                # Sort pairs: planning-preferred first, then normal, avoid last
                priority_map = getattr(self, "_pair_priority_map", {})

                # ─── Universe Scan + LLM Screener (funnel system) ─────
                try:
                    self._refresh_pair_universe()
                    self._run_universe_scan()

                    self._screener_cycle_counter += 1
                    if self._screener_cycle_counter >= self._SCREENER_INTERVAL:
                        self._screener_cycle_counter = 0
                        self._run_llm_screener()
                except Exception as _uf_err:
                    logger.warning(f"Universe funnel error (non-fatal): {_uf_err}")

                # Effective pairs: screener-selected (if any) or configured seed list
                effective_pairs = self._screener_active_pairs or self.pairs[:self._max_active_pairs]

                sorted_pairs = sorted(
                    effective_pairs,
                    key=lambda p: priority_map.get(p, 0.0),  # negative = preferred → first
                )

                try:
                    tasks = [self.pipeline_manager.run_pipeline(p) for p in sorted_pairs]

                    async def _run_pipelines():
                        await asyncio.gather(*tasks)

                    asyncio.run(_run_pipelines())
                except Exception as _pe:
                    logger.error(f"Pipeline worker error: {_pe}", exc_info=True)

                # ─── Check pending limit orders ───
                try:
                    self.executor.check_pending_orders()
                except Exception as _po_err:
                    logger.debug(f"Pending order check error: {_po_err}")

                # ─── Portfolio Rotation (autonomous swaps) ───
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
                        self.trailing_stops.remove_stop(pair)
                        pnl = close_result.get("pnl")
                        self.audit.log_trade(
                            pair=pair, action="trailing_stop_exit",
                            amount=close_result.get("close_price", trigger_price) or 0,
                            price=close_result.get("close_price", trigger_price) or 0,
                        )
                        if pnl is not None and pnl < 0:
                            self.rules.record_loss(abs(pnl))
                        event_msg = (
                            f"Trailing stop executed on {pair} "
                            f"at {format_currency(trigger_price)}"
                            + (f" — PnL: {format_currency(pnl)}" if pnl is not None else "")
                        )
                        if not close_result.get("success", True):
                            logger.error(
                                f"❌ Trailing stop sell FAILED for {pair} — "
                                "position state unchanged; will retry next cycle."
                            )
                    else:
                        # No open trade found — stop is stale; remove it
                        self.trailing_stops.remove_stop(pair)
                        event_msg = (
                            f"Trailing stop triggered on {pair} "
                            f"at {format_currency(trigger_price)} (no open position found)"
                        )

                    self.chat_handler.queue_event(event_msg)
                    if self.telegram:
                        self.telegram.send_trade_notification(
                            f"🎯 {event_msg}\n"
                            f"Entry: {format_currency(entry_price)}"
                        )

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
                            # FIFO tracking for tier exit sell
                            try:
                                base_asset = te_pair.split("-")[0] if "-" in te_pair else te_pair
                                self.fifo_tracker.record_sell(
                                    asset=base_asset,
                                    quantity=te_qty,
                                    sale_price_per_unit=te_price,
                                )
                            except Exception:
                                pass

                            tier_msg = (
                                f"Tier exit +{te_pct:.0f}% on {te_pair}: "
                                f"sold {te_qty:.6f} at {format_currency(te_price)} "
                                f"(PnL: +{te.get('pnl_pct', 0):.1f}%)"
                            )
                            self.chat_handler.queue_event(tier_msg)
                            if self.telegram:
                                self.telegram.send_trade_notification(f"📊 {tier_msg}")
                    except Exception as e:
                        logger.warning(f"⚠️ Tier exit failed for {te_pair}: {e}")

                # Check stop-losses on all positions
                closed = self.executor.check_stop_losses()
                for c in closed:
                    pnl = c.get("pnl", 0)
                    emoji = "🎯" if pnl and pnl > 0 else "⚠️"
                    event_msg = (
                        f"Position closed ({c['reason']}): {c['pair']} "
                        f"PnL: {format_currency(pnl or 0)}"
                    )
                    self.chat_handler.queue_event(event_msg)
                    if self.telegram:
                        self.telegram.send_trade_notification(
                            f"{emoji} {event_msg}"
                        )

                # Take portfolio snapshot
                self.state.take_portfolio_snapshot()

                # Check circuit breaker
                if self.state.max_drawdown >= self.config.get("risk", {}).get("max_drawdown_pct", 0.10):
                    self.state.circuit_breaker_triggered = True
                    msg = f"🛑 CIRCUIT BREAKER: Max drawdown {format_percentage(self.state.max_drawdown)} reached!"
                    logger.warning(msg)
                    self.audit.log_circuit_breaker("max_drawdown", self.state.max_drawdown)
                    self.chat_handler.queue_event(f"CRITICAL: {msg}")
                    if self.telegram:
                        self.telegram.send_alert(msg)
                    self._trigger_emergency_replan(
                        f"Circuit breaker: drawdown {format_percentage(self.state.max_drawdown)}"
                    )

                # Save state periodically
                self.state.save_state()

                # ─── Position reconciliation (live mode only) ────────────
                if not getattr(self.exchange, 'paper_mode', False):
                    self._reconcile_counter += 1
                    if self._reconcile_counter >= self._reconcile_every:
                        self._reconcile_counter = 0
                        self._reconcile_positions()

                # ─── Autonomous Settings Advisor ────────────────────
                if self.settings_advisor.should_run():
                    try:
                        advisor_ctx = {
                            "fear_greed": getattr(self.state, "fear_greed_summary", "unavailable"),
                            "recent_performance": self._get_performance_summary(),
                            "market_volatility": getattr(self.state, "volatility_summary", "moderate"),
                            "current_prices": dict(self.state.current_prices),
                            "cycle_id": str(cycle_count),
                            "stats_db": self.stats_db if hasattr(self, "stats_db") else None,
                            "trace_ctx": self.trace_ctx if hasattr(self, "trace_ctx") else None,
                            "scan_results_summary": self._get_scan_summary(),
                            "universe_size": len(self._pair_universe),
                        }
                        advisor_result = asyncio.run(
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
                                    old_count = len(self.pairs)
                                    self.pairs = new_pairs
                                    logger.info(
                                        f"🔄 Active pairs updated by settings advisor: "
                                        f"{old_count} → {len(self.pairs)} pairs"
                                    )
                            # Notify via Telegram
                            notif = format_advisor_notification(advisor_result)
                            if notif:
                                self.chat_handler.queue_event(notif)
                                if self.telegram:
                                    self.telegram.send_alert(notif)
                            self.audit.log_event(
                                "settings_advisor",
                                f"Applied {advisor_result['changes_applied']} autonomous setting change(s)",
                                advisor_result,
                            )
                    except Exception as _sa_err:
                        logger.warning(f"Settings advisor error (non-fatal): {_sa_err}")

                # Sync state to Redis
                self.state_manager.sync_to_redis()

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
                # Full traceback in server logs; redact message before broadcasting
                # to Telegram — exception text can contain connection strings, paths,
                # or credential fragments.
                logger.error(f"Pipeline error: {e}", exc_info=True)
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
                f"💼 Portfolio: {format_currency(self.state.portfolio_value)} | "
                f"Return: {format_percentage(self.state.return_pct)} | "
                f"Drawdown: {format_percentage(self.state.max_drawdown)} | "
                f"Trailing stops: {self.trailing_stops.get_active_count()}"
            )

            # ─── Proactive Updates (LLM-generated) ───
            self._send_proactive_update()

            # ─── Wait for next cycle, waking every 10 s to check early triggers ──
            # WS price-move or news pub/sub events can fire a pipeline mid-interval
            # so the agent reacts within seconds rather than waiting up to 120 s.
            elapsed = 0.0
            while (
                elapsed < self.interval
                and self.state.is_running
                and not self.state.is_paused
                and not self.state.circuit_breaker_triggered
            ):
                time.sleep(min(10.0, self.interval - elapsed))
                elapsed += 10.0

                with self._ws_trigger_lock:
                    early_pairs = (
                        self._ws_trigger_pairs | self._news_trigger_pairs
                    ).intersection(set(self.pairs))
                    self._ws_trigger_pairs.clear()
                    self._news_trigger_pairs.clear()

                if not early_pairs:
                    continue

                logger.info(
                    f"⚡ Early-trigger pipeline for {sorted(early_pairs)} "
                    f"({elapsed:.0f}s / {self.interval}s into cycle)"
                )
                try:
                    tasks = [self.pipeline_manager.run_pipeline(ep) for ep in early_pairs]

                    async def _run_early():
                        await asyncio.gather(*tasks)

                    asyncio.run(_run_early())
                except Exception as _ep_err:
                    logger.error(f"Early-trigger pipeline error: {_ep_err}", exc_info=True)
                    
                # Reset WS baselines so these pairs don’t immediately re-trigger
                if self.ws_feed:
                    with self._ws_trigger_lock:
                        for ep in early_pairs:
                            p_now = self.ws_feed.get_price(ep)
                            if p_now > 0:
                                self._ws_last_prices[ep] = p_now
                break  # restart the full interval after an early trigger

        logger.info("Orchestrator stopped.")

    # =========================================================================
    # Reactive Emergency Re-Planning
    # =========================================================================

    _REPLAN_COOLDOWN_S: float = 1800.0  # min 30 min between emergency replans
    _replan_last_ts: float = 0.0

    def _trigger_emergency_replan(self, reason: str) -> None:
        """Write an emergency conservative strategic context and attempt a Temporal replan.

        Called when the circuit breaker fires or an extreme WS price move (≥3%)
        is detected.  Works even if Temporal is down — the local DB write is
        immediate and the orchestrator picks it up on the next cache refresh.
        """
        now = time.time()
        if now - self._replan_last_ts < self._REPLAN_COOLDOWN_S:
            logger.debug("Emergency replan skipped — cooldown active")
            return
        self._replan_last_ts = now

        logger.warning(f"🚨 Emergency replan triggered: {reason}")

        # 1. Write a conservative emergency context to StatsDB immediately
        try:
            emergency_plan = {
                "regime": "volatile",
                "confidence": 0.3,
                "risk_posture": "conservative",
                "preferred_pairs": [],
                "avoid_pairs": list(self.pairs),  # avoid all pairs until next plan
                "key_observations": [
                    f"EMERGENCY: {reason}",
                    "All pairs set to avoid — waiting for next scheduled plan evaluation",
                ],
                "today_focus": "Capital preservation — emergency mode active",
                "summary": (
                    f"Emergency replan: {reason}. "
                    "Switched to conservative posture, all pairs on avoid. "
                    "Next scheduled plan will re-evaluate."
                ),
            }
            self.stats_db.save_strategic_context(
                horizon="daily",
                plan_json=emergency_plan,
                summary_text=emergency_plan["summary"],
            )
            # Invalidate the cache so the next cycle picks up the emergency plan
            self._strategic_context_ts = 0.0

            if self.telegram:
                self.telegram.send_alert(
                    f"🚨 *Emergency Replan*\n\n"
                    f"Reason: {reason}\n"
                    f"Action: Switched to conservative posture, all pairs on avoid.\n"
                    f"Next scheduled plan will re-evaluate."
                )
        except Exception as e:
            logger.error(f"Failed to write emergency context: {e}")

        # 2. Optionally trigger a Temporal DailyPlanWorkflow
        def _try_temporal_replan() -> None:
            try:
                import temporalio.client as _tc

                temporal_host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
                temporal_ns = os.environ.get("TEMPORAL_NAMESPACE", "default")

                async def _start_workflow():
                    client = await _tc.Client.connect(temporal_host, namespace=temporal_ns)
                    from src.planning.workflows import DailyPlanWorkflow
                    await client.start_workflow(
                        DailyPlanWorkflow.run,
                        id=f"emergency-replan-{uuid.uuid4().hex[:8]}",
                        task_queue="planning-queue",
                    )
                    logger.info("📋 Emergency Temporal replan workflow started")

                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(_start_workflow())
                finally:
                    loop.close()
            except Exception as e:
                logger.debug(
                    f"Temporal emergency replan unavailable (local context already written): {e}"
                )

        # Run Temporal attempt in background thread to avoid blocking
        threading.Thread(
            target=_try_temporal_replan, daemon=True, name="emergency-replan"
        ).start()

    # =========================================================================
    # Event-Driven Helpers — WS Trigger + News Pub/Sub
    # =========================================================================

    def _on_ws_ticker(self, data: dict) -> None:
        """Called by the WS feed on every ticker tick (runs in WS thread).

        Compares the new price against the snapshot recorded at the last pipeline
        start for this pair.  If the move exceeds *_ws_trigger_pct* the pair is
        queued for an early pipeline run during the idle sleep period.
        """
        pair = data.get("product_id", "")
        price = float(data.get("price", 0))
        if not pair or price <= 0 or pair not in self.pairs:
            return

        with self._ws_trigger_lock:
            last = self._ws_last_prices.get(pair, 0)
            if last > 0:
                change_pct = abs(price - last) / last
                if change_pct >= self._ws_trigger_pct:
                    self._ws_trigger_pairs.add(pair)
                    logger.info(
                        f"📡 WS trigger: {pair} moved {change_pct:+.2%} "
                        f"(${last:,.2f} → ${price:,.2f}) — early pipeline queued"
                    )
                # Extreme move (≥3%) → trigger emergency replan
                if change_pct >= 0.03:
                    threading.Thread(
                        target=self._trigger_emergency_replan,
                        args=(f"{pair} moved {change_pct:+.2%} in a single tick",),
                        daemon=True,
                        name="ws-emergency-replan",
                    ).start()
            # Always keep the running WS price current so the next check is fresh
            self._ws_last_prices[pair] = price

    def _start_news_subscriber(self) -> None:
        """Subscribe to Redis *news:updates* pub/sub channel in a daemon thread.

        When the news worker publishes a fresh batch all active pairs are added
        to the news-trigger set so the main loop fetches up-to-date headlines
        during the next early-pipeline check rather than waiting a full interval.
        No-op if Redis is not configured.
        """
        if not self.redis:
            return

        def _listener() -> None:
            try:
                pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe("news:updates")
                for message in pubsub.listen():
                    if not self.state.is_running:
                        break
                    if message and message.get("type") == "message":
                        with self._ws_trigger_lock:
                            self._news_trigger_pairs.update(self.pairs)
                        logger.debug(
                            "📰 Breaking news detected via pub/sub — early pipeline queued"
                        )
            except Exception as e:
                logger.debug(f"News pub/sub subscriber error: {e}")

        t = threading.Thread(target=_listener, daemon=True, name="news-sub")
        t.start()
        logger.info("📰 News pub/sub subscriber started")

    def _get_strategic_context(self) -> str:
        """Return the latest strategic context string (cached 60s, reads from StatsDB)."""
        now = time.time()
        if now - self._strategic_context_ts < self._STRATEGIC_CONTEXT_TTL:
            return self._strategic_context_str
        try:
            rows = self.stats_db.get_latest_strategic_context()
            if not rows:
                self._strategic_context_str = ""
                self._pair_priority_map = {}
            else:
                parts = []
                for row in rows:
                    horizon = row["horizon"].upper()
                    text = row["summary_text"] or ""
                    if text:
                        parts.append(f"[{horizon} PLAN] {text}")
                self._strategic_context_str = "\n".join(parts)

                # ── Parse pair priority from latest daily plan ──────────
                self._pair_priority_map = self._parse_pair_priorities(rows)

                # Warn when the newest plan is older than 48 h (planning worker down?)
                try:
                    latest_ts_str = max(row["ts"] for row in rows)
                    latest_ts = datetime.fromisoformat(latest_ts_str.replace("Z", "+00:00"))
                    age_h = (datetime.now(timezone.utc) - latest_ts).total_seconds() / 3600
                    if age_h > 48:
                        logger.warning(
                            f"⚠️ Strategic context is {age_h:.0f}h old — "
                            "planning worker may not be running; using stale plan."
                        )
                except Exception:
                    pass
            self._strategic_context_ts = now
        except Exception as e:
            logger.debug(f"Failed to load strategic context: {e}")
        return self._strategic_context_str

    def _parse_pair_priorities(self, context_rows: list[dict]) -> dict[str, float]:
        """Extract per-pair confidence adjustments from the latest daily/weekly plans.

        Returns a dict mapping pair -> confidence_adjustment:
          * preferred pairs get -0.05 (slightly more lenient)
          * avoid pairs get +0.10 (need stronger signal to trade)
          * other pairs get 0.0 (no adjustment)
        """
        preferred: set[str] = set()
        avoid: set[str] = set()

        for row in context_rows:
            try:
                plan = json.loads(row.get("plan_json", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue

            horizon = row.get("horizon", "")
            if horizon in ("daily", "weekly"):
                for p in plan.get("preferred_pairs", plan.get("pairs_to_focus", [])):
                    preferred.add(p)
                for p in plan.get("avoid_pairs", plan.get("pairs_to_reduce", [])):
                    avoid.add(p)

        priority_map: dict[str, float] = {}
        for pair in self.pairs:
            if pair in avoid:
                priority_map[pair] = 0.10   # raise min_confidence by 10pp
            elif pair in preferred:
                priority_map[pair] = -0.05   # lower min_confidence by 5pp
            # else: 0.0 (default, not stored to keep map sparse)

        if priority_map:
            logger.info(
                f"📋 Pair priority from planning: "
                f"focus={[p for p, v in priority_map.items() if v < 0]}, "
                f"avoid={[p for p, v in priority_map.items() if v > 0]}"
            )
        return priority_map

    def get_pair_confidence_adjustment(self, pair: str) -> float:
        """Return the confidence threshold adjustment for a pair (from planning context)."""
        return getattr(self, "_pair_priority_map", {}).get(pair, 0.0)

    def _maybe_refresh_holdings(self) -> None:
        """Refresh live Coinbase holdings if the TTL has elapsed.

        TTL-cached: only calls the API if enough time has passed since the
        last successful sync.  Graceful degradation: on failure, keeps the
        previous snapshot and does NOT update the timestamp, so the next
        cycle retries immediately.
        """
        if not self._holdings_sync_enabled or getattr(self.exchange, 'paper_mode', False):
            return
        now = time.time()
        if now - self.state._live_snapshot_ts < self._holdings_refresh_seconds:
            return  # still fresh
        try:
            snapshot = self._live_coinbase_snapshot()
            self.state.sync_live_holdings(
                snapshot, dust_threshold=self._holdings_dust_threshold
            )
        except Exception as e:
            # Graceful degradation: keep stale data, do NOT update _live_snapshot_ts
            # so the next pipeline cycle retries immediately.
            logger.warning(f"⚠️ Holdings refresh failed (keeping stale data): {e}")



    def _get_performance_summary(self) -> str:
        """Build a short performance summary string for the settings advisor.

        Uses StatsDB for accurate historical metrics (24h window) and
        TradingState for live portfolio/position data.
        """
        try:
            sym = self.state.currency_symbol
            parts: list[str] = []

            # Historical trade performance from StatsDB (24h)
            perf = self.stats_db.get_performance_summary(hours=24)
            stats = perf.get("trade_stats", {})
            total_trades = stats.get("total_trades", 0)
            winning = stats.get("winning", 0)
            total_pnl = stats.get("total_pnl", 0)
            win_rate = (winning / total_trades * 100) if total_trades > 0 else 0
            avg_confidence = stats.get("avg_confidence", 0)

            parts.append(f"24h trades: {total_trades}")
            if total_trades > 0:
                parts.append(f"win rate: {win_rate:.0f}%")
                parts.append(f"PnL: {sym}{total_pnl:+.2f}")
                parts.append(f"avg confidence: {avg_confidence:.0%}")

            # Current portfolio state
            n_positions = len(self.state.open_positions)
            pv = self.state.portfolio_value
            ret = self.state.return_pct
            dd = self.state.max_drawdown
            parts.append(f"open positions: {n_positions}")
            parts.append(f"portfolio: {sym}{pv:,.2f} ({ret:+.1%})")
            parts.append(f"max drawdown: {dd:.1%}")

            # Win/loss streak from recent trades
            recent = list(self.state.trades[-20:])
            closed = [t for t in recent if t.pnl is not None]
            if closed:
                streak = 0
                streak_type = "win" if (closed[-1].pnl or 0) > 0 else "loss"
                for t in reversed(closed):
                    if (streak_type == "win" and (t.pnl or 0) > 0) or \
                       (streak_type == "loss" and (t.pnl or 0) <= 0):
                        streak += 1
                    else:
                        break
                parts.append(f"current streak: {streak} {streak_type}{'es' if streak_type == 'loss' else 's'}")

            return " | ".join(parts)
        except Exception as e:
            logger.debug(f"Performance summary fallback: {e}")
            # Minimal fallback from TradingState only
            try:
                return (
                    f"trades: {self.state.total_trades}, "
                    f"win rate: {self.state.win_rate:.0%}, "
                    f"PnL: {self.state.total_pnl:+.2f}"
                )
            except Exception:
                return "unavailable"

    def _reconcile_positions(self) -> None:
        """
        Reconcile TradingState.positions against actual Coinbase balances (live mode only).
        Corrects drift caused by partial fills, crashes, or external account changes.
        Runs every reconcile_every_cycles cycles (~20 min at default 120s interval).
        """
        try:
            # Reconcile fiat-quoted pairs (USD, EUR, GBP, etc.).
            # Skip crypto-to-crypto cross pairs (e.g. ETH-BTC) because they
            # would produce an incorrect derived pair and corrupt state.
            # Build a mapping of base_currency -> original_pair for reconstruction.
            base_to_pair: dict[str, str] = {}
            expected: dict[str, float] = {}
            for pair, qty in self.state.open_positions.items():
                if qty <= 0 or "-" not in pair:
                    continue
                base, quote = pair.split("-", 1)
                if quote in _KNOWN_QUOTES:
                    expected[base] = qty
                    base_to_pair[base] = pair
            result = self.exchange.reconcile_positions(expected)

            if not result["matched"]:
                for d in result["discrepancies"]:
                    currency = d["currency"]
                    actual_qty = d["actual"]
                    # Reconstruct the original pair (e.g. ATOM-EUR, not ATOM-USD)
                    pair = base_to_pair.get(currency, f"{currency}-USD")

                    # Correct state to match actual Coinbase balance
                    with self.state._lock:
                        if actual_qty > 1e-8:
                            self.state.positions[pair] = actual_qty
                        else:
                            self.state.positions.pop(pair, None)

                    msg = (
                        f"⚠️ Position drift corrected: {currency} "
                        f"expected={d['expected']:.6f} actual={actual_qty:.6f} "
                        f"diff={d['diff']:+.6f}"
                    )
                    logger.warning(msg)
                    self.audit.log_rule_check(
                        "position_reconciliation",
                        passed=False,
                        details=msg,
                    )
                    self.stats_db.record_event(
                        event_type="reconciliation",
                        message=msg,
                        severity="warning",
                        pair=pair,
                        data=d,
                    )
                    if self.telegram:
                        self.telegram.send_alert(msg)
            else:
                logger.debug("✅ Position reconciliation: no discrepancies")

        except Exception as e:
            logger.warning(f"Position reconciliation failed: {e}")



    # =========================================================================
    # LLM Chat Handler — Function Registry
    # =========================================================================

    def _live_coinbase_snapshot(self) -> dict:
        """
        Fetch actual live account data directly from the Coinbase API.

        Returns a structured dict with:
          native_currency    – detected account currency (e.g. "EUR")
          total_portfolio    – total portfolio value in native currency
          fiat_cash          – fiat/cash balance in native currency
          currency_symbol    – display symbol (e.g. "€" or "$")
          holdings           – list of {currency, amount, native_value, is_fiat, pair, price}
          prices_by_pair     – {pair: price} for all tracked + held pairs
          fetch_ts           – UTC timestamp of this fetch
        """
        from datetime import timezone as _tz
        fetch_ts = datetime.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        # Detect the native (quote) currency from config or fallback to pairs
        # e.g. pairs=["BTC-EUR","ETH-EUR","ATOM-EUR"] → native="EUR"
        native = self.config.get("trading", {}).get("quote_currency", "auto").upper()
        if native == "AUTO":
            native = "USD"
            for pair in self.pairs:
                if "-" in pair:
                    _, quote = pair.rsplit("-", 1)
                    if quote in _KNOWN_FIAT:
                        native = quote
                        break
                    # EURC → treat as EUR for display
                    if quote in _EUR_EQUIVALENTS:
                        native = "EUR"
                        break
        currency_symbols = {"EUR": "€", "GBP": "£", "CHF": "CHF ", "USD": "$", "CAD": "C$", "AUD": "A$", "JPY": "¥"}
        symbol = currency_symbols.get(native, native + " ")

        try:
            accounts = self.exchange.get_accounts()
        except Exception as e:
            logger.warning(f"_live_coinbase_snapshot: get_accounts failed: {e}")
            accounts = []

        holdings = []
        prices_by_pair: dict[str, float] = {}
        total_value = 0.0
        fiat_cash = 0.0

        # Build a mapping of all tracked pairs to native currency pairs
        native_tracked = {}
        for pair in self.pairs:
            base = pair.split("-")[0] if "-" in pair else pair
            native_tracked[base] = pair  # e.g. ATOM → ATOM-EUR

        if accounts:
            raw = accounts.to_dict() if hasattr(accounts, "to_dict") else dict(accounts)
            account_list = raw.get("accounts", accounts)

            for acct in account_list:
                bal = acct.get("available_balance", {})
                currency = bal.get("currency", acct.get("currency", ""))
                val_str = bal.get("value", "0")
                try:
                    amount = float(val_str)
                except (ValueError, TypeError):
                    amount = 0.0

                if amount <= 0 or not currency:
                    continue

                is_fiat = currency in _KNOWN_FIAT or currency in _ALL_STABLECOINS

                # Convert to native account currency (e.g. EUR)
                if hasattr(self.exchange, "_currency_to_native"):
                    native_val = self.exchange._currency_to_native(currency, amount, native)
                else:
                    native_val = amount # Fallback

                # Determine best price pair to quote for this asset
                tracked_pair = native_tracked.get(currency)
                price = 0.0
                if not is_fiat:
                    # Try native pair first (e.g. ATOM-EUR)
                    native_pair = f"{currency}-{native}"
                    if tracked_pair:
                        try:
                            price = self.exchange.get_current_price(tracked_pair)
                            if price > 0:
                                prices_by_pair[tracked_pair] = price
                                self.state.update_price(tracked_pair, price)
                        except Exception:
                            pass
                    elif native != "USD":
                        # Try the native pair even if not tracked
                        try:
                            price = self.exchange.get_current_price(native_pair)
                            if price > 0:
                                prices_by_pair[native_pair] = price
                        except Exception:
                            pass
                    if price == 0:
                        # Fallback: USD pair → convert to native
                        usd_pair = f"{currency}-USD"
                        try:
                            price_usd = self.exchange.get_current_price(usd_pair)
                            if price_usd > 0:
                                prices_by_pair[usd_pair] = price_usd
                                # Convert to native display price
                                if native != "USD":
                                    rate_nat = _get_fiat_rate_usd(native)
                                    price = price_usd / rate_nat if rate_nat > 0 else price_usd
                                else:
                                    price = price_usd
                        except Exception:
                            pass

                holding = {
                    "currency": currency,
                    "amount": amount,
                    "native_value": round(native_val, 4),
                    "is_fiat": is_fiat,
                    "pair": tracked_pair or (f"{currency}-{native}" if not is_fiat else None),
                    "price": price,
                }
                holdings.append(holding)
                total_value += native_val
                if is_fiat:
                    fiat_cash += native_val

        # Also fetch prices for tracked pairs that aren't in the account
        for pair in self.pairs:
            if pair not in prices_by_pair:
                try:
                    p = self.exchange.get_current_price(pair)
                    if p > 0:
                        prices_by_pair[pair] = p
                        self.state.update_price(pair, p)
                except Exception:
                    pass

        # Sort holdings by value descending
        holdings.sort(key=lambda h: h["native_value"], reverse=True)

        return {
            "fetch_ts": fetch_ts,
            "native_currency": native,
            "currency_symbol": symbol,
            "total_portfolio": round(total_value, 2),
            "fiat_cash": round(fiat_cash, 2),
            # Legacy keys for backward compat
            "total_portfolio_usd": round(total_value, 2),
            "fiat_cash_usd": round(fiat_cash, 2),
            "holdings": holdings,
            "prices_by_pair": prices_by_pair,
            "tracked_pairs": self.pairs,
            "bot_pnl": self.state.total_pnl,
            "bot_trades": self.state.total_trades,
            "is_paused": self.state.is_paused,
            "circuit_breaker": self.state.circuit_breaker_triggered,
        }

    def _register_chat_functions(self) -> None:
        """Register all trading functions the LLM chat handler can call."""
        ch = self.chat_handler

        # ─── Read functions ────────────────────────────────────────────

        def _live_get_status(p):
            """Combine live Coinbase snapshot with agent state."""
            snap = self._live_coinbase_snapshot()
            return {
                # Currency metadata
                "native_currency": snap["native_currency"],
                "currency_symbol": snap["currency_symbol"],
                # Live Coinbase numbers (in native currency)
                "portfolio_value": snap["total_portfolio"],
                "fiat_cash": snap["fiat_cash"],
                "holdings": snap["holdings"],
                "tracked_pairs": snap["tracked_pairs"],
                "prices": snap["prices_by_pair"],
                "data_fetched_at": snap["fetch_ts"],
                # Agent state (bot activity)
                "bot_trades_executed": snap["bot_trades"],
                "bot_pnl": snap["bot_pnl"],
                "bot_open_positions": self.state.open_positions,
                "win_rate": self.state.win_rate,
                "max_drawdown": self.state.max_drawdown,
                "is_running": self.state.is_running,
                "is_paused": snap["is_paused"],
                "circuit_breaker": snap["circuit_breaker"],
            }

        ch.register_function("get_status", _live_get_status)

        def _live_get_positions(p):
            """Return actual Coinbase holdings, not just bot-tracked positions."""
            snap = self._live_coinbase_snapshot()
            crypto_holdings = [h for h in snap["holdings"] if not h["is_fiat"]]
            return {
                "data_source": "live_coinbase_api",
                "native_currency": snap["native_currency"],
                "currency_symbol": snap["currency_symbol"],
                "fetched_at": snap["fetch_ts"],
                "coinbase_holdings": crypto_holdings,
                "total_crypto_value": sum(h["native_value"] for h in crypto_holdings),
                # Also expose what the bot itself opened (may be subset/empty)
                "bot_tracked_positions": self.state.open_positions,
            }

        ch.register_function("get_positions", _live_get_positions)

        def _live_get_balance(p):
            """Return live Coinbase portfolio value, not stale agent state."""
            snap = self._live_coinbase_snapshot()
            fiat = [h for h in snap["holdings"] if h["is_fiat"]]
            crypto = [h for h in snap["holdings"] if not h["is_fiat"]]
            return {
                "data_source": "live_coinbase_api",
                "native_currency": snap["native_currency"],
                "currency_symbol": snap["currency_symbol"],
                "fetched_at": snap["fetch_ts"],
                "total_portfolio": snap["total_portfolio"],
                "fiat_cash": snap["fiat_cash"],
                "fiat_accounts": [{"currency": h["currency"], "amount": h["amount"]} for h in fiat],
                "crypto_positions_value": sum(h["native_value"] for h in crypto),
                "bot_pnl": snap["bot_pnl"],
            }

        ch.register_function("get_balance", _live_get_balance)

        def _live_get_prices(p):
            """Fetch fresh prices directly from Coinbase REST API."""
            prices = {}
            for pair in self.pairs:
                try:
                    price = self.exchange.get_current_price(pair)
                    if price > 0:
                        prices[pair] = price
                        self.state.update_price(pair, price)
                except Exception as e:
                    logger.debug(f"Price fetch failed for {pair}: {e}")
            return {
                "data_source": "live_coinbase_api",
                "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "prices": prices,
            }

        ch.register_function("get_current_prices", _live_get_prices)

        def _live_get_account_holdings(p):
            """Full raw account breakdown from Coinbase."""
            snap = self._live_coinbase_snapshot()
            return snap

        ch.register_function("get_account_holdings", _live_get_account_holdings)

        ch.register_function("get_recent_trades", lambda p: {
            "trades": [t.to_summary() for t in self.state.recent_trades]
        })


        ch.register_function("get_recent_signals", lambda p: {
            "signals": [
                {
                    "pair": s.pair,
                    "signal_type": s.signal_type,
                    "confidence": s.confidence,
                    "reasoning": s.reasoning[:200] if s.reasoning else "",
                }
                for s in self.state.recent_signals
            ]
        })

        ch.register_function("get_news_summary", lambda p: {
            "news": self.news.get_summary() if self.news else "News not configured."
        })

        ch.register_function("get_fear_greed", lambda p: {
            "fear_greed": self.fear_greed.get_current()
        })

        ch.register_function("get_trading_rules", lambda p: {
            **self.rules.get_all_rules(),
            **self.rules.get_status(),
        })

        ch.register_function("get_fee_info", lambda p: self.fee_manager.get_fee_summary())

        ch.register_function("get_pending_swaps", lambda p: {
            "pending_swaps": {
                sid: {
                    "sell": sp.sell_pair,
                    "buy": sp.buy_pair,
                    "quote_amount": sp.quote_amount,
                    "net_gain": f"+{sp.net_gain_pct*100:.2f}%",
                    "priority": sp.priority,
                }
                for sid, sp in self.rotator.pending_swaps.items()
            }
        })

        ch.register_function("get_highstakes_status", lambda p: {
            "status": self.high_stakes.get_status()
        })

        ch.register_function("get_rotation_analysis", lambda p: {
            "analysis": self._cmd_rotate({})
        })

        # ─── Action functions ──────────────────────────────────────────
        ch.register_function("enable_highstakes", lambda p: self._cmd_highstakes({
            "description": p.get("duration", "4h"),
            "user_id": "owner",
        }))

        ch.register_function("disable_highstakes", lambda p: self._cmd_highstakes({
            "description": "off",
            "user_id": "owner",
        }))

        ch.register_function("create_task", lambda p: self._cmd_task({
            "description": p.get("description", ""),
        }))

        ch.register_function("approve_item", lambda p: self._cmd_approve_trade({
            "trade_id": p.get("item_id", ""),
        }))

        ch.register_function("reject_item", lambda p: self._cmd_reject_trade({
            "trade_id": p.get("item_id", ""),
        }))

        ch.register_function("pause_trading", lambda p: self._cmd_pause({}))
        ch.register_function("resume_trading", lambda p: self._cmd_resume({}))
        ch.register_function("emergency_stop", lambda p: self._cmd_stop({}))

        # ─── Settings management (new) ─────────────────────────────────
        def _enable_trading(p: dict) -> dict:
            preset = p.get("preset", "moderate")
            ok, err, changes = sm.apply_preset(preset)
            if ok:
                sm.push_to_runtime(self.rules, self.config, changes)
                logger.warning(f"🟢 TRADING ENABLED via preset '{preset}' (Telegram)")
                return {"ok": True, "preset": preset, "changes": changes}
            return {"ok": False, "error": err}

        def _disable_trading(p: dict) -> dict:
            ok, err, changes = sm.apply_preset("disabled")
            if ok:
                sm.push_to_runtime(self.rules, self.config, changes)
                logger.warning("🔴 TRADING DISABLED (Telegram)")
                return {"ok": True, "preset": "disabled", "changes": changes}
            return {"ok": False, "error": err}

        def _apply_preset(p: dict) -> dict:
            preset = p.get("preset", "")
            if preset not in sm.PRESETS:
                return {"ok": False, "error": f"Unknown preset: {preset!r}. Available: {list(sm.PRESETS.keys())}"}
            ok, err, changes = sm.apply_preset(preset)
            if ok:
                sm.push_to_runtime(self.rules, self.config, changes)
                logger.warning(f"📋 PRESET '{preset}' applied (Telegram)")
                return {"ok": True, "preset": preset, "changes": changes}
            return {"ok": False, "error": err}

        def _update_settings(p: dict) -> dict:
            section = p.get("section", "")
            param = p.get("param", "")
            value = p.get("value", "")
            if not section or not param:
                return {"ok": False, "error": "section and param are required"}
            if not sm.is_telegram_allowed(section):
                return {"ok": False, "error": f"Section '{section}' is blocked for Telegram updates. Use the Dashboard instead."}
            ok, err, applied = sm.update_section(section, {param: value})
            if ok:
                sm.push_section_to_runtime(section, {param: applied[param]}, self.rules, self.config)
                logger.warning(f"🔧 SETTINGS UPDATE via Telegram | {section}.{param} → {applied[param]}")
                return {"ok": True, "section": section, "param": param, "new": applied[param]}
            return {"ok": False, "error": err}

        def _get_settings_tiers(_p: dict) -> dict:
            return sm.TELEGRAM_SAFETY_TIERS

        ch.register_function("enable_trading", _enable_trading)
        ch.register_function("disable_trading", _disable_trading)
        ch.register_function("apply_preset", _apply_preset)
        ch.register_function("update_settings", _update_settings)
        ch.register_function("get_settings_tiers", _get_settings_tiers)

        # ─── Stats & Analytics functions ───────────────────────────────
        ch.register_function("get_stats", lambda p: self.stats_db.get_performance_summary(
            hours=int(p.get("hours", 24))
        ))

        ch.register_function("get_trade_history", lambda p: {
            "trades": self.stats_db.get_trades(
                hours=int(p.get("hours", 24)),
                pair=p.get("pair"),
            )
        })

        ch.register_function("get_pair_stats", lambda p: self.stats_db.get_pair_stats(
            pair=p.get("pair", "BTC-USD"),
            hours=int(p.get("hours", 168)),
        ))

        ch.register_function("get_daily_summaries", lambda p: {
            "summaries": self.stats_db.get_daily_summaries(days=int(p.get("days", 7)))
        })

        ch.register_function("get_best_worst", lambda p: self.stats_db.get_best_worst_trades(
            hours=int(p.get("hours", 168))
        ))

        ch.register_function("schedule_report", lambda p: {
            "id": self.stats_db.add_scheduled_report(
                name=p.get("name", "Custom Report"),
                description=p.get("description", ""),
                cron_expression=p.get("interval", "1h"),
                query_type=p.get("query_type", "performance"),
                query_params=p,
            ),
            "status": "scheduled",
        })

        ch.register_function("get_schedules", lambda p: {
            "schedules": self.stats_db.get_active_schedules()
        })

        ch.register_function("delete_schedule", lambda p: {
            "deleted": self.stats_db.delete_schedule(int(p.get("id", 0)))
        })

        # ─── Config / settings read ────────────────────────────────────
        ch.register_function("get_config", lambda p: {
            "absolute_rules": self.rules.get_all_rules(),
            "trading": {
                "mode": self.config.get("trading", {}).get("mode", "paper"),
                "pairs": list(self.pairs),
                "interval_seconds": self.interval,
                "min_confidence": self.config.get("trading", {}).get("min_confidence", 1.0),
                "max_open_positions": self.config.get("trading", {}).get("max_open_positions", 3),
            },
            "risk": dict(self.config.get("risk", {})),
            "fees": dict(self.config.get("fees", {})),
            "high_stakes": self.high_stakes.get_status(),
            "rotation": dict(self.config.get("rotation", {})),
        })

        # ─── Config / settings write ───────────────────────────────────
        def _update_rule(p: dict) -> dict:
            result = self.rules.update_param(
                param=p.get("param", ""),
                value=str(p.get("value", "")),
            )
            # Persist to settings.yaml
            if result.get("ok"):
                try:
                    sm.update_section("absolute_rules", {p["param"]: result["new"]})
                except Exception as e:
                    logger.error(f"Failed to persist rule update to disk: {e}")
            return result

        ch.register_function("update_rule", _update_rule)

        def _update_trading_param(p: dict) -> dict:
            param = p.get("param", "")
            value_str = str(p.get("value", ""))
            trading_cfg = self.config.setdefault("trading", {})
            _float_params = {"min_confidence", "paper_slippage_pct"}
            _int_params = {"max_open_positions", "interval"}
            try:
                if param in _float_params:
                    new_val: Any = float(value_str)
                elif param in _int_params:
                    new_val = int(float(value_str))
                else:
                    return {"ok": False, "error": f"Unknown trading param: {param!r}"}
            except (ValueError, TypeError) as e:
                return {"ok": False, "error": str(e)}
            old_val = trading_cfg.get(param)
            trading_cfg[param] = new_val
            if param == "interval":
                self.interval = new_val
            logger.warning(f"🔧 TRADING PARAM UPDATED (runtime) | {param}: {old_val!r} → {new_val!r}")
            # Persist to settings.yaml
            try:
                sm.update_section("trading", {param: new_val})
            except Exception as e:
                logger.error(f"Failed to persist trading param to disk: {e}")
            return {"ok": True, "param": param, "old": old_val, "new": new_val}

        ch.register_function("update_trading_param", _update_trading_param)

        def _update_risk_param(p: dict) -> dict:
            param = p.get("param", "")
            value_str = str(p.get("value", ""))
            risk_cfg = self.config.setdefault("risk", {})
            _risk_params = {
                "stop_loss_pct", "take_profit_pct", "trailing_stop_pct",
                "max_position_pct", "max_total_exposure_pct", "max_drawdown_pct",
            }
            _risk_int_params = {"max_trades_per_hour", "loss_cooldown_seconds"}
            try:
                if param in _risk_params:
                    new_val: Any = float(value_str)
                elif param in _risk_int_params:
                    new_val = int(float(value_str))
                else:
                    return {"ok": False, "error": f"Unknown risk param: {param!r}"}
            except (ValueError, TypeError) as e:
                return {"ok": False, "error": str(e)}
            old_val = risk_cfg.get(param)
            risk_cfg[param] = new_val
            # Propagate trailing_stop_pct to the TrailingStopManager
            if param == "trailing_stop_pct":
                self.trailing_stops.default_trail_pct = new_val
            logger.warning(f"🔧 RISK PARAM UPDATED (runtime) | {param}: {old_val!r} → {new_val!r}")
            # Persist to settings.yaml
            try:
                sm.update_section("risk", {param: new_val})
            except Exception as e:
                logger.error(f"Failed to persist risk param to disk: {e}")
            return {"ok": True, "param": param, "old": old_val, "new": new_val}

        ch.register_function("update_risk_param", _update_risk_param)

        # ─── Pair management ───────────────────────────────────────────
        def _add_pair(p: dict) -> dict:
            pair = str(p.get("pair", "")).upper().strip()
            if not pair:
                return {"ok": False, "error": "pair is required"}
            if pair not in self.pairs:
                self.pairs.append(pair)
                self.config.setdefault("trading", {}).setdefault("pairs", []).append(pair)
                logger.info(f"📌 Pair added (runtime): {pair}. Active pairs: {self.pairs}")
            return {"ok": True, "pair": pair, "all_pairs": list(self.pairs)}

        ch.register_function("add_pair", _add_pair)

        def _remove_pair(p: dict) -> dict:
            pair = str(p.get("pair", "")).upper().strip()
            if pair in self.pairs:
                self.pairs.remove(pair)
                cfg_pairs = self.config.get("trading", {}).get("pairs", [])
                if pair in cfg_pairs:
                    cfg_pairs.remove(pair)
                logger.info(f"🗑️ Pair removed (runtime): {pair}. Active pairs: {self.pairs}")
            return {"ok": True, "pair": pair, "all_pairs": list(self.pairs)}

        ch.register_function("remove_pair", _remove_pair)

        ch.register_function("blacklist_pair", lambda p: self.rules.add_never_trade_pair(
            str(p.get("pair", ""))
        ))

        ch.register_function("unblacklist_pair", lambda p: self.rules.remove_never_trade_pair(
            str(p.get("pair", ""))
        ))

        # ─── Trailing stops ────────────────────────────────────────────
        ch.register_function("get_trailing_stops", lambda p: {
            "trailing_stops": self.trailing_stops.get_all_stops()  # already serialized dicts
        })

        # ─── Pending swap management ───────────────────────────────────
        def _cancel_swap(p: dict) -> dict:
            swap_id = str(p.get("swap_id", ""))
            if swap_id in self.rotator.pending_swaps:
                del self.rotator.pending_swaps[swap_id]
                return {"ok": True, "cancelled": swap_id}
            return {"ok": False, "error": f"Swap {swap_id!r} not found"}

        ch.register_function("cancel_swap", _cancel_swap)

        # ─── Simulated Trades ──────────────────────────────────────────────────
        def _simulate_trade(p: dict) -> dict:
            pair = str(p.get("pair", "")).upper().strip()
            from_currency = str(p.get("from_currency", "")).upper().strip()
            notes = str(p.get("notes", ""))
            try:
                from_amount = float(p.get("from_amount", 0))
            except (TypeError, ValueError):
                return {"ok": False, "error": "from_amount must be a number"}

            if not pair or not from_currency or from_amount <= 0:
                return {"ok": False, "error": "pair, from_currency, and from_amount are required"}

            # Derive to_currency from pair
            parts = pair.split("-")
            if len(parts) != 2:
                return {"ok": False, "error": f"Invalid pair format: {pair!r}"}
            base, quote = parts
            to_currency = base if from_currency == quote else quote

            # Get live entry price
            try:
                entry_price = self.exchange.get_current_price(pair)
            except Exception as e:
                return {"ok": False, "error": f"Cannot fetch price for {pair}: {e}"}

            if entry_price <= 0:
                return {"ok": False, "error": f"No live price available for {pair}"}

            quantity = from_amount / entry_price if from_currency == quote else from_amount * entry_price

            sim_id = self.stats_db.record_simulated_trade(
                pair=pair,
                from_currency=from_currency,
                from_amount=from_amount,
                entry_price=entry_price,
                quantity=quantity,
                to_currency=to_currency,
                notes=notes,
            )
            return {
                "ok": True,
                "id": sim_id,
                "pair": pair,
                "from_currency": from_currency,
                "to_currency": to_currency,
                "from_amount": from_amount,
                "entry_price": entry_price,
                "quantity": round(quantity, 8),
                "notes": notes,
            }

        ch.register_function("simulate_trade", _simulate_trade)

        def _list_simulations(p: dict) -> dict:
            include_closed = bool(p.get("include_closed", False))
            rows = self.stats_db.get_simulated_trades(include_closed=include_closed)
            # Enrich open rows with live PnL
            for row in rows:
                if row["status"] == "open":
                    try:
                        current_price = self.exchange.get_current_price(row["pair"])
                    except Exception:
                        current_price = row["entry_price"]
                    if current_price > 0:
                        pnl_abs = (current_price - row["entry_price"]) * row["quantity"]
                        pnl_pct = ((current_price / row["entry_price"]) - 1) * 100 if row["entry_price"] > 0 else 0.0
                    else:
                        pnl_abs = 0.0
                        pnl_pct = 0.0
                    row["current_price"] = current_price
                    row["pnl_abs"] = round(pnl_abs, 4)
                    row["pnl_pct"] = round(pnl_pct, 2)
            return {"simulations": rows, "count": len(rows)}

        ch.register_function("list_simulations", _list_simulations)

        def _close_simulation(p: dict) -> dict:
            try:
                sim_id = int(p.get("sim_id", 0))
            except (TypeError, ValueError):
                return {"ok": False, "error": "sim_id must be an integer"}
            # Fetch current price for this sim
            rows = self.stats_db.get_simulated_trades(include_closed=False)
            target = next((r for r in rows if r["id"] == sim_id), None)
            if not target:
                return {"ok": False, "error": f"No open simulation with id={sim_id}"}
            try:
                close_price = self.exchange.get_current_price(target["pair"])
            except Exception:
                close_price = target["entry_price"]
            if close_price <= 0:
                close_price = target["entry_price"]
            result = self.stats_db.close_simulated_trade(sim_id=sim_id, close_price=close_price)
            if not result:
                return {"ok": False, "error": f"Failed to close simulation {sim_id}"}
            return {"ok": True, **result}

        ch.register_function("close_simulation", _close_simulation)

        logger.info(f"🧠 Registered {len(ch._function_handlers)} chat functions ({len(ch._tool_defs)} with schemas)")


    # =========================================================================
    # Proactive Updates
    # =========================================================================

    def _send_proactive_update(self) -> None:
        """Generate and send LLM-powered proactive updates via Telegram."""
        if not self.telegram or not self.chat_handler:
            return

        try:
            context = self._get_trading_context()
            update = self.chat_handler.generate_proactive_update(context)
            if update:
                self.telegram.send_message(update)
        except Exception as e:
            logger.debug(f"Proactive update skipped: {e}")

    def _get_trading_context(self) -> dict:
        """Assemble trading context for the LLM."""
        s = self.state
        sym = s.currency_symbol
        return {
            "portfolio_value": format_currency(s.portfolio_value, sym),
            "cash_balance": format_currency(s.cash_balance, sym),
            "return_pct": format_percentage(s.return_pct),
            "max_drawdown": format_percentage(s.max_drawdown),
            "total_trades": s.total_trades,
            "win_rate": format_percentage(s.win_rate),
            "total_pnl": format_currency(s.total_pnl, sym),
            "open_positions": {
                pair: {
                    "qty": qty,
                    "price": format_currency(s.current_prices.get(pair, 0), sym),
                }
                for pair, qty in s.open_positions.items()
            },
            "current_prices": {
                pair: format_currency(price, sym)
                for pair, price in s.current_prices.items()
            },
            "is_paused": s.is_paused,
            "circuit_breaker": s.circuit_breaker_triggered,
            "fear_greed": self.fear_greed.get_current(),
            "high_stakes_active": self.high_stakes.is_active,
            "pending_swaps": len(self.rotator.pending_swaps),
            "trailing_stops": self.trailing_stops.get_active_count(),
            # Raw numeric data (for proactive engine calculations + stats DB)
            "raw_prices": dict(s.current_prices),
            "raw_positions": dict(s.open_positions),
            "raw_portfolio_value": s.portfolio_value,
            "raw_cash_balance": s.cash_balance,
            "raw_return_pct": s.return_pct,
            "raw_total_pnl": s.total_pnl,
            "raw_max_drawdown": s.max_drawdown,
            "currency_symbol": s.currency_symbol,
        }

    # =========================================================================
    # Telegram Command Handling (legacy fallback)
    # =========================================================================

    def _handle_telegram_command(self, command: str, data: dict) -> str:
        """Handle commands from Telegram."""
        handlers = {
            "status": self._cmd_status,
            "task": self._cmd_task,
            "rules": self._cmd_rules,
            "positions": self._cmd_positions,
            "trades": self._cmd_trades,
            "news": self._cmd_news,
            "balance": self._cmd_balance,
            "pause": self._cmd_pause,
            "resume": self._cmd_resume,
            "stop": self._cmd_stop,
            "approve_trade": self._cmd_approve_trade,
            "reject_trade": self._cmd_reject_trade,
            "highstakes": self._cmd_highstakes,
            "fees": self._cmd_fees,
            "swaps": self._cmd_swaps,
            "rotate": self._cmd_rotate,
            "message": self._cmd_message,
        }

        handler = handlers.get(command)
        if handler:
            return handler(data)
        return f"Unknown command: {command}"

    def _cmd_status(self, data: dict) -> str:
        s = self.state
        return (
            f"📊 *Portfolio Status*\n\n"
            f"💰 Value: {format_currency(s.portfolio_value)}\n"
            f"💵 Cash: {format_currency(s.cash_balance)}\n"
            f"📈 Return: {format_percentage(s.return_pct)}\n"
            f"📉 Max Drawdown: {format_percentage(s.max_drawdown)}\n"
            f"🔄 Total Trades: {s.total_trades}\n"
            f"✅ Win Rate: {format_percentage(s.win_rate)}\n"
            f"💰 Total PnL: {format_currency(s.total_pnl)}\n"
            f"📊 Open Positions: {len(s.open_positions)}\n"
            f"{'⏸️ PAUSED' if s.is_paused else '▶️ Running'}\n"
            f"{'🛑 CIRCUIT BREAKER' if s.circuit_breaker_triggered else ''}"
        )

    def _cmd_task(self, data: dict) -> str:
        description = sanitize_input(data.get("description", ""), max_length=300)
        if not description:
            return "Please provide a task description."

        # Try to parse spending limit from the task
        max_spend = None
        spend_match = re.search(r"\$(\d+(?:\.\d+)?)", description)
        if spend_match:
            max_spend = float(spend_match.group(1))

        # Try to identify pair
        pair = None
        for p in self.pairs:
            base = p.split("-")[0]
            if base.lower() in description.lower():
                pair = p
                break

        task = Task(description=description, max_spend=max_spend, pair=pair)
        # Evict completed/stale tasks; cap list at 20 to prevent unbounded growth
        self.active_tasks = [t for t in self.active_tasks if not t.completed][-20:]
        self.active_tasks.append(task)

        return (
            f"📝 *Task Created*\n\n"
            f"ID: `{task.id}`\n"
            f"Description: {description}\n"
            f"Max Spend: {format_currency(max_spend) if max_spend else 'Not set'}\n"
            f"Pair: {pair or 'Any'}"
        )

    def _cmd_rules(self, data: dict) -> str:
        rules_text = self.rules.get_rules_text()
        status = self.rules.get_status()
        return (
            f"{rules_text}\n"
            f"📊 *Today's Usage*\n"
            f"• Spent: {format_currency(status['daily_spend'])} / {format_currency(status['daily_spend_remaining'])} remaining\n"
            f"• Losses: {format_currency(status['daily_loss'])} / {format_currency(status['daily_loss_remaining'])} remaining\n"
            f"• Trades: {status['trades_today']} / {status['trades_remaining']} remaining"
        )

    def _cmd_positions(self, data: dict) -> str:
        positions = self.state.open_positions
        if not positions:
            return "📊 No open positions."

        lines = ["📊 *Open Positions*\n"]
        for pair, qty in positions.items():
            price = self.state.current_prices.get(pair, 0)
            value = qty * price
            lines.append(
                f"• {pair}: {qty:.6f} ({format_currency(value)})"
            )
        return "\n".join(lines)

    def _cmd_trades(self, data: dict) -> str:
        trades = self.state.recent_trades
        if not trades:
            return "📊 No recent trades."

        lines = ["📊 *Recent Trades*\n"]
        for trade in trades[-10:]:
            lines.append(trade.to_summary())
        return "\n".join(lines)

    def _cmd_news(self, data: dict) -> str:
        if self.news:
            headlines = self.news.get_headlines(10)
            return f"📰 *Latest Crypto News*\n\n{headlines}"

        if self.redis:
            try:
                cached = self.redis.get("news:latest")
                if cached:
                    articles = json.loads(cached)
                    lines = [f"- {a.get('title', '')}" for a in articles[:10]]
                    return "📰 *Latest News*\n\n" + "\n".join(lines)
            except Exception:
                pass
        return "📰 No news available."

    def _cmd_balance(self, data: dict) -> str:
        balance = self.exchange.balance
        lines = ["💰 *Account Balance*\n"]
        for currency, amount in balance.items():
            lines.append(f"• {currency}: {amount:,.6f}")
        return "\n".join(lines)

    def _cmd_pause(self, data: dict) -> str:
        self.state.is_paused = True
        return "⏸️ Trading paused."

    def _cmd_resume(self, data: dict) -> str:
        user_id = data.get("user_id", "telegram")
        was_circuit_breaker = self.state.circuit_breaker_triggered
        self.state.is_paused = False
        self.state.circuit_breaker_triggered = False
        self.audit.log_auth(user_id, authorized=True, command="resume_trading")
        if was_circuit_breaker:
            alert = f"⚠️ Circuit breaker manually reset and trading resumed by {user_id}."
            logger.warning(alert)
            if self.telegram:
                self.telegram.send_alert(alert)
        return "▶️ Trading resumed."

    def _cmd_stop(self, data: dict) -> str:
        self.state.is_running = False
        self.state.is_paused = True
        return "🛑 Emergency stop activated."

    def _cmd_approve_trade(self, data: dict) -> str:
        trade_id = data.get("trade_id", "")
        with self._pending_approvals_lock:
            approved = self._pending_approvals.pop(trade_id, None)
        if approved is not None:
            # Clear needs_approval so the executor does not short-circuit again
            approved = {**approved, "needs_approval": False}
            result = self.executor.execute({"approved_trade": approved})
            if result.get("executed"):
                return "✅ Trade executed successfully!"
            return f"❌ Execution failed: {result.get('error', 'Unknown')}"
        return "Trade not found or already processed."

    def _cmd_reject_trade(self, data: dict) -> str:
        trade_id = data.get("trade_id", "")
        with self._pending_approvals_lock:
            removed = self._pending_approvals.pop(trade_id, None)
        if removed is not None:
            return "Trade rejected."
        return "Trade not found or already processed."

    def _cmd_message(self, data: dict) -> str:
        """Legacy fallback — only used if chat handler is not connected."""
        text = sanitize_input(data.get("text", ""), max_length=500)
        if not text:
            return "Empty message received."
        return f"📝 Noted: {text}\nI'll factor this into my decisions."

    # =========================================================================
    # Universe Scanning & LLM Screener (Funnel System)
    # =========================================================================

    def _refresh_pair_universe(self) -> None:
        """Stage 1: Refresh full product universe from Coinbase (cached)."""
        now = time.time()
        if self._pair_universe and (now - self._pair_universe_ts) < self._PAIR_UNIVERSE_TTL:
            return  # cache still fresh

        try:
            never_trade = self.config.get("trading", {}).get("never_trade", [])
            only_trade = self.config.get("trading", {}).get("only_trade", [])
            products = self.exchange.discover_all_pairs_detailed(
                quote_currencies=None,  # use default
                never_trade=never_trade,
                only_trade=only_trade if only_trade else None,
                include_crypto_quotes=self._include_crypto_quotes,
            )
            old_ids = {p["product_id"] for p in self._pair_universe}
            new_ids = {p["product_id"] for p in products}
            added = new_ids - old_ids
            if added and self._pair_universe:  # skip first load
                logger.info(f"🌍 Universe refresh: {len(added)} new listings: {sorted(added)[:10]}")
            self._pair_universe = products
            self._pair_universe_ts = now
            logger.debug(f"Universe: {len(products)} tradeable products")
        except Exception as e:
            logger.warning(f"Universe refresh failed: {e}")

    def _run_universe_scan(self) -> None:
        """Stage 2: Technical screen — pure math, zero LLM calls.

        Filters universe by volume/movement thresholds, fetches candles,
        runs TechnicalAnalyzer + strategies, computes composite score.
        """
        if not self._pair_universe:
            return

        # Pre-filter by 24h volume and price movement
        candidates = []
        for p in self._pair_universe:
            vol = float(p.get("volume_24h", 0) or 0)
            pct = abs(float(p.get("price_percentage_change_24h", 0) or 0))
            if vol >= self._scan_volume_threshold and pct >= self._scan_movement_threshold_pct:
                candidates.append(p)

        if not candidates:
            logger.debug("Universe scan: no candidates passed volume/movement filter")
            return

        # Sort by volume descending, cap at 50 to limit API calls
        candidates.sort(key=lambda p: float(p.get("volume_24h", 0) or 0), reverse=True)
        candidates = candidates[:50]

        analyzer = TechnicalAnalyzer(
            self.config.get("analysis", {}).get("technical", {})
        )
        ema_strategy = EMACrossoverStrategy(self.config)
        bb_strategy = BollingerReversionStrategy(self.config)

        scan_results: dict[str, dict] = {}
        rate_limiter = get_rate_limiter()

        for product in candidates:
            pair = product["product_id"]
            try:
                rate_limiter.wait("coinbase_rest")
                candles = self.exchange.get_candles(pair, granularity="ONE_HOUR", limit=200)
                if not candles or len(candles) < 30:
                    continue

                analysis = analyzer.analyze(candles)
                if "error" in analysis:
                    continue

                # Run strategy signals (pure math)
                ema_sig = ema_strategy.generate_signal(pair, candles, analysis)
                bb_sig = bb_strategy.generate_signal(pair, candles, analysis)

                # Composite score: combine indicators
                indicators = analysis.get("indicators", {})
                rsi = indicators.get("rsi")
                adx = indicators.get("adx")
                volume_ratio = indicators.get("volume_ratio", 1.0)
                macd_hist = indicators.get("macd_histogram")

                score = 0.0
                # RSI momentum (not overbought, not oversold — sweet spot 30-65 for buys)
                if rsi is not None:
                    if 30 <= rsi <= 45:
                        score += 0.25  # oversold bounce potential
                    elif 45 < rsi <= 65:
                        score += 0.15  # healthy momentum
                    elif rsi > 80:
                        score -= 0.2  # overbought

                # ADX trend strength
                if adx is not None and adx > 25:
                    score += 0.2

                # Volume confirmation
                if volume_ratio > 1.5:
                    score += 0.15
                elif volume_ratio > 1.2:
                    score += 0.1

                # MACD histogram positive
                if macd_hist is not None and macd_hist > 0:
                    score += 0.1

                # Strategy confidence boost
                for sig in [ema_sig, bb_sig]:
                    if sig.action == "buy" and sig.confidence > 0.5:
                        score += 0.2 * sig.confidence

                # Movement bonus (higher absolute % change = more opportunity)
                pct_change = abs(float(product.get("price_percentage_change_24h", 0) or 0))
                score += min(pct_change / 20.0, 0.15)  # cap at 15%

                scan_results[pair] = {
                    "product": product,
                    "current_price": analysis.get("current_price"),
                    "rsi": rsi,
                    "adx": adx,
                    "volume_ratio": volume_ratio,
                    "macd_histogram": macd_hist,
                    "ema_signal": ema_sig.action,
                    "ema_confidence": ema_sig.confidence,
                    "bb_signal": bb_sig.action,
                    "bb_confidence": bb_sig.confidence,
                    "composite_score": round(score, 3),
                    "price_change_24h_pct": float(product.get("price_percentage_change_24h", 0) or 0),
                    "volume_24h": float(product.get("volume_24h", 0) or 0),
                }
            except Exception as e:
                logger.debug(f"Scan skip {pair}: {e}")
                continue

        self._scan_results = scan_results

        # Persist to StatsDB
        if scan_results:
            top_movers = sorted(
                scan_results.items(),
                key=lambda kv: kv[1]["composite_score"],
                reverse=True,
            )[:10]
            top_movers_str = ", ".join(
                f"{p}={d['composite_score']}" for p, d in top_movers
            )
            try:
                self.stats_db.save_scan_results(
                    universe_size=len(self._pair_universe),
                    scanned_pairs=len(scan_results),
                    results_json=scan_results,
                    top_movers=top_movers_str,
                    summary_text=self._get_scan_summary(),
                )
            except Exception as e:
                logger.debug(f"Failed to persist scan results: {e}")

            logger.info(
                f"📊 Universe scan: {len(candidates)} candidates → "
                f"{len(scan_results)} scored | top: {top_movers_str[:120]}"
            )

    def _run_llm_screener(self) -> None:
        """Stage 3: Single LLM call to pick top-N active pairs from scan results.

        Uses ONE compact prompt with a summary table — not per-pair analysis.
        """
        if not self._scan_results:
            return

        # Build top candidates sorted by composite score
        ranked = sorted(
            self._scan_results.items(),
            key=lambda kv: kv[1]["composite_score"],
            reverse=True,
        )[:20]  # top 20 for LLM consideration

        if not ranked:
            return

        # Build compact table for LLM
        table_lines = ["Pair | Price | RSI | ADX | Vol24h | MACDh | EMA | BB | Score | Chg24h%"]
        table_lines.append("-" * 90)
        for pair, d in ranked:
            table_lines.append(
                f"{pair} | {d.get('current_price', '?'):.6g} | "
                f"{d.get('rsi', '?'):.1f} | "
                f"{d.get('adx', '?'):.1f} | "
                f"{d.get('volume_24h', 0):.0f} | "
                f"{d.get('macd_histogram', '?'):.4f} | "
                f"{d.get('ema_signal', '?')}({d.get('ema_confidence', 0):.2f}) | "
                f"{d.get('bb_signal', '?')}({d.get('bb_confidence', 0):.2f}) | "
                f"{d.get('composite_score', 0):.3f} | "
                f"{d.get('price_change_24h_pct', 0):+.2f}%"
            )

        table_str = "\n".join(table_lines)

        # Currently held positions (must keep awareness)
        held_pairs = list(self.state.open_positions.keys())
        held_note = f"Currently holding positions in: {', '.join(held_pairs)}" if held_pairs else "No open positions."

        prompt = (
            f"You are a crypto pair screener. Pick the best {self._max_active_pairs} "
            f"pairs to actively trade from the scan results below.\n\n"
            f"SCAN RESULTS (sorted by composite score):\n{table_str}\n\n"
            f"{held_note}\n\n"
            f"RULES:\n"
            f"- Pick {self._max_active_pairs} pairs total (can include held pairs if still strong)\n"
            f"- Prioritize: high composite score, buy signals, strong momentum, adequate volume\n"
            f"- Avoid: overbought (RSI>80), low volume, sell signals unless reversal expected\n"
            f"- If a held pair is weakening, it's OK to drop it (rotation will handle exit)\n\n"
            f"Reply with ONLY a JSON array of pair names, e.g. [\"BTC-USD\",\"ETH-USD\",\"SOL-USD\"]\n"
            f"No explanation needed."
        )

        try:
            response = self.llm.generate(
                prompt=prompt,
                system_prompt="You are a systematic crypto screener. Output ONLY valid JSON.",
                temperature=0.2,
                max_tokens=200,
            )

            # Parse JSON array from response
            text = response.strip()
            # Extract JSON array from possible markdown wrapping
            import re as _re
            json_match = _re.search(r'\[.*?\]', text, _re.DOTALL)
            if json_match:
                selected = json.loads(json_match.group())
                if isinstance(selected, list) and all(isinstance(s, str) for s in selected):
                    # Validate pairs exist in scan results
                    valid = [p for p in selected if p in self._scan_results]
                    if valid:
                        old_pairs = set(self._screener_active_pairs)
                        self._screener_active_pairs = valid[:self._max_active_pairs]
                        new_pairs = set(self._screener_active_pairs)

                        if old_pairs != new_pairs:
                            added = new_pairs - old_pairs
                            removed = old_pairs - new_pairs
                            changes = []
                            if added:
                                changes.append(f"+{','.join(added)}")
                            if removed:
                                changes.append(f"-{','.join(removed)}")
                            logger.info(
                                f"🎯 LLM Screener selected {len(valid)} pairs: "
                                f"{valid} | changes: {' '.join(changes)}"
                            )

                            # Update WebSocket subscriptions
                            try:
                                if self.ws_feed:
                                    self.ws_feed.update_subscriptions(valid)
                            except Exception as ws_err:
                                logger.debug(f"WS subscription update failed: {ws_err}")
                        return

            logger.warning(f"LLM screener returned unparseable response: {text[:200]}")
        except Exception as e:
            logger.warning(f"LLM screener failed (non-fatal): {e}")

    def _get_scan_summary(self) -> str:
        """Build human-readable summary of latest scan results for injection."""
        if not self._scan_results:
            return "No scan results available yet."

        ranked = sorted(
            self._scan_results.items(),
            key=lambda kv: kv[1]["composite_score"],
            reverse=True,
        )

        lines = [f"Universe: {len(self._pair_universe)} products | Scanned: {len(self._scan_results)}"]
        if self._screener_active_pairs:
            lines.append(f"Active (LLM-selected): {', '.join(self._screener_active_pairs)}")

        lines.append("Top 10 by composite score:")
        for pair, d in ranked[:10]:
            lines.append(
                f"  {pair}: score={d['composite_score']:.3f} "
                f"RSI={d.get('rsi', '?'):.1f} ADX={d.get('adx', '?'):.1f} "
                f"EMA={d.get('ema_signal', '?')} BB={d.get('bb_signal', '?')} "
                f"vol24h={d.get('volume_24h', 0):.0f} chg={d.get('price_change_24h_pct', 0):+.2f}%"
            )
        return "\n".join(lines)

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

            proposals = asyncio.run(self.rotator.evaluate_rotation(
                held_pairs=held_pairs,
                all_pairs=all_candidate_pairs,
                current_prices=self.state.current_prices,
                portfolio_value=self.state.portfolio_value,
                scan_results=self._scan_results,
            ))

            if not proposals:
                return

            for proposal in proposals:
                if proposal.priority == "autonomous":
                    # Execute autonomously (within allocation, fee-positive)
                    logger.info(
                        f"🔄 Auto-swap: {proposal.sell_pair} → {proposal.buy_pair} "
                        f"({format_currency(proposal.quote_amount)}, net +{proposal.net_gain_pct*100:.2f}%)"
                    )
                    result = self.rotator.execute_swap(
                        proposal,
                        portfolio_value=self.state.portfolio_value,
                        cash_balance=self.state.cash_balance,
                    )
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
                            f"Amount: {format_currency(proposal.quote_amount)}\n"
                            f"Expected net gain: +{proposal.net_gain_pct*100:.2f}%\n"
                            f"Fees: {format_currency(proposal.fee_estimate.total_fee_quote)}\n"
                            f"Confidence: {format_percentage(proposal.confidence)}"
                            f"{route_info}"
                        )

                elif proposal.priority in ("high_impact", "critical"):
                    # Escalate to owner via Telegram
                    swap_id = f"swap_{uuid.uuid4().hex[:8]}_{proposal.buy_pair}"
                    self.rotator.pending_swaps[swap_id] = proposal
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

    # =========================================================================
    # New Telegram Commands
    # =========================================================================

    def _cmd_highstakes(self, data: dict) -> str:
        """
        /highstakes 4h     — Enable high-stakes mode for 4 hours
        /highstakes 2d     — Enable for 2 days
        /highstakes off    — Disable immediately
        /highstakes status — Show current status
        """
        arg = data.get("description", "").strip().lower()
        user_id = data.get("user_id", "unknown")

        if not arg or arg == "status":
            return self.high_stakes.get_status()

        if arg == "off":
            self.audit.log_auth(user_id, authorized=True, command="highstakes_off")
            return self.high_stakes.deactivate(deactivated_by=user_id)

        # Activate with duration
        self.audit.log_auth(user_id, authorized=True, command=f"highstakes_{arg}")
        success, msg = self.high_stakes.activate(
            duration_str=arg,
            activated_by=user_id,
        )

        if success and self.telegram:
            # Also notify as an alert
            self.telegram.send_alert(
                f"⚡ High-stakes mode activated by {user_id} for {arg}"
            )

        return msg

    def _cmd_fees(self, data: dict) -> str:
        """Show current fee configuration and breakeven analysis."""
        return self.fee_manager.get_fee_summary()

    def _cmd_swaps(self, data: dict) -> str:
        """Show pending swap proposals."""
        pending = self.rotator.pending_swaps
        if not pending:
            return "🔄 No pending swap proposals."

        lines = ["🔄 *Pending Swaps*\n"]
        for swap_id, proposal in pending.items():
            lines.append(
                f"• `{swap_id}`\n"
                f"  {proposal.sell_pair} → {proposal.buy_pair}\n"
                f"  {format_currency(proposal.quote_amount)} | "
                f"net +{proposal.net_gain_pct*100:.2f}%\n"
            )
        lines.append("\nReply /approve <id> or /reject <id>")
        return "\n".join(lines)

    def _cmd_rotate(self, data: dict) -> str:
        """Force a rotation check immediately."""
        held_pairs = list(self.state.open_positions.keys())
        if not held_pairs:
            return "🔄 No open positions to rotate."

        proposals = asyncio.run(self.rotator.evaluate_rotation(
            held_pairs=held_pairs,
            all_pairs=self.pairs,
            current_prices=self.state.current_prices,
            portfolio_value=self.state.portfolio_value,
        ))

        return self.rotator.get_rotation_summary(proposals)
