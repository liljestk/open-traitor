"""
Quant Analytics Manager.

Periodic refresh of all five quant analytics:
    1. Multi-factor regression  (market_factor_loadings)
    2. HAR-RV vol forecasts     (har_rv_forecasts)
    3. Granger causality scan   (granger_causality)
    4. Slippage impact model    (slippage_impact_models)
    5. Correlation regime event (correlation_regime_events)

Designed to be invoked from:
    * A Temporal activity (planning worker) — once per hour or nightly.
    * The trading orchestrator — opportunistically, skipping if the
      previous run is younger than ``min_interval_seconds``.
    * Tests — directly via ``QuantAnalyticsManager.run_once()``.

The manager **never** raises into the trading loop: every analyzer is
wrapped in try/except and logs warnings on failure.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from src.utils.logger import get_logger

from src.analysis import (
    correlation_regime as _cr,
    cross_asset as _xa,
    granger as _gr,
    har_rv as _hr,
    market_factors as _mf,
    slippage_model as _sm,
)

logger = get_logger("manager.quant_analytics")


# Minimum gap between full refreshes — skip if the previous run finished
# within this many seconds. Backstop against accidental hot loops.
DEFAULT_MIN_INTERVAL_SECONDS: int = 30 * 60   # 30 minutes


@dataclass
class QuantAnalyticsRunReport:
    """Per-step results so callers can log/serve them."""

    started_at: datetime
    finished_at: Optional[datetime] = None
    factor_loadings_written: int = 0
    har_rv_forecasts_written: int = 0
    granger_significant: int = 0
    slippage_model_fit: bool = False
    correlation_regime: Optional[str] = None
    correlation_avg_corr: Optional[float] = None
    correlation_z_score: Optional[float] = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "factor_loadings_written": self.factor_loadings_written,
            "har_rv_forecasts_written": self.har_rv_forecasts_written,
            "granger_significant": self.granger_significant,
            "slippage_model_fit": self.slippage_model_fit,
            "correlation_regime": self.correlation_regime,
            "correlation_avg_corr": self.correlation_avg_corr,
            "correlation_z_score": self.correlation_z_score,
            "errors": list(self.errors),
        }


# ─── Helpers ────────────────────────────────────────────────────────────


def _hourly_log_returns(candles: Sequence[dict]) -> list[float]:
    """Convert candles to log-returns list (ordered)."""
    out: list[float] = []
    prev: Optional[float] = None
    for c in candles:
        close = c.get("c") or c.get("close")
        if close is None:
            continue
        try:
            cf = float(close)
        except (TypeError, ValueError):
            continue
        if cf <= 0:
            prev = None
            continue
        if prev is not None and prev > 0:
            out.append(math.log(cf / prev))
        prev = cf
    return out


def _build_slippage_fills(
    trades: Sequence[dict],
    *,
    stats_db,
    exchange: str,
) -> list[dict]:
    """Build fill records for the slippage model from trades + 1h candles.

    Slippage is approximated as::

        bps = 10_000 · |fill_price − prev_close| / prev_close

    where ``prev_close`` is the close of the most-recent ONE_HOUR candle
    *before* the trade's timestamp. ``adv`` is the mean ONE_DAY volume
    over the prior 30 days. ``realised_vol`` is the stdev of hourly
    returns over the prior 24 bars.

    Returns empty list when not enough data is available.
    """
    out: list[dict] = []
    if not trades:
        return out
    # Group by pair so we cache candle pulls.
    pair_cache: dict[str, list[dict]] = {}
    daily_cache: dict[str, list[dict]] = {}
    now = datetime.now(timezone.utc)
    for t in trades:
        try:
            pair = str(t["pair"])
            price = float(t["price"])
            qty = float(t.get("quantity") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        if price <= 0 or qty <= 0:
            continue
        notional = abs(price * qty)
        # Resolve trade timestamp.
        ts_raw = t.get("ts")
        if isinstance(ts_raw, datetime):
            ts = ts_raw.astimezone(timezone.utc)
        else:
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
        # 1h candles around the fill.
        if pair not in pair_cache:
            try:
                pair_cache[pair] = stats_db.get_candles_range(
                    exchange=exchange, symbol=pair, granularity="ONE_HOUR",
                    start=now - timedelta(days=45), end=now,
                )
            except Exception:
                pair_cache[pair] = []
        candles = pair_cache[pair]
        if not candles:
            continue
        # Find most-recent candle whose ts <= ts (strict ≤ is OK since
        # our candles are end-of-period).
        prev_close: Optional[float] = None
        last_n_returns: list[float] = []
        prev_px: Optional[float] = None
        for c in candles:
            cts = c.get("ts")
            if not isinstance(cts, datetime):
                continue
            if cts.tzinfo is None:
                cts = cts.replace(tzinfo=timezone.utc)
            cf = c.get("c") or c.get("close")
            try:
                cf = float(cf)
            except (TypeError, ValueError):
                continue
            if cf <= 0:
                prev_px = None
                continue
            if prev_px is not None and prev_px > 0:
                last_n_returns.append(math.log(cf / prev_px))
            prev_px = cf
            if cts <= ts:
                prev_close = cf
            else:
                break
        if prev_close is None or prev_close <= 0:
            continue
        slip_bps = 10_000.0 * abs(price - prev_close) / prev_close
        # Realised vol from last 24 returns BEFORE the fill.
        rv_window = last_n_returns[-24:] if len(last_n_returns) >= 5 else []
        if rv_window:
            mean = sum(rv_window) / len(rv_window)
            var = sum((r - mean) ** 2 for r in rv_window) / max(1, len(rv_window) - 1)
            realised_vol = math.sqrt(var)
        else:
            realised_vol = 0.0
        # ADV: mean daily volume over prior 30 days.
        if pair not in daily_cache:
            try:
                daily_cache[pair] = stats_db.get_candles_range(
                    exchange=exchange, symbol=pair, granularity="ONE_DAY",
                    start=now - timedelta(days=45), end=now,
                )
            except Exception:
                daily_cache[pair] = []
        d_candles = daily_cache[pair]
        vols: list[float] = []
        for d in d_candles:
            v = d.get("v") or d.get("volume")
            try:
                vf = float(v)
            except (TypeError, ValueError):
                continue
            if vf > 0:
                vols.append(vf)
        if not vols:
            continue
        # ADV in *quote* units (price × volume) for size comparability.
        adv = (sum(vols) / len(vols)) * prev_close
        if adv <= 0:
            continue
        out.append({
            "notional": notional,
            "adv": adv,
            "realised_vol": realised_vol,
            "slippage_bps": slip_bps,
        })
    return out


# ─── Manager ────────────────────────────────────────────────────────────


class QuantAnalyticsManager:
    """Refresh quant analytics on a cadence, persist results, expose status.

    Construct one instance per profile/exchange. The orchestrator (or a
    Temporal activity) calls :py:meth:`maybe_run` at any cadence; the
    manager throttles runs internally.
    """

    def __init__(
        self,
        *,
        stats_db,
        exchange: str,
        universe: Sequence[str],
        factors: Sequence[str] = _mf.DEFAULT_FACTORS,
        factor_exchange: Optional[str] = None,
        factor_window_days: int = 252,
        har_window_days: int = 180,
        har_granularity: str = "ONE_HOUR",
        granger_window_days: int = 30,
        granger_granularity: str = "ONE_HOUR",
        granger_lags: Sequence[int] = (1, 2, 4, 12, 24),
        granger_significance: float = 0.05,
        correlation_window_days: int = _xa.DEFAULT_WINDOW_DAYS,
        slippage_lookback_hours: int = 24 * 30,
        min_interval_seconds: int = DEFAULT_MIN_INTERVAL_SECONDS,
    ) -> None:
        self.stats_db = stats_db
        self.exchange = (exchange or "").lower() or "coinbase"
        self.universe = list(dict.fromkeys(universe))  # dedupe, preserve order
        self.factors = tuple(factors)
        self.factor_exchange = (factor_exchange or self.exchange).lower()
        self.factor_window_days = int(factor_window_days)
        self.har_window_days = int(har_window_days)
        self.har_granularity = str(har_granularity)
        self.granger_window_days = int(granger_window_days)
        self.granger_granularity = str(granger_granularity)
        self.granger_lags = tuple(int(l) for l in granger_lags)
        self.granger_significance = float(granger_significance)
        self.correlation_window_days = int(correlation_window_days)
        self.slippage_lookback_hours = int(slippage_lookback_hours)
        self.min_interval_seconds = int(min_interval_seconds)
        self._last_run_monotonic: float = -1.0
        self._last_report: Optional[QuantAnalyticsRunReport] = None

    # ─── Throttle gate ──────────────────────────────────────────────────

    def maybe_run(self) -> Optional[QuantAnalyticsRunReport]:
        """Run if we haven't run within ``min_interval_seconds``."""
        if self._last_run_monotonic > 0 and (
            time.monotonic() - self._last_run_monotonic
        ) < self.min_interval_seconds:
            return None
        return self.run_once()

    @property
    def last_report(self) -> Optional[QuantAnalyticsRunReport]:
        return self._last_report

    # ─── Full refresh ───────────────────────────────────────────────────

    def run_once(self) -> QuantAnalyticsRunReport:
        report = QuantAnalyticsRunReport(
            started_at=datetime.now(timezone.utc),
        )
        # 1) Factor loadings.
        try:
            results = _mf.compute_factor_loadings(
                self.exchange,
                self.universe,
                stats_db=self.stats_db,
                factors=self.factors,
                factor_exchange=self.factor_exchange,
                window_days=self.factor_window_days,
            )
            report.factor_loadings_written = _mf.persist_factor_loadings(
                self.stats_db, exchange=self.exchange, results=results,
            )
            logger.info(
                f"quant: factor loadings → {len(results)} symbols, "
                f"{report.factor_loadings_written} rows written."
            )
        except Exception as e:
            msg = f"factor loadings failed: {e}"
            logger.warning(msg)
            report.errors.append(msg)
        # 2) HAR-RV forecasts.
        try:
            forecasts = _hr.compute_har_rv_forecasts(
                self.exchange,
                self.universe,
                stats_db=self.stats_db,
                window_days=self.har_window_days,
                granularity=self.har_granularity,
            )
            report.har_rv_forecasts_written = _hr.persist_har_rv_forecasts(
                self.stats_db, exchange=self.exchange, forecasts=forecasts,
            )
            logger.info(
                f"quant: HAR-RV forecasts → {len(forecasts)} symbols."
            )
        except Exception as e:
            msg = f"HAR-RV failed: {e}"
            logger.warning(msg)
            report.errors.append(msg)
        # 3) Granger causality scan.
        try:
            return_series: dict[str, list[float]] = {}
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=self.granger_window_days)
            # Build aligned hourly returns by intersecting timestamps.
            ts_returns: dict[str, dict[datetime, float]] = {}
            for sym in self.universe:
                try:
                    cs = self.stats_db.get_candles_range(
                        exchange=self.exchange, symbol=sym,
                        granularity=self.granger_granularity,
                        start=start, end=end,
                    )
                except Exception:
                    continue
                m: dict[datetime, float] = {}
                prev: Optional[float] = None
                for c in cs:
                    ts = c.get("ts")
                    close = c.get("c") or c.get("close")
                    if not isinstance(ts, datetime) or close is None:
                        continue
                    try:
                        cf = float(close)
                    except (TypeError, ValueError):
                        continue
                    if cf <= 0:
                        prev = None
                        continue
                    if prev is not None and prev > 0:
                        m[ts.astimezone(timezone.utc)] = math.log(cf / prev)
                    prev = cf
                if m:
                    ts_returns[sym] = m
            # Find common timestamps across the universe.
            if ts_returns:
                common = set.intersection(*(set(m.keys()) for m in ts_returns.values()))
                ordered = sorted(common)
                # Need at least the granger min observations after the largest lag.
                if len(ordered) >= max(self.granger_lags) + _gr.MIN_OBSERVATIONS_AFTER_LAGS + 1:
                    return_series = {
                        sym: [ts_returns[sym][t] for t in ordered]
                        for sym in ts_returns
                    }
            granger_results = _gr.scan_granger_universe(
                return_series,
                lags=self.granger_lags,
                significance=self.granger_significance,
            ) if return_series else []
            written = _gr.persist_granger_results(
                self.stats_db, exchange=self.exchange, results=granger_results,
            )
            report.granger_significant = len(granger_results)
            logger.info(
                f"quant: Granger → {len(granger_results)} significant edges, "
                f"{written} rows written."
            )
        except Exception as e:
            msg = f"Granger failed: {e}"
            logger.warning(msg)
            report.errors.append(msg)
        # 4) Slippage impact model (best-effort — needs trade history).
        try:
            try:
                trades = self.stats_db.get_trades(
                    hours=self.slippage_lookback_hours,
                    exchange=self.exchange,
                    limit=2000,
                )
            except Exception:
                trades = []
            fills = _build_slippage_fills(
                trades, stats_db=self.stats_db, exchange=self.exchange,
            )
            model = _sm.fit_slippage_model(fills)
            if model is not None:
                _sm.persist_slippage_model(
                    self.stats_db, exchange=self.exchange, model=model,
                )
                report.slippage_model_fit = True
                logger.info(
                    f"quant: slippage model fit (n={model.sample_count}, "
                    f"R²={model.r_squared:.3f})."
                )
            else:
                logger.info(
                    f"quant: slippage model skipped (n_fills={len(fills)} < {_sm.MIN_FILLS})"
                )
        except Exception as e:
            msg = f"slippage model failed: {e}"
            logger.warning(msg)
            report.errors.append(msg)
        # 5) Correlation regime snapshot.
        try:
            # Use the existing rolling correlation matrix if a fresh one
            # is available; otherwise compute one in-place.
            try:
                rows = self.stats_db.get_asset_correlations(
                    exchange=self.exchange,
                    window_days=self.correlation_window_days,
                    limit=10_000,
                )
            except Exception:
                rows = []
            if not rows and self.universe:
                rows = _xa.compute_correlation_matrix(
                    self.exchange,
                    self.universe,
                    stats_db=self.stats_db,
                    window_days=self.correlation_window_days,
                )
            history = self.stats_db.get_correlation_regime_history(
                self.exchange, window=_cr.HISTORY_WINDOW,
            )
            snap = _cr.detect_regime(rows, history=history)
            if snap is not None:
                _cr.persist_regime_snapshot(
                    self.stats_db, exchange=self.exchange, snapshot=snap,
                )
                report.correlation_regime = snap.regime
                report.correlation_avg_corr = snap.avg_corr
                report.correlation_z_score = snap.z_score
                logger.info(
                    f"quant: corr regime → {snap.regime} "
                    f"(avg={snap.avg_corr:.3f}, z={snap.z_score:.2f}, "
                    f"pairs={snap.n_pairs})"
                )
            else:
                logger.debug("quant: corr regime skipped (insufficient pairs).")
        except Exception as e:
            msg = f"correlation regime failed: {e}"
            logger.warning(msg)
            report.errors.append(msg)
        # Done.
        report.finished_at = datetime.now(timezone.utc)
        self._last_run_monotonic = time.monotonic()
        self._last_report = report
        return report


__all__ = [
    "QuantAnalyticsManager",
    "QuantAnalyticsRunReport",
    "DEFAULT_MIN_INTERVAL_SECONDS",
]
