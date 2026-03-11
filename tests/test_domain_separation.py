"""
Tests for domain separation — ensuring Crypto (coinbase) and Equity (ibkr)
never leak data into each other's counters, Redis keys, or news pipelines.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest

from src.core.rules import AbsoluteRules


# ═══════════════════════════════════════════════════════════════════════════
# AbsoluteRules — exchange-scoped daily counter seeding
# ═══════════════════════════════════════════════════════════════════════════


def _make_rules(exchange: str = "") -> AbsoluteRules:
    cfg = {
        "max_single_trade": 500,
        "max_daily_spend": 2000,
        "max_daily_loss": 300,
        "max_trades_per_day": 20,
    }
    return AbsoluteRules(cfg, exchange=exchange)


class TestRulesExchangeFilter:
    """seed_daily_counters must filter SQL by exchange."""

    def test_no_exchange_omits_filter(self):
        rules = _make_rules(exchange="")
        assert rules.exchange == ""

    def test_exchange_stored_on_init(self):
        rules = _make_rules(exchange="coinbase")
        assert rules.exchange == "coinbase"

    @patch("src.core.rules.psycopg2")
    @patch("src.utils.stats.get_dsn", return_value="postgresql://test")
    def test_seed_adds_exchange_filter_for_coinbase(self, _dsn, mock_pg):
        """SQL must include AND (exchange = %s OR exchange = %s)."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        # Return plausible rows for the three queries
        mock_cur.fetchone.side_effect = [
            {"cnt": 3, "spend": 150.0},
            {"spend": 100.0},
            {"loss": 25.0},
        ]
        mock_conn.cursor.return_value = mock_cur
        mock_pg.connect.return_value = mock_conn
        mock_pg.extras = MagicMock()

        rules = _make_rules(exchange="coinbase")
        rules.seed_daily_counters()

        # All three SQL calls must contain the exchange filter
        assert mock_cur.execute.call_count == 3
        for c in mock_cur.execute.call_args_list:
            sql = c[0][0]
            params = c[0][1]
            assert "exchange = %s" in sql, f"Missing exchange filter in: {sql}"
            assert "coinbase" in params
            assert "coinbase_paper" in params

    @patch("src.core.rules.psycopg2")
    @patch("src.utils.stats.get_dsn", return_value="postgresql://test")
    def test_seed_no_exchange_filter_when_empty(self, _dsn, mock_pg):
        """When exchange is empty, SQL must NOT filter by exchange."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.side_effect = [
            {"cnt": 5, "spend": 200.0},
            {"spend": 150.0},
            {"loss": 50.0},
        ]
        mock_conn.cursor.return_value = mock_cur
        mock_pg.connect.return_value = mock_conn
        mock_pg.extras = MagicMock()

        rules = _make_rules(exchange="")
        rules.seed_daily_counters()

        for c in mock_cur.execute.call_args_list:
            sql = c[0][0]
            assert "exchange" not in sql, f"Unexpected exchange filter in: {sql}"

    @patch("src.core.rules.psycopg2")
    @patch("src.utils.stats.get_dsn", return_value="postgresql://test")
    def test_seed_ibkr_uses_ibkr_paper_variant(self, _dsn, mock_pg):
        """IBKR exchange must match both 'ibkr' and 'ibkr_paper'."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.side_effect = [
            {"cnt": 1, "spend": 50.0},
            {"spend": 50.0},
            {"loss": 10.0},
        ]
        mock_conn.cursor.return_value = mock_cur
        mock_pg.connect.return_value = mock_conn
        mock_pg.extras = MagicMock()

        rules = _make_rules(exchange="ibkr")
        rules.seed_daily_counters()

        for c in mock_cur.execute.call_args_list:
            params = c[0][1]
            assert "ibkr" in params
            assert "ibkr_paper" in params


# ═══════════════════════════════════════════════════════════════════════════
# DashboardCommandManager — profile-scoped Redis keys
# ═══════════════════════════════════════════════════════════════════════════


class TestDashboardCommandsRedisKeys:
    """Redis keys must include the exchange profile prefix."""

    def _make_manager(self, exchange: str = "coinbase"):
        mock_orch = MagicMock()
        mock_orch.config = {"trading": {"exchange": exchange}}
        mock_orch.redis = MagicMock()
        mock_orch.trailing_stops.get_all_stops.return_value = {}

        from src.core.managers.dashboard_commands import DashboardCommandManager
        return DashboardCommandManager(mock_orch)

    def test_profile_stored_from_config(self):
        mgr = self._make_manager("coinbase")
        assert mgr._profile == "coinbase"

    def test_profile_ibkr(self):
        mgr = self._make_manager("ibkr")
        assert mgr._profile == "ibkr"

    def test_trailing_stops_key_is_scoped(self):
        mgr = self._make_manager("coinbase")
        mgr.publish_trailing_stops()

        redis = mgr.orch.redis
        redis.set.assert_called_once()
        key = redis.set.call_args[0][0]
        assert key == "coinbase:trailing_stops:state"

    def test_trailing_stops_key_ibkr_scoped(self):
        mgr = self._make_manager("ibkr")
        mgr.publish_trailing_stops()

        redis = mgr.orch.redis
        redis.set.assert_called_once()
        key = redis.set.call_args[0][0]
        assert key == "ibkr:trailing_stops:state"


