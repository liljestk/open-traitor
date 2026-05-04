"""
Thompson-sampling bandit for online strategy weighting.

Each (exchange, regime, strategy) cell maintains a Beta(alpha, beta) posterior
over the probability that picking this strategy yields a win in this regime.

Update rule (per closed trade):
    alpha += win_score      # win_score ∈ [0, 1]
    beta  += 1 - win_score

Sampling:
    sample θ_s ~ Beta(α_s, β_s) per strategy
    weight_s = θ_s / Σ θ
    Returned weights are dict[str, float] summing to 1.0.

Falls back to uniform weights if no posterior data.

Domain isolation: every read/write goes through ``StatsDB`` with explicit
``exchange`` so coinbase/ibkr never share state.
"""

from __future__ import annotations

import random
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("bandit")


def _sample_beta(alpha: float, beta: float, rng: Optional[random.Random] = None) -> float:
    """Stdlib-only Beta sample via gamma ratio (avoids numpy dep at import time)."""
    rng = rng or random
    a = max(float(alpha), 1e-6)
    b = max(float(beta), 1e-6)
    x = rng.gammavariate(a, 1.0)
    y = rng.gammavariate(b, 1.0)
    s = x + y
    return x / s if s > 0 else 0.5


class StrategyBandit:
    """Thompson-sampling allocator over strategy weights, per-regime.

    Usage::

        bandit = StrategyBandit(stats_db, exchange="coinbase")
        weights = bandit.sample_weights("trending_up", strategies=["ema", "bbands", "pattern"])
        # ... after trade closes ...
        bandit.update("trending_up", "ema", win_score=1.0 if pnl > 0 else 0.0)
    """

    def __init__(
        self,
        stats_db,
        *,
        exchange: str,
        rng: Optional[random.Random] = None,
        prior_alpha: float = 1.0,
        prior_beta: float = 1.0,
    ):
        if not exchange:
            raise ValueError("StrategyBandit requires explicit exchange")
        self.db = stats_db
        self.exchange = exchange
        self._rng = rng or random.Random()
        self._prior = (float(prior_alpha), float(prior_beta))

    # ── core ──────────────────────────────────────────────────────────────

    def sample_weights(
        self, regime: str, strategies: list[str]
    ) -> dict[str, float]:
        """Sample per-strategy weights from current posteriors.

        Strategies with no recorded outcomes get uniform-prior Beta(1, 1)
        which exposes them to exploration (mean 0.5).
        """
        if not strategies:
            return {}
        regime_key = (regime or "unknown").lower()
        try:
            state = self.db.get_bandit_state(self.exchange, regime_key)
        except Exception as e:
            logger.warning(f"bandit: state read failed ({e}); uniform fallback")
            n = len(strategies)
            return {s: 1.0 / n for s in strategies}

        samples: dict[str, float] = {}
        for s in strategies:
            row = state.get(s) or {}
            a = float(row.get("alpha") or self._prior[0])
            b = float(row.get("beta") or self._prior[1])
            samples[s] = _sample_beta(a, b, self._rng)

        total = sum(samples.values())
        if total <= 0:
            n = len(strategies)
            return {s: 1.0 / n for s in strategies}
        return {s: v / total for s, v in samples.items()}

    def update(
        self, regime: str, strategy: str, *, win_score: float
    ) -> None:
        """Apply a single observation. ``win_score`` ∈ [0, 1]."""
        regime_key = (regime or "unknown").lower()
        ws = max(0.0, min(1.0, float(win_score)))
        try:
            state = self.db.get_bandit_state(self.exchange, regime_key)
        except Exception as e:
            logger.warning(f"bandit: update state read failed ({e})")
            return
        row = state.get(strategy) or {}
        a = float(row.get("alpha") or self._prior[0]) + ws
        b = float(row.get("beta") or self._prior[1]) + (1.0 - ws)
        n = int(row.get("n_pulls") or 0) + 1
        try:
            self.db.upsert_bandit(
                exchange=self.exchange,
                regime=regime_key,
                strategy=strategy,
                alpha=a,
                beta=b,
                n_pulls=n,
            )
        except Exception as e:
            logger.warning(f"bandit: upsert failed ({e})")

    def expected_weight(
        self, regime: str, strategies: list[str]
    ) -> dict[str, float]:
        """Posterior-mean weights (deterministic; useful for dashboards)."""
        if not strategies:
            return {}
        regime_key = (regime or "unknown").lower()
        try:
            state = self.db.get_bandit_state(self.exchange, regime_key)
        except Exception:
            state = {}
        means: dict[str, float] = {}
        for s in strategies:
            row = state.get(s) or {}
            a = float(row.get("alpha") or self._prior[0])
            b = float(row.get("beta") or self._prior[1])
            means[s] = a / (a + b) if (a + b) > 0 else 0.5
        total = sum(means.values())
        if total <= 0:
            n = len(strategies)
            return {s: 1.0 / n for s in strategies}
        return {s: v / total for s, v in means.items()}


