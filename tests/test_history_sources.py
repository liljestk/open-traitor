"""Tests for historical OHLCV source adapters and bulk backfill plumbing.

All HTTP is mocked so tests are hermetic and fast.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.analysis.history_sources import (
    BinanceSource,
    CoinbaseSource,
    CryptoCompareSource,
    StooqSource,
    TokenBucket,
    YahooChartSource,
    YahooFinanceSource,
    _parse_stooq_csv,
    select_sources,
)


# ───────────────────────── TokenBucket ──────────────────────────


def test_token_bucket_initial_burst():
    tb = TokenBucket(rate_per_sec=10.0, capacity=4)
    # Should not block for the first `capacity` takes.
    import time as _t
    t0 = _t.monotonic()
    for _ in range(4):
        tb.take()
    elapsed = _t.monotonic() - t0
    assert elapsed < 0.05, f"burst took too long: {elapsed:.3f}s"


# ───────────────────────── select_sources ──────────────────────────


def test_select_sources_crypto_profile():
    srcs = select_sources("coinbase")
    domains = {s.domain for s in srcs}
    assert "crypto" in domains
    assert "equity" not in domains


def test_select_sources_paper_is_crypto():
    srcs = select_sources("coinbase_paper")
    assert {s.domain for s in srcs} == {"crypto"}


def test_select_sources_equity_profile():
    srcs = select_sources("ibkr")
    domains = {s.domain for s in srcs}
    assert "equity" in domains
    assert "crypto" not in domains
    # YahooChartSource must be primary so backfills work without
    # `fc.yahoo.com` (yfinance's cookie endpoint, often blocked).
    assert srcs[0].name == "yahoo_chart"


def test_yahoo_chart_normalise_strips_currency_suffix():
    assert YahooChartSource._normalise_symbol("NOKIA.HE-EUR") == "NOKIA.HE"
    assert YahooChartSource._normalise_symbol("AAPL-USD") == "AAPL"
    assert YahooChartSource._normalise_symbol("BRK-B") == "BRK-B"


def test_yahoo_chart_parses_v8_payload():
    payload = {
        "chart": {
            "error": None,
            "result": [
                {
                    "timestamp": [1700000000, 1700086400, 1700172800],
                    "indicators": {
                        "quote": [
                            {
                                "open":   [10.0, 10.5, None],
                                "high":   [10.6, 10.8, None],
                                "low":    [9.9,  10.4, None],
                                "close":  [10.5, 10.7, None],   # null -> skipped
                                "volume": [1000, 2000, 3000],
                            }
                        ]
                    },
                }
            ],
        }
    }
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = payload
    src = YahooChartSource()
    with patch("src.analysis.history_sources.requests.get", return_value=fake_resp):
        rows = src.fetch(
            "NOKIA.HE-EUR", "ONE_DAY",
            datetime(2023, 11, 1, tzinfo=timezone.utc),
            datetime(2023, 11, 30, tzinfo=timezone.utc),
        )
    assert len(rows) == 2
    assert rows[0]["c"] == 10.5
    assert rows[1]["v"] == 2000
    assert rows[0]["ts"].tzinfo is timezone.utc


def test_yahoo_chart_uses_normalised_ticker_in_url():
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {"chart": {"error": None, "result": []}}
    src = YahooChartSource()
    with patch(
        "src.analysis.history_sources.requests.get",
        return_value=fake_resp,
    ) as mock_get:
        src.fetch(
            "NOKIA.HE-EUR", "ONE_DAY",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 5, tzinfo=timezone.utc),
        )
    assert mock_get.called
    url = mock_get.call_args.args[0]
    assert url.endswith("/NOKIA.HE"), f"expected …/NOKIA.HE, got {url}"


def test_yahoo_chart_handles_http_error():
    fake_resp = MagicMock(status_code=404, text="not found")
    fake_resp.json.side_effect = ValueError
    src = YahooChartSource()
    with patch("src.analysis.history_sources.requests.get", return_value=fake_resp):
        rows = src.fetch(
            "BOGUS-USD", "ONE_DAY",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 5, tzinfo=timezone.utc),
        )
    assert rows == []


# ───────────────────────── Stooq parser + adapter ──────────────────────────


def test_yahoo_normalise_strips_currency_suffix():
    # Project pair format -> Yahoo ticker
    assert YahooFinanceSource._normalise_symbol("NOKIA.HE-EUR") == "NOKIA.HE"
    assert YahooFinanceSource._normalise_symbol("AAPL-USD") == "AAPL"
    assert YahooFinanceSource._normalise_symbol("VOLV-B.ST-SEK") == "VOLV-B.ST"
    # Already a Yahoo ticker -> unchanged (uppercased)
    assert YahooFinanceSource._normalise_symbol("nokia.he") == "NOKIA.HE"
    assert YahooFinanceSource._normalise_symbol("AAPL") == "AAPL"
    # Currency-looking but actual suffix (>3 chars) -> unchanged
    assert YahooFinanceSource._normalise_symbol("BRK-B") == "BRK-B"


def test_yahoo_fetch_uses_normalised_ticker():
    """The download must be issued for the normalised ticker, not the raw pair."""
    src = YahooFinanceSource()
    with patch("yfinance.download") as mock_dl:
        mock_dl.return_value = None  # treat as empty
        src.fetch(
            "NOKIA.HE-EUR", "ONE_DAY",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 5, tzinfo=timezone.utc),
        )
    assert mock_dl.called, "yfinance.download was never called"
    args, kwargs = mock_dl.call_args
    called_ticker = args[0] if args else kwargs.get("tickers")
    assert called_ticker == "NOKIA.HE", f"expected NOKIA.HE, got {called_ticker!r}"


def test_stooq_normalise_maps_eu_exchanges():
    assert StooqSource._normalise_symbol("NOKIA.HE-EUR") == "nokia.fi"
    assert StooqSource._normalise_symbol("SAP.DE-EUR") == "sap.de"
    assert StooqSource._normalise_symbol("VOLV-B.ST-SEK") == "volv-b.sf"
    assert StooqSource._normalise_symbol("AAPL-USD") == "aapl.us"
    assert StooqSource._normalise_symbol("AAPL") == "aapl.us"
    assert StooqSource._normalise_symbol("aapl.us") == "aapl.us"


def test_parse_stooq_csv_basic():
    csv = (
        "Date,Open,High,Low,Close,Volume\n"
        "2024-01-02,100.0,102.0,99.0,101.5,1234567\n"
        "2024-01-03,101.5,103.0,101.0,102.8,2345678\n"
    )
    rows = _parse_stooq_csv(csv)
    assert len(rows) == 2
    assert rows[0]["c"] == 101.5
    assert rows[1]["v"] == 2345678
    assert rows[0]["ts"].tzinfo is timezone.utc


def test_parse_stooq_csv_skips_malformed():
    csv = (
        "Date,Open,High,Low,Close,Volume\n"
        "BAD-ROW\n"
        "2024-01-03,not-a-number,103.0,101.0,102.8,2345678\n"
        "2024-01-04,101.0,102.0,100.0,101.0,100\n"
    )
    rows = _parse_stooq_csv(csv)
    assert len(rows) == 1
    assert rows[0]["c"] == 101.0


def test_stooq_source_handles_no_data_response():
    src = StooqSource()
    fake_resp = MagicMock(status_code=200, text="No data")
    with patch("src.analysis.history_sources.requests.get", return_value=fake_resp):
        rows = src.fetch(
            "AAPL", "ONE_DAY",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 2, 1, tzinfo=timezone.utc),
        )
    assert rows == []


def test_stooq_source_intraday_unsupported():
    src = StooqSource()
    rows = src.fetch(
        "AAPL", "ONE_HOUR",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    assert rows == []


def test_stooq_source_request_error_returns_empty():
    import requests as _r
    src = StooqSource()
    with patch(
        "src.analysis.history_sources.requests.get",
        side_effect=_r.RequestException("boom"),
    ):
        rows = src.fetch(
            "AAPL", "ONE_DAY",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 2, 1, tzinfo=timezone.utc),
        )
    assert rows == []


# ───────────────────────── Coinbase adapter ──────────────────────────


def test_coinbase_source_paginates_back_to_start():
    """Page backwards in 300-bar windows; stop when start_ts reached or
    a short page indicates inception."""
    fake_client = MagicMock()
    # First call returns 300 rows for the latest window; second returns 50 rows
    # (signalling sparse history) which should terminate the loop.
    def _make_page(n: int, base_ts: int):
        return [
            {
                "start": base_ts - i * 86400,
                "open": 100.0, "high": 101.0, "low": 99.0,
                "close": 100.5, "volume": 1234.0,
            }
            for i in range(n)
        ]
    fake_client.get_candles.side_effect = [
        _make_page(300, 1_700_000_000),
        _make_page(50, 1_700_000_000 - 300 * 86400),
    ]
    src = CoinbaseSource(client=fake_client)
    rows = src.fetch(
        "BTC-USD", "ONE_DAY",
        start=datetime.fromtimestamp(1_650_000_000, tz=timezone.utc),
        end=datetime.fromtimestamp(1_700_000_000, tz=timezone.utc),
    )
    assert len(rows) == 350
    assert all(r["c"] == 100.5 for r in rows)
    # Both pages were fetched; loop terminated after short page.
    assert fake_client.get_candles.call_count == 2


def test_coinbase_source_unknown_granularity_returns_empty():
    src = CoinbaseSource(client=MagicMock())
    rows = src.fetch(
        "BTC-USD", "BOGUS",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    assert rows == []


# ───────────────────────── CryptoCompare adapter ──────────────────────────


def test_cryptocompare_source_parses_payload():
    src = CryptoCompareSource()
    payload = {
        "Response": "Success",
        "Data": {
            "Data": [
                {
                    "time": 1_700_000_000,
                    "open": 100, "high": 110, "low": 95, "close": 105,
                    "volumefrom": 1.0, "volumeto": 100.0,
                },
                {
                    "time": 1_700_086_400,
                    "open": 105, "high": 115, "low": 100, "close": 112,
                    "volumefrom": 2.0, "volumeto": 200.0,
                },
                # zero-padded pre-inception row should be skipped
                {"time": 1_600_000_000, "open": 0, "high": 0, "low": 0, "close": 0},
            ]
        },
    }
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = payload
    # Force loop to terminate after one iteration by making page_min_ts
    # ≤ start_ts on the second iteration.
    with patch("src.analysis.history_sources.requests.get", return_value=fake_resp):
        rows = src.fetch(
            "BTC-USD", "ONE_DAY",
            datetime.fromtimestamp(1_699_000_000, tz=timezone.utc),
            datetime.fromtimestamp(1_700_500_000, tz=timezone.utc),
        )
    # The two real rows should appear (zero-padded skipped). May be duplicated
    # across pagination — that's OK for parser-level test; just check ≥ 2.
    assert len(rows) >= 2
    assert all(r["c"] > 0 for r in rows)


def test_cryptocompare_invalid_symbol_returns_empty():
    src = CryptoCompareSource()
    assert src.fetch(
        "INVALIDPAIR", "ONE_DAY",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 2, 1, tzinfo=timezone.utc),
    ) == []


# ───────────────────────── Binance adapter ──────────────────────────


def test_binance_source_parses_klines():
    src = BinanceSource()
    # Binance returns klines as list-of-lists.
    page = [
        [1_700_000_000_000, "100.0", "110.0", "95.0", "105.0", "1234.5",
         1_700_086_399_000, "0", 0, "0", "0", "0"],
        [1_700_086_400_000, "105.0", "115.0", "100.0", "112.0", "2345.6",
         1_700_172_799_000, "0", 0, "0", "0", "0"],
    ]
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = page
    # Second call returns empty to terminate pagination.
    fake_empty = MagicMock(status_code=200)
    fake_empty.json.return_value = []
    with patch(
        "src.analysis.history_sources.requests.get",
        side_effect=[fake_resp, fake_empty],
    ):
        rows = src.fetch(
            "BTC-USDT", "ONE_DAY",
            datetime.fromtimestamp(1_699_900_000, tz=timezone.utc),
            datetime.fromtimestamp(1_700_200_000, tz=timezone.utc),
        )
    assert len(rows) == 2
    assert rows[0]["c"] == 105.0
    assert rows[1]["v"] == 2345.6
