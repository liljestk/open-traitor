"""Tests for the IBKR equity affordability filter in UniverseScanner.

Whole-share IBKR equities priced above buying power can never be filled,
so the LLM screener must not auto-follow them. Humans can still follow
them manually via the dashboard watchlist.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.managers.universe_scanner import (
    _affordability_margin,
    _effective_buying_power,
    filter_unaffordable,
)


def _row(price: float) -> dict:
    return {"current_price": price, "composite_score": 0.5}


# --- filter_unaffordable -----------------------------------------------------

def test_drops_overpriced_equities():
    ranked = [
        ("AAPL-USD", _row(150.0)),
        ("BRK.A-USD", _row(600_000.0)),
        ("GOOG-USD", _row(200.0)),
    ]
    kept, dropped = filter_unaffordable(ranked, buying_power=1_000.0, margin=1.0)
    pairs_kept = [p for p, _ in kept]
    assert "AAPL-USD" in pairs_kept
    assert "GOOG-USD" in pairs_kept
    assert "BRK.A-USD" not in pairs_kept
    assert dropped == [("BRK.A-USD", 600_000.0)]


def test_held_positions_always_kept():
    ranked = [
        ("AAPL-USD", _row(150.0)),
        ("BRK.A-USD", _row(600_000.0)),
    ]
    kept, dropped = filter_unaffordable(
        ranked, buying_power=1_000.0, margin=1.0, held={"BRK.A-USD"}
    )
    assert {p for p, _ in kept} == {"AAPL-USD", "BRK.A-USD"}
    assert dropped == []


def test_zero_buying_power_returns_unchanged():
    ranked = [("AAPL-USD", _row(150.0)), ("BRK.A-USD", _row(600_000.0))]
    kept, dropped = filter_unaffordable(ranked, buying_power=0.0, margin=1.0)
    assert [p for p, _ in kept] == ["AAPL-USD", "BRK.A-USD"]
    assert dropped == []


def test_margin_widens_threshold():
    ranked = [("TSLA-USD", _row(900.0)), ("AAPL-USD", _row(150.0))]
    _, dropped1 = filter_unaffordable(ranked, buying_power=600.0, margin=1.0)
    assert ("TSLA-USD", 900.0) in dropped1
    kept2, dropped2 = filter_unaffordable(ranked, buying_power=600.0, margin=2.0)
    assert {p for p, _ in kept2} == {"TSLA-USD", "AAPL-USD"}
    assert dropped2 == []


def test_missing_or_zero_price_kept():
    ranked = [("X-USD", {"current_price": None}), ("Y-USD", {"current_price": 0})]
    kept, dropped = filter_unaffordable(ranked, buying_power=10.0, margin=1.0)
    assert len(kept) == 2
    assert dropped == []


# --- _effective_buying_power -------------------------------------------------

def test_effective_buying_power_prefers_live_balances():
    state = SimpleNamespace(live_cash_balances={"USD": 100.0, "EUR": 50.0}, cash_balance=999.0)
    assert _effective_buying_power(state) == pytest.approx(150.0)


def test_effective_buying_power_falls_back_to_cash_balance():
    state = SimpleNamespace(live_cash_balances={}, cash_balance=42.0)
    assert _effective_buying_power(state) == pytest.approx(42.0)


def test_effective_buying_power_missing_attrs():
    assert _effective_buying_power(SimpleNamespace()) == 0.0


# --- _affordability_margin ---------------------------------------------------

def test_affordability_margin_default(monkeypatch):
    monkeypatch.delenv("AUTO_TRAITOR_AFFORDABILITY_MARGIN", raising=False)
    assert _affordability_margin() == 1.0


def test_affordability_margin_env(monkeypatch):
    monkeypatch.setenv("AUTO_TRAITOR_AFFORDABILITY_MARGIN", "2.5")
    assert _affordability_margin() == 2.5


def test_affordability_margin_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("AUTO_TRAITOR_AFFORDABILITY_MARGIN", "bogus")
    assert _affordability_margin() == 1.0
    monkeypatch.setenv("AUTO_TRAITOR_AFFORDABILITY_MARGIN", "-1")
    assert _affordability_margin() == 1.0
