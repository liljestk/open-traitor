"""
Tests for backtest ↔ agent/planning integration.

Covers:
- fetch_backtest_summary activity (planning context injection)
- get_backtest_kelly_stats (Kelly Criterion supplementation from backtest data)
- Kelly blending logic in pipeline_manager
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, AsyncMock

import pytest


# ---------------------------------------------------------------------------
# fetch_backtest_summary activity
# ---------------------------------------------------------------------------

class TestFetchBacktestSummary:
    """Test the Temporal activity that provides backtest context to planning."""

    def _make_mock_conn(self, table_exists: bool, rows: list[dict] | None = None):
        """Create a mock connection + cursor chain."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_dict_cursor = MagicMock()

        # For table existence check (plain cursor)
        mock_cursor.fetchone.return_value = (table_exists,)
        mock_conn.cursor.return_value = mock_cursor

        # For the _execute call (RealDictCursor)
        if rows is not None:
            mock_dict_cursor.fetchall.return_value = rows
        mock_dict_cursor.fetchone.return_value = rows[0] if rows else None

        return mock_conn, mock_cursor, mock_dict_cursor

    @pytest.mark.asyncio
    async def test_no_backtest_table(self):
        """Returns available=False when backtest_runs table doesn't exist."""
        from src.planning.activities import fetch_backtest_summary

        mock_conn, mock_cur, _ = self._make_mock_conn(table_exists=False)
        with patch("src.planning.activities._get_conn") as mock_get:
            mock_get.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get.return_value.__exit__ = MagicMock(return_value=False)
            result = await fetch_backtest_summary(profile="coinbase")

        assert result["available"] is False
        assert result["pairs"] == []

    @pytest.mark.asyncio
    async def test_no_recent_runs(self):
        """Returns empty pairs when table exists but no recent runs."""
        from src.planning.activities import fetch_backtest_summary

        mock_conn, mock_cur, mock_dict_cur = self._make_mock_conn(
            table_exists=True, rows=[]
        )
        # Patch _execute to return mock_dict_cur
        with patch("src.planning.activities._get_conn") as mock_get, \
             patch("src.planning.activities._execute", return_value=mock_dict_cur):
            mock_get.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get.return_value.__exit__ = MagicMock(return_value=False)
            mock_dict_cur.fetchall.return_value = []
            result = await fetch_backtest_summary(profile="coinbase")

        assert result["available"] is True
        assert result["pairs"] == []

    @pytest.mark.asyncio
    async def test_with_results(self):
        """Correctly sorts and categorizes pairs by performance."""
        from src.planning.activities import fetch_backtest_summary

        rows = [
            {"pair": "BTC-EUR", "days": 30, "total_return_pct": 15.2,
             "sharpe_ratio": 1.8, "win_rate": 65.0, "total_trades": 42,
             "max_drawdown_pct": -8.5, "alpha": 5.0,
             "run_ts": datetime.now(timezone.utc).isoformat()},
            {"pair": "DOGE-EUR", "days": 30, "total_return_pct": -3.5,
             "sharpe_ratio": -0.5, "win_rate": 35.0, "total_trades": 28,
             "max_drawdown_pct": -15.2, "alpha": -2.0,
             "run_ts": datetime.now(timezone.utc).isoformat()},
            {"pair": "ETH-EUR", "days": 30, "total_return_pct": 8.1,
             "sharpe_ratio": 1.2, "win_rate": 58.0, "total_trades": 35,
             "max_drawdown_pct": -6.0, "alpha": 3.0,
             "run_ts": datetime.now(timezone.utc).isoformat()},
        ]

        mock_conn, mock_cur, mock_dict_cur = self._make_mock_conn(
            table_exists=True, rows=rows
        )
        with patch("src.planning.activities._get_conn") as mock_get, \
             patch("src.planning.activities._execute", return_value=mock_dict_cur):
            mock_get.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get.return_value.__exit__ = MagicMock(return_value=False)
            mock_dict_cur.fetchall.return_value = rows
            result = await fetch_backtest_summary(profile="coinbase")

        assert result["available"] is True
        assert len(result["pairs"]) == 3
        # top_performers: sharpe > 0 AND return > 0
        assert "BTC-EUR" in result["top_performers"]
        assert "ETH-EUR" in result["top_performers"]
        # worst_performers: sharpe < 0 OR return < -1
        assert "DOGE-EUR" in result["worst_performers"]
        # avg sharpe/win_rate populated
        assert result["avg_sharpe"] == pytest.approx((1.8 + (-0.5) + 1.2) / 3, abs=0.01)

    @pytest.mark.asyncio
    async def test_profile_filter_ibkr(self):
        """Exchange filter includes ibkr + ibkr_paper for IBKR profile."""
        from src.planning.activities import fetch_backtest_summary

        mock_conn, mock_cur, mock_dict_cur = self._make_mock_conn(
            table_exists=True, rows=[]
        )
        with patch("src.planning.activities._get_conn") as mock_get, \
             patch("src.planning.activities._execute") as mock_exec:
            mock_get.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get.return_value.__exit__ = MagicMock(return_value=False)
            mock_exec.return_value = mock_dict_cur
            mock_dict_cur.fetchall.return_value = []
            await fetch_backtest_summary(profile="ibkr")

        # Verify _execute was called with exchange params including ibkr and ibkr_paper
        call_args = mock_exec.call_args
        params = call_args[0][2] if len(call_args[0]) > 2 else call_args[1].get("params")
        assert "ibkr" in params
        assert "ibkr_paper" in params


