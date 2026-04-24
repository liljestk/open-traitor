"""Tests for /api/quant/* observability endpoints (Phase 8)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.dashboard.deps as deps
from src.dashboard.routes.quant_observability import router


# Disable the API key middleware concern by hitting the router in isolation.
@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_singletons(tmp_path, monkeypatch):
    """Reset singletons + per-profile registries before each test.

    Also chdir into a tmp_path so per-profile factories that write to
    data/<profile>/... don't pollute the workspace.
    """
    monkeypatch.chdir(tmp_path)
    deps.capital_allocator = None
    deps.self_healing = None
    deps.signal_edge_library = None
    deps._capital_allocators.clear()
    deps._self_healers.clear()
    deps._signal_edge_libraries.clear()
    yield
    deps.capital_allocator = None
    deps.self_healing = None
    deps.signal_edge_library = None
    deps._capital_allocators.clear()
    deps._self_healers.clear()
    deps._signal_edge_libraries.clear()


# --------------------------------------------------------------------- #

class TestAllocator:
    def test_lazy_factory_creates_per_profile_allocator(self, client):
        # No legacy singleton + no override => factory creates a real instance.
        r = client.get("/api/quant/allocator?profile=coinbase")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["profile"] == "coinbase"
        # weights() returns a dict
        assert isinstance(body["weights"], dict)

    def test_returns_weights_when_wired(self, client):
        deps.capital_allocator = SimpleNamespace(
            weights=lambda: {"zscore": 0.4, "momentum": 0.6},
            state=SimpleNamespace(
                cumulative_pnl={"zscore": 0.1, "momentum": 0.2},
                sample_count=42,
                last_updated=123.0,
            ),
        )
        r = client.get("/api/quant/allocator?profile=coinbase")
        body = r.json()
        assert body["available"] is True
        assert body["weights"]["momentum"] == 0.6
        assert body["state"]["sample_count"] == 42


class TestEdges:
    def test_lazy_factory_creates_per_profile_library(self, client):
        # Empty library => available True with empty edges list.
        r = client.get("/api/quant/edges?profile=coinbase&regime=ranging")
        body = r.json()
        assert body["available"] is True
        assert body["edges"] == []

    def test_real_library_with_recorded_samples(self, client):
        # Wire a real SignalEdgeLibrary, register a signal, record samples,
        # then verify the leaderboard reflects them.
        from src.analysis.signal_edge_library import (
            SignalEdgeLibrary, InMemorySignalEdgeStore,
        )
        lib = SignalEdgeLibrary(store=InMemorySignalEdgeStore(), exchange="coinbase")
        lib.register("rsi", lambda candles: 0.5)
        for i in range(40):
            lib.record_sample(
                signal_name="rsi", regime="ranging", score=0.5,
                forward_return=0.01, pair="BTC-EUR",
            )
        deps.signal_edge_library = lib
        r = client.get("/api/quant/edges?profile=coinbase&regime=ranging&limit=5")
        body = r.json()
        assert body["available"] is True
        assert body["profile"] == "coinbase"
        assert any(e.get("signal_name") == "rsi" for e in body["edges"])

    def test_with_library(self, client):
        deps.signal_edge_library = SimpleNamespace(
            leaderboard=lambda n: [{"signal": "rsi", "edge_bps": 12.3}][:n]
        )
        r = client.get("/api/quant/edges?profile=coinbase&limit=5")
        body = r.json()
        assert body["available"] is True
        assert body["edges"][0]["signal"] == "rsi"


class TestHealing:
    def test_lazy_factory_creates_per_profile_healer(self, client):
        r = client.get("/api/quant/healing?profile=coinbase")
        body = r.json()
        assert body["available"] is True
        assert body["strategies"] == []  # nothing recorded yet

    def test_with_controller(self, client):
        from src.core.self_healing import SelfHealingController

        sh = SelfHealingController(disable_window=2, disable_sharpe_floor=-0.5)
        sh.heartbeat("quant")
        for _ in range(2):
            sh.record_sharpe("zscore", -1.0)
        sh.evaluate_strategy("zscore")
        deps.self_healing = sh

        r = client.get("/api/quant/healing?profile=coinbase")
        body = r.json()
        assert body["available"] is True
        names = [s["name"] for s in body["strategies"]]
        assert "zscore" in names
        assert "quant" in body["tiers"]


class TestDomainSeparation:
    """Both profiles must work and never share state."""

    def test_separate_allocators_per_profile(self, client):
        c = deps.get_capital_allocator_for("coinbase")
        i = deps.get_capital_allocator_for("ibkr")
        assert c is not None and i is not None
        assert c is not i
        # State paths are profile-scoped.
        assert "coinbase" in str(c.state_path)
        assert "ibkr" in str(i.state_path)

    def test_alias_resolution(self, client):
        # 'crypto' -> 'coinbase', 'equity' -> 'ibkr'
        r1 = client.get("/api/quant/allocator?profile=crypto")
        r2 = client.get("/api/quant/allocator?profile=equity")
        assert r1.json()["profile"] == "coinbase"
        assert r2.json()["profile"] == "ibkr"

    def test_endpoints_for_both_profiles(self, client):
        for profile in ("coinbase", "ibkr"):
            for path in ("allocator", "healing", "promotions"):
                r = client.get(f"/api/quant/{path}?profile={profile}")
                assert r.status_code == 200, f"{path}/{profile} failed"
                assert r.json()["profile"] == profile


class TestPromotions:
    def test_no_audit_file(self, client, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        r = client.get("/api/quant/promotions?profile=test_profile")
        body = r.json()
        assert body["available"] is True
        assert body["promotions"] == []
        assert body["count"] == 0

    def test_reads_jsonl(self, client, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        audit_dir = tmp_path / "data" / "test_profile" / "audit"
        audit_dir.mkdir(parents=True)
        path = audit_dir / "wfo_promotions.jsonl"
        path.write_text(
            json.dumps({"strategy": "z", "promoted": True}) + "\n"
            + json.dumps({"strategy": "m", "promoted": False}) + "\n"
        )
        r = client.get("/api/quant/promotions?profile=test_profile")
        body = r.json()
        assert body["count"] == 2
        assert body["promotions"][0]["strategy"] == "z"

    def test_skips_corrupt_lines(self, client, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        audit_dir = tmp_path / "data" / "coinbase" / "audit"
        audit_dir.mkdir(parents=True)
        (audit_dir / "wfo_promotions.jsonl").write_text(
            "not json\n" + json.dumps({"ok": True}) + "\n"
        )
        r = client.get("/api/quant/promotions?profile=coinbase")
        body = r.json()
        assert body["count"] == 1
