"""Unit tests for the nightly price-backfill activity.

Verifies the contract that closes the "automatic + delta every night" gap:

1. Universe is sourced from yaml ``trading.pairs`` ∪ ``followed_pairs``.
2. Per-symbol calls to ``bulk_backfill_symbol`` use the right
   profile/exchange and request both ONE_DAY + ONE_HOUR granularities.
3. Lookback honors ``PRICE_BACKFILL_LOOKBACK_YEARS`` (clipped to [1, 10]).
4. Activity is fault-tolerant — one symbol failing does not stop the rest,
   and the failing symbol surfaces in the ``errors`` list.
5. Empty universe → no work, ``skipped: True`` payload.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("PRICE_BACKFILL_LOOKBACK_YEARS", raising=False)


# ---------------------------------------------------------------------------
# Lookback clamp
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 5),       # default
        ("3", 3),        # respected within bounds
        ("10", 10),      # max boundary
        ("25", 10),      # over-cap clipped to 10
        ("0", 1),        # under-floor clipped to 1
        ("-5", 1),       # negative clipped to 1
        ("garbage", 5),  # non-int falls back to default
        ("", 5),         # empty falls back to default
    ],
)
def test_lookback_years_clamps(monkeypatch, raw, expected):
    from src.planning.activities import _price_backfill_years

    if raw is None:
        monkeypatch.delenv("PRICE_BACKFILL_LOOKBACK_YEARS", raising=False)
    else:
        monkeypatch.setenv("PRICE_BACKFILL_LOOKBACK_YEARS", raw)
    assert _price_backfill_years() == expected


# ---------------------------------------------------------------------------
# Universe discovery
# ---------------------------------------------------------------------------

class _StubDB:
    def __init__(self, followed=()):
        self._followed = set(followed)
        self.calls: list[tuple] = []

    def get_followed_pairs_set(self, exchange=None, **_):  # noqa: ARG002
        return set(self._followed)


def test_universe_unions_yaml_and_followed(tmp_path, monkeypatch):
    """Yaml pairs ∪ followed pairs, sorted, deduped."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "coinbase.yaml").write_text(
        "trading:\n"
        "  exchange: coinbase\n"
        "  pairs:\n"
        "    - BTC-USD\n"
        "    - ETH-USD\n"
    )
    monkeypatch.chdir(tmp_path)

    stub = _StubDB(followed={"ETH-USD", "SOL-USD"})  # ETH overlaps
    with patch("src.utils.stats.StatsDB", return_value=stub):
        from src.planning.activities import _price_backfill_universe
        out = _price_backfill_universe("coinbase", "coinbase")

    assert out == ["BTC-USD", "ETH-USD", "SOL-USD"]


def test_universe_yaml_missing_falls_back_to_followed(tmp_path, monkeypatch):
    """No yaml = followed-pairs-only; never crashes."""
    monkeypatch.chdir(tmp_path)  # no config/ dir at all
    stub = _StubDB(followed={"AAPL", "MSFT"})
    with patch("src.utils.stats.StatsDB", return_value=stub):
        from src.planning.activities import _price_backfill_universe
        out = _price_backfill_universe("ibkr", "ibkr")
    assert out == ["AAPL", "MSFT"]


def test_universe_path_traversal_neutralised(tmp_path, monkeypatch):
    """Profile names with '/' or '..' must not escape the config dir."""
    monkeypatch.chdir(tmp_path)
    stub = _StubDB(followed=set())
    with patch("src.utils.stats.StatsDB", return_value=stub):
        from src.planning.activities import _price_backfill_universe
        # Should not raise; should just return [] (no config, no followed).
        out = _price_backfill_universe("../../etc/passwd", "coinbase")
    assert out == []


