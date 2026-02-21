"""
ContextManager — Strategic context, pair priorities, and performance summaries.

Extracted from Orchestrator for maintainability.  Takes an orchestrator reference
in its constructor (same pattern as PipelineManager / StateManager).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.core.orchestrator import Orchestrator

logger = get_logger("core.context_manager")


class ContextManager:
    """Manages strategic context loading, pair priorities, and performance summaries."""

    def __init__(self, orchestrator: "Orchestrator"):
        self.orchestrator = orchestrator

    def get_strategic_context(self) -> str:
        """Return the latest strategic context string (cached 60s, reads from StatsDB)."""
        orch = self.orchestrator
        now = time.time()
        if now - orch._strategic_context_ts < orch._STRATEGIC_CONTEXT_TTL:
            return orch._strategic_context_str
        try:
            rows = orch.stats_db.get_latest_strategic_context()
            if not rows:
                orch._strategic_context_str = ""
                orch._pair_priority_map = {}
            else:
                parts = []
                for row in rows:
                    horizon = row["horizon"].upper()
                    text = row["summary_text"] or ""
                    if text:
                        parts.append(f"[{horizon} PLAN] {text}")
                orch._strategic_context_str = "\n".join(parts)

                # ── Parse pair priority from latest daily plan ──────────
                orch._pair_priority_map = self.parse_pair_priorities(rows)

                # Warn when the newest plan is older than 48 h (planning worker down?)
                try:
                    latest_ts_str = max(row["ts"] for row in rows)
                    latest_ts = datetime.fromisoformat(latest_ts_str.replace("Z", "+00:00"))
                    age_h = (datetime.now(timezone.utc) - latest_ts).total_seconds() / 3600
                    if age_h > 48:
                        logger.warning(
                            f"⚠️ Strategic context is {age_h:.0f}h old — "
                            "planning worker may not be running; using stale plan."
                        )
                except Exception:
                    pass
            orch._strategic_context_ts = now
        except Exception as e:
            logger.debug(f"Failed to load strategic context: {e}")
        return orch._strategic_context_str

    def parse_pair_priorities(self, context_rows: list[dict]) -> dict[str, float]:
        """Extract per-pair confidence adjustments from the latest daily/weekly plans.

        Returns a dict mapping pair -> confidence_adjustment:
          * preferred pairs get -0.05 (slightly more lenient)
          * avoid pairs get +0.10 (need stronger signal to trade)
          * other pairs get 0.0 (no adjustment)
        """
        orch = self.orchestrator
        preferred: set[str] = set()
        avoid: set[str] = set()

        for row in context_rows:
            try:
                plan = json.loads(row.get("plan_json", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue

            horizon = row.get("horizon", "")
            if horizon in ("daily", "weekly"):
                for p in plan.get("preferred_pairs", plan.get("pairs_to_focus", [])):
                    preferred.add(p)
                for p in plan.get("avoid_pairs", plan.get("pairs_to_reduce", [])):
                    avoid.add(p)

        priority_map: dict[str, float] = {}
        for pair in orch.pairs:
            if pair in avoid:
                priority_map[pair] = 0.10   # raise min_confidence by 10pp
            elif pair in preferred:
                priority_map[pair] = -0.05   # lower min_confidence by 5pp
            # else: 0.0 (default, not stored to keep map sparse)

        if priority_map:
            logger.info(
                f"📋 Pair priority from planning: "
                f"focus={[p for p, v in priority_map.items() if v < 0]}, "
                f"avoid={[p for p, v in priority_map.items() if v > 0]}"
            )
        return priority_map

    def get_pair_confidence_adjustment(self, pair: str) -> float:
        """Return the confidence threshold adjustment for a pair (from planning context)."""
        return getattr(self.orchestrator, "_pair_priority_map", {}).get(pair, 0.0)

    def get_performance_summary(self) -> str:
        """Build a short performance summary string for the settings advisor.

        Uses StatsDB for accurate historical metrics (24h window) and
        TradingState for live portfolio/position data.
        """
        orch = self.orchestrator
        try:
            sym = orch.state.currency_symbol
            parts: list[str] = []

            # Historical trade performance from StatsDB (24h)
            perf = orch.stats_db.get_performance_summary(hours=24)
            stats = perf.get("trade_stats", {})
            total_trades = stats.get("total_trades", 0)
            winning = stats.get("winning", 0)
            total_pnl = stats.get("total_pnl", 0)
            win_rate = (winning / total_trades * 100) if total_trades > 0 else 0
            avg_confidence = stats.get("avg_confidence", 0)

            parts.append(f"24h trades: {total_trades}")
            if total_trades > 0:
                parts.append(f"win rate: {win_rate:.0f}%")
                parts.append(f"PnL: {sym}{total_pnl:+.2f}")
                parts.append(f"avg confidence: {avg_confidence:.0%}")

            # Current portfolio state
            n_positions = len(orch.state.open_positions)
            pv = orch.state.portfolio_value
            ret = orch.state.return_pct
            dd = orch.state.max_drawdown
            parts.append(f"open positions: {n_positions}")
            parts.append(f"portfolio: {sym}{pv:,.2f} ({ret:+.1%})")
            parts.append(f"max drawdown: {dd:.1%}")

            # Win/loss streak from recent trades
            recent = list(orch.state.trades[-20:])
            closed = [t for t in recent if t.pnl is not None]
            if closed:
                streak = 0
                streak_type = "win" if (closed[-1].pnl or 0) > 0 else "loss"
                for t in reversed(closed):
                    if (streak_type == "win" and (t.pnl or 0) > 0) or \
                       (streak_type == "loss" and (t.pnl or 0) <= 0):
                        streak += 1
                    else:
                        break
                parts.append(f"current streak: {streak} {streak_type}{'es' if streak_type == 'loss' else 's'}")

            return " | ".join(parts)
        except Exception as e:
            logger.debug(f"Performance summary fallback: {e}")
            # Minimal fallback from TradingState only
            try:
                return (
                    f"trades: {orch.state.total_trades}, "
                    f"win rate: {orch.state.win_rate:.0%}, "
                    f"PnL: {orch.state.total_pnl:+.2f}"
                )
            except Exception:
                return "unavailable"
