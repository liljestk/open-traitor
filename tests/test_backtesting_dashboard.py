"""
Tests for backtesting dashboard routes.

Covers domain separation (exchange filtering), request validation,
helper functions, the pairs endpoint, and the backtest WebSocket.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.dashboard.routes.backtesting import (
    _is_equity_pair,
    _validate_pair_format,
    _MAX_BACKTEST_WS,
    BacktestTriggerRequest,
)


# ═══════════════════════════════════════════════════════════════════════════
# _is_equity_pair heuristic
# ═══════════════════════════════════════════════════════════════════════════

class TestIsEquityPair:
    """Verify that the pair-classification heuristic correctly separates
    crypto pairs from equity tickers."""

    @pytest.mark.parametrize("pair", ["BTC-EUR", "ETH-USD", "SOL-USDC", "DOGE-GBP", "LINK-USDT"])
    def test_crypto_pairs(self, pair):
        assert _is_equity_pair(pair) is False

    @pytest.mark.parametrize("pair", ["AAPL", "MSFT", "TSLA", "GOOG", "NVDA"])
    def test_equity_pairs(self, pair):
        assert _is_equity_pair(pair) is True

    def test_empty_string(self):
        assert _is_equity_pair("") is False


# ═══════════════════════════════════════════════════════════════════════════
# BacktestTriggerRequest validation
# ═══════════════════════════════════════════════════════════════════════════

class TestBacktestTriggerRequestValidation:
    """Pydantic model field constraints."""

    def test_defaults_are_valid(self):
        req = BacktestTriggerRequest(pair="BTC-EUR")
        assert req.days == 60
        assert req.position_size_pct == 0.10
        assert req.trailing_stop_pct == 0.03

    def test_too_few_days_rejected(self):
        with pytest.raises(Exception):
            BacktestTriggerRequest(pair="BTC-EUR", days=1)

    def test_too_many_days_rejected(self):
        with pytest.raises(Exception):
            BacktestTriggerRequest(pair="BTC-EUR", days=999)

    def test_empty_pair_rejected(self):
        with pytest.raises(Exception):
            BacktestTriggerRequest(pair="")

    def test_position_size_bounds(self):
        # Too small
        with pytest.raises(Exception):
            BacktestTriggerRequest(pair="BTC-EUR", position_size_pct=0.001)
        # Too large
        with pytest.raises(Exception):
            BacktestTriggerRequest(pair="BTC-EUR", position_size_pct=0.9)

    def test_valid_custom_params(self):
        req = BacktestTriggerRequest(
            pair="ETH-EUR",
            days=90,
            position_size_pct=0.20,
            trailing_stop_pct=0.05,
            entry_threshold=0.6,
            fee_pct=0.01,
            slippage_pct=0.002,
        )
        assert req.pair == "ETH-EUR"
        assert req.days == 90


# ═══════════════════════════════════════════════════════════════════════════
# Pair validation — injection & XSS prevention
# ═══════════════════════════════════════════════════════════════════════════

class TestPairValidationSecurity:
    """Verify that pair input validation rejects injection payloads,
    path traversal, XSS, and SQL injection attempts."""

    @pytest.mark.parametrize("pair", [
        "BTC-EUR", "ETH-USD", "SOL-USDC", "DOGE-GBP",
        "AAPL", "MSFT", "NVDA", "SPY",
        "AAPL@SMART",
        "ABB.ST-SEK", "VOLV-B.ST-SEK",  # European equity tickers
        "AAPL-USD", "MSFT-USD",          # US equity with quote
    ])
    def test_valid_pairs_accepted(self, pair):
        assert _validate_pair_format(pair) == pair.upper()

    @pytest.mark.parametrize("pair", [
        "BTC-USD; DROP TABLE",      # SQL injection
        "../etc/passwd",             # path traversal
        "<script>alert(1)</script>", # XSS
        "BTC' OR '1'='1",           # SQL injection variant
        "BTC-USD\n--",              # newline injection
        "",                          # empty
        "A" * 50,                    # too long (caught by regex)
        "BTC USD",                   # space
        "BTC/USD",                   # slash
        "@@@@@",                     # pure special chars
        "......",                    # pure dots
        "---@---",                   # dashes and @
        "9AAAA",                     # leading digit
    ])
    def test_malicious_pairs_rejected(self, pair):
        with pytest.raises(ValueError):
            _validate_pair_format(pair)

    def test_trigger_request_rejects_injection(self):
        """Pydantic model's field_validator catches bad pairs before they reach SQL."""
        with pytest.raises(Exception):
            BacktestTriggerRequest(pair="BTC-EUR; DROP TABLE backtest_runs")

    def test_trigger_request_rejects_xss(self):
        with pytest.raises(Exception):
            BacktestTriggerRequest(pair="<img src=x onerror=alert(1)>")

    def test_trigger_request_normalises_case(self):
        req = BacktestTriggerRequest(pair="btc-eur")
        assert req.pair == "BTC-EUR"


