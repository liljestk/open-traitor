"""
Proactive Engine — background thread that monitors trading events
and autonomously pushes updates, morning plans, and evening summaries.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Optional  # noqa: F401 (Any used in type hints)

from src.telegram_bot.persona import PRO_TRADER_PERSONA
from src.utils.logger import get_logger

logger = get_logger("telegram.proactive")


class ProactiveEngine:
    """
    Background engine that ACTIVELY monitors and pushes updates.
    Runs its own thread — checks every 30s for things worth sharing.

    EVENT-BASED TRIGGERS (instant):
      - Trade executed → always notify (quiet+)
      - Big win/loss (>$50 or >5%) → celebrate or analyze
      - Approval needed → remind until handled
      - Circuit breaker / emergency → ALWAYS notify
      - Significant price movement (>3%) on held assets

    SCHEDULED TRIGGERS (timed):
      - Morning plan (06:00-09:00 UTC) — overnight recap + day plan
      - Evening summary (20:00-22:00 UTC) — how the day went
      - User-configured scheduled reports ("give me BTC stats hourly")
      - Periodic check-in (based on verbosity interval)
    """

    def __init__(self, send_callback: Callable, llm_client, personality):
        self._send = send_callback
        self._llm = llm_client
        self._personality = personality
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._events: deque[dict] = deque(maxlen=200)
        self._lock = threading.Lock()

        # Timing
        self._last_periodic = time.time()
        self._last_morning: Optional[str] = None  # date string
        self._last_evening: Optional[str] = None
        self._last_prices: dict[str, float] = {}
        self._pending_approvals_reminded: set[str] = set()

        # State & stats references (set by orchestrator)
        self._get_context: Optional[Callable] = None
        self._stats_db = None  # StatsDB reference
        self._currency_symbol: str = "$"  # Updated from context on each tick

        # Live config reference — updated via set_config() to support hot-reload
        self._live_config: dict = {}

        # Notification cooldowns (pair → last-sent epoch seconds)
        self._price_alert_ts: dict[str, float] = {}   # 20-min per-pair cooldown
        self._big_result_ts: dict[str, float] = {}    # 10-min per-pair cooldown

    def set_context_provider(self, provider: Callable) -> None:
        self._get_context = provider

    def set_stats_db(self, stats_db) -> None:
        self._stats_db = stats_db

    def set_config(self, live_config: dict) -> None:
        """Set reference to the live top-level config dict for hot-reloadable notification settings."""
        self._live_config = live_config

    def _tg(self, key: str, default: Any = None) -> Any:
        """Read a telegram config value from the live config (hot-reloadable)."""
        return self._live_config.get("telegram", {}).get(key, default)

    def queue_event(self, event: str, severity: str = "info", pair: Optional[str] = None) -> None:
        """
        Queue a trading event. Severity levels:
          critical — always sent immediately
          trade    — sent immediately if detail >= 1
          signal   — sent if detail >= 2
          info     — batched for periodic updates
        """
        event_data = {
            "message": event,
            "severity": severity,
            "pair": pair,
            "ts": time.time(),
        }

        with self._lock:
            self._events.append(event_data)

        # Record to stats DB
        if self._stats_db:
            try:
                self._stats_db.record_event(
                    event_type=severity, message=event,
                    severity=severity, pair=pair,
                )
            except Exception:
                pass

        # INSTANT notifications based on severity
        event_lower = event.lower()
        if severity == "critical" or any(kw in event_lower for kw in [
            "circuit breaker", "emergency stop", "emergency"
        ]):
            self._send(f"🚨 *ALERT*\n\n{event}")

        elif severity == "trade" or "trade executed" in event_lower:
            if self._tg("notify_on_trade", True) and self._personality.detail_level >= 1:
                self._send(f"📊 {event}")

            # Check for big wins/losses (pass pair for debounce)
            self._check_big_result(event, pair=pair)

        elif "approval" in event_lower or "pending" in event_lower:
            # Approval requests are always sent (at least in quiet mode)
            if self._personality.detail_level >= 1:
                self._send(f"⚠️ *Needs your approval:*\n{event}")

        elif severity == "signal" and self._personality.detail_level >= 3:
            self._send(f"📡 {event}")

    def _check_big_result(self, event: str, pair: Optional[str] = None) -> None:
        """Detect big wins or losses in trade events and react."""
        # 10-min per-pair debounce to prevent spam from rapid-fire trades
        if pair:
            now_ts = time.time()
            if now_ts - self._big_result_ts.get(pair, 0) < 600:
                return
        # MED-6: handle all sign/currency-symbol orderings:
        # "PnL: +$50.00", "PnL: -50.00", "PnL: $-50.00", "PnL: -€50", etc.
        pnl_match = re.search(
            r'PnL:\s*'
            r'(?P<outer_sign>[+-]?)\s*'   # optional sign before currency symbol
            r'[^\d+-]*'                    # optional currency symbol / spaces
            r'(?P<inner_sign>[+-]?)\s*'   # optional sign after currency symbol
            r'(?P<digits>[\d,]+\.?\d*)',
            event,
        )
        if not pnl_match:
            return

        try:
            outer = pnl_match.group("outer_sign")
            inner = pnl_match.group("inner_sign")
            sign = outer or inner  # prefer the outer sign if present
            pnl = float(pnl_match.group("digits").replace(",", ""))
            if sign == "-":
                pnl = -pnl
        except ValueError:
            return

        win_threshold = self._tg("big_win_threshold", 50)
        loss_threshold = self._tg("big_loss_threshold", 50)

        if pnl > win_threshold and self._tg("notify_on_big_win", True):
            sym = self._currency_symbol
            self._send(f"🔥 *Nice win!* +{sym}{pnl:,.2f}\n\nMomentum is on our side.")
            if pair:
                self._big_result_ts[pair] = time.time()
        elif pnl < -loss_threshold and self._tg("notify_on_big_loss", True):
            sym = self._currency_symbol
            self._send(
                f"📉 Took a hit: {sym}{pnl:,.2f}\n"
                f"Part of the game. Risk management kept it contained."
            )
            if pair:
                self._big_result_ts[pair] = time.time()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop_thread, daemon=True)
        self._thread.start()
        logger.info("🔄 Proactive engine started (30s tick)")

    def stop(self) -> None:
        self._running = False

    def _run_loop_thread(self) -> None:
        asyncio.run(self._run_loop())

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"Proactive tick error: {e}", exc_info=True)
            await asyncio.sleep(30)

    async def _tick(self) -> None:
        if not self._get_context:
            return

        now_utc = datetime.now(timezone.utc)
        today = now_utc.strftime("%Y-%m-%d")
        hour = now_utc.hour

        ctx = self._get_context()
        self._currency_symbol = ctx.get("currency_symbol", "$")

        # Always check price movements (even in quiet mode for held assets)
        if self._personality.detail_level >= 1:
            self._check_price_movements(ctx)

        # Skip everything else if proactive is off
        if not self._personality.proactive:
            return

        # ─── 1. Morning Plan (06:00-09:00 UTC, once per day) ───
        if self._last_morning != today and 6 <= hour <= 9:
            if self._tg("notify_morning_plan", True) and self._personality.detail_level >= 2:
                await self._send_morning_plan(ctx)
            self._last_morning = today

        # ─── 2. Evening Summary (20:00-22:00 UTC, once per day) ───
        if self._last_evening != today and 20 <= hour <= 22:
            if self._tg("notify_evening_summary", True) and self._personality.detail_level >= 1:
                await self._send_evening_summary(ctx)
            self._last_evening = today

        # ─── 3. Scheduled Reports ───
        self._run_scheduled_reports(ctx)

        # ─── 4. Periodic proactive update ───
        now = time.time()
        interval = self._tg("status_update_interval", self._personality.update_interval)
        if (
            self._tg("notify_periodic_update", True)
            and interval > 0
            and (now - self._last_periodic) >= interval
        ):
            await self._send_periodic_update(ctx)
            self._last_periodic = now

        # ─── 5. Record portfolio snapshot ───
        if self._stats_db:
            try:
                self._stats_db.record_snapshot(
                    portfolio_value=ctx.get("raw_portfolio_value", 0),
                    cash_balance=ctx.get("raw_cash_balance", 0),
                    return_pct=ctx.get("raw_return_pct", 0),
                    total_pnl=ctx.get("raw_total_pnl", 0),
                    max_drawdown=ctx.get("raw_max_drawdown", 0),
                    open_positions=ctx.get("raw_positions", {}),
                    current_prices=ctx.get("raw_prices", {}),
                    high_stakes_active=ctx.get("high_stakes_active", False),
                )
            except Exception as e:
                logger.debug(f"Snapshot record failed: {e}")

    def _check_price_movements(self, ctx: dict) -> None:
        if not self._tg("notify_on_price_move", True):
            return

        current_prices = ctx.get("raw_prices", {})
        positions = ctx.get("raw_positions", {})

        # Threshold: use configured value, fall back to detail-level heuristic
        configured_pct = self._tg("price_move_threshold_pct", None)
        if configured_pct is not None:
            threshold = float(configured_pct) / 100.0
        else:
            threshold = 0.03 if self._personality.detail_level >= 2 else 0.05

        cooldown_secs = self._tg("price_move_cooldown_minutes", 20) * 60

        for pair in positions:
            price = current_prices.get(pair, 0)
            last = self._last_prices.get(pair, 0)

            if last > 0 and price > 0:
                change = (price - last) / last

                if abs(change) >= threshold:
                    now_ts = time.time()
                    if now_ts - self._price_alert_ts.get(pair, 0) < cooldown_secs:
                        # Cooldown active — update baseline silently to avoid stale drift
                        self._last_prices[pair] = price
                        continue
                    d = "📈" if change > 0 else "📉"
                    sym = self._currency_symbol
                    self._send(
                        f"{d} *{pair}* moved *{change*100:+.1f}%*\n"
                        f"{sym}{last:,.2f} → {sym}{price:,.2f}"
                    )
                    self._last_prices[pair] = price  # Reset after alert
                    self._price_alert_ts[pair] = now_ts
            elif price > 0:
                self._last_prices[pair] = price

    async def _send_periodic_update(self, ctx: dict) -> None:
        # LOW-8: snapshot events first but only drain (remove from deque) after
        # a successful LLM call so they are not lost on LLM failure.
        with self._lock:
            events = list(self._events)

        if not events and self._personality.detail_level < 3:
            return

        events_text = "\n".join(f"• {e['message']}" for e in events[-10:]) if events else "No notable events."

        # Include stats if available
        stats_text = ""
        if self._stats_db:
            try:
                s = self._stats_db.get_trade_stats(hours=4)
                if s.get("total_trades", 0) > 0:
                    sym = self._currency_symbol
                    stats_text = (
                        f"\nRecent stats (4h): {s['total_trades']} trades, "
                        f"PnL: {sym}{s['total_pnl']:+,.2f}, "
                        f"Win rate: {s['winning']}/{s['total_trades']}"
                    )
            except Exception:
                pass

        # HIGH-7: strip credential-like fields before sending to cloud LLM.
        safe_ctx = self._sanitize_ctx_for_llm(ctx)

        prompt = f"""{PRO_TRADER_PERSONA}

