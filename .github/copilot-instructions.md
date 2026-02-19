# Copilot Instructions for auto-traitor
We are not live yet, refactoring is okay if needed.

## Big picture architecture
- Python trading daemon + React dashboard.
- Entry point: `src/main.py` initializes services, then runs `Orchestrator.run_forever()` in `src/core/orchestrator.py`.
- Core cycle: market analysis → strategy → risk validation → execution → rotation/trailing stops/reconciliation → snapshot/audit sync.
- `src/core/rules.py` (`AbsoluteRules`) is a hard boundary; no strategy/planning output may bypass it.
- Temporal planning (`src/planning/workflows.py`, `activities.py`, `worker.py`) writes soft context, not hard trade overrides.

## Service boundaries and data flow
- Persistent state/analytics live in SQLite via `StatsDB` (`src/utils/stats.py`, `data/stats.db`, WAL).
- LLM tracing path: agent span → `LLMTracer` (`src/utils/tracer.py`) → Langfuse + Redis `llm:events` → dashboard `/ws/live`.
- Dashboard backend (`src/dashboard/server.py`) reads from `StatsDB`; frontend consumes via `dashboard/frontend/src/api.ts`.
- Frontend uses relative `/api` + `/ws` and local Vite proxy (`dashboard/frontend/vite.config.ts`).

## Critical workflows and commands
- First-time setup: `./setup.ps1` (interactive `.env` generation, including Telegram auth).
- Main runtime is Docker Compose (`docker-compose.yml`) with `agent`, `news-worker`, `planning-worker`, `ollama`, `redis`, `langfuse`, `temporal`.
- Local commands: `python -m src.main --mode paper|live`, `python -m src.news.worker`, `python -m src.planning.worker`.
- Frontend (`dashboard/frontend`): `npm run dev`, `npm run build`, `npm run lint`.

## Project-specific coding patterns
- Agents inherit `BaseAgent` (`src/agents/base_agent.py`) and implement `run(context)`; call `.execute()` for tracking/error handling.
- For LLM paths, create spans (`trace_ctx.start_span(...)`) and persist reasoning via `StatsDB.save_reasoning(...)` (see `src/agents/market_analyst.py`).
- Strategic context should calibrate confidence only; do not override technical/risk evidence.
- Keep graceful degradation: Redis/Langfuse/WebSocket/Temporal outages must not stop core trading.

## Security and safety constraints
- `TELEGRAM_AUTHORIZED_USERS` is mandatory (`src/main.py`); never add fallback auth paths.
- Preserve deployment hardening from `docker-compose.yml` (`read_only`, `no-new-privileges`, non-root).
- Keep paper/live safeguards explicit (live-mode confirmation, circuit breakers, conservative defaults).

## Engineering principles for this project
- Apply least privilege by default for permissions, credentials, and runtime capabilities.
- Prefer modular changes within existing boundaries (`src/agents`, `src/core`, `src/planning`, `src/utils`).
- Keep contracts explicit/stable (agent outputs, dashboard payloads, planning activity/workflow schemas).
- Favor small, testable, behavior-preserving changes; avoid broad refactors unless necessary.
- Prioritize observability (structured logs + consistent tracing/reasoning persistence).

## Anti-patterns to avoid
- Do not bypass `AbsoluteRules` through strategy, executor, Telegram commands, or planning outputs.
- Do not add cross-layer coupling (for example, dashboard/frontend logic directly mutating trading runtime state).
- Do not hard-code secrets, tokens, or environment-specific endpoints in code.
- Do not make optional services (Redis/Langfuse/Temporal/WebSocket) hard dependencies for the core trading loop.
- Do not introduce auth fallbacks that allow control without explicit allowlisting (`TELEGRAM_AUTHORIZED_USERS`).

## Integration touchpoints to check when editing
- Trading pipeline changes: review `src/core/orchestrator.py`, relevant `src/agents/*`, and `src/core/*` managers together.
- Planning changes: update both Temporal workflow definitions and corresponding activity payload/DB persistence logic.
- Dashboard schema/API changes: update backend endpoints (`src/dashboard/server.py`) and frontend consumers/types (`dashboard/frontend/src/api.ts`, page components).

## Git workflow
- After completing changes, always create a commit and push to remote.
- Use clear, descriptive commit messages that explain what changed and why.