# ─── Batch update from realised trades ───────────────────────────────────
#
# Map technical signal tokens (as emitted by the market_analyst in
# ``key_factors``) to the ensemble strategy buckets the bandit posteriors
# are keyed on. Multiple tokens can map to the same bucket — that is by
# design (e.g. EMA + MACD both contribute to trend).
_FACTOR_TO_STRATEGY: dict[str, str] = {
    "ema": "ema_crossover",
    "macd": "ema_crossover",
    "trend": "ema_crossover",
    "bollinger": "bollinger_reversion",
    "bbands": "bollinger_reversion",
    "rsi": "bollinger_reversion",
    "stoch": "bollinger_reversion",
    "pattern": "pattern_engine",
    "candlestick": "pattern_engine",
    "breakout": "pattern_engine",
    "catalyst": "pattern_engine",
    "event": "pattern_engine",
}


def _normalise_regime(condition: str | None) -> str:
    """Bucket fine-grained ``market_condition`` strings into bandit regimes."""
    c = (condition or "").lower().replace("-", "_").replace(" ", "_")
    if not c:
        return "unknown"
    if "bull" in c or "up" in c:
        return "trending_up"
    if "bear" in c or "down" in c:
        return "trending_down"
    if "volatile" in c or "choppy" in c:
        return "volatile"
    if "neutral" in c or "side" in c or "range" in c:
        return "ranging"
    return c


def update_bandit_from_recent_trades(
    db, *, exchange: str, lookback_days: int = 30, limit: int = 500
) -> dict:
    """Replay closed trades into the (regime, strategy) bandit posteriors.

    Idempotency: scans ``trades`` JOIN ``agent_reasoning`` (market_analyst)
    for trades that have a realised ``pnl`` and have not yet been counted
    in ``bandit_state``. Each closed trade contributes ``win_score`` ∈ {0,1}
    to every strategy bucket implicated by the analyst's ``key_factors``.

    Best-effort: failures are logged and the routine returns a partial
    summary instead of raising.
    """
    import json
    from datetime import datetime, timedelta, timezone

    since = datetime.now(timezone.utc) - timedelta(days=int(lookback_days))
    sql = (
        "SELECT b.id AS trade_id, b.pair, agg.realised_pnl AS pnl, "
        "       r.reasoning_json "
        "FROM trades b "
        "JOIN agent_reasoning r "
        "  ON r.trade_id = b.id "
        "  AND r.exchange = b.exchange "
        "  AND r.agent_name = 'market_analyst' "
        "JOIN ( "
        "    SELECT b2.id AS buy_id, SUM(s.pnl) AS realised_pnl "
        "    FROM trades b2 "
        "    JOIN trades s "
        "      ON s.exchange = b2.exchange "
        "     AND s.pair = b2.pair "
        "     AND s.action = 'sell' "
        "     AND s.pnl IS NOT NULL "
        "     AND s.ts > b2.ts "
        "    WHERE b2.exchange = %s "
        "      AND b2.action = 'buy' "
        "      AND b2.ts >= %s "
        "    GROUP BY b2.id "
        ") agg ON agg.buy_id = b.id "
        "WHERE b.exchange = %s "
        "ORDER BY b.id ASC LIMIT %s"
    )
    try:
        with db._get_conn() as conn:
            rows = conn.execute(
                sql,
                (exchange, since.isoformat(), exchange, int(limit)),
            ).fetchall()
    except Exception as e:
        logger.warning(f"bandit_update: fetch failed: {e}")
        return {"trades": 0, "updates": 0, "error": str(e)}

    if not rows:
        return {"trades": 0, "updates": 0}

    bandit = StrategyBandit(db, exchange=exchange)
    seen: set[int] = set()
    updates = 0
    for row in rows:
        r = dict(row)
        tid = int(r.get("trade_id") or 0)
        if tid in seen:
            continue
        seen.add(tid)
        try:
            j = json.loads(r["reasoning_json"]) if r.get("reasoning_json") else {}
        except Exception:
            j = {}
        regime = _normalise_regime(j.get("market_condition"))
        try:
            pnl = float(r.get("pnl") or 0.0)
        except (TypeError, ValueError):
            continue
        win = 1.0 if pnl > 0 else 0.0

        # Identify implicated strategy buckets from key_factors. Fall back
        # to all three so the trade still contributes to every posterior.
        strategies: set[str] = set()
        for fac in (j.get("key_factors") or []):
            tok = str(fac).split()[0].lower() if fac else ""
            mapped = _FACTOR_TO_STRATEGY.get(tok)
            if mapped:
                strategies.add(mapped)
        if not strategies:
            strategies = {"ema_crossover", "bollinger_reversion", "pattern_engine"}

        for strat in strategies:
            try:
                bandit.update(regime, strat, win_score=win)
                updates += 1
            except Exception as e:
                logger.debug(f"bandit_update: {strat}/{regime} failed: {e}")

    logger.info(
        f"bandit_update: {exchange} trades={len(seen)} posteriors_updated={updates}"
    )
    return {"trades": len(seen), "updates": updates}
