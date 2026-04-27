"""Tests for src/news/onchain.py — non-crypto skip + integration-light."""
from src.news.onchain import fetch_and_persist_onchain


class _FakeDB:
    def __init__(self):
        self.rows = []
    def upsert_onchain(self, exchange, rows):
        self.rows.extend(rows)
        return len(rows)


def test_non_crypto_skipped():
    res = fetch_and_persist_onchain(_FakeDB(), exchange="ibkr")
    assert res["skipped"] is True
    assert res["persisted"] == 0


def test_crypto_returns_dict_shape():
    # Network calls may fail in CI; we only assert the function returns
    # the documented shape and never raises.
    res = fetch_and_persist_onchain(_FakeDB(), exchange="coinbase")
    assert isinstance(res, dict)
    assert "persisted" in res
    assert "sources" in res or "skipped" in res
