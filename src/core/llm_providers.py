"""
LLM Provider config, dataclass, and builder.

Extracted from llm_client.py to keep files under 1000 lines.

Provides:
  - _read_config_env / _resolve_env  – live-reload config/.env
  - check_openrouter_credits          – OpenRouter free-tier credit check
  - LLMProvider                       – dataclass for a single provider
  - build_providers                   – config list → provider chain
  - OPENROUTER_FREE_MODELS            – ordered list of free model slugs
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from openai import AsyncOpenAI

from src.utils.logger import get_logger

logger = get_logger("core.llm")

# ─── Config .env file reading (live-reload across containers) ────────────────
# Docker env_file vars are only injected at container creation.
# When the dashboard writes new API keys to config/.env, other containers
# won't see them in os.environ until restart.  This module reads config/.env
# directly as a fallback, with a short TTL cache to avoid repeated disk I/O.

_CONFIG_ENV_PATH = os.path.join("config", ".env")
_config_env_cache: dict[str, str] = {}
_config_env_mtime: float = 0.0
_config_env_lock = threading.Lock()


def _read_config_env() -> dict[str, str]:
    """Parse config/.env into a dict, cached by file mtime."""
    global _config_env_cache, _config_env_mtime
    try:
        st = os.stat(_CONFIG_ENV_PATH)
    except OSError:
        return _config_env_cache
    if st.st_mtime == _config_env_mtime and _config_env_cache:
        return _config_env_cache
    with _config_env_lock:
        # Double-check after acquiring lock
        try:
            st = os.stat(_CONFIG_ENV_PATH)
        except OSError:
            return _config_env_cache
        if st.st_mtime == _config_env_mtime and _config_env_cache:
            return _config_env_cache
        result: dict[str, str] = {}
        try:
            with open(_CONFIG_ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    idx = line.index("=")
                    result[line[:idx].strip()] = line[idx + 1:].strip()
        except OSError:
            return _config_env_cache
        _config_env_cache = result
        _config_env_mtime = st.st_mtime
        return result


def _resolve_env(var_name: str, default: str = "") -> str:
    """Resolve an env var: os.environ first, then config/.env file fallback."""
    val = os.environ.get(var_name, "")
    if val:
        return val
    return _read_config_env().get(var_name, default)


# ─── OpenRouter helpers ───────────────────────────────────────────────────────

# Default headers OpenRouter recommends for attribution/ranking
_OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/auto-traitor",
    "X-Title": "auto-traitor",
}

# Free-tier models on OpenRouter (suffix :free). Ordered by preference.
# Last verified: 2026-02-22. Check https://openrouter.ai/models?q=:free
OPENROUTER_FREE_MODELS: list[str] = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "deepseek/deepseek-r1-0528:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
    "google/gemma-3-27b-it:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
    "qwen/qwen3-coder:free",
]


async def check_openrouter_credits(api_key: str) -> dict[str, Any]:
    """Query OpenRouter /api/v1/auth/key to get remaining credits & usage.

    Returns dict with keys: ok, credits_remaining, usage, rate_limit, is_free_tier.
    On failure returns {"ok": False, "error": "..."}.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code != 200:
                return {"ok": False, "error": f"HTTP {resp.status_code}"}
            data = resp.json().get("data", {})
            credits_remaining = data.get("limit_remaining")
            usage = data.get("usage", 0)
            rate_limit = data.get("rate_limit", {})
            is_free = data.get("is_free_tier", credits_remaining == 0 and usage == 0)
            return {
                "ok": True,
                "credits_remaining": credits_remaining,
                "usage": usage,
                "rate_limit": rate_limit,
                "is_free_tier": is_free,
                "label": data.get("label", ""),
            }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


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
    daily_request_limit: int = 0  # 0 = unlimited; max requests/day for this provider
    cooldown_seconds: int = 60
    tier: str = "free"          # "free" or "paid" — controls smart routing
    reserve_for_priority: str = ""  # "" = available for all; "high" = only high-priority calls
    # Mutable tracking state
    cooldown_until: float = 0.0
    daily_tokens: int = 0
    daily_requests: int = 0     # requests made today (reset on day rollover)
    daily_date: str = ""
    rpm_timestamps: list[float] = field(default_factory=list)
    # Per-provider lock for thread-safe rate/quota tracking (H1 fix)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # OpenRouter-specific: cached credit info
    _credits_remaining: Optional[float] = field(default=None, repr=False)
    _credits_checked_at: float = field(default=0.0, repr=False)
    _free_model_index: int = field(default=0, repr=False)  # current free model rotation
    # Consecutive 429 counter for escalating cooldown (Gemini free tier)
    _consecutive_429s: int = field(default=0, repr=False)


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

        # Resolve API key — check os.environ, then fall back to config/.env
        # so keys saved by the dashboard are picked up without container restart.
        api_key_env = pc.get("api_key_env", "")
        api_key = _resolve_env(api_key_env) if api_key_env else ""
        if not is_local and not api_key:
            logger.info(f"Skipping provider '{name}': {api_key_env} not set")
            continue

        # Resolve base URL (same env + file fallback)
        base_url_env = pc.get("base_url_env", "")
        base_url = pc.get("base_url", "")
        if base_url_env:
            base_url = _resolve_env(base_url_env, base_url or fallback_base_url)
        if not base_url:
            base_url = fallback_base_url

        # Resolve model (same env + file fallback)
        model_env = pc.get("model_env", "")
        model = pc.get("model", fallback_model)
        if model_env:
            model = _resolve_env(model_env, model)

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
        elif name.startswith("openrouter"):
            # OpenRouter: add attribution headers for better rate limits
            client = AsyncOpenAI(
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                max_retries=0,
                default_headers=_OPENROUTER_HEADERS,
            )
        else:
            # Cloud provider: use real key, no retries (we handle fallback)
            client = AsyncOpenAI(
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                max_retries=0,
            )

        tier = pc.get("tier", "free")
        reserve_for = pc.get("reserve_for_priority", "")
        providers.append(LLMProvider(
            name=name,
            client=client,
            model=model,
            is_local=is_local,
            rpm_limit=pc.get("rpm_limit", 0),
            daily_token_limit=pc.get("daily_token_limit", 0),
            daily_request_limit=pc.get("daily_request_limit", 0),
            cooldown_seconds=pc.get("cooldown_seconds", 60),
            tier=tier,
            reserve_for_priority=reserve_for,
        ))

        logger.info(
            f"  Provider '{name}' ready | model={model} | local={is_local} | tier={tier}"
            + (f" | reserved={reserve_for}" if reserve_for else "")
        )

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
