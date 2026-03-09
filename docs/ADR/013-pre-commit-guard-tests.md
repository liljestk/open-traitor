# ADR-013: Pre-Commit Guard Tests

**Status:** Accepted

## Context

Domain separation (ADR-003) and security invariants (ADR-009) are critical system properties. Violations discovered in production — mixed crypto/equity data, missing auth checks — are expensive to fix and potentially dangerous. We need automated enforcement at the earliest possible point in the development workflow.

## Decision

Install a **Git pre-commit hook** that runs two fast test suites before every commit. If either fails, the commit is blocked.

### Hook Implementation

```bash
#!/bin/sh
echo "🔍 Running pre-commit tests..."

python -m pytest tests/test_domain_separation.py -x --tb=short -q
python -m pytest tests/test_security.py -x --tb=short -q

# Both must pass or commit is blocked (exit 1)
```

Total execution time: < 5 seconds.

### Test Suite 1: Domain Separation (`test_domain_separation.py`)

**Frontend QueryKey Rule** (`TestFrontendQueryKeysIncludeProfile`):
- Static scan of all `.tsx` files in `dashboard/frontend/src/pages/`.
- Every `useQuery({ queryKey: [...] })` must include `profile` in the array.
- Exempt pages: `Settings.tsx`, `LLMProviders.tsx` (truly profile-independent).
- Exempt keys: `"settings"`, `"presets"`, `"auth-status"`, `"llm-providers"`.

**Backend SQL Rules:**
- Dashboard routes that query trades, portfolios, or reasoning must pass `exchange=` parameter.
- Redis keys must include a profile prefix.

### Test Suite 2: Security (`test_security.py`)

- Password/secret variables must not have default values.
- Auth endpoints require authentication middleware.
- No hardcoded secrets in source code.
- HMAC verification on CSRF token validation.
- Input sanitization on user-facing endpoints.

### Installation

```bash
cp scripts/hooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

The setup script (`setup.ps1`) handles this automatically during initial project configuration.

### Emergency Bypass

```bash
git commit --no-verify  # Skip pre-commit hook
```

Available for emergencies but discouraged. The project guidelines explicitly state: "Never bypass with `--no-verify` unless explicitly instructed."

## Consequences

**Benefits:**
- Domain separation violations are caught at commit time, not in production.
- Security regressions are blocked before they enter the codebase.
- Fast execution (< 5s) ensures minimal developer friction.
- Static analysis (no database or network required) means tests run anywhere.

**Risks:**
- Tests only check committed files; work-in-progress branches may have temporary violations.
- Frontend regex-based scanning may miss edge cases in unusual code patterns.

**Trade-offs:**
- Pre-commit adds ~5 seconds to every commit. This is negligible compared to the cost of a production domain-separation bug.
- The bypass option exists for legitimate emergencies, but its use should be reviewed.

**Follow-on:**
- CI pipeline should also run these tests as a second safety net for any bypassed commits.
- New invariants (e.g., API versioning, schema compatibility) can be added to the test suites without changing the hook.