# ═══════════════════════════════════════════════════════════════════════════
# News pipeline — profile-scoped Redis key reads
# ═══════════════════════════════════════════════════════════════════════════


class TestNewsPipelineScoping:
    """News reads must prefer profile-scoped Redis keys."""

    def test_aggregator_get_latest_prefers_profile_key(self):
        """get_latest() should try news:{profile}:latest before news:latest."""
        import threading
        mock_redis = MagicMock()
        mock_redis.get.return_value = json.dumps([{"title": "BTC up", "source": "test", "url": "https://test.com", "published": None}])

        from src.news.aggregator import NewsAggregator
        agg = NewsAggregator.__new__(NewsAggregator)
        agg.redis = mock_redis
        agg.profile = "coinbase"
        agg._sources = []
        agg._poll_interval = 300
        agg._last_poll = 0
        agg.articles = []  # empty so it falls through to Redis
        agg._lock = threading.Lock()

        result = agg.get_latest()
        # Should have tried the profile-scoped key
        calls = [c[0][0] for c in mock_redis.get.call_args_list]
        assert any("coinbase" in k for k in calls), f"Expected profile-scoped key, got: {calls}"


# ═══════════════════════════════════════════════════════════════════════════
# Frontend — React Query cache keys MUST include `profile`
# ═══════════════════════════════════════════════════════════════════════════

import re
from pathlib import Path

# Pages whose useQuery calls are profile-independent (system settings, auth, etc.)
_PROFILE_EXEMPT_FILES = {"Settings.tsx", "LLMProviders.tsx"}

# Individual queryKey prefixes that are genuinely profile-independent
_PROFILE_EXEMPT_KEYS = {
    "settings",
    "presets",
    "style-modifiers",
    "events",             # system logs
    "llm-providers",
    "openrouter-credits",
    "auth-status",
    "setup-config",
}

# Regex to extract queryKey arrays from useQuery({ queryKey: [...] })
_QUERY_KEY_RE = re.compile(
    r"queryKey:\s*\[([^\]]+)\]",
    re.MULTILINE,
)


class TestFrontendQueryKeysIncludeProfile:
    """Static analysis: every useQuery in dashboard pages must include
    `profile` in its queryKey to prevent cross-domain cache leaks.

    If this test fails after a code change, the fix is:
      1. Add `const profile = useLiveStore((s) => s.profile)` in the component.
      2. Append `profile` to the queryKey array.
      3. Update any `invalidateQueries` calls to match.
    """

    PAGES_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "frontend" / "src" / "pages"

    def _collect_violations(self):
        """Return list of (file, line_no, queryKey_text) violations."""
        violations = []
        if not self.PAGES_DIR.exists():
            pytest.skip("Frontend pages directory not found")

        for tsx_file in sorted(self.PAGES_DIR.rglob("*.tsx")):
            if tsx_file.name in _PROFILE_EXEMPT_FILES:
                continue

            content = tsx_file.read_text(encoding="utf-8", errors="replace")
            for match in _QUERY_KEY_RE.finditer(content):
                key_body = match.group(1)
                # Extract the first string literal as the key prefix
                first_str = re.search(r"['\"]([^'\"]+)['\"]", key_body)
                if first_str and first_str.group(1) in _PROFILE_EXEMPT_KEYS:
                    continue

                if "profile" not in key_body:
                    line_no = content[:match.start()].count("\n") + 1
                    violations.append((tsx_file.name, line_no, f"[{key_body}]"))

        return violations

    def test_all_query_keys_contain_profile(self):
        """Every profile-dependent useQuery must include `profile` in queryKey."""
        violations = self._collect_violations()
        if violations:
            msg_lines = [
                "useQuery queryKey missing `profile` — will cause cross-domain cache bleed:",
            ]
            for fname, line, key in violations:
                msg_lines.append(f"  {fname}:{line}  queryKey: {key}")
            msg_lines.append(
                "\nFix: add `profile` to each queryKey array and ensure "
                "`useLiveStore((s) => s.profile)` is called in the component."
            )
            pytest.fail("\n".join(msg_lines))


# ═══════════════════════════════════════════════════════════════════════════
# Dashboard DB methods — exchange column filtering in SQL
# ═══════════════════════════════════════════════════════════════════════════


