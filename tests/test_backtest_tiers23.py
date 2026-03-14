"""
Tests for Tiers 2+3: backtest integration enhancements.

Covers:
- SettingsAdvisor backtest validation (_validate_risk_via_backtest)
- NightlyBacktestWorkflow + run_nightly_backtests activity
- Score divergence activity (fetch_score_divergence)
- entry_score recording in record_trade
- Candle fetch utility module
"""
from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# Bootstrap src.core into sys.modules first to break the circular import chain
# (src.core.__init__ → orchestrator → src.agents → base_agent → src.core.llm_client)
from src.core.rules import AbsoluteRules  # noqa: F401

_sa_mod = importlib.import_module("src.agents.settings_advisor")
SettingsAdvisorAgent = _sa_mod.SettingsAdvisorAgent
_BT_SHARPE_DEGRADE_LIMIT = _sa_mod._BT_SHARPE_DEGRADE_LIMIT
_BT_DRAWDOWN_INCREASE_LIMIT = _sa_mod._BT_DRAWDOWN_INCREASE_LIMIT
_BT_VALIDATION_DAYS = _sa_mod._BT_VALIDATION_DAYS
_BT_RISK_FIELDS = _sa_mod._BT_RISK_FIELDS


# ---------------------------------------------------------------------------
# Candle fetch utility
# ---------------------------------------------------------------------------

class TestCandleFetchUtility:
    """Test the shared candle_fetch module."""

    def test_is_equity_pair_crypto(self):
        from src.backtesting.candle_fetch import is_equity_pair
        assert is_equity_pair("BTC-USD") is False
        assert is_equity_pair("ETH-EUR") is False

    def test_is_equity_pair_equity(self):
        from src.backtesting.candle_fetch import is_equity_pair
        assert is_equity_pair("ABB.ST-SEK") is True
        assert is_equity_pair("AAPL") is True

    def test_is_equity_pair_empty(self):
        from src.backtesting.candle_fetch import is_equity_pair
        assert is_equity_pair("") is False

    @patch("src.backtesting.candle_fetch._build_coinbase_client", return_value=None)
    def test_fetch_crypto_no_client(self, _mock):
        from src.backtesting.candle_fetch import fetch_candles
        result = fetch_candles("BTC-USD", 30, is_equity=False)
        assert result == []

    @patch("src.backtesting.candle_fetch._fetch_equity_candles", return_value=[{"close": 100}])
    def test_fetch_equity_delegates(self, mock_eq):
        from src.backtesting.candle_fetch import fetch_candles
        result = fetch_candles("AAPL", 7, is_equity=True)
        assert len(result) == 1
        mock_eq.assert_called_once()


# ---------------------------------------------------------------------------
# SettingsAdvisor backtest validation
# ---------------------------------------------------------------------------

