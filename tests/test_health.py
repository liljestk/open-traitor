"""Tests for health check Flask endpoints and update functions."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.core.health import app, update_health, check_component_health, _health_state, _lock


@pytest.fixture
def client():
    """Flask test client."""
    app.config["TESTING"] = True
    # Reset health state before each test
    with _lock:
        _health_state.update({
            "status": "starting",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "last_cycle": None,
            "cycle_count": 0,
            "components": {},
            "cycle_duration_s": None,
        })
    with app.test_client() as c:
        yield c


class TestUpdateHealth:
    def test_basic_update(self, client):
        update_health(status="ok", cycle_count=5)
        with _lock:
            assert _health_state["status"] == "ok"
            assert _health_state["cycle_count"] == 5
            assert _health_state["last_cycle"] is not None

    def test_components(self, client):
        update_health(components={"ollama": {"status": "healthy"}})
        with _lock:
            assert _health_state["components"]["ollama"]["status"] == "healthy"

    def test_cycle_duration(self, client):
        update_health(cycle_duration_s=12.5)
        with _lock:
            assert _health_state["cycle_duration_s"] == 12.5


class TestHealthEndpoint:
    def test_healthy_starting(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "starting"

    def test_healthy_ok(self, client):
        update_health(status="ok", cycle_count=1)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"

    def test_stale_returns_503(self, client):
        # Simulate a stale last_cycle (>5 minutes old)
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with _lock:
            _health_state["status"] = "ok"
            _health_state["last_cycle"] = old
        resp = client.get("/health")
        assert resp.status_code == 503
        data = resp.get_json()
        assert data["status"] == "stale"

    def test_error_status_returns_503(self, client):
        update_health(status="error", cycle_count=0)
        resp = client.get("/health")
        assert resp.status_code == 503


class TestComponentsEndpoint:
    def test_empty_components(self, client):
        resp = client.get("/health/components")
        assert resp.status_code == 200
        assert resp.get_json() == {}

    def test_returns_components(self, client):
        update_health(components={"redis": {"status": "healthy"}})
        resp = client.get("/health/components")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["redis"]["status"] == "healthy"


class TestMetricsEndpoint:
    def test_prometheus_format(self, client):
        update_health(status="ok", cycle_count=42, cycle_duration_s=3.14)
        resp = client.get("/metrics")
        assert resp.status_code == 200
        text = resp.data.decode()
        assert "autotraitor_up 1" in text
        assert "autotraitor_cycles_total 42" in text
        assert "autotraitor_cycle_duration_seconds 3.14" in text

    def test_component_metrics(self, client):
        update_health(
            status="ok",
            components={
                "ollama": {"status": "healthy"},
                "redis": {"status": "unhealthy"},
            },
        )
        resp = client.get("/metrics")
        text = resp.data.decode()
        assert 'autotraitor_component_healthy{component="ollama"} 1' in text
        assert 'autotraitor_component_healthy{component="redis"} 0' in text

    def test_down_when_not_ok(self, client):
        update_health(status="error")
        resp = client.get("/metrics")
        text = resp.data.decode()
        assert "autotraitor_up 0" in text


class TestCheckComponentHealth:
    @patch("requests.get")
    def test_ollama_healthy(self, mock_get):
        mock_resp = mock_get.return_value
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.json.return_value = {"models": [{"name": "m1"}, {"name": "m2"}]}

        result = check_component_health(ollama_url="http://localhost:11434")
        assert result["ollama"]["status"] == "healthy"
        assert result["ollama"]["models"] == 2

    @patch("requests.get")
    def test_ollama_unreachable(self, mock_get):
        mock_get.side_effect = ConnectionError("dead")
        result = check_component_health()
        assert result["ollama"]["status"] == "unhealthy"

    def test_redis_not_configured(self):
        result = check_component_health(redis_client=None)
        assert result["redis"]["status"] == "not_configured"

    def test_redis_healthy(self):
        mock_redis = type("Redis", (), {
            "ping": lambda self: True,
            "info": lambda self, section: {"used_memory": 5 * 1024 * 1024},
        })()
        result = check_component_health(redis_client=mock_redis)
        assert result["redis"]["status"] == "healthy"
        assert result["redis"]["used_memory_mb"] == 5.0
