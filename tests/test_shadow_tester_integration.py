"""Settings advisor → ShadowTester integration test."""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from src.core.llm_advisor import ShadowTester


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_shadow_tester_propose_observe_promote_consume(tmp_path):
    state = tmp_path / "shadow.json"
    st = ShadowTester(
        state_path=str(state),
        min_observation_seconds=0,  # bypass time gate for the test
        min_observations=3,
        min_shadow_edge=0.001,
    )
    delta_id = st.propose(
        strategy="settings:trading",
        params={"min_confidence": 0.6},
        rationale="lower min_confidence to capture more signals",
    )
    assert st.get(delta_id) is not None
    # Three observations with shadow ahead of live should mark it promotable.
    for _ in range(3):
        st.observe(delta_id, shadow_pnl=0.005, live_pnl=0.001)
    promotable = st.list_promotable()
    assert any(d.delta_id == delta_id for d in promotable)
    # Consume removes it.
    consumed = st.consume(delta_id)
    assert consumed is not None
    assert st.get(delta_id) is None


def test_shadow_tester_persistence_roundtrip(tmp_path):
    state = tmp_path / "shadow.json"
    st1 = ShadowTester(state_path=str(state))
    delta_id = st1.propose(strategy="settings:risk", params={"x": 1}, rationale="r")
    # Reload from disk
    st2 = ShadowTester(state_path=str(state))
    assert st2.get(delta_id) is not None
