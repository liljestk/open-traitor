# Contributing to Auto-Traitor

Thank you for your interest in contributing! This is an automated trading system, so code quality, test discipline, and security are paramount.

> **Important:** This software handles real money in live mode. Every change must be thoroughly tested.

## Getting Started

### Prerequisites

- Python 3.11+
- Docker & Docker Compose
- Git

### Local Development Setup

```bash
# Clone the repository
git clone https://github.com/<owner>/auto-traitor.git
cd auto-traitor

# Create a virtual environment
python -m venv venv
source venv/bin/activate    # Linux/macOS
# or
.\venv\Scripts\Activate     # Windows PowerShell

# Install dependencies
pip install -r requirements.txt

# Copy the example env (never commit real credentials)
cp config/root.env config/.env
# Edit config/.env with your values

# Or run the interactive setup wizard:
./setup.sh        # Linux/macOS
.\setup.ps1       # Windows
```

### Running Tests

```bash
python -m pytest tests/ -x --tb=short
```

All tests must pass before submitting a PR. The CI pipeline enforces this automatically.

## Development Rules

Please read [AGENTS.MD](AGENTS.MD) — it contains the canonical development rules that all contributors (human and AI) must follow. Key points are summarized below.

### Test & Commit Discipline

- **Run tests after every change:** `python -m pytest tests/ -x --tb=short`
- **Fix failures immediately** — never submit a PR with broken tests.
- **Pre-commit hooks** run `test_domain_separation.py` and `test_security.py` on every commit. If they fail, the commit is blocked. Do not bypass with `--no-verify`.
- Write descriptive commit messages explaining the *why*, not just "updated files".

### Domain Separation (Critical)

Crypto (Coinbase) and equity (IBKR) data must **never** be mixed:

- **Frontend:** Every `useQuery` call must include `profile` in its `queryKey` array.
- **Backend SQL:** All queries for trades, reasoning, and portfolio data must filter by exchange.
- **Redis:** All keys must be prefixed with the profile name (e.g., `coinbase:trailing_stops:state`).

The static test `TestFrontendQueryKeysIncludeProfile` enforces this and will block commits that violate it.

### Security

- Never hardcode secrets, tokens, or endpoints.
- Never introduce auth fallbacks outside `TELEGRAM_AUTHORIZED_USERS`.
- Never bypass `AbsoluteRules` via any code path.
- Redis, Langfuse, Temporal, and WebSocket must not be hard dependencies for the core trading loop.

### Architecture

Keep changes within existing module boundaries:

| Directory | Purpose |
|-----------|---------|
| `src/agents/` | Trading agent implementations |
| `src/core/` | Orchestrator, rules engine, executors |
| `src/planning/` | Temporal workflows, planning logic |
| `src/strategies/` | Trading strategies |
| `src/dashboard/` | Dashboard API server |
| `src/utils/` | Shared utilities |
| `dashboard/frontend/` | Vue 3 SPA |

When modifying one layer, review its dependencies:
- **Trading pipeline:** Check `src/core/orchestrator.py` alongside relevant agents/managers.
- **Planning:** Update Temporal workflow definitions AND DB persistence.
- **Dashboard:** Sync backend endpoints with frontend types (`dashboard/frontend/src/api.ts`).

## Submitting Changes

### Pull Request Process

1. Fork the repository and create a feature branch from `main`.
2. Make your changes, following the rules above.
3. Ensure all tests pass locally: `python -m pytest tests/ -x --tb=short`
4. Write a clear PR description explaining what changed and why.
5. Link any related issues.

### What We Look For in Reviews

- Tests pass (CI will verify).
- Domain separation rules are respected.
- No security regressions.
- Changes stay within architectural boundaries.
- Commit messages are descriptive.

## Reporting Issues

When filing a bug report, please include:

- Steps to reproduce the issue.
- Expected vs. actual behavior.
- Which exchange profile is affected (Coinbase/IBKR).
- Relevant log output (redact any credentials).
- Whether the issue occurs in paper or live mode.

## Code of Conduct

Be respectful and constructive. We're all here to build something useful.

## License

By contributing, you agree that your contributions will be licensed under the [Apache License 2.0](LICENSE).
