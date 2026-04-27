"""
Temporal Activities for planning workflows.

Activities are the side-effectful units of work in Temporal -- they run
outside the workflow sandbox, can access DBs and external APIs, and are
retried automatically on failure.

All activities read their own DB/LLM connections so that the Temporal
worker process is self-contained (no shared singletons from the main bot).

LLM calls now go through the unified LLMClient + LLMTracer stack, so planning
decisions appear in the same Langfuse project as the trading-cycle agents.
"""

from __future__ import annotations

import json
import os
import threading
import yaml
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Any

import psycopg2
import psycopg2.extras

from temporalio import activity

from src.core.llm_client import LLMClient
from src.core.llm_providers import build_providers
from src.utils.logger import get_logger
from src.utils.stats import get_dsn

logger = get_logger("planning.activities")

_OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
_PLANNING_MODEL = os.environ.get("PLANNING_MODEL", os.environ.get("OLLAMA_MODEL", "llama3.1:8b"))

# NOTE: Langfuse credentials are intentionally NOT read at module-import time.
# The planning worker imports this module before dotenv is loaded, so reading
# them here would always produce empty strings.  _get_planning_tracer() reads
# them lazily at call time instead.

# Module-level singletons -- created once per Temporal worker process
_llm_client: LLMClient | None = None
_llm_client_lock = threading.Lock()


def _get_llm_client() -> LLMClient:
    """Thread-safe lazy-initialised LLMClient for use inside activities.

    Uses the full provider chain from settings.yaml (OpenRouter -> Gemini -> Ollama)
    so planning decisions benefit from cloud models instead of Ollama-only.
    """
    global _llm_client
    if _llm_client is None:
        with _llm_client_lock:
            # Double-checked locking
            if _llm_client is None:
                # Load provider config from settings.yaml
                providers_config: list[dict] = []
                try:
                    # MED-7: use a path relative to this file so it works
                    # regardless of CWD (Docker, systemd, tests, etc.).
                    _here = os.path.dirname(os.path.abspath(__file__))
                    settings_path = os.path.normpath(
                        os.path.join(_here, "..", "..", "config", "settings.yaml")
                    )
                    with open(settings_path, "r", encoding="utf-8") as f:
                        cfg = yaml.safe_load(f) or {}
                    providers_config = cfg.get("llm", {}).get("providers", [])
                except Exception as e:
                    logger.warning(f"Could not load settings.yaml for LLM providers: {e}")

                providers = build_providers(
                    providers_config,
                    fallback_base_url=_OLLAMA_BASE,
                    fallback_model=_PLANNING_MODEL,
                    fallback_timeout=60,
                    fallback_max_retries=1,
                ) if providers_config else None

                _llm_client = LLMClient(
                    base_url=_OLLAMA_BASE,
                    model=_PLANNING_MODEL,
                    temperature=0.3,
                    max_tokens=2000,
                    providers=providers,
                )
    return _llm_client


def _get_planning_tracer():
    """Return a module-level LLMTracer for the planning worker, or None."""
    try:
        from src.utils.tracer import LLMTracer
        # Read credentials lazily so dotenv has been loaded by the time we get here.
        langfuse_host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
        langfuse_pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        langfuse_sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
        if LLMTracer._instance is None:
            if not langfuse_pk or not langfuse_sk:
                return None
            # Build a Redis client so planning spans stream to the live dashboard
            redis_client = None
            redis_url = os.environ.get("REDIS_URL", "")
            if redis_url:
                try:
                    import redis as _redis
                    redis_client = _redis.Redis.from_url(redis_url)
                    redis_client.ping()
                except Exception as e:
                    logger.warning(f"Planning tracer: Redis unavailable ({e}), streaming disabled")
                    redis_client = None
            LLMTracer.init(
                public_key=langfuse_pk,
                secret_key=langfuse_sk,
                host=langfuse_host,
                redis_client=redis_client,
                enabled=True,
            )
        return LLMTracer._instance
    except Exception:
        return None


# --- Domain helpers -----------------------------------------------------------

def _detect_domain(profile: str) -> str:
    """Return 'equity' or 'crypto' by reading the profile's trading.exchange setting."""
    try:
        config_paths: list[str] = []
        if profile:
            # Sanitise: profile names must be alphanumeric + hyphen/underscore only.
            # Prevents path traversal (e.g. "../secrets") since profile comes from
            # Temporal workflow args which are operator-set but still untrusted input.
            safe_profile = "".join(c for c in profile if c.isalnum() or c in "-_")
            if safe_profile:
                config_paths.append(os.path.join("config", f"{safe_profile}.yaml"))
        from src.utils.settings_manager import get_settings_path
        config_paths.append(get_settings_path())
        for cfg_path in config_paths:
            if os.path.exists(cfg_path):
                with open(cfg_path) as f:
                    cfg = yaml.safe_load(f) or {}
                exchange = cfg.get("trading", {}).get("exchange", "coinbase")
                return "equity" if exchange == "ibkr" else "crypto"
    except Exception:
        pass
    return "crypto"


def _get_watchlist_tickers(profile: str) -> list[str]:
    """Return Yahoo Finance tickers for the current watchlist from profile config."""
    try:
        config_paths: list[str] = []
        if profile:
            safe_profile = "".join(c for c in profile if c.isalnum() or c in "-_")
            if safe_profile:
                config_paths.append(os.path.join("config", f"{safe_profile}.yaml"))
        from src.utils.settings_manager import get_settings_path
        config_paths.append(get_settings_path())
        for cfg_path in config_paths:
            if os.path.exists(cfg_path):
                with open(cfg_path) as f:
                    cfg = yaml.safe_load(f) or {}
                pairs = cfg.get("trading", {}).get("pairs", [])
                from src.core.equity_feed import pair_to_yahoo
                return [pair_to_yahoo(p) for p in pairs if p]
    except Exception:
        pass
    return []


# --- DB helpers ---------------------------------------------------------------

