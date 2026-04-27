"""Tests for src/utils/attribution.py — module surface."""
import inspect
from src.utils import attribution as att


def test_module_exports():
    assert callable(att.compute_attribution)
    assert inspect.iscoroutinefunction(att.replay_strategist)


def test_compute_attribution_handles_empty_db():
    class _DB:
        def _get_conn(self):
            raise RuntimeError("nope")
    res = att.compute_attribution(_DB(), exchange="coinbase", lookback_days=30)
    assert isinstance(res, dict)
    assert res.get("rows") == 0
