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
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone, timedelta
from typing import Any

from temporalio import activity

from src.core.llm_client import LLMClient
from src.utils.logger import get_logger

logger = get_logger("planning.activities")

_DB_PATH = os.path.join("data", "stats.db")
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
    """Thread-safe lazy-initialised LLMClient for use inside activities."""
    global _llm_client
    if _llm_client is None:
        with _llm_client_lock:
            # Double-checked locking
            if _llm_client is None:
                _llm_client = LLMClient(
                    base_url=_OLLAMA_BASE,
                    model=_PLANNING_MODEL,
                    temperature=0.3,
                    max_tokens=2000,
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


# --- Helpers ------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# --- Activities ---------------------------------------------------------------

@activity.defn
async def fetch_trade_history(days: int = 7, pair: str | None = None) -> list[dict]:
    """Fetch closed trade history from StatsDB for the planning LLM."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with closing(_get_conn()) as conn:
        if pair:
            rows = conn.execute(
                """SELECT ts, pair, action, price, quote_amount, confidence,
                          signal_type, pnl, fee_quote, reasoning
                   FROM trades WHERE ts >= ? AND pair = ? AND pnl IS NOT NULL
                   ORDER BY ts DESC LIMIT 500""",
                (cutoff, pair),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT ts, pair, action, price, quote_amount, confidence,
                          signal_type, pnl, fee_quote, reasoning
                   FROM trades WHERE ts >= ? AND pnl IS NOT NULL
                   ORDER BY ts DESC LIMIT 500""",
                (cutoff,),
            ).fetchall()
    return [dict(r) for r in rows]