{self._personality.to_prompt_fragment()}

Quick check-in with your owner. Events since last update:
{events_text}
{stats_text}

State: {json.dumps(safe_ctx, indent=1, default=str)}
Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}

RULES:
- If nothing interesting → respond with just "SKIP"
- 2-5 lines max. Lead with most important thing.
- Use specific numbers. Be opinionated."""

        try:
            r = await self._llm.chat(
                system_prompt="Pro crypto trader, quick Telegram update.",
                user_message=prompt, temperature=0.6, max_tokens=300,
            )
            if r.strip().upper() != "SKIP":
                self._send(r)
            # LOW-8: only drain events after a successful LLM call.
            self._drain_events()
        except Exception as e:
            logger.debug(f"Periodic update failed: {e}")

    async def _send_morning_plan(self, ctx: dict) -> None:
        """Morning briefing: overnight recap + plan for the day."""
        overnight_text = ""
        if self._stats_db:
            try:
                trades = self._stats_db.get_trades(hours=12)
                events = self._stats_db.get_events(hours=12)
                stats = self._stats_db.get_trade_stats(hours=12)
                sym = self._currency_symbol
                overnight_text = (
                    f"\nOvernight activity (12h):\n"
                    f"  Trades: {stats.get('total_trades', 0)}\n"
                    f"  PnL: {sym}{stats.get('total_pnl', 0):+,.2f}\n"
                    f"  Events: {len(events)}\n"
                    f"  Best PnL: {sym}{stats.get('best_pnl', 0):+,.2f}\n"
                    f"  Worst PnL: {sym}{stats.get('worst_pnl', 0):+,.2f}"
                )
            except Exception:
                pass

        # HIGH-7: strip credential-like fields before sending to cloud LLM.
        safe_ctx = self._sanitize_ctx_for_llm(ctx)

        prompt = f"""{PRO_TRADER_PERSONA}

