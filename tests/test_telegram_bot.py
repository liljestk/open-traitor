"""Unit tests for the Telegram outbound rate limiter (Phase H)."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from src.telegram_bot.bot import TelegramBot


def _make_bot() -> TelegramBot:
    # Patch send_message's underlying telegram import work — we override
    # send_message's network path entirely by stubbing `_send` via the bot.
    bot = TelegramBot(
        bot_token="x",
        chat_id="123",
        authorized_users=["456"],
        mode="reporting",
        exchange_name="TEST",
    )
    return bot


class TestOutboundRateLimit:
    def test_critical_always_passes(self):
        bot = _make_bot()
        bot.set_max_messages_per_hour(2)
        # Burn through the cap with non-critical
        sent = []
        with patch.object(bot, "_get_outbound_bot"):
            bot.send_message = lambda text, critical=False: sent.append(("c" if critical else "n", text))  # type: ignore
            for _ in range(5):
                bot.send_message("noise")
            for _ in range(3):
                bot.send_message("alarm", critical=True)
        # Our stub doesn't go through _allow_outbound, so call it directly:
        bot2 = _make_bot()
        bot2.set_max_messages_per_hour(2)
        assert bot2._allow_outbound(critical=False) is True
        assert bot2._allow_outbound(critical=False) is True
        assert bot2._allow_outbound(critical=False) is False  # capped
        assert bot2._allow_outbound(critical=True) is True   # bypass

    def test_window_resets_after_hour(self):
        bot = _make_bot()
        bot.set_max_messages_per_hour(1)
        assert bot._allow_outbound(critical=False) is True
        assert bot._allow_outbound(critical=False) is False
        # Move history into the past
        bot._outbound_history = [time.time() - 4000.0]
        assert bot._allow_outbound(critical=False) is True

    def test_set_max_messages_per_hour_clamps(self):
        bot = _make_bot()
        bot.set_max_messages_per_hour(0)
        assert bot._outbound_max_per_hour == 1
        bot.set_max_messages_per_hour("not a number")  # type: ignore[arg-type]
        # Stays at last valid value
        assert bot._outbound_max_per_hour == 1


def test_empty_authorized_users_rejected():
    with pytest.raises(ValueError):
        TelegramBot(
            bot_token="x", chat_id="1", authorized_users=[], mode="reporting"
        )