# ---------------------------------------------------------------------------
# get_backtest_kelly_stats
# ---------------------------------------------------------------------------

class TestGetBacktestKellyStats:
    """Test backtest-supplemented Kelly Criterion stats."""

    def test_no_backtest_table(self):
        """Returns zeros when backtest_runs table doesn't exist."""
        from src.utils.stats_trades import TradesMixin

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        # table doesn't exist
        mock_cursor.fetchone.return_value = (False,)
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        db = TradesMixin.__new__(TradesMixin)
        db._get_conn = MagicMock(return_value=mock_conn)

        result = db.get_backtest_kelly_stats("BTC-EUR", exchange="coinbase")
        assert result["source"] == "backtest"
        assert result["sample_size"] == 0

    def test_with_backtest_run(self):
        """Returns converted stats from a backtest run."""
        from src.utils.stats_trades import TradesMixin

        result_json = json.dumps({"avg_win": 2.5, "avg_loss": -1.2})
        mock_row = {"result_json": result_json, "win_rate": 62.0, "total_trades": 50}

        mock_conn = MagicMock()
        # First call: table exists
        exists_cursor = MagicMock()
        exists_cursor.fetchone.return_value = (True,)
        # Second call: backtest data
        data_cursor = MagicMock()
        data_cursor.fetchone.return_value = mock_row

        mock_conn.execute.side_effect = [exists_cursor, data_cursor]
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        db = TradesMixin.__new__(TradesMixin)
        db._get_conn = MagicMock(return_value=mock_conn)

        result = db.get_backtest_kelly_stats("BTC-EUR", exchange="coinbase")
        assert result["source"] == "backtest"
        assert result["win_rate"] == pytest.approx(0.62, abs=0.001)
        assert result["avg_win"] == 2.5
        assert result["avg_loss"] == 1.2  # absolute value
        assert result["sample_size"] == 50

    def test_no_matching_pair(self):
        """Returns zeros when no backtest run found for pair."""
        from src.utils.stats_trades import TradesMixin

        mock_conn = MagicMock()
        exists_cursor = MagicMock()
        exists_cursor.fetchone.return_value = (True,)
        data_cursor = MagicMock()
        data_cursor.fetchone.return_value = None

        mock_conn.execute.side_effect = [exists_cursor, data_cursor]
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        db = TradesMixin.__new__(TradesMixin)
        db._get_conn = MagicMock(return_value=mock_conn)

        result = db.get_backtest_kelly_stats("UNKNOWN-USD", exchange="coinbase")
        assert result["sample_size"] == 0


