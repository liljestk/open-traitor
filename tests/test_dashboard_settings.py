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
