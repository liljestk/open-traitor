# ADR-004: Multi-Provider LLM Fallback Chain

**Status:** Accepted

## Context

The trading pipeline depends on LLM calls for market analysis and strategy formulation. Free-tier cloud APIs (OpenRouter, Gemini) have strict rate and token limits. A single provider outage or quota exhaustion would halt the entire trading cycle. We need redundancy without incurring significant cost.

## Decision

Implement an ordered **fallback chain** of LLM providers with automatic cooldown and recovery. Providers are tried in priority order; on failure, the next provider is attempted. Local Ollama is always the last-resort fallback.

### Provider Configuration

Each provider is defined as:

```python
@dataclass
class LLMProvider:
    name: str              # "openrouter", "gemini", "ollama"
    client: AsyncOpenAI    # OpenAI-compatible async client
    model: str             # e.g., "meta-llama/llama-3.3-70b-instruct:free"
    is_local: bool         # True for Ollama
    tier: str              # "free" or "paid"
    rpm_limit: int         # requests per minute
    daily_token_limit: int # cumulative tokens per day (0 = unlimited)
    cooldown_seconds: int  # backoff on quota error
```

### Fallback Logic

1. `LLMClient.chat()` iterates providers in configured order.
2. On **rate-limit or quota error**: `_activate_cooldown(provider)` marks it unavailable for `cooldown_seconds`.
3. Provider is skipped until cooldown expires.
4. Falls through to next provider; Ollama (local) is always appended as final fallback.
5. **Recovery polling** (every 120s) checks if cooled-down providers are back online.
6. If all providers fail, the exception propagates and the cycle is skipped for that pair.

### Per-Provider State Tracking

Each provider tracks:
- `cooldown_until`: timestamp when the provider becomes available again
- `daily_tokens_used`: cumulative token consumption (reset at UTC midnight)
- `rpm_count`: rolling requests-per-minute counter

### Example Configuration (coinbase.yaml)

```yaml
llm:
  providers:
    - name: openrouter
      base_url: https://openrouter.ai/api/v1
      model: meta-llama/llama-3.3-70b-instruct:free
      tier: free
      rpm_limit: 20
      daily_token_limit: 0
      cooldown_seconds: 60
      api_key_env: OPENROUTER_API_KEY
      enabled: true
    - name: gemini
      base_url: https://generativelanguage.googleapis.com/v1beta/openai/
      model: gemini-2.5-flash-lite
      tier: free
      rpm_limit: 10
      daily_token_limit: 200000
      cooldown_seconds: 180
      api_key_env: GEMINI_API_KEY
      enabled: true
    # Ollama is always added automatically as final fallback
```

### All-OpenAI-Compatible

Every provider uses the `AsyncOpenAI` client with a custom `base_url`. This means any OpenAI-compatible API (Anthropic proxies, vLLM, LiteLLM) can be added as a provider with zero code changes.

## Consequences

**Benefits:**
- Zero-cost operation possible with free-tier cloud + local Ollama.
- Automatic recovery from transient quota exhaustion.
- New providers can be added via config without code changes.
- Ollama guarantees the pipeline always has a fallback (no external dependency for core operation).

**Risks:**
- Latency variance: ~100ms (local Ollama) vs ~3s (cloud). Mitigated by the pipeline running pairs in parallel.
- Model quality varies across providers; free-tier models may produce lower-quality signals.
- Per-provider state is in-memory; a restart loses cooldown/quota tracking (acceptable — counters recover naturally).

**Trade-offs:**
- Prioritizing free-tier providers means occasionally slower or lower-quality responses vs. paying for consistent premium models.
- Cooldown durations are static per provider; adaptive backoff could be more efficient but adds complexity.
