"""Tests for LLMAdvisor and ShadowTester (Phase 6)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.core.llm_advisor import (
    LLMAdvisor,
    PendingDelta,
    ShadowTester,
)


# --------------------------------------------------------------------- #
# LLMAdvisor
# --------------------------------------------------------------------- #

def _stub_llm(reply: str):
    return lambda prompt: reply


class TestLLMAdvisor:
    def test_classify_news_with_valid_json(self):
        reply = json.dumps({
            "sentiment": "BULLISH",
            "severity": 0.8,
            "affected_assets": ["BTC", "ETH"],
            "reasoning": "Spot ETF approved",
            "confidence": 0.9,
        })
        adv = LLMAdvisor(llm_client=_stub_llm(reply))
        c = adv.classify_news("ETF approved", "")
        assert c.sentiment == "bullish"
        assert c.severity == 0.8
        assert c.affected_assets == ("BTC", "ETH")
        assert c.confidence == 0.9

    def test_classify_news_with_markdown_fence(self):
        reply = "```json\n" + json.dumps({"sentiment": "bearish", "severity": 1.5, "confidence": 2.0}) + "\n```"
        adv = LLMAdvisor(llm_client=_stub_llm(reply))
        c = adv.classify_news("crash", "")
        assert c.sentiment == "bearish"
        assert c.severity == 1.0  # clipped
        assert c.confidence == 1.0

    def test_classify_news_unavailable_llm(self):
        adv = LLMAdvisor()  # null client
        c = adv.classify_news("hi")
        assert c.sentiment == "neutral"
        assert c.confidence == 0.0

    def test_classify_news_garbage_response(self):
        adv = LLMAdvisor(llm_client=_stub_llm("not json at all"))
        c = adv.classify_news("hi")
        assert c.sentiment == "neutral"

    def test_explain_anomaly(self):
        reply = json.dumps({
            "summary": "spread widened",
            "likely_causes": ["thin liquidity"],
            "suggested_actions": ["pause", "alert"],
        })
        adv = LLMAdvisor(llm_client=_stub_llm(reply))
        e = adv.explain_anomaly({"event": "wide_spread"})
        assert "spread" in e.summary
        assert "pause" in e.suggested_actions

    def test_explain_anomaly_handles_exception(self):
        def boom(_):
            raise RuntimeError("offline")
        adv = LLMAdvisor(llm_client=boom)
        e = adv.explain_anomaly({"event": "x"})
        assert "Anomaly" in e.summary or "anomaly" in e.summary.lower()

    def test_write_postmortem(self):
        reply = json.dumps({
            "title": "Bad fill",
            "timeline": "10:00 entered, 10:05 exited",
            "root_cause": "stale data",
            "lessons": ["validate data freshness"],
            "recommendations": ["add staleness check"],
        })
        adv = LLMAdvisor(llm_client=_stub_llm(reply))
        p = adv.write_postmortem({"loss_pct": -0.05})
        assert p.title == "Bad fill"
        assert "validate" in p.lessons[0]


# --------------------------------------------------------------------- #
# ShadowTester
# --------------------------------------------------------------------- #

class TestShadowTester:
    def test_propose_returns_id(self):
        st = ShadowTester(min_observation_seconds=0, min_observations=1, min_shadow_edge=0.0)
        d_id = st.propose("zscore", {"window": 30}, rationale="trial")
        assert isinstance(d_id, str) and len(d_id) > 0
        assert st.get(d_id).status == "pending"

    def test_observe_marks_promotable_after_gates(self):
        st = ShadowTester(
            min_observation_seconds=0, min_observations=2, min_shadow_edge=0.01,
        )
        d_id = st.propose("z", {"x": 1})
        st.observe(d_id, shadow_pnl=0.02, live_pnl=0.0)
        assert st.get(d_id).status == "pending"  # only 1 obs
        st.observe(d_id, shadow_pnl=0.0, live_pnl=0.0)
        d = st.get(d_id)
        assert d.status == "promotable"
        assert d.observations == 2

    def test_observe_below_edge_stays_pending(self):
        st = ShadowTester(
            min_observation_seconds=0, min_observations=1, min_shadow_edge=0.05,
        )
        d_id = st.propose("z", {"x": 1})
        st.observe(d_id, shadow_pnl=0.001, live_pnl=0.0)
        assert st.get(d_id).status == "pending"

    def test_observe_unknown_id_no_op(self):
        st = ShadowTester(min_observation_seconds=0, min_observations=1, min_shadow_edge=0.0)
        st.observe("nonexistent", shadow_pnl=1.0, live_pnl=0.0)
        # No exception.

    def test_observe_after_reject_no_change(self):
        st = ShadowTester(min_observation_seconds=0, min_observations=1, min_shadow_edge=0.0)
        d_id = st.propose("z", {"x": 1})
        st.reject(d_id, "manual")
        st.observe(d_id, shadow_pnl=1.0, live_pnl=0.0)
        d = st.get(d_id)
        assert d.status == "rejected"
        assert d.observations == 0

    def test_list_promotable_filters(self):
        st = ShadowTester(min_observation_seconds=0, min_observations=1, min_shadow_edge=0.0)
        a = st.propose("s1", {"x": 1})
        b = st.propose("s2", {"x": 2})
        st.observe(a, shadow_pnl=0.01, live_pnl=0.0)
        promo = st.list_promotable()
        ids = {d.delta_id for d in promo}
        assert a in ids and b not in ids

    def test_consume_removes(self):
        st = ShadowTester(min_observation_seconds=0, min_observations=1, min_shadow_edge=0.0)
        d_id = st.propose("z", {"x": 1})
        d = st.consume(d_id)
        assert d is not None and d.delta_id == d_id
        assert st.get(d_id) is None

    def test_persistence_roundtrip(self, tmp_path):
        path = tmp_path / "shadow.json"
        st = ShadowTester(state_path=str(path), min_observation_seconds=0,
                          min_observations=1, min_shadow_edge=0.0)
        d_id = st.propose("z", {"x": 1})
        st.observe(d_id, shadow_pnl=0.5, live_pnl=0.1)
        # Reload
        st2 = ShadowTester(state_path=str(path), min_observation_seconds=0,
                           min_observations=1, min_shadow_edge=0.0)
        d = st2.get(d_id)
        assert d is not None
        assert d.shadow_pnl == pytest.approx(0.5)
        assert d.live_pnl == pytest.approx(0.1)

    def test_observation_window_gate(self):
        # Even with edge & observations, must wait for time window.
        st = ShadowTester(
            min_observation_seconds=10_000,
            min_observations=1,
            min_shadow_edge=0.0,
        )
        d_id = st.propose("z", {"x": 1})
        st.observe(d_id, shadow_pnl=1.0, live_pnl=0.0)
        # Time barely advanced; should NOT be promotable.
        assert st.get(d_id).status == "pending"
