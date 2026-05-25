"""Evidence-weighted strategy governance for live trading decisions.

This module turns the platform's analytics substrate into a bounded per-symbol
policy: posture, sizing multiplier, expected return, horizon, and exit style.
It is deliberately deterministic. Agents may reason creatively, but the live
loop gets one stable policy object that can be audited, tested, and capped by
AbsoluteRules downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in (float("inf"), float("-inf")):
        return default
    return result


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _normalise_pct(value: Any) -> float:
    pct = _safe_float(value, 0.0)
    if abs(pct) > 1.0:
        pct /= 100.0
    return pct


@dataclass(frozen=True)
class StrategyPolicy:
    pair: str
    exchange: str
    posture: str = "watch"
    evidence_score: float = 0.0
    confidence_adjustment: float = 0.0
    size_multiplier: float = 1.0
    stop_multiplier: float = 1.0
    take_profit_multiplier: float = 1.0
    strategy_horizon_days: int = 1
    min_hold_minutes: float = 0.0
    exit_policy: str = "target_or_stop"
    target_gain_pct: float = 0.0
    expected_gross_return_pct: float = 0.0
    expected_net_return_pct: float = 0.0
    thesis: str = ""
    reasons: list[str] = field(default_factory=list)
    invalidation_reasons: list[str] = field(default_factory=list)
    adjustments: dict[str, float | str | int | bool] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "exchange": self.exchange,
            "posture": self.posture,
            "evidence_score": float(self.evidence_score),
            "confidence_adjustment": float(self.confidence_adjustment),
            "size_multiplier": float(self.size_multiplier),
            "stop_multiplier": float(self.stop_multiplier),
            "take_profit_multiplier": float(self.take_profit_multiplier),
            "strategy_horizon_days": int(self.strategy_horizon_days),
            "min_hold_minutes": float(self.min_hold_minutes),
            "exit_policy": self.exit_policy,
            "target_gain_pct": float(self.target_gain_pct),
            "expected_gross_return_pct": float(self.expected_gross_return_pct),
            "expected_net_return_pct": float(self.expected_net_return_pct),
            "thesis": self.thesis,
            "reasons": list(self.reasons),
            "invalidation_reasons": list(self.invalidation_reasons),
            "adjustments": dict(self.adjustments),
        }


def build_strategy_policy(
    *,
    pair: str,
    exchange: str,
    current_price: float,
    config: dict | None = None,
    market_signal: dict | None = None,
    strategy_signals: dict | None = None,
    pattern_signal: dict | None = None,
    regression_factor: dict | None = None,
    cross_asset_signal: dict | None = None,
    news_knn_prior: dict | None = None,
    quant_context: dict | None = None,
    kelly_stats: dict | None = None,
    prediction_accuracy: dict | None = None,
    fee_context: dict | None = None,
    planning_outlook: dict | None = None,
    upcoming_events: list[dict] | None = None,
) -> dict:
    """Return a JSON-safe strategy policy for the live pipeline.

    The scoring is intentionally bounded and conservative. Positive evidence
    can lower confidence thresholds and increase size modestly; negative or
    fee-inadequate evidence can block new buys, but never blocks sells.
    """
    cfg = _as_dict(config)
    gov_cfg = _as_dict(cfg.get("strategy_governance"))
    trading_cfg = _as_dict(cfg.get("trading"))
    risk_cfg = _as_dict(cfg.get("risk"))
    if gov_cfg.get("enabled", True) is False:
        return StrategyPolicy(
            pair=pair,
            exchange=exchange,
            posture="trade",
            evidence_score=0.0,
            size_multiplier=1.0,
            strategy_horizon_days=1,
            min_hold_minutes=float(trading_cfg.get("min_hold_minutes", 0) or 0),
            target_gain_pct=float(risk_cfg.get("take_profit_pct", 0.06) or 0.06),
            expected_gross_return_pct=float(risk_cfg.get("take_profit_pct", 0.06) or 0.06),
            thesis="strategy governance disabled",
            reasons=["strategy_governance disabled"],
        ).to_dict()

    market = _as_dict(market_signal)
    signals = _as_dict(strategy_signals)
    pattern = _as_dict(pattern_signal)
    regression = _as_dict(regression_factor)
    cross_asset = _as_dict(cross_asset_signal)
    news_prior = _as_dict(news_knn_prior)
    quant = _as_dict(quant_context)
    kelly = _as_dict(kelly_stats)
    accuracy = _as_dict(prediction_accuracy)
    fees = _as_dict(fee_context)
    plan = _as_dict(planning_outlook)

    score = 0.0
    reasons: list[str] = []
    invalidations: list[str] = []
    target_candidates: list[float] = []
    horizon_candidates: list[int] = []

    signal_type = str(market.get("signal_type") or market.get("action") or "neutral").lower()
    signal_conf = _clamp(_safe_float(market.get("confidence"), 0.0), 0.0, 1.0)
    if signal_type in {"strong_buy", "buy", "weak_buy"}:
        weight = {"strong_buy": 0.28, "buy": 0.18, "weak_buy": 0.08}[signal_type]
        delta = weight * signal_conf
        score += delta
        reasons.append(f"market {signal_type}@{signal_conf:.0%} +{delta:.2f}")
    elif signal_type in {"strong_sell", "sell", "weak_sell"}:
        weight = {"strong_sell": 0.32, "sell": 0.22, "weak_sell": 0.10}[signal_type]
        delta = weight * signal_conf
        score -= delta
        invalidations.append(f"market {signal_type}@{signal_conf:.0%}")

    suggested_tp = _safe_float(market.get("suggested_take_profit"), 0.0)
    if current_price > 0 and suggested_tp > current_price:
        target_candidates.append((suggested_tp - current_price) / current_price)

    ensemble = _as_dict(signals.get("_ensemble"))
    ensemble_action = str(ensemble.get("action") or "hold").lower()
    ensemble_conf = _clamp(_safe_float(ensemble.get("confidence"), 0.0), 0.0, 1.0)
    ensemble_agreement = _clamp(_safe_float(ensemble.get("agreement"), 0.0), 0.0, 1.0)
    if ensemble_action == "buy":
        delta = 0.30 * ensemble_conf * max(0.5, ensemble_agreement)
        score += delta
        reasons.append(f"ensemble buy@{ensemble_conf:.0%} agreement={ensemble_agreement:.0%} +{delta:.2f}")
    elif ensemble_action == "sell":
        delta = 0.30 * ensemble_conf * max(0.5, ensemble_agreement)
        score -= delta
        invalidations.append(f"ensemble sell@{ensemble_conf:.0%}")

    if pattern.get("available"):
        direction = str(pattern.get("direction") or "neutral").lower()
        confidence = _clamp(_safe_float(pattern.get("confidence"), 0.0), 0.0, 1.0)
        expected_drift = _normalise_pct(
            pattern.get("expected_drift_pct")
            or pattern.get("expected_move_pct")
            or pattern.get("median_forward_return_pct")
        )
        if expected_drift > 0:
            target_candidates.append(expected_drift)
            horizon_candidates.append(_safe_int(pattern.get("horizon_days"), 3) or 3)
        if direction == "bullish":
            delta = 0.24 * confidence
            score += delta
            reasons.append(f"pattern bullish@{confidence:.0%} +{delta:.2f}")
        elif direction == "bearish":
            delta = 0.28 * confidence
            score -= delta
            invalidations.append(f"pattern bearish@{confidence:.0%}")

    if regression.get("applied"):
        factor = _safe_float(regression.get("factor"), 1.0)
        direction = str(regression.get("direction") or "neutral").lower()
        model = _as_dict(regression.get("model"))
        r_squared = _safe_float(model.get("r_squared"), 0.0)
        if factor > 1.0 or direction in {"bullish", "positive"}:
            delta = _clamp((factor - 1.0) * 3.0 + r_squared * 0.20, 0.03, 0.22)
            score += delta
            reasons.append(f"regression bullish factor={factor:.3f} +{delta:.2f}")
        elif factor < 1.0 or direction in {"bearish", "negative"}:
            delta = _clamp((1.0 - factor) * 3.0 + r_squared * 0.20, 0.03, 0.24)
            score -= delta
            invalidations.append(f"regression bearish factor={factor:.3f}")

    cross_direction = str(cross_asset.get("direction") or cross_asset.get("signal") or "neutral").lower()
    cross_conf = _clamp(_safe_float(cross_asset.get("confidence"), 0.0), 0.0, 1.0)
    cross_drift = _normalise_pct(cross_asset.get("expected_drift_pct") or cross_asset.get("expected_move_pct"))
    if cross_drift > 0:
        target_candidates.append(cross_drift)
    if cross_direction in {"bullish", "up", "buy"}:
        delta = 0.14 * cross_conf
        score += delta
        reasons.append(f"cross-asset bullish@{cross_conf:.0%} +{delta:.2f}")
    elif cross_direction in {"bearish", "down", "sell"}:
        delta = 0.16 * cross_conf
        score -= delta
        invalidations.append(f"cross-asset bearish@{cross_conf:.0%}")

    prior_drift = _normalise_pct(news_prior.get("expected_drift_pct") or news_prior.get("mean_forward_return_pct"))
    prior_conf = _clamp(_safe_float(news_prior.get("confidence"), 0.0), 0.0, 1.0)
    if prior_drift > 0:
        target_candidates.append(prior_drift)
        delta = min(0.10, prior_drift * 3.0) * max(0.5, prior_conf)
        score += delta
        reasons.append(f"news prior drift {prior_drift:.1%} +{delta:.2f}")
    elif prior_drift < 0:
        delta = min(0.12, abs(prior_drift) * 3.0) * max(0.5, prior_conf)
        score -= delta
        invalidations.append(f"news prior drift {prior_drift:.1%}")

    win_rate = _clamp(_safe_float(kelly.get("win_rate"), 0.0), 0.0, 1.0)
    samples = _safe_int(kelly.get("sample_size") or kelly.get("samples"), 0)
    sample_weight = _clamp(samples / 30.0, 0.0, 1.0)
    if samples > 0 and win_rate > 0:
        delta = (win_rate - 0.50) * 0.55 * sample_weight
        score += delta
        if delta >= 0:
            reasons.append(f"historical win-rate {win_rate:.0%} n={samples} +{delta:.2f}")
        else:
            invalidations.append(f"historical win-rate {win_rate:.0%} n={samples}")
        avg_win = _safe_float(kelly.get("avg_win"), 0.0)
        avg_loss = _safe_float(kelly.get("avg_loss"), 0.0)
        if avg_win > 0 and avg_loss > 0:
            expectancy = win_rate * avg_win - (1.0 - win_rate) * avg_loss
            if expectancy > 0:
                delta = min(0.12, expectancy / max(avg_win + avg_loss, 1e-9))
                score += delta
                reasons.append(f"positive expectancy +{delta:.2f}")
            else:
                delta = min(0.16, abs(expectancy) / max(avg_win + avg_loss, 1e-9))
                score -= delta
                invalidations.append("negative historical expectancy")

    acc_pct = _safe_float(
        accuracy.get("weighted_accuracy_pct")
        or accuracy.get("accuracy_24h_pct")
        or accuracy.get("accuracy_pct"),
        0.0,
    )
    acc_samples = _safe_int(
        accuracy.get("weighted_sample_count")
        or accuracy.get("evaluated_24h")
        or accuracy.get("samples"),
        0,
    )
    if acc_pct > 0 and acc_samples >= 5:
        acc = _clamp(acc_pct / 100.0, 0.0, 1.0)
        delta = (acc - 0.50) * 0.35 * _clamp(acc_samples / 25.0, 0.2, 1.0)
        score += delta
        if delta >= 0:
            reasons.append(f"prediction accuracy {acc:.0%} n={acc_samples} +{delta:.2f}")
        else:
            invalidations.append(f"prediction accuracy {acc:.0%} n={acc_samples}")

    if plan:
        plan_gain = _normalise_pct(plan.get("gain_pct") or plan.get("expected_move_pct"))
        plan_conf = _clamp(_safe_float(plan.get("confidence"), 0.0), 0.0, 1.0)
        plan_horizon = _safe_int(plan.get("horizon_days"), 0)
        plan_direction = str(plan.get("direction") or "bullish").lower()
        if plan_horizon > 0:
            horizon_candidates.append(plan_horizon)
        if plan_gain > 0:
            target_candidates.append(plan_gain)
        if plan_direction == "bullish" and plan_gain > 0 and plan_conf >= 0.60:
            delta = 0.30 * plan_conf
            score += delta
            reasons.append(f"plan bullish {plan_gain:.1%}/{plan_horizon or '?'}d +{delta:.2f}")
        elif plan_direction == "bearish":
            delta = 0.30 * max(plan_conf, 0.5)
            score -= delta
            invalidations.append("planning outlook bearish")

    factor_alpha = _normalise_pct(quant.get("factor_alpha_annualised"))
    if factor_alpha:
        daily_alpha = factor_alpha / 252.0
        target_candidates.append(max(0.0, daily_alpha * 5.0))
        delta = _clamp(factor_alpha / 0.50, -0.18, 0.18)
        score += delta
        if delta >= 0:
            reasons.append(f"factor alpha {factor_alpha:.1%} +{delta:.2f}")
        else:
            invalidations.append(f"negative factor alpha {factor_alpha:.1%}")

    event_count = 0
    for event in upcoming_events or []:
        importance = _safe_int(event.get("importance") or event.get("severity"), 0)
        if importance >= 2:
            event_count += 1
    if event_count:
        penalty = min(0.16, 0.06 * event_count)
        score -= penalty
        invalidations.append(f"{event_count} near-term high-importance events")

    max_horizon = max(1, _safe_int(gov_cfg.get("max_horizon_days"), 30) or 30)
    if horizon_candidates:
        horizon_days = max(1, min(max_horizon, max(horizon_candidates)))
    elif score >= 0.50:
        horizon_days = min(max_horizon, 7)
    elif score >= 0.28:
        horizon_days = min(max_horizon, 3)
    else:
        horizon_days = 1

    fee_hurdle = max(
        _safe_float(fees.get("min_gain_pct"), 0.0),
        _safe_float(fees.get("breakeven_pct"), 0.0),
    )
    risk_tp = _safe_float(risk_cfg.get("take_profit_pct"), 0.06) or 0.06
    default_by_horizon = (
        max(risk_tp, 0.10) if horizon_days >= 21 else
        max(risk_tp, 0.06) if horizon_days >= 7 else
        max(risk_tp * 0.75, 0.035) if horizon_days >= 3 else
        max(risk_tp * 0.50, 0.02)
    )
    target_gain = max([default_by_horizon, *[x for x in target_candidates if x > 0]])
    if score >= 0.20 and fee_hurdle > 0:
        target_gain = max(target_gain, fee_hurdle * 1.35)
    expected_gross = max(0.0, target_gain)
    expected_net = expected_gross - fee_hurdle

    trade_threshold = _safe_float(gov_cfg.get("trade_score_threshold"), 0.25)
    watch_threshold = _safe_float(gov_cfg.get("watch_score_threshold"), 0.05)
    reduce_threshold = _safe_float(gov_cfg.get("reduce_score_threshold"), -0.18)
    min_expected_net = _safe_float(gov_cfg.get("min_expected_net_return_pct"), 0.003)
    if score < reduce_threshold:
        posture = "block"
    elif score < watch_threshold:
        posture = "reduce"
    elif score < trade_threshold:
        posture = "watch"
    else:
        posture = "trade"
    if expected_net < min_expected_net and posture == "trade":
        posture = "watch"
        invalidations.append(
            f"expected net {expected_net:.1%} below policy target {min_expected_net:.1%}"
        )
    if expected_gross <= fee_hurdle and posture in {"watch", "trade"}:
        posture = "reduce"
        invalidations.append("expected gross return does not clear fee hurdle")

    max_size_mult = _safe_float(gov_cfg.get("max_size_multiplier"), 1.35)
    min_size_mult = _safe_float(gov_cfg.get("min_size_multiplier"), 0.25)
    if posture == "block":
        size_mult = 0.0
        confidence_adj = 0.99
    elif posture == "reduce":
        size_mult = min_size_mult
        confidence_adj = 0.18
    elif posture == "watch":
        size_mult = max(min_size_mult, 0.50 + max(score, 0.0) * 0.25)
        confidence_adj = 0.08
    else:
        size_mult = _clamp(0.85 + score * 0.70, min_size_mult, max_size_mult)
        confidence_adj = -_clamp(score * 0.08, 0.0, 0.05)

    if event_count and size_mult > 0:
        size_mult = min(size_mult, 0.70)
    stop_mult = 1.0
    take_profit_mult = 1.0
    if horizon_days >= 7:
        stop_mult = 1.35
        take_profit_mult = 1.25
        exit_policy = "thesis_trailing"
    elif horizon_days >= 3:
        stop_mult = 1.15
        take_profit_mult = 1.10
        exit_policy = "swing_target_or_trailing"
    else:
        exit_policy = "target_or_stop"

    base_min_hold = _safe_float(trading_cfg.get("min_hold_minutes"), 0.0)
    if horizon_days >= 21:
        policy_min_hold = 24 * 60.0
    elif horizon_days >= 7:
        policy_min_hold = 12 * 60.0
    elif horizon_days >= 3:
        policy_min_hold = 4 * 60.0
    else:
        policy_min_hold = base_min_hold
    max_min_hold = _safe_float(gov_cfg.get("max_min_hold_minutes"), 7 * 24 * 60.0)
    min_hold = min(max(base_min_hold, policy_min_hold), max_min_hold)

    score = _clamp(score, -1.0, 1.0)
    thesis_parts = reasons[:3] if reasons else ["no durable positive evidence"]
    if invalidations:
        thesis_parts.append("risks: " + "; ".join(invalidations[:2]))
    thesis = " | ".join(thesis_parts)[:500]
    adjustments = {
        "fee_hurdle_pct": float(fee_hurdle),
        "min_expected_net_return_pct": float(min_expected_net),
        "event_risk_count": int(event_count),
        "policy_version": "strategy_governance_v1",
    }

    return StrategyPolicy(
        pair=pair,
        exchange=exchange,
        posture=posture,
        evidence_score=round(score, 4),
        confidence_adjustment=round(confidence_adj, 4),
        size_multiplier=round(size_mult, 4),
        stop_multiplier=round(stop_mult, 4),
        take_profit_multiplier=round(take_profit_mult, 4),
        strategy_horizon_days=int(horizon_days),
        min_hold_minutes=float(min_hold),
        exit_policy=exit_policy,
        target_gain_pct=round(target_gain, 6),
        expected_gross_return_pct=round(expected_gross, 6),
        expected_net_return_pct=round(expected_net, 6),
        thesis=thesis,
        reasons=reasons[:8],
        invalidation_reasons=invalidations[:8],
        adjustments=adjustments,
    ).to_dict()


__all__ = ["StrategyPolicy", "build_strategy_policy"]