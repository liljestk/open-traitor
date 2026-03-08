## Premium Request Optimization
- **Chain Tasks:** When given a multi-step objective, do NOT stop after the first step. Perform the full implementation, including edge cases and basic unit tests, in a single response.
- **Self-Correction:** If you realize a mistake while generating code, correct it immediately within the same output rather than waiting for user feedback.
- **Verify Imports:** Always double-check that all required imports/dependencies for your code are included in the output to avoid "fix the import" follow-up requests.

## Test & Commit Discipline
- **Run Tests After Every Change:** After completing code changes, always run `python -m pytest tests/ -x --tb=short` and fix any failures before considering the task done. Never leave broken tests.
- **Fix Test Failures Immediately:** If tests fail, diagnose and fix the root cause in the same response. Do not defer test fixes to the user.
- **Commit Smartly:** After all tests pass, stage changes, write a descriptive commit message explaining the *why* (not just "updated files"), and push. Group related changes into a single logical commit; split unrelated changes into separate commits.
- **Pre-Commit Hook:** A pre-commit hook runs `test_domain_separation.py` and `test_security.py` on every commit. If tests fail, the commit is blocked. Never bypass with `--no-verify` unless explicitly instructed.
- **Push After Commit:** Always `git push` after committing so the remote stays in sync. Don't leave unpushed commits.

## Domain Separation (CRITICAL)
- **Frontend queryKey Rule:** Every `useQuery` call in dashboard pages MUST include `profile` in its `queryKey` array. The static test `TestFrontendQueryKeysIncludeProfile` enforces this — if it fails, the commit is blocked.
- **Backend SQL Rule:** All SQL queries for trades, reasoning samples, and portfolio data MUST filter by exchange when a profile is active.
- **Redis Key Rule:** All Redis keys must be prefixed with the profile name (e.g., `coinbase:trailing_stops:state`).
- **Never Mix Domains:** Crypto (coinbase) and equity (ibkr) data must never appear together. No "All Assets" or "Default" combined views.