class TestSettingsAdvisorBacktestValidation:
    """Test _validate_risk_via_backtest rejects degrading settings."""

    def _make_advisor(self, config=None):
        llm = MagicMock()
        state = MagicMock()
        cfg = config or {
            "trading": {"pairs": ["BTC-USD"]},
            "risk": {"stop_loss_pct": 0.05, "trailing_stop_pct": 0.03},
            "analysis": {"technical": {}},
        }
        rules = MagicMock()
        return SettingsAdvisorAgent(llm, state, cfg, rules)

    def _mock_engine_result(self, sharpe, max_dd, trades):
        r = MagicMock()
        r.sharpe_ratio = sharpe
        r.max_drawdown_pct = max_dd
        r.total_trades = trades
        return r

    @patch("src.backtesting.candle_fetch.is_equity_pair", return_value=False)
    @patch("src.backtesting.candle_fetch.fetch_candles")
    def test_passes_when_sharpe_improves(self, mock_fetch, _eq):
        mock_fetch.return_value = [{"close": i} for i in range(200)]

        baseline = self._mock_engine_result(sharpe=1.0, max_dd=5.0, trades=10)
        proposed = self._mock_engine_result(sharpe=1.2, max_dd=4.0, trades=12)

        with patch("src.backtesting.engine.BacktestEngine") as mock_cls:
            # First call = baseline, second = proposed
            mock_cls.return_value.run = MagicMock(side_effect=[baseline, proposed])

            advisor = self._make_advisor()
            surviving, rejected = advisor._validate_risk_via_backtest(
                {"stop_loss_pct": 0.06}, "coinbase"
            )
        assert "stop_loss_pct" in surviving
        assert len(rejected) == 0

    @patch("src.backtesting.candle_fetch.is_equity_pair", return_value=False)
    @patch("src.backtesting.candle_fetch.fetch_candles")
    def test_rejects_when_sharpe_degrades(self, mock_fetch, _eq):
        mock_fetch.return_value = [{"close": i} for i in range(200)]

        baseline = self._mock_engine_result(sharpe=1.0, max_dd=5.0, trades=10)
        proposed = self._mock_engine_result(sharpe=0.5, max_dd=5.0, trades=8)

        with patch("src.backtesting.engine.BacktestEngine") as mock_cls:
            mock_cls.return_value.run = MagicMock(side_effect=[baseline, proposed])

            advisor = self._make_advisor()
            surviving, rejected = advisor._validate_risk_via_backtest(
                {"stop_loss_pct": 0.10}, "coinbase"
            )
        assert "stop_loss_pct" not in surviving
        assert len(rejected) == 1
        assert "Sharpe degraded" in rejected[0]["reason"]

    @patch("src.backtesting.candle_fetch.is_equity_pair", return_value=False)
    @patch("src.backtesting.candle_fetch.fetch_candles")
    def test_rejects_when_drawdown_increases(self, mock_fetch, _eq):
        mock_fetch.return_value = [{"close": i} for i in range(200)]

        baseline = self._mock_engine_result(sharpe=1.0, max_dd=5.0, trades=10)
        proposed = self._mock_engine_result(sharpe=1.0, max_dd=10.0, trades=10)

        with patch("src.backtesting.engine.BacktestEngine") as mock_cls:
            mock_cls.return_value.run = MagicMock(side_effect=[baseline, proposed])

            advisor = self._make_advisor()
            surviving, rejected = advisor._validate_risk_via_backtest(
                {"trailing_stop_pct": 0.01}, "coinbase"
            )
        assert "trailing_stop_pct" not in surviving
        assert any("MaxDD increased" in r["reason"] for r in rejected)

    def test_skips_non_risk_fields(self):
        advisor = self._make_advisor()
        surviving, rejected = advisor._validate_risk_via_backtest(
            {"some_other_field": 42}, "coinbase"
        )
        assert surviving == {"some_other_field": 42}
        assert rejected == []

    def test_skips_when_no_pairs(self):
        advisor = self._make_advisor(config={
            "trading": {"pairs": []},
            "risk": {},
            "analysis": {"technical": {}},
        })
        surviving, rejected = advisor._validate_risk_via_backtest(
            {"stop_loss_pct": 0.10}, "coinbase"
        )
        assert "stop_loss_pct" in surviving
        assert rejected == []

    @patch("src.backtesting.candle_fetch.fetch_candles", return_value=[])
    @patch("src.backtesting.candle_fetch.is_equity_pair", return_value=False)
    def test_skips_when_not_enough_candles(self, _eq, mock_fetch):
        advisor = self._make_advisor()
        surviving, rejected = advisor._validate_risk_via_backtest(
            {"stop_loss_pct": 0.10}, "coinbase"
        )
        assert "stop_loss_pct" in surviving
        assert rejected == []

    def test_empty_updates_returns_empty(self):
        advisor = self._make_advisor()
        surviving, rejected = advisor._validate_risk_via_backtest({}, "coinbase")
        assert surviving == {}
        assert rejected == []


# ---------------------------------------------------------------------------
# Nightly backtest activity
# ---------------------------------------------------------------------------

