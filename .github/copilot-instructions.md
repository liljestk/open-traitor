# Copilot Instructions for auto-traitor

## Big picture architecture
- This is a Python-first autonomous trading daemon with a separate React dashboard.
- Core runtime starts in `src/main.py`, builds shared services, then runs `Orchestrator.run_forever()` in `src/core/orchestrator.py`.
- Main decision flow per cycle is: market analysis → strategy → risk validation → execution, then rotation, trailing stops, reconciliation, snapshot/audit sync.
- Treat `src/core/rules.py` (absolute rules) as hard safety boundaries; strategy output is never allowed to bypass them.
- Temporal planning is a parallel subsystem (`src/planning/workflows.py`, `src/planning/activities.py`, `src/planning/worker.py`) that writes soft strategic context consumed by orchestrator/agents.

## Service boundaries and data flow
- Persistent analytics/state are SQLite-backed via `StatsDB` (`src/utils/stats.py`, DB at `data/stats.db`, WAL mode).
- LLM observability flows through `LLMTracer` (`src/utils/tracer.py`) into Langfuse + Redis pub/sub (`llm:events`) and is surfaced on dashboard WebSocket `/ws/live`.
- Dashboard API (`src/dashboard/server.py`) reads `StatsDB` and exposes REST endpoints consumed by frontend `dashboard/frontend/src/api.ts`.
- Frontend API calls are relative (`/api`, `/ws`) and rely on Vite proxy in `dashboard/frontend/vite.config.ts` during local dev.

## Critical workflows and commands
- First-time setup: run `./setup.ps1` (creates `config/.env` interactively, including security-critical Telegram settings).
- Full stack runtime is Docker Compose (`docker-compose.yml`) with services: `agent`, `news-worker`, `planning-worker`, `ollama`, `redis`, `langfuse`, `temporal`.
- Typical local run commands:
  - `python -m src.main --mode paper`
  - `python -m src.main --mode live`
  - `python -m src.news.worker`
  - `python -m src.planning.worker`
- Frontend commands (`dashboard/frontend`): `npm run dev`, `npm run build`, `npm run lint`.

## Project-specific coding patterns
- Agents inherit `BaseAgent` (`src/agents/base_agent.py`) and expose `run(context)`; orchestration should call `.execute()` for state/error tracking.
- When adding/modifying LLM calls, pass tracing spans (`trace_ctx.start_span(...)`) and persist reasoning to `StatsDB.save_reasoning(...)` as done in `src/agents/market_analyst.py`.
- Keep strategic context usage as calibration only (see market analyst prompt): it should influence confidence, not override technical/risk evidence.
- Preserve graceful degradation: Redis, Langfuse, and WebSocket features are optional; core trading loop should continue if they are unavailable.

## Security and safety constraints
- `TELEGRAM_AUTHORIZED_USERS` is required; do not add fallback logic that allows unauthenticated Telegram control (`src/main.py`).
- Respect container hardening decisions in `docker-compose.yml` (`read_only`, `no-new-privileges`, non-root runtime).
- Keep paper/live mode behavior explicit and conservative; never remove live-mode confirmation prompts or circuit-breaker protections.

## Engineering principles for this project
- Follow least privilege by default: keep permissions, credentials, and runtime capabilities as narrow as possible.
- Prefer modular design: add focused components in the existing boundaries (`src/agents`, `src/core`, `src/planning`, `src/utils`) instead of creating tightly coupled cross-cutting logic.
- Keep interfaces explicit and stable across boundaries (agent outputs, dashboard API payloads, planning activity/workflow contracts).
- Design for graceful degradation: optional dependencies (Redis, Langfuse, WebSocket, Temporal availability) must not break the core trading loop.
- Favor small, testable changes: avoid broad refactors unless required by the task; preserve existing behavior and safety invariants.
- Prioritize observability: when adding behavior, include structured logs and, for LLM paths, keep tracing + reasoning persistence patterns consistent.
- Maintain secure-by-default behavior: never weaken auth, approval gates, or safety checks for convenience.

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
