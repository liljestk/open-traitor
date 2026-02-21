"""
Trade Journal — Persists every decision for review and learning.
Records trades, holds, rejections, and all decision context.
"""

from __future__ import annotations

import csv
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from src.utils.helpers import get_data_dir
from src.utils.logger import get_logger

logger = get_logger("utils.journal")


class TradeJournal:
    """
    Persistent trade journal that logs every decision the agent makes.

    Creates two files:
      - decisions.jsonl  (machine-readable, every decision with full context)
      - trades.csv       (human-readable summary of executed trades)
    """

    def __init__(self, data_dir: str = None):
        if data_dir is None:
            data_dir = get_data_dir()
        self.data_dir = data_dir
        self.journal_dir = os.path.join(data_dir, "journal")
        os.makedirs(self.journal_dir, exist_ok=True)

        self._decisions_file = os.path.join(self.journal_dir, "decisions.jsonl")
        self._trades_file = os.path.join(self.journal_dir, "trades.csv")
        self._lock = threading.Lock()

        # Initialize CSV if it doesn't exist
        if not os.path.exists(self._trades_file):
            self._init_csv()

        logger.info(f"📓 Trade journal initialized at {self.journal_dir}")

    def _init_csv(self) -> None:
        """Initialize the trades CSV with headers."""
        with open(self._trades_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "pair", "action", "quantity", "price",
                "quote_amount", "fee", "stop_loss", "take_profit",
                "confidence", "signal_type", "reasoning",
                "fear_greed", "rsi", "macd_signal",
            ])

    def log_decision(
        self,
        decision_type: str,
        pair: str,
        action: str,
        context: dict,
        reasoning: str = "",
    ) -> None:
        """
        Log any decision (trade, hold, rejection, etc.)

        Args:
            decision_type: "trade_executed", "trade_rejected", "hold", "approval_requested"
            pair: Trading pair (e.g., "BTC-USD")
            action: "buy", "sell", "hold"
            context: Full decision context (signal, indicators, etc.)
            reasoning: Human-readable reasoning
        """
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": decision_type,
            "pair": pair,
            "action": action,
            "reasoning": reasoning,
            "context": context,
        }

        with self._lock:
            try:
                with open(self._decisions_file, "a") as f:
                    f.write(json.dumps(entry, default=str) + "\n")
            except Exception as e:
                logger.error(f"Failed to write journal entry: {e}")

    def log_trade(
        self,
        pair: str,
        action: str,
        quantity: float,
        price: float,
        quote_amount: float,
        fee: float = 0.0,
        stop_loss: float = 0.0,
        take_profit: float = 0.0,
        confidence: float = 0.0,
        signal_type: str = "",
        reasoning: str = "",
        fear_greed: int = 0,
        rsi: float = 0.0,
        macd_signal: str = "",
    ) -> None:
        """Log an executed trade to both JSONL and CSV."""
        # Log to JSONL
        self.log_decision(
            decision_type="trade_executed",
            pair=pair,
            action=action,
            context={
                "quantity": quantity,
                "price": price,
                "quote_amount": quote_amount,
                "fee": fee,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "confidence": confidence,
                "signal_type": signal_type,
                "fear_greed": fear_greed,
                "rsi": rsi,
                "macd_signal": macd_signal,
            },
            reasoning=reasoning,
        )

        # Log to CSV
        with self._lock:
            try:
                with open(self._trades_file, "a", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        datetime.now(timezone.utc).isoformat(),
                        pair, action, quantity, price, quote_amount, fee,
                        stop_loss, take_profit, confidence, signal_type,
                        reasoning[:200],  # Truncate for CSV
                        fear_greed, rsi, macd_signal,
                    ])
            except Exception as e:
                logger.error(f"Failed to write trade to CSV: {e}")

    def get_stats(self, days: int = 7) -> dict:
        """Get journal statistics for the last N days."""
        from datetime import timedelta

        stats = {
            "total_decisions": 0,
            "trades_executed": 0,
            "trades_rejected": 0,
            "holds": 0,
            "by_pair": {},
        }

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        try:
            if not os.path.exists(self._decisions_file):
                return stats

            with open(self._decisions_file, "r") as f:
                for line in f:
                    try:
                        entry = json.loads(line.strip())

                        # Filter by date
                        ts_str = entry.get("timestamp", "")
                        if ts_str:
                            try:
                                ts = datetime.fromisoformat(ts_str)
                                if ts < cutoff:
                                    continue
                            except (ValueError, TypeError):
                                pass

                        stats["total_decisions"] += 1

                        dtype = entry.get("type", "")
                        if dtype == "trade_executed":
                            stats["trades_executed"] += 1
                        elif dtype == "trade_rejected":
                            stats["trades_rejected"] += 1
                        elif dtype == "hold":
                            stats["holds"] += 1

                        pair = entry.get("pair", "unknown")
                        if pair not in stats["by_pair"]:
                            stats["by_pair"][pair] = 0
                        stats["by_pair"][pair] += 1

                    except json.JSONDecodeError:
                        continue

        except Exception as e:
            logger.error(f"Failed to read journal stats: {e}")

        return stats

    def get_recent_decisions(self, count: int = 10) -> list[dict]:
        """Get the most recent N decisions."""
        decisions = []
        try:
            if not os.path.exists(self._decisions_file):
                return []

            with open(self._decisions_file, "r") as f:
                lines = f.readlines()

            for line in lines[-count:]:
                try:
                    decisions.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    continue

        except Exception as e:
            logger.error(f"Failed to read recent decisions: {e}")

        return decisions
