"""
Auto WFO — Automated Walk-Forward Optimization in production.

Scheduled weekly: runs WFO on recent candle data for active pairs,
promotes parameters where Walk-Forward Efficiency ≥ 0.5, with auto-rollback
if post-promotion accuracy drops > 10%.

New DB table: ``parameter_promotions``
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.utils.logger import get_logger

logger = get_logger("utils.auto_wfo")

# Guardrails
_MIN_WFE = 0.5                   # minimum Walk-Forward Efficiency to promote
_MAX_PROMOTIONS_PER_WEEK = 3     # max parameter changes per weekly run
_ROLLBACK_ACCURACY_DROP = 0.10   # 10% accuracy drop triggers auto-rollback
_CANDLE_DAYS = 60                # how many days of candles to use


class AutoWFO:
    """Automated Walk-Forward Optimization for production parameter tuning.

    Lifecycle:
        wfo = AutoWFO(stats_db, exchange, config, settings_manager, audit)
        result = await wfo.run_optimization(pairs=["BTC-USD"])
        wfo.check_rollbacks()
    """

    def __init__(self, stats_db, exchange, config: dict, settings_manager=None, audit=None):
        self._db = stats_db
        self._exchange = exchange
        self._config = config
        self._sm = settings_manager
        self._audit = audit

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @staticmethod
    def create_table_sql() -> str:
        return """
        CREATE TABLE IF NOT EXISTS parameter_promotions (
            id SERIAL PRIMARY KEY,
            run_ts TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
            pair TEXT NOT NULL,
            param_name TEXT NOT NULL,
            old_value REAL NOT NULL,
            new_value REAL NOT NULL,
            wfe REAL NOT NULL,
            oos_sharpe REAL DEFAULT NULL,
            promoted BOOLEAN DEFAULT FALSE,
            rolled_back BOOLEAN DEFAULT FALSE,
            rollback_ts TEXT DEFAULT NULL,
            rollback_reason TEXT DEFAULT NULL,
            pre_promotion_accuracy REAL DEFAULT NULL,
            post_promotion_accuracy REAL DEFAULT NULL
        )
        """

    @staticmethod
    def create_indexes_sql() -> list[str]:
        return [
            "CREATE INDEX IF NOT EXISTS idx_promotions_pair ON parameter_promotions(pair)",
            "CREATE INDEX IF NOT EXISTS idx_promotions_ts ON parameter_promotions(run_ts)",
        ]

    # ------------------------------------------------------------------
    # Optimization run (weekly)
    # ------------------------------------------------------------------

    async def run_optimization(
        self, pairs: list[str], candle_days: int = _CANDLE_DAYS
    ) -> dict[str, Any]:
        """Run WFO for each pair and promote robust parameters.

        Returns summary of optimization results and promotions.
        """
        import asyncio
        from src.backtesting.walk_forward import WalkForwardOptimizer, DEFAULT_PARAM_GRID

        results = {}
        promotions_this_run = 0

        for pair in pairs:
            if promotions_this_run >= _MAX_PROMOTIONS_PER_WEEK:
                logger.info(
                    f"🔬 WFO: max promotions ({_MAX_PROMOTIONS_PER_WEEK}) reached, "
                    f"skipping remaining pairs"
                )
                break

            try:
                # Fetch historical candles
                candles = await asyncio.to_thread(
                    self._exchange.get_candles,
                    pair,
                    granularity="ONE_HOUR",
                    limit=candle_days * 24,
                )

                if not candles or len(candles) < 200:
                    results[pair] = {"skipped": True, "reason": "insufficient_candles", "count": len(candles) if candles else 0}
                    continue

                # Run WFO
                optimizer = WalkForwardOptimizer(
                    config=self._config,
                    is_window_size=min(500, len(candles) // 3),
                    oos_window_size=min(150, len(candles) // 6),
                    step_size=min(150, len(candles) // 6),
                )
                wfo_result = await asyncio.to_thread(optimizer.run, candles, pair)

                results[pair] = {
                    "windows": wfo_result.total_windows,
                    "avg_wfe": wfo_result.avg_wfe,
                    "avg_oos_return": wfo_result.avg_oos_return,
                    "avg_oos_sharpe": wfo_result.avg_oos_sharpe,
                    "is_robust": wfo_result.is_robust,
                    "best_params": wfo_result.best_overall_params,
                    "promoted": [],
                }

                if not wfo_result.is_robust:
                    results[pair]["skipped_promotion"] = True
                    results[pair]["reason"] = f"WFE {wfo_result.avg_wfe:.3f} < {_MIN_WFE}"
                    continue

                # Promote robust parameters
                promoted = self._promote_parameters(pair, wfo_result)
                results[pair]["promoted"] = promoted
                promotions_this_run += len(promoted)

            except Exception as e:
                results[pair] = {"error": str(e)}
                logger.warning(f"WFO failed for {pair}: {e}")

        total_promoted = sum(len(r.get("promoted", [])) for r in results.values())
        logger.info(
            f"🔬 Auto WFO complete: {len(pairs)} pairs, "
            f"{total_promoted} parameters promoted"
        )
        return {
            "pairs_analyzed": len(pairs),
            "total_promoted": total_promoted,
            "results": results,
        }

    # ------------------------------------------------------------------
    # Rollback checking
    # ------------------------------------------------------------------

    def check_rollbacks(self, scorecard) -> list[dict]:
        """Check if any promoted parameters should be rolled back.

        Compares post-promotion accuracy (7 days) against pre-promotion baseline.
        Returns list of rollback actions taken.
        """
        rollbacks = []
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        try:
            with self._db._get_conn() as conn:
                # Find promotions from ~7 days ago that haven't been rolled back
                promotions = conn.execute(
                    """
                    SELECT id, pair, param_name, old_value, new_value,
                           pre_promotion_accuracy, run_ts
                    FROM parameter_promotions
                    WHERE promoted = TRUE
                      AND rolled_back = FALSE
                      AND run_ts <= %s
                      AND pre_promotion_accuracy IS NOT NULL
                    """,
                    (cutoff,),
                ).fetchall()

            for promo in promotions:
                pair = promo["pair"]
                pre_acc = promo["pre_promotion_accuracy"]

                # Get current accuracy for this pair
                post_acc_data = scorecard.get_rolling_accuracy(
                    pair=pair, window_days=7, horizon_hours=24
                )
                post_acc = post_acc_data.get("accuracy_pct")

                if post_acc is None or pre_acc is None:
                    continue

                # Check if accuracy dropped significantly
                drop = (pre_acc - post_acc) / 100.0  # convert from pct
                if drop > _ROLLBACK_ACCURACY_DROP:
                    self._rollback_parameter(promo)
                    rollbacks.append({
                        "pair": pair,
                        "param": promo["param_name"],
                        "old_value": promo["old_value"],
                        "restored_to": promo["old_value"],
                        "accuracy_drop": round(drop * 100, 1),
                    })

                    # Update post-promotion accuracy in DB
                    with self._db._get_conn() as conn:
                        conn.execute(
                            """
                            UPDATE parameter_promotions
                            SET post_promotion_accuracy = %s
                            WHERE id = %s
                            """,
                            (post_acc, promo["id"]),
                        )
                        conn.commit()

        except Exception as e:
            logger.warning(f"Rollback check failed: {e}")

        if rollbacks:
            logger.warning(f"⚠️ Auto WFO: {len(rollbacks)} parameters rolled back")
        return rollbacks

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _promote_parameters(self, pair: str, wfo_result) -> list[dict]:
        """Promote optimized parameters to production settings."""
        promoted = []
        best = wfo_result.best_overall_params

        # Map WFO params to settings.yaml sections
        param_mapping = {
            "trailing_stop_pct": ("risk", "trailing_stop_pct"),
            "stop_pct": ("risk", "stop_loss_pct"),
            "target_pct": ("risk", "take_profit_pct"),
        }

        for param_name, (section, field) in param_mapping.items():
            if param_name not in best:
                continue

            new_value = best[param_name]
            old_value = self._config.get(section, {}).get(field)
            if old_value is None:
                continue

            # Skip if change is negligible
            if abs(new_value - old_value) < 0.001:
                continue

            # Get current accuracy before promotion
            pre_accuracy = None
            try:
                from src.utils.signal_scorecard import SignalScorecard
                sc = SignalScorecard(self._db)
                acc = sc.get_rolling_accuracy(pair=pair, window_days=7, horizon_hours=24)
                pre_accuracy = acc.get("accuracy_pct")
            except Exception:
                pass

            # Record the promotion
            try:
                with self._db._get_conn() as conn:
                    conn.execute(
                        """
                        INSERT INTO parameter_promotions
                            (pair, param_name, old_value, new_value, wfe,
                             oos_sharpe, promoted, pre_promotion_accuracy)
                        VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s)
                        """,
                        (
                            pair, param_name, old_value, new_value,
                            wfo_result.avg_wfe, wfo_result.avg_oos_sharpe,
                            pre_accuracy,
                        ),
                    )
                    conn.commit()
            except Exception as e:
                logger.warning(f"Failed to record promotion: {e}")
                continue

            # Apply to settings
            if self._sm:
                try:
                    ok, err, _ = self._sm.update_section(section, {field: new_value})
                    if ok:
                        promoted.append({
                            "param": param_name,
                            "section": section,
                            "field": field,
                            "old": old_value,
                            "new": new_value,
                            "wfe": wfo_result.avg_wfe,
                        })
                        logger.info(
                            f"🔬 WFO promoted {pair}/{param_name}: "
                            f"{old_value} → {new_value} (WFE={wfo_result.avg_wfe:.3f})"
                        )
                    else:
                        logger.warning(f"WFO promotion rejected by settings manager: {err}")
                except Exception as e:
                    logger.warning(f"WFO promotion failed: {e}")

            # Audit
            if self._audit and promoted:
                self._audit.log("wfo_parameter_promotion", {
                    "pair": pair,
                    "param": param_name,
                    "old": old_value,
                    "new": new_value,
                    "wfe": wfo_result.avg_wfe,
                })

        return promoted

    def _rollback_parameter(self, promo: dict) -> None:
        """Restore a parameter to its pre-promotion value."""
        param_mapping = {
            "trailing_stop_pct": ("risk", "trailing_stop_pct"),
            "stop_pct": ("risk", "stop_loss_pct"),
            "target_pct": ("risk", "take_profit_pct"),
        }

        param_name = promo["param_name"]
        if param_name not in param_mapping:
            return

        section, field = param_mapping[param_name]
        old_value = promo["old_value"]

        if self._sm:
            try:
                ok, err, _ = self._sm.update_section(section, {field: old_value})
                if ok:
                    logger.info(
                        f"⏪ WFO rollback {promo['pair']}/{param_name}: "
                        f"restored to {old_value}"
                    )
            except Exception as e:
                logger.warning(f"WFO rollback failed: {e}")

        # Mark as rolled back in DB
        try:
            now = datetime.now(timezone.utc).isoformat()
            with self._db._get_conn() as conn:
                conn.execute(
                    """
                    UPDATE parameter_promotions
                    SET rolled_back = TRUE,
                        rollback_ts = %s,
                        rollback_reason = 'accuracy_drop'
                    WHERE id = %s
                    """,
                    (now, promo["id"]),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to record rollback: {e}")

        if self._audit:
            self._audit.log("wfo_parameter_rollback", {
                "pair": promo["pair"],
                "param": param_name,
                "restored_to": old_value,
                "promotion_id": promo["id"],
            })

    def get_promotion_history(self, limit: int = 50) -> list[dict]:
        """Get recent parameter promotions for dashboard display."""
        try:
            with self._db._get_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT pair, param_name, old_value, new_value, wfe,
                           oos_sharpe, promoted, rolled_back, run_ts,
                           pre_promotion_accuracy, post_promotion_accuracy
                    FROM parameter_promotions
                    ORDER BY run_ts DESC
                    LIMIT %s
                    """,
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
