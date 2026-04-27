"""Tests for StrategyBandit (Thompson sampling)."""
import random
from src.utils.bandit import StrategyBandit, _sample_beta


class _FakeStatsDB:
    """In-memory fake StatsDB for bandit tests."""
    def __init__(self):
        # state: {(exchange, regime): {strategy: {alpha, beta, n_pulls}}}
        self._state: dict = {}

    def get_bandit_state(self, exchange, regime):
        return self._state.get((exchange, regime), {}).copy()

    def upsert_bandit(self, *, exchange, regime, strategy, alpha, beta, n_pulls, **_):
        bucket = self._state.setdefault((exchange, regime), {})
        bucket[strategy] = {"alpha": alpha, "beta": beta, "n_pulls": n_pulls}


def test_sample_beta_in_range():
    rng = random.Random(1)
    for _ in range(50):
        x = _sample_beta(2.0, 5.0, rng)
        assert 0.0 <= x <= 1.0


def test_uniform_when_no_state():
    db = _FakeStatsDB()
    b = StrategyBandit(db, exchange="coinbase", rng=random.Random(0))
    w = b.sample_weights("trending_up", ["ema", "bbands", "pattern"])
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert set(w) == {"ema", "bbands", "pattern"}


def test_update_increments_alpha_for_win():
    db = _FakeStatsDB()
    b = StrategyBandit(db, exchange="coinbase")
    b.update("chop", "ema", win_score=1.0)
    s = db.get_bandit_state("coinbase", "chop")
    assert s["ema"]["alpha"] == 2.0  # prior 1 + win 1
    assert s["ema"]["beta"] == 1.0
    assert s["ema"]["n_pulls"] == 1


def test_update_increments_beta_for_loss():
    db = _FakeStatsDB()
    b = StrategyBandit(db, exchange="coinbase")
    b.update("chop", "bbands", win_score=0.0)
    s = db.get_bandit_state("coinbase", "chop")
    assert s["bbands"]["beta"] == 2.0
    assert s["bbands"]["alpha"] == 1.0


def test_winner_gets_more_weight_after_many_wins():
    db = _FakeStatsDB()
    b = StrategyBandit(db, exchange="coinbase", rng=random.Random(42))
    for _ in range(50):
        b.update("trending_up", "winner", win_score=1.0)
    for _ in range(50):
        b.update("trending_up", "loser", win_score=0.0)
    # average over many samples → winner > loser
    totals = {"winner": 0.0, "loser": 0.0}
    for _ in range(200):
        w = b.sample_weights("trending_up", ["winner", "loser"])
        totals["winner"] += w["winner"]
        totals["loser"] += w["loser"]
    assert totals["winner"] > totals["loser"] * 5


def test_requires_exchange():
    import pytest
    with pytest.raises(ValueError):
        StrategyBandit(_FakeStatsDB(), exchange="")