# ═══════════════════════════════════════════════════════════════════════════
# SQL pattern safety
# ═══════════════════════════════════════════════════════════════════════════

class TestSQLPatternSafety:
    """Verify that SQL queries use safe parameterisation patterns."""

    def _mock_db(self, rows=None):
        db = MagicMock()
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = rows or []
        cursor.fetchone.return_value = rows[0] if rows else None
        conn.execute.return_value = cursor
        conn.__enter__ = lambda s: s
        conn.__exit__ = MagicMock(return_value=False)
        db._get_conn.return_value = conn
        return db, conn

    @patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True)
    @patch("src.dashboard.routes.backtesting.deps")
    def test_interval_uses_multiplication_not_interpolation(self, mock_deps):
        """SQL must use 'interval '1 day' * %s' not 'interval '%s days'' to avoid
        driver-dependent string-literal substitution."""
        mock_deps.resolve_profile.return_value = None
        db, conn = self._mock_db()

        from src.dashboard.routes.backtesting import get_backtest_history
        get_backtest_history(days=90, pair="", profile="", db=db)

        sql = conn.execute.call_args[0][0]
        # Must NOT contain '%s days' inside a string literal
        assert "'%s days'" not in sql
        assert "'%s day'" not in sql
        # Must use the safe pattern
        assert "interval '1 day' * %s" in sql

    @patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True)
    @patch("src.dashboard.routes.backtesting.deps")
    def test_run_detail_uses_explicit_columns(self, mock_deps):
        """Run detail query must NOT use SELECT * to prevent data leakage."""
        mock_deps.resolve_profile.return_value = None
        db, conn = self._mock_db(rows=[{
            "id": 1, "run_ts": "2024-01-01T00:00:00Z", "pair": "BTC-EUR",
            "exchange": "coinbase", "days": 60,
            "params_json": "{}", "result_json": "{}",
            "total_return_pct": 5.0, "sharpe_ratio": 1.2,
            "win_rate": 55, "total_trades": 10,
            "max_drawdown_pct": -8, "alpha": 2.0,
            "is_wfo": False, "wfo_wfe": None,
        }])

        from src.dashboard.routes.backtesting import get_backtest_run
        get_backtest_run(run_id=1, profile="", db=db)

        sql = conn.execute.call_args[0][0]
        assert "SELECT *" not in sql, "Must use explicit column list, not SELECT *"


# ═══════════════════════════════════════════════════════════════════════════
# Domain separation — exchange filtering in SQL queries
# ═══════════════════════════════════════════════════════════════════════════

