"""
Cross-Asset Agent — reactive + proactive cross-asset reaction signals.

Companion to ``PatternAgent``. Where ``PatternAgent`` looks at the *own*
symbol's nearest catalyst, ``CrossAssetAgent`` looks at:

* **Reactive:** catalysts firing on a *driver* symbol that has historical
  cross-event regression evidence of moving the *target* (this pair).
  When a driver event is within ``reactive_horizon_days`` (default 3),
  the agent emits a directional advisory weighted by ``beta * R²``.

* **Proactive:** for the cluster the target belongs to, scan upcoming
  catalysts on cluster-mates within ``proactive_horizon_days`` (default
  14) and emit a forward-looking heads-up (no direction, only a
  ``cluster_event_density`` score that the strategist can use to pre-tune
  position sizing).

The agent is purely advisory — emits a structured ``cross_asset_signal``
dict. The strategist may down-weight, but ``AbsoluteRules`` always wins
downstream.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.agents.base_agent import BaseAgent
from src.utils.logger import get_logger

logger = get_logger("agent.cross_asset")


# Conservative defaults so a single noisy driver can't dominate.
_MIN_R2: float = 0.05
_MIN_SAMPLES: int = 6
_MAX_REACTIVE_SIGNALS: int = 5


class CrossAssetAgent(BaseAgent):
    """Reactive + proactive cross-asset reaction signal producer."""

    def __init__(self, llm, state, config):
        super().__init__("cross_asset_agent", llm, state, config)
        ccfg = (config.get("cross_asset_engine") or {})
        self.enabled: bool = bool(ccfg.get("enabled", True))
        self.reactive_horizon_days: int = int(ccfg.get("reactive_horizon_days", 3))
        self.proactive_horizon_days: int = int(ccfg.get("proactive_horizon_days", 14))
        self.min_r2: float = float(ccfg.get("min_r2", _MIN_R2))
        self.min_samples: int = int(ccfg.get("min_samples", _MIN_SAMPLES))
        self.preferred_horizon_days: int = int(ccfg.get("preferred_horizon_days", 5))

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            return {"cross_asset_signal": {"available": False, "reason": "disabled"}}

        pair: str = context.get("pair", "")
        exchange: str = context.get("exchange", "coinbase")
        stats_db = context.get("stats_db")
        if stats_db is None or not pair:
            return {"cross_asset_signal": {"available": False, "reason": "missing_context"}}

        now = datetime.now(timezone.utc)

        # ── Reactive: find drivers with imminent events ─────────────────
        reactive_signals = self._reactive_signals(
            stats_db=stats_db, exchange=exchange, target=pair, now=now,
        )

        # ── Proactive: cluster-mate upcoming catalyst density ───────────
        cluster_mates, proactive = self._proactive_signal(
            stats_db=stats_db, exchange=exchange, target=pair, now=now,
        )

        if not reactive_signals and proactive["upcoming_count"] == 0:
            return {
                "cross_asset_signal": {
                    "available": False,
                    "reason": "no_signals",
                    "cluster_mates": cluster_mates,
                }
            }

        # Aggregate a coarse direction from reactive signals (sum of
        # beta * R² weighted by horizon proximity).
        net_drift = 0.0
        for s in reactive_signals:
            net_drift += float(s.get("expected_drift") or 0.0)
        direction = (
            "long" if net_drift > 0.001 else
            "short" if net_drift < -0.001 else
            "neutral"
        )

        payload = {
            "available": True,
            "target": pair,
            "exchange": exchange,
            "direction": direction,
            "expected_drift": net_drift,
            "reactive": reactive_signals,
            "proactive": proactive,
            "cluster_mates": cluster_mates,
            "computed_at": now.isoformat(),
        }
        self.logger.info(
            f"🔗 {pair}: cross_asset={direction} drift={net_drift:+.4f} "
            f"reactive={len(reactive_signals)} "
            f"proactive_density={proactive['upcoming_count']}"
        )
        return {"cross_asset_signal": payload}

    # ─── Reactive ────────────────────────────────────────────────────────

    def _reactive_signals(
        self,
        *,
        stats_db,
        exchange: str,
        target: str,
        now: datetime,
    ) -> list[dict]:
        """Drivers with imminent catalysts that historically moved this target."""
        try:
            regs = stats_db.get_cross_event_regressions(
                exchange=exchange,
                target_symbol=target,
                min_samples=self.min_samples,
                limit=200,
            )
        except Exception as e:
            self.logger.debug(f"get_cross_event_regressions({target}) failed: {e}")
            return []
        if not regs:
            return []

        # Index: driver_symbol → list of regression rows for this target.
        by_driver: dict[str, list[dict]] = {}
        for r in regs:
            r2 = r.get("r_squared") or 0.0
            if r2 < self.min_r2:
                continue
            by_driver.setdefault(r["driver_symbol"], []).append(r)
        if not by_driver:
            return []

        horizon_end = now + timedelta(days=self.reactive_horizon_days)
        out: list[dict] = []
        for driver, rows in by_driver.items():
            try:
                events = stats_db.get_catalyst_events(
                    exchange=exchange,
                    symbol=driver,
                    start=now - timedelta(days=1),
                    end=horizon_end,
                    limit=20,
                )
            except Exception as e:
                self.logger.debug(f"get_catalyst_events({driver}) failed: {e}")
                continue
            if not events:
                continue

            for ev in events:
                # Find the regression row matching this event_type at the
                # operator's preferred horizon, falling back to whichever
                # row has the highest R² for this event_type.
                et = ev.get("event_type")
                candidates = [r for r in rows if r.get("driver_event_type") == et]
                if not candidates:
                    continue
                preferred = next(
                    (r for r in candidates if int(r["horizon_days"])
                     == self.preferred_horizon_days),
                    None,
                )
                row = preferred or max(
                    candidates, key=lambda r: r.get("r_squared") or 0.0
                )
                beta = float(row.get("beta") or 0.0)
                r2 = float(row.get("r_squared") or 0.0)
                # expected_drift = beta * R² (R² weighting downscales noise).
                expected_drift = beta * r2
                event_ts = ev.get("event_ts")
                if isinstance(event_ts, datetime):
                    days_to = max((event_ts - now).total_seconds() / 86400.0, 0.0)
                else:
                    days_to = 0.0
                out.append({
                    "driver_symbol": driver,
                    "driver_event_type": et,
                    "event_ts": event_ts.isoformat() if isinstance(event_ts, datetime) else None,
                    "days_to_event": round(days_to, 2),
                    "horizon_days": int(row["horizon_days"]),
                    "beta": beta,
                    "r_squared": r2,
                    "sample_count": int(row.get("sample_count") or 0),
                    "expected_drift": expected_drift,
                })

        # Strongest signals first; cap so the payload stays small.
        out.sort(key=lambda s: abs(s["expected_drift"]), reverse=True)
        return out[:_MAX_REACTIVE_SIGNALS]

    # ─── Proactive ───────────────────────────────────────────────────────

    def _proactive_signal(
        self,
        *,
        stats_db,
        exchange: str,
        target: str,
        now: datetime,
    ) -> tuple[list[str], dict]:
        """Density of upcoming catalysts on the target's cluster mates."""
        try:
            mates = stats_db.get_cluster_for_symbol(
                exchange=exchange, symbol=target,
            )
        except Exception as e:
            self.logger.debug(f"get_cluster_for_symbol({target}) failed: {e}")
            return [], {"upcoming_count": 0, "events": []}
        if not mates:
            return [], {"upcoming_count": 0, "events": []}

        horizon_end = now + timedelta(days=self.proactive_horizon_days)
        upcoming: list[dict] = []
        for sym in mates:
            try:
                evs = stats_db.get_catalyst_events(
                    exchange=exchange,
                    symbol=sym,
                    start=now,
                    end=horizon_end,
                    limit=10,
                )
            except Exception:
                continue
            for ev in evs:
                ts = ev.get("event_ts")
                if isinstance(ts, datetime):
                    upcoming.append({
                        "symbol": sym,
                        "event_type": ev.get("event_type"),
                        "event_ts": ts.isoformat(),
                        "days_to_event": round(
                            max((ts - now).total_seconds() / 86400.0, 0.0), 2,
                        ),
                    })

        upcoming.sort(key=lambda e: e["days_to_event"])
        return mates, {
            "upcoming_count": len(upcoming),
            "horizon_days": self.proactive_horizon_days,
            "events": upcoming[:10],
        }