@contextmanager
def _get_conn():
    """Yield a psycopg2 connection using the shared DATABASE_URL.

    All profiles now share a single PostgreSQL database; exchange-level
    filtering is done via the ``exchange`` column in each table.
    """
    conn = psycopg2.connect(get_dsn())
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def _dict_row(cursor):
    """Convert a psycopg2 cursor result to list of dicts using RealDictCursor."""
    if cursor.description is None:
        return []
    columns = [col.name for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _execute(conn, sql, params=None):
    """Execute SQL and return a helper object mimicking sqlite3 cursor interface."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    return cur


# --- Activities ---------------------------------------------------------------

@activity.defn
async def fetch_backtest_summary(profile: str = "") -> dict:
    """Fetch recent backtest results per pair for planning context.

    Returns a dict with top-performing and worst-performing pairs based on
    the most recent backtest run per pair, plus aggregate stats.
    Only considers runs from the last 30 days with at least 1 trade.
    """
    _exch_frag = ""
    _exch_params: tuple = ()
    if profile:
        _resolved = profile.lower()
        if _resolved in ("crypto",):
            _resolved = "coinbase"
        _exch_frag = " AND (exchange = %s OR exchange = %s)"
        _exch_params = (_resolved, f"{_resolved}_paper")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    with _get_conn() as conn:
        # Check if backtest_runs table exists
        cur = conn.cursor()
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'backtest_runs')"
        )
        if not cur.fetchone()[0]:
            return {"available": False, "pairs": []}

        # Get the most recent run per pair (with trades > 0)
        rows = _execute(conn,
            f"""SELECT DISTINCT ON (pair)
                    pair, days, total_return_pct, sharpe_ratio, win_rate,
                    total_trades, max_drawdown_pct, alpha, run_ts
                FROM backtest_runs
                WHERE run_ts >= %s AND total_trades > 0{_exch_frag}
                ORDER BY pair, run_ts DESC""",
            (cutoff, *_exch_params),
        ).fetchall()

        if not rows:
            return {"available": True, "pairs": []}

        pairs = [dict(r) for r in rows]
        pairs.sort(key=lambda r: r.get("sharpe_ratio") or 0, reverse=True)

        return {
            "available": True,
            "pairs": pairs,
            "top_performers": [
                p["pair"] for p in pairs[:5]
                if (p.get("sharpe_ratio") or 0) > 0 and (p.get("total_return_pct") or 0) > 0
            ],
            "worst_performers": [
                p["pair"] for p in pairs
                if (p.get("sharpe_ratio") or 0) < 0 or (p.get("total_return_pct") or 0) < -1
            ][:5],
            "avg_sharpe": sum(p.get("sharpe_ratio") or 0 for p in pairs) / len(pairs),
            "avg_win_rate": sum(p.get("win_rate") or 0 for p in pairs) / len(pairs),
        }


@activity.defn
async def fetch_trade_history(days: int = 7, pair: str | None = None, profile: str = "") -> list[dict]:
    """Fetch closed trade history from StatsDB for the planning LLM."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    # Build exchange filter to prevent cross-domain bleed
    _exch_frag = ""
    _exch_params: tuple = ()
    if profile:
        _resolved = profile.lower()
        if _resolved in ("crypto",):
            _resolved = "coinbase"
        _exch_frag = " AND (exchange = %s OR exchange = %s)"
        _exch_params = (_resolved, f"{_resolved}_paper")
    with _get_conn() as conn:
        if pair:
            cur = _execute(conn,
                f"""SELECT ts, pair, action, price, quote_amount, confidence,
                          signal_type, pnl, fee_quote, reasoning
                   FROM trades WHERE ts >= %s AND pair = %s AND pnl IS NOT NULL{_exch_frag}
                   ORDER BY ts DESC LIMIT 500""",
                (cutoff, pair, *_exch_params),
            )
        else:
            cur = _execute(conn,
                f"""SELECT ts, pair, action, price, quote_amount, confidence,
                          signal_type, pnl, fee_quote, reasoning
                   FROM trades WHERE ts >= %s AND pnl IS NOT NULL{_exch_frag}
                   ORDER BY ts DESC LIMIT 500""",
                (cutoff, *_exch_params),
            )
        return [dict(r) for r in cur.fetchall()]


@activity.defn
async def fetch_portfolio_history(days: int = 7, profile: str = "") -> dict:
    """Fetch portfolio performance summary for the planning LLM."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    # Build exchange filter to prevent cross-domain bleed
    _exch_frag = ""
    _exch_params: tuple = ()
    if profile:
        _resolved = profile.lower()
        if _resolved in ("crypto",):
            _resolved = "coinbase"
        _exch_frag = " AND (exchange = %s OR exchange = %s)"
        _exch_params = (_resolved, f"{_resolved}_paper")

    with _get_conn() as conn:
        trade_stats = _execute(conn,
            f"""SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing,
                COALESCE(SUM(pnl), 0) as total_pnl,
                COALESCE(AVG(pnl), 0) as avg_pnl,
                COALESCE(MAX(pnl), 0) as best_pnl,
                COALESCE(MIN(pnl), 0) as worst_pnl,
                COALESCE(AVG(confidence), 0) as avg_confidence,
                COALESCE(SUM(quote_amount), 0) as total_volume,
                COALESCE(SUM(fee_quote), 0) as total_fees
               FROM trades WHERE ts >= %s AND pnl IS NOT NULL{_exch_frag}""",
            (cutoff, *_exch_params),
        ).fetchone()

        pair_breakdown = _execute(conn,
            f"""SELECT pair,
                COUNT(*) as trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                COALESCE(SUM(pnl), 0) as pnl,
                COALESCE(AVG(confidence), 0) as avg_confidence
               FROM trades WHERE ts >= %s AND pnl IS NOT NULL{_exch_frag}
               GROUP BY pair ORDER BY pnl DESC""",
            (cutoff, *_exch_params),
        ).fetchall()

        _snap_frag = ""
        _snap_params: tuple = (cutoff,)
        if profile:
            _snap_resolved = profile.lower()
            if _snap_resolved in ("crypto",):
                _snap_resolved = "coinbase"
            _snap_frag = " AND exchange = %s"
            _snap_params = (cutoff, _snap_resolved)
        portfolio_range = _execute(conn,
            f"""SELECT MIN(portfolio_value) as low, MAX(portfolio_value) as high,
                      AVG(portfolio_value) as avg
               FROM portfolio_snapshots WHERE ts >= %s{_snap_frag}""",
            _snap_params,
        ).fetchone()

        # Fetch reasoning traces to understand what drove losses
        _t_exch_frag = _exch_frag.replace("exchange", "t.exchange") if _exch_frag else ""
        reasoning_sample = _execute(conn,
            f"""SELECT ar.ts, ar.pair, ar.signal_type, ar.confidence,
                      ar.reasoning_json, t.action, t.pnl
               FROM agent_reasoning ar
               LEFT JOIN trades t ON t.id = ar.trade_id
               WHERE ar.ts >= %s AND ar.agent_name = 'market_analyst'
                 AND t.pnl IS NOT NULL{_t_exch_frag}
               ORDER BY t.pnl ASC LIMIT 20""",
            (cutoff, *_exch_params),
        ).fetchall()

    # Detect native currency from profile-specific config (or fallback to settings)
    currency_symbol = "$"
    native_currency = "USD"
    try:
        # Try profile-specific config first
        config_paths = []
        if profile:
            _safe = "".join(c for c in profile if c.isalnum() or c in "-_")
            if _safe:
                config_paths.append(os.path.join("config", f"{_safe}.yaml"))
        from src.utils.settings_manager import get_settings_path
        config_paths.append(get_settings_path())

        for cfg_path in config_paths:
            if os.path.exists(cfg_path):
                with open(cfg_path) as f:
                    _cfg = yaml.safe_load(f) or {}
                pairs = _cfg.get("trading", {}).get("pairs", [])
                _known_fiat = {"USD", "EUR", "GBP", "CAD", "AUD", "CHF", "JPY"}
                _currency_symbols = {"EUR": "\u20ac", "GBP": "\u00a3", "CHF": "CHF ", "USD": "$", "CAD": "C$", "AUD": "A$", "JPY": "\u00a5"}
                for pair in pairs:
                    if "-" in pair:
                        _, quote = pair.rsplit("-", 1)
                        if quote in _known_fiat:
                            native_currency = quote
                            currency_symbol = _currency_symbols.get(quote, quote + " ")
                            break
                if native_currency != "USD":
                    break  # found a match
    except Exception:
        pass  # fallback to USD/$

    return {
        "trade_stats": dict(trade_stats) if trade_stats else {},
        "pair_breakdown": [dict(r) for r in pair_breakdown],
        "portfolio_range": dict(portfolio_range) if portfolio_range else {},
        "reasoning_sample": [dict(r) for r in reasoning_sample],
        "currency_symbol": currency_symbol,
        "native_currency": native_currency,
    }


@activity.defn
async def call_planning_llm(horizon: str, review_data: dict) -> dict:
    """
    Call the LLM to produce a structured strategic plan from review data.
    Uses the unified LLMClient + LLMTracer so calls appear in Langfuse
    alongside trading-cycle agent spans.

    horizon: "daily" | "weekly" | "monthly"
    review_data: output from fetch_portfolio_history / fetch_trade_history.
                 May include "domain" ("crypto"|"equity") and "equity_events" dict.
    """
    domain = review_data.get("domain", "crypto")

    # ── System prompts, split by domain ───────────────────────────────────
    _CRYPTO_PROMPTS = {
        "daily": """You are the strategic planning module for an automated crypto trading bot.
Review yesterday's and last week's trading performance and produce a clear daily plan.

Respond ONLY with JSON:
{
    "regime": "bullish" | "bearish" | "neutral" | "volatile",
    "confidence": 0.0-1.0,
    "preferred_pairs": ["BTC-USD", ...],
    "avoid_pairs": [...],
    "risk_posture": "aggressive" | "normal" | "conservative",
    "key_observations": ["obs1", "obs2"],
    "today_focus": "One sentence on the main trading focus for today",
    "summary": "2-3 sentence plain-English summary of the daily plan"
}""",

        "weekly": """You are the strategic planning module for an automated crypto trading bot.
Review the last month of trading performance and produce a weekly strategy.

Respond ONLY with JSON:
{
    "market_regime": "bull_market" | "bear_market" | "sideways" | "volatile",
    "regime_confidence": 0.0-1.0,
    "strategy_adjustments": ["adjustment1", "adjustment2", ...],
    "pairs_to_focus": ["BTC-USD", ...],
    "pairs_to_reduce": [...],
    "risk_posture": "aggressive" | "normal" | "conservative",
    "pattern_observations": ["pattern1", ...],
    "loss_analysis": "What drove the losses and what to change",
    "weekly_targets": {"win_rate_target": 0.0, "max_drawdown_tolerance": 0.0},
    "pair_outlooks": {
        "BTC-USD": {"direction": "bullish", "expected_move_pct": 8.0, "confidence": 0.70}
    },
    "summary": "3-4 sentence weekly strategic plan"
}

For pair_outlooks: include up to 5 pairs with meaningful conviction.
expected_move_pct is the expected % move over 7 days. Only include pairs with confidence >= 0.60.""",

        "monthly": """You are the strategic planning module for an automated crypto trading bot.
Review the last 90 days and YTD performance to produce a monthly portfolio strategy.

Respond ONLY with JSON:
{
    "macro_regime": "risk_on" | "risk_off" | "transitional",
    "crypto_cycle_phase": "accumulation" | "markup" | "distribution" | "markdown" | "unknown",
    "portfolio_allocation_targets": {"BTC-USD": 0.0, "ETH-USD": 0.0, "cash": 0.0},
    "max_single_position_pct": 0.0,
    "risk_tolerance": "high" | "medium" | "low",
    "strategic_themes": ["theme1", "theme2", ...],
    "performance_assessment": "Assessment of last 90 days",
    "goal_progress": "Progress toward portfolio growth targets",
    "summary": "4-5 sentence monthly strategic plan"
}""",
    }

    _EQUITY_PROMPTS = {
        "daily": """You are the strategic planning module for an automated equity trading bot (IBKR, EU large-caps).
Review yesterday's and last week's trading performance and produce a clear daily plan.
Consider upcoming earnings releases, ex-dividend dates, and macro events (ECB, FOMC) in your reasoning.
Pre-earnings: stocks typically drift toward expected surprise then gap on the report day — reduce size into prints.
Ex-dividend: price drops by approximately the dividend amount on ex-div date — avoid buying the day before.

Respond ONLY with JSON:
{
    "regime": "bullish" | "bearish" | "neutral" | "volatile",
    "confidence": 0.0-1.0,
    "preferred_pairs": ["ASML.AS-EUR", ...],
    "avoid_pairs": [...],
    "risk_posture": "aggressive" | "normal" | "conservative",
    "event_risk_today": "Any earnings/ex-div/macro events hitting today or tomorrow worth flagging",
    "key_observations": ["obs1", "obs2"],
    "today_focus": "One sentence on the main trading focus for today",
    "summary": "2-3 sentence plain-English summary of the daily plan"
}""",

        "weekly": """You are the strategic planning module for an automated equity trading bot (IBKR, EU large-caps).
Review the last month of trading performance and produce a weekly strategy.
Account for upcoming earnings releases, ex-dividend dates, ECB/FOMC decisions, and sector rotation.
EU large-caps typically drift toward analyst consensus in the 2 weeks pre-earnings, then gap significantly
on the report day — reduce or close positions before earnings to avoid uncontrolled gap risk.

Respond ONLY with JSON:
{
    "market_regime": "bull_market" | "bear_market" | "sideways" | "volatile",
    "regime_confidence": 0.0-1.0,
    "strategy_adjustments": ["adjustment1", "adjustment2", ...],
    "pairs_to_focus": ["ASML.AS-EUR", ...],
    "pairs_to_reduce": [...],
    "risk_posture": "aggressive" | "normal" | "conservative",
    "earnings_season_posture": "pre_season_accumulate" | "in_season_cautious" | "post_season_momentum" | "between_seasons_normal",
    "upcoming_earnings_risk": ["ASML.AS-EUR", "..."],
    "sector_rotation_theme": "e.g. defensives over growth given ECB hawkishness",
    "pattern_observations": ["pattern1", ...],
    "loss_analysis": "What drove the losses and what to change",
    "weekly_targets": {"win_rate_target": 0.0, "max_drawdown_tolerance": 0.0},
    "pair_outlooks": {
        "ASML.AS-EUR": {"direction": "bullish", "expected_move_pct": 3.0, "confidence": 0.70}
    },
    "summary": "3-4 sentence weekly strategic plan"
}

For pair_outlooks: EU large-caps typically move 2-8% on earnings, 0.5-2% on normal weeks.
expected_move_pct is the expected % move over 7 days. Only include pairs with confidence >= 0.60.""",

        "monthly": """You are the strategic planning module for an automated equity trading bot (IBKR, EU large-caps).
Review the last 90 days and YTD performance to produce a monthly portfolio strategy.
Consider macroeconomic regime (ECB policy, EUR/USD, European growth), quarterly earnings seasons,
sector rotation patterns, and calendar effects (January effect, Q4 rebalancing, summer liquidity thin-out).

Respond ONLY with JSON:
{
    "macro_regime": "risk_on" | "risk_off" | "transitional",
    "ecb_stance": "dovish" | "neutral" | "hawkish",
    "earnings_season_phase": "pre_season" | "active_season" | "post_season" | "between_seasons",
    "sector_rotation": "e.g. rotating into defensives, value over growth",
    "seasonal_theme": "e.g. Q1 earnings season starting, position for beat/miss patterns",
    "portfolio_allocation_targets": {"ASML.AS-EUR": 0.0, "SAP.DE-EUR": 0.0, "cash": 0.0},
    "max_single_position_pct": 0.0,
    "risk_tolerance": "high" | "medium" | "low",
    "strategic_themes": ["theme1", "theme2", ...],
    "performance_assessment": "Assessment of last 90 days",
    "goal_progress": "Progress toward portfolio growth targets",
    "summary": "4-5 sentence monthly strategic plan"
}""",
    }

    prompts = _EQUITY_PROMPTS if domain == "equity" else _CRYPTO_PROMPTS
    system_prompt = prompts.get(horizon, prompts["daily"])

    # ── Format backward-looking performance data ───────────────────────────
    stats = review_data.get("trade_stats", {})
    pairs = review_data.get("pair_breakdown", [])
    port = review_data.get("portfolio_range", {})

    total = stats.get("total_trades") or 0
    wins = stats.get("winning") or 0
    win_rate = (wins / total * 100) if total > 0 else 0

    sym = review_data.get("currency_symbol") or "$"
    native = review_data.get("native_currency") or "USD"

    pairs_text = "\n".join(
        f"  {p['pair']}: {p.get('trades', 0)} trades, {p.get('wins', 0)} wins, PnL {sym}{(p.get('pnl') or 0):.2f}"
        for p in pairs[:10]
    ) or "  No data"

    reasoning_sample = review_data.get("reasoning_sample", [])
    loss_reasoning = []
    for r in reasoning_sample[:5]:
        if r.get("pnl") and r["pnl"] < 0:
            try:
                rj = json.loads(r.get("reasoning_json") or "{}")
                factors = rj.get("key_factors", [])[:2]
                loss_reasoning.append(
                    f"  {r['pair']} LOSS {sym}{r['pnl']:.2f}: sig={r.get('signal_type', '?')}"
                    f" conf={(r.get('confidence') or 0):.0%} factors={factors}"
                )
            except Exception:
                pass

    loss_text = "\n".join(loss_reasoning) or "  No recent losses with reasoning."

    correction_note = ""
    if review_data.get("portfolio_correction_applied"):
        exchange_name = "IBKR" if domain == "equity" else "Coinbase"
        correction_note = (
            f"\nIMPORTANT: The bot's portfolio tracking was recently corrected to reflect "
            f"actual live {exchange_name} holdings. Earlier data may reflect stale or incorrect "
            f"portfolio assumptions. Weight recent data and current holdings more heavily.\n"
        )

    eval_data = review_data.get("previous_plan_evaluation", {})
    if eval_data.get("has_previous_plan") and eval_data.get("accuracy_score") is not None:
        eval_block = (
            f"\nPREVIOUS PLAN EVALUATION:\n"
            f"  {eval_data.get('summary', '')}\n"
            f"  Accuracy: {eval_data['accuracy_score']:.0%}\n"
            f"  Component scores: {', '.join(f'{k}={v:.0%}' for k, v in eval_data.get('component_scores', {}).items())}\n"
            + (f"  Preferred pairs PnL: {eval_data.get('actual_pair_pnl', {})}\n" if eval_data.get("actual_pair_pnl") else "")
            + "\nUse this evaluation to improve your current plan. If accuracy was low,\n"
              "adjust your approach. If certain pair predictions were wrong, reconsider."
        )
    elif eval_data.get("has_previous_plan"):
        eval_block = f"\nPREVIOUS PLAN EVALUATION:\n  {eval_data.get('summary', 'No trades since last plan.')}"
    else:
        eval_block = ""

    # ── Equity-only: forward-looking events block ──────────────────────────
    equity_events_block = ""
    if domain == "equity":
        ev = review_data.get("equity_events", {})
        earnings: dict = ev.get("earnings", {})
        dividends: dict = ev.get("dividends", {})
        macro: list = ev.get("macro", [])
        season: dict = ev.get("earnings_season", {})

        lines: list[str] = []

        if season and season.get("season_label"):
            phase = season.get("phase", "")
            if phase == "active":
                lines.append(f"  \u26a0\ufe0f  {season['season_label']} ACTIVE \u2014 heightened gap risk on individual names")
            elif phase == "pre_season":
                lines.append(f"  \U0001f4c5 {season['season_label']} starts in ~{season.get('days_to_peak', '?')} days \u2014 consider pre-positioning")
            if season.get("notes"):
                lines.append(f"     {season['notes']}")

        if earnings:
            lines.append("  UPCOMING EARNINGS:")
            for ticker, info in sorted(earnings.items(), key=lambda x: x[1].get("days_away", 99)):
                days = info["days_away"]
                date = info["earnings_date"]
                eps = f", est. EPS {info['eps_estimate']:.2f}" if info.get("eps_estimate") is not None else ""
                prefix = "\u26a0\ufe0f  " if days <= 7 else "   "
                lines.append(f"    {prefix}{ticker}: {date} ({days}d away{eps})")

        if dividends:
            lines.append("  UPCOMING EX-DIVIDEND DATES:")
            for ticker, info in sorted(dividends.items(), key=lambda x: x[1].get("days_away", 99)):
                days = info["days_away"]
                date = info["ex_div_date"]
                div = f", div {sym}{info['annual_dividend']:.2f}/yr" if info.get("annual_dividend") is not None else ""
                yld = f" ({info['yield_pct']:.1f}% yield)" if info.get("yield_pct") is not None else ""
                lines.append(f"    {ticker}: {date} ({days}d away{div}{yld})")

        if macro:
            lines.append("  MACRO EVENTS (ECB/FOMC):")
            for evt in macro[:5]:
                days = evt["days_away"]
                timing = f"{abs(days)}d ago" if days < 0 else ("TODAY" if days == 0 else f"in {days}d")
                lines.append(f"    {evt['event']} \u2014 {evt['date']} ({timing})")

        if lines:
            equity_events_block = "\nFORWARD-LOOKING EVENTS (next 60 days):\n" + "\n".join(lines) + "\n"

    # ── Backtest insights block ────────────────────────────────────────────
    backtest_block = ""
    bt_data = review_data.get("backtest_summary", {})
    if bt_data.get("available") and bt_data.get("pairs"):
        bt_pairs = bt_data["pairs"]
        bt_lines = []
        for p in bt_pairs[:10]:
            bt_lines.append(
                f"  {p['pair']}: return={p.get('total_return_pct', 0):+.2f}% "
                f"sharpe={p.get('sharpe_ratio', 0):.2f} "
                f"win_rate={p.get('win_rate', 0):.0f}% "
                f"trades={p.get('total_trades', 0)} "
                f"max_dd={p.get('max_drawdown_pct', 0):.1f}% "
                f"alpha={p.get('alpha', 0):+.2f}% "
                f"({p.get('days', '?')}d test)"
            )
        bt_text = "\n".join(bt_lines)
        top = bt_data.get("top_performers", [])
        worst = bt_data.get("worst_performers", [])
        backtest_block = (
            f"\nBACKTEST INSIGHTS (recent simulations on historical data):\n"
            f"  Avg Sharpe: {bt_data.get('avg_sharpe', 0):.2f} | "
            f"Avg Win Rate: {bt_data.get('avg_win_rate', 0):.0f}%\n"
            + (f"  Top performers: {', '.join(top)}\n" if top else "")
            + (f"  Underperformers: {', '.join(worst)}\n" if worst else "")
            + f"\n  Per-pair results:\n{bt_text}\n"
            + "\nUse backtest data to validate pair selections and risk posture. "
            "Pairs with negative alpha or low Sharpe may warrant avoidance or smaller sizing.\n"
        )

    # ── Score divergence block ─────────────────────────────────────────────
    score_div_block = ""
    sd_data = review_data.get("score_divergence", {})
    if sd_data.get("available") and sd_data.get("sample_size", 0) >= 5:
        div = sd_data.get("divergence", {})
        score_div_block = (
            f"\nENTRY SCORE DIVERGENCE (live trades vs backtest threshold):\n"
            f"  Sample: {sd_data['sample_size']} buy trades (last 30 days)\n"
            f"  Avg entry score: {sd_data.get('avg_entry_score', 0):.3f} | "
            f"Backtest threshold: {sd_data.get('backtest_threshold', 0.4)}\n"
            f"  Above threshold: {sd_data.get('above_threshold_count', 0)} trades "
            f"(avg PnL: {div.get('above_threshold_avg_pnl', 0):+.4f})\n"
            f"  Below threshold: {sd_data.get('below_threshold_count', 0)} trades "
            f"({sd_data.get('below_threshold_pct', 0):.0f}%) "
            f"(avg PnL: {div.get('below_threshold_avg_pnl', 0):+.4f})\n"
            f"  LLM confidence vs score gap: {div.get('score_confidence_gap', 0):.3f}\n"
            f"\nIf below-threshold trades have worse PnL, consider tightening "
            "entry criteria or raising min_confidence.\n"
        )

    user_message = (
        f"PERFORMANCE REVIEW ({horizon.upper()} | domain={domain.upper()})\n"
        f"{correction_note}{eval_block}\n"
        f"TRADE STATISTICS:\n"
        f"  Total trades: {total}\n"
        f"  Win rate: {win_rate:.1f}%  ({wins} wins / {stats.get('losing') or 0} losses)\n"
        f"  Total PnL: {sym}{stats.get('total_pnl') or 0:.2f}\n"
        f"  Avg PnL per trade: {sym}{stats.get('avg_pnl') or 0:.2f}\n"
        f"  Best trade: {sym}{stats.get('best_pnl') or 0:.2f}\n"
        f"  Worst trade: {sym}{stats.get('worst_pnl') or 0:.2f}\n"
        f"  Avg confidence: {(stats.get('avg_confidence') or 0):.0%}\n"
        f"  Total volume: {sym}{stats.get('total_volume') or 0:,.2f}\n"
        f"  Total fees: {sym}{stats.get('total_fees') or 0:.2f}\n"
        f"\nPAIR BREAKDOWN:\n{pairs_text}\n"
        f"\nPORTFOLIO VALUE RANGE ({native}):\n"
        f"  Low: {sym}{port.get('low') or 0:,.2f}\n"
        f"  High: {sym}{port.get('high') or 0:,.2f}\n"
        f"  Avg: {sym}{port.get('avg') or 0:,.2f}\n"
        f"\nRECENT LOSS ANALYSIS (signal reasoning that led to losses):\n{loss_text}\n"
        f"{equity_events_block}"
        f"{backtest_block}"
        f"{score_div_block}"
        f"\nGenerate the {horizon} plan as JSON."
    )

    # ── Langfuse tracing ───────────────────────────────────────────────────
    trace_id = f"planning-{horizon}-{uuid.uuid4().hex[:8]}"
    trace_ctx = None
    span = None
    try:
        tracer = _get_planning_tracer()
        if tracer:
            trace_ctx = tracer.start_trace(
                cycle_id=trace_id,
                pair="planning",
                metadata={"horizon": horizon, "domain": domain},
                exchange="ibkr" if domain == "equity" else "coinbase",
            )
        if trace_ctx is not None:
            span = trace_ctx.start_span(
                f"planning_{horizon}",
                input_data={"system": system_prompt[:500], "user": user_message[:500]},
                model=_PLANNING_MODEL,
            )
    except Exception as e:
        logger.warning(f"Planning trace setup failed (non-fatal): {e}")
        trace_ctx = None
        span = None

    llm = _get_llm_client()
    try:
        result = await llm.chat_json(
            system_prompt=system_prompt,
            user_message=user_message,
            span=span,
            agent_name=f"planning_{horizon}",
        )
        logger.info(
            f"Planning LLM response for {horizon} ({domain}): "
            f"regime={result.get('regime', result.get('market_regime', result.get('macro_regime', '?')))}"
        )
        result["_langfuse_trace_id"] = trace_ctx.trace_id if trace_ctx else trace_id
        if trace_ctx:
            trace_ctx.finish(metadata={"horizon": horizon, "domain": domain, "regime": result.get("regime")})
        return result
    except Exception as e:
        logger.error(f"Planning LLM call failed: {e}")
        if trace_ctx:
            trace_ctx.finish(metadata={"error": str(e)})
        return {
            "error": str(e),
            "summary": f"Planning LLM unavailable -- defaulting to neutral {horizon} posture.",
            "regime": "neutral",
            "_langfuse_trace_id": trace_ctx.trace_id if trace_ctx else trace_id,
        }


@activity.defn
async def evaluate_previous_plan(horizon: str, profile: str = "") -> dict:
    """
    Evaluate how well the previous plan's predictions matched reality.

    Reads the latest strategic_context for *horizon*, compares predicted
    regime / pair preferences / risk posture against actual trade outcomes
    since that plan was written, and returns an accuracy breakdown that
    the next planning LLM call can learn from.
    """
    with _get_conn() as conn:
        row = _execute(conn,
            """SELECT * FROM strategic_context WHERE horizon = %s
               ORDER BY ts DESC LIMIT 1""",
            (horizon,),
        ).fetchone()

        if not row:
            return {"has_previous_plan": False, "summary": "No previous plan to evaluate."}

        prev_plan: dict = json.loads(row["plan_json"])
        plan_ts: str = row["ts"]

        # Actual trade outcomes since (and including) the plan timestamp
        _exch_frag = ""
        _exch_params: tuple = ()
        if profile:
            _resolved = profile.lower()
            if _resolved in ("crypto",):
                _resolved = "coinbase"
            _exch_frag = " AND (exchange = %s OR exchange = %s)"
            _exch_params = (_resolved, f"{_resolved}_paper")
        trades = _execute(conn,
            f"""SELECT pair, action, pnl, confidence, signal_type
               FROM trades WHERE ts >= %s AND pnl IS NOT NULL{_exch_frag}
               ORDER BY ts DESC LIMIT 500""",
            (plan_ts, *_exch_params),
        ).fetchall()
        trades = [dict(t) for t in trades]

    if not trades:
        return {
            "has_previous_plan": True,
            "plan_ts": plan_ts,
            "horizon": horizon,
            "previous_plan_summary": prev_plan.get("summary", ""),
            "trades_since": 0,
            "accuracy_score": None,
            "summary": f"No trades executed since last {horizon} plan -- cannot evaluate.",
        }

    # -- aggregate actuals --
    total_trades = len(trades)
    winning = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
    total_pnl = sum(t.get("pnl") or 0 for t in trades)
    win_rate = winning / total_trades if total_trades else 0

    pair_pnl: dict[str, float] = {}
    for t in trades:
        p = t.get("pair", "UNKNOWN")
        pair_pnl[p] = pair_pnl.get(p, 0) + (t.get("pnl") or 0)

    scores: dict[str, float] = {}
    abs_total = max(abs(total_pnl), 0.01)

    # 1. Pair-selection accuracy
    preferred = prev_plan.get("preferred_pairs", prev_plan.get("pairs_to_focus", []))
    avoid = prev_plan.get("avoid_pairs", prev_plan.get("pairs_to_reduce", []))

    if preferred:
        preferred_pnl = sum(pair_pnl.get(p, 0) for p in preferred)
        other_pnl = sum(v for k, v in pair_pnl.items() if k not in preferred)
        if preferred_pnl > 0 and preferred_pnl >= other_pnl:
            scores["pair_selection"] = min(1.0, 0.5 + preferred_pnl / abs_total * 0.5)
        elif preferred_pnl > 0:
            scores["pair_selection"] = 0.5
        else:
            scores["pair_selection"] = max(0.0, 0.5 - abs(preferred_pnl) / abs_total * 0.5)

    if avoid:
        avoid_pnl = sum(pair_pnl.get(p, 0) for p in avoid)
        scores["avoid_accuracy"] = 1.0 if avoid_pnl <= 0 else max(0.0, 0.5 - avoid_pnl / abs_total)

    # 2. Risk-posture alignment
    risk_posture = prev_plan.get("risk_posture", "normal")
    if risk_posture == "conservative":
        scores["risk_posture"] = 0.7 if total_pnl >= 0 else 0.3
    elif risk_posture == "aggressive":
        scores["risk_posture"] = 1.0 if total_pnl > 0 else 0.2
    else:
        scores["risk_posture"] = 0.5 + (0.3 if total_pnl > 0 else -0.1)

    # 3. Regime prediction
    regime = prev_plan.get(
        "regime",
        prev_plan.get("market_regime", prev_plan.get("macro_regime", "neutral")),
    )
    bull_signal = regime in ("bullish", "bull_market", "risk_on")
    bear_signal = regime in ("bearish", "bear_market", "risk_off")

    if bull_signal and total_pnl > 0:
        scores["regime_prediction"] = min(1.0, 0.6 + win_rate * 0.4)
    elif bear_signal and total_pnl < 0:
        scores["regime_prediction"] = 0.6
    elif bull_signal and total_pnl < 0:
        scores["regime_prediction"] = 0.2
    elif bear_signal and total_pnl > 0:
        scores["regime_prediction"] = 0.3
    else:
        scores["regime_prediction"] = 0.5

    # 4. Equity earnings discipline
    # If the plan listed upcoming_earnings_risk pairs, check whether new BUY
    # trades were placed on those pairs (which would be undisciplined).
    # Handle both list (current schema) and legacy string values.
    upcoming_risk_raw = prev_plan.get("upcoming_earnings_risk")
    if isinstance(upcoming_risk_raw, list):
        risk_pairs = set(upcoming_risk_raw)
    elif isinstance(upcoming_risk_raw, str) and upcoming_risk_raw:
        # Comma-separated string fallback (old plans)
        risk_pairs = {p.strip() for p in upcoming_risk_raw.split(",") if p.strip()}
    else:
        risk_pairs = set()
    if risk_pairs:
        buys_on_risky = [
            t for t in trades
            if t.get("pair") in risk_pairs and t.get("action") == "buy"
        ]
        if not buys_on_risky:
            scores["earnings_discipline"] = 1.0   # respected guidance — no buys near earnings
        elif len(buys_on_risky) == 1:
            scores["earnings_discipline"] = 0.5   # partial restraint
        else:
            scores["earnings_discipline"] = 0.2   # repeatedly ignored earnings risk warnings

    accuracy = sum(scores.values()) / len(scores) if scores else 0.5

    evaluation = {
        "has_previous_plan": True,
        "plan_ts": plan_ts,
        "horizon": horizon,
        "previous_plan_summary": prev_plan.get("summary", ""),
        "predicted_regime": regime,
        "predicted_risk_posture": risk_posture,
        "predicted_preferred_pairs": preferred,
        "predicted_avoid_pairs": avoid,
        "actual_trades": total_trades,
        "actual_win_rate": round(win_rate, 3),
        "actual_total_pnl": round(total_pnl, 4),
        "actual_pair_pnl": {k: round(v, 4) for k, v in pair_pnl.items()},
        "component_scores": {k: round(v, 3) for k, v in scores.items()},
        "accuracy_score": round(accuracy, 3),
        "summary": (
            f"Previous {horizon} plan accuracy: {accuracy:.0%}. "
            f"Predicted {regime} regime with {risk_posture} posture. "
            f"Actual: {total_trades} trades, {win_rate:.0%} win rate, PnL {total_pnl:.2f}. "
            f"Component scores: {', '.join(f'{k}={v:.0%}' for k, v in scores.items())}."
        ),
    }

    logger.info(f"Plan evaluation ({horizon}): accuracy={accuracy:.2f}, trades={total_trades}")
    return evaluation


@activity.defn
async def write_strategic_context(
    horizon: str,
    plan_json: dict,
    summary_text: str = "",
    temporal_workflow_id: str = "",
    temporal_run_id: str = "",
    profile: str = "",
) -> int:
    """Persist a planning workflow result to StatsDB."""
    from src.utils.stats import StatsDB
    db = StatsDB()
    # MED-8: work on a copy so we don't mutate the Temporal-serialised input.
    plan_json = dict(plan_json)
    langfuse_trace_id = plan_json.pop("_langfuse_trace_id", None)
    row_id = db.save_strategic_context(
        horizon=horizon,
        plan_json=plan_json,
        summary_text=summary_text or plan_json.get("summary", ""),
        langfuse_trace_id=langfuse_trace_id,
        temporal_workflow_id=temporal_workflow_id or None,
        temporal_run_id=temporal_run_id or None,
    )
    logger.info(f"Wrote {horizon} strategic context (id={row_id}): {summary_text[:80]}")
    return row_id


@activity.defn
async def write_daily_plan(date: str, plan_text: str, profile: str = "") -> None:
    """Write the daily plan text into daily_summaries."""
    from src.utils.stats import StatsDB
    db = StatsDB()
    db.write_daily_plan(date=date, plan_text=plan_text)
    logger.info(f"Wrote daily plan for {date}: {plan_text[:80]}")


@activity.defn
async def fetch_pair_universe(profile: str = "") -> dict:
    """Fetch the current pair universe size and product summary.

    Routes to the correct data source based on the profile domain:
      - crypto (Coinbase): queries the Coinbase product catalog
      - equity (IBKR):     queries the equity universe via equity_feed
    """
    domain = _detect_domain(profile)

    if domain == "equity":
        try:
            from src.core.equity_feed import discover_pairs_detailed
            products = discover_pairs_detailed(exchange_id="ibkr")
            by_quote: dict[str, int] = {}
            for p in products:
                pid = p.get("product_id", "")
                q = pid.split("-")[-1] if "-" in pid else "?"
                by_quote[q] = by_quote.get(q, 0) + 1
            top_by_volume = sorted(
                products, key=lambda p: float(p.get("volume_24h", 0) or 0), reverse=True
            )[:20]
            return {
                "domain": "equity",
                "universe_size": len(products),
                "by_quote_currency": by_quote,
                "top_20_by_volume": [
                    {
                        "product_id": p["product_id"],
                        "volume_24h": float(p.get("volume_24h", 0) or 0),
                        "price_change_24h": float(p.get("price_percentage_change_24h", 0) or 0),
                    }
                    for p in top_by_volume
                ],
            }
        except Exception as e:
            logger.warning(f"fetch_pair_universe (equity) failed: {e}")
            return {"domain": "equity", "universe_size": 0, "error": str(e)}

    # crypto / Coinbase path
    try:
        from src.core.coinbase_client import CoinbaseClient
        # Read-only universe scan; paper_mode flag is irrelevant for
        # the public product-listing endpoints used here.
        coinbase = CoinbaseClient(paper_mode=True)
        products = coinbase.discover_all_pairs_detailed(include_crypto_quotes=True)
        by_quote = {}
        for p in products:
            q = p.get("quote_currency", "?")
            by_quote[q] = by_quote.get(q, 0) + 1
        top_by_volume = sorted(
            products, key=lambda p: float(p.get("volume_24h", 0) or 0), reverse=True
        )[:20]
        return {
            "domain": "crypto",
            "universe_size": len(products),
            "by_quote_currency": by_quote,
            "top_20_by_volume": [
                {
                    "product_id": p["product_id"],
                    "volume_24h": float(p.get("volume_24h", 0) or 0),
                    "price_change_24h": float(p.get("price_percentage_change_24h", 0) or 0),
                }
                for p in top_by_volume
            ],
        }
    except Exception as e:
        logger.warning(f"fetch_pair_universe (crypto) failed: {e}")
        return {"domain": "crypto", "universe_size": 0, "error": str(e)}


@activity.defn
async def fetch_universe_scan_summary(profile: str = "") -> dict:
    """Fetch the latest universe scan results from StatsDB."""
    try:
        from src.utils.stats import StatsDB
        db = StatsDB()
        scan = db.get_latest_scan_results(exchange=profile or None)
        if scan:
            return {
                "universe_size": scan.get("universe_size", 0),
                "scanned_pairs": scan.get("scanned_pairs", 0),
                "top_movers": scan.get("top_movers", ""),
                "summary_text": scan.get("summary_text", ""),
                "ts": scan.get("ts", ""),
            }
        return {"summary_text": "No scan results available yet."}
    except Exception as e:
        logger.warning(f"fetch_universe_scan_summary activity failed: {e}")
        return {"summary_text": f"Error: {e}"}


@activity.defn
async def fetch_equity_events(profile: str = "") -> dict:
    """Fetch earnings dates, ex-dividend dates, and macro events for the equity watchlist.

    Returns a structured dict that ``call_planning_llm`` injects into the LLM context.
    Returns ``{"domain": "crypto"}`` immediately (no external calls) for crypto profiles
    so the workflow code can call this unconditionally without branching.
    """
    domain = _detect_domain(profile)
    if domain != "equity":
        return {"domain": "crypto", "earnings": {}, "dividends": {}, "macro": [], "earnings_season": {}}

    tickers = _get_watchlist_tickers(profile)
    if not tickers:
        logger.warning("fetch_equity_events: no tickers found in profile config")
        return {"domain": "equity", "earnings": {}, "dividends": {}, "macro": [], "earnings_season": {}}

    try:
        from src.core.equity_feed import (
            get_earnings_calendar,
            get_dividend_calendar,
            get_macro_calendar,
            get_earnings_season_context,
        )
        earnings = get_earnings_calendar(tickers, days_ahead=60)
        dividends = get_dividend_calendar(tickers, days_ahead=60)
        macro = get_macro_calendar(days_ahead=60)
        earnings_season = get_earnings_season_context()

        logger.info(
            f"📅 Equity events: {len(earnings)} earnings, {len(dividends)} ex-div, "
            f"{len(macro)} macro events for {len(tickers)} tickers"
        )
        return {
            "domain": "equity",
            "tickers": tickers,
            "earnings": earnings,
            "dividends": dividends,
            "macro": macro,
            "earnings_season": earnings_season,
        }
    except Exception as e:
        logger.warning(f"fetch_equity_events failed: {e}")
        return {"domain": "equity", "earnings": {}, "dividends": {}, "macro": [], "earnings_season": {}}


# ---------------------------------------------------------------------------
# Nightly backtest activity
# ---------------------------------------------------------------------------

@activity.defn
async def run_nightly_backtests(profile: str = "") -> dict:
    """Run 30-day backtests on the top-N followed pairs and save results.

    Called by NightlyBacktestWorkflow at 2 AM UTC.  Results are stored in
    backtest_runs so that fetch_backtest_summary surfaces them to the daily
    planning prompt the next morning.
    """
    import asyncio as _asyncio

    domain = _detect_domain(profile)
    is_equity = domain == "equity"
    _resolved = (profile or "coinbase").lower()
    if _resolved == "crypto":
        _resolved = "coinbase"

    # Load pairs from config
    pairs: list[str] = []
    try:
        config_paths: list[str] = []
        if profile:
            safe_profile = "".join(c for c in profile if c.isalnum() or c in "-_")
            if safe_profile:
                config_paths.append(os.path.join("config", f"{safe_profile}.yaml"))
        from src.utils.settings_manager import get_settings_path
        config_paths.append(get_settings_path())
        for cfg_path in config_paths:
            if os.path.exists(cfg_path):
                with open(cfg_path) as f:
                    cfg = yaml.safe_load(f) or {}
                pairs = cfg.get("trading", {}).get("pairs", [])
                break
    except Exception as e:
        logger.warning(f"run_nightly_backtests: failed to load config pairs: {e}")

    if not pairs:
        logger.info("run_nightly_backtests: no pairs configured, skipping")
        return {"ran": 0, "saved": 0, "pairs": []}

    # Cap at 10 pairs to limit runtime
    pairs = pairs[:10]

    # Load full config for BacktestEngine
    full_config: dict = {}
    try:
        _here = os.path.dirname(os.path.abspath(__file__))
        settings_path = os.path.normpath(
            os.path.join(_here, "..", "..", "config", "settings.yaml")
        )
        with open(settings_path, "r", encoding="utf-8") as f:
            full_config = yaml.safe_load(f) or {}
    except Exception:
        pass

    from src.backtesting.engine import BacktestEngine
    from src.backtesting.candle_fetch import fetch_candles

    results_saved = 0
    pair_results: list[dict] = []

    for pair in pairs:
        try:
            candles = fetch_candles(pair, 30, is_equity)
            if len(candles) < 100:
                logger.info(f"Nightly BT: skipping {pair} — only {len(candles)} candles")
                continue

            engine = BacktestEngine(full_config)
            result = engine.run(candles, pair=pair)

            # Save to backtest_runs
            params_dict = {"source": "nightly_auto", "days": 30}
            result_dict = {
                "total_return_pct": result.total_return_pct,
                "sharpe_ratio": result.sharpe_ratio,
                "win_rate": result.win_rate,
                "total_trades": result.total_trades,
                "max_drawdown_pct": result.max_drawdown_pct,
                "alpha": result.alpha,
                "sortino_ratio": result.sortino_ratio,
            }

            with _get_conn() as conn:
                _execute(conn,
                    """INSERT INTO backtest_runs
                        (pair, exchange, days, params_json, result_json,
                         total_return_pct, sharpe_ratio, win_rate,
                         total_trades, max_drawdown_pct, alpha)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        pair, _resolved, 30,
                        json.dumps(params_dict),
                        json.dumps(result_dict, default=str),
                        result.total_return_pct,
                        result.sharpe_ratio,
                        result.win_rate,
                        result.total_trades,
                        result.max_drawdown_pct,
                        result.alpha,
                    ),
                )
            results_saved += 1
            pair_results.append({
                "pair": pair,
                "sharpe": round(result.sharpe_ratio, 3),
                "return_pct": round(result.total_return_pct, 2),
                "trades": result.total_trades,
            })
            logger.info(
                f"📊 Nightly BT: {pair} — Sharpe={result.sharpe_ratio:.2f}, "
                f"Return={result.total_return_pct:.1f}%, Trades={result.total_trades}"
            )

        except Exception as e:
            logger.warning(f"Nightly BT failed for {pair}: {e}")

    logger.info(f"🌙 Nightly backtests complete: {results_saved}/{len(pairs)} saved")

    # ── Recommendations queue ─────────────────────────────────────────────
    # Surface noteworthy results as pending recommendations so the operator
    # has an actionable inbox instead of just dashboard telemetry. Heuristic
    # is intentionally conservative: high Sharpe + sufficient sample size on
    # a pair that is *not* currently in the active rotation.
    try:
        from src.utils.stats import StatsDB
        db = StatsDB()
        active_pairs: set[str] = set()
        for _ex in (full_config.get("trading", {}).get("pairs") or []):
            active_pairs.add(str(_ex))
        recs_made = 0
        for pr in pair_results:
            sharpe = float(pr.get("sharpe", 0) or 0)
            ret = float(pr.get("return_pct", 0) or 0)
            trades = int(pr.get("trades", 0) or 0)
            sym = str(pr.get("pair") or "")
            if not sym:
                continue
            # Only suggest pairs that aren't already actively traded.
            if sym in active_pairs:
                continue
            if sharpe >= 1.5 and trades >= 5 and ret > 0:
                db.upsert_recommendation(
                    exchange=_resolved,
                    kind="add_pair",
                    symbol=sym,
                    summary=(
                        f"Add {sym} to active rotation "
                        f"(Sharpe {sharpe:.2f}, return {ret:+.1f}%, {trades} trades)"
                    ),
                    rationale=(
                        "30-day nightly backtest produced a Sharpe ≥ 1.5 with a "
                        "positive return on a pair that is not in the current "
                        "active rotation. Operator review required before any "
                        "config change is applied."
                    ),
                    payload={
                        "pair": sym,
                        "sharpe": sharpe,
                        "return_pct": ret,
                        "trades": trades,
                        "window_days": 30,
                    },
                    metric_name="sharpe_30d",
                    metric_value=sharpe,
                    source="nightly_backtest",
                )
                recs_made += 1
        if recs_made:
            logger.info(f"🌙 Nightly BT: surfaced {recs_made} recommendations")
    except Exception as _rec_e:  # pragma: no cover — best-effort
        logger.debug(f"Nightly BT recommendation surfacing failed: {_rec_e}")

    return {"ran": len(pairs), "saved": results_saved, "pairs": pair_results}