class TestRunNightlyBacktests:
    """Test the run_nightly_backtests activity."""

    @pytest.mark.asyncio
    async def test_runs_backtests_on_configured_pairs(self):
        from src.planning.activities import run_nightly_backtests

        # Engine result
        result = MagicMock()
        result.total_return_pct = 5.0
        result.sharpe_ratio = 1.5
        result.win_rate = 60.0
        result.total_trades = 10
        result.max_drawdown_pct = 3.0
        result.alpha = 2.0
        result.sortino_ratio = 1.8

        mock_engine = MagicMock()
        mock_engine.return_value.run.return_value = result

        candles = [{"close": i} for i in range(200)]

        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)

        import io, yaml
        config_content = yaml.dump({"trading": {"pairs": ["BTC-USD", "ETH-USD"]}})

        def fake_open(path, *a, **kw):
            return io.StringIO(config_content)

        with patch("src.planning.activities._detect_domain", return_value="crypto"), \
             patch("src.planning.activities.os.path.exists", return_value=True), \
             patch("builtins.open", side_effect=fake_open), \
             patch("src.backtesting.engine.BacktestEngine", mock_engine), \
             patch("src.backtesting.candle_fetch.fetch_candles", return_value=candles), \
             patch("src.planning.activities._get_conn", return_value=conn), \
             patch("src.planning.activities._execute"), \
             patch("src.utils.settings_manager.get_settings_path", return_value="config/coinbase.yaml"):
            out = await run_nightly_backtests("coinbase")

        assert out["ran"] == 2
        assert out["saved"] == 2

    @pytest.mark.asyncio
    async def test_no_pairs_returns_empty(self):
        from src.planning.activities import run_nightly_backtests
        with patch("src.planning.activities._detect_domain", return_value="crypto"), \
             patch("src.planning.activities.os.path.exists", return_value=False), \
             patch("src.utils.settings_manager.get_settings_path", return_value="config/missing.yaml"):
            result = await run_nightly_backtests("coinbase")
            assert result["ran"] == 0
            assert result["saved"] == 0


# ---------------------------------------------------------------------------
# NightlyBacktestWorkflow
# ---------------------------------------------------------------------------

class TestNightlyBacktestWorkflow:
    """Test the NightlyBacktestWorkflow class exists and has correct structure."""

    def test_workflow_class_exists(self):
        from src.planning.workflows import NightlyBacktestWorkflow
        assert hasattr(NightlyBacktestWorkflow, "run")

    def test_workflow_registered_in_worker(self):
        from src.planning.workflows import NightlyBacktestWorkflow
        # Just verify it can be imported from the workflows module
        assert NightlyBacktestWorkflow is not None


# ---------------------------------------------------------------------------
# Score divergence activity
# ---------------------------------------------------------------------------