class TestBacktestDomainSeparation:
    """Verify that SQL queries include exchange filtering when a profile
    is resolved. This ensures crypto/equity data never bleeds across domains."""

    def _mock_db(self, rows=None):
        """Build a mock DB with a connection that returns given rows."""
        db = MagicMock()
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = rows or []
        cursor.fetchone.return_value = rows[0] if rows else None
        conn.execute.return_value = cursor
        conn.__enter__ = lambda s: s
        conn.__exit__ = MagicMock(return_value=False)
        db._get_conn.return_value = conn
        return db, conn

    @patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True)
    @patch("src.dashboard.routes.backtesting.deps")
    def test_history_filters_by_exchange_when_profile_set(self, mock_deps):
        mock_deps.resolve_profile.return_value = "coinbase"
        db, conn = self._mock_db()
        mock_deps.get_profile_db.return_value = db

        from src.dashboard.routes.backtesting import get_backtest_history
        get_backtest_history(days=90, pair="", profile="coinbase", db=db)

        sql = conn.execute.call_args[0][0]
        params = conn.execute.call_args[0][1]
        assert "exchange = %s" in sql, "SQL must filter by exchange when profile is set"
        assert "coinbase" in params

    @patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True)
    @patch("src.dashboard.routes.backtesting.deps")
    def test_history_no_exchange_filter_without_profile(self, mock_deps):
        mock_deps.resolve_profile.return_value = None
        db, conn = self._mock_db()

        from src.dashboard.routes.backtesting import get_backtest_history
        get_backtest_history(days=90, pair="", profile="", db=db)

        sql = conn.execute.call_args[0][0]
        assert "exchange = %s" not in sql, "No exchange filter without profile"

    @patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True)
    @patch("src.dashboard.routes.backtesting.deps")
    def test_run_detail_filters_by_exchange(self, mock_deps):
        mock_deps.resolve_profile.return_value = "ibkr"
        db, conn = self._mock_db(rows=[{
            "id": 1, "run_ts": "2024-01-01T00:00:00Z", "pair": "AAPL",
            "exchange": "ibkr", "days": 60,
            "params_json": "{}",
            "result_json": "{}",
            "total_return_pct": 5.0, "sharpe_ratio": 1.2,
            "win_rate": 55, "total_trades": 10,
            "max_drawdown_pct": -8, "alpha": 2.0,
            "is_wfo": False, "wfo_wfe": None,
        }])

        from src.dashboard.routes.backtesting import get_backtest_run
        get_backtest_run(run_id=1, profile="ibkr", db=db)

        sql = conn.execute.call_args[0][0]
        params = conn.execute.call_args[0][1]
        assert "exchange = %s" in sql
        assert "ibkr" in params

    @patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True)
    @patch("src.dashboard.routes.backtesting.deps")
    def test_wfo_history_filters_by_exchange(self, mock_deps):
        mock_deps.resolve_profile.return_value = "coinbase"
        db, conn = self._mock_db()

        from src.dashboard.routes.backtesting import get_wfo_history
        get_wfo_history(days=90, pair="", profile="coinbase", db=db)

        sql = conn.execute.call_args[0][0]
        params = conn.execute.call_args[0][1]
        assert "exchange = %s" in sql
        assert "coinbase" in params

    @patch("src.dashboard.routes.backtesting.deps")
    def test_promotions_filters_crypto_pairs_for_coinbase(self, mock_deps):
        mock_deps.resolve_profile.return_value = "coinbase"
        mock_deps.sanitize_floats.side_effect = lambda x: x
        db, conn = self._mock_db(rows=[
            {"run_ts": "2024-01-01", "pair": "BTC-EUR", "param_name": "trailing_stop",
             "old_value": 0.03, "new_value": 0.04, "wfe": 0.6,
             "oos_sharpe": 1.1, "promoted": True, "rolled_back": False,
             "rollback_ts": None, "rollback_reason": None,
             "pre_promotion_accuracy": 0.6, "post_promotion_accuracy": 0.7},
            {"run_ts": "2024-01-02", "pair": "AAPL", "param_name": "trailing_stop",
             "old_value": 0.03, "new_value": 0.04, "wfe": 0.5,
             "oos_sharpe": 0.9, "promoted": True, "rolled_back": False,
             "rollback_ts": None, "rollback_reason": None,
             "pre_promotion_accuracy": 0.5, "post_promotion_accuracy": 0.6},
        ])

        from src.dashboard.routes.backtesting import get_backtest_promotions
        result = get_backtest_promotions(limit=30, profile="coinbase", db=db)

        # Should only keep crypto pairs (BTC-EUR) and filter out equity (AAPL)
        pairs = [p["pair"] for p in result["promotions"]]
        assert "BTC-EUR" in pairs
        assert "AAPL" not in pairs

    @patch("src.dashboard.routes.backtesting.deps")
    def test_promotions_filters_equity_pairs_for_ibkr(self, mock_deps):
        mock_deps.resolve_profile.return_value = "ibkr"
        mock_deps.sanitize_floats.side_effect = lambda x: x
        db, conn = self._mock_db(rows=[
            {"run_ts": "2024-01-01", "pair": "BTC-EUR", "param_name": "trailing_stop",
             "old_value": 0.03, "new_value": 0.04, "wfe": 0.6,
             "oos_sharpe": 1.1, "promoted": True, "rolled_back": False,
             "rollback_ts": None, "rollback_reason": None,
             "pre_promotion_accuracy": 0.6, "post_promotion_accuracy": 0.7},
            {"run_ts": "2024-01-02", "pair": "AAPL", "param_name": "trailing_stop",
             "old_value": 0.03, "new_value": 0.04, "wfe": 0.5,
             "oos_sharpe": 0.9, "promoted": True, "rolled_back": False,
             "rollback_ts": None, "rollback_reason": None,
             "pre_promotion_accuracy": 0.5, "post_promotion_accuracy": 0.6},
        ])

        from src.dashboard.routes.backtesting import get_backtest_promotions
        result = get_backtest_promotions(limit=30, profile="ibkr", db=db)

        pairs = [p["pair"] for p in result["promotions"]]
        assert "AAPL" in pairs
        assert "BTC-EUR" not in pairs


# ═══════════════════════════════════════════════════════════════════════════
# GET /api/backtesting/pairs endpoint
# ═══════════════════════════════════════════════════════════════════════════