# ---------------------------------------------------------------------------
# Score divergence activity
# ---------------------------------------------------------------------------

@activity.defn
async def fetch_score_divergence(profile: str = "") -> dict:
    """Compare live trade entry_scores vs backtest entry threshold.

    Surfaces divergence between how the deterministic scoring engine rated
    actual live entries vs what the backtest engine's threshold would accept.
    High divergence suggests the LLM is overriding the scoring engine too
    aggressively (taking low-quality entries or skipping high-quality ones).
    """
    _exch_frag = ""
    _exch_params: tuple = ()
    if profile:
        _resolved = profile.lower()
        if _resolved == "crypto":
            _resolved = "coinbase"
        _exch_frag = " AND (exchange = %s OR exchange = %s)"
        _exch_params = (_resolved, f"{_resolved}_paper")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    with _get_conn() as conn:
        # Check if entry_score column exists
        cur = conn.cursor()
        cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_name = 'trades' AND column_name = 'entry_score'"""
        )
        if not cur.fetchone():
            return {"available": False, "reason": "entry_score column not yet migrated"}

        # Fetch trades with entry_score
        rows = _execute(conn,
            f"""SELECT pair, action, confidence, entry_score, pnl
                FROM trades
                WHERE ts >= %s AND entry_score IS NOT NULL
                      AND action = 'buy'{_exch_frag}
                ORDER BY ts DESC LIMIT 200""",
            (cutoff, *_exch_params),
        ).fetchall()

        if not rows:
            return {"available": True, "sample_size": 0, "divergence": {}}

        trades = [dict(r) for r in rows]
        scores = [t["entry_score"] for t in trades if t["entry_score"] is not None]
        confidences = [t["confidence"] for t in trades if t["confidence"] is not None]

        if not scores:
            return {"available": True, "sample_size": 0, "divergence": {}}

        avg_score = sum(scores) / len(scores)
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0

        # Backtest threshold is typically 0.4 (from engine._entry_threshold)
        bt_threshold = 0.4

        # How many trades had entry_score below backtest threshold?
        below_threshold = sum(1 for s in scores if s < bt_threshold)
        above_threshold = sum(1 for s in scores if s >= bt_threshold)

        # PnL split: trades above vs below threshold
        above_pnl = [t["pnl"] for t in trades if (t.get("entry_score") or 0) >= bt_threshold and t.get("pnl") is not None]
        below_pnl = [t["pnl"] for t in trades if (t.get("entry_score") or 0) < bt_threshold and t.get("pnl") is not None]

        return {
            "available": True,
            "sample_size": len(scores),
            "avg_entry_score": round(avg_score, 3),
            "avg_llm_confidence": round(avg_confidence, 3),
            "backtest_threshold": bt_threshold,
            "below_threshold_count": below_threshold,
            "above_threshold_count": above_threshold,
            "below_threshold_pct": round(below_threshold / len(scores) * 100, 1),
            "divergence": {
                "score_confidence_gap": round(abs(avg_score - avg_confidence), 3),
                "above_threshold_avg_pnl": round(sum(above_pnl) / len(above_pnl), 4) if above_pnl else 0,
                "below_threshold_avg_pnl": round(sum(below_pnl) / len(below_pnl), 4) if below_pnl else 0,
            },
        }


# ---------------------------------------------------------------------------
# Event–price regression activity
# ---------------------------------------------------------------------------

@activity.defn
async def run_event_regressions(profile: str = "") -> dict:
    """Fit OLS forward-return regressions on every (symbol, event_type) pair.

    Reads ``catalyst_events`` and ``historical_candles`` from StatsDB,
    fits one model per (symbol, event_type, horizon) using
    ``EventRegressionEngine``, and writes results back to
    ``event_price_regressions`` for the dashboard. Domain-isolated by
    exchange. Called by EventRegressionWorkflow nightly.
    """
    from src.utils.stats import StatsDB
    from src.analysis.event_regression import (
        DEFAULT_HORIZONS_DAYS,
        run_event_regressions_for_profile,
    )

    domain = _detect_domain(profile)
    resolved = (profile or "coinbase").lower()
    if resolved == "crypto":
        resolved = "coinbase"
    exchange = "ibkr" if domain == "equity" else resolved

    db = StatsDB()
    try:
        results = run_event_regressions_for_profile(
            db=db,
            exchange=exchange,
            horizons=DEFAULT_HORIZONS_DAYS,
        )
    except Exception as e:
        logger.warning(f"run_event_regressions failed: {e}")
        return {"profile": profile, "exchange": exchange, "fitted": 0, "error": str(e)}

    ok = sum(1 for r in results if r.notes == "ok")
    return {
        "profile": profile,
        "exchange": exchange,
        "fitted": len(results),
        "ok": ok,
        "horizons": list(DEFAULT_HORIZONS_DAYS),
    }


# ---------------------------------------------------------------------------
# Nightly historical-OHLCV backfill activity
# ---------------------------------------------------------------------------

# Lookback window for the FIRST run of a symbol (no prior coverage).
# Trusted public sources support multi-decade daily candles, but pulling
# 10 years for a hundred symbols on every cron tick is wasteful — once the
# initial fill lands, ``bulk_backfill_symbol`` only walks the missing tail
# (the delta), so the second night's run is cheap regardless of this knob.
_PRICE_BACKFILL_DEFAULT_YEARS = 5
_PRICE_BACKFILL_MAX_YEARS = 10


def _price_backfill_years() -> int:
    """Initial-fill lookback in years. Env-overridable, hard-clipped to 10."""
    raw = os.environ.get("PRICE_BACKFILL_LOOKBACK_YEARS", "")
    try:
        v = int(raw) if raw else _PRICE_BACKFILL_DEFAULT_YEARS
    except ValueError:
        v = _PRICE_BACKFILL_DEFAULT_YEARS
    return max(1, min(v, _PRICE_BACKFILL_MAX_YEARS))


def _price_backfill_universe(profile: str, exchange: str) -> list[str]:
    """Return the union of yaml-configured pairs and currently-followed pairs.

    Mirrors the universe we already use for catalyst collection so the price
    history stays in lock-step with the symbols the agents actually see.
    """
    pairs: set[str] = set()
    # 1) yaml-configured pairs
    try:
        safe_profile = "".join(c for c in (profile or "") if c.isalnum() or c in "-_")
        if safe_profile:
            cfg_path = os.path.join("config", f"{safe_profile}.yaml")
            if os.path.isfile(cfg_path):
                with open(cfg_path) as f:
                    cfg = yaml.safe_load(f) or {}
                for p in (cfg.get("trading", {}) or {}).get("pairs", []) or []:
                    if isinstance(p, str) and p.strip():
                        pairs.add(p.strip())
    except Exception as e:
        logger.debug(f"price_backfill: yaml pair load failed for {profile}: {e}")
    # 2) followed pairs (orchestrator/agents have these in pair_follows)
    try:
        from src.utils.stats import StatsDB
        followed = StatsDB().get_followed_pairs_set(exchange=exchange) or set()
        pairs.update(followed)
    except Exception as e:
        logger.debug(f"price_backfill: followed-pair load failed for {exchange}: {e}")
    return sorted(pairs)


@activity.defn
async def run_price_backfill(profile: str = "") -> dict:
    """Nightly OHLCV history backfill for every symbol in the active profile.

    Strategy:
      * On a symbol's first run (no prior rows in ``historical_candles``)
        walk back ``PRICE_BACKFILL_LOOKBACK_YEARS`` (default 5y, capped 10y)
        on ONE_DAY granularity to seed the regression / pattern engines.
      * On subsequent runs ``bulk_backfill_symbol`` consults
        ``backfill_progress`` / ``get_candles_coverage`` and only fetches
        the missing tail (delta), so the activity is cheap after seeding.
      * Granularities: ``ONE_DAY`` (event-regression input) plus
        ``ONE_HOUR`` (analysis.technical) — same pair the bulk CLI uses.
      * Sources are profile-scoped via ``select_sources(profile)`` —
        equity profiles hit Yahoo Finance + Stooq; crypto profiles hit
        Coinbase + CryptoCompare + Binance.

    Domain isolation: all writes carry ``exchange`` explicitly so coinbase
    rows can never bleed into ibkr or vice versa.
    """
    from src.utils.stats import StatsDB
    from src.analysis.history_bulk_backfill import bulk_backfill_symbol

    domain = _detect_domain(profile)
    resolved = (profile or "coinbase").lower()
    if resolved == "crypto":
        resolved = "coinbase"
    exchange = "ibkr" if domain == "equity" else resolved

    universe = _price_backfill_universe(profile, exchange)
    if not universe:
        logger.info(
            f"price_backfill: no symbols to backfill for {exchange} "
            f"(profile={profile!r})"
        )
        return {
            "profile": profile,
            "exchange": exchange,
            "symbols": 0,
            "rows_written": 0,
            "skipped": True,
        }

    granularities = ("ONE_DAY", "ONE_HOUR")
    until = datetime.now(timezone.utc)
    since = until - timedelta(days=365 * _price_backfill_years())

    db = StatsDB()
    total_rows = 0
    per_symbol: dict[str, int] = {}
    errors: list[str] = []

    logger.info(
        f"price_backfill: {exchange} starting — symbols={len(universe)} "
        f"granularities={list(granularities)} since={since.date().isoformat()}"
    )

    for sym in universe:
        try:
            # Heartbeat so Temporal knows long-running fills are alive.
            try:
                activity.heartbeat({"symbol": sym, "rows_so_far": total_rows})
            except Exception:
                # heartbeat is best-effort — never fail the activity over it
                pass
            written = bulk_backfill_symbol(
                profile=resolved,
                symbol=sym,
                granularities=granularities,
                since=since,
                until=until,
                stats_db=db,
            )
            n = sum(int(v or 0) for v in (written or {}).values())
            per_symbol[sym] = n
            total_rows += n
        except Exception as e:
            err = f"{sym}: {e}"
            errors.append(err)
            logger.warning(f"price_backfill: {err}")

    logger.info(
        f"price_backfill: {exchange} complete — "
        f"symbols={len(per_symbol)} rows_written={total_rows} errors={len(errors)}"
    )

    return {
        "profile": profile,
        "exchange": exchange,
        "symbols": len(per_symbol),
        "rows_written": total_rows,
        "lookback_years": _price_backfill_years(),
        "granularities": list(granularities),
        "errors": errors[:10],  # cap returned payload
    }


# ---------------------------------------------------------------------------
# Fine-tuning export activity
# ---------------------------------------------------------------------------

@activity.defn
async def run_finetune_export(profile: str = "", window_days: int = 90) -> dict:
    """Curate trade reasoning into Ollama/OpenAI fine-tuning datasets.

    Wraps :py:meth:`src.utils.finetuning_pipeline.FinetuningPipeline.curate_and_export`
    so the monthly export runs on Temporal's schedule rather than the
    in-process LearningManager (which only ticks during a healthy cycle).

    The export itself is local-disk only; downstream model training is
    operator-owned. We notify Telegram when a new dataset lands.
    """
    from src.utils.stats import StatsDB
    from src.utils.finetuning_pipeline import FinetuningPipeline

    domain = _detect_domain(profile)
    resolved = (profile or "coinbase").lower()
    if resolved == "crypto":
        resolved = "coinbase"
    exchange = "ibkr" if domain == "equity" else resolved

    cfg: dict = {"trading": {"exchange": exchange}}
    db = StatsDB()
    try:
        pipe = FinetuningPipeline(db, cfg, audit=None)
        result = pipe.curate_and_export(window_days=int(window_days))
    except Exception as e:
        logger.warning(f"run_finetune_export failed: {e}")
        return {"profile": profile, "exchange": exchange, "skipped": True, "error": str(e)}

    out: dict = {"profile": profile, "exchange": exchange, **result}

    # Best-effort operator notification — never block the activity on it.
    try:
        if not result.get("skipped"):
            ollama_path = (result.get("exports") or {}).get("ollama", {}).get("path")
            n = result.get("total_examples") or 0
            wins = result.get("win_count") or 0
            losses = result.get("loss_count") or 0
            _notify_operator(
                exchange,
                f"📦 Fine-tuning dataset ready ({n} examples, "
                f"{wins} wins, {losses} losses).\n"
                f"`{ollama_path}`\n"
                f"Operator action: import into Ollama / training pipeline.",
            )
    except Exception as _e:  # pragma: no cover
        logger.debug(f"finetune notify skipped: {_e}")

    return out


def _notify_operator(exchange: str, message: str) -> None:
    """Send a Telegram message via the agent's outbound bot endpoint.

    Activities run in the planning worker process, which has no direct
    handle to the agent's TelegramBot. We post to the agent's local
    webhook (``/api/notify``) when available; otherwise we just log.
    """
    import os
    import urllib.request
    import urllib.error

    base = os.environ.get(
        f"AGENT_NOTIFY_URL_{exchange.upper()}",
        os.environ.get("AGENT_NOTIFY_URL", ""),
    )
    if not base:
        logger.info(f"[notify {exchange}] {message}")
        return
    try:
        req = urllib.request.Request(
            base,
            data=json.dumps({"message": message, "exchange": exchange}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5).read()
    except (urllib.error.URLError, OSError) as e:
        logger.info(f"[notify {exchange}] {message} (delivery skipped: {e})")


# ---------------------------------------------------------------------------
# Cross-asset analytics activities
# ---------------------------------------------------------------------------

@activity.defn
async def run_taxonomy_seed(profile: str = "") -> dict:
    """Seed/refresh the asset_taxonomy table for the active profile.

    Idempotent — re-running merges new tags into existing rows. Uses
    curated crypto ecosystems for crypto profiles and yfinance sector
    lookups for equity profiles. Operator overrides in
    ``config/{profile}.taxonomy.yaml`` always win.
    """
    from src.utils.stats import StatsDB
    from src.analysis.taxonomy_seeder import seed_taxonomy_for_profile

    domain = _detect_domain(profile)
    resolved = (profile or "coinbase").lower()
    if resolved == "crypto":
        resolved = "coinbase"
    exchange = "ibkr" if domain == "equity" else resolved

    universe = _price_backfill_universe(profile, exchange)
    if not universe:
        logger.info(f"taxonomy_seed: no symbols for {exchange} (profile={profile!r})")
        return {"profile": profile, "exchange": exchange, "symbols": 0, "skipped": True}

    db = StatsDB()
    try:
        written = seed_taxonomy_for_profile(profile or exchange, universe, stats_db=db)
    except Exception as e:
        logger.warning(f"taxonomy_seed failed: {e}")
        return {"profile": profile, "exchange": exchange, "error": str(e)}

    return {
        "profile": profile,
        "exchange": exchange,
        "symbols": len(universe),
        "rows_written": int(written),
    }


@activity.defn
async def run_correlation_matrix(profile: str = "") -> dict:
    """Compute the nightly pairwise correlation matrix + asset clusters.

    Reads ``ONE_DAY`` candles for the active profile's universe, computes
    pearson + spearman + lead-lag scores, then derives clusters via
    union-find on the |pearson| ≥ threshold edges. Both the correlation
    rows and the cluster snapshot are persisted (the cluster snapshot is
    *replaced* atomically per exchange).
    """
    from src.utils.stats import StatsDB
    from src.analysis.cross_asset import (
        compute_correlation_matrix,
        compute_clusters_from_correlations,
        DEFAULT_WINDOW_DAYS,
    )

    domain = _detect_domain(profile)
    resolved = (profile or "coinbase").lower()
    if resolved == "crypto":
        resolved = "coinbase"
    exchange = "ibkr" if domain == "equity" else resolved

    universe = _price_backfill_universe(profile, exchange)
    if len(universe) < 2:
        logger.info(
            f"correlation_matrix: <2 symbols for {exchange} "
            f"(profile={profile!r}) — skipping"
        )
        return {
            "profile": profile,
            "exchange": exchange,
            "symbols": len(universe),
            "skipped": True,
        }

    db = StatsDB()
    try:
        rows = compute_correlation_matrix(
            exchange=exchange,
            symbols=universe,
            stats_db=db,
            window_days=DEFAULT_WINDOW_DAYS,
        )
        # Heartbeat once between heavy phases.
        try:
            activity.heartbeat({"phase": "correlations_done", "pairs": len(rows)})
        except Exception:
            pass
        written_pairs = db.upsert_asset_correlations(exchange, rows) if rows else 0
        clusters = compute_clusters_from_correlations(rows)
        written_cluster_rows = db.replace_asset_clusters(exchange, clusters)
    except Exception as e:
        logger.warning(f"run_correlation_matrix failed: {e}")
        return {"profile": profile, "exchange": exchange, "error": str(e)}

    logger.info(
        f"correlation_matrix: {exchange} — "
        f"pairs={written_pairs} clusters={len(clusters)} "
        f"cluster_rows={written_cluster_rows}"
    )
    return {
        "profile": profile,
        "exchange": exchange,
        "symbols": len(universe),
        "pairs": int(written_pairs),
        "clusters": len(clusters),
    }


@activity.defn
async def run_cross_event_regressions(profile: str = "") -> dict:
    """Fit cross-asset event-reaction regressions.

    For every (driver, target) pair where ``|pearson| ≥ min_correlation``
    and every ``driver_event_type``, fits OLS of the target's forward
    return (1d/5d/20d) on the driver's pre-event log return. Persists to
    ``cross_event_regressions``.
    """
    from src.utils.stats import StatsDB
    from src.analysis.cross_asset import fit_cross_event_regressions

    domain = _detect_domain(profile)
    resolved = (profile or "coinbase").lower()
    if resolved == "crypto":
        resolved = "coinbase"
    exchange = "ibkr" if domain == "equity" else resolved

    db = StatsDB()
    try:
        results = fit_cross_event_regressions(exchange=exchange, stats_db=db)
    except Exception as e:
        logger.warning(f"run_cross_event_regressions failed: {e}")
        return {"profile": profile, "exchange": exchange, "error": str(e)}

    return {
        "profile": profile,
        "exchange": exchange,
        "regressions": len(results),
    }


# ─── Phase 1-8 smarts activities ────────────────────────────────────────────


def _smart_exchange(profile: str) -> str:
    """Resolve canonical exchange string for smarts activities."""
    domain = _detect_domain(profile)
    resolved = (profile or "coinbase").lower()
    if resolved == "crypto":
        resolved = "coinbase"
    return "ibkr" if domain == "equity" else resolved


def _smart_universe(exchange: str, limit: int = 25) -> list[str]:
    """Return up to ``limit`` actively-traded pairs for the given exchange."""
    try:
        from src.utils.stats import StatsDB
        db = StatsDB()
        with db._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT pair, COUNT(*) AS n
                FROM trades
                WHERE exchange = %s
                GROUP BY pair
                ORDER BY n DESC
                LIMIT %s
                """,
                (exchange, int(limit)),
            ).fetchall()
        out = [r["pair"] if isinstance(r, dict) else r[0] for r in rows]
        if out:
            return out
    except Exception:
        pass
    # Fallback to followed pairs
    try:
        from src.utils.stats import StatsDB
        return sorted(StatsDB().get_followed_pairs_set(exchange=exchange) or [])[:limit]
    except Exception:
        return []


