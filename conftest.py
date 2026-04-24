"""
Root conftest.py — Shared fixtures for all test suites.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# Ensure tests never hit real APIs
os.environ.setdefault("COINBASE_API_KEY", "test-key")
os.environ.setdefault("COINBASE_API_SECRET", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


# ── Disable real rate limiting during tests ────────────────────────────
# The module-level RateLimiter singleton accumulates timestamps across the
# entire test session and a hot loop in ``acquire`` can deadlock with
# background threads from prior tests. Patching the limiter once at
# import time keeps every ``acquire``/``wait`` an instant no-op without
# changing production behavior.
def _install_noop_rate_limiter() -> None:
    try:
        from src.utils import rate_limiter as _rl

        def _noop_acquire(self, service, block=True, timeout=30.0):
            return True

        async def _noop_async_acquire(self, service, timeout=30.0):
            return True

        def _noop_wait(self, service, timeout=30.0):
            return None

        async def _noop_async_wait(self, service, timeout=30.0):
            return None

        _rl.RateLimiter._orig_acquire = _rl.RateLimiter.acquire  # type: ignore[attr-defined]
        _rl.RateLimiter._orig_async_acquire = _rl.RateLimiter.async_acquire  # type: ignore[attr-defined]
        _rl.RateLimiter._orig_wait = _rl.RateLimiter.wait  # type: ignore[attr-defined]
        _rl.RateLimiter._orig_async_wait = _rl.RateLimiter.async_wait  # type: ignore[attr-defined]
        _rl.RateLimiter.acquire = _noop_acquire  # type: ignore[method-assign]
        _rl.RateLimiter.async_acquire = _noop_async_acquire  # type: ignore[method-assign]
        _rl.RateLimiter.wait = _noop_wait  # type: ignore[method-assign]
        _rl.RateLimiter.async_wait = _noop_async_wait  # type: ignore[method-assign]
    except Exception:
        pass


_install_noop_rate_limiter()


@pytest.fixture
def base_config() -> dict:
    """Minimal config dict used across tests."""
    return {
        "trading": {
            "pairs": ["BTC-EUR", "ETH-EUR"],
            "max_active_pairs": 5,
            "portfolio_scaling": True,
        },
        "absolute_rules": {
            "max_single_trade": 500,
            "max_daily_spend": 2000,
            "max_daily_loss": 300,
            "max_portfolio_risk_pct": 0.20,
            "require_approval_above": 200,
            "min_trade_interval_seconds": 60,
            "max_trades_per_day": 20,
            "max_cash_per_trade_pct": 0.25,
            "emergency_stop_portfolio": 5000,
            "always_use_stop_loss": True,
            "max_stop_loss_pct": 0.05,
        },
        "fees": {
            "model_type": "crypto_percentage",
            "trade_fee_pct": 0.006,
            "maker_fee_pct": 0.004,
            "safety_margin": 1.5,
            "min_gain_after_fees_pct": 0.005,
            "min_trade_usd": 1.0,
        },
        "analysis": {
            "technical": {},
        },
        "strategies": {
            "ema_crossover": {},
            "bollinger_reversion": {},
        },
        "news": {
            "max_articles": 50,
        },
    }
