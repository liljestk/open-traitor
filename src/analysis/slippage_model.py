"""
Slippage impact regression.

Linear OLS model::

    slippage_bps = α + β_size · (notional / ADV) + β_vol · realised_vol + ε

Fitted from the historical fill log (``trades`` table). Forward-predicts
slippage for upcoming candidate orders so the executor can:

    * Switch from MARKET to LIMIT when predicted slippage > threshold.
    * Skip trades whose predicted impact erodes expected edge.
    * Surface a ``predicted_slip_bps`` field in the dashboard for QA.

Pure numpy. No new deps.

Usage
-----
    >>> model = fit_slippage_model(fills)
    >>> if model:
    ...     bps = model.predict(notional=500.0, adv=50_000.0, realised_vol=0.02)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from src.utils.logger import get_logger

logger = get_logger("analysis.slippage_model")


MIN_FILLS: int = 30
# Cap predicted slippage to avoid pathological extrapolation when the
# operator submits an unusually large order outside the training range.
PREDICTION_CEIL_BPS: float = 500.0


@dataclass
class SlippageImpactModel:
    """Fitted size + vol slippage model."""

    alpha: float           # intercept (bps)
    beta_size: float       # bps per unit of (notional/ADV)
    beta_vol: float        # bps per unit of realised vol
    r_squared: float
    sample_count: int

    def predict(
        self,
        *,
        notional: float,
        adv: float,
        realised_vol: float,
    ) -> float:
        """Predict slippage in basis points for one candidate order.

        Bounded to [0, PREDICTION_CEIL_BPS] — slippage is always non-negative
        on average and the executor should never see negative bps.
        """
        if notional <= 0 or adv <= 0 or not math.isfinite(notional) or not math.isfinite(adv):
            return 0.0
        size_term = notional / adv
        bps = (
            self.alpha
            + self.beta_size * size_term
            + self.beta_vol * float(realised_vol or 0.0)
        )
        if not math.isfinite(bps):
            return 0.0
        return max(0.0, min(PREDICTION_CEIL_BPS, float(bps)))


# ─── Fit ────────────────────────────────────────────────────────────────


def fit_slippage_model(
    fills: Sequence[dict],
) -> Optional[SlippageImpactModel]:
    """Fit the slippage model from a list of historical fill records.

    Each fill is a dict with at least::

        {
            "notional":     float,   # USD/EUR value of the fill
            "adv":          float,   # average daily volume of the symbol
            "realised_vol": float,   # short-window realised vol around fill
            "slippage_bps": float,   # observed slippage vs reference price
        }

    Rows missing any field or with adv == 0 are skipped.

    Returns ``None`` if fewer than ``MIN_FILLS`` valid rows or design
    matrix is singular.
    """
    xs_size: list[float] = []
    xs_vol: list[float] = []
    ys: list[float] = []
    for f in fills:
        try:
            notional = float(f.get("notional"))
            adv = float(f.get("adv"))
            vol = float(f.get("realised_vol", 0.0))
            slip = float(f.get("slippage_bps"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(notional) and math.isfinite(adv) and math.isfinite(slip)):
            continue
        if adv <= 0 or notional <= 0:
            continue
        xs_size.append(notional / adv)
        xs_vol.append(vol if math.isfinite(vol) else 0.0)
        ys.append(slip)
    if len(ys) < MIN_FILLS:
        return None
    n = len(ys)
    X = np.column_stack([
        np.ones(n),
        np.asarray(xs_size, dtype=float),
        np.asarray(xs_vol, dtype=float),
    ])
    y = np.asarray(ys, dtype=float)
    try:
        coef, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    if not np.all(np.isfinite(coef)):
        return None
    yhat = X @ coef
    resid = y - yhat
    sse = float(np.sum(resid * resid))
    sst = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - (sse / sst) if sst > 0 else 0.0
    return SlippageImpactModel(
        alpha=float(coef[0]),
        beta_size=float(coef[1]),
        beta_vol=float(coef[2]),
        r_squared=float(max(0.0, min(1.0, r2))),
        sample_count=n,
    )


# ─── Persistence helper ─────────────────────────────────────────────────


def persist_slippage_model(
    stats_db,
    *,
    exchange: str,
    model: SlippageImpactModel,
) -> int:
    return stats_db.upsert_slippage_impact_model({
        "exchange": exchange,
        "alpha": model.alpha,
        "beta_size": model.beta_size,
        "beta_vol": model.beta_vol,
        "r_squared": model.r_squared,
        "sample_count": model.sample_count,
    })


__all__ = [
    "MIN_FILLS",
    "PREDICTION_CEIL_BPS",
    "SlippageImpactModel",
    "fit_slippage_model",
    "persist_slippage_model",
]
