"""
Custom indicator computations — convenience wrappers.
"""

from __future__ import annotations

from typing import Optional

from src.analysis.technical import TechnicalAnalyzer


def compute_all_indicators(
    candles: list[dict],
    config: Optional[dict] = None,
) -> dict:
    """
    Convenience function to compute all technical indicators at once.
    Returns a flat dictionary of indicator values and signals.
    """
    analyzer = TechnicalAnalyzer(config)
    return analyzer.analyze(candles)
