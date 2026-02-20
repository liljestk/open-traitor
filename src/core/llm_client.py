"""
LLM Client wrapper for Ollama (OpenAI-compatible API).
Runs entirely locally — no data leaves your machine.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional, TYPE_CHECKING

from openai import OpenAI

from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.utils.tracer import SpanContext

logger = get_logger("core.llm")


class LLMClient:
    """
    Wrapper around Ollama's OpenAI-compatible API.
    Uses the openai Python client pointed at the local Ollama server.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama3.1:8b",
        temperature: float = 0.2,
        max_tokens: int = 2000,
        max_retries: int = 3,
        timeout: int = 60,
        persona: str = "",
    ):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.timeout = timeout
        self.persona = persona

        # Ollama exposes an OpenAI-compatible endpoint at /v1
        ollama_url = f"{base_url.rstrip('/')}/v1"

        self.client = OpenAI(
            base_url=ollama_url,
            api_key="ollama",  # Ollama doesn't need a real key
            timeout=timeout,
            max_retries=max_retries,
        )

        self._call_count = 0
        self._total_tokens = 0

        logger.info(
            f"✅ LLM Client initialized (Ollama) | "
            f"URL: {ollama_url} | Model: {model} | Temp: {temperature}"
        )

    def chat(
        self,
        system_prompt: str,
        user_message: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        span: Optional["SpanContext"] = None,
        agent_name: Optional[str] = None,
    ) -> str:
        """Send a chat completion request to Ollama and return the response text."""
        start_time = time.time()

        # Prepend persona to system prompt if set
        full_system = system_prompt
        if self.persona:
            full_system = f"{self.persona}\n\n{system_prompt}"

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": full_system},
                {"role": "user", "content": user_message},
            ],
            "temperature": temperature or self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }

        try:
            response = self.client.chat.completions.create(**kwargs)
            elapsed = time.time() - start_time
            elapsed_ms = elapsed * 1000

            self._call_count += 1
            prompt_tokens = 0
            completion_tokens = 0
            if response.usage:
                prompt_tokens = response.usage.prompt_tokens or 0
                completion_tokens = response.usage.completion_tokens or 0
                self._total_tokens += response.usage.total_tokens
                logger.debug(
                    f"LLM call #{self._call_count} | {elapsed:.1f}s | "
                    f"Tokens: {response.usage.total_tokens}"
                )
            else:
                logger.debug(f"LLM call #{self._call_count} | {elapsed:.1f}s")

            content = response.choices[0].message.content or ""
            content = content.strip()

            # Finish the tracing span with metrics
            if span is not None:
                span.finish(
                    output=content,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_ms=elapsed_ms,
                )

            return content

        except Exception as e:
            logger.error(f"❌ LLM call failed: {e}")
            if span is not None:
                span.finish(
                    output={"error": str(e)},
                    latency_ms=(time.time() - start_time) * 1000,
                )
            raise

    def chat_with_tools(
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

        Ollama/llama3.1 supports native tool calling — the model returns
        structured tool_calls instead of free-text ACTION: lines when it
        wants to invoke a function.

        Returns:
            (text_content, tool_calls, assistant_raw_msg)
            - text_content:       The model's text response (None if it only called tools)
            - tool_calls:         List of {name, arguments, id} dicts for invoked tools
            - assistant_raw_msg:  Raw message dict ready to re-append for multi-turn continuation

        Raises on network/model errors — let the caller handle gracefully.
        """
        start_time = time.time()

        full_system = system_prompt
        if self.persona:
            full_system = f"{self.persona}\n\n{system_prompt}"

        chat_messages: list[dict] = [{"role": "system", "content": full_system}]
        if messages:
            chat_messages.extend(messages)
        chat_messages.append({"role": "user", "content": user_message})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": chat_messages,
            "temperature": temperature or self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "tools": tools,
            "tool_choice": "auto",
        }

        response = self.client.chat.completions.create(**kwargs)
        elapsed_ms = (time.time() - start_time) * 1000
        self._call_count += 1

        if response.usage:
            self._total_tokens += response.usage.total_tokens

        msg = response.choices[0].message
        text_content: Optional[str] = (msg.content or "").strip() or None

        # Parse tool calls from the structured response
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
            f"chat_with_tools | {elapsed_ms:.0f}ms | "
            f"tool_calls={len(parsed_calls)} | text={'yes' if text_content else 'no'}"
        )

        # Build the raw assistant message needed for multi-turn continuation
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

    def chat_json(
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

        response = self.chat(
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

    def is_available(self) -> bool:
        """Check if Ollama is reachable and the model is loaded."""
        try:
            import requests
            # Use /api/tags endpoint — lightweight check, no GPU inference
            ollama_url = str(self.client.base_url).rstrip("/").removesuffix("/v1")
            resp = requests.get(f"{ollama_url}/api/tags", timeout=5)
            if resp.status_code != 200:
                return False
            models = resp.json().get("models", [])
            return any(self.model in m.get("name", "") for m in models)
        except Exception:
            return False

    @property
    def stats(self) -> dict:
        """Get usage statistics."""
        return {
            "total_calls": self._call_count,
            "total_tokens": self._total_tokens,
            "model": self.model,
        }
