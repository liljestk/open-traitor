import json
import os
import time

from src.utils.logger import get_logger

logger = get_logger("core.state_manager")

class StateManager:
    """Manages the synchronization and reconciliation of trading state."""

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    def _get_redis_key(self, base_key: str) -> str:
        profile = os.environ.get("AUTO_TRAITOR_PROFILE", "")
        return f"{profile}:{base_key}" if profile else base_key

    def sync_to_redis(self):
        """Sync current state to Redis for the dashboard / other services."""
        orch = self.orchestrator
        if not orch.redis:
            return
        try:
            orch.redis.set(
                self._get_redis_key("agent:state"),
                json.dumps(orch.state.to_summary(), default=str),
                ex=300,
            )
            orch.redis.set(
                self._get_redis_key("agent:rules_status"),
                json.dumps(orch.rules.get_status(), default=str),
                ex=300,
            )
            # Persist pending approvals so they survive restarts
            with orch._pending_approvals_lock:
                pending_snapshot = dict(orch._pending_approvals) if orch._pending_approvals else None
            if pending_snapshot:
                orch.redis.set(
                    self._get_redis_key("agent:pending_approvals"),
                    json.dumps(pending_snapshot, default=str),
                    ex=86400,  # 24h TTL
                )
        except Exception as e:
            logger.debug(f"Redis sync failed: {e}")

    def load_pending_approvals(self):
        orch = self.orchestrator
        if not orch.redis:
            return
        try:
            data = orch.redis.get(self._get_redis_key("agent:pending_approvals"))
            if not data:
                return
            loaded: dict = json.loads(data)
            validated: dict = {}
            for trade_id, approval in loaded.items():
                is_swap = approval.get("_is_swap", False)
                if is_swap:
                    # Cycle-3 fix: validate swap approvals too (pair format + amount bounds)
                    from src.utils.security import validate_trading_pair
                    sell_pair = approval.get("sell_pair", "")
                    buy_pair = approval.get("buy_pair", "")
                    try:
                        swap_amount = float(approval.get("quote_amount") or 0)
                    except (TypeError, ValueError):
                        swap_amount = 0.0
                    if (
                        not validate_trading_pair(sell_pair)
                        or not validate_trading_pair(buy_pair)
                        or swap_amount <= 0
                        or swap_amount > orch.rules.max_single_trade * 2
                    ):
                        logger.warning(
                            f"Discarding invalid swap approval from Redis: "
                            f"id={trade_id!r} sell={sell_pair!r} buy={buy_pair!r} "
                            f"amount={swap_amount}"
                        )
                        continue
                    validated[trade_id] = approval
                    continue
                pair = approval.get("pair", "")
                action = approval.get("action", "")
                try:
                    quote_amount = float(approval.get("quote_amount") or approval.get("usd_amount") or 0)
                except (TypeError, ValueError):
                    quote_amount = 0.0
                from src.utils.security import validate_trading_pair
                if (
                    not validate_trading_pair(pair)
                    or action not in ("buy", "sell")
                    or quote_amount <= 0
                    or quote_amount > orch.rules.max_single_trade * 2
                ):
                    logger.warning(
                        f"⚠️ Discarding invalid pending approval from Redis: "
                        f"id={trade_id!r} pair={pair!r} action={action!r} "
                        f"amount={quote_amount}"
                    )
                    continue
                validated[trade_id] = approval
            discarded = len(loaded) - len(validated)
            # M3: assign under lock to avoid race with approve/reject/prune
            with orch._pending_approvals_lock:
                orch._pending_approvals = validated
            logger.info(
                f"Loaded {len(validated)} pending approvals from Redis "
                f"(discarded {discarded} invalid)"
            )
        except Exception as e:
            logger.warning(f"Failed to load pending approvals from Redis: {e}")

    def prune_stale_approvals(self):
        orch = self.orchestrator
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        removed = []
        with orch._pending_approvals_lock:
            for trade_id, approval in list(orch._pending_approvals.items()):
                queued_at_str = approval.get("_queued_at")
                if not queued_at_str:
                    removed.append(trade_id)
                    continue
                try:
                    queued_at = datetime.fromisoformat(queued_at_str)
                    age_seconds = (now - queued_at).total_seconds()
                    # 1 hour TTL for pending approvals
                    if age_seconds > 3600:
                        removed.append(trade_id)
                except Exception:
                    removed.append(trade_id)

            for trade_id in removed:
                del orch._pending_approvals[trade_id]

        if removed:
            logger.info(f"Pruned {len(removed)} stale pending approvals")