@activity.defn
async def run_outcome_attribution(profile: str = "") -> dict:
    """Phase 1: nightly Brier-score attribution per (feature, agent)."""
    from src.utils.stats import StatsDB
    from src.utils.attribution import compute_attribution
    exch = _smart_exchange(profile)
    db = StatsDB()
    try:
        n = compute_attribution(db, exchange=exch, lookback_days=30)
        return {"profile": profile, "exchange": exch, "rows_written": int(n)}
    except Exception as e:
        logger.warning(f"run_outcome_attribution failed: {e}")
        return {"profile": profile, "exchange": exch, "error": str(e)}


@activity.defn
async def run_counterfactual_replay(profile: str = "") -> dict:
    """Phase 1: replay strategist on historical contexts to detect drift."""
    from src.utils.stats import StatsDB
    from src.utils.attribution import replay_strategist
    exch = _smart_exchange(profile)
    db = StatsDB()
    try:
        llm = _get_llm_client()
        n = await replay_strategist(db, llm, exchange=exch, lookback_days=7, sample_size=30)
        return {"profile": profile, "exchange": exch, "replays": int(n)}
    except Exception as e:
        logger.warning(f"run_counterfactual_replay failed: {e}")
        return {"profile": profile, "exchange": exch, "error": str(e)}


