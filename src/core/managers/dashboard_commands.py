"""
Dashboard HITL (Human-In-The-Loop) command manager.

Handles HMAC-signed commands published to Redis by the trading dashboard
and publishes trailing-stop state for dashboard display.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from src.utils.helpers import format_currency
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.core.orchestrator import Orchestrator

logger = get_logger("core.dashboard_commands")


class DashboardCommandManager:
    """Processes signed HITL commands from the dashboard.

    Wired by the Orchestrator; follows the existing manager pattern
    where ``self.orch`` provides access to all shared runtime state.
    """

    def __init__(self, orch: Orchestrator) -> None:
        self.orch = orch

    # ── Trailing-stop state publishing ───────────────────────────────────

    def publish_trailing_stops(self) -> None:
        """Publish current trailing stop state to Redis for the dashboard."""
        redis = self.orch.redis
        if not redis:
            return
        try:
            stops = self.orch.trailing_stops.get_all_stops()
            import json as _json
            redis.set(
                "trailing_stops:state",
                _json.dumps(stops, default=str),
                ex=300,  # 5 min TTL
            )
        except Exception as e:
            logger.debug(f"Failed to publish trailing stops: {e}")

    # ── Command queue processing ─────────────────────────────────────────

    def process_commands(self) -> None:
        """Check Redis for HITL commands published by the dashboard."""
        redis = self.orch.redis
        if not redis:
            return
        try:
            import json as _json
            # Process up to 5 commands per cycle to avoid blocking
            for _ in range(5):
                raw = redis.lpop("dashboard:commands_queue")
                if not raw:
                    break
                try:
                    cmd = _json.loads(raw)
                except Exception:
                    continue

                valid, reason = self._validate_command(cmd)
                if not valid:
                    logger.warning(f"Rejected dashboard command: {reason}")
                    continue

                action = cmd.get("action")
                pair = cmd.get("pair", "")
                logger.info(f"📥 Dashboard HITL command: {action} for {pair}")

                if action == "liquidate":
                    self._handle_liquidate(pair)
                elif action == "tighten_stop":
                    self._handle_tighten_stop(pair)
                elif action == "pause":
                    self._handle_pause_pair(pair)
                elif action == "add_watchlist_pair":
                    self._handle_add_watchlist_pair(pair)
                elif action == "remove_watchlist_pair":
                    self._handle_remove_watchlist_pair(pair)
                else:
                    logger.warning(f"Unknown dashboard command: {action}")
        except Exception as e:
            logger.warning(f"Dashboard command processing error: {e}")

    # ── Signature verification ───────────────────────────────────────────

    def _validate_command(self, cmd: dict) -> tuple[bool, str]:
        """Validate signature and freshness of dashboard-originated commands."""
        if not isinstance(cmd, dict):
            return False, "payload is not a JSON object"

        orch = self.orch
        if not orch._dashboard_command_signing_key:
            return False, "signing key not configured"

        action = str(cmd.get("action", ""))
        pair = str(cmd.get("pair", ""))
        ts = str(cmd.get("ts", ""))
        source = str(cmd.get("source", ""))
        nonce = str(cmd.get("nonce", ""))
        signature = str(cmd.get("signature", ""))

        if source != "dashboard":
            return False, f"invalid source: {source!r}"

        if not all([action, pair, ts, nonce, signature]):
            return False, "missing required signed fields"

        try:
            normalized_ts = ts.replace("Z", "+00:00")
            parsed_ts = datetime.fromisoformat(normalized_ts)
            if parsed_ts.tzinfo is None:
                parsed_ts = parsed_ts.replace(tzinfo=timezone.utc)
            age_seconds = (
                datetime.now(timezone.utc) - parsed_ts.astimezone(timezone.utc)
            ).total_seconds()
            if age_seconds < -30:
                return False, "timestamp is in the future"
            if age_seconds > orch._dashboard_command_max_age_seconds:
                return False, "timestamp is stale"
        except Exception:
            return False, "invalid timestamp"

        payload = f"{action}|{pair}|{ts}|{source}|{nonce}"
        expected = hmac.new(
            orch._dashboard_command_signing_key.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(signature, expected):
            return False, "invalid signature"

        # Reject replayed nonces
        now = time.monotonic()
        with orch._nonce_lock:
            cutoff = now - orch._dashboard_command_max_age_seconds - 30
            stale = [n for n, t in orch._used_nonces.items() if t < cutoff]
            for n in stale:
                del orch._used_nonces[n]
            if nonce in orch._used_nonces:
                return False, "nonce already used (replay detected)"
            orch._used_nonces[nonce] = now

        return True, "ok"

    # ── Command handlers ─────────────────────────────────────────────────

    def _handle_liquidate(self, pair: str) -> None:
        """Emergency liquidate a position."""
        orch = self.orch
        try:
            price = orch.state.current_prices.get(pair, 0)
            result = orch.executor.close_position_by_pair(
                pair, price, "dashboard_liquidate"
            )
            if result:
                orch.trailing_stops.remove_stop(pair)
                pnl = result.get("pnl", 0)
                msg = (
                    f"🔴 Dashboard liquidation: {pair} at "
                    f"{format_currency(price)} — PnL: {format_currency(pnl or 0)}"
                )
                logger.warning(msg)
                if orch.telegram:
                    orch.telegram.send_alert(msg)
                orch.chat_handler.queue_event(msg)
            else:
                logger.warning(
                    f"Dashboard liquidation: no open position for {pair}"
                )
        except Exception as e:
            logger.error(f"Dashboard liquidation failed for {pair}: {e}")

    def _handle_tighten_stop(self, pair: str) -> None:
        """Move trailing stop to breakeven."""
        orch = self.orch
        try:
            stop = orch.trailing_stops.tighten_to_breakeven(pair)
            if stop:
                entry = stop["entry_price"]
                msg = (
                    f"🎯 Dashboard tightened stop on {pair} to breakeven "
                    f"({format_currency(entry)})"
                )
                logger.info(msg)
                if orch.telegram:
                    orch.telegram.send_alert(msg)
                orch.chat_handler.queue_event(msg)
            else:
                logger.warning(f"No trailing stop found for {pair}")
        except Exception as e:
            logger.error(f"Dashboard tighten-stop failed for {pair}: {e}")

    def _handle_pause_pair(self, pair: str) -> None:
        """Exclude a pair from trading."""
        orch = self.orch
        try:
            from src.utils.settings_manager import load_settings, update_section

            settings = load_settings()
            excluded = settings.get("absolute_rules", {}).get(
                "never_trade_pairs", []
            )
            if pair not in excluded:
                excluded.append(pair)
                # H16 fix: was save_section (doesn't exist), should be update_section
                update_section("absolute_rules", {"never_trade_pairs": excluded})
                orch.rules.never_trade_pairs = set(excluded)
                with orch._pairs_lock:
                    if pair in orch.pairs:
                        orch.pairs = [p for p in orch.pairs if p != pair]
                        orch.all_tracked_pairs = list(
                            set(orch.pairs + orch.watchlist_pairs)
                        )
                msg = f"⏸️ Dashboard paused trading on {pair}"
                logger.info(msg)
                if orch.telegram:
                    orch.telegram.send_alert(msg)
                orch.chat_handler.queue_event(msg)
            else:
                logger.info(f"{pair} already in never_trade list")
        except Exception as e:
            logger.error(f"Dashboard pause-pair failed for {pair}: {e}")

    def _handle_add_watchlist_pair(self, pair: str) -> None:
        """Add a human-followed pair to the live trading pipeline."""
        orch = self.orch
        try:
            with orch._pairs_lock:
                if pair not in orch.watchlist_pairs:
                    orch.watchlist_pairs = orch.watchlist_pairs + [pair]
                orch.all_tracked_pairs = list(set(orch.pairs + orch.watchlist_pairs))
            # Subscribe to real-time prices for crypto profiles
            if orch.ws_feed is not None:
                orch.ws_feed.update_subscriptions(orch.all_tracked_pairs)
            logger.info(f"👁️ Dashboard watchlist add: {pair} now tracked by pipeline")
            if orch.telegram:
                orch.telegram.send_alert(f"👁️ Now tracking {pair} (dashboard watchlist)")
        except Exception as e:
            logger.error(f"Dashboard add-watchlist-pair failed for {pair}: {e}")

    def _handle_remove_watchlist_pair(self, pair: str) -> None:
        """Remove a human-followed pair from the live trading pipeline."""
        orch = self.orch
        try:
            with orch._pairs_lock:
                orch.watchlist_pairs = [p for p in orch.watchlist_pairs if p != pair]
                orch.all_tracked_pairs = list(set(orch.pairs + orch.watchlist_pairs))
            # Update WebSocket subscriptions — but keep pair subscribed if it's still in base pairs
            if orch.ws_feed is not None and pair not in orch.pairs:
                orch.ws_feed.update_subscriptions(orch.all_tracked_pairs)
            logger.info(f"👁️ Dashboard watchlist remove: {pair} removed from pipeline")
        except Exception as e:
            logger.error(f"Dashboard remove-watchlist-pair failed for {pair}: {e}")
