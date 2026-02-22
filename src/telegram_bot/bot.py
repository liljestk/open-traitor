"""
Telegram Bot for Auto-Traitor — LLM-Powered Conversational Interface.

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
    LLM-powered Telegram bot for the Auto-Traitor trading agent.

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
    ):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.chat_handler = chat_handler  # TelegramChatHandler
        self.on_command = on_command  # Legacy fallback
        self.mode = mode
        self._app = None
        self._thread: Optional[threading.Thread] = None
        self._running_event = threading.Event()
        self._outbound_bot = None  # H8: reuse Bot instance for outbound messages

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

        # Inline keyboard callbacks (approve/reject buttons)
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))

        # ALL free text goes through the LLM
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        logger.info("🤖 Telegram bot starting polling...")
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

    def start(self) -> None:
        """Start the bot in a background thread."""
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._start_bot())
                self._running_event.set()
                loop.run_forever()
            except Exception as e:
                logger.error(f"Telegram bot error: {e}")

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
            from telegram import Bot
            self._outbound_bot = Bot(token=self.bot_token)
        return self._outbound_bot

    def send_message(self, text: str) -> None:
        """Send a message to the configured chat (thread-safe, uses library)."""
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
                asyncio.run_coroutine_threadsafe(
                    _send(bot, self.chat_id, text), loop
                )
            except RuntimeError:
                asyncio.run(_send(bot, self.chat_id, text))
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")

    def send_trade_notification(self, trade_summary: str) -> None:
        """Send a trade notification."""
        self.send_message(f"📊 *Trade Executed*\n\n{trade_summary}")

    def send_signal_notification(self, signal_summary: str) -> None:
        """Send a signal notification."""
        self.send_message(f"📡 *Signal Detected*\n\n{signal_summary}")

    def send_alert(self, alert: str) -> None:
        """Send an important alert (always sent, even in silent mode)."""
        self.send_message(f"🚨 *ALERT*\n\n{alert}")

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
                asyncio.run_coroutine_threadsafe(_send_approval(bot), loop)
            except RuntimeError:
                asyncio.run(_send_approval(bot))

            logger.info(f"📱 Approval requested for {trade_id}")
        except Exception as e:
            logger.error(f"Failed to request approval: {e}")