class TestSimulatedMixinExchangeFilter:
    """SimulatedMixin DB methods must include exchange filtering when provided."""

    def _make_mixin(self):
        """Build a SimulatedMixin with a mocked connection."""
        from src.utils.stats_simulated import SimulatedMixin

        mixin = SimulatedMixin.__new__(SimulatedMixin)

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_cursor.fetchone.return_value = None
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)

        mixin._get_conn = MagicMock(return_value=mock_conn)
        return mixin, mock_conn

    # --- get_simulated_trades ------------------------------------------------

    def test_simulated_trades_with_exchange_filters_sql(self):
        mixin, conn = self._make_mixin()
        mixin.get_simulated_trades(exchange="coinbase")

        sql = conn.execute.call_args[0][0]
        params = conn.execute.call_args[0][1]
        assert "exchange = %s" in sql
        assert "coinbase" in params

    def test_simulated_trades_without_exchange_no_filter(self):
        mixin, conn = self._make_mixin()
        mixin.get_simulated_trades()

        sql = conn.execute.call_args[0][0]
        assert "exchange" not in sql

    # --- get_latest_scan_results ---------------------------------------------

    def test_scan_results_with_exchange_filters_sql(self):
        mixin, conn = self._make_mixin()
        mixin.get_latest_scan_results(exchange="ibkr")

        sql = conn.execute.call_args[0][0]
        params = conn.execute.call_args[0][1]
        assert "exchange = %s" in sql
        assert "ibkr" in params

    def test_scan_results_without_exchange_no_filter(self):
        mixin, conn = self._make_mixin()
        mixin.get_latest_scan_results()

        sql = conn.execute.call_args[0][0]
        assert "exchange" not in sql

    # --- get_pair_follows ----------------------------------------------------

    def test_pair_follows_with_exchange_filters_sql(self):
        mixin, conn = self._make_mixin()
        mixin.get_pair_follows(exchange="coinbase")

        sql = conn.execute.call_args[0][0]
        params = conn.execute.call_args[0][1]
        assert "exchange = %s" in sql
        assert "coinbase" in params

    def test_pair_follows_without_exchange_no_filter(self):
        mixin, conn = self._make_mixin()
        mixin.get_pair_follows()

        sql = conn.execute.call_args[0][0]
        assert "exchange = %s" not in sql

    # --- get_followed_pairs_set ----------------------------------------------

    def test_followed_pairs_set_with_exchange_filters_sql(self):
        mixin, conn = self._make_mixin()
        mixin.get_followed_pairs_set(exchange="ibkr")

        sql = conn.execute.call_args[0][0]
        params = conn.execute.call_args[0][1]
        assert "exchange = %s" in sql
        assert "ibkr" in params

    def test_followed_pairs_set_without_exchange_no_filter(self):
        mixin, conn = self._make_mixin()
        mixin.get_followed_pairs_set()

        sql = conn.execute.call_args[0][0]
        assert "exchange = %s" not in sql


# ═══════════════════════════════════════════════════════════════════════════
# Dashboard routes — static analysis: every DB call must pass exchange
# ═══════════════════════════════════════════════════════════════════════════

import ast
import textwrap

# Methods that MUST have exchange= when called from dashboard routes
_EXCHANGE_REQUIRED_METHODS = {
    "get_simulated_trades",
    "get_latest_scan_results",
    "get_pair_follows",
    "get_followed_pairs_set",
}

_ROUTES_DIR = Path(__file__).resolve().parent.parent / "src" / "dashboard" / "routes"


class TestDashboardRoutesPassExchange:
    """Static analysis: every call to exchange-filterable DB methods in
    dashboard route files must pass the `exchange` keyword argument.

    If this test fails, the fix is:
      1. Add `resolved = deps.resolve_profile(profile)` in the route handler.
      2. Pass `exchange=resolved or None` to the DB method call.
    """

    def _collect_violations(self) -> list[tuple[str, int, str]]:
        violations: list[tuple[str, int, str]] = []
        if not _ROUTES_DIR.exists():
            pytest.skip("Dashboard routes directory not found")

        for py_file in sorted(_ROUTES_DIR.glob("*.py")):
            source = py_file.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                # Match db.get_xxx() or self.get_xxx() style calls
                func_name = ""
                if isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                if func_name not in _EXCHANGE_REQUIRED_METHODS:
                    continue
                # Check if exchange= is passed as a keyword
                kw_names = {kw.arg for kw in node.keywords if kw.arg is not None}
                if "exchange" not in kw_names:
                    violations.append((py_file.name, node.lineno, func_name))

        return violations

    def test_all_db_calls_pass_exchange(self):
        violations = self._collect_violations()
        if violations:
            msg_lines = [
                "Dashboard route DB calls missing `exchange=` — will cause cross-domain data bleed:",
            ]
            for fname, line, method in violations:
                msg_lines.append(f"  {fname}:{line}  {method}() missing exchange=")
            msg_lines.append(
                "\nFix: pass `exchange=resolved or None` where "
                "`resolved = deps.resolve_profile(profile)`."
            )
            pytest.fail("\n".join(msg_lines))
