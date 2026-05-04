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


# ─── batch update from realised trades ──────────────────────────────────

class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
    def execute(self, sql, params):
        return self
    def fetchall(self):
        return list(self._rows)
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _FakeTradesDB(_FakeStatsDB):
    def __init__(self, rows):
        super().__init__()
        self._rows = rows
    def _get_conn(self):
        return _FakeConn(self._rows)


def test_update_bandit_from_recent_trades_populates_state():
    from src.utils.bandit import update_bandit_from_recent_trades
    rows = [
        {"trade_id": 1, "pair": "BTC-EUR", "pnl": 5.0,
         "reasoning_json": '{"market_condition":"bullish","key_factors":["EMA bullish","MACD bullish"]}'},
        {"trade_id": 2, "pair": "BTC-EUR", "pnl": -2.0,
         "reasoning_json": '{"market_condition":"bearish","key_factors":["RSI overbought"]}'},
        {"trade_id": 3, "pair": "ETH-EUR", "pnl": 1.0,
         "reasoning_json": '{"market_condition":"neutral","key_factors":["Pattern breakout"]}'},
    ]
    db = _FakeTradesDB(rows)
    res = update_bandit_from_recent_trades(db, exchange="coinbase", lookback_days=30)
    assert res["trades"] == 3
    assert res["updates"] >= 3
    # bullish trade with EMA+MACD → ema_crossover bucket got a win.
    s_up = db.get_bandit_state("coinbase", "trending_up")
    assert s_up.get("ema_crossover", {}).get("alpha", 0) > 1.0
    # bearish RSI loss → bollinger_reversion bucket in trending_down got a loss.
    s_dn = db.get_bandit_state("coinbase", "trending_down")
    assert s_dn.get("bollinger_reversion", {}).get("beta", 0) > 1.0
    # pattern breakout → pattern_engine bucket in ranging.
    s_n = db.get_bandit_state("coinbase", "ranging")
    assert s_n.get("pattern_engine", {}).get("alpha", 0) > 1.0


def test_update_bandit_from_recent_trades_empty():
    from src.utils.bandit import update_bandit_from_recent_trades
    db = _FakeTradesDB([])
    res = update_bandit_from_recent_trades(db, exchange="coinbase")
    assert res == {"trades": 0, "updates": 0}
