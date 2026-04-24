"""
Cross-asset regime arbiter (Phase 12).

Combines per-profile regime snapshots (crypto + equity) into a single
*macro view* the dashboard can show. Strict read-only — does NOT mix
trading state. The two domains stay isolated for execution; only the
*observation layer* is allowed to take a cross-asset view.

Output schema (consumed by /api/quant/macro_regime):
    {
      "ts": ISO,
      "profiles": {
        "coinbase": {"regime": "...", "confidence": 0.x, "atr_pct": ..., ...},
        "ibkr":     {...},
      },
      "consensus": {
        "regime": "RISK_ON" | "RISK_OFF" | "MIXED" | "UNKNOWN",
        "rationale": "..."
      }
    }
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("analysis.cross_asset_regime")

_PROFILES = ("coinbase", "ibkr")


def _latest_snapshot(profile: str) -> Optional[dict]:
    """Read the most recent regime snapshot a substrate may have written.
    The orchestrator writes to ``data/<profile>/regime_snapshot.json`` after
    every cycle (see Orchestrator._write_regime_snapshot).
    """
    p = Path("data") / profile / "regime_snapshot.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception as e:
        logger.warning(f"cross_asset.read err profile={profile}: {e}")
        return None


def _classify(coinbase: Optional[dict], ibkr: Optional[dict]) -> dict:
    """Derive a coarse macro consensus.
    Rule:  both bullish (TRENDING_UP / MEAN_REVERTING with positive bias)  → RISK_ON
           both negative (TRENDING_DOWN / HIGH_VOL with confidence > 0.5)  → RISK_OFF
           else                                                             → MIXED
    """
    def _bias(snap: Optional[dict]) -> int:
        if not snap:
            return 0
        regime = (snap.get("regime") or "").upper()
        conf = float(snap.get("confidence") or 0.0)
        if regime in {"TRENDING_UP"} and conf >= 0.4:
            return +1
        if regime in {"MEAN_REVERTING"} and conf >= 0.4:
            return +1 if (snap.get("slope") or 0.0) >= 0 else -1
        if regime in {"TRENDING_DOWN", "HIGH_VOL"} and conf >= 0.4:
            return -1
        return 0

    b_c, b_i = _bias(coinbase), _bias(ibkr)
    if b_c == +1 and b_i == +1:
        return {"regime": "RISK_ON", "rationale": "both crypto and equities bullish"}
    if b_c == -1 and b_i == -1:
        return {"regime": "RISK_OFF", "rationale": "both crypto and equities bearish/high-vol"}
    if b_c == 0 and b_i == 0:
        return {"regime": "UNKNOWN", "rationale": "insufficient confidence on either side"}
    return {"regime": "MIXED", "rationale": f"crypto bias={b_c:+d} equity bias={b_i:+d}"}


def macro_view() -> dict:
    """Build the cross-asset macro view from on-disk per-profile snapshots."""
    profiles_data: dict[str, dict] = {}
    for p in _PROFILES:
        snap = _latest_snapshot(p)
        if snap is not None:
            profiles_data[p] = snap
    consensus = _classify(profiles_data.get("coinbase"), profiles_data.get("ibkr"))
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "profiles": profiles_data,
        "consensus": consensus,
    }


__all__ = ["macro_view"]
