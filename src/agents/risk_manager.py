"""
Risk Manager Agent — Validates and adjusts trade proposals.
Applies position sizing (Kelly Criterion), enforces rules,
manages stop-losses, and accounts for portfolio correlation.
"""

from __future__ import annotations

import math
import os
from typing import Any

from src.agents.base_agent import BaseAgent
from src.core.rules import AbsoluteRules
from src.core.portfolio_scaler import PortfolioScaler
from src.core.vol_target import vol_target_multiplier
from src.models.trade import TradeAction
from src.utils.logger import get_logger

logger = get_logger("agent.risk_manager")


class RiskManagerAgent(BaseAgent):
    """
    Validates trade proposals against absolute rules and risk parameters.
    This agent has the final say before a trade reaches the executor.

    Position sizing hierarchy:
      1. Kelly Criterion (half-Kelly) based on historical win rate
         — strong signals can override with their signal-type-specific win rate
      2. ATR volatility adjustment
      3. Correlation penalty for correlated positions
      4. Signal-strength multiplier (strong_buy=1.0×, buy=0.8×, weak_buy=0.6×)
      5. Strong-signal floor (prevents near-zero sizes for new/poor-history assets)
      6. Cap by max_position_pct config
    """

    # Position size multipliers by signal type.
    # Strong signals get full allocation; weaker signals are sized proportionally smaller.
    _SIGNAL_STRENGTH_MULT: dict[str, float] = {
        "strong_buy":  1.00,
        "buy":         0.80,
        "weak_buy":    0.60,
        "strong_sell": 1.00,
        "sell":        0.80,
        "weak_sell":   0.60,
    }
    _STRONG_SIGNAL_TYPES: frozenset[str] = frozenset({"strong_buy", "strong_sell"})

    def __init__(self, llm, state, config, rules: AbsoluteRules,
                 portfolio_scaler: PortfolioScaler | None = None):
        super().__init__("risk_manager", llm, state, config)
        self.rules = rules
        self.scaler = portfolio_scaler
        self.risk_config = config.get("risk", {})
        self.trading_config = config.get("trading", {})
        self.stop_loss_pct = self.risk_config.get("stop_loss_pct", 0.03)
        self.take_profit_pct = self.risk_config.get("take_profit_pct", 0.06)
        # P2: configurable ATR multipliers and floor/ceiling bands. The floor
        # prevents absurdly-tight stops during low-vol regimes; the ceiling
        # caps stop-loss distance during vol shocks so one bad candle can't
        # wipe out risk budget.
        self.atr_stop_mult = float(self.risk_config.get("atr_stop_mult", 2.0))
        self.atr_tp_mult = float(self.risk_config.get("atr_tp_mult", 3.0))
        self.atr_stop_floor_pct = float(self.risk_config.get("atr_stop_floor_pct", 0.01))
        self.atr_stop_ceiling_pct = float(self.risk_config.get("atr_stop_ceiling_pct", 0.10))
        self.use_kelly = self.risk_config.get("use_kelly_criterion", True)
        self.kelly_fraction = self.risk_config.get("kelly_fraction", 0.5)  # Half-Kelly
        self.use_correlation_penalty = self.risk_config.get("use_correlation_penalty", True)
        self.correlation_threshold = self.risk_config.get("correlation_threshold", 0.7)
        self._style_modifiers: set[str] = set(
            self.trading_config.get("style_modifiers", [])
        )

    def _compute_kelly_size(
        self,
        portfolio_value: float,
        win_rate: float = 0.0,
        avg_win: float = 0.0,
        avg_loss: float = 0.0,
    ) -> float:
        """
        Compute Half-Kelly position size.

        Kelly formula: f* = (p * b - q) / b
          where p = win probability, q = 1-p, b = avg_win / avg_loss

        We use half-Kelly (0.5 * f*) for safety — reduces variance significantly
        while retaining ~75% of full-Kelly growth rate.

        Returns maximum position size as a fraction of portfolio.
        """
        if win_rate <= 0 or avg_win <= 0 or avg_loss <= 0:
            # Insufficient data → fallback to config max
            return self.risk_config.get("max_position_pct", 0.05)

        p = min(max(win_rate, 0.01), 0.99)
        q = 1 - p
        b = avg_win / avg_loss  # Win/loss ratio

        kelly_f = (p * b - q) / b

        if kelly_f <= 0:
            # Kelly says don't bet (negative edge)
            self.logger.info(
                f"Kelly: negative edge (f*={kelly_f:.4f}, win_rate={p:.2f}, "
                f"W/L ratio={b:.2f}) — minimum position"
            )
            return 0.01  # Minimum 1% if we still proceed

        # Apply fraction (half-Kelly by default)
        position_frac = kelly_f * self.kelly_fraction

        # Cap at tier-aware max (caller may further cap via effective_max_position_pct)
        config_max = self.risk_config.get("max_position_pct", 0.05)
        tier_max = self.scaler.tier.max_position_pct if self.scaler else config_max
        max_pct = min(config_max, tier_max)  # Use the stricter of config/tier
        position_frac = min(position_frac, max_pct)

        self.logger.info(
            f"Kelly: f*={kelly_f:.4f}, half-Kelly={kelly_f * self.kelly_fraction:.4f}, "
            f"capped={position_frac:.4f} | "
            f"win_rate={p:.2f}, W/L={b:.2f}"
        )

        return position_frac

    def _read_news_bias(self) -> float:
        """Read the per-profile news_bias multiplier written by NewsReflex.
        Returns 1.0 (neutral) on any error or if file is missing/stale."""
        try:
            import json
            import os
            import time
            from pathlib import Path

            profile = (
                os.environ.get("AUTO_TRAITOR_PROFILE")
                or self.trading_config.get("exchange", "default")
            ).lower()
            path = Path("data") / profile / "news_bias.json"
            if not path.exists():
                return 1.0
            # Stale guard: ignore biases older than 6h
            try:
                age = time.time() - path.stat().st_mtime
                if age > 6 * 3600:
                    return 1.0
            except Exception:
                pass
            data = json.loads(path.read_text())
            return float(data.get("bias", 1.0))
        except Exception as e:
            self.logger.debug(f"news_bias read failed: {e}")
            return 1.0

    def _compute_correlation_penalty(
        self,
        pair: str,
        correlation_matrix: dict[str, dict[str, float]] | None = None,
    ) -> float:
        """
        Reduce position size if the new asset is highly correlated with
        existing open positions.

        Returns a multiplier between 0.5 and 1.0.
        """
        if not self.use_correlation_penalty or not correlation_matrix:
            return 1.0

        base_currency = pair.split("-")[0] if "-" in pair else pair
        open_bases = set()
        for trade in self.state.get_open_trades():
            if trade.action == TradeAction.BUY:
                open_base = trade.pair.split("-")[0] if "-" in trade.pair else trade.pair
                open_bases.add(open_base)

        if not open_bases:
            return 1.0

        max_corr = 0.0
        for open_base in open_bases:
            # Check correlation between new asset and each existing position
            corr = 0.0
            if base_currency in correlation_matrix:
                for existing_pair_key in correlation_matrix[base_currency]:
                    # H13: exact match to avoid substring collision (e.g. BTC vs WBTC)
                    epk_base = existing_pair_key.split("-")[0] if "-" in existing_pair_key else existing_pair_key
                    if open_base == epk_base:
                        corr = abs(correlation_matrix[base_currency].get(existing_pair_key, 0))
                        break
            # Also check reverse direction
            for existing_pair_key in correlation_matrix:
                epk_base = existing_pair_key.split("-")[0] if "-" in existing_pair_key else existing_pair_key
                if open_base == epk_base:
                    corr = max(corr, abs(
                        correlation_matrix.get(existing_pair_key, {}).get(base_currency, 0)
                    ))

            max_corr = max(max_corr, corr)

        if max_corr >= self.correlation_threshold:
            # High correlation: reduce position by up to 50%
            denom = 1.0 - self.correlation_threshold
            if denom <= 0:
                # correlation_threshold >= 1.0: treat as max penalty
                penalty = 0.5
            else:
                penalty = 1.0 - (max_corr - self.correlation_threshold) / denom * 0.5
                penalty = max(0.5, min(1.0, penalty))
            self.logger.info(
                f"Correlation penalty: {pair} has {max_corr:.2f} corr with open positions → "
                f"size multiplier={penalty:.2f}"
            )
            return penalty

        return 1.0

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Validate and potentially adjust a trade proposal.

        Context expected:
            - proposal: dict (from StrategistAgent)
            - portfolio_value: float
            - cash_balance: float
            - cycle_id: str (optional, for reasoning persistence)
            - stats_db: StatsDB instance (optional)
            - trace_ctx: TraceContext (optional, for Langfuse tracing)
            - win_rate: float (optional, from StatsDB for Kelly sizing)
            - avg_win: float (optional, average winning trade %)
            - avg_loss: float (optional, average losing trade %)
            - correlation_matrix: dict (optional, from PairsCorrelationMonitor)
        """
        proposal = context.get("proposal", {})
        portfolio_value = context.get("portfolio_value", 0)
        cash_balance = context.get("cash_balance", 0)
        cycle_id = context.get("cycle_id", "")
        stats_db = context.get("stats_db")
        exchange = context.get("exchange", "coinbase")
        win_rate = context.get("win_rate", 0)

        def _reject(result: dict[str, Any]) -> dict[str, Any]:
            """Return ``result`` after persisting a ``risk_manager`` reasoning
            span. Ensures rejection paths are always visible on the dashboard
            instead of silently disappearing into the cycle (only the trader
            span would otherwise be persisted, leaving the UI to render
            'No strategy generated')."""
            if stats_db and cycle_id:
                try:
                    stats_db.save_reasoning(
                        cycle_id=cycle_id,
                        pair=result.get("pair") or proposal.get("pair") or "?",
                        agent_name="risk_manager",
                        reasoning_json=result,
                        signal_type=result.get("action") or proposal.get("action") or "hold",
                        confidence=float(proposal.get("confidence", 0) or 0),
                        exchange=exchange,
                    )
                except Exception as _e:  # pragma: no cover — diagnostics only
                    self.logger.debug(f"Failed to persist risk_manager rejection: {_e}")
            return result

        avg_win = context.get("avg_win", 0)
        avg_loss = context.get("avg_loss", 0)
        # Phase 5: sample size feeds shrinkage in shrunk-Kelly. Best-effort
        # — defaults to a value that makes shrinkage neutral when missing.
        kelly_samples = int(context.get("trade_count")
                            or (context.get("kelly_stats") or {}).get("samples")
                            or 100)
        correlation_matrix = context.get("correlation_matrix")
        signal_type = context.get("signal_type", "neutral")
        signal_type_win_rate = context.get("signal_type_win_rate")  # float | None

        action = proposal.get("action", "hold")

        # Hold = no risk check needed
        if action == "hold":
            return {"approved": True, "action": "hold", "reason": "No trade proposed"}

        # ─── Minimum confidence gate ─────────────────────────────
        # Reject low-confidence proposals (e.g. weak_buy at 62%)
        # before spending resources on position sizing.
        # NOTE: read both ``min_signal_confidence`` (legacy) and ``min_confidence``
        # (the actual key in coinbase.yaml / ibkr.yaml). Earlier versions defaulted
        # silently to 0.65 when only ``min_confidence`` was present, which blocked
        # ~40% of all proposals (see audit logs 2026-04-30 → 2026-05-04).
        min_signal_confidence = float(
            self.trading_config.get("min_signal_confidence")
            or self.trading_config.get("min_confidence")
            or 0.55
        )
        proposal_confidence = float(proposal.get("confidence", 0))
        if proposal_confidence < min_signal_confidence and action == "buy":
            self.logger.info(
                f"🚫 Trade rejected: confidence {proposal_confidence:.0%} "
                f"below minimum {min_signal_confidence:.0%}"
            )
            return _reject({
                "approved": False,
                "action": action,
                "pair": proposal.get("pair", "?"),
                "reason": (
                    f"Signal confidence {proposal_confidence:.0%} below "
                    f"minimum {min_signal_confidence:.0%} — not worth the fee risk"
                ),
            })
        # ─── High-conviction-only modifier ───────────────────────
        # Reject anything weaker than strong_buy / strong_sell.
        if "high_conviction_only" in self._style_modifiers and action == "buy":
            if signal_type not in self._STRONG_SIGNAL_TYPES:
                self.logger.info(
                    f"🚫 High-conviction-only: rejecting {signal_type} signal for {proposal.get('pair', '?')}"
                )
                return _reject({
                    "approved": False,
                    "action": action,
                    "pair": proposal.get("pair", "?"),
                    "reason": (
                        f"Style modifier 'high_conviction_only' active — "
                        f"{signal_type} rejected (only strong_buy/strong_sell allowed)"
                    ),
                })
        # ─── Portfolio-tier scaling ───────────────────────────────
        # Override static config values with tier-appropriate ones.
        if self.scaler and portfolio_value > 0:
            self.scaler.update(portfolio_value)
            tier = self.scaler.tier
            effective_stop_loss_pct = tier.stop_loss_pct
            effective_take_profit_pct = tier.take_profit_pct
            effective_max_position_pct = tier.max_position_pct
            effective_max_open = tier.max_open_positions
        else:
            effective_stop_loss_pct = self.stop_loss_pct
            effective_take_profit_pct = self.take_profit_pct
            effective_max_position_pct = self.risk_config.get("max_position_pct", 0.05)
            effective_max_open = self.trading_config.get("max_open_positions", 3)

        # ─── Wider-targets modifier ───────────────────────────────
        # Let winners run: TP ×2.0, SL ×1.33 (more breathing room).
        if "wider_targets" in self._style_modifiers:
            effective_take_profit_pct *= 2.0
            effective_stop_loss_pct *= 1.33
            self.logger.info(
                f"🎛 wider_targets: TP→{effective_take_profit_pct:.1%}, "
                f"SL→{effective_stop_loss_pct:.1%}"
            )

        pair = proposal.get("pair", "BTC-EUR")
        quote_amount = float(proposal.get("quote_amount", proposal.get("usd_amount", 0)) or 0)
        price = float(proposal.get("current_price", 0) or self.state.current_prices.get(pair, 0))
        stop_loss = proposal.get("stop_loss_price")
        take_profit = proposal.get("take_profit_price")

        # Get ATR from pipeline context (computed by TechnicalAnalyzer)
        atr = context.get("atr")

        # If no amount specified, use quantity * price
        quantity = float(proposal.get("quantity", 0) or 0)
        if quote_amount <= 0 and quantity > 0 and price > 0:
            quote_amount = quantity * price

        # If still no amount, fall back to a tier-aware default rather than
        # rejecting outright. The trader/strategist routinely returns
        # ``quote_amount=null`` for buy actions when it wants the risk layer
        # to size the trade (audit logs show ~800 silent rejections in the
        # last week alone). Default sizing = min(cash * tier.cash_pct,
        # max_single_trade ceiling). Sells with no amount still reject
        # because we cannot guess the position quantity to liquidate.
        if quote_amount <= 0 and action == "buy" and cash_balance > 0:
            tier_cash_pct = (
                self.scaler.tier.max_cash_per_trade_pct
                if self.scaler
                else self.rules.max_cash_per_trade_pct
            )
            fallback = cash_balance * tier_cash_pct
            ceiling = float(self.rules.max_single_trade)
            quote_amount = max(0.0, min(fallback, ceiling))
            self.logger.info(
                f"💡 Auto-sized {pair} buy: trader returned no amount, "
                f"defaulting to €{quote_amount:.2f} "
                f"({tier_cash_pct:.0%} of €{cash_balance:.2f} cash)"
            )

        if quote_amount <= 0:
            return _reject({
                "approved": False,
                "action": action,
                "pair": pair,
                "reason": (
                    "No valid trade amount specified "
                    "(strategist/trader returned quote_amount=null and quantity=0)"
                ),
            })

        # ─── Clamp oversized buy proposals before AbsoluteRules ─────────
        # The trader sometimes proposes a notional larger than available cash
        # (audit logs show 75 EUR proposed against 27 EUR cash, etc.). Rather
        # than reject and lose the signal, clamp to the tier-aware ceiling so
        # AbsoluteRules sees a feasible amount.
        if action == "buy" and cash_balance > 0:
            tier_cash_pct = (
                self.scaler.tier.max_cash_per_trade_pct
                if self.scaler
                else self.rules.max_cash_per_trade_pct
            )
            cash_ceiling = cash_balance * tier_cash_pct
            single_ceiling = float(self.rules.max_single_trade)
            if portfolio_value > 0:
                # Honour AbsoluteRules' dynamic per-trade cap
                single_ceiling = min(
                    single_ceiling, portfolio_value * tier_cash_pct
                )
            ceiling = min(cash_ceiling, single_ceiling)
            if ceiling > 0 and quote_amount > ceiling:
                self.logger.info(
                    f"🪚 Clamped {pair} buy from €{quote_amount:.2f} → "
                    f"€{ceiling:.2f} (cash €{cash_balance:.2f}, "
                    f"tier {tier_cash_pct:.0%})"
                )
                quote_amount = ceiling

        # Enforce max_open_positions for buy orders (tier-scaled)
        if action == "buy":
            max_positions = effective_max_open
            current_positions = len(self.state.open_positions)
            if current_positions >= max_positions:
                self.logger.info(
                    f"🚫 Trade rejected: {current_positions} open positions "
                    f"(max {max_positions})"
                )
                return _reject({
                    "approved": False,
                    "action": action,
                    "pair": pair,
                    "quote_amount": quote_amount,
                    "reason": (
                        f"Max open positions reached ({current_positions}/{max_positions}). "
                        f"Close an existing position before opening a new one."
                    ),
                })

        # Ensure stop-loss (tier-scaled)
        has_stop_loss = stop_loss is not None

        # Phase 5: regime-conditioned stop tightening + CVaR-based widening.
        # Both are best-effort and only narrow/widen the *clamped* stop pct
        # within configured bands — they never bypass AbsoluteRules.
        regime_label = (
            (context.get("market_signal") or {}).get("market_condition")
            or (context.get("strategy_signals") or {}).get("_ensemble", {}).get("market_regime")
            or context.get("regime")
            or "unknown"
        )
        # Correlation spike heuristic from cross_asset_signal (when present).
        _ca = context.get("cross_asset_signal") or {}
        _correlation_spike = bool(_ca.get("correlation_spike", False))
        try:
            from src.utils.cvar import regime_stop_multiplier as _stop_mult
            _regime_mult = _stop_mult(
                str(regime_label).lower(),
                correlation_spike=_correlation_spike,
            )
        except Exception:
            _regime_mult = 1.0

        if not has_stop_loss and action == "buy" and price > 0:
            if atr:
                # P2: ATR-scaled stop with configurable multiplier and
                # floor/ceiling bands. Floor prevents absurdly-tight stops
                # in low-vol regimes; ceiling caps loss during vol shocks.
                float_atr = float(atr)
                raw_pct = (self.atr_stop_mult * float_atr) / price
                clamped_pct = max(
                    self.atr_stop_floor_pct,
                    min(raw_pct, self.atr_stop_ceiling_pct),
                )
                # Apply regime-conditioned multiplier within bands.
                clamped_pct = max(
                    self.atr_stop_floor_pct,
                    min(clamped_pct * _regime_mult, self.atr_stop_ceiling_pct),
                )
                stop_loss = max(price * (1 - clamped_pct), 0.0)
                self.logger.info(
                    f"Added ATR-based stop-loss ({self.atr_stop_mult}×ATR={float_atr:.2f}, "
                    f"raw {raw_pct:.2%} → clamped {clamped_pct:.2%}, "
                    f"regime={regime_label} ×{_regime_mult:.2f}): {stop_loss:,.2f}"
                )
            else:
                stop_loss = price * (1 - effective_stop_loss_pct)
                self.logger.info(f"Added default percentage stop-loss ({effective_stop_loss_pct:.1%}): {stop_loss:,.2f}")
            has_stop_loss = True

        # Ensure take-profit (tier-scaled)
        if take_profit is None and action == "buy" and price > 0:
            if atr:
                # P2: symmetric ATR TP with band preserved at configured R:R.
                float_atr = float(atr)
                raw_pct = (self.atr_tp_mult * float_atr) / price
                rr = self.atr_tp_mult / self.atr_stop_mult if self.atr_stop_mult > 0 else 1.5
                clamped_pct = max(
                    self.atr_stop_floor_pct * rr,
                    min(raw_pct, self.atr_stop_ceiling_pct * rr),
                )
                take_profit = price * (1 + clamped_pct)
                self.logger.info(
                    f"Added ATR-based take-profit ({self.atr_tp_mult}×ATR={float_atr:.2f}, "
                    f"raw {raw_pct:.2%} → clamped {clamped_pct:.2%}): {take_profit:,.2f}"
                )
            else:
                take_profit = price * (1 + effective_take_profit_pct)
                self.logger.info(f"Added default percentage take-profit ({effective_take_profit_pct:.1%}): {take_profit:,.2f}")

        # ===== CHECK ABSOLUTE RULES =====
        trade_action = TradeAction.BUY if action == "buy" else TradeAction.SELL
        is_allowed, violations, needs_approval = self.rules.check_trade(
            pair=pair,
            action=trade_action,
            quote_value=quote_amount,
            portfolio_value=portfolio_value,
            cash_balance=cash_balance,
            has_stop_loss=has_stop_loss,
        )

        if not is_allowed:
            violation_text = "; ".join(str(v) for v in violations)
            self.logger.warning(f"🚫 Trade REJECTED by absolute rules: {violation_text}")
            return _reject({
                "approved": False,
                "action": action,
                "pair": pair,
                "quote_amount": quote_amount,
                "reason": f"Absolute rule violation: {violation_text}",
                "violations": [str(v) for v in violations],
            })

        # ===== POSITION SIZING (tier-scaled) =====
        # Step 1: Kelly Criterion — use signal-type-specific win rate for strong signals
        # when available (overrides global win rate, improving sizing for assets where
        # strong signals historically outperform the overall track record).
        effective_win_rate = win_rate
        if (
            signal_type in self._STRONG_SIGNAL_TYPES
            and signal_type_win_rate is not None
            and signal_type_win_rate > win_rate
        ):
            effective_win_rate = signal_type_win_rate
            self.logger.info(
                f"Strong signal override: using {signal_type} win_rate "
                f"{signal_type_win_rate:.0%} (global {win_rate:.0%})"
            )

        if self.use_kelly and action == "buy" and portfolio_value > 0:
            # Phase 5: shrunk fractional-Kelly. Falls back to legacy half-Kelly
            # via _compute_kelly_size when the helper returns 0 (e.g. no edge).
            try:
                from src.utils.cvar import shrunk_kelly
                kelly_frac = shrunk_kelly(
                    effective_win_rate, avg_win, avg_loss, kelly_samples,
                    kelly_fraction=self.kelly_fraction,
                    kelly_cap=float(self.risk_config.get("kelly_cap", 0.25)),
                    shrinkage_n=float(self.risk_config.get("kelly_shrinkage_n", 50.0)),
                )
                if kelly_frac <= 0:
                    kelly_frac = self._compute_kelly_size(
                        portfolio_value, effective_win_rate, avg_win, avg_loss
                    )
            except Exception:
                kelly_frac = self._compute_kelly_size(
                    portfolio_value, effective_win_rate, avg_win, avg_loss
                )
            # Cap Kelly by the tier's max_position_pct, not just config
            kelly_frac = min(kelly_frac, effective_max_position_pct)
            max_position = portfolio_value * kelly_frac
        else:
            max_position = portfolio_value * effective_max_position_pct

        # Step 2: ATR volatility adjustment
        if atr and price > 0 and action == "buy":
            float_atr = float(atr)
            atr_pct = float_atr / price
            # If ATR is > 5% of price, reduce position size (high volatility)
            if atr_pct > 0.05:
                volatility_reduction = min(0.5, (0.05 / atr_pct))  # Cap reduction at 50%
                max_position = max_position * volatility_reduction
                self.logger.info(f"High volatility detected (ATR {atr_pct:.1%}). Reduced max position size by {1-volatility_reduction:.1%}.")

        # Step 2b (Phase 13): vol-target multiplier — scale position inversely
        # with realised return-volatility. Strict opt-in via risk.use_vol_target.
        # Strict bound [0.10, 3.00] inside the helper; we additionally cap the
        # *upscale* leg here so vol-target can never DOUBLE a Kelly-sized bet.
        if action == "buy" and self.risk_config.get("use_vol_target", True):
            recent_returns = context.get("recent_returns") or []
            if recent_returns:
                target_vol = float(self.risk_config.get("vol_target", 0.01))
                # Optional HAR-RV forecast injection — when the pipeline
                # manager has fetched a fresh forecast for this pair we
                # let it override the trailing realised vol. Opt-in via
                # the ``RISK_USE_HAR_RV`` env flag (default off so we
                # don't change behavior of running deployments without
                # explicit intent).
                forecast_vol = None
                if os.environ.get("RISK_USE_HAR_RV", "").lower() in ("1", "true", "yes"):
                    try:
                        forecast_vol = context.get("har_rv_forecast")
                        if forecast_vol is not None:
                            forecast_vol = float(forecast_vol)
                    except (TypeError, ValueError):
                        forecast_vol = None
                vt_mult = vol_target_multiplier(
                    recent_returns, target_vol=target_vol,
                    forecast_vol=forecast_vol,
                )
                # Clamp upscale: max 1.25x of Kelly-sized notional. Downscale
                # is unbounded (subject to floor 0.10) so calm regimes shrink
                # rather than grow positions.
                vt_mult_capped = min(vt_mult, 1.25)
                if abs(vt_mult_capped - 1.0) > 0.01:
                    max_position *= vt_mult_capped
                    self.logger.info(
                        f"Vol-target × {vt_mult_capped:.2f} "
                        f"(target={target_vol:.2%}, n={len(recent_returns)}) "
                        f"→ max {max_position:,.2f}"
                    )

        # Step 3: Correlation penalty
        if action == "buy":
            corr_mult = self._compute_correlation_penalty(pair, correlation_matrix)
            if corr_mult < 1.0:
                max_position *= corr_mult

        # Step 4: Signal-strength multiplier
        # strong_buy=1.0×, buy=0.8×, weak_buy=0.6× (mirrors sell-side symmetrically)
        if action == "buy":
            strength_mult = self._SIGNAL_STRENGTH_MULT.get(signal_type, 0.80)
            max_position *= strength_mult
            if strength_mult != 1.0:
                self.logger.info(
                    f"Signal strength ({signal_type}): size × {strength_mult:.0%} → "
                    f"max {max_position:,.2f}"
                )

        # Step 4b (Phase 14): news bias multiplier — bounded [0.5, 1.5].
        # Strict opt-in: only applied when risk.use_news_bias is true.
        if action == "buy" and self.risk_config.get("use_news_bias", True):
            news_bias = self._read_news_bias()
            if news_bias and abs(news_bias - 1.0) > 0.01:
                # Clamp defensively even if file was tampered with.
                news_bias = max(0.5, min(1.5, float(news_bias)))
                max_position *= news_bias
                self.logger.info(
                    f"News bias × {news_bias:.2f} → max {max_position:,.2f}"
                )

        # Step 4c: Catalyst Pattern Engine multiplier.
        # Advisory only — bounded [0.5, 1.5] and never overrides AbsoluteRules.
        # Direction-aware: bullish historical analogs amplify a buy proposal,
        # bearish ones shrink it. Magnitude scales with engine confidence.
        pattern_signal = context.get("pattern_signal") or {}
        if (
            action == "buy"
            and self.risk_config.get("use_pattern_engine", True)
            and isinstance(pattern_signal, dict)
            and pattern_signal.get("available")
        ):
            try:
                p_dir = pattern_signal.get("direction", "neutral")
                p_conf = float(pattern_signal.get("confidence", 0.0))
                # Map (direction, confidence) → multiplier in [0.5, 1.5].
                if p_dir == "bullish":
                    pattern_mult = 1.0 + 0.5 * max(0.0, min(1.0, p_conf))
                elif p_dir == "bearish":
                    pattern_mult = 1.0 - 0.5 * max(0.0, min(1.0, p_conf))
                else:
                    pattern_mult = 1.0
                pattern_mult = max(0.5, min(1.5, pattern_mult))
                if abs(pattern_mult - 1.0) > 0.01:
                    max_position *= pattern_mult
                    self.logger.info(
                        f"Pattern engine ({p_dir}, conf {p_conf:.2f}) × "
                        f"{pattern_mult:.2f} → max {max_position:,.2f}"
                    )
            except (TypeError, ValueError) as _e:
                self.logger.debug(f"pattern_signal multiplier skipped: {_e}")

        # Step 4d: Regression factor — turns nightly event-price OLS fits
        # into a real, bounded size adjustment. Strict opt-in (built upstream
        # in pipeline_manager honoring REGRESSION_RISK_FACTOR_ENABLED env or
        # per-profile risk.use_regression_factor). When the upstream factor
        # is not "applied" (disabled, no catalyst, weak fit) this block is a
        # no-op — the multiplier defaults to 1.0.
        regression_factor = context.get("regression_factor") or {}
        if (
            action == "buy"
            and isinstance(regression_factor, dict)
            and regression_factor.get("applied")
        ):
            try:
                rf = float(regression_factor.get("factor", 1.0))
                # Defensive clip — pipeline_manager already bounds this but
                # we never trust an upstream-supplied multiplier blindly.
                rf = max(0.9, min(1.1, rf))
                if abs(rf - 1.0) > 0.005:
                    max_position *= rf
                    _model = regression_factor.get("model") or {}
                    self.logger.info(
                        f"Regression factor ({regression_factor.get('direction', '?')}, "
                        f"R²={(_model.get('r_squared') or 0):.2f}, N={_model.get('sample_count')}) "
                        f"× {rf:.3f} → max {max_position:,.2f}"
                    )
            except (TypeError, ValueError) as _e:
                self.logger.debug(f"regression_factor multiplier skipped: {_e}")

        # Step 5: Strong-signal floor (override quality penalty for new/poor-history assets)
        # A confident strong signal should not be near-zero-sized just because Kelly
        # produced a tiny fraction from a thin/bad historical track record.
        strong_signal_min_pct = self.risk_config.get("strong_signal_min_position_pct", 0.015)
        if (
            action == "buy"
            and signal_type in self._STRONG_SIGNAL_TYPES
            and proposal_confidence >= 0.80
            and portfolio_value > 0
        ):
            floor = portfolio_value * strong_signal_min_pct
            if max_position < floor:
                max_position = floor
                self.logger.info(
                    f"Strong-signal floor applied: min {strong_signal_min_pct:.1%} "
                    f"of portfolio ({floor:,.2f})"
                )

        if quote_amount > max_position:
            original = quote_amount
            quote_amount = max_position
            self.logger.info(
                f"Adjusted position size: {original:,.2f} → {quote_amount:,.2f} "
                f"(max allowed: {max_position:,.2f})"
            )

        # Step 4d: Capital-allocator budget cap.
        # The TraderAgent's DecisionEngine attaches an `allocator_budget_cap`
        # to every approved proposal. We honour it as an additional hard
        # ceiling so a hot-running strategy cannot starve underweighted ones.
        # Falls back to reading directly from `context['quant'].allocator`
        # when the proposal predates the engine (legacy strategist paths).
        allocator_cap = proposal.get("allocator_budget_cap")
        if allocator_cap is None and action == "buy":
            quant_ctx = context.get("quant")
            if quant_ctx is not None and getattr(quant_ctx, "allocator", None):
                try:
                    weights = quant_ctx.allocator.weights() or {}
                    strat_name = proposal.get("strategy") or "llm_strategist"
                    if strat_name in weights and portfolio_value > 0:
                        allocator_cap = float(weights[strat_name]) * portfolio_value
                except Exception:
                    allocator_cap = None
        if (
            action == "buy"
            and allocator_cap is not None
            and float(allocator_cap) > 0
            and quote_amount > float(allocator_cap)
        ):
            original = quote_amount
            quote_amount = float(allocator_cap)
            self.logger.info(
                f"Allocator budget cap: {original:,.2f} → {quote_amount:,.2f} "
                f"(allocator_cap={allocator_cap:,.2f})"
            )

        # P3: portfolio-level exposure cap. Sum of open buy-side notional +
        # this new order must not exceed max_total_exposure_pct of portfolio.
        # Prevents accidentally going all-in across many small positions.
        max_total_exposure_pct = float(
            self.risk_config.get("max_total_exposure_pct", 0.8)
        )
        if action == "buy" and portfolio_value > 0 and max_total_exposure_pct > 0:
            existing_exposure = 0.0
            try:
                for pos_pair, pos in self.state.open_positions.items():
                    qty = float(getattr(pos, "quantity", 0) or 0)
                    px = float(self.state.current_prices.get(pos_pair, 0) or 0)
                    existing_exposure += abs(qty * px)
            except Exception:
                existing_exposure = 0.0
            exposure_cap = portfolio_value * max_total_exposure_pct
            headroom = max(exposure_cap - existing_exposure, 0.0)
            if quote_amount > headroom:
                if headroom <= 0:
                    self.logger.warning(
                        f"🚫 Exposure cap reached: {existing_exposure:,.2f} / "
                        f"{exposure_cap:,.2f} ({max_total_exposure_pct:.0%}) — rejecting buy"
                    )
                    return _reject({
                        "approved": False,
                        "action": action,
                        "pair": pair,
                        "quote_amount": quote_amount,
                        "reason": (
                            f"Portfolio exposure cap reached "
                            f"({existing_exposure:,.2f} / {exposure_cap:,.2f})"
                        ),
                    })
                original = quote_amount
                quote_amount = headroom
                self.logger.info(
                    f"Exposure cap trim: {original:,.2f} → {quote_amount:,.2f} "
                    f"(existing {existing_exposure:,.2f} / cap {exposure_cap:,.2f})"
                )

        # Calculate final quantity
        # For sells with a specific quantity from the strategist (e.g. pre-existing
        # holdings), preserve the original quantity rather than recalculating.
        if action == "sell" and quantity > 0:
            # Strategist specified exact quantity from ACTUAL COINBASE HOLDINGS
            pass  # keep quantity as-is
        elif price > 0:
            quantity = quote_amount / price

        result = {
            "approved": True,
            "needs_approval": needs_approval,
            "action": action,
            "pair": pair,
            "quote_amount": quote_amount,
            "quantity": quantity,
            "price": price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "confidence": proposal.get("confidence", 0),
            "reasoning": proposal.get("reasoning", ""),
        }

        status = "✅ APPROVED" if not needs_approval else "⚠️ NEEDS TELEGRAM APPROVAL"
        # H26 fix: stop_loss/take_profit are None for sell orders
        sl_str = f"{stop_loss:,.2f}" if stop_loss is not None else "N/A"
        tp_str = f"{take_profit:,.2f}" if take_profit is not None else "N/A"
        self.logger.info(
            f"{status}: {action.upper()} {quote_amount:,.2f} of {pair} | "
            f"SL: {sl_str} | TP: {tp_str}"
        )

        # Persist risk decision trace (no LLM call — rule-based, so tokens=0)
        if stats_db and cycle_id:
            try:
                stats_db.save_reasoning(
                    cycle_id=cycle_id,
                    pair=pair,
                    agent_name="risk_manager",
                    reasoning_json=result,
                    signal_type=action,
                    confidence=float(proposal.get("confidence", 0)),
                    exchange=exchange,
                )
            except Exception as e:
                self.logger.debug(f"Failed to save risk_manager trace: {e}")

        return result
