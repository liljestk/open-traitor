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
from src.core.coinbase_client import CoinbaseClient
from src.core.llm_client import LLMClient
from src.core.rules import AbsoluteRules
from src.core.state import TradingState
from src.core.ws_feed import CoinbaseWebSocketFeed
from src.core.trailing_stop import TrailingStopManager
from src.core.health import update_health, check_component_health, start_health_server
from src.core.fee_manager import FeeManager
from src.core.high_stakes import HighStakesManager
from src.core.portfolio_rotator import PortfolioRotator
from src.analysis.fear_greed import FearGreedIndex
from src.analysis.multi_timeframe import MultiTimeframeAnalyzer
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
        coinbase: CoinbaseClient,
        llm: LLMClient,
        rules: AbsoluteRules,
        news_aggregator: Optional[NewsAggregator] = None,
        telegram_bot=None,
        redis_client=None,
        ws_feed: Optional[CoinbaseWebSocketFeed] = None,
    ):
        self.config = config
        self.coinbase = coinbase
        self.llm = llm
        self.rules = rules
        self.news = news_aggregator
        self.telegram = telegram_bot
        self.redis = redis_client
        self.ws_feed = ws_feed
        self.rate_limiter = get_rate_limiter()

        # Trading state
        if coinbase.paper_mode:
            initial_balance = 10000.0
        else:
            try:
                initial_balance = coinbase.get_portfolio_value()
            except Exception as _e:
                logger.warning(f"⚠️ Could not fetch live portfolio value on startup: {_e} — defaulting to $0")
                initial_balance = 0.0
        self.state = TradingState(initial_balance=initial_balance)
        self.state.is_running = True

        # Trading pairs
        self.pairs = config.get("trading", {}).get("pairs", ["BTC-USD"])
        self.interval = config.get("trading", {}).get("interval", 120)

        # Initialize agents
        self.market_analyst = MarketAnalystAgent(llm, self.state, config)
        self.strategist = StrategistAgent(llm, self.state, config)
        self.risk_manager = RiskManagerAgent(llm, self.state, config, rules)
        self.executor = ExecutorAgent(llm, self.state, config, coinbase, rules)

        # New analysis components
        self.fear_greed = FearGreedIndex()
        self.multi_tf = MultiTimeframeAnalyzer(config, coinbase)
        self.trailing_stops = TrailingStopManager(
            default_trail_pct=config.get("risk", {}).get("trailing_stop_pct", 0.03)
        )

        # Fee management and high-stakes mode
        self.fee_manager = FeeManager(config)
        self.high_stakes = HighStakesManager(config, audit=None)  # Will set audit below

        # Journal and audit
        self.journal = TradeJournal()
        self.audit = AuditLog()
        self.high_stakes.audit = self.audit  # Connect audit after creation

        # Portfolio rotator (autonomous crypto-to-crypto swaps)
        self.rotator = PortfolioRotator(
            config=config,
            coinbase_client=coinbase,
            llm_client=llm,
            fee_manager=self.fee_manager,
            high_stakes=self.high_stakes,
            multi_tf=self.multi_tf,
            fear_greed=self.fear_greed,
            journal=self.journal,
            audit=self.audit,
        )

        # Tasks
        self.active_tasks: list[Task] = []
        self._pending_approvals: dict[str, dict] = {}
        self._pending_approvals_lock = threading.Lock()
        self._load_pending_approvals()

        # ─── Stats Database (persistent analytics) ───
        self.stats_db = StatsDB()

        # ─── Strategic context cache (refreshed every 60s from DB) ───
        self._strategic_context_str: str = ""
        self._strategic_context_ts: float = 0.0
        self._STRATEGIC_CONTEXT_TTL: float = 60.0

        # ─── Position reconciliation (live mode only) ───
        # Reconcile TradingState.positions against real Coinbase balances every N cycles
        self._reconcile_every: int = config.get("trading", {}).get("reconcile_every_cycles", 10)
        self._reconcile_counter: int = 0

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

        logger.info("═══════════════════════════════════════════")
        logger.info("  🤖 Orchestrator initialized")
        logger.info(f"  Trading pairs: {self.pairs}")
        logger.info(f"  Interval: {self.interval}s")
        logger.info(f"  WebSocket: {'✅ Enabled' if ws_feed else '❌ Disabled (polling)'}")
        logger.info(f"  LLM Chat: ✅ Conversational mode")
        logger.info(f"  Fear & Greed: ✅ Enabled")
        logger.info(f"  Multi-Timeframe: ✅ Enabled")
        logger.info(f"  Trailing Stops: ✅ Enabled")
        logger.info(f"  Portfolio Rotation: ✅ Enabled")
        logger.info(f"  Fee-Aware Trading: ✅ Enabled")
        logger.info(f"  High-Stakes Mode: ✅ Ready")
        logger.info(f"  Audit Log: ✅ Enabled")
        logger.info(f"  Mode: {'📝 PAPER' if coinbase.paper_mode else '💰 LIVE'}")
        logger.info("═══════════════════════════════════════════")

    def run_forever(self) -> None:
        """Main loop — runs continuously until stopped."""
        logger.info("🚀 Starting main trading loop...")

        if self.telegram:
            self.telegram.send_message(
                "🤖 *Auto-Traitor Online*\n\n"
                f"Mode: {'📝 Paper' if self.coinbase.paper_mode else '💰 Live'}\n"
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

            try:
                # Run the full pipeline for each pair
                for pair in self.pairs:
                    self._run_pipeline(pair)

                # ─── Portfolio Rotation (autonomous swaps) ───
                self._run_rotation()

                # Update trailing stops with current prices
                triggered = self.trailing_stops.update_prices(
                    self.state.current_prices
                )
                for t in triggered:
                    self.audit.log_trade(
                        pair=t["pair"], action="trailing_stop_exit",
                        amount=0, price=t.get("trigger_price", 0),
                    )
                    event_msg = (
                        f"Trailing stop triggered on {t['pair']} "
                        f"at {format_currency(t.get('trigger_price', 0))}"
                    )
                    self.chat_handler.queue_event(event_msg)
                    if self.telegram:
                        self.telegram.send_trade_notification(
                            f"🎯 {event_msg}\n"
                            f"Entry: {format_currency(t.get('entry_price', 0))}"
                        )

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
                snapshot = self.state.take_portfolio_snapshot()

                # Check circuit breaker
                if self.state.max_drawdown >= self.config.get("risk", {}).get("max_drawdown_pct", 0.10):
                    self.state.circuit_breaker_triggered = True
                    msg = f"🛑 CIRCUIT BREAKER: Max drawdown {format_percentage(self.state.max_drawdown)} reached!"
                    logger.warning(msg)
                    self.audit.log_circuit_breaker("max_drawdown", self.state.max_drawdown)
                    self.chat_handler.queue_event(f"CRITICAL: {msg}")
                    if self.telegram:
                        self.telegram.send_alert(msg)

                # Save state periodically
                self.state.save_state()

                # ─── Position reconciliation (live mode only) ────────────
                if not self.coinbase.paper_mode:
                    self._reconcile_counter += 1
                    if self._reconcile_counter >= self._reconcile_every:
                        self._reconcile_counter = 0
                        self._reconcile_positions()

                # Sync state to Redis
                self._sync_to_redis()

                # Update health status
                components = check_component_health(
                    ollama_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
                    redis_client=self.redis,
                )
                update_health(
                    status="ok",
                    cycle_count=cycle_count,
                    components=components,
                )

            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Pipeline error: {e}", exc_info=True)
                if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    alert_msg = (
                        f"🚨 *Auto-Traitor alert*: {consecutive_errors} consecutive pipeline "
                        f"errors — last: `{type(e).__name__}: {str(e)[:120]}`"
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

            # Wait for next cycle
            time.sleep(self.interval)

        logger.info("Orchestrator stopped.")

    def _get_strategic_context(self) -> str:
        """Return the latest strategic context string (cached 60s, reads from StatsDB)."""
        now = time.time()
        if now - self._strategic_context_ts < self._STRATEGIC_CONTEXT_TTL:
            return self._strategic_context_str
        try:
            rows = self.stats_db.get_latest_strategic_context()
            if not rows:
                self._strategic_context_str = ""
            else:
                parts = []
                for row in rows:
                    horizon = row["horizon"].upper()
                    text = row["summary_text"] or ""
                    if text:
                        parts.append(f"[{horizon} PLAN] {text}")
                self._strategic_context_str = "\n".join(parts)
            self._strategic_context_ts = now
        except Exception as e:
            logger.debug(f"Failed to load strategic context: {e}")
        return self._strategic_context_str

    def _run_pipeline(self, pair: str) -> None:
        """Run the full analysis → strategy → risk → execute pipeline for a pair."""
        logger.info(f"🔍 Analyzing {pair}...")

        # Cycle identifier — links all reasoning traces for this run together
        cycle_id = str(uuid.uuid4())
        strategic_context = self._get_strategic_context()

        # Start a Langfuse trace for this cycle (no-op if tracer not initialised)
        tracer = get_llm_tracer()
        trace_ctx = tracer.start_trace(
            cycle_id=cycle_id,
            pair=pair,
            metadata={"strategic_context_preview": strategic_context[:200]},
        ) if tracer else None

        # Step 1: Fetch market data (rate-limited)
        self.rate_limiter.wait("coinbase_rest")
        candles = self.coinbase.get_candles(
            pair,
            granularity=self.config.get("analysis", {}).get("technical", {}).get(
                "candle_granularity", "ONE_HOUR"
            ),
        )

        # Use WebSocket price if available (low latency), otherwise REST
        if self.ws_feed:
            price = self.ws_feed.get_price(pair)
            if price <= 0:  # WS not ready yet, fall back to REST
                self.rate_limiter.wait("coinbase_rest")
                price = self.coinbase.get_current_price(pair)
        else:
            self.rate_limiter.wait("coinbase_rest")
            price = self.coinbase.get_current_price(pair)
        self.state.update_price(pair, price)

        # Get latest news headlines
        news_headlines = "No news available."
        if self.news:
            news_headlines = self.news.get_headlines(
                self.config.get("news", {}).get("articles_for_analysis", 15)
            )
        elif self.redis:
            try:
                cached = self.redis.get("news:latest")
                if cached:
                    articles = json.loads(cached)
                    news_headlines = "\n".join(
                        f"- [{a.get('source', '?')}] {a.get('title', '')}"
                        for a in articles[:15]
                    )
            except Exception:
                pass

        # Get Fear & Greed Index
        fg_prompt = ""
        try:
            fg_prompt = self.fear_greed.get_for_prompt()
        except Exception as e:
            logger.debug(f"Fear & Greed unavailable: {e}")

        # Get multi-timeframe confluence
        mtf_prompt = ""
        try:
            mtf_prompt = self.multi_tf.get_for_prompt(pair)
        except Exception as e:
            logger.debug(f"Multi-TF unavailable: {e}")

        # Get recent trade outcomes for outcome feedback (strategist prompt)
        recent_outcomes = ""
        try:
            recent_outcomes = self.stats_db.get_recent_outcomes(pair, n=10)
        except Exception as e:
            logger.debug(f"Failed to load recent outcomes: {e}")

        # Step 2: Market Analysis (with F&G, multi-TF, strategic context, reasoning persistence)
        analysis_result = self.market_analyst.execute({
            "pair": pair,
            "candles": candles,
            "news_headlines": news_headlines,
            "fear_greed": fg_prompt,
            "multi_timeframe": mtf_prompt,
            "strategic_context": strategic_context,
            "cycle_id": cycle_id,
            "stats_db": self.stats_db,
            "trace_ctx": trace_ctx,
        })

        signal = analysis_result.get("signal", {})
        if "error" in analysis_result:
            logger.warning(f"Analysis failed for {pair}: {analysis_result.get('error')}")
            self.journal.log_decision("analysis_failed", pair, "none", {"error": analysis_result.get('error')})
            return

        # Notify on high-confidence signals
        confidence = signal.get("confidence", 0)
        notify_threshold = self.config.get("telegram", {}).get(
            "notify_on_signal_confidence", 0.8
        )
        if confidence >= notify_threshold and self.telegram:
            signal_obj = self.state.signals[-1] if self.state.signals else None
            if signal_obj:
                self.telegram.send_signal_notification(signal_obj.to_summary())

        # Step 3: Strategy Generation
        strategy_result = self.strategist.execute({
            "signal": signal,
            "active_tasks": [t.to_dict() for t in self.active_tasks if not t.completed],
            "current_balance": self.coinbase.balance,
            "open_positions": self.state.open_positions,
            "recent_trades": [t.to_summary() for t in self.state.recent_trades],
            "recent_outcomes": recent_outcomes,
            "strategic_context": strategic_context,
            "cycle_id": cycle_id,
            "stats_db": self.stats_db,
            "trace_ctx": trace_ctx,
        })

        if strategy_result.get("action") == "hold":
            logger.info(f"📊 {pair}: HOLD — {strategy_result.get('reason', strategy_result.get('reasoning', 'No action'))}")
            self.journal.log_decision("hold", pair, "hold", {"signal": signal, "reasoning": strategy_result.get('reason', '')})
            return

        # Add current price to strategy result for risk manager
        strategy_result["current_price"] = price

        # Step 4: Risk Validation
        risk_result = self.risk_manager.execute({
            "proposal": strategy_result,
            "portfolio_value": self.state.portfolio_value,
            "cash_balance": self.state.cash_balance,
            "cycle_id": cycle_id,
            "stats_db": self.stats_db,
            "trace_ctx": trace_ctx,
        })

        if not risk_result.get("approved"):
            logger.info(f"🚫 {pair}: Trade rejected — {risk_result.get('reason', 'Unknown')}")
            self.journal.log_decision("trade_rejected", pair, strategy_result.get("action", "unknown"), {
                "reason": risk_result.get("reason", "Unknown"),
                "proposal": strategy_result,
            })
            self.audit.log_rule_check("risk_validation", passed=False, details=risk_result.get('reason', 'Unknown'))
            return

        # Step 5: Handle approval or execute
        if risk_result.get("needs_approval"):
            trade_desc = (
                f"{risk_result['action'].upper()} {risk_result['pair']}\n"
                f"Amount: {format_currency(risk_result['usd_amount'])}\n"
                f"Price: {format_currency(risk_result['price'])}\n"
                f"Stop-Loss: {format_currency(risk_result.get('stop_loss', 0))}\n"
                f"Take-Profit: {format_currency(risk_result.get('take_profit', 0))}\n"
                f"Confidence: {format_percentage(risk_result.get('confidence', 0))}"
            )
            trade_id = f"pending_{uuid.uuid4().hex[:8]}"
            with self._pending_approvals_lock:
                self._pending_approvals[trade_id] = risk_result

            if self.telegram:
                self.telegram.request_approval(trade_desc, trade_id)
            else:
                logger.warning("Trade needs approval but Telegram not configured — skipping")
            return

        # Step 6: Execute Trade
        exec_result = self.executor.execute({
            "approved_trade": risk_result,
        })

        if exec_result.get("executed"):
            # Persist trade to StatsDB (returns the SQLite row id for backfilling)
            try:
                stats_trade_id = self.stats_db.record_trade(
                    pair=risk_result.get('pair', pair),
                    action=risk_result.get('action', 'unknown'),
                    price=risk_result.get('price', price),
                    quantity=exec_result.get('quantity', 0),
                    usd_amount=risk_result.get('usd_amount', 0),
                    confidence=risk_result.get('confidence', 0),
                    signal_type=signal.get('signal_type', ''),
                    stop_loss=risk_result.get('stop_loss', 0),
                    take_profit=risk_result.get('take_profit', 0),
                    reasoning=risk_result.get('reasoning', ''),
                )
                # Link this cycle's reasoning rows to the trade that resulted from them
                self.stats_db.backfill_reasoning_trade_id(cycle_id, stats_trade_id)
            except Exception as e:
                logger.debug(f"Failed to record trade in StatsDB: {e}")

            # Log to journal and audit
            self.journal.log_trade(
                pair=risk_result.get('pair', pair),
                action=risk_result.get('action', 'unknown'),
                quantity=exec_result.get('quantity', 0),
                price=risk_result.get('price', price),
                usd_amount=risk_result.get('usd_amount', 0),
                confidence=risk_result.get('confidence', 0),
                signal_type=signal.get('signal_type', ''),
                stop_loss=risk_result.get('stop_loss', 0),
                take_profit=risk_result.get('take_profit', 0),
                reasoning=risk_result.get('reasoning', ''),
                fear_greed=self.fear_greed.last_value or 0,
                rsi=signal.get('rsi', 0),
                macd_signal=signal.get('macd_signal', ''),
            )
            self.audit.log_trade(
                pair=risk_result.get('pair', pair),
                action=risk_result.get('action', 'unknown'),
                amount=risk_result.get('usd_amount', 0),
                price=risk_result.get('price', price),
            )

            # Set up trailing stop for new position
            if risk_result.get('action') == 'buy':
                self.trailing_stops.add_stop(
                    pair=risk_result.get('pair', pair),
                    entry_price=risk_result.get('price', price),
                    initial_stop=risk_result.get('stop_loss'),
                )

            # Notify: trade executed
            trade_event = (
                f"{'BUY' if risk_result['action'] == 'buy' else 'SELL'} "
                f"{risk_result['pair']} — "
                f"{format_currency(risk_result['usd_amount'])} "
                f"at {format_currency(risk_result['price'])} "
                f"(confidence: {format_percentage(risk_result.get('confidence', 0))})"
            )
            self.chat_handler.queue_event(f"Trade executed: {trade_event}")

            if self.telegram and self.config.get("telegram", {}).get("notify_on_trade", True):
                trade_data = exec_result.get("trade", {})
                self.telegram.send_trade_notification(trade_event)

            # Finish trace with trade outcome metadata
            if trace_ctx is not None:
                try:
                    trace_ctx.finish(metadata={
                        "trade_executed": True,
                        "action": risk_result.get("action"),
                        "usd_amount": risk_result.get("usd_amount"),
                        "confidence": risk_result.get("confidence"),
                    })
                except Exception:
                    pass

    def _reconcile_positions(self) -> None:
        """
        Reconcile TradingState.positions against actual Coinbase balances (live mode only).
        Corrects drift caused by partial fills, crashes, or external account changes.
        Runs every reconcile_every_cycles cycles (~20 min at default 120s interval).
        """
        try:
            # Only reconcile USD-quoted pairs; cross pairs (e.g. ETH-BTC) would
            # produce an incorrect derived pair ("ETH-USD") and corrupt state.
            expected = {
                pair.split("-")[0]: qty
                for pair, qty in self.state.open_positions.items()
                if qty > 0 and pair.endswith("-USD")
            }
            result = self.coinbase.reconcile_positions(expected)

            if not result["matched"]:
                for d in result["discrepancies"]:
                    currency = d["currency"]
                    actual_qty = d["actual"]
                    pair = f"{currency}-USD"

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

    def _sync_to_redis(self) -> None:
        """Sync current state to Redis for the dashboard / other services."""
        if not self.redis:
            return
        try:
            self.redis.set(
                "agent:state",
                json.dumps(self.state.to_summary(), default=str),
                ex=300,
            )
            self.redis.set(
                "agent:rules_status",
                json.dumps(self.rules.get_status(), default=str),
                ex=300,
            )
            # Persist pending approvals so they survive restarts
            with self._pending_approvals_lock:
                pending_snapshot = dict(self._pending_approvals) if self._pending_approvals else None
            if pending_snapshot:
                self.redis.set(
                    "agent:pending_approvals",
                    json.dumps(pending_snapshot, default=str),
                    ex=86400,  # 24h TTL
                )
        except Exception as e:
            logger.debug(f"Redis sync failed: {e}")

    def _load_pending_approvals(self) -> None:
        """Load pending approvals from Redis on startup.

        Re-validates each entry so a poisoned/corrupted Redis key cannot inject
        arbitrary trade payloads that bypass AbsoluteRules on the next /approve.
        """
        if not self.redis:
            return
        try:
            data = self.redis.get("agent:pending_approvals")
            if not data:
                return
            loaded: dict = json.loads(data)
            validated: dict = {}
            for trade_id, approval in loaded.items():
                is_swap = approval.get("_is_swap", False)
                if is_swap:
                    # Swap proposals are structurally different — keep but log
                    validated[trade_id] = approval
                    continue
                pair = approval.get("pair", "")
                action = approval.get("action", "")
                try:
                    usd_amount = float(approval.get("usd_amount") or 0)
                except (TypeError, ValueError):
                    usd_amount = 0.0
                from src.utils.security import validate_trading_pair
                if (
                    not validate_trading_pair(pair)
                    or action not in ("buy", "sell")
                    or usd_amount <= 0
                    or usd_amount > self.rules.max_single_trade_usd * 2
                ):
                    logger.warning(
                        f"⚠️ Discarding invalid pending approval from Redis: "
                        f"id={trade_id!r} pair={pair!r} action={action!r} "
                        f"usd={usd_amount}"
                    )
                    continue
                validated[trade_id] = approval
            discarded = len(loaded) - len(validated)
            self._pending_approvals = validated
            logger.info(
                f"Loaded {len(validated)} pending approvals from Redis"
                + (f" ({discarded} discarded as invalid)" if discarded else "")
            )
        except Exception as e:
            logger.debug(f"Failed to load pending approvals: {e}")

    # =========================================================================
    # LLM Chat Handler — Function Registry
    # =========================================================================

    def _register_chat_functions(self) -> None:
        """Register all trading functions the LLM chat handler can call."""
        ch = self.chat_handler

        # ─── Read functions ────────────────────────────────────────────
        ch.register_function("get_status", lambda p: self.state.to_summary())

        ch.register_function("get_positions", lambda p: {
            "open_positions": self.state.open_positions,
            "current_prices": {
                k: v for k, v in self.state.current_prices.items()
                if k in self.state.open_positions
            },
        })

        ch.register_function("get_recent_trades", lambda p: {
            "trades": [t.to_summary() for t in self.state.recent_trades]
        })

        ch.register_function("get_balance", lambda p: {
            "portfolio_value": format_currency(self.state.portfolio_value),
            "cash_balance": format_currency(self.state.cash_balance),
            "return_pct": format_percentage(self.state.return_pct),
            "total_pnl": format_currency(self.state.total_pnl),
        })

        ch.register_function("get_current_prices", lambda p: {
            "prices": self.state.current_prices
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

        ch.register_function("get_trading_rules", lambda p: self.rules.get_status())

        ch.register_function("get_fee_info", lambda p: self.fee_manager.get_fee_summary())

        ch.register_function("get_pending_swaps", lambda p: {
            "pending_swaps": {
                sid: {
                    "sell": sp.sell_pair,
                    "buy": sp.buy_pair,
                    "usd": sp.usd_amount,
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

        logger.info(f"🧠 Registered {len(ch._function_handlers)} chat functions")

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
        return {
            "portfolio_value": format_currency(s.portfolio_value),
            "cash_balance": format_currency(s.cash_balance),
            "return_pct": format_percentage(s.return_pct),
            "max_drawdown": format_percentage(s.max_drawdown),
            "total_trades": s.total_trades,
            "win_rate": format_percentage(s.win_rate),
            "total_pnl": format_currency(s.total_pnl),
            "open_positions": {
                pair: {
                    "qty": qty,
                    "price": format_currency(s.current_prices.get(pair, 0)),
                }
                for pair, qty in s.open_positions.items()
            },
            "current_prices": {
                pair: format_currency(price)
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
        balance = self.coinbase.balance
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
                return f"✅ Trade executed successfully!"
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

            proposals = self.rotator.evaluate_rotation(
                held_pairs=held_pairs,
                all_pairs=self.pairs,
                current_prices=self.state.current_prices,
                portfolio_value=self.state.portfolio_value,
            )

            if not proposals:
                return

            for proposal in proposals:
                if proposal.priority == "autonomous":
                    # Execute autonomously (within allocation, fee-positive)
                    logger.info(
                        f"🔄 Auto-swap: {proposal.sell_pair} → {proposal.buy_pair} "
                        f"(${proposal.usd_amount:.0f}, net +{proposal.net_gain_pct*100:.2f}%)"
                    )
                    result = self.rotator.execute_swap(proposal)
                    if result.get("executed") and self.telegram:
                        self.telegram.send_trade_notification(
                            f"🔄 *Auto-Swap Executed*\n\n"
                            f"Sold: {proposal.sell_pair}\n"
                            f"Bought: {proposal.buy_pair}\n"
                            f"Amount: {format_currency(proposal.usd_amount)}\n"
                            f"Expected net gain: +{proposal.net_gain_pct*100:.2f}%\n"
                            f"Fees: {format_currency(proposal.fee_estimate.total_fee_usd)}\n"
                            f"Confidence: {format_percentage(proposal.confidence)}"
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
                f"  ${proposal.usd_amount:.0f} | "
                f"net +{proposal.net_gain_pct*100:.2f}%\n"
            )
        lines.append("\nReply /approve <id> or /reject <id>")
        return "\n".join(lines)

    def _cmd_rotate(self, data: dict) -> str:
        """Force a rotation check immediately."""
        held_pairs = list(self.state.open_positions.keys())
        if not held_pairs:
            return "🔄 No open positions to rotate."

        proposals = self.rotator.evaluate_rotation(
            held_pairs=held_pairs,
            all_pairs=self.pairs,
            current_prices=self.state.current_prices,
            portfolio_value=self.state.portfolio_value,
        )

        return self.rotator.get_rotation_summary(proposals)