It's morning. Give your owner a briefing.
{overnight_text}

Current state: {json.dumps(safe_ctx, indent=1, default=str)}

FORMAT:
☀️ *Morning Briefing — {datetime.now(timezone.utc).strftime('%b %d')}*

1. Overnight recap (what happened while you slept)
2. Current market vibe (1 line, be opinionated)
3. What I'm watching today (specific pairs + levels)
4. Planned moves
5. Risk notes

Keep it under 12 lines. Specific prices and levels."""

        try:
            r = await self._llm.chat(
                system_prompt="Pro crypto trader, morning briefing.",
                user_message=prompt, temperature=0.5, max_tokens=600,
            )
            self._send(r)
        except Exception as e:
            # LOW-6: warn rather than silently debug-log; the user expects a morning briefing.
            logger.warning(f"Morning plan failed: {e}")

    async def _send_evening_summary(self, ctx: dict) -> None:
        """Evening recap: how the day went, save to stats DB."""
        day_text = ""
        if self._stats_db:
            try:
                stats = self._stats_db.get_trade_stats(hours=16)
                bw = self._stats_db.get_best_worst_trades(hours=16)
                port_range = self._stats_db.get_portfolio_range(hours=16)
                sym = self._currency_symbol
                # MED-11: only show portfolio range when we have actual samples.
                port_range_text = ""
                if port_range.get("samples", 0) > 0:
                    port_range_text = (
                        f"\n  Portfolio range: {sym}{port_range.get('low', 0):,.2f} - "
                        f"{sym}{port_range.get('high', 0):,.2f}"
                    )
                day_text = (
                    f"\nToday's numbers:\n"
                    f"  Trades: {stats.get('total_trades', 0)} "
                    f"(W:{stats.get('winning', 0)} L:{stats.get('losing', 0)})\n"
                    f"  PnL: {sym}{stats.get('total_pnl', 0):+,.2f}\n"
                    f"  Volume: {sym}{stats.get('total_volume', 0):,.2f}\n"
                    f"  Fees: {sym}{stats.get('total_fees', 0):,.2f}"
                    f"{port_range_text}"
                )

                # Save daily summary
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                self._stats_db.save_daily_summary(
                    date=today,
                    total_trades=stats.get("total_trades", 0),
                    winning_trades=stats.get("winning", 0),
                    losing_trades=stats.get("losing", 0),
                    total_pnl=stats.get("total_pnl", 0),
                    high_value=port_range.get("high", 0),
                    low_value=port_range.get("low", 0),
                    events_count=sum(self._stats_db.get_event_counts(hours=16).values()),
                )
            except Exception as e:
                logger.debug(f"Evening stats gathering failed: {e}")

        # HIGH-7: strip credential-like fields before sending to cloud LLM.
        safe_ctx = self._sanitize_ctx_for_llm(ctx)

        prompt = f"""{PRO_TRADER_PERSONA}