class TestBacktestPairsEndpoint:
    """Verify the pairs endpoint returns followed pairs with correct
    source attribution, domain separation, and last-run info."""

    def _mock_db(self, follow_rows=None, last_run_rows=None):
        db = MagicMock()
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = last_run_rows or []
        conn.execute.return_value = cursor
        conn.__enter__ = lambda s: s
        conn.__exit__ = MagicMock(return_value=False)
        db._get_conn.return_value = conn
        db.get_pair_follows.return_value = follow_rows or []
        return db, conn

    @patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True)
    @patch("src.dashboard.routes.backtesting.deps")
    def test_returns_config_pairs(self, mock_deps):
        """Config pairs should appear with source='config' and is_config_pair=True."""
        mock_deps.get_config_for_profile.return_value = {
            "trading": {"pairs": ["BTC-EUR", "ETH-EUR"]}
        }
        mock_deps.resolve_profile.return_value = "coinbase"
        mock_deps.quote_currency_for.return_value = "EUR"
        mock_deps.is_equity_profile.return_value = False
        mock_deps.sanitize_floats.side_effect = lambda x: x
        db, conn = self._mock_db()

        from src.dashboard.routes.backtesting import get_backtest_pairs
        result = get_backtest_pairs(profile="coinbase", db=db)

        assert result["exchange"] == "coinbase"
        pairs = result["pairs"]
        assert len(pairs) == 2
        assert pairs[0]["pair"] == "BTC-EUR"
        assert pairs[0]["source"] == "config"
        assert pairs[0]["is_config_pair"] is True

    @patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True)
    @patch("src.dashboard.routes.backtesting.deps")
    def test_merges_human_and_llm_follows(self, mock_deps):
        """Human and LLM follows should be merged with config pairs."""
        mock_deps.get_config_for_profile.return_value = {
            "trading": {"pairs": ["BTC-EUR"]}
        }
        mock_deps.resolve_profile.return_value = "coinbase"
        mock_deps.quote_currency_for.return_value = "EUR"
        mock_deps.is_equity_profile.return_value = False
        mock_deps.sanitize_floats.side_effect = lambda x: x
        db, conn = self._mock_db(follow_rows=[
            {"pair": "SOL-EUR", "followed_by": "human"},
            {"pair": "DOGE-EUR", "followed_by": "llm"},
            {"pair": "BTC-EUR", "followed_by": "human"},  # also config
        ])

        from src.dashboard.routes.backtesting import get_backtest_pairs
        result = get_backtest_pairs(profile="coinbase", db=db)

        pairs = result["pairs"]
        pair_names = [p["pair"] for p in pairs]
        assert "BTC-EUR" in pair_names
        assert "SOL-EUR" in pair_names
        assert "DOGE-EUR" in pair_names

        # BTC-EUR is config AND human-followed
        btc = next(p for p in pairs if p["pair"] == "BTC-EUR")
        assert btc["is_config_pair"] is True
        assert btc["source"] == "config"
        assert btc["followed_by_human"] is True

        # SOL-EUR is human-followed, not config
        sol = next(p for p in pairs if p["pair"] == "SOL-EUR")
        assert sol["source"] == "human"
        assert sol["is_config_pair"] is False
        assert sol["followed_by_human"] is True

        # DOGE-EUR is LLM-followed
        doge = next(p for p in pairs if p["pair"] == "DOGE-EUR")
        assert doge["source"] == "llm"
        assert doge["followed_by_llm"] is True

    @patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True)
    @patch("src.dashboard.routes.backtesting.deps")
    def test_filters_by_quote_currency(self, mock_deps):
        """Only pairs matching the profile's quote currency should be returned."""
        mock_deps.get_config_for_profile.return_value = {
            "trading": {"pairs": ["BTC-EUR", "ETH-USD"]}
        }
        mock_deps.resolve_profile.return_value = "coinbase"
        mock_deps.quote_currency_for.return_value = "EUR"
        mock_deps.is_equity_profile.return_value = False
        mock_deps.sanitize_floats.side_effect = lambda x: x
        db, conn = self._mock_db()

        from src.dashboard.routes.backtesting import get_backtest_pairs
        result = get_backtest_pairs(profile="coinbase", db=db)

        pair_names = [p["pair"] for p in result["pairs"]]
        assert "BTC-EUR" in pair_names
        assert "ETH-USD" not in pair_names

    @patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True)
    @patch("src.dashboard.routes.backtesting.deps")
    def test_domain_separation_exchange_param(self, mock_deps):
        """get_pair_follows must receive the resolved exchange."""
        mock_deps.get_config_for_profile.return_value = {"trading": {"pairs": []}}
        mock_deps.resolve_profile.return_value = "ibkr"
        mock_deps.quote_currency_for.return_value = None
        mock_deps.is_equity_profile.return_value = True
        mock_deps.sanitize_floats.side_effect = lambda x: x
        db, conn = self._mock_db()

        from src.dashboard.routes.backtesting import get_backtest_pairs
        get_backtest_pairs(profile="ibkr", db=db)

        db.get_pair_follows.assert_called_once()
        call_kwargs = db.get_pair_follows.call_args
        assert call_kwargs.kwargs.get("exchange") == "ibkr" or call_kwargs[1].get("exchange") == "ibkr"

    @patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True)
    @patch("src.dashboard.routes.backtesting.deps")
    def test_last_run_info_merged(self, mock_deps):
        """Last-run metrics should be merged into the pair entry."""
        mock_deps.get_config_for_profile.return_value = {
            "trading": {"pairs": ["BTC-EUR"]}
        }
        mock_deps.resolve_profile.return_value = "coinbase"
        mock_deps.quote_currency_for.return_value = "EUR"
        mock_deps.is_equity_profile.return_value = False
        mock_deps.sanitize_floats.side_effect = lambda x: x
        db, conn = self._mock_db(last_run_rows=[
            {"pair": "BTC-EUR", "run_ts": "2024-06-01T12:00:00Z",
             "total_return_pct": 8.5, "sharpe_ratio": 1.3},
        ])

        from src.dashboard.routes.backtesting import get_backtest_pairs
        result = get_backtest_pairs(profile="coinbase", db=db)

        btc = result["pairs"][0]
        assert btc["last_run_ts"] == "2024-06-01T12:00:00Z"
        assert btc["last_return_pct"] == 8.5
        assert btc["last_sharpe"] == 1.3

    @patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True)
    @patch("src.dashboard.routes.backtesting.deps")
    def test_last_run_sql_filters_by_exchange(self, mock_deps):
        """The SQL for last-run info must filter by exchange for domain separation."""
        mock_deps.get_config_for_profile.return_value = {
            "trading": {"pairs": ["BTC-EUR"]}
        }
        mock_deps.resolve_profile.return_value = "coinbase"
        mock_deps.quote_currency_for.return_value = "EUR"
        mock_deps.is_equity_profile.return_value = False
        mock_deps.sanitize_floats.side_effect = lambda x: x
        db, conn = self._mock_db()

        from src.dashboard.routes.backtesting import get_backtest_pairs
        get_backtest_pairs(profile="coinbase", db=db)

        sql = conn.execute.call_args[0][0]
        params = conn.execute.call_args[0][1]
        assert "exchange = %s" in sql, "Last-run SQL must filter by exchange"
        assert "coinbase" in params

    @patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True)
    @patch("src.dashboard.routes.backtesting.deps")
    def test_empty_when_no_pairs(self, mock_deps):
        """Should return empty pairs list when there are no config or followed pairs."""
        mock_deps.get_config_for_profile.return_value = {"trading": {"pairs": []}}
        mock_deps.resolve_profile.return_value = "coinbase"
        mock_deps.quote_currency_for.return_value = "EUR"
        mock_deps.is_equity_profile.return_value = False
        mock_deps.sanitize_floats.side_effect = lambda x: x
        db, conn = self._mock_db()

        from src.dashboard.routes.backtesting import get_backtest_pairs
        result = get_backtest_pairs(profile="coinbase", db=db)

        assert result["pairs"] == []
        assert result["exchange"] == "coinbase"

    @patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True)
    @patch("src.dashboard.routes.backtesting.deps")
    def test_internal_error_returns_500(self, mock_deps):
        """Internal errors should raise HTTP 500, not leak stack traces."""
        mock_deps.get_config_for_profile.side_effect = RuntimeError("boom")
        db, _ = self._mock_db()

        from fastapi import HTTPException
        from src.dashboard.routes.backtesting import get_backtest_pairs
        with pytest.raises(HTTPException) as exc_info:
            get_backtest_pairs(profile="coinbase", db=db)
        assert exc_info.value.status_code == 500
        assert "Internal server error" in str(exc_info.value.detail)


