"""
Learning Manager — Coordinates all Adaptive Learning Engine (ALE) subsystems.

Schedules subsystem runs on appropriate cadences:
  - Signal Scorecard: every cycle (lightweight SQL)
  - Confidence Calibrator: weekly retrain
  - Ensemble Optimizer: weekly weight update
  - Prompt Evolver: weekly prompt supplement regeneration
  - Auto WFO: weekly parameter optimization
  - Fine-Tuning Pipeline: monthly export

Provides global kill-switch via ``llm_optimizer.get("learning_enabled")``.
Tracks run history in ``learning_runs`` DB table.
"""

from __future__ import annotations

import asyncio
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.utils.logger import get_logger
from src.utils import llm_optimizer

logger = get_logger("core.learning_manager")

# ── Schedule cadences (seconds) ──────────────────────────────────────────────
_SCORECARD_INTERVAL = 0       # every cycle
_CALIBRATOR_INTERVAL = 7 * 24 * 3600   # weekly
_ENSEMBLE_INTERVAL = 7 * 24 * 3600     # weekly
_PROMPT_INTERVAL = 7 * 24 * 3600       # weekly
_WFO_INTERVAL = 7 * 24 * 3600          # weekly
_FINETUNE_INTERVAL = 30 * 24 * 3600    # monthly
_QUANT_ATTRIBUTION_INTERVAL = 24 * 3600  # daily — cheap pure-SQL pass

# How often the heartbeat-pump thread bumps the orchestrator watchdog
# while a learning subsystem is running. Module-level so tests can patch.
_HEARTBEAT_PUMP_INTERVAL_S = 30.0


