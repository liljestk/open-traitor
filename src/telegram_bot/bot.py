"""
Telegram Bot for OpenTraitor — LLM-Powered Conversational Interface.

This is NOT a traditional command-based bot. It's an LLM agent that uses
Telegram as its communication channel. Every message — commands, free text,
button presses — flows through the LLM for interpretation and response.

SECURITY MODEL:
  - ONLY users whose Telegram numeric user ID is in TELEGRAM_AUTHORIZED_USERS
    can interact with this bot.
  - Every unauthorized attempt is logged.
  - There is NO fallback, NO open mode, NO "allow all" option.
  - Bot REFUSES to start if authorized_users list is empty.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from src.utils.logger import get_logger

logger = get_logger("telegram")


class TelegramBot:
    """
    LLM-powered Telegram bot for the OpenTraitor trading agent.

    All messages route through the LLM chat handler. Slash commands are
    supported as shortcuts but are still interpreted by the LLM for
    natural, contextual responses.

    SECURITY: Only numeric user IDs in authorized_users can interact.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        authorized_users: list[str],
        chat_handler=None,
        on_command: Optional[Callable] = None,
        mode: str = "controller",
        exchange_name: str = "",
    ):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.chat_handler = chat_handler  # TelegramChatHandler
        self.on_command = on_command  # Legacy fallback
        self.mode = mode
        self.exchange_name = exchange_name  # e.g. "COINBASE", "IBKR"
        self._app = None
        self._thread: Optional[threading.Thread] = None
        self._running_event = threading.Event()
        self._outbound_bot = None  # H8: reuse Bot instance for outbound messages
        self._outbound_bot_lock = threading.Lock()

        # Hard outbound rate limit (anti-chatty guardrail) — sliding 1h window.
        # Critical messages (ALERT/approval) bypass this cap.
        self._outbound_lock = threading.Lock()
        self._outbound_history: list[float] = []
        self._outbound_max_per_hour: int = 20  # default; overridden by config
        self._rate_limit_warned_at: float = 0.0

        # =====================================================================
        # AUTHORIZATION — STRICT USER ID ALLOWLIST
        # =====================================================================
        if not authorized_users:
            raise ValueError(
                "TELEGRAM_AUTHORIZED_USERS is empty! "
                "You MUST provide at least one numeric Telegram user ID. "
                "Message @userinfobot on Telegram to get your user ID."
            )

        self.authorized_users: set[str] = set()
        for uid in authorized_users:
            uid_str = str(uid).strip()
            if not uid_str or not uid_str.lstrip("-").isdigit():
                raise ValueError(
                    f"Invalid Telegram user ID: '{uid}'. "
                    "User IDs must be numeric."
                )
            self.authorized_users.add(uid_str)

        self._unauthorized_attempts: dict[str, int] = {}
        self._unauthorized_log_times: dict[str, float] = {}  # last log timestamp per user
        self._MAX_TRACKED_UNAUTHORIZED = 1000  # Cap to prevent unbounded memory growth

        # Optional stats_db handle for direct deterministic commands
        # (e.g. /label) that should NOT route through the LLM. Set via
        # ``set_stats_db()`` after construction.
        self._stats_db = None
        self._label_exchange: str = (exchange_name or "").lower()

        logger.info(
            f"🔒 Telegram bot initialized | Chat: {self.chat_id} | "
            f"Authorized users: {len(self.authorized_users)} "
            f"({', '.join(self.authorized_users)})"
        )

    # =========================================================================
    # Bot Lifecycle
    # =========================================================================

    async def _start_bot(self) -> None:
        """Start the Telegram bot with polling."""
        if self.mode == "reporting":
            logger.info("🤖 Telegram bot in REPORTING mode (no polling, outbound only).")
            return

        from telegram import Update
        from telegram.ext import (
            Application,
            CommandHandler,
            MessageHandler,
            CallbackQueryHandler,
            filters,
        )

        self._app = Application.builder().token(self.bot_token).build()

        # Slash commands are shortcuts — they still flow through the LLM
        # but we pass them with a hint so the LLM knows the intent
        shortcuts = [
            "start", "help", "status", "positions", "trades", "balance",
            "task", "rules", "news", "pause", "resume", "stop",
            "highstakes", "fees", "swaps", "rotate",
            "approve", "reject",
            "quiet", "chatty", "silent", "verbose",
            "simulate", "sims",
        ]
        for cmd in shortcuts:
            self._app.add_handler(CommandHandler(cmd, self._handle_command))

        # Direct deterministic handlers — bypass the LLM. Used for write
        # operations where the operator needs unambiguous behaviour
        # (labelling trades, listing pending labels, removing a label).
        self._app.add_handler(CommandHandler("label", self._handle_label))
        self._app.add_handler(CommandHandler("labels", self._handle_labels_list))
        self._app.add_handler(CommandHandler("unlabel", self._handle_unlabel))

        # Inline keyboard callbacks (approve/reject buttons)
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))

        # ALL free text goes through the LLM
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        logger.info(f"🤖 Telegram bot starting polling (mode={self.mode})...")
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

    def start(self) -> None:
        """Start the bot in a background thread."""
        def _run():
            # C1 fix: Use thread-local event loop without calling set_event_loop.
            # This avoids conflicting with the orchestrator's main loop and
            # Temporal emergency replans that spin up their own loops.
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(self._start_bot())
                self._running_event.set()
                loop.run_forever()
            except Exception as e:
                logger.error(f"Telegram bot error: {e}")
            finally:
                loop.close()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        logger.info("📱 Telegram bot running in background")

    async def stop(self) -> None:
        """Stop the bot."""
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        self._running_event.clear()

    # =========================================================================
    # Authorization
    # =========================================================================

    def _is_authorized(self, user_id: int, username: str = "", context: str = "") -> bool:
        """Check if user is authorized. Logs unauthorized attempts."""
        uid_str = str(user_id)

        if uid_str in self.authorized_users:
            return True

        # UNAUTHORIZED — evict oldest entries if dict is at cap
        if len(self._unauthorized_attempts) >= self._MAX_TRACKED_UNAUTHORIZED and uid_str not in self._unauthorized_attempts:
            oldest = min(self._unauthorized_log_times, key=self._unauthorized_log_times.get, default=None)
            if oldest:
                self._unauthorized_attempts.pop(oldest, None)
                self._unauthorized_log_times.pop(oldest, None)
        self._unauthorized_attempts[uid_str] = self._unauthorized_attempts.get(uid_str, 0) + 1
        count = self._unauthorized_attempts[uid_str]

        # Always log on first attempt; then throttle to once per 60 s per user
        # so a sustained brute-force is never silently swallowed in the logs.
        import time as _time
        now_mono = _time.monotonic()
        last_log = self._unauthorized_log_times.get(uid_str, 0.0)
        if count == 1 or (now_mono - last_log) >= 60:
            self._unauthorized_log_times[uid_str] = now_mono
            logger.warning(
                f"🚫 UNAUTHORIZED #{count} | "
                f"User: {user_id} (@{username or '?'}) | "
                f"Context: {context} | "
                f"Time: {datetime.now(timezone.utc).isoformat()}"
            )

        return False

    # =========================================================================
    # Message Handlers
    # =========================================================================

    async def _handle_command(self, update, context) -> None:
        """Handle slash commands — route through LLM with intent hint."""
        user = update.effective_user
        if not self._is_authorized(user.id, user.username, f"/{context.matches}"):
            await update.message.reply_text("⛔ Unauthorized. This attempt has been logged.")
            return

        # Build the message as if the user typed it naturally
        command = update.message.text  # e.g., "/highstakes 4h"
        args = " ".join(context.args) if context.args else ""

        # For simple shortcut commands, translate to natural language
        cmd_name = command.split()[0].lstrip("/").lower()

        shortcuts_to_text = {
            "start": "Hey, I just connected! What's the status?",
            "help": "What can you do? Show me all available commands.",
            "quiet": "Be quiet for a while.",
            "chatty": "Talk to me more! Be chatty.",
            "silent": "Be silent. Only critical alerts.",
            "verbose": "Give me everything — full verbosity mode.",
        }

        if cmd_name in shortcuts_to_text and not args:
            message_text = shortcuts_to_text[cmd_name]
        else:
            # Pass the command as-is — the LLM understands /commands
            message_text = command

        response = await self._get_response(message_text, user)
        await self._send_reply(update.message, response)

    async def _handle_message(self, update, context) -> None:
        """Handle free-text messages — the heart of the conversational bot."""
        user = update.effective_user
        if not self._is_authorized(user.id, user.username, "message"):
            return  # Silent ignore for free text

        response = await self._get_response(update.message.text, user)
        await self._send_reply(update.message, response)

    # ─── Direct deterministic handlers (bypass LLM) ──────────────────────

    def set_stats_db(self, stats_db, exchange: str = "") -> None:
        """Attach a StatsDB handle for deterministic write commands.

        Called from the orchestrator after construction. Without this the
        ``/label`` family of commands replies with a configuration error
        instead of silently routing through the LLM.
        """
        self._stats_db = stats_db
        if exchange:
            self._label_exchange = exchange.lower()

    async def _handle_label(self, update, context) -> None:
        """``/label <trade_id> <win|loss|skip|unsure> [note...]``

        Persists an operator-supplied label on a closed trade. Last write
        wins per (exchange, trade_id). Direct DB write — no LLM round-trip.
        """
        user = update.effective_user
        if not self._is_authorized(user.id, user.username, "/label"):
            await update.message.reply_text("⛔ Unauthorized. This attempt has been logged.")
            return
        if self._stats_db is None or not self._label_exchange:
            await update.message.reply_text(
                "⚠️ Labelling is not available — stats DB is not attached."
            )
            return
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: `/label <trade_id> <win|loss|skip|unsure> [note...]`\n"
                "Tip: send `/labels` to see recent unlabeled trades.",
                parse_mode="Markdown",
            )
            return
        raw_id, raw_label, *note_parts = args
        note = " ".join(note_parts).strip()
        try:
            row = await asyncio.to_thread(
                self._stats_db.add_trade_label,
                int(raw_id),
                raw_label,
                self._label_exchange,
                note,
                str(user.id),
                "telegram",
            )
        except ValueError as e:
            await update.message.reply_text(f"❌ {e}")
            return
        except Exception as e:
            logger.warning(f"/label failed: {e}")
            await update.message.reply_text(f"❌ Internal error labelling trade: {e}")
            return
        note_suffix = f" — _{note}_" if note else ""
        await update.message.reply_text(
            f"🏷️ Labeled trade `#{row.get('trade_id')}` as *{row.get('label')}*"
            f"{note_suffix}",
            parse_mode="Markdown",
        )

    async def _handle_labels_list(self, update, context) -> None:
        """``/labels [N]`` — list up to N (default 10) recent unlabeled
        closed trades for the operator to review.
        """
        user = update.effective_user
        if not self._is_authorized(user.id, user.username, "/labels"):
            await update.message.reply_text("⛔ Unauthorized. This attempt has been logged.")
            return
        if self._stats_db is None or not self._label_exchange:
            await update.message.reply_text(
                "⚠️ Labelling is not available — stats DB is not attached."
            )
            return
        args = context.args or []
        try:
            limit = int(args[0]) if args else 10
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, 25))
        try:
            rows = await asyncio.to_thread(
                self._stats_db.get_recent_unlabeled_trades,
                self._label_exchange,
                limit,
                720,  # 30-day window
            )
            counts = await asyncio.to_thread(
                self._stats_db.count_trade_labels, self._label_exchange
            )
        except Exception as e:
            logger.warning(f"/labels lookup failed: {e}")
            await update.message.reply_text(f"❌ Could not list trades: {e}")
            return
        if not rows:
            await update.message.reply_text(
                f"✅ No unlabeled closed trades in the last 30 days "
                f"(total labels: {counts.get('total', 0)})."
            )
            return
        lines = [
            f"🏷️ *{len(rows)} unlabeled trades* "
            f"(W={counts.get('win',0)} L={counts.get('loss',0)} "
            f"S={counts.get('skip',0)} U={counts.get('unsure',0)})",
            "",
        ]
        for r in rows:
            pnl = float(r.get("pnl") or 0.0)
            sign = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
            lines.append(
                f"{sign} `#{r.get('id')}` {r.get('pair')} "
                f"{str(r.get('action','')).upper()} "
                f"pnl={pnl:+.2f}"
            )
        lines.append("")
        lines.append("Use `/label <id> <win|loss|skip|unsure> [note]`")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _handle_unlabel(self, update, context) -> None:
        """``/unlabel <trade_id>`` — remove a label."""
        user = update.effective_user
        if not self._is_authorized(user.id, user.username, "/unlabel"):
            await update.message.reply_text("⛔ Unauthorized. This attempt has been logged.")
            return
        if self._stats_db is None or not self._label_exchange:
            await update.message.reply_text(
                "⚠️ Labelling is not available — stats DB is not attached."
            )
            return
        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: `/unlabel <trade_id>`",
                                            parse_mode="Markdown")
            return
        try:
            ok = await asyncio.to_thread(
                self._stats_db.delete_trade_label,
                int(args[0]),
                self._label_exchange,
            )
        except (TypeError, ValueError):
            await update.message.reply_text("❌ trade_id must be an integer.")
            return
        except Exception as e:
            logger.warning(f"/unlabel failed: {e}")
            await update.message.reply_text(f"❌ Internal error: {e}")
            return
        if ok:
            await update.message.reply_text(f"🗑️ Removed label for trade `#{args[0]}`",
                                            parse_mode="Markdown")
        else:
            await update.message.reply_text(
                f"ℹ️ No label found for trade `#{args[0]}`",
                parse_mode="Markdown",
            )

    async def _handle_callback(self, update, context) -> None:
        """Handle inline keyboard callbacks (approve/reject buttons)."""
        query = update.callback_query
        user = query.from_user
        if not self._is_authorized(user.id, user.username, "callback"):
            await query.answer("⛔ Unauthorized")
            return

        data = query.data
        await query.answer()

        # Route through LLM: "I'm approving trade <id>"
        if data.startswith("approve:"):
            trade_id = data[8:]
            message = f"/approve {trade_id}"
        elif data.startswith("reject:"):
            trade_id = data[7:]
            message = f"/reject {trade_id}"
        else:
            message = f"Button pressed: {data}"

        response = await self._get_response(message, user)
        # M9: Markdown fallback for callback responses
        try:
            await query.edit_message_text(response, parse_mode="Markdown")
        except Exception:
            await query.edit_message_text(response)

    # =========================================================================
    # Core Response Logic
    # =========================================================================

    async def _get_response(self, text: str, user) -> str:
        """
        Get a response for a message. Tries LLM chat handler first,
        falls back to legacy command handler.
        """
        user_name = user.first_name or user.username or "Owner"
        user_id = str(user.id)

        # Primary: LLM chat handler
        if self.chat_handler:
            try:
                return await self.chat_handler.handle_message(
                    text=text,
                    user_name=user_name,
                    user_id=user_id,
                )
            except Exception as e:
                logger.error(f"Chat handler error: {e}", exc_info=True)
                # Fall through to legacy handler

        # Fallback: Legacy command handler
        if self.on_command:
            # Extract command from slash syntax
            if text.startswith("/"):
                parts = text.split(maxsplit=1)
                cmd = parts[0].lstrip("/").lower()
                desc = parts[1] if len(parts) > 1 else ""
                return self.on_command(cmd, {
                    "description": desc,
                    "text": text,
                    "user_id": user_id,
                })
            else:
                return self.on_command("message", {
                    "text": text,
                    "user_id": user_id,
                })

        return "🤖 I'm not fully connected yet. Give me a moment..."

    async def _send_reply(self, message, text: str) -> None:
        """Send a reply, handling Telegram message length limits."""
        # Telegram has a 4096 character limit per message
        if len(text) <= 4096:
            try:
                await message.reply_text(text, parse_mode="Markdown")
            except Exception:
                # If Markdown parsing fails, send as plain text
                await message.reply_text(text)
        else:
            # Split into chunks
            chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
            for chunk in chunks:
                try:
                    await message.reply_text(chunk, parse_mode="Markdown")
                except Exception:
                    await message.reply_text(chunk)

    # =========================================================================
    # Outbound Messaging (thread-safe, called from orchestrator)
    # =========================================================================

    def _get_outbound_bot(self):
        """Return a reusable Bot instance for outbound messages (H8)."""
        if self._outbound_bot is None:
            with self._outbound_bot_lock:
                if self._outbound_bot is None:
                    from telegram import Bot
                    self._outbound_bot = Bot(token=self.bot_token)
        return self._outbound_bot

    def _get_send_loop(self):
        """Return a dedicated event loop for outbound messages (M3 fix).

        Lazily creates a background thread running an event loop, reused for
        all send_message() calls from threads without their own loop.
        """
        if not hasattr(self, '_send_loop') or self._send_loop is None:
            import asyncio
            self._send_loop = asyncio.new_event_loop()
            t = threading.Thread(target=self._send_loop.run_forever, daemon=True)
            t.start()
        return self._send_loop

    def set_max_messages_per_hour(self, n: int) -> None:
        """Hot-reloadable cap on non-critical outbound messages per rolling hour."""
        try:
            self._outbound_max_per_hour = max(1, int(n))
        except (TypeError, ValueError):
            pass

    def _allow_outbound(self, critical: bool) -> bool:
        """Return True if message may be sent. Critical messages always pass."""
        if critical:
            return True
        import time as _t
        now = _t.time()
        with self._outbound_lock:
            cutoff = now - 3600.0
            self._outbound_history = [t for t in self._outbound_history if t >= cutoff]
            if len(self._outbound_history) >= self._outbound_max_per_hour:
                # One-shot warning per hour to surface the throttle in logs.
                if now - self._rate_limit_warned_at > 3600.0:
                    logger.warning(
                        "Telegram outbound rate-limit hit (%d/h); dropping non-critical msg",
                        self._outbound_max_per_hour,
                    )
                    self._rate_limit_warned_at = now
                return False
            self._outbound_history.append(now)
        return True

    def send_message(self, text: str, critical: bool = False) -> None:
        """Send a message to the configured chat (thread-safe, uses library).

        ``critical=True`` bypasses the per-hour outbound rate limit. Use for
        alerts, approval requests, and circuit-breaker notifications only.
        """
        if not self._allow_outbound(critical):
            return
        try:
            from telegram import Bot

            async def _send(bot: Bot, chat_id: str, text: str):
                chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
                for chunk in chunks:
                    try:
                        await bot.send_message(
                            chat_id=chat_id, text=chunk, parse_mode="Markdown"
                        )
                    except Exception:
                        await bot.send_message(chat_id=chat_id, text=chunk)

            bot = self._get_outbound_bot()
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                future = asyncio.run_coroutine_threadsafe(
                    _send(bot, self.chat_id, text), loop
                )
                future.add_done_callback(
                    lambda f: f.exception() and logger.error(f"Telegram send_message failed: {f.exception()}")
                )
            except RuntimeError:
                # M3 fix: reuse a dedicated outbound event loop instead of
                # creating/destroying one per message via asyncio.run()
                loop = self._get_send_loop()
                future = asyncio.run_coroutine_threadsafe(
                    _send(bot, self.chat_id, text), loop
                )
                future.result(timeout=30)  # block until sent
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")

    def send_trade_notification(self, trade_summary: str) -> None:
        """Send a trade notification."""
        tag = f"[{self.exchange_name}] " if self.exchange_name else ""
        self.send_message(f"📊 *{tag}Trade Executed*\n\n{trade_summary}")

    def send_signal_notification(self, signal_summary: str) -> None:
        """Send a signal notification."""
        tag = f"[{self.exchange_name}] " if self.exchange_name else ""
        self.send_message(f"📡 *{tag}Signal Detected*\n\n{signal_summary}")

    def send_alert(self, alert: str) -> None:
        """Send an important alert (always sent, even in silent mode)."""
        tag = f"[{self.exchange_name}] " if self.exchange_name else ""
        self.send_message(f"🚨 *{tag}ALERT*\n\n{alert}", critical=True)

    def send_daily_summary(self, summary: str) -> None:
        """Send a daily summary."""
        self.send_message(f"📋 *Daily Summary*\n\n{summary}")

    def request_approval(self, trade_description: str, trade_id: str) -> None:
        """Request trade approval via inline keyboard."""
        try:
            from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{trade_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject:{trade_id}"),
            ]])

            async def _send_approval(bot: Bot):
                await bot.send_message(
                    chat_id=self.chat_id,
                    text=(
                        f"⚠️ *Trade Approval Required*\n\n"
                        f"{trade_description}\n\n"
                        f"This trade exceeds your approval threshold.\n"
                        f"Approve or reject?"
                    ),
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )

            bot = self._get_outbound_bot()
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                future = asyncio.run_coroutine_threadsafe(_send_approval(bot), loop)
                future.add_done_callback(
                    lambda f: f.exception() and logger.error(f"Telegram approval request failed: {f.exception()}")
                )
            except RuntimeError:
                asyncio.run(_send_approval(bot))

            logger.info(f"📱 Approval requested for {trade_id}")
        except Exception as e:
            logger.error(f"Failed to request approval: {e}")
