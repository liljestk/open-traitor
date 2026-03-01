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

from src.utils.helpers import get_data_dir
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

    def __init__(self, log_dir: str = None):
        if log_dir is None:
            log_dir = get_data_dir()
        self.log_dir = os.path.join(log_dir, "audit")
        os.makedirs(self.log_dir, exist_ok=True)

        self._log_file = os.path.join(self.log_dir, "audit.jsonl")
        self._lock = threading.Lock()
        self._last_hash = self._get_last_hash()
        self._sequence = self._get_sequence()

        logger.info(f"📋 Audit log initialized (seq: {self._sequence})")

    def _get_last_hash(self) -> str:
        """Get the hash of the last entry in the log.

        Reads from the end of the file (O(1) for typical line lengths)
        instead of scanning the entire file.
        """
        if not os.path.exists(self._log_file):
            return "genesis"

        try:
            with open(self._log_file, "rb") as f:
                # Seek to end, then scan backwards for last newline
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return "genesis"
                # Read last chunk (audit lines are typically < 2KB)
                chunk_size = min(size, 4096)
                f.seek(size - chunk_size)
                chunk = f.read().decode("utf-8", errors="replace")
                lines = chunk.strip().split("\n")
                if lines:
                    # M8 fix: Try to parse last line, fall back to earlier lines if corrupted
                    for i in range(len(lines) - 1, -1, -1):
                        try:
                            entry = json.loads(lines[i].strip())
                            if i < len(lines) - 1:
                                logger.warning(
                                    f"⚠️ Audit log: last line corrupted, recovered hash from line -{len(lines) - 1 - i}"
                                )
                            return entry.get("hash", "genesis")
                        except json.JSONDecodeError:
                            continue
                    # All lines in chunk corrupted - this is serious
                    logger.error(
                        "🚨 Audit log corruption detected: unable to recover hash chain. "
                        "Tamper-evidence may be compromised. Starting new chain."
                    )
        except Exception as e:
            logger.error(f"🚨 Audit log read error: {e}. Starting new chain.")

        return "genesis"

    def _get_sequence(self) -> int:
        """Get the current sequence number (L17 fix: fast binary newline count)."""
        if not os.path.exists(self._log_file):
            return 0
        try:
            count = 0
            with open(self._log_file, "rb") as f:
                while chunk := f.read(65536):
                    count += chunk.count(b'\n')
            return count
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

            # Write (fsync to ensure durability for audit integrity)
            try:
                with open(self._log_file, "a") as f:
                    f.write(json.dumps(full_entry, default=str) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
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
        
        H7 fix: File read is done outside the lock to avoid blocking trade logging.
        The audit log is append-only so reading without lock is safe.
        """
        prev_hash = "genesis"
        count = 0
        broken_at = None

        try:
            # H7 fix: Check existence and read file content outside the lock
            if not os.path.exists(self._log_file):
                return {"valid": True, "entries": 0}
            
            # Read file content without holding the lock (append-only file is safe to read)
            with open(self._log_file, "r") as f:
                lines = f.readlines()
            
            # Process verification without blocking writers
            for line_num, line in enumerate(lines, 1):
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
                    # H7: Partial line at end (concurrent write) is expected, not an error
                    # Only treat as broken if it's not the last line
                    if line_num < len(lines):
                        broken_at = line_num
                    break

        except Exception as e:
            return {"valid": False, "error": str(e)}

        if broken_at:
            logger.warning(f"⚠️ Audit chain broken at entry {broken_at}!")
            return {"valid": False, "entries": count, "broken_at": broken_at}

        return {"valid": True, "entries": count}
