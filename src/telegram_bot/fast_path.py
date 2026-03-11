"""
Fast-path pattern table for instant Telegram responses without LLM round-trip.
"""

from __future__ import annotations

import re


# Patterns that can be answered instantly with data lookups.
# Each entry: (compiled_regex, function_name_to_call, response_template)
# The response template uses {data} as placeholder.
FAST_PATTERNS: list[tuple[re.Pattern, str, str]] = []


def _build_fast_patterns():
    """Build regex patterns for instant responses."""
    patterns = [
        # Status queries
        (r"^/status$|^status\??$|^how (are )?we doing\??$|^how's it going\??$|^update\??$",
         "get_status", None),  # None = use smart formatter
        # Balance
        (r"^/balance$|^balance\??$|^how much (do we|have).*\??$|^portfolio\??$",
         "get_balance", None),
        # Positions (bot-tracked open positions only)
        (r"^/positions?$|^positions?\??$|^what('re| are) (our )?positions?\??$|^open positions?\??$",
         "get_positions", None),
        # Prices
        (r"^/prices?$|^prices?\??$|^current prices?\??$|^what('s| is) (the )?price",
         "get_current_prices", None),
        # Recent trades
        (r"^/trades?$|^trades?\??$|^recent trades?\??$|^what did we (buy|sell|trade)",
         "get_recent_trades", None),
        # Fear & Greed
        (r"^/feargreed$|^fear.{0,5}greed\??$|^f.?g.?i\??$|^sentiment\??$|^what('s| is) (the )?(market )?(fear|sentiment)",
         "get_fear_greed", None),
        # News
        (r"^/news$|^news\??$|^what('s| is).*news",
         "get_news_summary", None),
        # Rules
        (r"^/rules?$|^rules?\??$|^what are (the|our) rules?\??$|^limits?\??$",
         "get_trading_rules", None),
        # Fees
        (r"^/fees?$|^fees?\??$|^fee info\??$|^breakeven\??$",
         "get_fee_info", None),
        # Swaps
        (r"^/swaps?$|^swaps?\??$|^pending swaps?\??$|^rotation proposals?\??$",
         "get_pending_swaps", None),
        # High-stakes status
        (r"^/highstakes\s*status$|^high.?stakes?\s*(status|mode)\??$|^(is )?high.?stakes? (on|active|enabled)\??$",
         "get_highstakes_status", None),
        # Signals
        (r"^/signals?$|^signals?\??$|^recent signals?\??$",
         "get_recent_signals", None),
        # Pause
        (r"^/pause$|^pause\s*(trading)?$",
         "pause_trading", "⏸️ Trading paused."),
        # Resume
        (r"^/resume$|^resume\s*(trading)?$",
         "resume_trading", "▶️ Trading resumed."),
        # Emergency stop
        (r"^/stop$|^stop\s*everything$|^emergency\s*stop$|^kill\s*switch$",
         "emergency_stop", "🛑 EMERGENCY STOP — all trading halted."),
        # Verbosity shortcuts
        (r"^/quiet$|^be quiet|^quiet\s*mode|^tone.*down|^less (updates?|talk)",
         "_set_verbosity_quiet", None),
        (r"^/silent$|^(be )?silent|^shut\s*up|^stfu|^don'?t talk|^no (more )?updates?",
         "_set_verbosity_silent", None),
        (r"^/chatty$|^be (more )?(chatty|talkative)|^talk (to )?me (more)?|^more updates?",
         "_set_verbosity_chatty", None),
        (r"^/verbose$|^verbose|^give me everything|^full (detail|verbosity)|^play.?by.?play",
         "_set_verbosity_verbose", None),
        (r"^(back to )?normal|^/normal$|^default (mode|verbosity)",
         "_set_verbosity_normal", None),
        # Stats & Analytics
        (r"^/stats$|^stats\??$|^performance\??$|^how did (we|I) do\??$",
         "get_stats", None),
        (r"^/history$|^trade history\??$",
         "get_trade_history", None),
        (r"^/schedules?$|^(my |active )?schedules?\??$|^(what are|show) (my )?scheduled",
         "get_schedules", None),
        (r"^best.?worst|^winners?.?losers?",
         "get_best_worst", None),
        # Account holdings (live Coinbase) — broad match for natural language
        (r"^/holdings?$|^holdings?\??$"
         r"|my\s+(current\s+)?(portfolio\s+|wallet\s+|crypto\s+|account\s+)?holdings?\b"
         r"|my\s+(current\s+)?wallet\b"
         r"|my\s+(current\s+)?portfolio\s*\?*$"
         r"|my\s+(current\s+)?crypto\s*\?*$"
         r"|what (do I|do we|i) (own|hold|have)\??$"
         r"|^show.*(holdings|account|portfolio)$"
         r"|^account overview",
         "get_account_holdings", None),
        # Simulations fast path
        (r"^/sims?$|^my simulations?\??$|^list simulations?\??$|^open simulations?\??$|^active simulations?\??$",
         "list_simulations", None),
        # Enable / disable trading
        (r"^/enable.?trading$|^enable\s*trading$|^turn on\s*trading$|^start\s*trading$",
         "enable_trading", "🟢 Trading enabled (moderate preset applied)."),
        (r"^/disable.?trading$|^disable\s*trading$|^turn off\s*trading$|^stop\s*trading$",
         "disable_trading", "🔴 Trading disabled (all limits set to zero)."),
        # Apply presets
        (r"^/preset\s+(disabled|conservative|moderate|aggressive)$"
         r"|^(set|apply|use)\s*(preset\s+)?(disabled|conservative|moderate|aggressive)$",
         "apply_preset", None),
        # Settings tiers info
        (r"^/settings.?tiers?$|^(what|which)\s+settings?\s+(can|are)\s+(I|we)?\s*(change|update|safe|allowed)",
         "get_settings_tiers", None),
        # Approve / reject trade (inline keyboard buttons & typed commands)
        (r"^/approve\s+(\S+)$|^approve\s+(?:trade\s+)?(\S+)$",
         "approve_item", None),
        (r"^/reject\s+(\S+)$|^reject\s+(?:trade\s+)?(\S+)$",
         "reject_item", None),
    ]
    for pattern_str, func_name, template in patterns:
        FAST_PATTERNS.append((
            re.compile(pattern_str, re.IGNORECASE),
            func_name,
            template,
        ))


_build_fast_patterns()