@activity.defn
async def fetch_portfolio_history(days: int = 7) -> dict:
    """Fetch portfolio performance summary for the planning LLM."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with closing(_get_conn()) as conn:
        trade_stats = conn.execute(
            """SELECT
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
               FROM trades WHERE ts >= ? AND pnl IS NOT NULL""",
            (cutoff,),
        ).fetchone()

        pair_breakdown = conn.execute(
            """SELECT pair,
                COUNT(*) as trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                COALESCE(SUM(pnl), 0) as pnl,
                COALESCE(AVG(confidence), 0) as avg_confidence
               FROM trades WHERE ts >= ? AND pnl IS NOT NULL
               GROUP BY pair ORDER BY pnl DESC""",
            (cutoff,),
        ).fetchall()

        portfolio_range = conn.execute(
            """SELECT MIN(portfolio_value) as low, MAX(portfolio_value) as high,
                      AVG(portfolio_value) as avg
               FROM portfolio_snapshots WHERE ts >= ?""",
            (cutoff,),
        ).fetchone()

        # Fetch reasoning traces to understand what drove losses
        reasoning_sample = conn.execute(
            """SELECT ar.ts, ar.pair, ar.signal_type, ar.confidence,
                      ar.reasoning_json, t.action, t.pnl
               FROM agent_reasoning ar
               LEFT JOIN trades t ON t.id = ar.trade_id
               WHERE ar.ts >= ? AND ar.agent_name = 'market_analyst'
                 AND t.pnl IS NOT NULL
               ORDER BY t.pnl ASC LIMIT 20""",
            (cutoff,),
        ).fetchall()

    # Detect native currency from settings (same logic as Orchestrator)
    currency_symbol = "$"
    native_currency = "USD"
    try:
        import yaml
        settings_path = os.path.join(os.path.dirname(_DB_PATH), "..", "config", "settings.yaml")
        if os.path.exists(settings_path):
            with open(settings_path) as f:
                _cfg = yaml.safe_load(f) or {}
            pairs = _cfg.get("trading", {}).get("pairs", [])
            _known_fiat = {"USD", "EUR", "GBP", "CAD", "AUD", "CHF", "JPY"}
            _currency_symbols = {"EUR": "€", "GBP": "£", "CHF": "CHF ", "USD": "$", "CAD": "C$", "AUD": "A$", "JPY": "¥"}
            for pair in pairs:
                if "-" in pair:
                    _, quote = pair.rsplit("-", 1)
                    if quote in _known_fiat:
                        native_currency = quote
                        currency_symbol = _currency_symbols.get(quote, quote + " ")
                        break
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
    review_data: output from fetch_portfolio_history / fetch_trade_history
    """
    SYSTEM_PROMPTS = {
        "daily": """You are the strategic planning module for an automated cryptocurrency trading bot.
Your job is to review yesterday's and last week's trading performance and produce a clear daily plan.

Respond ONLY with JSON:
{
    "regime": "bullish" | "bearish" | "neutral" | "volatile",
    "confidence": 0.0-1.0,
    "preferred_pairs": ["BTC-USD", ...],
    "avoid_pairs": [...],
    "risk_posture": "aggressive" | "normal" | "conservative",
    "key_observations": ["obs1", "obs2", ...],
    "today_focus": "One sentence on the main trading focus for today",
    "summary": "2-3 sentence plain-English summary of the daily plan"
}""",

        "weekly": """You are the strategic planning module for an automated cryptocurrency trading bot.
Your job is to review the last month of trading performance and produce a weekly strategy.

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
    "summary": "3-4 sentence weekly strategic plan"
}""",

        "monthly": """You are the strategic planning module for an automated cryptocurrency trading bot.
Your job is to review the last 90 days and year-to-date performance and produce a monthly portfolio strategy.

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

    system_prompt = SYSTEM_PROMPTS.get(horizon, SYSTEM_PROMPTS["daily"])

    # Format the review data as a concise user message
    stats = review_data.get("trade_stats", {})
    pairs = review_data.get("pair_breakdown", [])
    port = review_data.get("portfolio_range", {})

    total = stats.get("total_trades", 0)
    wins = stats.get("winning", 0)
    win_rate = (wins / total * 100) if total > 0 else 0

    # Dynamic currency symbol — passed through review_data or defaults to '$'
    sym = review_data.get("currency_symbol", "$")
    native = review_data.get("native_currency", "USD")

    pairs_text = "\n".join(
        f"  {p['pair']}: {p['trades']} trades, {p['wins']} wins, PnL {sym}{p['pnl']:.2f}"
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
                    f"  {r['pair']} LOSS {sym}{r['pnl']:.2f}: sig={r['signal_type']} "
                    f"conf={r['confidence']:.0%} factors={factors}"
                )
            except Exception:
                pass

    loss_text = "\n".join(loss_reasoning) or "  No recent losses with reasoning."

    # NOTE on portfolio corrections:
    # If portfolio_correction_note is set, the bot's portfolio tracking was recently
    # corrected.  Tell the LLM to weight recent data more heavily.
    correction_note = ""
    if review_data.get("portfolio_correction_applied"):
        correction_note = """\n
IMPORTANT: The bot's portfolio tracking was recently corrected to reflect
actual live Coinbase holdings. Earlier data may reflect stale or incorrect
portfolio assumptions. Weight recent data and current holdings more heavily.\n"""

    # ── Previous plan evaluation feedback ──────────────────────────────
    eval_data = review_data.get("previous_plan_evaluation", {})
    if eval_data.get("has_previous_plan") and eval_data.get("accuracy_score") is not None:
        eval_block = f"""
PREVIOUS PLAN EVALUATION:
  {eval_data.get('summary', '')}
  Accuracy: {eval_data['accuracy_score']:.0%}
  Component scores: {', '.join(f'{k}={v:.0%}' for k, v in eval_data.get('component_scores', {}).items())}
  {f"Preferred pairs PnL: {eval_data.get('actual_pair_pnl', {})}" if eval_data.get('actual_pair_pnl') else ""}

Use this evaluation to improve your current plan. If accuracy was low,
adjust your approach. If certain pair predictions were wrong, reconsider."""
    elif eval_data.get("has_previous_plan"):
        eval_block = f"""
PREVIOUS PLAN EVALUATION:
  {eval_data.get('summary', 'No trades since last plan — no accuracy data.')}"""
    else:
        eval_block = ""

    user_message = f"""PERFORMANCE REVIEW ({horizon.upper()})
{correction_note}{eval_block}
TRADE STATISTICS:
  Total trades: {total}
  Win rate: {win_rate:.1f}%  ({wins} wins / {stats.get('losing', 0)} losses)
  Total PnL: {sym}{stats.get('total_pnl', 0):.2f}
  Avg PnL per trade: {sym}{stats.get('avg_pnl', 0):.2f}
  Best trade: {sym}{stats.get('best_pnl', 0):.2f}
  Worst trade: {sym}{stats.get('worst_pnl', 0):.2f}
  Avg confidence: {stats.get('avg_confidence', 0):.0%}
  Total volume: {sym}{stats.get('total_volume', 0):,.2f}
  Total fees: {sym}{stats.get('total_fees', 0):.2f}

PAIR BREAKDOWN:
{pairs_text}

PORTFOLIO VALUE RANGE ({native}):
  Low: {sym}{port.get('low', 0):,.2f}
  High: {sym}{port.get('high', 0):,.2f}
  Avg: {sym}{port.get('avg', 0):,.2f}

RECENT LOSS ANALYSIS (signal reasoning that led to losses):
{loss_text}

Generate the {horizon} plan as JSON."""

    # Create a planning trace in Langfuse
    trace_id = f"planning-{horizon}-{uuid.uuid4().hex[:8]}"
    tracer = _get_planning_tracer()
    trace_ctx = tracer.start_trace(
        cycle_id=trace_id,
        pair="planning",
        metadata={"horizon": horizon},
    ) if tracer else None

    span = None
    if trace_ctx is not None:
        span = trace_ctx.start_span(
            f"planning_{horizon}",
            input_data={"system": system_prompt[:500], "user": user_message[:500]},
            model=_PLANNING_MODEL,
        )

    llm = _get_llm_client()
    try:
        result = llm.chat_json(
            system_prompt=system_prompt,
            user_message=user_message,
            span=span,
            agent_name=f"planning_{horizon}",
        )
        logger.info(
            f"Planning LLM response for {horizon}: "
            f"regime={result.get('regime', result.get('market_regime', result.get('macro_regime', '?')))}"
        )
        # Attach the Langfuse trace ID so write_strategic_context can persist it
        result["_langfuse_trace_id"] = trace_id
        if trace_ctx:
            trace_ctx.finish(metadata={"horizon": horizon, "regime": result.get("regime")})
        return result
    except Exception as e:
        logger.error(f"Planning LLM call failed: {e}")
        if trace_ctx:
            trace_ctx.finish(metadata={"error": str(e)})
        return {
            "error": str(e),
            "summary": f"Planning LLM unavailable -- defaulting to neutral {horizon} posture.",
            "regime": "neutral",
            "_langfuse_trace_id": trace_id,
        }


@activity.defn
async def evaluate_previous_plan(horizon: str) -> dict:
    """
    Evaluate how well the previous plan's predictions matched reality.

    Reads the latest strategic_context for *horizon*, compares predicted
    regime / pair preferences / risk posture against actual trade outcomes
    since that plan was written, and returns an accuracy breakdown that
    the next planning LLM call can learn from.
    """
    with closing(_get_conn()) as conn:
        row = conn.execute(
            """SELECT * FROM strategic_context WHERE horizon = ?
               ORDER BY ts DESC LIMIT 1""",
            (horizon,),
        ).fetchone()

        if not row:
            return {"has_previous_plan": False, "summary": "No previous plan to evaluate."}

        prev_plan: dict = json.loads(row["plan_json"])
        plan_ts: str = row["ts"]

        # Actual trade outcomes since (and including) the plan timestamp
        trades = conn.execute(
            """SELECT pair, action, pnl, confidence, signal_type
               FROM trades WHERE ts >= ? AND pnl IS NOT NULL
               ORDER BY ts DESC LIMIT 500""",
            (plan_ts,),
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
            "summary": f"No trades executed since last {horizon} plan — cannot evaluate.",
        }

    # ── aggregate actuals ──────────────────────────────────────────────
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
) -> int:
    """Persist a planning workflow result to StatsDB."""
    from src.utils.stats import StatsDB  # import here to use existing singleton logic
    db = StatsDB()
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
async def write_daily_plan(date: str, plan_text: str) -> None:
    """Write the daily plan text into daily_summaries."""
    from src.utils.stats import StatsDB
    db = StatsDB()
    db.write_daily_plan(date=date, plan_text=plan_text)
    logger.info(f"Wrote daily plan for {date}: {plan_text[:80]}")


@activity.defn
async def fetch_pair_universe() -> dict:
    """Fetch the current pair universe size and product summary.

    Returns a dict with universe_size and a sample of products.
    This queries the Coinbase product catalog directly.
    """
    try:
        from src.core.coinbase_client import CoinbaseClient
        coinbase = CoinbaseClient()
        products = coinbase.discover_all_pairs_detailed(
            include_crypto_quotes=True,
        )
        # Summarise — don't send full list to LLM
        by_quote: dict[str, int] = {}
        for p in products:
            q = p.get("quote_currency", "?")
            by_quote[q] = by_quote.get(q, 0) + 1
        top_by_volume = sorted(
            products, key=lambda p: float(p.get("volume_24h", 0) or 0), reverse=True
        )[:20]
        return {
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
        logger.warning(f"fetch_pair_universe activity failed: {e}")
        return {"universe_size": 0, "error": str(e)}


@activity.defn
async def fetch_universe_scan_summary() -> dict:
    """Fetch the latest universe scan results from StatsDB."""
    try:
        from src.utils.stats import StatsDB
        db = StatsDB()
        scan = db.get_latest_scan_results()
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