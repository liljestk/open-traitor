"""Tests for src/utils/decision_drift.py — pure helpers."""
from src.utils.decision_drift import _percentile


def test_percentile_basic():
    s = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
    assert _percentile(s, 0.5) == 0.4 or _percentile(s, 0.5) == 0.5
    assert _percentile(s, 0.0) == 0.0
    assert _percentile(s, 1.0) == 0.9


def test_percentile_empty():
    assert _percentile([], 0.5) is None


def test_percentile_single():
    assert _percentile([0.42], 0.5) == 0.42
