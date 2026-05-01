"""Unit tests for regression coverage of every followed asset.

Exercises ``analysis.regression_coverage`` end-to-end with a tiny
in-memory StatsDB stub so we don't need Postgres. The contract:

* When a symbol is followed but lacks catalysts, the factor regression
  must still emit at least one row in ``market_factor_loadings``.
* When factor candles are present under the ``_factors_`` exchange,
  ``compute_factor_loadings`` must converge for every symbol with
  enough candles, regardless of profile (coinbase or ibkr).
* The coverage probe must report 100% when every followed symbol has
  at least one row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.analysis.factor_universe import FACTOR_EXCHANGE
from src.analysis.market_factors import (
    DEFAULT_FACTORS,
    MIN_OBSERVATIONS,
    compute_factor_loadings,
    persist_factor_loadings,
)
from src.analysis.regression_coverage import (
    compute_coverage_stats,
    refresh_regression_for_symbols,
)


# ── In-memory StatsDB stub ──────────────────────────────────────────────

class _StubDB:
    """Tiny stand-in for StatsDB exposing only the methods we need."""

    def __init__(self, followed: dict[str, list[str]] | None = None):
        self._candles: dict[tuple[str, str, str], list[dict]] = {}
        self._followed = followed or {}
        self._factor_rows: list[dict] = []
        self._event_rows: list[dict] = []

    # ── candles
    def upsert_candles(self, *, exchange, symbol, granularity, candles, source="stub"):
        key = (exchange, symbol, granularity)
        existing = {c["ts"] for c in self._candles.get(key, [])}
        new = [c for c in candles if c["ts"] not in existing]
        self._candles.setdefault(key, []).extend(new)
        self._candles[key].sort(key=lambda c: c["ts"])
        return len(new)

    def get_candles_range(
        self, exchange, symbol, granularity,
        start=None, end=None, limit=None,
    ):
        key = (exchange, symbol, granularity)
        rows = list(self._candles.get(key, []))
        if start is not None:
            rows = [r for r in rows if r["ts"] >= start]
        if end is not None:
            rows = [r for r in rows if r["ts"] <= end]
        if limit is not None:
            rows = rows[-int(limit):]
        return rows

    # ── follows
    def get_followed_pairs_set(
        self, followed_by=None, quote_currency=None, exchange=None,
    ):
        if exchange:
            return set(self._followed.get(exchange, []))
        out: set[str] = set()
        for v in self._followed.values():
            out.update(v)
        return out

    # ── factor loadings
    def upsert_market_factor_loadings(self, rows):
        for r in rows:
            # delete prior matching row, then append
            self._factor_rows = [
                x for x in self._factor_rows
                if not (x["exchange"] == r["exchange"]
                        and x["symbol"] == r["symbol"]
                        and x["factor"] == r["factor"])
            ]
            r2 = dict(r)
            r2.setdefault("computed_at", datetime.now(timezone.utc))
            self._factor_rows.append(r2)
        return len(rows)

    def get_market_factor_loadings(
        self, exchange, *, symbol=None, factor=None,
        min_abs_t_stat=0.0, limit=500,
    ):
        out = [r for r in self._factor_rows if r["exchange"] == exchange]
        if symbol:
            out = [r for r in out if r["symbol"] == symbol]
        if factor:
            out = [r for r in out if r["factor"] == factor]
        return out[: int(limit)]

    # ── event regressions (catalyst-driven; empty in these tests)
    def get_catalyst_events(self, **_kw):
        return []

    def upsert_event_regression(self, row):
        self._event_rows.append(dict(row))
        return 1

    def get_event_regressions(self, exchange, **_kw):
        return [r for r in self._event_rows if r.get("exchange") == exchange]


# ── Synthetic data ─────────────────────────────────────────────────────

def _seed_daily_candles(db, exchange, symbol, *, n=300, seed=0, beta=1.0,
                        factor_candles=None, end_date=None):
    """Seed daily candles ending at ``end_date`` (default today UTC).

    If ``factor_candles`` is provided, returns are constructed as
    ``beta * factor_returns + noise`` so the regression converges with
    a non-trivial slope.
    """
    rng = np.random.default_rng(seed)
    end = end_date or datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    base_ts = end - timedelta(days=n - 1)
    if factor_candles is None:
        rets = rng.normal(0, 0.012, size=n)
    else:
        f_closes = [c["c"] for c in factor_candles[-n:]]
        f_rets = np.diff(np.log(f_closes))
        eps = rng.normal(0, 0.008, size=len(f_rets))
        rets = beta * f_rets + eps
        rets = np.concatenate([[0.0], rets])
    closes = 100.0 * np.exp(np.cumsum(rets))
    candles = [
        {
            "ts": base_ts + timedelta(days=i),
            "o": float(closes[i]),
            "h": float(closes[i] * 1.005),
            "l": float(closes[i] * 0.995),
            "c": float(closes[i]),
            "v": 1000.0,
        }
        for i in range(n)
    ]
    db.upsert_candles(
        exchange=exchange, symbol=symbol, granularity="ONE_DAY",
        candles=candles, source="test",
    )
    return candles


# ── Tests ───────────────────────────────────────────────────────────────

def test_factor_regression_persists_rows_for_synthetic_universe():
    """compute → persist → query round-trip writes one row per (sym, factor)."""
    db = _StubDB()
    # Seed the factor candles under the shared _factors_ exchange.
    factor_series = {}
    for f in DEFAULT_FACTORS:
        factor_series[f] = _seed_daily_candles(
            db, FACTOR_EXCHANGE, f, n=300, seed=hash(f) & 0xFFFF,
        )
    # Seed three followed assets (ibkr) with returns loaded on SPX.
    spx = factor_series["^GSPC"]
    syms = ["AAA-EUR", "BBB-EUR", "CCC-EUR"]
    for i, s in enumerate(syms):
        _seed_daily_candles(
            db, "ibkr", s, n=300, seed=10 + i, beta=0.6 + 0.1 * i,
            factor_candles=spx,
        )

    results = compute_factor_loadings(
        exchange="ibkr",
        symbols=syms,
        stats_db=db,
        factors=DEFAULT_FACTORS,
        factor_exchange=FACTOR_EXCHANGE,
    )
    assert len(results) == len(syms), (
        f"expected one regression per symbol, got {len(results)}"
    )
    written = persist_factor_loadings(db, exchange="ibkr", results=results)
    assert written == len(syms) * len(DEFAULT_FACTORS)
    # Every followed symbol now has at least one factor row.
    for s in syms:
        rows = db.get_market_factor_loadings(exchange="ibkr", symbol=s)
        assert rows, f"no rows persisted for {s}"
        # SPX beta should be in a reasonable neighbourhood of the seeded slope.
        spx_rows = [r for r in rows if r["factor"] == "^GSPC"]
        assert spx_rows
        assert spx_rows[0]["sample_count"] >= MIN_OBSERVATIONS


def test_refresh_regression_for_symbols_covers_followed_assets(monkeypatch):
    """refresh_regression_for_symbols guarantees factor coverage for each."""
    db = _StubDB(followed={"ibkr": ["BIOBV.HE-EUR", "NOKIA.HE-EUR"]})
    factor_series = {}
    for f in DEFAULT_FACTORS:
        factor_series[f] = _seed_daily_candles(
            db, FACTOR_EXCHANGE, f, n=320, seed=hash(f) & 0xFFFF,
        )
    spx = factor_series["^GSPC"]
    for i, s in enumerate(["BIOBV.HE-EUR", "NOKIA.HE-EUR"]):
        _seed_daily_candles(
            db, "ibkr", s, n=320, seed=99 + i, beta=0.5,
            factor_candles=spx,
        )

    # Skip network: ensure_factor_candles must not call yfinance in tests.
    monkeypatch.setattr(
        "src.analysis.regression_coverage.ensure_factor_candles",
        lambda *a, **kw: {f: 0 for f in DEFAULT_FACTORS},
    )

    summary = refresh_regression_for_symbols(
        stats_db=db,
        exchange="ibkr",
        symbols=["BIOBV.HE-EUR", "NOKIA.HE-EUR"],
    )
    assert summary["factor_models"] == 2
    assert summary["factor_rows"] == 2 * len(DEFAULT_FACTORS)
    assert summary["errors"] == []

    # Coverage probe should now report 100%.
    cov = compute_coverage_stats(db, "ibkr")
    assert cov["followed"] == 2
    assert cov["modeled"] == 2
    assert cov["missing"] == []
    assert cov["coverage_pct"] == pytest.approx(100.0)
    assert cov["factor_symbols"] == 2


def test_coverage_excludes_macro_placeholder_from_event_models():
    """The legacy ``_MACRO_`` placeholder row must not count as coverage."""
    db = _StubDB(followed={"ibkr": ["X-EUR"]})
    db._event_rows.append({
        "exchange": "ibkr", "symbol": "_MACRO_", "event_type": "macro",
        "horizon_days": 1,
    })
    cov = compute_coverage_stats(db, "ibkr")
    assert cov["followed"] == 1
    assert cov["modeled"] == 0
    assert cov["missing"] == ["X-EUR"]
    assert cov["event_symbols"] == 0


def test_coverage_handles_zero_followed():
    db = _StubDB(followed={})
    cov = compute_coverage_stats(db, "coinbase")
    assert cov["followed"] == 0
    assert cov["coverage_pct"] == 100.0
    assert cov["missing"] == []


def test_refresh_for_followed_returns_no_followed_when_empty(monkeypatch):
    from src.analysis.regression_coverage import refresh_regression_for_followed

    db = _StubDB(followed={})
    monkeypatch.setattr(
        "src.analysis.regression_coverage.ensure_factor_candles",
        lambda *a, **kw: {f: 0 for f in DEFAULT_FACTORS},
    )
    res = refresh_regression_for_followed(stats_db=db, exchange="ibkr")
    assert res["note"] == "no_followed_symbols"
    assert res["symbols"] == []