class LearningManager:
    """Orchestrates all ALE subsystems on their individual schedules.

    Lifecycle::

        lm = LearningManager(orchestrator)
        # Called at end of each trading cycle:
        await lm.tick(cycle_count)
    """

    def __init__(self, orchestrator):
        self.orch = orchestrator
        self._stats_db = orchestrator.stats_db
        self._config = orchestrator.config
        self._audit = orchestrator.audit

        # ── Subsystems (initialized lazily to avoid import loops) ─────────
        self._scorecard = None
        self._calibrator = None
        self._ensemble = None
        self._prompt_evolver = None
        self._auto_wfo = None
        self._finetune = None

        # ── Last-run timestamps ───────────────────────────────────────────
        self._last_run: dict[str, float] = {
            "scorecard": 0.0,
            "calibrator": 0.0,
            "ensemble": 0.0,
            "prompt_evolver": 0.0,
            "auto_wfo": 0.0,
            "finetune": 0.0,
            "quant_attribution": 0.0,
        }

        # Latest quant-attribution snapshot (consumed by the model card +
        # by adaptive overlays). Populated by ``_run_quant_attribution``.
        self._latest_quant_attribution: Optional[dict[str, Any]] = None
        self._latest_quant_adjustments: Optional[dict[str, Any]] = None

        # ── DB table creation ─────────────────────────────────────────────
        self._ensure_tables()

        # ── Restore last-run times from DB ────────────────────────────────
        self._restore_last_runs()

        # ── Init subsystems ――――――――――――――――――――――――――――――――――――――――――――──
        self._init_subsystems()

        logger.info("🧠 Learning Manager initialized")

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @staticmethod
    def create_table_sql() -> str:
        return """
        CREATE TABLE IF NOT EXISTS learning_runs (
            id SERIAL PRIMARY KEY,
            run_ts TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
            subsystem TEXT NOT NULL,
            cycle_count INTEGER DEFAULT 0,
            duration_ms INTEGER DEFAULT 0,
            status TEXT DEFAULT 'ok',
            result_json TEXT DEFAULT '{}',
            error_text TEXT DEFAULT NULL
        )
        """

    @staticmethod
    def create_indexes_sql() -> list[str]:
        return [
            "CREATE INDEX IF NOT EXISTS idx_learning_runs_subsystem ON learning_runs(subsystem)",
            "CREATE INDEX IF NOT EXISTS idx_learning_runs_ts ON learning_runs(run_ts DESC)",
        ]

    # ------------------------------------------------------------------
    # Initialization helpers
    # ------------------------------------------------------------------

    def _ensure_tables(self) -> None:
        """Create learning_runs table and all subsystem tables."""
        try:
            with self._stats_db._get_conn() as conn:
                # Learning Manager's own table
                conn.execute(self.create_table_sql())
                for idx in self.create_indexes_sql():
                    conn.execute(idx)

                # Subsystem tables — import class-level SQL factories
                from src.utils.signal_scorecard import SignalScorecard
                from src.utils.confidence_calibrator import ConfidenceCalibrator
                from src.utils.ensemble_optimizer import EnsembleOptimizer
                from src.utils.prompt_evolver import PromptEvolver
                from src.utils.auto_wfo import AutoWFO
                from src.utils.finetuning_pipeline import FinetuningPipeline

                for cls in (SignalScorecard, ConfidenceCalibrator, EnsembleOptimizer,
                            PromptEvolver, AutoWFO, FinetuningPipeline):
                    conn.execute(cls.create_table_sql())
                    for idx_sql in cls.create_indexes_sql():
                        conn.execute(idx_sql)

                conn.commit()
        except Exception as e:
            logger.warning(f"ALE table creation (non-fatal): {e}")

    def _restore_last_runs(self) -> None:
        """Load last successful run timestamps from DB."""
        try:
            with self._stats_db._get_conn() as conn:
                for subsystem in self._last_run:
                    row = conn.execute(
                        """
                        SELECT run_ts FROM learning_runs
                        WHERE subsystem = %s AND status = 'ok'
                        ORDER BY run_ts DESC LIMIT 1
                        """,
                        (subsystem,),
                    ).fetchone()
                    if row:
                        ts = datetime.fromisoformat(row["run_ts"].replace("Z", "+00:00"))
                        self._last_run[subsystem] = ts.timestamp()
        except Exception as e:
            logger.debug(f"Could not restore last learning runs: {e}")

    def _init_subsystems(self) -> None:
        """Initialize subsystem instances."""
        try:
            from src.utils.signal_scorecard import SignalScorecard
            from src.utils.confidence_calibrator import ConfidenceCalibrator
            from src.utils.ensemble_optimizer import EnsembleOptimizer
            from src.utils.prompt_evolver import PromptEvolver
            from src.utils.auto_wfo import AutoWFO
            from src.utils.finetuning_pipeline import FinetuningPipeline

            self._scorecard = SignalScorecard(self._stats_db)
            self._calibrator = ConfidenceCalibrator(self._stats_db, self._scorecard)
            self._ensemble = EnsembleOptimizer(
                self._stats_db, self._scorecard, self._audit,
                exchange=self._config.get("trading", {}).get("exchange", "").lower(),
            )
            self._prompt_evolver = PromptEvolver(
                self._stats_db, self._scorecard, self.orch.llm, self._audit
            )
            self._auto_wfo = AutoWFO(
                self._stats_db, self.orch.exchange, self._config,
                None,  # settings_manager — injected later if needed
                self._audit
            )
            self._finetune = FinetuningPipeline(self._stats_db, self._config, self._audit)

            # Load persisted state
            self._calibrator.load_from_db()
            self._ensemble.load_from_db()
            self._prompt_evolver.load_active_supplements()

            logger.info("🧠 ALE subsystems initialized")
        except Exception as e:
            logger.warning(f"ALE subsystem init (non-fatal): {e}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def tick(self, cycle_count: int = 0) -> dict[str, Any]:
        """Run due subsystems. Called once per trading cycle.

        Returns summary of what ran.
        """
        # Global kill-switch
        settings = llm_optimizer.get_settings()
        if not settings.get("learning_enabled", True):
            return {"skipped": True, "reason": "learning_disabled"}

        now = time.time()
        summary: dict[str, Any] = {}

        # ── Scorecard (every cycle) ───────────────────────────────────────
        if self._scorecard and self._due("scorecard", now, _SCORECARD_INTERVAL):
            summary["scorecard"] = await self._run_subsystem(
                "scorecard", cycle_count, self._run_scorecard
            )

        # ── Calibrator (weekly) ───────────────────────────────────────────
        if self._calibrator and self._due("calibrator", now, _CALIBRATOR_INTERVAL):
            summary["calibrator"] = await self._run_subsystem(
                "calibrator", cycle_count, self._run_calibrator
            )

        # ── Ensemble (weekly) ─────────────────────────────────────────────
        if self._ensemble and self._due("ensemble", now, _ENSEMBLE_INTERVAL):
            summary["ensemble"] = await self._run_subsystem(
                "ensemble", cycle_count, self._run_ensemble
            )

        # ── Prompt Evolver (weekly) ───────────────────────────────────────
        if self._prompt_evolver and self._due("prompt_evolver", now, _PROMPT_INTERVAL):
            summary["prompt_evolver"] = await self._run_subsystem(
                "prompt_evolver", cycle_count, self._run_prompt_evolver
            )

        # ── Auto WFO (weekly) ─────────────────────────────────────────────
        if self._auto_wfo and self._due("auto_wfo", now, _WFO_INTERVAL):
            summary["auto_wfo"] = await self._run_subsystem(
                "auto_wfo", cycle_count, self._run_auto_wfo
            )

        # ── Fine-Tuning Pipeline (monthly) ────────────────────────────────
        if self._finetune and self._due("finetune", now, _FINETUNE_INTERVAL):
            summary["finetune"] = await self._run_subsystem(
                "finetune", cycle_count, self._run_finetune
            )

        # ── Quant Attribution (daily) ─────────────────────────────────────
        # Joins decision-time quant snapshots with realised outcomes and
        # turns the result into bounded confidence/sizing adjustments.
        if self._due("quant_attribution", now, _QUANT_ATTRIBUTION_INTERVAL):
            summary["quant_attribution"] = await self._run_subsystem(
                "quant_attribution", cycle_count, self._run_quant_attribution
            )

        return summary

    # ------------------------------------------------------------------
    # Accessor for pipeline integration
    # ------------------------------------------------------------------

    @property
    def scorecard(self) -> Optional[Any]:
        return self._scorecard

    @property
    def calibrator(self) -> Optional[Any]:
        return self._calibrator

    @property
    def ensemble(self) -> Optional[Any]:
        return self._ensemble

    @property
    def prompt_evolver(self) -> Optional[Any]:
        return self._prompt_evolver

    def get_status(self) -> dict[str, Any]:
        """Return learning subsystem status for dashboard."""
        settings = llm_optimizer.get_settings()
        enabled = settings.get("learning_enabled", True)

        status: dict[str, Any] = {
            "enabled": enabled,
            "subsystems": {},
        }

        for name, last_ts in self._last_run.items():
            if last_ts > 0:
                dt = datetime.fromtimestamp(last_ts, tz=timezone.utc)
                status["subsystems"][name] = {
                    "last_run": dt.isoformat(),
                    "seconds_ago": int(time.time() - last_ts),
                }
            else:
                status["subsystems"][name] = {
                    "last_run": None,
                    "seconds_ago": None,
                }

        # Scorecard accuracy snapshot
        if self._scorecard:
            try:
                acc = self._scorecard.get_rolling_accuracy(window_days=7)
                status["accuracy_7d"] = acc
            except Exception:
                pass

        return status

    # ------------------------------------------------------------------
    # Subsystem runners (wrapped by _run_subsystem for logging/timing)
    # ------------------------------------------------------------------

    async def _run_scorecard(self) -> dict:
        """Score recent signals against realized prices."""
        count = self._scorecard.backfill_scores()
        acc = self._scorecard.get_rolling_accuracy(window_days=7)

        # Check for WFO rollbacks while scoring
        if self._auto_wfo:
            self._auto_wfo.check_rollbacks(self._scorecard)

        return {"scored": count, "accuracy_7d": acc}

    async def _run_calibrator(self) -> dict:
        """Retrain calibration models."""
        result = self._calibrator.retrain()
        return result

    async def _run_ensemble(self) -> dict:
        """Update strategy weights from recent outcomes."""
        result = self._ensemble.update_weights()
        return result

    async def _run_prompt_evolver(self) -> dict:
        """Generate new prompt supplements from prediction patterns."""
        result = await self._prompt_evolver.evolve_prompts()
        return result

    async def _run_auto_wfo(self) -> dict:
        """Run walk-forward optimization on active pairs."""
        pairs = list(self.orch.pairs) if hasattr(self.orch, "pairs") else []
        if not pairs:
            return {"skipped": True, "reason": "no_pairs"}
        result = await self._auto_wfo.run_optimization(pairs[:5])  # cap at 5
        return result

    async def _run_finetune(self) -> dict:
        """Export fine-tuning training data."""
        settings = llm_optimizer.get_settings()
        min_examples = settings.get("finetune_min_examples", 50)
        result = self._finetune.curate_and_export(window_days=90)
        return result

    async def _run_quant_attribution(self) -> dict:
        """Run quant-feature attribution + persist a model card export.

        Scope:
          * Joins ``quant_decision_snapshots`` with ``signal_scores`` to
            compute hit-rate / avg-pnl per quant-feature bucket.
          * Translates the result into bounded multiplicative
            adjustments (``derive_learning_adjustments``) that the
            pipeline can consume to adaptively size into / out of
            quant-favoured regimes.
          * Exports the latest QuantAnalytics ModelCard JSON to
            ``data/<exchange>/quant_model_card.json`` so it can be
            archived, diffed and shipped alongside the trained agent.
        Pure read of quant tables; never raises.
        """
        try:
            from src.utils.quant_attribution import (
                attribute_quant_signals,
                derive_learning_adjustments,
            )
            from src.utils.quant_model_card import (
                build_model_card,
                export_to_disk,
            )
        except Exception as e:
            return {"skipped": True, "reason": f"import_failed: {e}"}

        exchange = (
            self._config.get("trading", {}).get("exchange", "") or ""
        ).lower()
        if not exchange:
            return {"skipped": True, "reason": "no_exchange"}

        attribution = attribute_quant_signals(
            self._stats_db, exchange, lookback_days=30,
        )
        adjustments = derive_learning_adjustments(attribution)
        self._latest_quant_attribution = attribution
        self._latest_quant_adjustments = adjustments

        # Export the model card so the artifact is exportable + diffable.
        try:
            import os
            from pathlib import Path
            universe = (
                list(getattr(self.orch, "all_tracked_pairs", []))
                or list(getattr(self.orch, "pairs", []))
            )
            card = build_model_card(
                self._stats_db, exchange, universe=universe,
                attribution=attribution,
            )
            out_dir = Path("data") / exchange
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / "quant_model_card.json"
            export_to_disk(card, str(out_path))
            export_size = os.path.getsize(out_path)
        except Exception as e:
            logger.debug(f"model card export failed: {e}")
            export_size = 0

        return {
            "decisions_total": attribution.get("decisions_total", 0),
            "outcomes_total": attribution.get("outcomes_total", 0),
            "feature_signal_strength": attribution.get("feature_signal_strength", 0.0),
            "adjustments": adjustments,
            "model_card_bytes": export_size,
        }

    @property
    def latest_quant_attribution(self) -> Optional[dict[str, Any]]:
        return self._latest_quant_attribution

    @property
    def latest_quant_adjustments(self) -> Optional[dict[str, Any]]:
        return self._latest_quant_adjustments

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _due(self, subsystem: str, now: float, interval: float) -> bool:
        """Check if a subsystem is due to run."""
        if interval <= 0:
            return True  # every cycle
        return (now - self._last_run.get(subsystem, 0.0)) >= interval

    async def _run_subsystem(
        self, name: str, cycle_count: int, fn
    ) -> dict[str, Any]:
        """Run a subsystem with timing, logging, and error handling.

        While the subsystem runs, a daemon thread bumps the orchestrator's
        cycle watchdog heartbeat every 30s. Long-but-progressing learning
        work (e.g. weekly Auto-WFO running 1000+ backtests, prompt evolver
        making slow LLM calls) would otherwise starve the heartbeat and
        trigger a spurious watchdog-driven container restart, since
        ``LearningManager.tick`` is invoked synchronously from the
        orchestrator's main loop. Genuine hangs in the trading pipeline
        (not in here) still trip the watchdog as intended.
        """
        t0 = time.monotonic()
        stop_evt = threading.Event()
        bump = getattr(self.orch, "bump_heartbeat", None)

        def _heartbeat_pump() -> None:
            # Bump immediately so we don't wait 30s for the first refresh.
            try:
                if bump:
                    bump()
            except Exception:
                pass
            while not stop_evt.wait(_HEARTBEAT_PUMP_INTERVAL_S):
                try:
                    if bump:
                        bump()
                except Exception:
                    pass

        pump_thread: Optional[threading.Thread] = None
        if bump is not None:
            pump_thread = threading.Thread(
                target=_heartbeat_pump,
                name=f"ale-heartbeat-{name}",
                daemon=True,
            )
            pump_thread.start()

        try:
            result = await fn()
            duration_ms = int((time.monotonic() - t0) * 1000)
            self._last_run[name] = time.time()

            self._persist_run(name, cycle_count, duration_ms, "ok", result)
            logger.info(f"🧠 ALE.{name} completed in {duration_ms}ms")
            return {"status": "ok", "duration_ms": duration_ms, **result}

        except Exception as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            err = traceback.format_exc()
            self._persist_run(name, cycle_count, duration_ms, "error", None, str(e))
            logger.warning(f"🧠 ALE.{name} failed ({duration_ms}ms): {e}")
            return {"status": "error", "error": str(e), "duration_ms": duration_ms}
        finally:
            stop_evt.set()
            if pump_thread is not None:
                pump_thread.join(timeout=1.0)
            # Final bump so the next watchdog window starts fresh.
            try:
                if bump:
                    bump()
            except Exception:
                pass

    def _persist_run(
        self, subsystem: str, cycle_count: int,
        duration_ms: int, status: str,
        result: Optional[dict] = None, error_text: Optional[str] = None
    ) -> None:
        """Write a learning run record to the database."""
        import json
        try:
            with self._stats_db._get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO learning_runs
                        (subsystem, cycle_count, duration_ms, status, result_json, error_text)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        subsystem, cycle_count, duration_ms, status,
                        json.dumps(result or {}, default=str),
                        error_text,
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"Failed to persist learning run: {e}")