End of day wrap-up for your owner.
{day_text}

Current state: {json.dumps(safe_ctx, indent=1, default=str)}

FORMAT:
🌙 *Evening Wrap — {datetime.now(timezone.utc).strftime('%b %d')}*

1. Day's result (1 line, straight to the point)
2. Best/worst moves
3. What I learned today
4. Overnight plan

Keep it under 10 lines. Be honest about losses."""

        try:
            r = await self._llm.chat(
                system_prompt="Pro crypto trader, evening recap.",
                user_message=prompt, temperature=0.5, max_tokens=500,
            )
            self._send(r)
        except Exception as e:
            # LOW-6: warn rather than silently debug-log.
            logger.warning(f"Evening summary failed: {e}")

    def _run_scheduled_reports(self, ctx: dict) -> None:
        """Execute any due scheduled reports."""
        if not self._stats_db:
            return

        try:
            schedules = self._stats_db.get_active_schedules()
        except Exception:
            return

        now_utc = datetime.now(timezone.utc)

        for sched in schedules:
            if not self._schedule_is_due(sched, now_utc):
                continue

            try:
                report = self._generate_scheduled_report(sched, ctx)
                if report:
                    self._send(f"📊 *Scheduled: {sched['name']}*\n\n{report}")
                    self._stats_db.update_schedule_last_run(sched["id"])
            except Exception as e:
                logger.debug(f"Scheduled report {sched['name']} failed: {e}")

    def _schedule_is_due(self, sched: dict, now: datetime) -> bool:
        """Simple interval-based schedule check."""
        last_run = sched.get("last_run_ts")
        if not last_run:
            return True  # Never run before

        try:
            last = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return True

        # Parse cron expression as simple interval
        cron = sched.get("cron_expression", "")
        try:
            if cron.endswith("h"):
                interval_hours = int(cron[:-1])
            elif cron.endswith("m"):
                interval_hours = int(cron[:-1]) / 60
            elif cron.endswith("d"):
                interval_hours = int(cron[:-1]) * 24
            else:
                # MED-12: log unrecognised cron expressions so misconfigurations are visible.
                logger.warning(f"Unrecognised cron expression {cron!r} for schedule {sched.get('name')!r} — defaulting to hourly")
                interval_hours = 1
        except (ValueError, TypeError):
            logger.warning(f"Malformed cron expression {cron!r} for schedule {sched.get('name')!r} — defaulting to hourly")
            interval_hours = 1

        return (now - last).total_seconds() >= interval_hours * 3600

    def _generate_scheduled_report(self, sched: dict, ctx: dict) -> Optional[str]:
        """Generate content for a scheduled report."""
        query_type = sched.get("query_type", "")
        params = json.loads(sched.get("query_params", "{}"))

        data = {}
        if query_type == "pair_stats":
            pair = params.get("pair", "BTC-USD")
            hours = params.get("hours", 24)
            data = self._stats_db.get_pair_stats(pair, hours)
        elif query_type == "performance":
            hours = params.get("hours", 24)
            data = self._stats_db.get_performance_summary(hours)
        elif query_type == "portfolio_history":
            data = self._stats_db.get_portfolio_range(params.get("hours", 24))
        elif query_type == "trades":
            data = {"trades": self._stats_db.get_trades(params.get("hours", 24))}
        else:
            data = ctx

        return f"```\n{json.dumps(data, indent=2, default=str)[:1500]}\n```"

    def _drain_events(self) -> list[dict]:
        events = []
        with self._lock:
            while self._events:
                events.append(self._events.popleft())
        return events

    # HIGH-7: fields whose names suggest credentials/secrets that should never
    # leave this process, even when ctx is sent to cloud LLM providers.
    _SENSITIVE_KEY_FRAGMENTS: frozenset[str] = frozenset({
        "key", "secret", "password", "token", "credential",
        "auth", "apikey", "api_key", "api_secret", "private",
    })

    def _sanitize_ctx_for_llm(self, ctx: dict) -> dict:
        """Return a copy of *ctx* safe to send to external LLM providers.

        Keeps all financial and trading fields (portfolio values, positions,
        prices, PnL, etc.) because that is exactly what the LLM needs to give
        useful trading advice.  Only strips fields whose names suggest they
        contain credentials or internal secrets.
        """
        sanitized = {}
        for k, v in ctx.items():
            k_lower = k.lower().replace("-", "_")
            if any(frag in k_lower for frag in self._SENSITIVE_KEY_FRAGMENTS):
                continue  # drop credential-like fields
            if isinstance(v, dict):
                sanitized[k] = self._sanitize_ctx_for_llm(v)
            else:
                sanitized[k] = v
        return sanitized