@activity.defn
async def run_lead_lag_matrix(profile: str = "") -> dict:
    """Phase 3: refit pairwise lead-lag OLS for the active universe."""
    from src.utils.stats import StatsDB
    from src.analysis.lead_lag import compute_lead_lag
    exch = _smart_exchange(profile)
    universe = _smart_universe(exch, limit=25)
    db = StatsDB()
    try:
        n = compute_lead_lag(db, exchange=exch, universe=universe)
        return {
            "profile": profile, "exchange": exch,
            "universe_size": len(universe), "edges_persisted": int(n),
        }
    except Exception as e:
        logger.warning(f"run_lead_lag_matrix failed: {e}")
        return {"profile": profile, "exchange": exch, "error": str(e)}


@activity.defn
async def run_event_calendar_sync(profile: str = "") -> dict:
    """Phase 4: refresh upcoming macro / unlock / earnings events."""
    from src.utils.stats import StatsDB
    from src.news.event_calendar import fetch_and_persist_calendar
    exch = _smart_exchange(profile)
    db = StatsDB()
    try:
        result = fetch_and_persist_calendar(db, exchange=exch)
        return {"profile": profile, "exchange": exch, **(result if isinstance(result, dict) else {"persisted": result})}
    except Exception as e:
        logger.warning(f"run_event_calendar_sync failed: {e}")
        return {"profile": profile, "exchange": exch, "error": str(e)}


