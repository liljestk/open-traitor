import sys
import types

import pytest
from fastapi import HTTPException

from src.dashboard import deps


def test_require_db_lazy_initialises_stats_db(monkeypatch):
    fake_db = object()
    fake_stats_module = types.ModuleType("src.utils.stats")

    class FakeStatsDB:
        def __new__(cls):
            return fake_db

    fake_stats_module.StatsDB = FakeStatsDB
    monkeypatch.setitem(sys.modules, "src.utils.stats", fake_stats_module)
    monkeypatch.setattr(deps, "stats_db", None)

    assert deps.require_db("coinbase") is fake_db
    assert deps.stats_db is fake_db


def test_require_db_preserves_503_when_lazy_initialise_fails(monkeypatch):
    fake_stats_module = types.ModuleType("src.utils.stats")

    class BrokenStatsDB:
        def __init__(self):
            raise RuntimeError("database unavailable")

    fake_stats_module.StatsDB = BrokenStatsDB
    monkeypatch.setitem(sys.modules, "src.utils.stats", fake_stats_module)
    monkeypatch.setattr(deps, "stats_db", None)

    with pytest.raises(HTTPException) as exc_info:
        deps.require_db("coinbase")

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Stats DB not initialised"
    assert deps.stats_db is None