# ---------------------------------------------------------------------------
# Activity behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_activity_skips_empty_universe(monkeypatch):
    """No symbols → no fetches, skipped payload."""
    with patch(
        "src.planning.activities._price_backfill_universe", return_value=[]
    ), patch(
        "src.planning.activities._detect_domain", return_value="crypto"
    ):
        from src.planning.activities import run_price_backfill
        out = await run_price_backfill("coinbase")

    assert out["skipped"] is True
    assert out["symbols"] == 0
    assert out["rows_written"] == 0
    assert out["exchange"] == "coinbase"


@pytest.mark.asyncio
async def test_activity_calls_bulk_backfill_with_correct_args(monkeypatch):
    """Each symbol must be passed to bulk_backfill_symbol with both granularities."""
    monkeypatch.setenv("PRICE_BACKFILL_LOOKBACK_YEARS", "3")
    captured: list[dict] = []

    def fake_bulk(profile, symbol, granularities, since, until, stats_db):  # noqa: ARG001
        captured.append({
            "profile": profile,
            "symbol": symbol,
            "granularities": tuple(granularities),
            "since": since,
            "until": until,
        })
        return {"ONE_DAY": 100, "ONE_HOUR": 50}

    with patch(
        "src.planning.activities._price_backfill_universe",
        return_value=["BTC-USD", "ETH-USD"],
    ), patch(
        "src.planning.activities._detect_domain", return_value="crypto"
    ), patch(
        "src.utils.stats.StatsDB"
    ), patch(
        "src.analysis.history_bulk_backfill.bulk_backfill_symbol",
        side_effect=fake_bulk,
    ):
        from src.planning.activities import run_price_backfill
        out = await run_price_backfill("coinbase")

    assert len(captured) == 2
    assert {c["symbol"] for c in captured} == {"BTC-USD", "ETH-USD"}
    for c in captured:
        assert c["profile"] == "coinbase"
        assert c["granularities"] == ("ONE_DAY", "ONE_HOUR")
        # Lookback ~3y from `until`. Allow a generous slack — the activity
        # uses ``datetime.now(timezone.utc)`` which can drift by a second.
        delta_days = (c["until"] - c["since"]).days
        assert 365 * 3 - 2 <= delta_days <= 365 * 3 + 2
        assert c["until"].tzinfo is timezone.utc

    assert out["symbols"] == 2
    # 2 symbols × (100+50) rows
    assert out["rows_written"] == 300
    assert out["lookback_years"] == 3
    assert out["errors"] == []


@pytest.mark.asyncio
async def test_activity_continues_on_per_symbol_failure(monkeypatch):
    """One bad symbol must not abort the rest of the universe."""
    def fake_bulk(profile, symbol, granularities, since, until, stats_db):  # noqa: ARG001
        if symbol == "BAD":
            raise RuntimeError("network down")
        return {"ONE_DAY": 10, "ONE_HOUR": 5}

    with patch(
        "src.planning.activities._price_backfill_universe",
        return_value=["GOOD1", "BAD", "GOOD2"],
    ), patch(
        "src.planning.activities._detect_domain", return_value="crypto"
    ), patch(
        "src.utils.stats.StatsDB"
    ), patch(
        "src.analysis.history_bulk_backfill.bulk_backfill_symbol",
        side_effect=fake_bulk,
    ):
        from src.planning.activities import run_price_backfill
        out = await run_price_backfill("coinbase")

    # Both GOODs counted; BAD captured in errors.
    assert out["symbols"] == 2
    assert out["rows_written"] == 30  # 2 × 15
    assert any("BAD" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_activity_resolves_ibkr_exchange(monkeypatch):
    """ibkr profile must produce exchange='ibkr', not 'coinbase'."""
    with patch(
        "src.planning.activities._price_backfill_universe", return_value=[]
    ), patch(
        "src.planning.activities._detect_domain", return_value="equity"
    ):
        from src.planning.activities import run_price_backfill
        out = await run_price_backfill("ibkr")
    assert out["exchange"] == "ibkr"
