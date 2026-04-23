"""
Walk-Forward Auto-Promotion.

Closes the autonomous self-tuning loop: when a Walk-Forward
Optimization run produces a parameter set that passes robustness
gates (sufficient OOS Sharpe AND Walk-Forward Efficiency above
threshold), the winning params are written to a per-strategy YAML
under ``config/strategies/<name>.live.yaml``. Live strategies
re-read this file on next cycle, no human in the loop.

The promotion is intentionally conservative:
  • Hard gates (WFE, OOS Sharpe, min windows) must all pass.
  • Existing live params are only overwritten if the candidate
    *also* beats the existing params' recorded OOS Sharpe — never
    a regression.
  • A snapshot of the previously promoted params is kept under
    ``config/strategies/<name>.live.prev.yaml`` so self-healing
    (Phase 7) can roll back automatically on circuit-breaker trips.
  • Every promotion writes an audit JSON line under
    ``data/<exchange>/audit/wfo_promotions.jsonl`` for traceability.

Pure file-system operations + YAML; no network, no DB. Safe to call
inside the trading loop.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from src.backtesting.walk_forward import WFOResult
from src.utils.logger import get_logger

logger = get_logger("backtesting.auto_promotion")


@dataclass(frozen=True)
class PromotionConfig:
    """Promotion gating thresholds — defaults are conservative."""
    min_wfe: float = 0.6
    min_oos_sharpe: float = 0.3
    min_windows: int = 4
    require_positive_combined_return: bool = True


@dataclass(frozen=True)
class PromotionDecision:
    promoted: bool
    reason: str
    live_path: Optional[str] = None
    prev_oos_sharpe: Optional[float] = None
    new_oos_sharpe: Optional[float] = None


# ------------------------------------------------------------------ #

class WFOAutoPromoter:
    """Promote WFO-validated parameters into the live config tree."""

    def __init__(
        self,
        config_root: str = "config/strategies",
        audit_root: str = "data",
        gate: Optional[PromotionConfig] = None,
    ) -> None:
        self.config_root = Path(config_root)
        self.audit_root = Path(audit_root)
        self.gate = gate or PromotionConfig()

    # -------------------------------------------------------------- #

    def evaluate(
        self,
        strategy_name: str,
        wfo: WFOResult,
        *,
        exchange: str = "coinbase",
        pair: Optional[str] = None,
        force: bool = False,
    ) -> PromotionDecision:
        """
        Decide and (if eligible) promote the WFO winner.

        Parameters
        ----------
        strategy_name : str
            Used to construct ``<config_root>/<strategy_name>.live.yaml``.
        wfo : WFOResult
            Output of WalkForwardOptimizer.run().
        exchange : str
            Domain (coinbase/ibkr) for audit-log scoping.
        pair : str, optional
            Recorded in the YAML for traceability.
        force : bool
            Bypass the "no regression" check (still respects hard
            gates). Use only for first-time promotion of a brand-new
            strategy.
        """
        # Hard gates first.
        gate = self.gate
        if wfo.total_windows < gate.min_windows:
            return PromotionDecision(False, f"insufficient_windows ({wfo.total_windows} < {gate.min_windows})")
        if wfo.avg_wfe < gate.min_wfe:
            return PromotionDecision(False, f"wfe_below_gate ({wfo.avg_wfe:.3f} < {gate.min_wfe})")
        if wfo.avg_oos_sharpe < gate.min_oos_sharpe:
            return PromotionDecision(False, f"oos_sharpe_below_gate ({wfo.avg_oos_sharpe:.3f} < {gate.min_oos_sharpe})")
        if gate.require_positive_combined_return and wfo.combined_oos_return <= 0:
            return PromotionDecision(False, f"non_positive_combined_oos ({wfo.combined_oos_return:.4f})")
        if not wfo.best_overall_params:
            return PromotionDecision(False, "empty_param_set")

        live_path = self.config_root / f"{strategy_name}.live.yaml"
        prev_path = self.config_root / f"{strategy_name}.live.prev.yaml"

        # No-regression check.
        existing = self._load_existing(live_path)
        prev_oos = None
        if existing and not force:
            prev_oos = float(existing.get("metrics", {}).get("oos_sharpe", -1e9))
            if wfo.avg_oos_sharpe <= prev_oos:
                return PromotionDecision(
                    False,
                    f"no_improvement (new {wfo.avg_oos_sharpe:.3f} <= live {prev_oos:.3f})",
                    live_path=str(live_path),
                    prev_oos_sharpe=prev_oos,
                    new_oos_sharpe=wfo.avg_oos_sharpe,
                )

        # Snapshot the previous live config (for Phase-7 rollback).
        if existing:
            self._safe_write_yaml(prev_path, existing)

        payload = {
            "strategy": strategy_name,
            "exchange": exchange,
            "pair": pair,
            "promoted_at": int(time.time()),
            "params": dict(wfo.best_overall_params),
            "metrics": {
                "avg_wfe": float(wfo.avg_wfe),
                "oos_sharpe": float(wfo.avg_oos_sharpe),
                "oos_return": float(wfo.avg_oos_return),
                "combined_oos_return": float(wfo.combined_oos_return),
                "windows": int(wfo.total_windows),
                "is_robust": bool(wfo.is_robust),
            },
            "gate": {
                "min_wfe": gate.min_wfe,
                "min_oos_sharpe": gate.min_oos_sharpe,
                "min_windows": gate.min_windows,
            },
        }
        self._safe_write_yaml(live_path, payload)
        self._audit(strategy_name, exchange, payload, prev_oos)
        logger.info(
            f"🎓 WFO auto-promoted {strategy_name} ({exchange}): "
            f"WFE={wfo.avg_wfe:.2f} OOS_Sharpe={wfo.avg_oos_sharpe:.2f} → {live_path}"
        )
        return PromotionDecision(
            True,
            "promoted",
            live_path=str(live_path),
            prev_oos_sharpe=prev_oos,
            new_oos_sharpe=wfo.avg_oos_sharpe,
        )

    # -------------------------------------------------------------- #

    def load_live_params(self, strategy_name: str) -> Optional[dict]:
        """Live strategies call this each cycle to pick up promoted params."""
        path = self.config_root / f"{strategy_name}.live.yaml"
        data = self._load_existing(path)
        if not data:
            return None
        return dict(data.get("params") or {})

    def rollback(self, strategy_name: str) -> bool:
        """
        Restore previous-live snapshot. Used by self-healing on
        circuit-breaker trips.
        """
        live_path = self.config_root / f"{strategy_name}.live.yaml"
        prev_path = self.config_root / f"{strategy_name}.live.prev.yaml"
        prev = self._load_existing(prev_path)
        if not prev:
            return False
        self._safe_write_yaml(live_path, prev)
        try:
            os.remove(prev_path)
        except OSError:
            pass
        logger.warning(f"⏪ Rolled back live params for {strategy_name}")
        return True

    # -------------------------------------------------------------- #

    def _load_existing(self, path: Path) -> Optional[dict]:
        if not path.exists():
            return None
        try:
            with open(path, "r") as f:
                data = yaml.safe_load(f) or {}
            return data if isinstance(data, dict) else None
        except Exception as e:  # pragma: no cover - logged
            logger.error(f"Failed to read {path}: {e}")
            return None

    def _safe_write_yaml(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w") as f:
            yaml.safe_dump(data, f, sort_keys=True)
        os.replace(tmp, path)  # atomic on POSIX

    def _audit(
        self,
        strategy_name: str,
        exchange: str,
        payload: dict,
        prev_oos_sharpe: Optional[float],
    ) -> None:
        audit_dir = self.audit_root / exchange / "audit"
        try:
            audit_dir.mkdir(parents=True, exist_ok=True)
            line = {
                "ts": int(time.time()),
                "strategy": strategy_name,
                "exchange": exchange,
                "prev_oos_sharpe": prev_oos_sharpe,
                "new_oos_sharpe": payload["metrics"]["oos_sharpe"],
                "wfe": payload["metrics"]["avg_wfe"],
                "params": payload["params"],
            }
            with open(audit_dir / "wfo_promotions.jsonl", "a") as f:
                f.write(json.dumps(line) + "\n")
        except Exception as e:  # pragma: no cover - audit best-effort
            logger.error(f"WFO promotion audit write failed: {e}")


__all__ = ["WFOAutoPromoter", "PromotionConfig", "PromotionDecision"]
