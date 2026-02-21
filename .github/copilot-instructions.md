# Copilot Instructions for auto-traitor
We are not live yet, refactoring is okay if needed.

NEVER suggest to edit or update files manually, just tell if you cant edit and I will fix.

## General Principles
- ** Autonomous Agent Focus:** All suggestions should prioritize enhancing the autonomous capabilities of the trading agents, improving decision-making, execution, and adaptability without manual intervention.
- **Modularity & Maintainability:** Propose changes that are modular, well-encapsulated, and maintainable. Avoid monolithic changes that touch too many unrelated components.

## Premium Request Optimization
- **Chain Tasks:** When given a multi-step objective, do NOT stop after the first step. Perform the full implementation, including edge cases and basic unit tests, in a single response.
- **Self-Correction:** If you realize a mistake while generating code, correct it immediately within the same output rather than waiting for user feedback.
- **Verify Imports:** Always double-check that all required imports/dependencies for your code are included in the output to avoid "fix the import" follow-up requests.

## Output Efficiency
- **No Conversational Filler:** Skip "Sure, I can help with that" or "Here is the code." Start directly with the implementation or the plan.
- **Diff-Only Format:** For large files, provide only the changed code blocks or a `sed`-style patch unless I explicitly ask for the full file. 
- **Plan Mode First:** (For Agent mode) Always present a concise plan (using <plan> tags) and wait for a single "Go" before consuming a heavy reasoning request for the implementation.


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
- After completing changes, always create a commit and push to remote using command line.
- Use clear, descriptive commit messages that explain what changed and why.

## MCPs
Avoid MCPs if possible; if a change is large, break it into logical commits that can be reviewed independently.
