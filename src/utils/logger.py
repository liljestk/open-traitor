"""
Logging configuration for Auto-Traitor.
"""

import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.logging import RichHandler

from src.utils.helpers import get_log_dir

_loggers: dict[str, logging.Logger] = {}
_initialized = False

console = Console()


def setup_logger(
    log_level: str = "INFO",
    log_dir: str = None,
    max_file_size_mb: int = 50,
    backup_count: int = 5,
    file_enabled: bool = True,
) -> None:
    """Initialize the logging system."""
    global _initialized
    if _initialized:
        return

    level = getattr(logging, log_level.upper(), logging.INFO)

    # Resolve profile-scoped log directory
    if log_dir is None:
        log_dir = get_log_dir()

    # Create log directory
    if file_enabled:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)

    # Root logger configuration
    root_logger = logging.getLogger("auto_traitor")
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    # Rich console handler (beautiful terminal output)
    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        markup=True,
        rich_tracebacks=True,
        tracebacks_show_locals=True,
    )
    rich_handler.setLevel(level)
    rich_format = logging.Formatter("%(message)s", datefmt="[%X]")
    rich_handler.setFormatter(rich_format)
    root_logger.addHandler(rich_handler)

    # File handler
    if file_enabled:
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = log_path / f"auto_traitor_{today}.log"
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_file_size_mb * 1024 * 1024,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_format = logging.Formatter(
            "%(asctime)s | %(name)-25s | %(levelname)-8s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(file_format)
        root_logger.addHandler(file_handler)

        # Trade-specific log file
        trade_log_file = log_path / f"trades_{today}.log"
        trade_handler = RotatingFileHandler(
            trade_log_file,
            maxBytes=max_file_size_mb * 1024 * 1024,
            backupCount=backup_count,
            encoding="utf-8",
        )
        trade_handler.setLevel(logging.INFO)
        trade_handler.setFormatter(file_format)
        trade_logger = logging.getLogger("auto_traitor.trades")
        trade_logger.addHandler(trade_handler)

    _initialized = True


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the given name under the auto_traitor namespace."""
    full_name = f"auto_traitor.{name}" if not name.startswith("auto_traitor") else name
    if full_name not in _loggers:
        _loggers[full_name] = logging.getLogger(full_name)
    return _loggers[full_name]
