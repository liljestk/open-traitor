"""Smoke tests for SmartsMixin — DDL strings parse-able, _f helper."""
import pytest

from src.utils import stats_smarts as ss


def test_ddl_constants_present():
    assert isinstance(ss._SMARTS_DDL, tuple)
    assert len(ss._SMARTS_DDL) > 5
    # Each DDL stmt has CREATE
    for stmt in ss._SMARTS_DDL:
        assert "CREATE" in stmt.upper()


def test_to_iso_handles_none():
    assert ss._to_iso(None) is None


def test_to_iso_handles_string():
    out = ss._to_iso("2025-01-01T00:00:00Z")
    assert out is not None
    assert out.tzinfo is not None


def test_smarts_mixin_methods_exist():
    cls = ss.SmartsMixin
    for name in (
        "_init_smarts_schema",
        "write_feature_attribution",
        "get_feature_brier",
        "get_bandit_state",
        "upsert_bandit",
        "write_counterfactual",
        "upsert_lead_lag",
        "get_lead_lag_for",
        "upsert_upcoming_events",
        "get_upcoming_events",
        "write_decision_drift",
        "write_reasoning_judge",
        "write_l2_snapshot",
        "upsert_onchain",
        "get_recent_onchain",
        "write_shadow_decision",
    ):
        assert hasattr(cls, name), f"SmartsMixin missing {name}"
