"""
LLM Client wrapper with multi-provider fallback chain.

Supports an ordered list of providers (e.g. Gemini -> OpenRouter -> Ollama).
Each provider uses the OpenAI-compatible API. On rate-limit or quota errors
the client automatically falls through to the next provider in the chain.

Smart free-tier management:
  - Tracks per-provider RPM, daily token budgets, and cooldowns.
  - OpenRouter: periodically checks remaining free credits via /api/v1/auth/key.
  - Gemini: respects 10 RPM / 200K token / 20 RPD free limits (gemini-2.5-flash-lite).
  - Automatic recovery polling re-enables providers after cooldown / day rollover.

Provider config, LLMProvider dataclass, and build_providers() live in
llm_providers.py. Re-exported here for backward compatibility.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from datetime import date as dt_date
from typing import Any, Optional, TYPE_CHECKING

from openai import AsyncOpenAI, RateLimitError, APIStatusError

from src.utils.logger import get_logger

# Re-export provider infrastructure for backward compatibility
from src.core.llm_providers import (  # noqa: F401
    LLMProvider,
    build_providers,
    check_openrouter_credits,
    OPENROUTER_FREE_MODELS,
    _resolve_env,
)

if TYPE_CHECKING:
    from src.utils.tracer import SpanContext

logger = get_logger("core.llm")


# ─── LLMClient ─────────────────────────────────────────────────────────────────

class LLMClient:
    """
    Multi-provider LLM client with automatic fallback chain.

    Tries providers in order. On rate-limit/quota errors, activates a cooldown
    on that provider and falls through to the next one. Local providers
    (Ollama) are always available as the final fallback.

    Tier-aware smart routing:
      - "paid" tier: all calls try cloud first (original behaviour).
      - "free" tier: only high-priority calls (strategy, risk, telegram) try
        cloud first; normal/low-priority calls prefer local, with cloud as
        fallback only if local fails.
    """

    # Agent name → priority mapping. Unknown agents default to "normal".
    AGENT_PRIORITIES: dict[str, str] = {
        "strategist":         "high",    # trade decisions – accuracy matters
        "risk_manager":       "high",    # risk gating – correctness critical
        "portfolio_rotator":  "high",    # portfolio-level rebalancing
        "telegram_chat":      "high",    # user-facing interactive chat
        "market_analyst":     "normal",  # runs per-pair each cycle, high volume
        "executor":           "normal",  # mostly deterministic, LLM rarely used
        "settings_advisor":   "low",     # periodic, non-urgent
    }

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama3.1:8b",
        temperature: float = 0.2,
        max_tokens: int = 2000,
        max_retries: int = 1,
        timeout: int = 45,
        persona: str = "",
        providers: Optional[list[LLMProvider]] = None,
    ):
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.timeout = timeout
        self.persona = persona

        # Stored config for rescan/recovery polling
        self._providers_config: list[dict] = []
        self._fallback_base_url = base_url
        self._fallback_model = model
        self._fallback_timeout = timeout
        self._fallback_max_retries = max_retries
        self._recovery_task: Optional[asyncio.Task] = None
        self._recovery_interval: float = 120.0  # seconds

        if providers:
            self._providers = providers
        else:
            # Backward compat: single Ollama provider
            ollama_url = f"{base_url.rstrip('/')}/v1"
            client = AsyncOpenAI(
                base_url=ollama_url,
                api_key="ollama",
                timeout=timeout,
                max_retries=max_retries,
            )
            self._providers = [LLMProvider(
                name="ollama", client=client, model=model, is_local=True,
            )]

        # Legacy fields kept for backward compat
        self.model = self._providers[0].model
        self.client = self._providers[0].client

        self._call_count = 0
        self._total_tokens = 0
        self._last_provider = ""
        self._providers_lock = threading.RLock()
        self._interaction_callback = None  # set by TrainingDataCollector

        names = [p.name for p in self._providers]
        logger.info(
            f"✅ LLM Client initialized | "
            f"chain: {' → '.join(names)} | "
            f"primary: {self._providers[0].name}/{self._providers[0].model}"
        )

    # ── Provider availability ─────────────────────────────────────────────

    def _is_provider_available(self, p: LLMProvider) -> bool:
        """Check if a provider is available for the next call."""
        if p.is_local:
            return True

        now = time.monotonic()

        with p._lock:
            # Cooldown check
            if now < p.cooldown_until:
                return False

            # Day rollover — reset daily counters
            today = dt_date.today().isoformat()
            if p.daily_date != today:
                p.daily_tokens = 0
                p.daily_requests = 0
                p.daily_date = today

            # Daily token budget
            if p.daily_token_limit > 0 and p.daily_tokens >= p.daily_token_limit:
                return False

            # Daily request budget
            if p.daily_request_limit > 0 and p.daily_requests >= p.daily_request_limit:
                return False

            # RPM check
            if p.rpm_limit > 0:
                now_wall = time.time()
                cutoff = now_wall - 60.0
                p.rpm_timestamps = [t for t in p.rpm_timestamps if t > cutoff]
                if len(p.rpm_timestamps) >= p.rpm_limit:
                    return False
                # Per-second spacing to avoid bursts
                # Gemini free tier: enforce 60/rpm_limit spacing (e.g. 8 RPM → 7.5s)
                # OpenRouter: ~3s minimum spacing
                if p.rpm_timestamps:
                    if p.name == "gemini" and p.tier == "free":
                        min_spacing = 60.0 / max(p.rpm_limit, 1) + 1.0  # e.g. 8.5s for 8 RPM
                    else:
                        min_spacing = 3.0
                    if (now_wall - p.rpm_timestamps[-1]) < min_spacing:
                        return False

        return True

    def _record_call(self, p: LLMProvider, total_tokens: int) -> None:
        """Record a successful call for rate/quota tracking."""
        if p.is_local:
            return
        with p._lock:
            p.rpm_timestamps.append(time.time())
            today = dt_date.today().isoformat()
            if p.daily_date != today:
                p.daily_tokens = 0
                p.daily_requests = 0
                p.daily_date = today
            p.daily_tokens += total_tokens
            p.daily_requests += 1

            # Reset consecutive 429 counter on success
            if p._consecutive_429s > 0:
                p._consecutive_429s = 0

            # Warn when approaching daily limits (Gemini free tier)
            if p.name == "gemini" and p.tier == "free":
                if p.daily_request_limit > 0:
                    remaining_reqs = p.daily_request_limit - p.daily_requests
                    if remaining_reqs <= 5:
                        logger.warning(
                            f"⚠️ Gemini daily requests: {p.daily_requests}/{p.daily_request_limit} "
                            f"({remaining_reqs} remaining)"
                        )
                if p.daily_token_limit > 0:
                    token_pct = p.daily_tokens / p.daily_token_limit * 100
                    if token_pct >= 80:
                        logger.warning(
                            f"⚠️ Gemini daily tokens: {p.daily_tokens:,}/{p.daily_token_limit:,} "
                            f"({token_pct:.0f}%)"
                        )

    def _activate_cooldown(self, p: LLMProvider, reason: str) -> None:
        """Put a provider on cooldown after a rate-limit or quota error.

        For OpenRouter on free tier: rotate to next free model before cooldown,
        so we can keep trying different free models.

        For Gemini on free tier: use escalating cooldown based on consecutive
        429 errors — each successive 429 doubles the cooldown (capped at 30 min).
        This prevents hammering the API when daily limits are near exhaustion.
        """
        cooldown_secs = p.cooldown_seconds

        # Gemini free tier: escalate cooldown on consecutive 429s
        if p.name == "gemini" and p.tier == "free":
            with p._lock:
                p._consecutive_429s += 1
                # Exponential backoff: base * 2^(n-1), capped at 30 minutes
                escalation = min(2 ** (p._consecutive_429s - 1), 10)
                cooldown_secs = min(int(cooldown_secs * escalation), 1800)

                # If we're near daily limits, go into long cooldown
                if p.daily_request_limit > 0 and p.daily_requests >= p.daily_request_limit - 2:
                    cooldown_secs = 3600  # 1 hour — effectively done for the day
                    logger.warning(
                        f"🛑 Gemini daily request limit nearly exhausted "
                        f"({p.daily_requests}/{p.daily_request_limit}), "
                        f"long cooldown {cooldown_secs}s"
                    )

        with p._lock:
            p.cooldown_until = time.monotonic() + cooldown_secs

        # OpenRouter free-model rotation: if one free model is rate-limited,
        # try the next one before giving up entirely
        if p.name.startswith("openrouter") and p.tier == "free":
            self._rotate_openrouter_model(p, reason)

        logger.warning(
            f"⏸️ Provider '{p.name}' cooldown ({cooldown_secs}s): {reason}"
        )

    def _rotate_openrouter_model(self, p: LLMProvider, reason: str) -> None:
        """Rotate an OpenRouter provider to the next free model.

        If all free models have been tried, stay on cooldown.
        """
        with p._lock:
            old_model = p.model
            if old_model.endswith(":free") or old_model in OPENROUTER_FREE_MODELS:
                p._free_model_index = (p._free_model_index + 1) % len(OPENROUTER_FREE_MODELS)
                new_model = OPENROUTER_FREE_MODELS[p._free_model_index]
                if new_model != old_model:
                    p.model = new_model
                    # Clear cooldown since we're trying a different model
                    p.cooldown_until = 0.0
                    logger.info(
                        f"🔄 OpenRouter: rotated free model {old_model} → {new_model} "
                        f"(reason: {reason[:80]})"
                    )

    async def check_openrouter_credits_cached(self, p: LLMProvider) -> Optional[dict]:
        """Check OpenRouter credits with a 5-minute cache.

        Returns the credit info dict or None if not an OpenRouter provider.
        """
        if not p.name.startswith("openrouter"):
            return None

        now = time.monotonic()
        with p._lock:
            if now - p._credits_checked_at < 300:  # 5 min cache
                return {"ok": True, "credits_remaining": p._credits_remaining}

        # Fetch fresh data
        api_key = p.client.api_key
        if not api_key or api_key == "ollama":
            return None

        info = await check_openrouter_credits(api_key)
        if info.get("ok"):
            with p._lock:
                p._credits_remaining = info.get("credits_remaining")
                p._credits_checked_at = now
            remaining = info.get("credits_remaining")
            is_free = info.get("is_free_tier", False)
            logger.debug(
                f"OpenRouter credits: remaining={remaining}, "
                f"free_tier={is_free}, usage={info.get('usage', 0)}"
            )
        return info

    def _select_providers(
        self, agent_name: Optional[str] = None, priority: Optional[str] = None,
    ) -> list[LLMProvider]:
        """Return the provider list, filtered by call priority.

        Priority-aware routing:
          - Providers with ``reserve_for_priority`` set (e.g. "high") are only
            included in the chain when the call's priority matches.  This
            preserves rate-limited providers (like free-tier Gemini) for the
            calls where quality matters most (strategist, risk_manager,
            telegram_chat).
          - For normal / low priority calls the reserved provider is skipped
            entirely, so the chain typically becomes: OpenRouter → Ollama.

        Args:
            agent_name: the calling agent (mapped via AGENT_PRIORITIES).
            priority:   explicit override — if set, agent_name mapping is ignored.
        """
        effective_priority = priority or self.AGENT_PRIORITIES.get(
            agent_name or "", "normal"
        )

        # Priority hierarchy for matching: high > normal > low
        _PRIORITY_RANK = {"high": 3, "normal": 2, "low": 1}
        call_rank = _PRIORITY_RANK.get(effective_priority, 2)

        with self._providers_lock:
            all_providers = list(self._providers)

        result: list[LLMProvider] = []
        for p in all_providers:
            if p.reserve_for_priority:
                # Only include this provider if the call meets the reservation
                required_rank = _PRIORITY_RANK.get(p.reserve_for_priority, 2)
                if call_rank < required_rank:
                    continue
            result.append(p)

        # Safety: always include at least one provider (local fallback)
        if not result:
            for p in all_providers:
                if p.is_local:
                    result.append(p)
                    break
            if not result:
                result = all_providers  # shouldn't happen, but don't break

        if logger.isEnabledFor(10):  # DEBUG
            names = [p.name for p in result]
            skipped = [p.name for p in all_providers if p not in result]
            if skipped:
                logger.debug(
                    f"Provider chain for {agent_name or 'unknown'} "
                    f"(priority={effective_priority}): "
                    f"{' → '.join(names)} (skipped: {', '.join(skipped)})"
                )

        return result

    @staticmethod
    def _is_rate_or_quota_error(exc: Exception) -> bool:
        """Check if an exception is a rate-limit or quota error."""
        if isinstance(exc, RateLimitError):
            return True
        if isinstance(exc, APIStatusError):
            if exc.status_code == 429:
                return True
            if exc.status_code in (400, 402, 403):
                msg = str(exc).lower()
                if any(kw in msg for kw in ("quota", "resource", "exhausted", "billing")):
                    return True
        return False

    # ── Raw API calls ─────────────────────────────────────────────────────

    async def _do_chat(
        self,
        provider: LLMProvider,
        system_prompt: str,
        user_message: str,
        temperature: float,
        max_tokens: int,
    ) -> Any:
        """Raw chat completion against a specific provider."""
        full_system = system_prompt
        if self.persona:
            full_system = f"{self.persona}\n\n{system_prompt}"

        return await provider.client.chat.completions.create(
            model=provider.model,
            messages=[
                {"role": "system", "content": full_system},
                {"role": "user", "content": user_message},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def _do_chat_with_tools(
        self,
        provider: LLMProvider,
        messages: list[dict],
        tools: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> Any:
        """Raw chat completion with tools against a specific provider."""
        return await provider.client.chat.completions.create(
            model=provider.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice="auto",
        )

    # ── Public methods ────────────────────────────────────────────────────

    async def chat(
        self,
        system_prompt: str,
        user_message: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        span: Optional["SpanContext"] = None,
        agent_name: Optional[str] = None,
        priority: Optional[str] = None,
    ) -> str:
        """Send a chat completion request, trying providers in chain order."""
        start_time = time.time()
        temp = temperature or self.temperature
        tokens = max_tokens or self.max_tokens
        last_error: Optional[Exception] = None
        providers = self._select_providers(agent_name=agent_name, priority=priority)

        for provider in providers:
            if not self._is_provider_available(provider):
                continue

            try:
                response = await self._do_chat(
                    provider, system_prompt, user_message, temp, tokens,
                )

                # Success — record metrics
                elapsed_ms = (time.time() - start_time) * 1000
                with self._providers_lock:
                    self._call_count += 1
                    self._last_provider = provider.name

                prompt_tokens = 0
                completion_tokens = 0
                if response.usage:
                    prompt_tokens = response.usage.prompt_tokens or 0
                    completion_tokens = response.usage.completion_tokens or 0
                    total = response.usage.total_tokens or 0
                    with self._providers_lock:
                        self._total_tokens += total
                    self._record_call(provider, total)

                content = (response.choices[0].message.content or "").strip()

                _agent_label = f" ({agent_name})" if agent_name else ""
                logger.debug(
                    f"LLM #{self._call_count}{_agent_label} | "
                    f"provider={provider.name} | {elapsed_ms:.0f}ms | "
                    f"tokens={prompt_tokens}+{completion_tokens}"
                )

                if span is not None:
                    span.finish(
                        output=content,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        latency_ms=elapsed_ms,
                        model=f"{provider.name}/{provider.model}",
                    )

                # Fire-and-forget callback for training data collection
                if self._interaction_callback is not None:
                    try:
                        self._interaction_callback(
                            agent_name=agent_name or "",
                            system_prompt=system_prompt,
                            user_message=user_message,
                            response_text=content,
                            provider=provider.name,
                            model=provider.model,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            latency_ms=elapsed_ms,
                            temperature=temp,
                        )
                    except Exception:
                        pass  # never break LLM flow

                return content

            except Exception as e:
                last_error = e
                if self._is_rate_or_quota_error(e):
                    self._activate_cooldown(provider, str(e))
                    continue
                if not provider.is_local:
                    _agent_label = f" for {agent_name}" if agent_name else ""
                    logger.warning(
                        f"⚠️ Provider '{provider.name}' failed{_agent_label}: "
                        f"{type(e).__name__}: {e} — trying next"
                    )
                    continue
                # Local provider error — no more fallbacks
                elapsed = time.time() - start_time
                self._retry_count = getattr(self, "_retry_count", 0) + 1
                _agent_label = f" for {agent_name}" if agent_name else ""
                logger.warning(
                    f"⚠️ LLM call failed{_agent_label} after {elapsed:.1f}s "
                    f"(attempt {self._retry_count}): {type(e).__name__}: {e}"
                )
                if span is not None:
                    span.finish(
                        output={"error": str(e)},
                        latency_ms=(time.time() - start_time) * 1000,
                        model=f"{provider.name}/{provider.model}",
                    )
                raise

        # All providers exhausted
        if span is not None:
            span.finish(
                output={"error": "All providers exhausted"},
                latency_ms=(time.time() - start_time) * 1000,
            )
        raise last_error or RuntimeError("All LLM providers exhausted")

    async def chat_with_tools(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[dict],
        messages: Optional[list[dict]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        agent_name: Optional[str] = None,
        priority: Optional[str] = None,
    ) -> tuple[Optional[str], list[dict], Optional[dict]]:
        """
        Send a chat completion with OpenAI-format tool definitions.
        Tries providers in chain order with automatic fallback.

        Returns:
            (text_content, tool_calls, assistant_raw_msg)
        """
        start_time = time.time()
        temp = temperature or self.temperature
        tokens = max_tokens or self.max_tokens
        last_error: Optional[Exception] = None

        full_system = system_prompt
        if self.persona:
            full_system = f"{self.persona}\n\n{system_prompt}"

        chat_messages: list[dict] = [{"role": "system", "content": full_system}]
        if messages:
            chat_messages.extend(messages)
        if user_message:
            chat_messages.append({"role": "user", "content": user_message})

        # Smart routing: reorder providers based on tier + priority
        providers = self._select_providers(agent_name=agent_name, priority=priority)

        for provider in providers:
            if not self._is_provider_available(provider):
                continue

            try:
                response = await self._do_chat_with_tools(
                    provider, chat_messages, tools, temp, tokens,
                )

                elapsed_ms = (time.time() - start_time) * 1000
                with self._providers_lock:
                    self._call_count += 1
                    self._last_provider = provider.name

                if response.usage:
                    total = response.usage.total_tokens or 0
                    with self._providers_lock:
                        self._total_tokens += total
                    self._record_call(provider, total)

                msg = response.choices[0].message
                text_content: Optional[str] = (msg.content or "").strip() or None

                parsed_calls: list[dict] = []
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        try:
                            args = tc.function.arguments
                            if isinstance(args, str):
                                args = json.loads(args) if args else {}
                        except (json.JSONDecodeError, AttributeError):
                            args = {}
                        parsed_calls.append({
                            "name": tc.function.name,
                            "arguments": args,
                            "id": tc.id,
                        })

                logger.debug(
                    f"chat_with_tools | provider={provider.name} | "
                    f"{elapsed_ms:.0f}ms | tool_calls={len(parsed_calls)}"
                )

                assistant_raw: dict = {"role": "assistant", "content": msg.content or ""}
                if msg.tool_calls:
                    assistant_raw["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments
                                if isinstance(tc.function.arguments, str)
                                else json.dumps(tc.function.arguments),
                            },
                        }
                        for tc in msg.tool_calls
                    ]

                return text_content, parsed_calls, assistant_raw

            except Exception as e:
                last_error = e
                if self._is_rate_or_quota_error(e):
                    self._activate_cooldown(provider, str(e))
                    continue
                if not provider.is_local:
                    logger.warning(
                        f"⚠️ Provider '{provider.name}' tool call failed: "
                        f"{type(e).__name__}: {e} — trying next"
                    )
                    continue
                raise

        raise last_error or RuntimeError("All LLM providers exhausted")

    async def chat_json(
        self,
        system_prompt: str,
        user_message: str,
        temperature: Optional[float] = None,
        span: Optional["SpanContext"] = None,
        agent_name: Optional[str] = None,
        priority: Optional[str] = None,
    ) -> dict:
        """
        Send a chat request and parse the response as JSON.
        Instructs the model to respond in JSON and parses it.
        """
        json_instruction = (
            "\n\nIMPORTANT: Respond ONLY with valid JSON. "
            "No markdown, no explanation, no code blocks. Just raw JSON."
        )

        response = await self.chat(
            system_prompt=system_prompt + json_instruction,
            user_message=user_message,
            temperature=temperature,
            span=span,
            agent_name=agent_name,
            priority=priority,
        )

        try:
            return json.loads(response)
        except json.JSONDecodeError:
            logger.warning("Response not valid JSON, attempting extraction...")
            return self._extract_json(response)

    def _extract_json(self, text: str) -> dict:
        """Attempt to extract JSON from text that may contain other content."""
        # Try to find JSON in markdown code blocks
        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        # Try to find JSON object pattern (brace-balanced extraction)
        # Find opening braces and try to parse balanced substrings
        # Limit to first 10 opening braces to avoid O(n²) on malformed output
        brace_attempts = 0
        for i, ch in enumerate(text):
            if ch == '{':
                brace_attempts += 1
                if brace_attempts > 10:
                    break
                depth = 0
                for j in range(i, len(text)):
                    if text[j] == '{':
                        depth += 1
                    elif text[j] == '}':
                        depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[i:j+1])
                        except json.JSONDecodeError:
                            break  # try next opening brace

        logger.error(f"Could not extract JSON from response: {text[:300]}")
        return {"error": "Failed to parse LLM response", "raw": text[:500]}

    # ── Health & stats ────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Check if at least one LLM provider is reachable."""
        with self._providers_lock:
            providers = list(self._providers)
        # Cloud providers: if we have a key and aren't in cooldown, assume available
        for p in providers:
            if not p.is_local and self._is_provider_available(p):
                return True

        # Check local providers (Ollama)
        for p in providers:
            if p.is_local:
                try:
                    import requests
                    ollama_url = str(p.client.base_url).rstrip("/").removesuffix("/v1")
                    resp = requests.get(f"{ollama_url}/api/tags", timeout=5)
                    if resp.status_code == 200:
                        models = resp.json().get("models", [])
                        if any(p.model in m.get("name", "") for m in models):
                            return True
                except Exception:
                    pass

        return False

    def reload_providers(self, new_providers: list[LLMProvider]) -> None:
        """Hot-reload the provider chain, preserving token counters for existing providers."""
        with self._providers_lock:
            # Build a lookup of existing provider state by name
            old_state: dict[str, LLMProvider] = {
                p.name: p for p in self._providers
            }

            # Carry forward daily token counters and RPM timestamps
            for np in new_providers:
                old = old_state.get(np.name)
                if old and not np.is_local:
                    with old._lock:
                        np.daily_tokens = old.daily_tokens
                        np.daily_requests = old.daily_requests
                        np.daily_date = old.daily_date
                        np.rpm_timestamps = list(old.rpm_timestamps)
                        # Preserve cooldown only if still active
                        if old.cooldown_until > time.monotonic():
                            np.cooldown_until = old.cooldown_until

            self._providers = list(new_providers)
            if self._providers:
                self.model = self._providers[0].model
                self.client = self._providers[0].client
            names = [p.name for p in self._providers]
        logger.info(f"🔄 LLM providers reloaded: {' → '.join(names)}")

    def update_providers_config(
        self,
        providers_config: list[dict],
        fallback_base_url: str = "http://localhost:11434",
        fallback_model: str = "llama3.1:8b",
        fallback_timeout: int = 60,
        fallback_max_retries: int = 1,
    ) -> None:
        """Store the raw provider config for use by rescan_and_reload()."""
        self._providers_config = list(providers_config)
        self._fallback_base_url = fallback_base_url
        self._fallback_model = fallback_model
        self._fallback_timeout = fallback_timeout
        self._fallback_max_retries = fallback_max_retries

    def rescan_and_reload(self) -> bool:
        """Re-run build_providers() against os.environ + config/.env and reload if the chain changed.

        This enables live-reload of API keys written by the dashboard to
        config/.env without needing a container restart.
        Returns True if a reload happened.
        """
        if not self._providers_config:
            return False

        new_providers = build_providers(
            self._providers_config,
            fallback_base_url=self._fallback_base_url,
            fallback_model=self._fallback_model,
            fallback_timeout=self._fallback_timeout,
            fallback_max_retries=self._fallback_max_retries,
        )

        # Compare chain: name+model tuples
        with self._providers_lock:
            old_sig = [(p.name, p.model) for p in self._providers]
        new_sig = [(p.name, p.model) for p in new_providers]

        if old_sig != new_sig:
            added = set(dict(new_sig)) - set(dict(old_sig))
            removed = set(dict(old_sig)) - set(dict(new_sig))
            parts = []
            if added:
                parts.append(f"added={added}")
            if removed:
                parts.append(f"removed={removed}")
            logger.info(f"♻️ Provider chain changed ({', '.join(parts)}), reloading...")
            self.reload_providers(new_providers)
            return True

        return False

    def check_provider_recovery(self) -> None:
        """Check if any cloud provider has recovered from cooldown or daily token exhaustion.

        Logs recovery transitions so they're visible in dashboards/logs.
        Called by the recovery poller and optionally by the orchestrator each cycle.
        """
        now = time.monotonic()
        with self._providers_lock:
            providers = list(self._providers)

        for p in providers:
            if p.is_local:
                continue

            with p._lock:
                # Check cooldown recovery
                was_cooling = getattr(p, '_was_in_cooldown', False)
                in_cooldown = now < p.cooldown_until
                if was_cooling and not in_cooldown:
                    logger.info(
                        f"♻️ Provider '{p.name}' recovered from cooldown — "
                        f"resuming as {'primary' if providers[0].name == p.name else 'fallback'}"
                    )
                p._was_in_cooldown = in_cooldown  # type: ignore[attr-defined]

                # Check daily budget recovery (date rollover)
                today = dt_date.today().isoformat()
                if p.daily_date != today:
                    if p.daily_token_limit > 0 and p.daily_tokens >= p.daily_token_limit:
                        logger.info(
                            f"♻️ Provider '{p.name}' daily token budget reset "
                            f"({p.daily_tokens:,} → 0) — new day {today}"
                        )
                    if p.daily_request_limit > 0 and p.daily_requests >= p.daily_request_limit:
                        logger.info(
                            f"♻️ Provider '{p.name}' daily request budget reset "
                            f"({p.daily_requests} → 0) — new day {today}"
                        )
                    p.daily_tokens = 0
                    p.daily_requests = 0
                    p.daily_date = today

    async def _recovery_poll_loop(self) -> None:
        """Background coroutine that periodically rescans providers and checks recovery.

        Also checks OpenRouter free-tier credits and rotates models if needed.
        """
        logger.info(
            f"🔄 LLM recovery poller started (interval={self._recovery_interval:.0f}s)"
        )
        try:
            while True:
                await asyncio.sleep(self._recovery_interval)
                try:
                    self.check_provider_recovery()
                    self.rescan_and_reload()
                    # Check OpenRouter credits for all openrouter providers
                    await self._check_all_openrouter_credits()
                except Exception as exc:
                    logger.warning(f"Recovery poll error (non-fatal): {exc}")
        except asyncio.CancelledError:
            logger.info("🔄 LLM recovery poller stopped")

    async def _check_all_openrouter_credits(self) -> None:
        """Check OpenRouter credit balance for all OpenRouter providers in the chain."""
        with self._providers_lock:
            providers = list(self._providers)
        for p in providers:
            if p.name.startswith("openrouter") and not p.is_local:
                info = await self.check_openrouter_credits_cached(p)
                if info and info.get("ok"):
                    remaining = info.get("credits_remaining")
                    if remaining is not None and remaining <= 0 and p.tier == "free":
                        # Free credits exhausted — ensure we're using :free models
                        with p._lock:
                            if not p.model.endswith(":free"):
                                old = p.model
                                p.model = OPENROUTER_FREE_MODELS[0]
                                logger.info(
                                    f"💸 OpenRouter credits exhausted, "
                                    f"switched to free model: {old} → {p.model}"
                                )

    def start_recovery_polling(
        self,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        interval: Optional[float] = None,
    ) -> None:
        """Start the background recovery poller.

        Args:
            loop: The asyncio event loop to schedule the task on.
                  If None, uses asyncio.get_event_loop().
            interval: Override the polling interval in seconds.
        """
        if interval is not None:
            self._recovery_interval = max(30.0, float(interval))

        if self._recovery_task is not None and not self._recovery_task.done():
            logger.debug("Recovery poller already running")
            return

        target_loop = loop or asyncio.get_event_loop()
        self._recovery_task = target_loop.create_task(self._recovery_poll_loop())

    def stop_recovery_polling(self) -> None:
        """Cancel the background recovery poller."""
        if self._recovery_task and not self._recovery_task.done():
            self._recovery_task.cancel()
            self._recovery_task = None

    def provider_status(self) -> list[dict]:
        """Return status of each provider for dashboard display."""
        now = time.monotonic()
        result = []
        with self._providers_lock:
            providers = list(self._providers)
        for p in providers:
            status: dict[str, Any] = {
                "name": p.name,
                "model": p.model,
                "is_local": p.is_local,
                "available": self._is_provider_available(p),
                "tier": p.tier,
                "reserve_for_priority": p.reserve_for_priority,
            }
            if not p.is_local:
                status.update({
                    "in_cooldown": now < p.cooldown_until,
                    "cooldown_remaining_s": max(0, int(p.cooldown_until - now)),
                    "daily_tokens": p.daily_tokens,
                    "daily_token_limit": p.daily_token_limit,
                    "daily_requests": p.daily_requests,
                    "daily_request_limit": p.daily_request_limit,
                    "rpm_limit": p.rpm_limit,
                    "rpm_current": len([
                        t for t in p.rpm_timestamps if t > time.time() - 60
                    ]),
                })
                # OpenRouter-specific: credit balance
                if p.name.startswith("openrouter"):
                    with p._lock:
                        status["credits_remaining"] = p._credits_remaining
                        status["free_model_index"] = p._free_model_index
                    status["is_free_model"] = p.model.endswith(":free")
            result.append(status)
        return result

    @property
    def stats(self) -> dict:
        """Get usage statistics."""
        return {
            "total_calls": self._call_count,
            "total_tokens": self._total_tokens,
            "last_provider": self._last_provider,
            "providers": self.provider_status(),
        }
