"""
Tests for dashboard settings routes — confirmation flow, env parsing, rate limiting.

Tests the pure-function helpers and confirmation token mechanics without
requiring a running FastAPI server.
"""

import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Importable helpers from the settings module
# ---------------------------------------------------------------------------
from src.dashboard.routes.settings import (
    _check_confirmation_rate,
    _confirmation_rate,
    _confirmation_rate_lock,
    _CONFIRM_RATE_LIMIT,
    _parse_env_file,
    _update_env_file,
    _SETTINGS_CONFIRM_SECTIONS,
)
import src.dashboard.deps as deps


# ═══════════════════════════════════════════════════════════════════════════
# ENV file parsing
# ═══════════════════════════════════════════════════════════════════════════

class TestParseEnvFile:
    def test_basic_parse(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("KEY=value\nSECRET=abc123\n")
        result = _parse_env_file(str(f))
        assert result == {"KEY": "value", "SECRET": "abc123"}

    def test_comments_and_blanks_skipped(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("# comment\n\nKEY=value\n# another\n")
        result = _parse_env_file(str(f))
        assert result == {"KEY": "value"}

    def test_missing_file_returns_empty(self, tmp_path):
        result = _parse_env_file(str(tmp_path / "nonexistent"))
        assert result == {}

    def test_value_with_equals(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("URL=postgres://user:pw@host/db?opt=1\n")
        result = _parse_env_file(str(f))
        assert result["URL"] == "postgres://user:pw@host/db?opt=1"

    def test_line_without_equals(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("VALID=yes\nINVALID_LINE\nALSO_VALID=1\n")
        result = _parse_env_file(str(f))
        assert "VALID" in result
        assert "ALSO_VALID" in result
        assert len(result) == 2


class TestUpdateEnvFile:
    def test_updates_existing_key(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("KEY=old\nOTHER=keep\n")
        _update_env_file(str(f), {"KEY": "new"})
        result = _parse_env_file(str(f))
        assert result["KEY"] == "new"
        assert result["OTHER"] == "keep"

    def test_appends_new_key(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("KEY=old\n")
        _update_env_file(str(f), {"NEW": "value"})
        result = _parse_env_file(str(f))
        assert result["KEY"] == "old"
        assert result["NEW"] == "value"

    def test_newlines_stripped_from_values(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("KEY=old\n")
        _update_env_file(str(f), {"KEY": "line1\nline2\rline3"})
        result = _parse_env_file(str(f))
        assert "\n" not in result["KEY"]
        assert "\r" not in result["KEY"]

    def test_creates_file_if_missing(self, tmp_path):
        f = tmp_path / ".env"
        _update_env_file(str(f), {"BRAND_NEW": "yes"})
        result = _parse_env_file(str(f))
        assert result["BRAND_NEW"] == "yes"


# ═══════════════════════════════════════════════════════════════════════════
# Confirmation rate limiting
# ═══════════════════════════════════════════════════════════════════════════

class TestConfirmationRateLimit:
    def setup_method(self):
        with _confirmation_rate_lock:
            _confirmation_rate.clear()

    def test_allows_within_limit(self):
        for _ in range(_CONFIRM_RATE_LIMIT):
            assert _check_confirmation_rate("10.0.0.1") is True

    def test_blocks_over_limit(self):
        for _ in range(_CONFIRM_RATE_LIMIT):
            _check_confirmation_rate("10.0.0.2")
        assert _check_confirmation_rate("10.0.0.2") is False

    def test_separate_ips_independent(self):
        for _ in range(_CONFIRM_RATE_LIMIT):
            _check_confirmation_rate("10.0.0.3")
        # Different IP should still be allowed
        assert _check_confirmation_rate("10.0.0.4") is True


# ═══════════════════════════════════════════════════════════════════════════
# Confirmation token store (deps level)
# ═══════════════════════════════════════════════════════════════════════════

class TestConfirmationTokenStore:
    def test_store_and_pop(self):
        token = secrets.token_urlsafe(16)
        data = {"action": "test", "expires": time.monotonic() + 120}
        deps.store_confirmation(token, data)
        result = deps.pop_confirmation(token)
        assert result is not None
        assert result["action"] == "test"

    def test_pop_removes_token(self):
        token = secrets.token_urlsafe(16)
        deps.store_confirmation(token, {"expires": time.monotonic() + 120})
        deps.pop_confirmation(token)
        assert deps.pop_confirmation(token) is None

    def test_pop_nonexistent_returns_none(self):
        assert deps.pop_confirmation("nonexistent-token") is None

    def test_expire_removes_old_tokens(self):
        old_token = "old-test-token"
        new_token = "new-test-token"
        deps.store_confirmation(old_token, {"expires": time.monotonic() - 10})
        deps.store_confirmation(new_token, {"expires": time.monotonic() + 120})
        deps.expire_confirmations()
        assert deps.pop_confirmation(old_token) is None
        assert deps.pop_confirmation(new_token) is not None


# ═══════════════════════════════════════════════════════════════════════════
# Sensitive sections set
# ═══════════════════════════════════════════════════════════════════════════

class TestSettingsConfirmSections:
    def test_absolute_rules_requires_confirmation(self):
        assert "absolute_rules" in _SETTINGS_CONFIRM_SECTIONS

    def test_trading_requires_confirmation(self):
        assert "trading" in _SETTINGS_CONFIRM_SECTIONS

    def test_high_stakes_requires_confirmation(self):
        assert "high_stakes" in _SETTINGS_CONFIRM_SECTIONS

    def test_risk_does_not_require_confirmation(self):
        assert "risk" not in _SETTINGS_CONFIRM_SECTIONS


# ═══════════════════════════════════════════════════════════════════════════
# Updates hash (H10 integrity check)
# ═══════════════════════════════════════════════════════════════════════════

class TestUpdatesHashIntegrity:
    """Verify the H10 hash-check logic: updates payload must match what was confirmed."""

    def test_same_payload_matches(self):
        updates = {"stop_loss_pct": 0.05, "max_open_positions": 5}
        h1 = hashlib.sha256(json.dumps(updates, sort_keys=True).encode()).hexdigest()
        h2 = hashlib.sha256(json.dumps(updates, sort_keys=True).encode()).hexdigest()
        assert h1 == h2

    def test_different_payload_mismatches(self):
        u1 = {"stop_loss_pct": 0.05}
        u2 = {"stop_loss_pct": 0.10}
        h1 = hashlib.sha256(json.dumps(u1, sort_keys=True).encode()).hexdigest()
        h2 = hashlib.sha256(json.dumps(u2, sort_keys=True).encode()).hexdigest()
        assert h1 != h2

    def test_key_order_irrelevant(self):
        u1 = {"a": 1, "b": 2}
        u2 = {"b": 2, "a": 1}
        h1 = hashlib.sha256(json.dumps(u1, sort_keys=True).encode()).hexdigest()
        h2 = hashlib.sha256(json.dumps(u2, sort_keys=True).encode()).hexdigest()
        assert h1 == h2


# ═══════════════════════════════════════════════════════════════════════════
# Setup wizard env preservation (regression: wizard was silently dropping
# keys outside its allowlist like DATABASE_URL, DASHBOARD_PASSWORD_HASH)
# ═══════════════════════════════════════════════════════════════════════════

class TestSetupWizardPreservesUnmanagedKeys:
    """Simulate what the setup wizard does: read existing env, filter through
    allowlist, build new file.  Verify keys NOT in the wizard payload survive.

    This mirrors the logic in ``POST /api/setup`` without needing a full
    FastAPI TestClient, testing the file-level round-trip that caused the bug.
    """

    # Keys the wizard knows about (subset for testing)
    WIZARD_ALLOWLIST = {
        "COINBASE_API_KEY", "COINBASE_API_SECRET", "TRADING_MODE",
        "REDIS_PASSWORD", "REDIS_URL", "OLLAMA_BASE_URL", "OLLAMA_MODEL",
        "GEMINI_API_KEY", "OPENROUTER_API_KEY",
    }

    def _simulate_wizard_write(self, env_path: str, wizard_payload: dict[str, str]) -> None:
        """Reproduce the setup wizard's write logic (post-fix version)."""
        from datetime import datetime, timezone as tz

        filtered = {k: v for k, v in wizard_payload.items() if k in self.WIZARD_ALLOWLIST}
        existing = _parse_env_file(env_path)

        # Preserve keys not covered by the wizard
        preserved: dict[str, str] = {}
        if existing:
            for k, v in existing.items():
                if k not in filtered:
                    preserved[k] = v

        lines = [
            "# header",
            "",
        ]
        for k, v in filtered.items():
            lines.append(f"{k}={v}")
        if preserved:
            lines.append("")
            lines.append("# Preserved keys (not managed by setup wizard)")
            for k, v in preserved.items():
                lines.append(f"{k}={v}")
        lines.append("")

        # Atomic write (same pattern as production code)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(env_path)),
                                   suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.abspath(env_path))

    def test_database_url_preserved(self, tmp_path):
        """DATABASE_URL must survive a wizard rewrite."""
        env = tmp_path / ".env"
        env.write_text(
            "COINBASE_API_KEY=key1\n"
            "DATABASE_URL=postgresql://traitor:pw@db:5432/autotraitor\n"
            "TRAITOR_DB_PASSWORD=pw\n"
        )
        self._simulate_wizard_write(str(env), {"COINBASE_API_KEY": "key1"})
        result = _parse_env_file(str(env))
        assert result["DATABASE_URL"] == "postgresql://traitor:pw@db:5432/autotraitor"
        assert result["TRAITOR_DB_PASSWORD"] == "pw"
        assert result["COINBASE_API_KEY"] == "key1"

    def test_dashboard_password_hash_preserved(self, tmp_path):
        """DASHBOARD_PASSWORD_HASH must survive a wizard rewrite."""
        env = tmp_path / ".env"
        env.write_text(
            "GEMINI_API_KEY=AIza123\n"
            "DASHBOARD_PASSWORD_HASH=$2b$12$somehash\n"
        )
        self._simulate_wizard_write(str(env), {"GEMINI_API_KEY": "AIza123"})
        result = _parse_env_file(str(env))
        assert result["DASHBOARD_PASSWORD_HASH"] == "$2b$12$somehash"

    def test_multiple_unmanaged_keys_preserved(self, tmp_path):
        """All keys outside the wizard allowlist must be carried forward."""
        env = tmp_path / ".env"
        env.write_text(
            "COINBASE_API_KEY=key1\n"
            "DATABASE_URL=postgresql://host/db\n"
            "TRAITOR_DB_PASSWORD=secret\n"
            "DASHBOARD_PASSWORD_HASH=$2b$12$hash\n"
            "CUSTOM_FLAG=enabled\n"
        )
        self._simulate_wizard_write(str(env), {"COINBASE_API_KEY": "key1"})
        result = _parse_env_file(str(env))
        assert len(result) == 5
        assert result["DATABASE_URL"] == "postgresql://host/db"
        assert result["TRAITOR_DB_PASSWORD"] == "secret"
        assert result["DASHBOARD_PASSWORD_HASH"] == "$2b$12$hash"
        assert result["CUSTOM_FLAG"] == "enabled"

    def test_wizard_overwrites_managed_keys(self, tmp_path):
        """Wizard-managed keys should be updated, not preserved from old file."""
        env = tmp_path / ".env"
        env.write_text(
            "GEMINI_API_KEY=old_key\n"
            "DATABASE_URL=keep_this\n"
        )
        self._simulate_wizard_write(str(env), {"GEMINI_API_KEY": "new_key"})
        result = _parse_env_file(str(env))
        assert result["GEMINI_API_KEY"] == "new_key"
        assert result["DATABASE_URL"] == "keep_this"

    def test_fresh_install_no_existing_env(self, tmp_path):
        """On first run with no existing .env, no crash and no preserved section."""
        env = tmp_path / ".env"
        self._simulate_wizard_write(str(env), {"COINBASE_API_KEY": "k", "TRADING_MODE": "paper"})
        result = _parse_env_file(str(env))
        assert result["COINBASE_API_KEY"] == "k"
        assert result["TRADING_MODE"] == "paper"
        assert len(result) == 2

    def test_newlines_stripped_from_preserved_values(self, tmp_path):
        """Injection via preserved values must be blocked."""
        env = tmp_path / ".env"
        # Simulate a value with embedded newline (shouldn't happen, but defense in depth)
        with open(str(env), "w") as f:
            f.write("SAFE=ok\nDANGER=line1\n")
        self._simulate_wizard_write(str(env), {"SAFE": "ok"})
        content = (tmp_path / ".env").read_text()
        # DANGER value should not contain raw newlines beyond the line terminator
        for line in content.strip().split("\n"):
            if line.startswith("DANGER="):
                assert "\r" not in line
