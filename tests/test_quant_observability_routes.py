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
def clean_singletons():
    """Reset singletons before/after each test."""
    deps.capital_allocator = None
    deps.self_healing = None
    deps.signal_edge_library = None
    yield
    deps.capital_allocator = None
    deps.self_healing = None
    deps.signal_edge_library = None


# --------------------------------------------------------------------- #

class TestAllocator:
    def test_unavailable_when_no_singleton(self, client):
        r = client.get("/api/quant/allocator?profile=coinbase")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert body["weights"] == {}

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
    def test_unavailable(self, client):
        r = client.get("/api/quant/edges?profile=coinbase")
        body = r.json()
        assert body["available"] is False
        assert body["edges"] == []

    def test_with_library(self, client):
        deps.signal_edge_library = SimpleNamespace(
            leaderboard=lambda n: [{"signal": "rsi", "edge_bps": 12.3}][:n]
        )
        r = client.get("/api/quant/edges?profile=coinbase&limit=5")
        body = r.json()
        assert body["available"] is True
        assert body["edges"][0]["signal"] == "rsi"


class TestHealing:
    def test_unavailable(self, client):
        r = client.get("/api/quant/healing?profile=coinbase")
        body = r.json()
        assert body["available"] is False
        assert body["strategies"] == []

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