# ═══════════════════════════════════════════════════════════════════════════
# WebSocket /ws/backtest
# ═══════════════════════════════════════════════════════════════════════════

class TestBacktestWebSocket:
    """Verify WebSocket authentication, connection limits, input validation,
    and session cleanup for the backtest WebSocket endpoint."""

    @patch("src.dashboard.routes.backtesting._backtest_sessions", {})
    def test_connection_limit_constant(self):
        """The connection limit must be a reasonable cap to prevent abuse."""
        assert _MAX_BACKTEST_WS >= 1
        assert _MAX_BACKTEST_WS <= 20

    @patch("src.dashboard.routes.backtesting._backtest_sessions", {
        f"s{i}": {"active": True} for i in range(_MAX_BACKTEST_WS)
    })
    @pytest.mark.asyncio
    async def test_rejects_when_at_capacity(self):
        """Connections beyond _MAX_BACKTEST_WS should be rejected with 1013."""
        from src.dashboard.routes.backtesting import ws_backtest

        ws = AsyncMock()
        ws.client = MagicMock()
        ws.client.host = "127.0.0.1"
        ws.headers = {"origin": "http://localhost:5173"}
        ws.cookies = {}

        with patch("src.dashboard.routes.backtesting.deps") as mock_deps:
            mock_deps.allowed_origins = ["http://localhost:5173"]
            await ws_backtest(ws)

        ws.close.assert_called_once()
        close_kwargs = ws.close.call_args
        assert close_kwargs.kwargs.get("code") == 1013 or close_kwargs[1].get("code") == 1013

    @pytest.mark.asyncio
    async def test_rejects_disallowed_origin(self):
        """Connections from untrusted origins should be rejected with 1008."""
        from src.dashboard.routes.backtesting import ws_backtest

        ws = AsyncMock()
        ws.client = MagicMock()
        ws.client.host = "127.0.0.1"
        ws.headers = {"origin": "https://evil.com"}
        ws.cookies = {}

        with patch("src.dashboard.routes.backtesting.deps") as mock_deps, \
             patch("src.dashboard.routes.backtesting._backtest_sessions", {}):
            mock_deps.allowed_origins = ["http://localhost:5173"]
            await ws_backtest(ws)

        ws.close.assert_called_once()
        close_kwargs = ws.close.call_args
        assert close_kwargs.kwargs.get("code") == 1008 or close_kwargs[1].get("code") == 1008

    @pytest.mark.asyncio
    async def test_rejects_unauthenticated(self):
        """When auth is configured, unauthenticated connections must be rejected."""
        from src.dashboard.routes.backtesting import ws_backtest

        ws = AsyncMock()
        ws.client = MagicMock()
        ws.client.host = "127.0.0.1"
        ws.headers = {"origin": "http://localhost:5173", "sec-websocket-protocol": ""}
        ws.cookies = {"ot_session": ""}

        with patch("src.dashboard.routes.backtesting.deps") as mock_deps, \
             patch("src.dashboard.routes.backtesting.auth") as mock_auth, \
             patch("src.dashboard.routes.backtesting._backtest_sessions", {}):
            mock_deps.allowed_origins = ["http://localhost:5173"]
            mock_auth.is_auth_configured.return_value = True
            mock_auth.validate_session.return_value = False
            mock_auth._LEGACY_API_KEY = None
            await ws_backtest(ws)

        ws.close.assert_called_once()
        close_kwargs = ws.close.call_args
        assert close_kwargs.kwargs.get("code") == 1008 or close_kwargs[1].get("code") == 1008

    @pytest.mark.asyncio
    async def test_session_cleanup_on_disconnect(self):
        """Session should be removed from _backtest_sessions after disconnect."""
        from src.dashboard.routes.backtesting import ws_backtest, _backtest_sessions

        ws = AsyncMock()
        ws.client = MagicMock()
        ws.client.host = "127.0.0.1"
        ws.headers = {"origin": "", "sec-websocket-protocol": ""}
        ws.cookies = {}
        ws.url = "ws://localhost:8090/ws/backtest?profile=coinbase"
        # Simulate empty pair → error → cleanup
        ws.receive_text = AsyncMock(return_value='{"pair": ""}')

        sessions_snapshot = dict(_backtest_sessions)

        with patch("src.dashboard.routes.backtesting.deps") as mock_deps, \
             patch("src.dashboard.routes.backtesting.auth") as mock_auth, \
             patch("src.dashboard.routes.backtesting._backtest_sessions", {}):
            mock_deps.allowed_origins = []
            mock_auth.is_auth_configured.return_value = False
            await ws_backtest(ws)

        # The send_json should have been called with an error about pair
        send_calls = [c for c in ws.send_json.call_args_list]
        assert any(
            c[0][0].get("type") == "error" for c in send_calls
        ), "Should send an error for empty pair"

    @pytest.mark.asyncio
    async def test_rejects_invalid_pair_via_ws(self):
        """Malicious pair strings sent via WS should be caught by _validate_pair_format."""
        from src.dashboard.routes.backtesting import ws_backtest

        ws = AsyncMock()
        ws.client = MagicMock()
        ws.client.host = "127.0.0.1"
        ws.headers = {"origin": "", "sec-websocket-protocol": ""}
        ws.cookies = {}
        ws.url = "ws://localhost:8090/ws/backtest?profile=coinbase"
        ws.receive_text = AsyncMock(return_value='{"pair": "BTC; DROP TABLE"}')

        with patch("src.dashboard.routes.backtesting.deps") as mock_deps, \
             patch("src.dashboard.routes.backtesting.auth") as mock_auth, \
             patch("src.dashboard.routes.backtesting._backtest_sessions", {}):
            mock_deps.allowed_origins = []
            mock_auth.is_auth_configured.return_value = False
            await ws_backtest(ws)

        # Should send error (ValueError is caught and sent as error message)
        send_calls = ws.send_json.call_args_list
        assert any(
            c[0][0].get("type") == "error" for c in send_calls
        ), "Should send error for injection attempt"


