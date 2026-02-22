"""
Health Check HTTP Endpoint — Lightweight Flask server for monitoring.
Docker uses this to determine if the agent is truly functional.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from flask import Flask, jsonify

from src.utils.logger import get_logger

logger = get_logger("core.health")

app = Flask(__name__)

# Global references set by the orchestrator
_health_state: dict[str, Any] = {
    "status": "starting",
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_cycle": None,
    "cycle_count": 0,
    "components": {},
    "cycle_duration_s": None,
}
_lock = threading.Lock()


def update_health(
    status: str = "ok",
    cycle_count: int = 0,
    components: Optional[dict] = None,
    cycle_duration_s: Optional[float] = None,
) -> None:
    """Update health state (called by orchestrator each cycle)."""
    global _health_state
    with _lock:
        _health_state["status"] = status
        _health_state["last_cycle"] = datetime.now(timezone.utc).isoformat()
        _health_state["cycle_count"] = cycle_count
        if components:
            _health_state["components"] = components
        if cycle_duration_s is not None:
            _health_state["cycle_duration_s"] = cycle_duration_s


def check_component_health(
    ollama_url: str = "http://localhost:11434",
    redis_client=None,
) -> dict:
    """Check health of all dependent services."""
    components = {}

    # Ollama
    try:
        import requests
        resp = requests.get(f"{ollama_url}/api/tags", timeout=5)
        components["ollama"] = {
            "status": "healthy" if resp.status_code == 200 else "degraded",
            "models": len(resp.json().get("models", [])) if resp.ok else 0,
        }
    except Exception as e:
        components["ollama"] = {"status": "unhealthy", "error": str(e)}

    # Redis
    if redis_client:
        try:
            redis_client.ping()
            info = redis_client.info("memory")
            components["redis"] = {
                "status": "healthy",
                "used_memory_mb": round(info.get("used_memory", 0) / 1024 / 1024, 1),
            }
        except Exception as e:
            components["redis"] = {"status": "unhealthy", "error": str(e)}
    else:
        components["redis"] = {"status": "not_configured"}

    return components


@app.route("/health")
def health():
    """
    Health check endpoint.
    Returns 200 if healthy, 503 if degraded.

    Docker HEALTHCHECK uses this.
    """
    with _lock:
        state = dict(_health_state)

    # Check if the agent is actually running (last cycle within 5 minutes)
    is_healthy = state["status"] in ("ok", "starting")

    if state["last_cycle"]:
        last = datetime.fromisoformat(state["last_cycle"])
        age = (datetime.now(timezone.utc) - last).total_seconds()
        if age > 300:  # 5 minutes without a cycle = problem
            is_healthy = False
            state["status"] = "stale"

    status_code = 200 if is_healthy else 503
    return jsonify(state), status_code


@app.route("/health/components")
def health_components():
    """Detailed component health."""
    # H12: acquire lock before reading shared state
    with _lock:
        components = dict(_health_state.get("components", {}))
    return jsonify(components)


@app.route("/metrics")
def metrics():
    """Prometheus-compatible metrics endpoint."""
    with _lock:
        state = dict(_health_state)

    # Build Prometheus text format
    lines = [
        "# HELP autotraitor_up Whether the agent is running",
        "# TYPE autotraitor_up gauge",
        f"autotraitor_up {1 if state['status'] == 'ok' else 0}",
        "",
        "# HELP autotraitor_cycles_total Total trading cycles completed",
        "# TYPE autotraitor_cycles_total counter",
        f"autotraitor_cycles_total {state['cycle_count']}",
    ]

    # Component health
    components = state.get("components", {})
    for name, comp in components.items():
        healthy = 1 if comp.get("status") == "healthy" else 0
        lines.append(f"autotraitor_component_healthy{{component=\"{name}\"}} {healthy}")

    # Cycle duration gauge
    cd = state.get("cycle_duration_s")
    if cd is not None:
        lines.extend([
            "",
            "# HELP autotraitor_cycle_duration_seconds Wall-clock duration of the last trading cycle",
            "# TYPE autotraitor_cycle_duration_seconds gauge",
            f"autotraitor_cycle_duration_seconds {cd}",
        ])

    return "\n".join(lines) + "\n", 200, {"Content-Type": "text/plain"}


def start_health_server(port: int = 8080) -> threading.Thread:
    """Start the health check server in a background thread."""
    def _run():
        try:
            from waitress import serve
            serve(app, host="127.0.0.1", port=port, threads=2, _quiet=True)
        except ImportError:
            # Fallback to Flask dev server if waitress not installed
            import logging
            log = logging.getLogger("werkzeug")
            log.setLevel(logging.WARNING)
            logger.warning("waitress not installed — using Flask dev server")
            app.run(host="127.0.0.1", port=port, threaded=True)

    thread = threading.Thread(target=_run, daemon=True, name="health-server")
    thread.start()
    logger.info(f"🏥 Health check server running on port {port}")
    return thread
