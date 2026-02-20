"""
Base Agent class — all specialized agents inherit from this.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Optional

from src.core.llm_client import LLMClient
from src.core.state import TradingState
from src.utils.logger import get_logger


class BaseAgent(ABC):
    """Base class for all trading agents."""

    def __init__(
        self,
        name: str,
        llm: LLMClient,
        state: TradingState,
        config: dict,
    ):
        self.name = name
        self.llm = llm
        self.state = state
        self.config = config
        self.logger = get_logger(f"agent.{name}")
        self._last_run: Optional[datetime] = None
        self._run_count = 0
        self._error_count = 0

        self.logger.info(f"🤖 Agent [{self.name}] initialized")

    @abstractmethod
    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Execute the agent's main logic.

        Args:
            context: Dictionary with relevant data for this agent

        Returns:
            Dictionary with the agent's output/decisions
        """
        ...

    def _update_state(self, result: dict) -> None:
        """Update the shared state with this agent's status."""
        self.state.update_agent_state(self.name, {
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "run_count": self._run_count,
            "error_count": self._error_count,
            "last_result_summary": str(result)[:200],
        })

    async def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        """Execute the agent with error handling and state tracking."""
        self._last_run = datetime.now(timezone.utc)
        self._run_count += 1

        try:
            result = await self.run(context)
            self._update_state(result)
            return result
        except Exception as e:
            self._error_count += 1
            self.logger.error(f"Agent [{self.name}] error: {e}", exc_info=True)
            error_result = {"error": str(e), "agent": self.name}
            self._update_state(error_result)
            return error_result
