"""
Audit Log — Immutable append-only log of all critical operations.
Used for debugging, compliance, and trust verification.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

from src.utils.logger import get_logger

logger = get_logger("utils.audit")


class AuditLog:
    """
    Append-only audit log with hash chaining for integrity.

    Each entry includes a hash of the previous entry, creating
    a tamper-evident chain. If any entry is modified, the chain
    breaks and it's detectable.

    Events logged:
      - Trade executions (buy/sell)
      - Rule checks (pass/fail)
      - Approval requests and responses
      - Config changes
      - Authentication events (Telegram)
      - Circuit breaker activations
      - Emergency stops
    """

    def __init__(self, log_dir: str = "data"):
        self.log_dir = os.path.join(log_dir, "audit")
        os.makedirs(self.log_dir, exist_ok=True)

        self._log_file = os.path.join(self.log_dir, "audit.jsonl")
        self._lock = threading.Lock()
        self._last_hash = self._get_last_hash()
        self._sequence = self._get_sequence()

        logger.info(f"📋 Audit log initialized (seq: {self._sequence})")

    def _get_last_hash(self) -> str:
        """Get the hash of the last entry in the log."""
        if not os.path.exists(self._log_file):
            return "genesis"

        try:
            from collections import deque
            with open(self._log_file, "r") as f:
                last_lines = deque(f, maxlen=1)
            if last_lines:
                entry = json.loads(last_lines[0].strip())
                return entry.get("hash", "genesis")
        except (json.JSONDecodeError, Exception):
            pass

        return "genesis"

    def _get_sequence(self) -> int:
        """Get the current sequence number."""
        if not os.path.exists(self._log_file):
            return 0
        try:
            with open(self._log_file, "r") as f:
                return sum(1 for _ in f)
        except Exception:
            return 0

    def _compute_hash(self, data: str, prev_hash: str) -> str:
        """Compute the chain hash for an entry."""
        content = f"{prev_hash}|{data}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()  # full 64-char SHA-256

    def log(
        self,
        event_type: str,
        details: dict[str, Any],
        severity: str = "info",
    ) -> None:
        """
        Log an auditable event.

        Args:
            event_type: Type of event (trade, rule_check, approval, auth, etc.)
            details: Event details
            severity: "info", "warning", "critical"
        """
        with self._lock:
            self._sequence += 1
            timestamp = datetime.now(timezone.utc).isoformat()

            # Build entry
            entry_data = json.dumps({
                "seq": self._sequence,
                "ts": timestamp,
                "type": event_type,
                "severity": severity,
                "details": details,
            }, default=str, separators=(",", ":"))

            # Compute chain hash
            entry_hash = self._compute_hash(entry_data, self._last_hash)

            # Full entry with hash
            full_entry = json.loads(entry_data)
            full_entry["prev_hash"] = self._last_hash
            full_entry["hash"] = entry_hash

            # Write
            try:
                with open(self._log_file, "a") as f:
                    f.write(json.dumps(full_entry, default=str) + "\n")
                self._last_hash = entry_hash
            except Exception as e:
                logger.error(f"Audit log write failed: {e}")

    # Convenience methods
    def log_trade(self, pair: str, action: str, amount: float, price: float, **extra) -> None:
        self.log("trade_execution", {
            "pair": pair, "action": action, "amount": amount, "price": price, **extra,
        })

    def log_rule_check(self, rule: str, passed: bool, details: str = "") -> None:
        self.log("rule_check", {
            "rule": rule, "passed": passed, "details": details,
        }, severity="warning" if not passed else "info")

    def log_approval(self, trade_id: str, approved: bool, approver: str = "") -> None:
        self.log("trade_approval", {
            "trade_id": trade_id, "approved": approved, "approver": approver,
        })

    def log_auth(self, user_id: str, authorized: bool, command: str = "") -> None:
        self.log("authentication", {
            "user_id": user_id, "authorized": authorized, "command": command,
        }, severity="critical" if not authorized else "info")

    def log_circuit_breaker(self, reason: str, drawdown: float) -> None:
        self.log("circuit_breaker", {
            "reason": reason, "drawdown": drawdown,
        }, severity="critical")

    def log_emergency_stop(self, triggered_by: str = "system") -> None:
        self.log("emergency_stop", {
            "triggered_by": triggered_by,
        }, severity="critical")

    def verify_chain(self) -> dict:
        """
        Verify the integrity of the entire audit chain.
        Returns verification result.
        """
        if not os.path.exists(self._log_file):
            return {"valid": True, "entries": 0}

        prev_hash = "genesis"
        count = 0
        broken_at = None

        try:
            with open(self._log_file, "r") as f:
                for line_num, line in enumerate(f, 1):
                    try:
                        entry = json.loads(line.strip())
                        stored_prev = entry.get("prev_hash", "")
                        stored_hash = entry.get("hash", "")

                        # Verify chain
                        if stored_prev != prev_hash:
                            broken_at = line_num
                            break

                        # Verify entry hash
                        entry_copy = {k: v for k, v in entry.items()
                                      if k not in ("prev_hash", "hash")}
                        entry_data = json.dumps(entry_copy, default=str, separators=(",", ":"))
                        expected_hash = self._compute_hash(entry_data, prev_hash)

                        if expected_hash != stored_hash:
                            broken_at = line_num
                            break

                        prev_hash = stored_hash
                        count += 1

                    except json.JSONDecodeError:
                        broken_at = line_num
                        break

        except Exception as e:
            return {"valid": False, "error": str(e)}

        if broken_at:
            logger.warning(f"⚠️ Audit chain broken at entry {broken_at}!")
            return {"valid": False, "entries": count, "broken_at": broken_at}

        return {"valid": True, "entries": count}