@activity.defn
async def run_decision_drift(profile: str = "") -> dict:
    """Phase 6: snapshot decision drift vs 30-day baseline."""
    from src.utils.stats import StatsDB
    from src.utils.decision_drift import compute_drift_snapshot
    exch = _smart_exchange(profile)
    db = StatsDB()
    try:
        snap = compute_drift_snapshot(db, exchange=exch)
        return {"profile": profile, "exchange": exch, "snapshot": snap}
    except Exception as e:
        logger.warning(f"run_decision_drift failed: {e}")
        return {"profile": profile, "exchange": exch, "error": str(e)}


@activity.defn
async def run_reasoning_judge(profile: str = "") -> dict:
    """Phase 6: LLM-judge a 1% sample of recent reasoning outputs."""
    from src.utils.stats import StatsDB
    from src.utils.llm_judge import sample_and_judge
    exch = _smart_exchange(profile)
    db = StatsDB()
    try:
        llm = _get_llm_client()
        n = await sample_and_judge(db, llm, exchange=exch, lookback_hours=24, sample_pct=0.01)
        return {"profile": profile, "exchange": exch, "judged": int(n)}
    except Exception as e:
        logger.warning(f"run_reasoning_judge failed: {e}")
        return {"profile": profile, "exchange": exch, "error": str(e)}


@activity.defn
async def run_onchain_sync(profile: str = "") -> dict:
    """Phase 7: refresh on-chain signals (BTC hashrate, stablecoin supply, dominance)."""
    from src.utils.stats import StatsDB
    from src.news.onchain import fetch_and_persist_onchain
    exch = _smart_exchange(profile)
    db = StatsDB()
    try:
        result = fetch_and_persist_onchain(db, exchange=exch)
        return {"profile": profile, "exchange": exch, **(result if isinstance(result, dict) else {"persisted": result})}
    except Exception as e:
        logger.warning(f"run_onchain_sync failed: {e}")
        return {"profile": profile, "exchange": exch, "error": str(e)}
