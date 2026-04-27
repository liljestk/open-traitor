"""Tests for src/news/event_calendar.py — bundled macro + override loader."""
import json
import os
from pathlib import Path

from src.news.event_calendar import _macro_events_for, _load_overrides


def test_macro_events_have_required_fields():
    events = _macro_events_for("coinbase")
    assert len(events) > 0
    for ev in events:
        assert ev["exchange"] == "coinbase"
        assert ev["symbol"] == "*"
        assert ev["event_type"] in {"FOMC", "CPI", "NFP"}
        assert ev["importance"] >= 1
        assert ev["source"] == "bundled_macro"
        assert "event_ts" in ev


def test_macro_events_per_exchange_string():
    events = _macro_events_for("ibkr")
    assert all(ev["exchange"] == "ibkr" for ev in events)


def test_load_overrides_no_file():
    p = Path("config") / "event_overrides.json"
    backup = None
    if p.exists():
        backup = p.read_text()
        p.unlink()
    try:
        assert _load_overrides("coinbase") == []
    finally:
        if backup is not None:
            p.write_text(backup)


def test_load_overrides_filters_exchange(tmp_path, monkeypatch):
    # Use a tmp working dir so we don't clobber real config
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "event_overrides.json").write_text(json.dumps([
        {"exchange": "coinbase", "event_type": "test", "event_ts": "2099-01-01T00:00:00Z", "importance": 1},
        {"exchange": "ibkr",     "event_type": "earn",  "event_ts": "2099-01-01T00:00:00Z", "importance": 1},
    ]))
    cb = _load_overrides("coinbase")
    ib = _load_overrides("ibkr")
    assert len(cb) == 1 and cb[0]["event_type"] == "test"
    assert len(ib) == 1 and ib[0]["event_type"] == "earn"
