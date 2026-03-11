"""
LLM-powered conversational handler for OpenTraitor's Telegram interface.

DESIGN PHILOSOPHY:
  This bot should feel like talking to a SHARP PRO TRADER — fast, opinionated,
  proactive, always on top of the market. NOT a chatbot. A trader with a terminal.

SPEED ARCHITECTURE:
  1. FAST PATH — Simple queries (status, balance, prices) are pattern-matched
     and answered INSTANTLY with formatted data. No LLM round-trip.
  2. SMART PATH — Complex messages (natural language, tasks, opinions) go
     through the LLM for interpretation + response in a SINGLE call.
  3. PROACTIVE ENGINE — Background thread that monitors for events worth
     sharing: price movements, trade executions, daily briefings, etc.

The bot should feel INSTANT for data queries and THOUGHTFUL for complex ones.

Module structure:
  - persona.py     — PRO_TRADER_PERSONA, PersonalityConfig, ConversationMemory
  - fast_path.py   — FAST_PATTERNS regex table
  - formatters.py  — DATA_FORMATTERS (10 smart formatters)
  - proactive.py   — ProactiveEngine background thread
  - chat_handler.py (this file) — TelegramChatHandler (the brain)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from src.utils.logger import get_logger
from src.utils.security import sanitize_input
from src.telegram_bot.tools import ToolDef, BUILTIN_TOOL_REGISTRY

# Extracted modules
from src.telegram_bot.persona import (
    PRO_TRADER_PERSONA,
    build_persona,
    PersonalityConfig,
    ConversationMemory,
)
from src.telegram_bot.fast_path import FAST_PATTERNS
from src.telegram_bot.formatters import DATA_FORMATTERS
from src.telegram_bot.proactive import ProactiveEngine

logger = get_logger("telegram.chat")


# ============================================================================
# Chat Handler — the brain
# ============================================================================

class TelegramChatHandler:
    """
    LLM-powered conversational handler with FAST PATH for instant responses.

    Architecture:
      1. Message comes in
      2. Try FAST PATH (regex match → data lookup → formatted response) — <50ms
      3. If no fast match → SMART PATH (single LLM call) — ~3-8s
      4. Background: ProactiveEngine sends updates autonomously
    """

    def __init__(self, llm_client, rate_limiter=None, exchange_type: str = "coinbase"):
        self.llm = llm_client
        self.rate_limiter = rate_limiter
        self.exchange_type = exchange_type
        self._persona = build_persona(exchange_type)
        self.personality = PersonalityConfig()
        self.memory = ConversationMemory(max_messages=30)

        # Function handlers — connected by the orchestrator
        self._function_handlers: dict[str, Callable] = {}
        # Tool schemas (ToolDef) — used for native tool calling + rich prompt docs
        self._tool_defs: dict[str, ToolDef] = {}

        # Proactive engine (started after send_callback is set)
        self._proactive: Optional[ProactiveEngine] = None
        self._send_callback: Optional[Callable] = None

        logger.info("🧠 Chat handler initialized (fast-path + native tool-calling enabled)")

    def register_function(self, name: str, handler: Callable, tool_def: Optional[ToolDef] = None) -> None:
        """Register a callable function + optional schema for tool calling.

        If tool_def is not provided, the built-in registry is checked automatically
        so callers don't need to import ToolDefs explicitly for standard tools.
        """
        self._function_handlers[name] = handler
        if tool_def is not None:
            self._tool_defs[name] = tool_def
        elif name in BUILTIN_TOOL_REGISTRY:
            self._tool_defs[name] = BUILTIN_TOOL_REGISTRY[name]

    def set_send_callback(self, callback: Callable) -> None:
        self._send_callback = callback
        # Create and start proactive engine
        self._proactive = ProactiveEngine(callback, self.llm, self.personality, exchange_type=self.exchange_type)
        self._proactive.start()

    def set_context_provider(self, provider: Callable) -> None:
        """Connect the proactive engine to trading state."""
        if self._proactive:
            self._proactive.set_context_provider(provider)

    def queue_event(self, event: str, severity: str = "info", pair: str | None = None) -> None:
        """Queue event for proactive engine."""
        if self._proactive:
            self._proactive.queue_event(event, severity=severity, pair=pair)
        self.memory.add("system", f"[EVENT] {event}")

    # ────────────────────────────────────────────────────────────────────────
    # Main message handler
    # ────────────────────────────────────────────────────────────────────────

    async def handle_message(self, text: str, user_name: str = "Owner", user_id: str = "") -> str:
        """
        Handle any incoming message. Tries fast path first, falls back to LLM.
        """
        text = sanitize_input(text, max_length=1000)
        if not text:
            return "👀"

        self.memory.add("user", text)

        # ─── FAST PATH: pattern match → instant response ───
        fast_response = self._try_fast_path(text)
        if fast_response is not None:
            logger.debug(f"⚡ Fast path hit for: {text[:60]!r}")
            self.memory.add("assistant", fast_response)
            return fast_response

        # ─── SMART PATH: single LLM call ───
        try:
            if self.rate_limiter:
                await self.rate_limiter.async_wait("ollama")

            response = await self._smart_response(text, user_name)
            self.memory.add("assistant", response)
            return response

        except Exception as e:
            logger.error(f"Chat handler error: {e}", exc_info=True)
            fallback = "⚡ Processing hiccup — still trading though. Try again?"
            self.memory.add("assistant", fallback)
            return fallback

    # ────────────────────────────────────────────────────────────────────────
    # Fast Path
    # ────────────────────────────────────────────────────────────────────────

    def _try_fast_path(self, text: str) -> Optional[str]:
        """Try to answer instantly without LLM. Returns None if no match."""
        text_clean = text.strip()

        for pattern, func_name, template in FAST_PATTERNS:
            m = pattern.search(text_clean)
            if m:
                # Special: extract preset name from the regex match groups
                if func_name == "apply_preset":
                    preset = None
                    for g in m.groups():
                        if g and g.lower() in ("disabled", "conservative", "moderate", "aggressive"):
                            preset = g.lower()
                            break
                    if not preset:
                        return "⚠️ Please specify a preset: disabled, conservative, moderate, or aggressive."
                    return self._execute_fast_with_args(func_name, {"preset": preset})
                # Approve/reject: extract trade ID from regex groups
                if func_name in ("approve_item", "reject_item"):
                    item_id = next((g for g in m.groups() if g), None)
                    if not item_id:
                        return "⚠️ Please specify a trade ID."
                    result = self._execute_fast_with_args(func_name, {"item_id": item_id})
                    verb = "approved" if func_name == "approve_item" else "rejected"
                    if isinstance(result, str):
                        return result
                    if isinstance(result, dict) and result.get("ok"):
                        return f"✅ Trade **{item_id}** {verb}."
                    err = result.get("error", "Unknown error") if isinstance(result, dict) else str(result)
                    return f"⚠️ Could not {verb.rstrip('d')} trade: {err}"
                return self._execute_fast(func_name, template)

        # ── Contextual affirmatives ──────────────────────────────────────────
        if re.match(
            r"^(yes|yep|yea|yeah|sure|do it|go ahead|please|y|ja|absolutely|"
            r"show me|fetch it|go for it|let'?s do it|affirmative)\s*[.!?]*\s*$",
            text_clean,
            re.IGNORECASE,
        ):
            resolved = self._resolve_contextual_yes()
            if resolved is not None:
                return resolved

        # Quick ack patterns (no data needed)
        ack_patterns = {
            r"^(ok|okay|k|cool|nice|thanks|thx|ty|got it|roger|👍|great|perfect)\s*!?\s*$":
                None,
        }
        for pat, _ in ack_patterns.items():
            if re.match(pat, text_clean, re.IGNORECASE):
                return "👍"

        return None

    def _resolve_contextual_yes(self) -> Optional[str]:
        """
        Look at the last assistant message to figure out what "yes" means.
        Returns a fast-path style response, or None to fall through to LLM.
        """
        recent = self.memory.get_recent(4)
        last_bot_msg = ""
        for msg in reversed(recent):
            if msg["role"] == "assistant":
                last_bot_msg = msg["content"].lower()
                break

        if not last_bot_msg:
            return None

        _CONTEXT_MAP: list[tuple[list[str], str]] = [
            (["holdings", "portfolio", "what you own", "what you hold",
              "check your portfolio", "summary of your", "current holdings",
              "wallet", "account"],
             "get_account_holdings"),
            (["balance", "how much", "cash"],
             "get_balance"),
            (["positions", "open position"],
             "get_positions"),
            (["price", "prices", "how much is"],
             "get_current_prices"),
            (["trade history", "recent trades"],
             "get_recent_trades"),
            (["fear", "greed", "sentiment"],
             "get_fear_greed"),
            (["news"],
             "get_news_summary"),
            (["stats", "performance", "how did"],
             "get_stats"),
            (["high stakes", "high-stakes", "highstakes"],
             "get_highstakes_status"),
        ]

        for keywords, func_name in _CONTEXT_MAP:
            for kw in keywords:
                if kw in last_bot_msg:
                    handler = self._function_handlers.get(func_name)
                    if handler:
                        return self._execute_fast(func_name, None)
                    break

        return None

    def _execute_fast(self, func_name: str, template: Optional[str]) -> str:
        """Execute a fast-path function and format the result."""

        # Handle verbosity shortcuts
        verbosity_map = {
            "_set_verbosity_quiet": ("quiet", "🤫 Going quiet — trades and alerts only."),
            "_set_verbosity_silent": ("silent", "🔇 Silent mode. Only emergencies."),
            "_set_verbosity_chatty": ("chatty", "📢 Chatty mode! I'll keep you posted on everything."),
            "_set_verbosity_verbose": ("verbose", "📋 Full verbosity — play-by-play of every decision."),
            "_set_verbosity_normal": ("normal", "👍 Back to normal updates."),
        }

        if func_name in verbosity_map:
            level, msg = verbosity_map[func_name]
            self.personality.set_verbosity(level)
            return msg

        # Execute the registered function
        handler = self._function_handlers.get(func_name)
        if not handler:
            return f"⚙️ {func_name} not connected yet."

        try:
            data = handler({})
        except Exception as e:
            return f"⚠️ Error getting data: {str(e)[:100]}"

        # Use template if provided
        if template is not None:
            return template

        # Use smart formatter
        formatter = DATA_FORMATTERS.get(func_name)
        if formatter:
            try:
                return formatter(data)
            except Exception as e:
                logger.debug(f"Formatter {func_name} failed: {e}")

        # Fallback: dump as readable text
        if isinstance(data, dict):
            return f"```\n{json.dumps(data, indent=2, default=str)[:2000]}\n```"
        return str(data)[:2000]

    def _execute_fast_with_args(self, func_name: str, args: dict) -> str:
        """Like _execute_fast but passes args to the handler."""
        handler = self._function_handlers.get(func_name)
        if not handler:
            return f"⚙️ {func_name} not connected yet."
        try:
            data = handler(args)
        except Exception as e:
            return f"⚠️ Error: {str(e)[:100]}"
        if isinstance(data, dict):
            if data.get("ok"):
                preset = args.get("preset", "")
                return f"✅ Preset **{preset}** applied! Changes: {', '.join(f'`{k}`' for k in data.get('changes', {}).keys()) or 'none'}"
            return f"⚠️ {data.get('error', 'Unknown error')}"
        return str(data)[:2000]

    # ────────────────────────────────────────────────────────────────────────
    # Smart Path — single LLM call for complex messages
    # ────────────────────────────────────────────────────────────────────────

    def _build_openai_tools(self) -> list[dict]:
        """Return OpenAI-format tool schemas for all registered functions that have a ToolDef."""
        return [
            td.to_openai_schema()
            for name, td in self._tool_defs.items()
            if name in self._function_handlers
        ]

    def _execute_tool_call(self, name: str, arguments: dict) -> Any:
        """Execute a single named tool call and return its result.

        Only tools in the safe allowlist may be invoked via the tool-calling path.
        Mutating tools are blocked to prevent prompt-injection attacks.
        """
        if name not in self._TEXT_FALLBACK_SAFE_ACTIONS:
            logger.warning(
                f"Blocked tool-calling invocation of mutating tool: {name} "
                f"(only read-only tools allowed via LLM tool calls)"
            )
            return {"error": f"Tool '{name}' is read-only restricted and cannot be called via chat"}

        handler = self._function_handlers.get(name)
        if not handler:
            return {"error": f"Unknown tool: {name}"}
        try:
            return handler(arguments)
        except Exception as e:
            logger.error(f"Tool execution error [{name}]: {e}", exc_info=True)
            return {"error": str(e)}

    def _build_system_prompt(self, user_name: str, quick_data: str, conv: str) -> str:
        """Build the base system prompt for the smart path."""
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return (
            f"{self._persona}\n\n"
            f"{self.personality.to_prompt_fragment()}\n\n"
            f"You're chatting with {user_name} on Telegram.\n\n"
            f"━━━ LIVE DATA (fetched {now_ts} — USE ONLY THESE NUMBERS) ━━━\n"
            f"{quick_data}\n"
            f"━━━ END LIVE DATA ━━━\n\n"
            f"{'Recent conversation:' + chr(10) + conv + chr(10) + chr(10) if conv else ''}"
            f"Current time: {now_ts}"
        )

    async def _smart_response(self, text: str, user_name: str) -> str:
        """
        Handle complex messages using native LLM tool calling.
        Tries tool-calling first, falls back to ACTION: text parsing.
        """
        quick_data = self._get_quick_snapshot()
        recent = self.memory.get_recent(6)
        conv = "\n".join(
            f"{'You' if m['role'] == 'user' else 'Me'}: {m['content'][:150]}"
            for m in recent[:-1]
        )
        system_prompt = self._build_system_prompt(user_name, quick_data, conv)
        conv_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in recent[:-1]
        ]

        # ── Native tool calling ──────────────────────────────────────────────
        tools = self._build_openai_tools()
        if tools:
            try:
                return await self._smart_response_with_tools(
                    text, system_prompt, conv_messages, tools
                )
            except Exception as e:
                logger.warning(
                    f"Native tool calling failed ({e!r}), falling back to text path"
                )

        # ── Text fallback (legacy ACTION: parsing) ──────────────────────────
        return await self._smart_response_text(text, system_prompt)

    async def _smart_response_with_tools(
        self,
        text: str,
        system_prompt: str,
        conv_messages: list[dict],
        tools: list[dict],
    ) -> str:
        """
        Tool-calling smart path.
        Step 1 — let the model decide what to call.
        Step 2 — execute all tool calls in order.
        Step 3 — send results back and get the final trader-voiced response.
        """
        # ── Step 1: initial call ─────────────────────────────────────────────
        text_content, tool_calls, assistant_msg = await self.llm.chat_with_tools(
            system_prompt=system_prompt,
            user_message=text,
            tools=tools,
            messages=conv_messages,
            temperature=0.5,
            max_tokens=600,
            agent_name="telegram_chat",
        )

        # Model responded with no tool calls — just return the text.
        if not tool_calls:
            _PRICE_KEYWORDS = (
                "price", "worth", "cost", "value", "market", "trading at",
                "how much", "bitcoin", "btc", "eth", "ethereum", "atom",
                "sol", "ada", "bnb", "xrp", "coin", "crypto",
            )
            if any(kw in text.lower() for kw in _PRICE_KEYWORDS):
                price_handler = self._function_handlers.get("get_current_prices")
                if price_handler:
                    try:
                        fresh = price_handler({})
                        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        enriched_system = (
                            system_prompt
                            + f"\n\n━━━ FRESHLY FETCHED PRICES ({now_ts}) ━━━\n"
                            + json.dumps(fresh, default=str)
                            + "\n━━━ USE ONLY THESE PRICES — NOT TRAINING DATA ━━━"
                        )
                        final_text, _, _ = await self.llm.chat_with_tools(
                            system_prompt=enriched_system,
                            user_message=text,
                            tools=tools,
                            messages=conv_messages,
                            temperature=0.5,
                            max_tokens=600,
                            agent_name="telegram_chat",
                        )
                        logger.debug("Re-answered with live price injection")
                        return final_text or "👍"
                    except Exception as e:
                        logger.debug(f"Auto price inject failed: {e}")

            response = self._parse_and_execute_actions(text_content or "")
            return response or "👍"

        # ── Step 2: execute tool calls ───────────────────────────────────────
        tool_result_messages: list[dict] = []
        for tc in tool_calls:
            result = self._execute_tool_call(tc["name"], tc["arguments"])
            logger.info(f"🔧 Tool called: {tc['name']}({tc['arguments']}) → {str(result)[:120]}")
            tool_result_messages.append({
                "role": "tool",
                "content": json.dumps(result, default=str),
                "tool_call_id": tc.get("id", tc["name"]),
            })

        # ── Step 3: summarise results ────────────────────────────────────────
        continuation_messages = (
            conv_messages
            + [{"role": "user", "content": text}]
            + [assistant_msg]
            + tool_result_messages
        )

        final_text, remaining_calls, _ = await self.llm.chat_with_tools(
            system_prompt=system_prompt,
            user_message="",
            tools=tools,
            messages=continuation_messages,
            temperature=0.5,
            max_tokens=600,
            agent_name="telegram_chat",
        )

        if remaining_calls:
            for tc in remaining_calls:
                result = self._execute_tool_call(tc["name"], tc["arguments"])
                logger.info(f"🔧 (chained) Tool called: {tc['name']} → {str(result)[:80]}")

        return final_text or "Done."

    async def _smart_response_text(self, text: str, system_prompt: str) -> str:
        """
        Legacy text-based smart path used as fallback.
        Prompts the LLM to embed ACTION: lines which are then parsed and executed.
        """
        tool_docs_lines = ["AVAILABLE TOOLS (use ACTION: syntax below to call them):"]
        categories: dict[str, list[str]] = {}
        for name, td in sorted(self._tool_defs.items()):
            if name in self._function_handlers:
                cat = td.category
                categories.setdefault(cat, [])
                categories[cat].extend(td.to_prompt_lines())
        for cat, lines in categories.items():
            tool_docs_lines.append(f"\n[{cat.upper()}]")
            tool_docs_lines.extend(lines)

        bare = sorted(
            n for n in self._function_handlers
            if n not in self._tool_defs
        )
        if bare:
            tool_docs_lines.append("\n[OTHER]")
            for n in bare:
                tool_docs_lines.append(f"• {n}")

        tool_docs = "\n".join(tool_docs_lines)

        action_prompt = (
            f"{system_prompt}\n\n"
            f"{tool_docs}\n\n"
            "RESPONSE FORMAT:\n"
            "- Respond naturally as a trader.\n"
            "- To call a tool, put it on its own line: ACTION:tool_name|param=value\n"
            "- Examples:\n"
            "    ACTION:enable_highstakes|duration=4h\n"
            "    ACTION:update_rule|param=max_single_trade|value=300\n"
            "    ACTION:get_stats|hours=48\n"
        )

        raw = await self.llm.chat(
            system_prompt=action_prompt,
            user_message=text,
            temperature=0.5,
            max_tokens=600,
            agent_name="telegram_chat",
        )
        return self._parse_and_execute_actions(raw)

    # Actions the text-fallback ACTION: parser is allowed to invoke.
    _TEXT_FALLBACK_SAFE_ACTIONS: frozenset[str] = frozenset({
        # Read-only data retrieval
        "get_status", "get_positions", "get_balance", "get_current_prices",
        "get_account_holdings", "get_recent_trades", "get_recent_signals",
        "get_news_summary", "get_fear_greed", "get_trading_rules",
        "get_fee_info", "get_pending_swaps", "get_highstakes_status",
        "get_rotation_analysis", "get_stats", "get_trade_history",
        "get_pair_stats", "get_daily_summaries", "get_best_worst",
        "get_schedules", "get_config", "get_trailing_stops",
        "get_settings_tiers", "list_simulations",
        # Harmless personality tweaks
        "set_verbosity", "mute_topic", "unmute_topic",
    })

    def _parse_and_execute_actions(self, raw_response: str) -> str:
        """Extract ACTION: lines, execute them, and return clean response."""
        lines = raw_response.split("\n")
        clean_lines = []
        action_results = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("ACTION:"):
                result = self._execute_action_line(stripped)
                if result:
                    action_results.append(result)
            else:
                clean_lines.append(line)

        response = "\n".join(clean_lines).strip()

        if action_results:
            response += "\n\n" + "\n".join(action_results)

        return response if response else "👍 Done."

    def _execute_action_line(self, action_line: str) -> Optional[str]:
        """Parse and execute an ACTION:function_name|params line."""
        try:
            action_part = action_line[7:]  # Remove "ACTION:"
            parts = action_part.split("|", 1)
            func_name = parts[0].strip()
            params = {}

            if len(parts) > 1:
                for kv in parts[1].split("|"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        params[k.strip()] = v.strip()

            # Handle verbosity changes (safe — personality only)
            if func_name.startswith("set_verbosity"):
                level = params.get("level", func_name.split("_")[-1] if "_" in func_name else "normal")
                self.personality.set_verbosity(level)
                return None

            if func_name == "mute_topic":
                self.personality.muted_topics.add(params.get("topic", ""))
                return None

            if func_name == "unmute_topic":
                self.personality.muted_topics.discard(params.get("topic", ""))
                return None

            # Block mutating actions from text-fallback path
            if func_name not in self._TEXT_FALLBACK_SAFE_ACTIONS:
                logger.warning(
                    f"Blocked text-fallback ACTION for mutating tool: {func_name} "
                    f"(only read-only tools allowed via ACTION: parser)"
                )
                return None

            handler = self._function_handlers.get(func_name)
            if handler:
                handler(params)
                return None
            else:
                logger.warning(f"Unknown action: {func_name}")
                return None

        except Exception as e:
            logger.error(f"Action execution failed: {e}")
            return f"⚠️ Action failed: {str(e)[:100]}"

    def _get_quick_snapshot(self) -> str:
        """Build a grounded, timestamped state snapshot for the LLM."""
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        sections: dict[str, Any] = {"fetched_at": now_ts}

        priority_tools = [
            "get_status",
            "get_current_prices",
            "get_fear_greed",
            "get_highstakes_status",
            "get_positions",
        ]
        for func_name in priority_tools:
            handler = self._function_handlers.get(func_name)
            if handler:
                try:
                    data = handler({})
                    sections[func_name] = data
                except Exception as e:
                    sections[func_name] = {"error": str(e)}

        return json.dumps(sections, default=str)

    # ────────────────────────────────────────────────────────────────────────
    # Legacy compatibility
    # ────────────────────────────────────────────────────────────────────────

    def should_send_proactive_update(self) -> bool:
        """Legacy compat — proactive engine handles this now."""
        return False

    def generate_proactive_update(self, trading_context: dict) -> Optional[str]:
        """Legacy compat — proactive engine handles this now."""
        return None

    def generate_daily_plan(self, trading_context: dict) -> Optional[str]:
        """Legacy compat — proactive engine handles daily plans."""
        return None
