"""Per-symbol summary routes — domain-aware picker + drill-down detail.

The RegressionAI page (and any future per-symbol view) needs a single
endpoint that aggregates the data scattered across regression rows,
upcoming catalysts, recent trades and the agent_reasoning journal so
the operator can answer one question per request:

    "What does the system actually know about $SYMBOL, and which of
     those signals currently flow into trading decisions?"

CRITICAL — no LLM in the data path. Every number returned is sourced
from a real DB row; the plain-English summary is built deterministically
from those numbers (template, not generation). This honours the
"no hallucinations" directive.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable

from fastapi import APIRouter, HTTPException, Query

import src.dashboard.deps as deps
from src.utils.logger import get_logger

logger = get_logger("dashboard.symbols")

router = APIRouter(tags=["Symbols"])


# ---------------------------------------------------------------------------
# Helpers (shared with regression route — kept local to avoid coupling)
# ---------------------------------------------------------------------------

def _profile_to_exchange(profile: str) -> str:
    cfg = deps.get_config_for_profile(profile)
    return (cfg.get("trading", {}).get("exchange") or profile or "").lower()


def _domain_for(profile: str) -> str:
    """Return 'crypto' or 'equity' for the active profile."""
    return "equity" if deps.is_equity_profile(profile) else "crypto"


def _clean(value: Any) -> Any:
    """Recursively coerce non-finite floats / datetimes to JSON-safe values.

    Mirrors regression.py — duplicated intentionally because importing
    routes from routes creates circular imports under FastAPI's lifespan.
    """
    if isinstance(value, float):
        return None if not math.isfinite(value) else value
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _sym_universe(db, profile: str, exchange: str) -> list[str]:
    """Return the full picker universe for ``profile`` / ``exchange``.

    The picker is the source of truth for "what does the system know about?"
    so it must mirror the *exact* universe the regression-coverage worker
    treats as in-scope. That means the union of:

        1. ``config/{profile}.yaml`` ``trading.pairs`` (yaml-curated baseline)
        2. ``pair_follows`` rows (human + LLM screener)
        3. The broader scanned candle universe — every symbol on
           ``exchange`` with ≥ ``MIN_BARS_FOR_AUTO_COVERAGE`` daily bars,
           because the universe-scanner has decided it's worth tracking
           and the regression worker will fit a model for it.
        4. Symbols that already have ``event_price_regressions`` rows
           (defensive: a model was fitted for them so the picker must
           expose them even if (1)–(3) miss).

    Domain isolation is enforced upstream — every query filters by
    ``exchange``. No REST calls; the picker must remain snappy.
    """
    cfg = deps.get_config_for_profile(profile)
    pairs: list[str] = list(cfg.get("trading", {}).get("pairs", []) or [])
    seen = {p.upper() for p in pairs}

    def _add(value: Any) -> None:
        up = (value or "").upper().strip() if isinstance(value, str) else ""
        if up and up not in seen and up != "_MACRO_":
            pairs.append(up)
            seen.add(up)

    # 2) human + LLM follows
    try:
        followed: Iterable[str] = db.get_followed_pairs_set(exchange=exchange) or set()
        for p in followed:
            _add(p)
    except Exception as exc:  # pragma: no cover — degraded path
        logger.debug(f"get_followed_pairs_set failed: {exc}")

    # 3) broader scanned/tradeable universe (same heuristic as
    #    regression_coverage._candle_universe_symbols, kept inline so the
    #    dashboard route stays decoupled from the worker package).
    try:
        from src.analysis.regression_coverage import (
            MIN_BARS_FOR_AUTO_COVERAGE,
            _candle_universe_symbols,
        )

        for sym in _candle_universe_symbols(
            db, exchange, min_bars=MIN_BARS_FOR_AUTO_COVERAGE
        ):
            _add(sym)
    except Exception as exc:  # pragma: no cover — degraded path
        logger.debug(f"candle-universe probe failed: {exc}")

    # 4) anything already modeled — picker must surface it even if the
    #    follows/yaml/candle sources have churned away from it.
    try:
        for r in db.get_event_regressions(exchange=exchange, limit=2000) or []:
            _add(r.get("symbol"))
    except Exception as exc:  # pragma: no cover — degraded path
        logger.debug(f"get_event_regressions(universe) failed: {exc}")

    try:
        for r in db.get_market_factor_loadings(exchange=exchange, limit=2000) or []:
            _add(r.get("symbol"))
    except Exception as exc:  # pragma: no cover — degraded path
        logger.debug(f"get_market_factor_loadings(universe) failed: {exc}")

    return sorted(pairs)


def _quality_label(r2: float | None, n: int) -> str:
    """Mirror the frontend QualityBadge — keep server + client in lockstep."""
    if n < 5:
        return "n_a"
    v = r2 if (r2 is not None and not math.isnan(r2)) else 0.0
    if v >= 0.3 and n >= 20:
        return "strong"
    if v >= 0.1 and n >= 10:
        return "moderate"
    return "weak"


# ---------------------------------------------------------------------------
# /api/symbols/list — domain-aware, filterable picker source
# ---------------------------------------------------------------------------

@router.get(
    "/api/symbols/list",
    summary="Domain-aware tradable symbols with data-availability flags",
)
def list_symbols(
    profile: str = Query("", description="Exchange profile"),
    q: str = Query("", description="Optional case-insensitive substring filter"),
):
    """Return the full picker list for the current profile.

    Each entry includes flags (`has_regression`, `has_patterns`, `has_trades`)
    so the UI can render a "R / P / T" tri-dot indicator without N round-trips.
    """
    resolved = deps.resolve_profile(profile)
    db = deps.require_db(resolved)
    exchange = _profile_to_exchange(resolved)
    if not exchange:
        raise HTTPException(status_code=400, detail="profile has no exchange mapping")

    domain = _domain_for(resolved)
    pairs = _sym_universe(db, resolved, exchange)

    # --- bulk lookups so the picker is one query each, not one-per-symbol ---
    # ``has_regression`` reflects coverage from EITHER regression family
    # (event-driven OR macro-factor), matching the product invariant in
    # ``regression_coverage.py``. The quality label still prefers the
    # event-regression score when present (since it's catalyst-anchored
    # and therefore higher-signal), and falls back to the factor R².
    reg_symbols: set[str] = set()
    reg_quality: dict[str, str] = {}
    try:
        reg_rows = db.get_event_regressions(exchange=exchange, limit=500)
        for r in reg_rows:
            sym = (r.get("symbol") or "").upper()
            if not sym:
                continue
            reg_symbols.add(sym)
            label = _quality_label(r.get("r_squared"), int(r.get("sample_count") or 0))
            # keep the *best* quality seen for the symbol
            if reg_quality.get(sym) not in {"strong", "moderate"}:
                reg_quality[sym] = label
            elif label == "strong":
                reg_quality[sym] = "strong"
    except Exception as exc:
        logger.debug(f"get_event_regressions(list) failed: {exc}")

    # Macro-factor regression coverage — the universal fallback.
    try:
        for r in db.get_market_factor_loadings(exchange=exchange, limit=5000) or []:
            sym = (r.get("symbol") or "").upper()
            if not sym:
                continue
            reg_symbols.add(sym)
            if sym not in reg_quality:
                reg_quality[sym] = _quality_label(
                    r.get("r_squared"), int(r.get("sample_count") or 0)
                )
    except Exception as exc:
        logger.debug(f"get_market_factor_loadings(list) failed: {exc}")

    # Recent trade activity (7d) — cheap join via stats_trades
    traded_symbols: set[str] = set()
    try:
        recent_trades = db.get_trades(hours=24 * 7, limit=2000, exchange=exchange)
        for t in recent_trades:
            up = (t.get("pair") or "").upper()
            if up:
                traded_symbols.add(up)
    except Exception as exc:
        logger.debug(f"get_trades(list) failed: {exc}")

    # Upcoming catalysts (30d) — symbol-scoped flag only. One bulk fetch
    # so the picker stays O(1) DB calls regardless of universe size.
    pattern_symbols: set[str] = set()
    try:
        for ev in db.get_upcoming_catalysts(exchange=exchange, horizon_days=30) or []:
            sym = (ev.get("symbol") or "").upper()
            if sym:
                pattern_symbols.add(sym)
    except Exception as exc:
        logger.debug(f"get_upcoming_catalysts(list) failed: {exc}")

    needle = q.strip().upper()
    items: list[dict] = []
    for sym in pairs:
        up = sym.upper()
        if needle and needle not in up:
            continue
        base, _, quote = up.partition("-")
        items.append({
            "symbol": up,
            "base": base,
            "quote": quote or "",
            "domain": domain,
            "has_regression": up in reg_symbols,
            "has_patterns": up in pattern_symbols,
            "has_trades": up in traded_symbols,
            "regression_quality": reg_quality.get(up),
        })

    # Surface symbols that have data first so the picker gravitates to
    # actionable rows.
    items.sort(key=lambda x: (
        not (x["has_regression"] or x["has_patterns"] or x["has_trades"]),
        x["symbol"],
    ))

    return {
        "profile": resolved,
        "exchange": exchange,
        "domain": domain,
        "count": len(items),
        "items": items,
    }


# ---------------------------------------------------------------------------
# /api/symbols/{symbol}/summary — one-shot drill-down payload
# ---------------------------------------------------------------------------

def _build_plain_summary(
    *,
    symbol: str,
    domain: str,
    regressions: list[dict],
    factor_loadings: list[dict],
    upcoming: list[dict],
    trades: list[dict],
    reasoning: list[dict],
) -> str:
    """Deterministic plain-English summary built from the actual numbers.

    No LLM. No generation. Pure template. Every clause is anchored to a
    countable input — if the inputs are empty the clause is omitted, so
    the operator never sees a sentence not backed by data.

    Coverage is reported across BOTH regression families:
    * ``event_price_regressions`` (catalyst-driven; sparse — only fires
      when ``catalyst_events`` exist for the symbol).
    * ``market_factor_loadings``  (macro-factor; the universal fallback,
      always present when the symbol has ≥``MIN_BARS_FOR_AUTO_COVERAGE``
      daily candles). This is the row that satisfies the "every followed
      symbol must have a regression" invariant for the long tail of
      non-US equities Yahoo can’t provide catalyst dates for.
    """
    parts: list[str] = []
    asset = "stock" if domain == "equity" else "asset"
    parts.append(f"{symbol} ({asset}).")

    # Regression clause
    if regressions:
        strong = [r for r in regressions if _quality_label(r.get("r_squared"), int(r.get("sample_count") or 0)) == "strong"]
        moderate = [r for r in regressions if _quality_label(r.get("r_squared"), int(r.get("sample_count") or 0)) == "moderate"]
        if strong or moderate:
            best = max(regressions, key=lambda r: (r.get("r_squared") or 0))
            r2 = best.get("r_squared") or 0
            mfr = best.get("mean_forward_return")
            etype = best.get("event_type")
            n = best.get("sample_count")
            mfr_pct = f"{(mfr * 100):+.2f}%" if isinstance(mfr, (int, float)) and math.isfinite(mfr) else "n/a"
            parts.append(
                f"After {etype} events, mean {best.get('horizon_days')}-day forward "
                f"return is {mfr_pct} across {n} samples (R²={r2 * 100:.1f}%)."
            )
        else:
            parts.append(
                f"{len(regressions)} regression model(s) fitted but none reach a "
                f"moderate-confidence threshold yet (R² < 10% or N < 10)."
            )
    else:
        # No event regression — fall back to the macro-factor regression,
        # which is the universal coverage path.
        if factor_loadings:
            best_factor = max(
                factor_loadings,
                key=lambda r: abs(float(r.get("t_stat") or 0.0)),
            )
            r2 = best_factor.get("r_squared") or 0
            n = best_factor.get("sample_count") or 0
            beta = best_factor.get("beta")
            t_stat = best_factor.get("t_stat")
            factor = best_factor.get("factor") or "market"
            beta_str = f"{beta:+.2f}" if isinstance(beta, (int, float)) and math.isfinite(beta) else "n/a"
            t_str = f"{t_stat:+.2f}" if isinstance(t_stat, (int, float)) and math.isfinite(t_stat) else "n/a"
            parts.append(
                f"No catalyst-driven regression yet (no upcoming/past "
                f"earnings or dividends recorded). Macro-factor model fitted: "
                f"strongest loading is {factor} (β={beta_str}, t={t_str}, "
                f"R²={r2 * 100:.1f}%, N={n})."
            )
        else:
            parts.append("No regression model has been fitted for this symbol yet.")

    # Upcoming catalysts
    if upcoming:
        next_ev = upcoming[0]
        ets = next_ev.get("event_ts")
        et = next_ev.get("event_type")
        parts.append(f"Next upcoming catalyst: {et} at {ets}.")

    # Trade activity
    if trades:
        wins = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
        losses = sum(1 for t in trades if (t.get("pnl") or 0) < 0)
        total_pnl = sum((t.get("pnl") or 0) for t in trades)
        parts.append(
            f"{len(trades)} trade(s) in last 7d — {wins}W/{losses}L, "
            f"net P&L {total_pnl:+.2f}."
        )
    else:
        parts.append("No trades executed in the last 7d.")

    # Reasoning activity. Treat empty strings as missing so the summary
    # never renders the meaningless "Most recent agent signal: ?" sentence
    # — if no agent in the latest cycle produced a verdict, omit the clause.
    if reasoning:
        latest = reasoning[0]
        sig = (latest.get("signal_type") or "").strip() or (latest.get("action") or "").strip()
        if sig:
            parts.append(f"Most recent agent signal: {sig}.")

    return " ".join(parts)


def _live_impact_block() -> dict:
    """Hard-coded ground truth about which signals currently affect trading.

    These flags reflect the actual code paths in the trading loop. When a
    new signal becomes consumed by `risk_manager` or the `trader`, update
    this block — it is the user-facing source of truth that prevents
    "shiny dashboard, dead data" hallucinations.
    """
    return {
        "patterns": {
            "in_decision_loop": True,
            "where": "risk_manager.py:513 (confidence multiplier), trader.py:104 (LLM context)",
        },
        "regressions": {
            "in_decision_loop": True,  # wired in this commit; gated by REGRESSION_RISK_FACTOR_ENABLED
            "where": "pipeline_manager.py (regression_factor build), risk_manager.py:Step 4d (bounded multiplier)",
            "feature_flag": "REGRESSION_RISK_FACTOR_ENABLED env or risk.use_regression_factor in profile yaml (default off)",
            "bounds": [0.9, 1.1],
            "quality_gate": "R² ≥ 0.10 and N ≥ 10 and matching catalyst within 72h",
        },
        "trades": {
            "in_decision_loop": True,
            "where": "executor + portfolio reconciliation; PnL feeds Kelly stats and edge library",
        },
        "reasoning_journal": {
            "in_decision_loop": False,
            "where": "Display-only; consumed downstream by ensemble_optimizer (analytics) + finetuning_pipeline.",
        },
    }


@router.get(
    "/api/symbols/{symbol}/summary",
    summary="Aggregated per-symbol drill-down (regression + patterns + trades + reasoning)",
)
def get_symbol_summary(
    symbol: str,
    profile: str = Query(""),
    horizon_days: int = Query(30, ge=1, le=180),
    trade_hours: int = Query(24 * 7, ge=1, le=24 * 90),
    reasoning_limit: int = Query(5, ge=1, le=50),
):
    """Return a single payload aggregating everything the system knows.

    Layout matches the right-panel tabs (`Overview | Regression | Patterns
    | Trades | Reasoning`) so the frontend can render with a single fetch.
    """
    resolved = deps.resolve_profile(profile)
    db = deps.require_db(resolved)
    exchange = _profile_to_exchange(resolved)
    if not exchange:
        raise HTTPException(status_code=400, detail="profile has no exchange mapping")

    sym = symbol.upper()
    domain = _domain_for(resolved)

    # --- regression rows -----------------------------------------------------
    try:
        regressions = db.get_event_regressions(
            exchange=exchange, symbol=sym, order_by="r_squared", limit=50
        )
    except Exception as exc:
        logger.debug(f"get_event_regressions({sym}) failed: {exc}")
        regressions = []

    # --- factor-loading rows (universal coverage fallback) ------------------
    # The macro-factor regression always produces a row when the symbol
    # has ≥ MIN_BARS_FOR_AUTO_COVERAGE daily candles. Surfacing it here
    # ensures the picker / drill-down never lies about "no regression"
    # for symbols whose catalyst feed is empty (e.g. .DE / .PA / .L
    # equities Yahoo's free quoteSummary endpoint can't reach).
    try:
        factor_loadings = db.get_market_factor_loadings(
            exchange=exchange, symbol=sym, limit=50,
        )
    except Exception as exc:
        logger.debug(f"get_market_factor_loadings({sym}) failed: {exc}")
        factor_loadings = []

    # --- upcoming catalysts (and per-event pattern outcome if available) ---
    upcoming: list[dict] = []
    try:
        raw_upcoming = db.get_upcoming_catalysts(
            exchange=exchange, horizon_days=horizon_days, symbol=sym
        )
    except Exception as exc:
        logger.debug(f"get_upcoming_catalysts({sym}) failed: {exc}")
        raw_upcoming = []

    if raw_upcoming:
        # Lazy import — pattern_engine pulls numpy.
        try:
            from src.analysis.pattern_engine import predict_for_upcoming
        except Exception:
            predict_for_upcoming = None  # type: ignore[assignment]

        for ev in raw_upcoming[:10]:
            entry = {k: _clean(v) for k, v in ev.items()}
            if predict_for_upcoming is not None:
                try:
                    outcome = predict_for_upcoming(
                        db=db,
                        exchange=exchange,
                        symbol=sym,
                        upcoming_event_ts=ev["event_ts"],
                        event_type=ev["event_type"],
                        granularity="ONE_DAY",
                        sentiment_score=None,
                        k=20,
                    )
                    entry["outcome"] = {
                        "direction": outcome.direction,
                        "expected_drift": _clean(outcome.expected_drift),
                        "n_matches": outcome.n_matches,
                        "confidence": _clean(outcome.confidence),
                    }
                except Exception as exc:
                    logger.debug(f"predict_for_upcoming({sym}) failed: {exc}")
                    entry["outcome"] = {"direction": "neutral", "n_matches": 0, "error": str(exc)[:120]}
            upcoming.append(entry)

    # --- recent trades -------------------------------------------------------
    try:
        recent_trades = db.get_trades(
            hours=trade_hours, pair=sym, limit=50, exchange=exchange,
        )
    except Exception as exc:
        logger.debug(f"get_trades({sym}) failed: {exc}")
        recent_trades = []

    # --- agent reasoning cycles (verbatim — no LLM rewrite) ----------------
    try:
        cycles = db.get_cycles(pair=sym, limit=reasoning_limit, exchange=exchange)
    except Exception as exc:
        logger.debug(f"get_cycles({sym}) failed: {exc}")
        cycles = []

    cleaned_regressions = [{k: _clean(v) for k, v in r.items()} for r in regressions]
    cleaned_factor_loadings = [{k: _clean(v) for k, v in r.items()} for r in factor_loadings]
    cleaned_trades = [{k: _clean(v) for k, v in t.items()} for t in recent_trades]
    cleaned_cycles = [{k: _clean(v) for k, v in c.items()} for c in cycles]

    plain_summary = _build_plain_summary(
        symbol=sym,
        domain=domain,
        regressions=cleaned_regressions,
        factor_loadings=cleaned_factor_loadings,
        upcoming=upcoming,
        trades=cleaned_trades,
        reasoning=cleaned_cycles,
    )

    return {
        "profile": resolved,
        "exchange": exchange,
        "symbol": sym,
        "domain": domain,
        "plain_summary": plain_summary,
        "regression": {
            "count": len(cleaned_regressions),
            "rows": cleaned_regressions,
        },
        "factor_regression": {
            "count": len(cleaned_factor_loadings),
            "rows": cleaned_factor_loadings,
        },
        "patterns": {
            "count": len(upcoming),
            "upcoming": upcoming,
        },
        "trades": {
            "count": len(cleaned_trades),
            "rows": cleaned_trades,
        },
        "reasoning": {
            "count": len(cleaned_cycles),
            "cycles": cleaned_cycles,
        },
        "live_impact": _live_impact_block(),
    }


__all__ = ["router"]
