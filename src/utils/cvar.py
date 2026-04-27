"""
Regime-conditioned CVaR / VaR + shrunk-Kelly sizing.

Two helpers consumed by the risk_manager:

  * ``cvar_for_regime(returns, alpha=0.05)``
        Historical VaR(α) and CVaR(α) over a return series. CVaR is the
        average loss in the worst α-tail — strictly more conservative than
        VaR, and stable enough to compute from ~250 daily candles.

  * ``shrunk_kelly(win_rate, avg_win, avg_loss, samples, *, kelly_cap=0.25)``
        Half-Kelly with Bayesian shrinkage toward zero proportional to
        sample-size: as samples → ∞ this approaches half-Kelly; with very
        few samples it shrinks aggressively.

Both are pure functions; risk_manager wires them in below.
"""

from __future__ import annotations

import math
from typing import Iterable, Optional


def cvar_for_regime(
    returns: Iterable[float], *, alpha: float = 0.05
) -> Optional[dict]:
    """Historical VaR & CVaR (one-sided lower-tail loss).

    Returns ``{"var": float, "cvar": float, "n": int}`` or ``None`` if too
    few observations. Both values are *negative numbers* representing losses
    (e.g. -0.04 = -4%).
    """
    rs = [float(r) for r in returns if r is not None and math.isfinite(r)]
    n = len(rs)
    if n < 30:
        return None
    a = max(0.001, min(0.5, float(alpha)))
    rs_sorted = sorted(rs)
    cutoff_idx = max(1, int(math.floor(a * n)))
    tail = rs_sorted[:cutoff_idx]
    var = rs_sorted[cutoff_idx - 1]
    cvar = sum(tail) / len(tail) if tail else var
    return {"var": var, "cvar": cvar, "n": n}


def shrunk_kelly(
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    samples: int,
    *,
    kelly_fraction: float = 0.5,
    kelly_cap: float = 0.25,
    shrinkage_n: float = 50.0,
) -> float:
    """Shrunk fractional-Kelly position size.

    Standard Kelly: ``f* = (p·b − q) / b`` where ``p=win_rate``,
    ``q=1-p``, ``b=avg_win/avg_loss``. We multiply by ``kelly_fraction``
    (default half-Kelly) AND a Bayesian shrinkage factor::

        shrink = samples / (samples + shrinkage_n)

    so that small-sample edges get pulled aggressively toward 0 and large
    samples approach the full half-Kelly. Final result is clamped at
    ``[0, kelly_cap]`` for tail safety.
    """
    if win_rate <= 0 or avg_win <= 0 or avg_loss <= 0 or samples <= 0:
        return 0.0
    p = min(max(float(win_rate), 0.01), 0.99)
    q = 1.0 - p
    b = float(avg_win) / float(avg_loss)
    if b <= 0:
        return 0.0
    kelly_f = (p * b - q) / b
    if kelly_f <= 0:
        return 0.0
    shrink = float(samples) / (float(samples) + float(shrinkage_n))
    f = kelly_f * float(kelly_fraction) * shrink
    return max(0.0, min(float(kelly_cap), f))


def regime_stop_multiplier(
    regime_label: str,
    *,
    correlation_spike: bool = False,
) -> float:
    """Stop-width tightening factor based on regime + correlation regime.

    Returns a multiplier ∈ [0.5, 1.5] applied on top of the ATR stop:
        * HIGH_VOL → 1.3 (wider stops, vol shock)
        * TRENDING_UP/DOWN → 1.0 (normal)
        * MEAN_REVERTING → 0.85 (tighter)
        * CHOP → 0.75 (tightest, defend capital)
    Correlation-spike adds a 0.1 widening on top of base.
    """
    base = {
        "high_vol":       1.30,
        "trending_up":    1.00,
        "trending_down":  1.00,
        "mean_reverting": 0.85,
        "chop":           0.75,
    }.get((regime_label or "").lower(), 1.0)
    if correlation_spike:
        base += 0.10
    return max(0.5, min(1.5, base))
