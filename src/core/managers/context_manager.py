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

    _ACCURACY_CACHE_TTL: float = 6 * 3600  # re-fetch signal accuracy at most every 6 hours

    def __init__(self, orchestrator: "Orchestrator"):
        self.orchestrator = orchestrator
        self._accuracy_cache: dict | None = None
        self._accuracy_cache_ts: float = 0.0

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

        Also populates orch._pair_expected_gains from pair_outlooks in each plan.
        Daily plans take precedence over weekly (more specific horizon).
        """
        orch = self.orchestrator
        preferred: set[str] = set()
        avoid: set[str] = set()

        # Horizon priority: daily (1) > weekly (7). Lower = more specific.
        _horizon_days = {"daily": 1, "weekly": 7}
        # Accumulate per-pair expected gains; daily beats weekly when both present.
        expected_gains: dict[str, dict] = {}

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

                # Parse pair_outlooks for fee-gate / TP override
                horizon_days = _horizon_days.get(horizon, 7)
                for pair, outlook in plan.get("pair_outlooks", {}).items():
                    try:
                        direction = outlook.get("direction", "neutral")
                        move_pct = float(outlook.get("expected_move_pct", 0))
                        confidence = float(outlook.get("confidence", 0))
                        if direction == "neutral" or move_pct <= 0 or confidence < 0.60:
                            continue
                        if direction == "bearish":
                            continue  # long-only: skip bearish predictions for now
                        existing = expected_gains.get(pair)
                        # Daily beats weekly; within same horizon keep higher confidence
                        if existing is None or horizon_days < existing["horizon_days"] or (
                            horizon_days == existing["horizon_days"]
                            and confidence > existing["confidence"]
                        ):
                            expected_gains[pair] = {
                                "gain_pct": move_pct / 100.0,
                                "direction": direction,
                                "horizon_days": horizon_days,
                                "confidence": confidence,
                            }
                    except (TypeError, ValueError):
                        continue

        # ── Step 1: Scale plan confidence by historical signal accuracy ──────
        # Well-calibrated signals (>50% right) get a confidence boost so their
        # TP overrides are more likely to trigger.  Unreliable signals (<50%)
        # are penalised so their overrides are skipped or need stronger evidence.
        # Requires at least 5 evaluated signals before trusting the accuracy figure.
        _MIN_ACCURACY_SAMPLES = 5
        accuracy_data = self._get_signal_accuracy()
        overall_mult = (
            self._accuracy_multiplier(accuracy_data["overall_pct"])
            if accuracy_data["overall_samples"] >= _MIN_ACCURACY_SAMPLES
            else 1.0
        )
        for pair, entry in expected_gains.items():
            pair_info = accuracy_data["per_pair"].get(pair, {})
            pair_acc = pair_info.get("accuracy_pct")
            pair_samples = pair_info.get("samples", 0)
            if pair_acc is not None and pair_samples >= _MIN_ACCURACY_SAMPLES:
                mult = self._accuracy_multiplier(pair_acc)
                source = f"per-pair {pair_acc:.1f}%"
            elif overall_mult != 1.0:
                mult = overall_mult
                source = f"overall {accuracy_data['overall_pct']:.1f}%"
            else:
                continue  # not enough data — leave confidence unchanged

            if mult == 1.0:
                continue
            original = entry["confidence"]
            entry["confidence"] = round(min(1.0, original * mult), 3)
            logger.info(
                f"📊 {pair}: plan confidence {original:.0%} → {entry['confidence']:.0%} "
                f"(signal accuracy {source}, factor {mult:.2f}x)"
            )

        orch._pair_expected_gains = expected_gains
        if expected_gains:
            logger.info(
                f"📋 Plan-based expected gains: "
                + ", ".join(
                    f"{p} +{v['gain_pct']*100:.1f}% ({v['horizon_days']}d, conf={v['confidence']:.0%})"
                    for p, v in expected_gains.items()
                )
            )

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

    # ── Accuracy-weighted confidence ──────────────────────────────────────

    def _get_signal_accuracy(self) -> dict:
        """Return cached 7-day signal accuracy data (refreshed every 6h).

        Returns:
            overall_pct     – overall 24h direction accuracy (0-100) or None
            overall_samples – number of evaluated signals
            per_pair        – {pair: {"accuracy_pct": float|None, "samples": int}}
        """
        now = time.time()
        if self._accuracy_cache is not None and now - self._accuracy_cache_ts < self._ACCURACY_CACHE_TTL:
            return self._accuracy_cache

        result: dict = {"overall_pct": None, "overall_samples": 0, "per_pair": {}}
        try:
            raw = self.orchestrator.stats_db.get_prediction_accuracy(days=7)
            overall = raw.get("overall", {})
            result["overall_pct"] = overall.get("accuracy_24h_pct")
            result["overall_samples"] = overall.get("evaluated_24h", 0)
            result["per_pair"] = {
                pair: {
                    "accuracy_pct": data.get("accuracy_24h_pct"),
                    "samples": data.get("evaluated_24h", 0),
                }
                for pair, data in raw.get("per_pair", {}).items()
            }
            logger.debug(
                f"📊 Signal accuracy refreshed: overall={result['overall_pct']}% "
                f"(n={result['overall_samples']}, {len(result['per_pair'])} pairs)"
            )
        except Exception as e:
            logger.debug(f"Signal accuracy fetch failed (non-fatal): {e}")

        self._accuracy_cache = result
        self._accuracy_cache_ts = now
        return result

    @staticmethod
    def _accuracy_multiplier(accuracy_pct: float | None) -> float:
        """Convert a direction-accuracy % into a confidence multiplier.

        Calibration (linear, normalised at 50% = coin-flip baseline):
            90 % accuracy → 1.5 × (capped)
            75 % accuracy → 1.5 ×
            60 % accuracy → 1.2 ×
            50 % accuracy → 1.0 × (neutral)
            40 % accuracy → 0.8 ×
            30 % accuracy → 0.6 ×
            10 % accuracy → 0.4 × (floored)

        Formula: clamp(accuracy_pct / 50, 0.4, 1.5)
        """
        if accuracy_pct is None:
            return 1.0
        return max(0.4, min(1.5, accuracy_pct / 50.0))

    def get_pair_confidence_adjustment(self, pair: str) -> float:
        """Return the confidence threshold adjustment for a pair (from planning context)."""
        return getattr(self.orchestrator, "_pair_priority_map", {}).get(pair, 0.0)

    def get_pair_expected_gain(self, pair: str) -> dict | None:
        """Return plan-based expected gain for a pair, or None if not in any plan.

        Returns dict with keys: gain_pct, direction, horizon_days, confidence.
        Only bullish predictions with confidence >= 0.60 are included.
        """
        return getattr(self.orchestrator, "_pair_expected_gains", {}).get(pair)

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
            perf = orch.stats_db.get_performance_summary(hours=24, exchange=orch.config.get("trading", {}).get("exchange", "coinbase").lower())
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
            else:
                parts.append("no trades executed yet — no performance data")

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
                if orch.state.total_trades == 0:
                    return "No trades executed yet — no performance data available."
                return (
                    f"trades: {orch.state.total_trades}, "
                    f"win rate: {orch.state.win_rate:.0%}, "
                    f"PnL: {orch.state.total_pnl:+.2f}"
                )
            except Exception:
                return "unavailable"
