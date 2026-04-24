"""
Volatility-targeted position sizing (Phase 13).

Given a strategy's recent return series and a target portfolio-volatility
budget, compute a notional multiplier that scales positions inversely with
realised vol — classic "vol target" sizing as used by CTAs and risk-parity
funds.

Combines with Kelly (already implemented in RiskManagerAgent) by taking
the *minimum* of the two notionals → never larger than either constraint.

Pure functions, numpy-free (uses statistics stdlib). Bounded so a stale or
empty return stream never zeroes the trade.
"""

from __future__ import annotations

import math
import statistics
from typing import Iterable, Optional

# Hard caps that bound the multiplier even if the realised vol is tiny.
_MULT_FLOOR = 0.10
_MULT_CEIL  = 3.00


def realised_vol(returns: Iterable[float], min_n: int = 5) -> Optional[float]:
    """Return population stdev of `returns`, or None if insufficient data."""
    rs = [float(r) for r in returns if math.isfinite(r)]
    if len(rs) < min_n:
        return None
    try:
        v = statistics.pstdev(rs)
        return v if math.isfinite(v) and v > 0 else None
    except Exception:
        return None


def vol_target_multiplier(
    returns: Iterable[float],
    *,
    target_vol: float = 0.01,   # ~1% per period
    min_n: int = 5,
) -> float:
    """Multiplier in [_MULT_FLOOR, _MULT_CEIL]: target / realised."""
    if target_vol <= 0:
        return 1.0
    v = realised_vol(returns, min_n=min_n)
    if v is None:
        # No history → neutral multiplier; rely on Kelly cap.
        return 1.0
    mult = target_vol / v
    return max(_MULT_FLOOR, min(_MULT_CEIL, float(mult)))


def vol_targeted_notional(
    base_notional: float,
    returns: Iterable[float],
    *,
    target_vol: float = 0.01,
    kelly_notional: Optional[float] = None,
) -> float:
    """Apply vol-target multiplier; if a kelly notional is supplied, take min."""
    if base_notional <= 0:
        return 0.0
    mult = vol_target_multiplier(returns, target_vol=target_vol)
    sized = base_notional * mult
    if kelly_notional is not None and kelly_notional > 0:
        sized = min(sized, float(kelly_notional))
    return float(sized)


__all__ = ["realised_vol", "vol_target_multiplier", "vol_targeted_notional"]
