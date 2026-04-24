"""Tests for the catalyst event collector — equity, halvings, listings,
regulatory tagging."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.news.catalyst_collector import (
    _BTC_HALVINGS_UTC,
    _HALVING_SCHEDULES,
    _profile_exchange,
    _tag_regulatory_articles,
    collect_crypto_catalysts,
)


# ───────────────────────── deterministic halvings ──────────────────────────


def test_btc_halving_schedule_is_chronologically_ordered():
    ts_list = list(_BTC_HALVINGS_UTC)
    assert ts_list == sorted(ts_list)
    # Every halving should be roughly 4 years apart (±6 months).
    from datetime import timedelta as _td
    for prev, nxt in zip(ts_list, ts_list[1:]):
        delta = nxt - prev
        assert _td(days=3 * 365) <= delta <= _td(days=5 * 365)


def test_halving_schedules_cover_btc_ltc_bch():
    assert set(_HALVING_SCHEDULES.keys()) >= {"BTC", "LTC", "BCH"}
    for ts_list in _HALVING_SCHEDULES.values():
        assert all(t.tzinfo is timezone.utc for t in ts_list)


def test_profile_exchange_mapping():
    assert _profile_exchange("coinbase") == "coinbase"
    assert _profile_exchange("coinbase_paper") == "coinbase_paper"
    assert _profile_exchange("ibkr") == "ibkr"
    # Unknown profile falls back to lowercase identity.
    assert _profile_exchange("WeIrD") == "weird"


# ───────────────────────── halving event emission ──────────────────────────


def test_collect_crypto_catalysts_emits_halvings_within_horizon():
    """For BTC-USD pair, we should get the upcoming + most recent halving
    within a 5-year horizon."""
    fake_db = MagicMock()
    fake_db.upsert_catalyst_events.return_value = 1
    # Patch out HTTP/news so only halvings are exercised.
    with patch(
        "src.news.catalyst_collector._fetch_coinbase_listings", return_value=[]
    ), patch(
        "src.news.catalyst_collector._tag_regulatory_articles", return_value=[]
    ):
        n = collect_crypto_catalysts(
            profile="coinbase",
            pairs=["BTC-USD"],
            horizon_days=5 * 365,
            stats_db=fake_db,
        )
    assert n == 1
    # Inspect what was passed to the DB.
    written = fake_db.upsert_catalyst_events.call_args[0][0]
    # Should include at least one BTC halving event.
    halvings = [r for r in written if r["event_type"] == "halving"]
    assert len(halvings) >= 1
    for r in halvings:
        assert r["exchange"] == "coinbase"
        assert r["symbol"] == "BTC-USD"
        assert r["confidence"] == 1.0


def test_collect_crypto_catalysts_skips_non_crypto_profile():
    fake_db = MagicMock()
    n = collect_crypto_catalysts(
        profile="ibkr",
        pairs=["AAPL"],
        stats_db=fake_db,
    )
    assert n == 0
    fake_db.upsert_catalyst_events.assert_not_called()


def test_collect_crypto_catalysts_no_halving_for_non_halving_asset():
    fake_db = MagicMock()
    fake_db.upsert_catalyst_events.return_value = 0
    with patch(
        "src.news.catalyst_collector._fetch_coinbase_listings", return_value=[]
    ), patch(
        "src.news.catalyst_collector._tag_regulatory_articles", return_value=[]
    ):
        collect_crypto_catalysts(
            profile="coinbase",
            pairs=["XRP-USD"],
            stats_db=fake_db,
        )
    # Either no rows written, or the call wasn't made at all (both acceptable).
    if fake_db.upsert_catalyst_events.called:
        written = fake_db.upsert_catalyst_events.call_args[0][0]
        halvings = [r for r in written if r["event_type"] == "halving"]
        assert halvings == []


# ───────────────────────── regulatory tagging ──────────────────────────


def _mk_article(title: str, summary: str, when: datetime):
    return SimpleNamespace(title=title, summary=summary, published=when)


def test_tag_regulatory_articles_matches_etf_decision():
    ts = datetime(2024, 1, 10, tzinfo=timezone.utc)
    art = _mk_article(
        "SEC approves spot Bitcoin ETF after years of delay",
        "Major decision impacts BTC and ETH listings.",
        ts,
    )
    rows = _tag_regulatory_articles(
        "coinbase", ["BTC-USD", "ETH-USD"], [art]
    )
    assert len(rows) >= 1
    assert all(r["event_type"].startswith("regulatory.") for r in rows)
    assert all(r["exchange"] == "coinbase" for r in rows)


def test_tag_regulatory_articles_skips_unrelated_news():
    ts = datetime(2024, 1, 10, tzinfo=timezone.utc)
    art = _mk_article(
        "Apple unveils new iPhone with improved camera",
        "Smartphone gets minor refresh.",
        ts,
    )
    rows = _tag_regulatory_articles("coinbase", ["BTC-USD"], [art])
    assert rows == []


def test_tag_regulatory_articles_handles_missing_published():
    art = SimpleNamespace(
        title="SEC approves Bitcoin ETF spot listing",
        summary="x",
        published=None,
    )
    rows = _tag_regulatory_articles("coinbase", ["BTC-USD"], [art])
    assert rows == []  # missing timestamp ⇒ skip


def test_tag_regulatory_articles_handles_no_articles():
    rows = _tag_regulatory_articles("coinbase", ["BTC-USD"], None)
    assert rows == []
