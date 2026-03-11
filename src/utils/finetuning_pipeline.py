"""
Fine-Tuning Pipeline — Curates instruction-tuning data from trade outcomes.

Monthly batch: reads training JSONL files, curates positive/negative examples,
exports in Ollama and OpenAI fine-tuning formats, and optionally triggers
local fine-tuning via Ollama API.

New DB table: ``finetune_exports``
"""

from __future__ import annotations

import json
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from src.utils.helpers import get_data_dir
from src.utils.logger import get_logger

logger = get_logger("utils.finetuning_pipeline")

# Configuration
_MIN_PNL_PCT = 0.01     # 1% — filter out noise
_MAX_EXAMPLES = 500      # cap per export
_WIN_LOSS_RATIO = 0.6    # target 60% positive examples


class FinetuningPipeline:
    """Curates and exports fine-tuning data from historical trade outcomes.

    Lifecycle:
        pipe = FinetuningPipeline(stats_db, config)
        result = pipe.curate_and_export(window_days=90)
    """

    def __init__(self, stats_db, config: dict, audit=None):
        self._db = stats_db
        self._config = config
        self._audit = audit
        self._export_dir = os.path.join(get_data_dir(), "finetuning")
        os.makedirs(self._export_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    @staticmethod
    def create_table_sql() -> str:
        return """
        CREATE TABLE IF NOT EXISTS finetune_exports (
            id SERIAL PRIMARY KEY,
            export_ts TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
            example_count INTEGER NOT NULL DEFAULT 0,
            win_count INTEGER NOT NULL DEFAULT 0,
            loss_count INTEGER NOT NULL DEFAULT 0,
            file_path TEXT NOT NULL,
            model_target TEXT NOT NULL DEFAULT 'ollama',
            window_days INTEGER NOT NULL DEFAULT 90,
            avg_win_pnl_pct REAL DEFAULT NULL,
            avg_loss_pnl_pct REAL DEFAULT NULL,
            status TEXT DEFAULT 'exported'
        )
        """

    @staticmethod
    def create_indexes_sql() -> list[str]:
        return [
            "CREATE INDEX IF NOT EXISTS idx_finetune_ts ON finetune_exports(export_ts)",
        ]

    # ------------------------------------------------------------------
    # Curation & Export
    # ------------------------------------------------------------------

    def curate_and_export(self, window_days: int = 90) -> dict[str, Any]:
        """Curate training examples and export in multiple formats.

        Returns summary of export.
        """
        # Gather raw data from database
        examples = self._gather_examples(window_days)
        if not examples:
            return {"skipped": True, "reason": "no_examples"}

        # Balance win/loss ratio
        balanced = self._balance_examples(examples)
        if len(balanced) < 10:
            return {"skipped": True, "reason": "too_few_examples", "raw_count": len(examples)}

        # Export in both formats
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        results = {}

        # Ollama-compatible JSONL
        ollama_path = os.path.join(self._export_dir, f"ollama_{ts_str}.jsonl")
        ollama_examples = self._format_ollama(balanced)
        self._write_jsonl(ollama_path, ollama_examples)
        results["ollama"] = {"path": ollama_path, "count": len(ollama_examples)}
        self._record_export(ollama_path, balanced, "ollama", window_days)

        # OpenAI-compatible JSONL
        openai_path = os.path.join(self._export_dir, f"openai_{ts_str}.jsonl")
        openai_examples = self._format_openai(balanced)
        self._write_jsonl(openai_path, openai_examples)
        results["openai"] = {"path": openai_path, "count": len(openai_examples)}
        self._record_export(openai_path, balanced, "openai", window_days)

        win_count = sum(1 for e in balanced if e["is_win"])
        loss_count = len(balanced) - win_count

        logger.info(
            f"📦 Fine-tuning export: {len(balanced)} examples "
            f"({win_count} wins, {loss_count} losses) → {ollama_path}"
        )

        if self._audit:
            self._audit.log("finetune_export", {
                "total_examples": len(balanced),
                "win_count": win_count,
                "loss_count": loss_count,
                "window_days": window_days,
                "ollama_path": ollama_path,
                "openai_path": openai_path,
            })

        return {
            "total_examples": len(balanced),
            "win_count": win_count,
            "loss_count": loss_count,
            "exports": results,
        }

    # ------------------------------------------------------------------
    # Data Gathering
    # ------------------------------------------------------------------

    def _gather_examples(self, window_days: int) -> list[dict]:
        """Gather trade examples with full context from DB."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()

        # Build exchange filter to prevent cross-domain training data bleed
        _exchange = self._config.get("trading", {}).get("exchange", "").lower()
        _exch_frag = ""
        _exch_params: tuple = (cutoff,)
        if _exchange:
            _exch_frag = " AND (t.exchange = %s OR t.exchange = %s)"
            _exch_params = (cutoff, _exchange, f"{_exchange}_paper")

        with self._db._get_conn() as conn:
            # Get trades with associated reasoning and meaningful PnL
            trades = conn.execute(
                f"""
                SELECT t.id, t.ts, t.pair, t.action, t.price, t.quantity,
                       t.pnl, t.confidence, t.signal_type, t.stop_loss,
                       t.take_profit, t.reasoning,
                       ar.reasoning_json, ar.raw_prompt
                FROM trades t
                LEFT JOIN agent_reasoning ar ON ar.trade_id = t.id
                    AND ar.agent_name = 'market_analyst'
                WHERE t.ts >= %s
                  AND t.pnl IS NOT NULL
                  AND t.price > 0
                  AND t.quantity > 0{_exch_frag}
                ORDER BY t.ts ASC
                """,
                _exch_params,
            ).fetchall()

        examples = []
        for trade in trades:
            pnl = trade["pnl"] or 0
            price = trade["price"] or 0
            if price <= 0:
                continue

            # Calculate PnL percentage
            trade_value = price * (trade["quantity"] or 0)
            if trade_value > 0:
                pnl_pct = pnl / trade_value
            else:
                continue

            # Filter: only clear signals (|pnl_pct| > threshold)
            if abs(pnl_pct) < _MIN_PNL_PCT:
                continue

            # Parse reasoning context
            try:
                reasoning = json.loads(trade["reasoning_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                reasoning = {}

            examples.append({
                "trade_id": trade["id"],
                "ts": trade["ts"],
                "pair": trade["pair"],
                "action": trade["action"],
                "price": price,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "confidence": trade["confidence"] or 0,
                "signal_type": trade["signal_type"] or "",
                "stop_loss": trade["stop_loss"],
                "take_profit": trade["take_profit"],
                "reasoning": reasoning,
                "raw_prompt": trade.get("raw_prompt", ""),
                "is_win": pnl > 0,
            })

        return examples

    def _balance_examples(self, examples: list[dict]) -> list[dict]:
        """Balance win/loss ratio and cap total examples."""
        wins = [e for e in examples if e["is_win"]]
        losses = [e for e in examples if not e["is_win"]]

        # Shuffle for variety
        random.shuffle(wins)
        random.shuffle(losses)

        # Target ratio
        total_target = min(len(examples), _MAX_EXAMPLES)
        win_target = int(total_target * _WIN_LOSS_RATIO)
        loss_target = total_target - win_target

        # Take what we can
        selected_wins = wins[:win_target]
        selected_losses = losses[:loss_target]

        # If one side is short, fill from the other
        remaining = total_target - len(selected_wins) - len(selected_losses)
        if remaining > 0:
            if len(selected_wins) < win_target:
                selected_losses.extend(losses[loss_target:loss_target + remaining])
            else:
                selected_wins.extend(wins[win_target:win_target + remaining])

        result = selected_wins + selected_losses
        random.shuffle(result)
        return result[:_MAX_EXAMPLES]

    # ------------------------------------------------------------------
    # Format converters
    # ------------------------------------------------------------------

    def _format_ollama(self, examples: list[dict]) -> list[dict]:
        """Format examples for Ollama fine-tuning (Modelfile TEMPLATE format)."""
        formatted = []
        for ex in examples:
            context = self._build_context(ex)
            if ex["is_win"]:
                # Positive example: the actual decision was correct
                response = self._build_correct_response(ex)
            else:
                # Negative example: reconstruct what should have happened
                response = self._build_corrected_response(ex)

            formatted.append({
                "prompt": context,
                "response": response,
            })
        return formatted

    def _format_openai(self, examples: list[dict]) -> list[dict]:
        """Format examples for OpenAI fine-tuning API."""
        formatted = []
        for ex in examples:
            context = self._build_context(ex)
            if ex["is_win"]:
                response = self._build_correct_response(ex)
            else:
                response = self._build_corrected_response(ex)

            formatted.append({
                "messages": [
                    {"role": "system", "content": "You are a cryptocurrency trading analyst. Analyze market conditions and provide trading recommendations."},
                    {"role": "user", "content": context},
                    {"role": "assistant", "content": response},
                ]
            })
        return formatted

    def _build_context(self, ex: dict) -> str:
        """Build market context from a trade example."""
        reasoning = ex.get("reasoning", {})
        lines = [
            f"Pair: {ex['pair']}",
            f"Price: ${ex['price']:.2f}",
        ]

        # Add technical indicators if available
        tech = reasoning.get("technical", {})
        if tech:
            for key in ("rsi", "macd_histogram", "bb_position", "trend"):
                if key in tech:
                    lines.append(f"{key.upper()}: {tech[key]}")

        # Add sentiment
        sentiment = reasoning.get("sentiment", {})
        if sentiment:
            lines.append(f"Sentiment: {sentiment.get('overall', 'neutral')}")

        # Add market condition
        mc = reasoning.get("market_condition", "")
        if mc:
            lines.append(f"Market Condition: {mc}")

        # Add key factors
        factors = reasoning.get("key_factors", [])
        if factors:
            lines.append(f"Key Factors: {', '.join(factors[:5])}")

        return "\n".join(lines)

    def _build_correct_response(self, ex: dict) -> str:
        """For winning trades: reproduce the correct decision."""
        return json.dumps({
            "action": ex["action"],
            "confidence": ex["confidence"],
            "signal_type": ex["signal_type"],
            "reasoning": f"Correct decision. PnL: {ex['pnl_pct']*100:+.1f}%",
            "stop_loss": ex.get("stop_loss"),
            "take_profit": ex.get("take_profit"),
        })

    def _build_corrected_response(self, ex: dict) -> str:
        """For losing trades: reconstruct the correct decision with hindsight."""
        # Reverse the action
        correct_action = "sell" if ex["action"].lower() == "buy" else "hold"
        reasoning = ex.get("reasoning", {})
        factors = reasoning.get("key_factors", [])

        return json.dumps({
            "action": correct_action,
            "confidence": 0.3,  # low confidence since market was uncertain
            "signal_type": "neutral",
            "reasoning": (
                f"Hindsight correction: the {ex['action']} at ${ex['price']:.2f} "
                f"led to {ex['pnl_pct']*100:+.1f}% loss. "
                f"Warning signs: {', '.join(factors[:3]) if factors else 'insufficient conviction'}. "
                f"Better action: {correct_action}."
            ),
            "stop_loss": None,
            "take_profit": None,
        })

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    @staticmethod
    def _write_jsonl(path: str, records: list[dict]) -> None:
        """Write records to a JSONL file."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, default=str) + "\n")

    def _record_export(
        self, file_path: str, examples: list[dict],
        model_target: str, window_days: int
    ) -> None:
        """Record export metadata in the database."""
        win_count = sum(1 for e in examples if e["is_win"])
        loss_count = len(examples) - win_count

        wins = [e for e in examples if e["is_win"]]
        losses = [e for e in examples if not e["is_win"]]
        avg_win = sum(e["pnl_pct"] for e in wins) / len(wins) if wins else 0
        avg_loss = sum(e["pnl_pct"] for e in losses) / len(losses) if losses else 0

        try:
            with self._db._get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO finetune_exports
                        (example_count, win_count, loss_count, file_path,
                         model_target, window_days, avg_win_pnl_pct, avg_loss_pnl_pct)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        len(examples), win_count, loss_count, file_path,
                        model_target, window_days,
                        round(avg_win * 100, 2), round(avg_loss * 100, 2),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to record fine-tune export: {e}")

    def get_export_history(self, limit: int = 20) -> list[dict]:
        """Get recent export history for dashboard display."""
        try:
            with self._db._get_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT export_ts, example_count, win_count, loss_count,
                           file_path, model_target, window_days, status,
                           avg_win_pnl_pct, avg_loss_pnl_pct
                    FROM finetune_exports
                    ORDER BY export_ts DESC
                    LIMIT %s
                    """,
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