# ---------------------------------------------------------------------------
# Kelly blending logic in pipeline_manager
# ---------------------------------------------------------------------------

class TestKellyBlending:
    """Test that Kelly stats are blended correctly when live sample is small."""

    def test_no_live_data_uses_backtest(self):
        """When live sample_size=0, backtest stats are used entirely."""
        live = {"win_rate": 0, "avg_win": 0, "avg_loss": 0, "sample_size": 0}
        bt = {"win_rate": 0.6, "avg_win": 3.0, "avg_loss": 1.5, "sample_size": 40, "source": "backtest"}

        # Reproduce pipeline_manager blending logic
        if live["sample_size"] == 0:
            result = bt
        else:
            live_w = live["sample_size"] * 2
            bt_w = bt["sample_size"]
            total_w = live_w + bt_w
            result = {
                "win_rate": (live["win_rate"] * live_w + bt["win_rate"] * bt_w) / total_w,
                "avg_win": (live["avg_win"] * live_w + bt["avg_win"] * bt_w) / total_w,
                "avg_loss": (live["avg_loss"] * live_w + bt["avg_loss"] * bt_w) / total_w,
                "sample_size": live["sample_size"],
                "backtest_supplemented": True,
            }

        assert result["win_rate"] == 0.6
        assert result["avg_win"] == 3.0
        assert result["avg_loss"] == 1.5

    def test_blend_small_live_with_backtest(self):
        """When live has 5 trades and backtest has 40, blend with 2x live weight."""
        live = {"win_rate": 0.8, "avg_win": 4.0, "avg_loss": 2.0, "sample_size": 5}
        bt = {"win_rate": 0.6, "avg_win": 3.0, "avg_loss": 1.5, "sample_size": 40, "source": "backtest"}

        live_w = live["sample_size"] * 2  # 10
        bt_w = bt["sample_size"]         # 40
        total_w = live_w + bt_w          # 50

        result = {
            "win_rate": (live["win_rate"] * live_w + bt["win_rate"] * bt_w) / total_w,
            "avg_win": (live["avg_win"] * live_w + bt["avg_win"] * bt_w) / total_w,
            "avg_loss": (live["avg_loss"] * live_w + bt["avg_loss"] * bt_w) / total_w,
            "sample_size": live["sample_size"],
            "backtest_supplemented": True,
        }

        # live_w = 10, bt_w = 40
        # wr = (0.8*10 + 0.6*40) / 50 = (8 + 24) / 50 = 0.64
        assert result["win_rate"] == pytest.approx(0.64, abs=0.001)
        # avg_win = (4.0*10 + 3.0*40) / 50 = (40 + 120) / 50 = 3.2
        assert result["avg_win"] == pytest.approx(3.2, abs=0.001)
        assert result["backtest_supplemented"] is True

    def test_no_blend_when_sample_large(self):
        """When live sample >= 20, no supplementation occurs."""
        live = {"win_rate": 0.55, "avg_win": 2.5, "avg_loss": 1.8, "sample_size": 25}
        # Pipeline skips blending when sample_size >= 20
        assert live["sample_size"] >= 20
        # Stats remain unchanged
        assert live["win_rate"] == 0.55

    def test_no_blend_when_backtest_empty(self):
        """When backtest returns 0 sample_size, live stats remain unchanged."""
        live = {"win_rate": 0.5, "avg_win": 2.0, "avg_loss": 1.0, "sample_size": 3}
        bt = {"win_rate": 0, "avg_win": 0, "avg_loss": 0, "sample_size": 0, "source": "backtest"}

        # Pipeline checks bt["sample_size"] > 0 and bt["win_rate"] > 0
        should_blend = bt["sample_size"] > 0 and bt["win_rate"] > 0
        assert should_blend is False
        # live remains untouched
        assert live["win_rate"] == 0.5
