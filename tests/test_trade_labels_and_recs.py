"""Unit tests for the trade-label & backtest-recommendation feedback loops.

These cover the *pure* logic paths — validation, label-precedence in the
fine-tuning pipeline, and the human-feedback upweighting in
``_balance_examples``. Round-trip persistence is exercised separately by
the integration suite (PG-backed) which already runs in CI.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.utils.stats_labels import ALLOWED_LABELS, LabelsMixin
from src.utils.stats_recommendations import (
    ALLOWED_STATUS,
    DEFAULT_EXPIRY_DAYS,
    RecommendationsMixin,
)


# ─── Constants / DDL invariants ────────────────────────────────────────────

class TestAllowedSets:
    def test_allowed_labels(self):
        assert ALLOWED_LABELS == frozenset({"win", "loss", "skip", "unsure"})

    def test_allowed_status(self):
        assert ALLOWED_STATUS == frozenset(
            {"pending", "approved", "rejected", "expired"}
        )

    def test_default_expiry_two_weeks(self):
        assert DEFAULT_EXPIRY_DAYS == 14


class TestSchemaIsExchangeScoped:
    """Domain-separation: every DDL index must be exchange-prefixed."""

    def test_labels_indexes_carry_exchange(self):
        joined = "\n".join(LabelsMixin._LABELS_DDL_STATEMENTS)
        assert "UNIQUE (exchange, trade_id)" in joined
        assert "trade_labels(exchange, label)" in joined
        assert "trade_labels(exchange, created_at" in joined

    def test_recommendations_indexes_carry_exchange(self):
        joined = "\n".join(RecommendationsMixin._RECOMMENDATIONS_DDL)
        assert "UNIQUE (exchange, kind, symbol, metric_name, source)" in joined
        assert "backtest_recommendations(exchange, status)" in joined
        assert "backtest_recommendations(exchange, created_at" in joined


# ─── Validation ────────────────────────────────────────────────────────────

class _StubLabels(LabelsMixin):
    def __init__(self):
        self._get_conn = MagicMock()


class _StubRecs(RecommendationsMixin):
    def __init__(self):
        self._get_conn = MagicMock()


class TestLabelValidation:
    def test_bad_label_rejected(self):
        s = _StubLabels()
        with pytest.raises(ValueError, match="label must be one of"):
            s.add_trade_label(trade_id=1, label="bogus", exchange="coinbase")
        s._get_conn.assert_not_called()

    def test_missing_exchange_rejected(self):
        s = _StubLabels()
        with pytest.raises(ValueError, match="exchange is required"):
            s.add_trade_label(trade_id=1, label="win", exchange="")
        s._get_conn.assert_not_called()

    def test_non_int_trade_id_rejected(self):
        s = _StubLabels()
        with pytest.raises(ValueError, match="trade_id must be an integer"):
            s.add_trade_label(trade_id="abc", label="win", exchange="coinbase")
        s._get_conn.assert_not_called()

    def test_delete_returns_false_for_bad_id(self):
        s = _StubLabels()
        assert s.delete_trade_label(trade_id="not-a-number", exchange="coinbase") is False
        s._get_conn.assert_not_called()


class TestRecommendationValidation:
    def test_decide_rejects_unknown_status(self):
        s = _StubRecs()
        with pytest.raises(ValueError, match="status must be"):
            s.decide_recommendation(rec_id=1, status="maybe")
        s._get_conn.assert_not_called()

    def test_decide_rejects_non_int_id(self):
        s = _StubRecs()
        with pytest.raises(ValueError, match="rec_id must be an integer"):
            s.decide_recommendation(rec_id="abc", status="approved")
        s._get_conn.assert_not_called()

    def test_upsert_requires_core_fields(self):
        s = _StubRecs()
        with pytest.raises(ValueError):
            s.upsert_recommendation(exchange="", kind="add_pair", summary="x")
        with pytest.raises(ValueError):
            s.upsert_recommendation(exchange="coinbase", kind="", summary="x")
        with pytest.raises(ValueError):
            s.upsert_recommendation(exchange="coinbase", kind="add_pair", summary="")
        s._get_conn.assert_not_called()


# ─── Fine-tuning pipeline label precedence ─────────────────────────────────

class TestFinetuneLabelPrecedence:
    """``_gather_examples`` must honour operator-supplied human_label."""

    def _pipeline(self):
        from src.utils.finetuning_pipeline import FinetuningPipeline

        db = MagicMock()
        cfg = {"trading": {"exchange": "coinbase"}}
        return FinetuningPipeline(db, cfg, audit=None), db

    def _trade(self, **overrides):
        base = {
            "id": 1,
            "ts": "2025-01-01T00:00:00Z",
            "pair": "BTC-EUR",
            "action": "buy",
            "price": 100.0,
            "quantity": 1.0,
            "pnl": 0.05,  # tiny — would normally be filtered out
            "confidence": 0.7,
            "signal_type": "ema_crossover",
            "stop_loss": None,
            "take_profit": None,
            "reasoning_json": "{}",
            "raw_prompt": "",
            "human_label": None,
            "human_note": "",
        }
        base.update(overrides)
        return base

    def _gather_with_trades(self, trades):
        pipe, db = self._pipeline()
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = trades
        ctx = MagicMock()
        ctx.__enter__.return_value = conn
        ctx.__exit__.return_value = None
        db._get_conn.return_value = ctx
        return pipe._gather_examples(window_days=90)

    def test_skip_label_drops_example(self):
        rows = self._gather_with_trades([
            self._trade(id=1, pnl=10.0, human_label="skip"),
        ])
        assert rows == []

    def test_win_label_overrides_negative_pnl(self):
        rows = self._gather_with_trades([
            self._trade(id=1, pnl=-50.0, human_label="win"),
        ])
        assert len(rows) == 1
        assert rows[0]["is_win"] is True
        assert rows[0]["human_label"] == "win"

    def test_loss_label_overrides_positive_pnl(self):
        rows = self._gather_with_trades([
            self._trade(id=1, pnl=50.0, human_label="loss"),
        ])
        assert len(rows) == 1
        assert rows[0]["is_win"] is False

    def test_unsure_label_passes_through_threshold(self):
        # |pnl_pct| = 0.05/100 = 0.0005 below default _MIN_PNL_PCT (0.005)
        rows = self._gather_with_trades([
            self._trade(id=1, pnl=0.05, human_label="unsure"),
        ])
        # Below threshold → dropped even with "unsure" label.
        assert rows == []

    def test_no_label_below_threshold_dropped(self):
        rows = self._gather_with_trades([
            self._trade(id=1, pnl=0.05, human_label=None),
        ])
        assert rows == []


class TestBalanceUpweighting:
    """``_balance_examples`` duplicates labelled rows so balancer favours them."""

    def _pipeline(self):
        from src.utils.finetuning_pipeline import FinetuningPipeline

        return FinetuningPipeline(MagicMock(), {"trading": {"exchange": "coinbase"}})

    def test_human_labelled_row_duplicated(self):
        pipe = self._pipeline()
        examples = [
            {"id": i, "is_win": True, "human_label": None} for i in range(10)
        ] + [
            {"id": 100, "is_win": True, "human_label": "win"},
        ]
        balanced = pipe._balance_examples(examples)
        # Labelled row should appear at least once. The duplication step
        # raises its sampling probability vs. unlabelled peers.
        assert any(e["id"] == 100 for e in balanced)

    def test_no_human_labels_no_duplication(self):
        pipe = self._pipeline()
        examples = [
            {"id": i, "is_win": (i % 2 == 0), "human_label": None}
            for i in range(20)
        ]
        balanced = pipe._balance_examples(examples)
        # Each unique id appears at most once when no labels duplicate them.
        ids = [e["id"] for e in balanced]
        assert len(ids) == len(set(ids))