class TestFetchScoreDivergence:
    """Test the fetch_score_divergence activity."""

    def _make_mock_conn_ctx(self, has_column: bool, rows: list[dict] | None = None):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_dict_cursor = MagicMock()

        # Column existence check
        if has_column:
            mock_cursor.fetchone.return_value = {"column_name": "entry_score"}
        else:
            mock_cursor.fetchone.return_value = None
        mock_conn.cursor.return_value = mock_cursor

        # Data fetch
        mock_dict_cursor.fetchall.return_value = rows or []

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=mock_conn)
        ctx.__exit__ = MagicMock(return_value=False)

        return ctx, mock_conn, mock_cursor, mock_dict_cursor

    @pytest.mark.asyncio
    @patch("src.planning.activities._get_conn")
    @patch("src.planning.activities._execute")
    async def test_no_column_returns_unavailable(self, mock_exec, mock_conn_ctx):
        from src.planning.activities import fetch_score_divergence
        ctx, conn, cursor, _ = self._make_mock_conn_ctx(has_column=False)
        mock_conn_ctx.return_value = ctx

        result = await fetch_score_divergence("coinbase")
        assert result["available"] is False

    @pytest.mark.asyncio
    @patch("src.planning.activities._get_conn")
    @patch("src.planning.activities._execute")
    async def test_with_data(self, mock_exec, mock_conn_ctx):
        from src.planning.activities import fetch_score_divergence

        rows = [
            {"pair": "BTC-USD", "action": "buy", "confidence": 0.8, "entry_score": 0.6, "pnl": 0.05},
            {"pair": "ETH-USD", "action": "buy", "confidence": 0.7, "entry_score": 0.3, "pnl": -0.02},
            {"pair": "SOL-USD", "action": "buy", "confidence": 0.9, "entry_score": 0.5, "pnl": 0.03},
            {"pair": "ADA-USD", "action": "buy", "confidence": 0.6, "entry_score": 0.45, "pnl": 0.01},
            {"pair": "DOT-USD", "action": "buy", "confidence": 0.75, "entry_score": 0.35, "pnl": -0.01},
        ]

        ctx, conn, cursor, _ = self._make_mock_conn_ctx(has_column=True, rows=rows)
        cursor.fetchone.return_value = {"column_name": "entry_score"}
        mock_conn_ctx.return_value = ctx
        mock_exec.return_value.fetchall.return_value = rows

        result = await fetch_score_divergence("coinbase")
        assert result["available"] is True
        assert result["sample_size"] == 5
        assert result["backtest_threshold"] == 0.4
        assert "divergence" in result


# ---------------------------------------------------------------------------
# entry_score in record_trade
# ---------------------------------------------------------------------------

class TestEntryScoreRecordTrade:
    """Test that record_trade accepts and persists entry_score."""

    def test_record_trade_signature_includes_entry_score(self):
        from src.utils.stats_trades import TradesMixin
        import inspect
        sig = inspect.signature(TradesMixin.record_trade)
        assert "entry_score" in sig.parameters

    def test_entry_score_default_is_none(self):
        from src.utils.stats_trades import TradesMixin
        import inspect
        sig = inspect.signature(TradesMixin.record_trade)
        param = sig.parameters["entry_score"]
        assert param.default is None


# ---------------------------------------------------------------------------
# Worker registration
# ---------------------------------------------------------------------------

class TestWorkerRegistration:
    """Verify new activities and workflows are registered."""

    def test_imports_nightly_workflow(self):
        from src.planning.worker import NightlyBacktestWorkflow
        assert NightlyBacktestWorkflow is not None

    def test_imports_nightly_activity(self):
        from src.planning.worker import run_nightly_backtests
        assert run_nightly_backtests is not None

    def test_imports_score_divergence(self):
        from src.planning.worker import fetch_score_divergence
        assert fetch_score_divergence is not None


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

class TestEntryScoreMigration:
    """Verify entry_score is in the migration allowlist and schema."""

    def test_entry_score_in_allowlist(self):
        from src.utils.stats import StatsDB
        assert ("trades", "entry_score") in StatsDB._MIGRATION_ALLOWLIST

    def test_entry_score_in_schema(self):
        """Verify the CREATE TABLE statement includes entry_score."""
        import inspect
        from src.utils.stats import StatsDB
        source = inspect.getsource(StatsDB._init_db)
        assert "entry_score" in source


# ---------------------------------------------------------------------------
# Backtest validation constants
# ---------------------------------------------------------------------------

class TestBacktestValidationConstants:
    """Verify the validation thresholds are reasonable."""

    def test_sharpe_degrade_limit(self):
        assert 0 < _BT_SHARPE_DEGRADE_LIMIT <= 0.5

    def test_drawdown_increase_limit(self):
        assert 0 < _BT_DRAWDOWN_INCREASE_LIMIT <= 1.0

    def test_validation_days(self):
        assert _BT_VALIDATION_DAYS >= 7

    def test_risk_fields_covered(self):
        assert "stop_loss_pct" in _BT_RISK_FIELDS
        assert "trailing_stop_pct" in _BT_RISK_FIELDS
        assert "take_profit_pct" in _BT_RISK_FIELDS
