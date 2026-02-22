"""
LLM Client wrapper with multi-provider fallback chain.

Supports an ordered list of providers (e.g. Gemini -> OpenAI -> Ollama).
Each provider uses the OpenAI-compatible API. On rate-limit or quota errors
the client automatically falls through to the next provider in the chain.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date as dt_date
from typing import Any, Optional, TYPE_CHECKING

from openai import AsyncOpenAI, RateLimitError, APIStatusError

from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.utils.tracer import SpanContext

logger = get_logger("core.llm")


# ─── Provider dataclass ───────────────────────────────────────────────────────

@dataclass
class LLMProvider:
    """A single LLM backend in the provider chain."""
    name: str
    client: AsyncOpenAI
    model: str
    is_local: bool = False
    rpm_limit: int = 0          # 0 = no local RPM tracking
    daily_token_limit: int = 0  # 0 = unlimited
    cooldown_seconds: int = 60
    # Mutable tracking state
    cooldown_until: float = 0.0
    daily_tokens: int = 0
    daily_date: str = ""
    rpm_timestamps: list[float] = field(default_factory=list)
    # Per-provider lock for thread-safe rate/quota tracking (H1 fix)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def build_providers(
    providers_config: list[dict],
    fallback_base_url: str = "http://localhost:11434",
    fallback_model: str = "llama3.1:8b",
    fallback_timeout: int = 60,
    fallback_max_retries: int = 1,
) -> list[LLMProvider]:
    """
    Build LLMProvider instances from the config list.

    Resolves env vars for API keys and base URLs.
    Skips cloud providers whose API key env var is empty.
    If the list is empty or all cloud providers are skipped,
    ensures at least one Ollama provider exists.
    """
    providers: list[LLMProvider] = []

    for pc in providers_config:
        if not pc.get("enabled", True):
            continue

        is_local = pc.get("is_local", False)
        name = pc.get("name", "unknown")

        # Resolve API key
        api_key_env = pc.get("api_key_env", "")
        api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        if not is_local and not api_key:
            logger.info(f"Skipping provider '{name}': {api_key_env} not set")
            continue

        # Resolve base URL
        base_url_env = pc.get("base_url_env", "")
        base_url = pc.get("base_url", "")
        if base_url_env:
            base_url = os.environ.get(base_url_env, base_url or fallback_base_url)
        if not base_url:
            base_url = fallback_base_url

        # Resolve model
        model_env = pc.get("model_env", "")
        model = pc.get("model", fallback_model)
        if model_env:
            model = os.environ.get(model_env, model)

        timeout = pc.get("timeout", fallback_timeout)

        # Build the AsyncOpenAI client
        if is_local:
            # Ollama: append /v1, use dummy key
            client = AsyncOpenAI(
                base_url=f"{base_url.rstrip('/')}/v1",
                api_key="ollama",
                timeout=timeout,
                max_retries=fallback_max_retries,
            )
        else:
            # Cloud provider: use real key, no retries (we handle fallback)
            client = AsyncOpenAI(
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                max_retries=0,
            )

        providers.append(LLMProvider(
            name=name,
            client=client,
            model=model,
            is_local=is_local,
            rpm_limit=pc.get("rpm_limit", 0),
            daily_token_limit=pc.get("daily_token_limit", 0),
            cooldown_seconds=pc.get("cooldown_seconds", 60),
        ))

        logger.info(f"  Provider '{name}' ready | model={model} | local={is_local}")

    # Ensure at least one provider (Ollama fallback)
    if not providers:
        logger.warning("No providers configured, using Ollama fallback")
        client = AsyncOpenAI(
            base_url=f"{fallback_base_url.rstrip('/')}/v1",
            api_key="ollama",
            timeout=fallback_timeout,
            max_retries=fallback_max_retries,
        )
        providers.append(LLMProvider(
            name="ollama",
            client=client,
            model=fallback_model,
            is_local=True,
        ))

    return providers


# ─── LLMClient ─────────────────────────────────────────────────────────────────

class LLMClient:
    """
    Multi-provider LLM client with automatic fallback chain.

    Tries providers in order. On rate-limit/quota errors, activates a cooldown
    on that provider and falls through to the next one. Local providers
    (Ollama) are always available as the final fallback.
    """

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

            # Daily token budget
            today = dt_date.today().isoformat()
            if p.daily_date != today:
                p.daily_tokens = 0
                p.daily_date = today
            if p.daily_token_limit > 0 and p.daily_tokens >= p.daily_token_limit:
                return False

            # RPM check
            if p.rpm_limit > 0:
                cutoff = time.time() - 60.0
                p.rpm_timestamps = [t for t in p.rpm_timestamps if t > cutoff]
                if len(p.rpm_timestamps) >= p.rpm_limit:
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
                p.daily_date = today
            p.daily_tokens += total_tokens

    def _activate_cooldown(self, p: LLMProvider, reason: str) -> None:
        """Put a provider on cooldown after a rate-limit or quota error."""
        with p._lock:
            p.cooldown_until = time.monotonic() + p.cooldown_seconds
        logger.warning(
            f"⏸️ Provider '{p.name}' cooldown ({p.cooldown_seconds}s): {reason}"
        )

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
    ) -> str:
        """Send a chat completion request, trying providers in chain order."""
        start_time = time.time()
        temp = temperature or self.temperature
        tokens = max_tokens or self.max_tokens
        last_error: Optional[Exception] = None
        with self._providers_lock:
            providers = list(self._providers)

        for provider in providers:
            if not self._is_provider_available(provider):
                continue

            try:
                response = await self._do_chat(
                    provider, system_prompt, user_message, temp, tokens,
                )

                # Success — record metrics
                elapsed_ms = (time.time() - start_time) * 1000
                self._call_count += 1
                self._last_provider = provider.name

                prompt_tokens = 0
                completion_tokens = 0
                if response.usage:
                    prompt_tokens = response.usage.prompt_tokens or 0
                    completion_tokens = response.usage.completion_tokens or 0
                    total = response.usage.total_tokens or 0
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

        # H25 fix: snapshot provider list under lock (mirrors chat() fix H14)
        with self._providers_lock:
            providers = list(self._providers)

        for provider in providers:
            if not self._is_provider_available(provider):
                continue

            try:
                response = await self._do_chat_with_tools(
                    provider, chat_messages, tools, temp, tokens,
                )

                elapsed_ms = (time.time() - start_time) * 1000
                self._call_count += 1
                self._last_provider = provider.name

                if response.usage:
                    total = response.usage.total_tokens or 0
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

        # Try to find JSON object pattern
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

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
        """Hot-reload the provider chain (called from dashboard settings)."""
        with self._providers_lock:
            self._providers = list(new_providers)
            if self._providers:
                self.model = self._providers[0].model
                self.client = self._providers[0].client
            names = [p.name for p in self._providers]
        logger.info(f"🔄 LLM providers reloaded: {' → '.join(names)}")

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
            }
            if not p.is_local:
                status.update({
                    "in_cooldown": now < p.cooldown_until,
                    "cooldown_remaining_s": max(0, int(p.cooldown_until - now)),
                    "daily_tokens": p.daily_tokens,
                    "daily_token_limit": p.daily_token_limit,
                    "rpm_limit": p.rpm_limit,
                    "rpm_current": len([
                        t for t in p.rpm_timestamps if t > time.time() - 60
                    ]),
                })
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
