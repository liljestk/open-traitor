# ADR-007: Conversational LLM Telegram Bot

**Status:** Accepted

## Context

Monitoring and controlling the trading system requires a mobile-friendly interface. A Telegram bot provides push notifications, on-the-go status checks, and trade approvals without needing dashboard access. The bot must be both fast for common queries and intelligent for complex interactions.

## Decision

Implement a **dual-path Telegram bot** with strict user authentication, combining regex-based fast responses with LLM-powered conversational intelligence.

### Security Model

**Strict user ID allowlist** — no fallback, no "allow all" mode:

```python
self.authorized_users: set[str] = set()
for uid in authorized_users:
    uid_str = str(uid).strip()
    if not uid_str or not uid_str.lstrip("-").isdigit():
        raise ValueError(f"Invalid Telegram user ID: '{uid}'.")
    self.authorized_users.add(uid_str)
```

- Only numeric user IDs from `TELEGRAM_AUTHORIZED_USERS` can interact.
- Bot refuses to start if the list is empty.
- Every unauthorized attempt is logged.
- Aligns with the principle of least privilege from the project directives.

### Dual-Path Architecture

**1. Fast Path** (< 50ms, regex-based):
- Pattern-matched against a `FAST_PATTERNS` table (status, balance, prices, trades).
- Instant responses without an LLM round-trip.
- Example: `"show me BTC"` → regex match → fetch live price → format response.

**2. Smart Path** (3–8s, LLM-powered):
- Complex or ambiguous messages route to `TelegramChatHandler._smart_response()`.
- Single LLM call with:
  - **Persona**: Opinionated, sharp, proactive trader personality.
  - **Conversation memory**: Last 30 messages stored in-memory.
  - **Tool registry**: Access to trading functions (approve trade, apply preset, check portfolio, etc.).
- LLM interprets natural language + invokes tools, returns formatted response.

```python
async def handle_message(text: str, user_name: str = "Owner") -> str:
    fast_response = self._try_fast_path(text)
    if fast_response:
        return fast_response
    return await self._smart_response(text, user_name)
```

### Proactive Engine

A background thread monitors trading events and sends unsolicited updates:
- Price alerts when thresholds are hit.
- Trade execution notifications.
- Daily briefing summaries.

This runs independently from the chat message loop.

### Commands

All commands flow through the LLM for context-aware interpretation rather than rigid hardcoded parsing. The LLM has access to tool functions it can invoke based on user intent.

## Consequences

**Benefits:**
- Common queries (status, prices) are instant without LLM overhead.
- Complex interactions ("what happened with my BTC trades this week?") are handled naturally.
- Proactive alerts keep the user informed without polling.
- Strict auth prevents unauthorized access even if the bot token leaks.

**Risks:**
- Smart path latency (3–8s) depends on LLM provider availability (mitigated by fallback chain, ADR-004).
- In-memory conversation history (30 messages) is lost on restart.
- LLM tool-calling errors could produce incorrect trade approvals (mitigated by AbsoluteRules, ADR-002).

**Trade-offs:**
- No support for multiple concurrent users by design — this is a personal trading bot.
- Persona is opinionated ("sharp trader"), which may not suit all users but makes responses more actionable.