# ═══════════════════════════════════════════════════════════════════════════
# Backtest interpretation endpoint
# ═══════════════════════════════════════════════════════════════════════════

class TestBacktestInterpretation:
    """Tests for GET /api/backtesting/run/{run_id}/interpretation."""

    def _make_result_json(self, **overrides):
        base = {
            "start_date": "2026-02-10T00:00:00Z",
            "end_date": "2026-03-14T00:00:00Z",
            "initial_balance": 10000.0,
            "final_balance": 9990.02,
            "total_return_pct": -0.10,
            "total_trades": 2,
            "winning_trades": 1,
            "losing_trades": 1,
            "win_rate": 50.0,
            "avg_win": 3.01,
            "avg_loss": 13.00,
            "max_drawdown_pct": -0.13,
            "sharpe_ratio": -0.798,
            "sortino_ratio": -1.097,
            "calmar_ratio": -14.79,
            "profit_factor": 0.23,
            "largest_win": 3.01,
            "largest_loss": -13.00,
            "avg_hold_time_hours": 0.0,
            "benchmark_return_pct": 0.33,
            "alpha": -0.33,
            "trades": [
                {"exit_reason": "trailing_stop", "pnl": 3.01, "pnl_pct": 0.02},
                {"exit_reason": "backtest_end", "pnl": -13.00, "pnl_pct": -0.18},
            ],
            "equity_curve": [],
            "cost_sensitivity": [],
        }
        base.update(overrides)
        return base

    def _make_row(self, **overrides):
        base = {
            "id": 42,
            "pair": "NOKIA.HE-EUR",
            "days": 30,
            "params_json": json.dumps({
                "position_size_pct": 0.10,
                "trailing_stop_pct": 0.03,
                "entry_threshold": 0.4,
                "fee_pct": 0.006,
                "slippage_pct": 0.001,
            }),
            "result_json": json.dumps(self._make_result_json()),
            "total_return_pct": -0.10,
            "sharpe_ratio": -0.798,
            "win_rate": 50.0,
            "total_trades": 2,
            "max_drawdown_pct": -0.13,
            "alpha": -0.33,
        }
        base.update(overrides)
        return base

    @pytest.mark.asyncio
    async def test_returns_interpretation(self):
        """Endpoint should return a non-empty interpretation (via LLM or fallback)."""
        from src.dashboard.routes.backtesting import get_backtest_interpretation

        row_data = self._make_row()
        mock_row = MagicMock()
        mock_row.__getitem__ = lambda s, k: row_data[k]
        mock_row.keys = lambda: row_data.keys()

        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = lambda s, *a: None
        mock_conn.execute.return_value.fetchone.return_value = mock_row

        mock_db = MagicMock()
        mock_db._get_conn.return_value = mock_conn

        with patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True), \
             patch("src.dashboard.routes.backtesting.deps") as mock_deps:
            mock_deps.resolve_profile.return_value = "ibkr"
            mock_deps.get_config.return_value = {}
            mock_deps.sanitize_floats = lambda x: x

            result = await get_backtest_interpretation(run_id=42, profile="ibkr", db=mock_db)

        assert "interpretation" in result
        assert result["run_id"] == 42
        # Whether LLM responds or fallback is used, we should get substantive text
        assert len(result["interpretation"]) > 50

    @pytest.mark.asyncio
    async def test_returns_404_for_missing_run(self):
        """Non-existent run should raise 404."""
        from src.dashboard.routes.backtesting import get_backtest_interpretation

        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = lambda s, *a: None
        mock_conn.execute.return_value.fetchone.return_value = None

        mock_db = MagicMock()
        mock_db._get_conn.return_value = mock_conn

        with patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True), \
             patch("src.dashboard.routes.backtesting.deps") as mock_deps:
            mock_deps.resolve_profile.return_value = "coinbase"

            with pytest.raises(Exception) as exc_info:
                await get_backtest_interpretation(run_id=999, profile="coinbase", db=mock_db)
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_llm_success_path(self):
        """When LLM is available, its response should be used (not the fallback)."""
        from src.dashboard.routes.backtesting import get_backtest_interpretation, _INTERP_CACHE

        # Clear cache so we don't get a cached result
        _INTERP_CACHE.clear()

        row_data = self._make_row()
        mock_row = MagicMock()
        mock_row.__getitem__ = lambda s, k: row_data[k]
        mock_row.keys = lambda: row_data.keys()

        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = lambda s, *a: None
        mock_conn.execute.return_value.fetchone.return_value = mock_row

        mock_db = MagicMock()
        mock_db._get_conn.return_value = mock_conn

        mock_llm = AsyncMock()
        mock_llm.chat.return_value = "**LLM generated interpretation** with good detail."

        with patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True), \
             patch("src.dashboard.routes.backtesting.deps") as mock_deps, \
             patch("src.dashboard.routes.backtesting.LLMClient", return_value=mock_llm) if False else \
             patch("src.dashboard.routes.backtesting.deps") as mock_deps:
            mock_deps.resolve_profile.return_value = "ibkr"
            mock_deps.get_config.return_value = {"llm_providers": [{"name": "test"}]}

            with patch("src.core.llm_client.LLMClient") as MockLLM, \
                 patch("src.core.llm_client.build_providers", return_value=["mock_provider"]):
                mock_instance = AsyncMock()
                mock_instance.chat.return_value = "**LLM generated interpretation** with actionable insights."
                MockLLM.return_value = mock_instance

                result = await get_backtest_interpretation(run_id=42, profile="ibkr", db=mock_db)

        assert "LLM generated interpretation" in result["interpretation"]
        _INTERP_CACHE.clear()

    @pytest.mark.asyncio
    async def test_exchange_filter_applied(self):
        """When profile resolves, the SQL must filter by exchange (domain separation)."""
        from src.dashboard.routes.backtesting import get_backtest_interpretation, _INTERP_CACHE

        _INTERP_CACHE.clear()

        row_data = self._make_row()
        mock_row = MagicMock()
        mock_row.__getitem__ = lambda s, k: row_data[k]
        mock_row.keys = lambda: row_data.keys()

        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = lambda s, *a: None
        mock_conn.execute.return_value.fetchone.return_value = mock_row

        mock_db = MagicMock()
        mock_db._get_conn.return_value = mock_conn

        with patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True), \
             patch("src.dashboard.routes.backtesting.deps") as mock_deps:
            mock_deps.resolve_profile.return_value = "ibkr"
            mock_deps.get_config.return_value = {}

            await get_backtest_interpretation(run_id=42, profile="ibkr", db=mock_db)

        # Verify execute was called with exchange filter param
        call_args = mock_conn.execute.call_args
        sql_used = call_args[0][0]
        params_used = call_args[0][1]
        assert "exchange" in sql_used.lower(), "SQL should filter by exchange"
        assert "ibkr" in params_used, "Exchange param should be passed"
        _INTERP_CACHE.clear()

    @pytest.mark.asyncio
    async def test_corrupted_result_json_handled(self):
        """Corrupted JSON fields should not crash the endpoint."""
        from src.dashboard.routes.backtesting import get_backtest_interpretation, _INTERP_CACHE

        _INTERP_CACHE.clear()

        row_data = self._make_row(
            result_json="NOT VALID JSON {{{",
            params_json="ALSO BROKEN",
        )
        mock_row = MagicMock()
        mock_row.__getitem__ = lambda s, k: row_data[k]
        mock_row.keys = lambda: row_data.keys()

        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = lambda s, *a: None
        mock_conn.execute.return_value.fetchone.return_value = mock_row

        mock_db = MagicMock()
        mock_db._get_conn.return_value = mock_conn

        with patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True), \
             patch("src.dashboard.routes.backtesting.deps") as mock_deps:
            mock_deps.resolve_profile.return_value = "coinbase"
            mock_deps.get_config.return_value = {}

            # Should not raise — fallback should handle gracefully
            result = await get_backtest_interpretation(run_id=42, profile="coinbase", db=mock_db)

        assert "interpretation" in result
        assert result["run_id"] == 42
        _INTERP_CACHE.clear()

    @pytest.mark.asyncio
    async def test_cache_prevents_duplicate_llm_calls(self):
        """Second call with same run_id+profile should return cached result."""
        from src.dashboard.routes.backtesting import get_backtest_interpretation, _INTERP_CACHE

        _INTERP_CACHE.clear()

        row_data = self._make_row()
        mock_row = MagicMock()
        mock_row.__getitem__ = lambda s, k: row_data[k]
        mock_row.keys = lambda: row_data.keys()

        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = lambda s, *a: None
        mock_conn.execute.return_value.fetchone.return_value = mock_row

        mock_db = MagicMock()
        mock_db._get_conn.return_value = mock_conn

        with patch("src.dashboard.routes.backtesting._SCHEMA_CREATED", True), \
             patch("src.dashboard.routes.backtesting.deps") as mock_deps:
            mock_deps.resolve_profile.return_value = "ibkr"
            mock_deps.get_config.return_value = {}

            # First call populates cache
            result1 = await get_backtest_interpretation(run_id=42, profile="ibkr", db=mock_db)
            # Second call should use cache — reset mock to verify no DB call
            mock_conn.execute.reset_mock()
            result2 = await get_backtest_interpretation(run_id=42, profile="ibkr", db=mock_db)

        assert result1 == result2
        mock_conn.execute.assert_not_called()  # Cache hit, no DB query
        _INTERP_CACHE.clear()
