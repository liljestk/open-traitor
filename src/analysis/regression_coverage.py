"""Regression coverage for every followed asset.

This module exists to satisfy a single product invariant:

    *Every symbol followed by a human or by the LLM screener must have
    at least one regression row in either ``event_price_regressions`` or
    ``market_factor_loadings`` for its exchange.*

It is the connective tissue between:

* ``analysis.event_regression`` — catalyst-driven fits (only produces
  rows when the symbol has catalysts).
* ``analysis.market_factors``   — macro-factor regressions (always
  produces rows when daily candles exist).
* ``analysis.factor_universe``  — keeps the macro-factor candle store
  fresh.

Public entry points:

* :func:`refresh_regression_for_symbols` — sync, used by the dashboard
  follow-hook to refresh a single symbol on demand.
* :func:`refresh_regression_for_followed` — sync, used by the Temporal
  activity to refresh the full followed-asset universe.
* :func:`compute_coverage_stats`         — read-only summary used by
  the ``/api/regression/coverage`` dashboard endpoint.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from src.analysis.event_regression import (
    DEFAULT_HORIZONS_DAYS,
    run_event_regressions_for_profile,
)
from src.analysis.factor_universe import (
    FACTOR_EXCHANGE,
    ensure_factor_candles,
)
from src.analysis.market_factors import (
    DEFAULT_FACTORS,
    compute_factor_loadings,
    persist_factor_loadings,
)
from src.utils.logger import get_logger

logger = get_logger("analysis.regression_coverage")


def _followed_symbols(stats_db, exchange: str, profile: str = "") -> list[str]:
    """Union of yaml-configured pairs + human/LLM follows for ``exchange``.

    Yaml pairs are the system's AI-curated baseline; ``pair_follows``
    contains both human additions and LLM-screener selections.
    """
    out: set[str] = set()
    # 1) yaml-configured pairs
    try:
        import os
        import yaml as _yaml
        safe_profile = "".join(
            c for c in (profile or exchange or "") if c.isalnum() or c in "-_"
        )
        if safe_profile:
            cfg_path = os.path.join("config", f"{safe_profile}.yaml")
            if os.path.isfile(cfg_path):
                with open(cfg_path) as f:
                    cfg = _yaml.safe_load(f) or {}
                for p in (cfg.get("trading", {}) or {}).get("pairs", []) or []:
                    if isinstance(p, str) and p.strip():
                        out.add(p.strip())
    except Exception as exc:
        logger.debug(f"regression_coverage: yaml pair load failed: {exc}")
    # 2) DB follows (human + LLM)
    try:
        out.update(stats_db.get_followed_pairs_set(exchange=exchange) or set())
    except Exception as exc:
        logger.warning(f"regression_coverage: followed-pair load failed: {exc}")
    return sorted(out)


def refresh_regression_for_symbols(
    *,
    stats_db,
    exchange: str,
    symbols: Sequence[str],
    factors: Sequence[str] = DEFAULT_FACTORS,
    horizons: Iterable[int] = DEFAULT_HORIZONS_DAYS,
    ensure_factors: bool = True,
) -> dict:
    """Refresh both event- and factor-regressions for ``symbols``.

    Always idempotent — safe to call repeatedly. Returns a summary
    dict suitable for log/HTTP responses.
    """
    symbols = [s.strip() for s in (symbols or []) if s and s.strip()]
    if not symbols:
        return {
            "exchange": exchange,
            "symbols": [],
            "event_models": 0,
            "factor_models": 0,
            "factor_rows": 0,
            "errors": [],
        }

    errors: list[str] = []

    if ensure_factors:
        try:
            ensure_factor_candles(stats_db, factors=factors)
        except Exception as exc:
            logger.warning(f"regression_coverage: ensure_factor_candles failed: {exc}")
            errors.append(f"ensure_factors:{exc}")

    # ── event regressions (catalyst-driven; may be 0 rows by design) ──
    event_models = 0
    try:
        ev_results = run_event_regressions_for_profile(
            db=stats_db,
            exchange=exchange,
            symbols=symbols,
            horizons=horizons,
        )
        event_models = sum(1 for r in ev_results if getattr(r, "notes", "") == "ok")
    except Exception as exc:
        logger.warning(f"regression_coverage: event regression failed: {exc}")
        errors.append(f"event:{exc}")

    # ── factor regressions (always produces a row when candles exist) ──
    factor_models = 0
    factor_rows = 0
    try:
        results = compute_factor_loadings(
            exchange=exchange,
            symbols=symbols,
            stats_db=stats_db,
            factors=factors,
            factor_exchange=FACTOR_EXCHANGE,
        )
        factor_models = len(results)
        factor_rows = persist_factor_loadings(
            stats_db, exchange=exchange, results=results,
        )
    except Exception as exc:
        logger.warning(f"regression_coverage: factor regression failed: {exc}")
        errors.append(f"factor:{exc}")

    logger.info(
        f"regression_coverage: {exchange} symbols={len(symbols)} "
        f"event_models={event_models} factor_models={factor_models} "
        f"factor_rows={factor_rows} errors={len(errors)}"
    )
    return {
        "exchange": exchange,
        "symbols": symbols,
        "event_models": event_models,
        "factor_models": factor_models,
        "factor_rows": factor_rows,
        "errors": errors,
    }


def refresh_regression_for_followed(
    *,
    stats_db,
    exchange: str,
    factors: Sequence[str] = DEFAULT_FACTORS,
    profile: str = "",
) -> dict:
    """Refresh regressions for every symbol currently followed."""
    symbols = _followed_symbols(stats_db, exchange, profile=profile)
    if not symbols:
        return {
            "exchange": exchange,
            "symbols": [],
            "event_models": 0,
            "factor_models": 0,
            "factor_rows": 0,
            "errors": [],
            "note": "no_followed_symbols",
        }
    return refresh_regression_for_symbols(
        stats_db=stats_db,
        exchange=exchange,
        symbols=symbols,
        factors=factors,
    )


def compute_coverage_stats(stats_db, exchange: str, profile: str = "") -> dict:
    """Coverage summary for the ``/api/regression/coverage`` endpoint.

    Returns ``{exchange, followed, modeled, missing[], coverage_pct}``
    where ``modeled`` is the count of followed symbols that have at
    least one row in *either* ``event_price_regressions`` or
    ``market_factor_loadings``.
    """
    followed = _followed_symbols(stats_db, exchange, profile=profile)
    if not followed:
        return {
            "exchange": exchange,
            "followed": 0,
            "modeled": 0,
            "missing": [],
            "coverage_pct": 100.0,
            "factor_symbols": 0,
            "event_symbols": 0,
        }

    # Event-regression symbols.
    event_syms: set[str] = set()
    try:
        rows = stats_db.get_event_regressions(
            exchange=exchange, limit=5000,
        )
        for r in rows or []:
            sym = r.get("symbol")
            if sym and sym != "_MACRO_":
                event_syms.add(sym)
    except Exception as exc:
        logger.debug(f"coverage: event row probe failed: {exc}")

    # Factor-loading symbols.
    factor_syms: set[str] = set()
    try:
        rows = stats_db.get_market_factor_loadings(
            exchange=exchange, limit=5000,
        )
        for r in rows or []:
            sym = r.get("symbol")
            if sym:
                factor_syms.add(sym)
    except Exception as exc:
        logger.debug(f"coverage: factor row probe failed: {exc}")

    modeled_set = event_syms | factor_syms
    missing = sorted(s for s in followed if s not in modeled_set)
    modeled = len(followed) - len(missing)
    pct = round(100.0 * modeled / len(followed), 2) if followed else 100.0
    return {
        "exchange": exchange,
        "followed": len(followed),
        "modeled": modeled,
        "missing": missing,
        "coverage_pct": pct,
        "factor_symbols": len(factor_syms),
        "event_symbols": len(event_syms),
    }


__all__ = [
    "refresh_regression_for_symbols",
    "refresh_regression_for_followed",
    "compute_coverage_stats",
]
