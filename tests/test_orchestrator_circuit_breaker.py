from __future__ import annotations

from types import SimpleNamespace

from src.core.orchestrator import Orchestrator


def test_circuit_breaker_halt_refreshes_holdings_before_recovery(monkeypatch):
    calls: list[str | tuple[str, dict]] = []
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.state = SimpleNamespace(circuit_breaker_triggered=True)
    orchestrator.redis = None

    def record_event() -> None:
        calls.append("record")

    def refresh_holdings() -> None:
        calls.append("refresh")

    def recover() -> None:
        calls.append("recover")

    def sync_to_redis() -> None:
        calls.append("sync")

    def fake_component_health(**_: object) -> dict:
        calls.append("components")
        return {"redis": {"status": "healthy"}}

    def fake_update_health(**kwargs: object) -> None:
        calls.append(("health", kwargs))

    orchestrator._record_circuit_breaker_halt_event = record_event
    orchestrator.holdings_manager = SimpleNamespace(maybe_refresh_holdings=refresh_holdings)
    orchestrator._try_circuit_breaker_recovery = recover
    orchestrator.state_manager = SimpleNamespace(sync_to_redis=sync_to_redis)

    monkeypatch.setattr("src.core.orchestrator.check_component_health", fake_component_health)
    monkeypatch.setattr("src.core.orchestrator.update_health", fake_update_health)

    orchestrator._handle_circuit_breaker_halt(cycle_count=51)

    assert calls[:4] == ["record", "refresh", "recover", "sync"]
    health_call = calls[-1]
    assert isinstance(health_call, tuple)
    assert health_call[1]["status"] == "ok"
    assert health_call[1]["cycle_count"] == 51
    assert health_call[1]["components"]["trading"] == {
        "status": "halted",
        "reason": "circuit_breaker",
    }


def test_circuit_breaker_halt_still_recovers_after_refresh_failure(monkeypatch):
    calls: list[str | tuple[str, dict]] = []
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.state = SimpleNamespace(circuit_breaker_triggered=False)
    orchestrator.redis = None

    def refresh_holdings() -> None:
        calls.append("refresh")
        raise RuntimeError("temporary dns failure")

    def recover() -> None:
        calls.append("recover")

    def sync_to_redis() -> None:
        calls.append("sync")

    def fake_component_health(**_: object) -> dict:
        return {}

    def fake_update_health(**kwargs: object) -> None:
        calls.append(("health", kwargs))

    orchestrator._record_circuit_breaker_halt_event = lambda: calls.append("record")
    orchestrator.holdings_manager = SimpleNamespace(maybe_refresh_holdings=refresh_holdings)
    orchestrator._try_circuit_breaker_recovery = recover
    orchestrator.state_manager = SimpleNamespace(sync_to_redis=sync_to_redis)

    monkeypatch.setattr("src.core.orchestrator.check_component_health", fake_component_health)
    monkeypatch.setattr("src.core.orchestrator.update_health", fake_update_health)

    orchestrator._handle_circuit_breaker_halt(cycle_count=52)

    assert calls[:4] == ["record", "refresh", "recover", "sync"]
    health_call = calls[-1]
    assert isinstance(health_call, tuple)
    assert health_call[1]["components"]["trading"] == {
        "status": "recovered",
        "reason": "circuit_breaker",
    }