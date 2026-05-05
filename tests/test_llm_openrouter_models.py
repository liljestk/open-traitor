from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError, AsyncOpenAI

from src.core.llm_client import LLMClient
from src.core.llm_providers import (
    LLMProvider,
    OPENROUTER_FREE_MODEL_BLOCKLIST,
    OPENROUTER_FREE_MODELS,
    is_openrouter_free_model_supported,
)


@pytest.fixture()
def restore_openrouter_models():
    original_models = list(OPENROUTER_FREE_MODELS)
    original_blocklist = set(OPENROUTER_FREE_MODEL_BLOCKLIST)
    try:
        yield
    finally:
        OPENROUTER_FREE_MODELS[:] = original_models
        OPENROUTER_FREE_MODEL_BLOCKLIST.clear()
        OPENROUTER_FREE_MODEL_BLOCKLIST.update(original_blocklist)


def test_gemma_free_model_is_excluded_from_openrouter_rotation():
    assert not is_openrouter_free_model_supported("google/gemma-3-27b-it:free")
    assert "google/gemma-3-27b-it:free" not in OPENROUTER_FREE_MODELS


@pytest.mark.asyncio
async def test_openrouter_developer_instruction_400_blacklists_model_and_falls_back(
    monkeypatch,
    restore_openrouter_models,
):
    bad_model = "example/bad-system-role-model:free"
    good_model = "meta-llama/llama-3.3-70b-instruct:free"
    OPENROUTER_FREE_MODEL_BLOCKLIST.discard(bad_model)
    OPENROUTER_FREE_MODELS[:] = [bad_model, good_model]

    openrouter = LLMProvider(
        name="openrouter",
        client=AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key="test", max_retries=0),
        model=bad_model,
        tier="free",
    )
    ollama = LLMProvider(
        name="ollama",
        client=AsyncOpenAI(base_url="http://ollama/v1", api_key="ollama"),
        model="llama3.1:8b",
        is_local=True,
    )
    client = LLMClient(providers=[openrouter, ollama])

    err = APIStatusError(
        "Error code: 400 - Developer instruction is not enabled for models/bad-system-role-model",
        response=httpx.Response(
            400,
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        ),
        body={
            "error": {
                "message": "Developer instruction is not enabled for models/bad-system-role-model",
                "status": "INVALID_ARGUMENT",
            }
        },
    )

    async def fake_do_chat(provider, *_args):
        if provider.name == "openrouter":
            raise err
        return SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        )

    monkeypatch.setattr(client, "_do_chat", fake_do_chat)

    assert await client.chat("system", "user", agent_name="advisor") == "ok"
    assert bad_model in OPENROUTER_FREE_MODEL_BLOCKLIST
    assert bad_model not in OPENROUTER_FREE_MODELS
    assert openrouter.model == good_model