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
