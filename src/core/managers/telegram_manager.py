"""
TelegramCommandHandler — Handles all Telegram commands and chat function registration.

Extracted from Orchestrator for maintainability.  Takes an orchestrator reference
in its constructor (same pattern as PipelineManager / StateManager).
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.utils.logger import get_logger
from src.utils.helpers import format_currency, format_percentage
from src.utils.security import sanitize_input
from src.utils import settings_manager as sm

if TYPE_CHECKING:
    from src.core.orchestrator import Orchestrator

logger = get_logger("core.telegram_manager")


class TelegramManager:
    """Registers all chat functions and handles legacy Telegram commands."""

    def __init__(self, orchestrator: "Orchestrator"):
        self.orchestrator = orchestrator

    # =========================================================================
    # Chat Function Registry
    # =========================================================================

    def register_chat_functions(self) -> None:
        """Register all trading functions the LLM chat handler can call."""
        orch = self.orchestrator
        ch = orch.chat_handler

        # ─── Read functions ────────────────────────────────────────────

        def _live_get_status(p):
            """Combine live Coinbase snapshot with agent state."""
            snap = orch.holdings_manager.live_coinbase_snapshot()
            return {
                # Currency metadata
                "native_currency": snap["native_currency"],
                "currency_symbol": snap["currency_symbol"],
                # Live Coinbase numbers (in native currency)
                "portfolio_value": snap["total_portfolio"],
                "fiat_cash": snap["fiat_cash"],
                "holdings": snap["holdings"],
                "tracked_pairs": snap["tracked_pairs"],
                "prices": snap["prices_by_pair"],
                "data_fetched_at": snap["fetch_ts"],
                # Agent state (bot activity)
                "bot_trades_executed": snap["bot_trades"],
                "bot_pnl": snap["bot_pnl"],
                "bot_open_positions": orch.state.open_positions,
                "win_rate": orch.state.win_rate,
                "max_drawdown": orch.state.max_drawdown,
                "is_running": orch.state.is_running,
                "is_paused": snap["is_paused"],
                "circuit_breaker": snap["circuit_breaker"],
            }

        ch.register_function("get_status", _live_get_status)

        def _live_get_positions(p):
            """Return actual Coinbase holdings, not just bot-tracked positions."""
            snap = orch.holdings_manager.live_coinbase_snapshot()
            crypto_holdings = [h for h in snap["holdings"] if not h["is_fiat"]]
            return {
                "data_source": "live_coinbase_api",
                "native_currency": snap["native_currency"],
                "currency_symbol": snap["currency_symbol"],
                "fetched_at": snap["fetch_ts"],
                "coinbase_holdings": crypto_holdings,
                "total_crypto_value": sum(h["native_value"] for h in crypto_holdings),
                # Also expose what the bot itself opened (may be subset/empty)
                "bot_tracked_positions": orch.state.open_positions,
            }

        ch.register_function("get_positions", _live_get_positions)

        def _live_get_balance(p):
            """Return live Coinbase portfolio value, not stale agent state."""
            snap = orch.holdings_manager.live_coinbase_snapshot()
            fiat = [h for h in snap["holdings"] if h["is_fiat"]]
            crypto = [h for h in snap["holdings"] if not h["is_fiat"]]
            return {
                "data_source": "live_coinbase_api",
                "native_currency": snap["native_currency"],
                "currency_symbol": snap["currency_symbol"],
                "fetched_at": snap["fetch_ts"],
                "total_portfolio": snap["total_portfolio"],
                "fiat_cash": snap["fiat_cash"],
                "fiat_accounts": [{"currency": h["currency"], "amount": h["amount"]} for h in fiat],
                "crypto_positions_value": sum(h["native_value"] for h in crypto),
                "bot_pnl": snap["bot_pnl"],
            }

        ch.register_function("get_balance", _live_get_balance)

        def _live_get_prices(p):
            """Fetch fresh prices directly from Coinbase REST API."""
            prices = {}
            for pair in orch.pairs:
                try:
                    price = orch.exchange.get_current_price(pair)
                    if price > 0:
                        prices[pair] = price
                        orch.state.update_price(pair, price)
                except Exception as e:
                    logger.debug(f"Price fetch failed for {pair}: {e}")
            return {
                "data_source": "live_coinbase_api",
                "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "prices": prices,
            }

        ch.register_function("get_current_prices", _live_get_prices)

        def _live_get_account_holdings(p):
            """Full raw account breakdown from Coinbase."""
            snap = orch.holdings_manager.live_coinbase_snapshot()
            return snap

        ch.register_function("get_account_holdings", _live_get_account_holdings)

        ch.register_function("get_recent_trades", lambda p: {
            "trades": [t.to_summary() for t in orch.state.recent_trades]
        })

        ch.register_function("get_recent_signals", lambda p: {
            "signals": [
                {
                    "pair": s.pair,
                    "signal_type": s.signal_type,
                    "confidence": s.confidence,
                    "reasoning": s.reasoning[:200] if s.reasoning else "",
                }
                for s in orch.state.recent_signals
            ]
        })

        ch.register_function("get_news_summary", lambda p: {
            "news": orch.news.get_summary() if orch.news else "News not configured."
        })

        ch.register_function("get_fear_greed", lambda p: {
            "fear_greed": orch.fear_greed.get_current()
        })

        ch.register_function("get_trading_rules", lambda p: {
            **orch.rules.get_all_rules(),
            **orch.rules.get_status(),
        })

        ch.register_function("get_fee_info", lambda p: orch.fee_manager.get_fee_summary())

        ch.register_function("get_pending_swaps", lambda p: {
            "pending_swaps": {
                sid: {
                    "sell": sp.sell_pair,
                    "buy": sp.buy_pair,
                    "quote_amount": sp.quote_amount,
                    "net_gain": f"+{sp.net_gain_pct*100:.2f}%",
                    "priority": sp.priority,
                }
                for sid, sp in orch.rotator.pending_swaps.items()
            }
        })

        ch.register_function("get_highstakes_status", lambda p: {
            "status": orch.high_stakes.get_status()
        })

        ch.register_function("get_rotation_analysis", lambda p: {
            "analysis": self.cmd_rotate({})
        })

        # ─── Action functions ──────────────────────────────────────────
        ch.register_function("enable_highstakes", lambda p: self.cmd_highstakes({
            "description": p.get("duration", "4h"),
            "user_id": "owner",
        }))

        ch.register_function("disable_highstakes", lambda p: self.cmd_highstakes({
            "description": "off",
            "user_id": "owner",
        }))

        ch.register_function("create_task", lambda p: self.cmd_task({
            "description": p.get("description", ""),
        }))

        ch.register_function("approve_item", lambda p: self.cmd_approve_trade({
            "trade_id": p.get("item_id", ""),
        }))

        ch.register_function("reject_item", lambda p: self.cmd_reject_trade({
            "trade_id": p.get("item_id", ""),
        }))

        ch.register_function("pause_trading", lambda p: self.cmd_pause({}))
        ch.register_function("resume_trading", lambda p: self.cmd_resume({}))
        ch.register_function("emergency_stop", lambda p: self.cmd_stop({}))

        # ─── Settings management ─────────────────────────────────────
        def _enable_trading(p: dict) -> dict:
            preset = p.get("preset", "moderate")
            ok, err, changes = sm.apply_preset(preset)
            if ok:
                sm.push_to_runtime(orch.rules, orch.config, changes)
                logger.warning(f"🟢 TRADING ENABLED via preset '{preset}' (Telegram)")
                return {"ok": True, "preset": preset, "changes": changes}
            return {"ok": False, "error": err}

        def _disable_trading(p: dict) -> dict:
            ok, err, changes = sm.apply_preset("disabled")
            if ok:
                sm.push_to_runtime(orch.rules, orch.config, changes)
                logger.warning("🔴 TRADING DISABLED (Telegram)")
                return {"ok": True, "preset": "disabled", "changes": changes}
            return {"ok": False, "error": err}

        def _apply_preset(p: dict) -> dict:
            preset = p.get("preset", "")
            if preset not in sm.PRESETS:
                return {"ok": False, "error": f"Unknown preset: {preset!r}. Available: {list(sm.PRESETS.keys())}"}
            ok, err, changes = sm.apply_preset(preset)
            if ok:
                sm.push_to_runtime(orch.rules, orch.config, changes)
                logger.warning(f"📋 PRESET '{preset}' applied (Telegram)")
                return {"ok": True, "preset": preset, "changes": changes}
            return {"ok": False, "error": err}

        def _update_settings(p: dict) -> dict:
            section = p.get("section", "")
            param = p.get("param", "")
            value = p.get("value", "")
            if not section or not param:
                return {"ok": False, "error": "section and param are required"}
            if not sm.is_telegram_allowed(section):
                return {"ok": False, "error": f"Section '{section}' is blocked for Telegram updates. Use the Dashboard instead."}
            ok, err, applied = sm.update_section(section, {param: value})
            if ok:
                sm.push_section_to_runtime(section, {param: applied[param]}, orch.rules, orch.config)
                logger.warning(f"🔧 SETTINGS UPDATE via Telegram | {section}.{param} → {applied[param]}")
                return {"ok": True, "section": section, "param": param, "new": applied[param]}
            return {"ok": False, "error": err}

        def _get_settings_tiers(_p: dict) -> dict:
            return sm.TELEGRAM_SAFETY_TIERS

        ch.register_function("enable_trading", _enable_trading)
        ch.register_function("disable_trading", _disable_trading)
        ch.register_function("apply_preset", _apply_preset)
        ch.register_function("update_settings", _update_settings)
        ch.register_function("get_settings_tiers", _get_settings_tiers)

        # ─── Stats & Analytics functions ───────────────────────────────
        ch.register_function("get_stats", lambda p: orch.stats_db.get_performance_summary(
            hours=int(p.get("hours", 24))
        ))

        ch.register_function("get_trade_history", lambda p: {
            "trades": orch.stats_db.get_trades(
                hours=int(p.get("hours", 24)),
                pair=p.get("pair"),
            )
        })

        ch.register_function("get_pair_stats", lambda p: orch.stats_db.get_pair_stats(
            pair=p.get("pair", "BTC-USD"),
            hours=int(p.get("hours", 168)),
        ))

        ch.register_function("get_daily_summaries", lambda p: {
            "summaries": orch.stats_db.get_daily_summaries(days=int(p.get("days", 7)))
        })

        ch.register_function("get_best_worst", lambda p: orch.stats_db.get_best_worst_trades(
            hours=int(p.get("hours", 168))
        ))

        ch.register_function("schedule_report", lambda p: {
            "id": orch.stats_db.add_scheduled_report(
                name=p.get("name", "Custom Report"),
                description=p.get("description", ""),
                cron_expression=p.get("interval", "1h"),
                query_type=p.get("query_type", "performance"),
                query_params=p,
            ),
            "status": "scheduled",
        })

        ch.register_function("get_schedules", lambda p: {
            "schedules": orch.stats_db.get_active_schedules()
        })

        ch.register_function("delete_schedule", lambda p: {
            "deleted": orch.stats_db.delete_schedule(int(p.get("id", 0)))
        })

        # ─── Config / settings read ────────────────────────────────────
        ch.register_function("get_config", lambda p: {
            "absolute_rules": orch.rules.get_all_rules(),
            "trading": {
                "mode": orch.config.get("trading", {}).get("mode", "paper"),
                "pairs": list(orch.pairs),
                "interval_seconds": orch.interval,
                "min_confidence": orch.config.get("trading", {}).get("min_confidence", 1.0),
                "max_open_positions": orch.config.get("trading", {}).get("max_open_positions", 3),
            },
            "risk": dict(orch.config.get("risk", {})),
            "fees": dict(orch.config.get("fees", {})),
            "high_stakes": orch.high_stakes.get_status(),
            "rotation": dict(orch.config.get("rotation", {})),
        })

        # ─── Config / settings write ───────────────────────────────────
        def _update_rule(p: dict) -> dict:
            result = orch.rules.update_param(
                param=p.get("param", ""),
                value=str(p.get("value", "")),
            )
            # Persist to settings.yaml
            if result.get("ok"):
                try:
                    sm.update_section("absolute_rules", {p["param"]: result["new"]})
                except Exception as e:
                    logger.error(f"Failed to persist rule update to disk: {e}")
            return result

        ch.register_function("update_rule", _update_rule)

        def _update_trading_param(p: dict) -> dict:
            from typing import Any
            param = p.get("param", "")
            value_str = str(p.get("value", ""))
            trading_cfg = orch.config.setdefault("trading", {})
            _float_params = {"min_confidence", "paper_slippage_pct"}
            _int_params = {"max_open_positions", "interval"}
            try:
                if param in _float_params:
                    new_val: Any = float(value_str)
                elif param in _int_params:
                    new_val = int(float(value_str))
                else:
                    return {"ok": False, "error": f"Unknown trading param: {param!r}"}
            except (ValueError, TypeError) as e:
                return {"ok": False, "error": str(e)}
            old_val = trading_cfg.get(param)
            trading_cfg[param] = new_val
            if param == "interval":
                orch.interval = new_val
            logger.warning(f"🔧 TRADING PARAM UPDATED (runtime) | {param}: {old_val!r} → {new_val!r}")
            # Persist to settings.yaml
            try:
                sm.update_section("trading", {param: new_val})
            except Exception as e:
                logger.error(f"Failed to persist trading param to disk: {e}")
            return {"ok": True, "param": param, "old": old_val, "new": new_val}

        ch.register_function("update_trading_param", _update_trading_param)

        def _update_risk_param(p: dict) -> dict:
            from typing import Any
            param = p.get("param", "")
            value_str = str(p.get("value", ""))
            risk_cfg = orch.config.setdefault("risk", {})
            _risk_params = {
                "stop_loss_pct", "take_profit_pct", "trailing_stop_pct",
                "max_position_pct", "max_total_exposure_pct", "max_drawdown_pct",
            }
            _risk_int_params = {"max_trades_per_hour", "loss_cooldown_seconds"}
            try:
                if param in _risk_params:
                    new_val: Any = float(value_str)
                elif param in _risk_int_params:
                    new_val = int(float(value_str))
                else:
                    return {"ok": False, "error": f"Unknown risk param: {param!r}"}
            except (ValueError, TypeError) as e:
                return {"ok": False, "error": str(e)}
            old_val = risk_cfg.get(param)
            risk_cfg[param] = new_val
            # Propagate trailing_stop_pct to the TrailingStopManager
            if param == "trailing_stop_pct":
                orch.trailing_stops.default_trail_pct = new_val
            logger.warning(f"🔧 RISK PARAM UPDATED (runtime) | {param}: {old_val!r} → {new_val!r}")
            # Persist to settings.yaml
            try:
                sm.update_section("risk", {param: new_val})
            except Exception as e:
                logger.error(f"Failed to persist risk param to disk: {e}")
            return {"ok": True, "param": param, "old": old_val, "new": new_val}

        ch.register_function("update_risk_param", _update_risk_param)

        # ─── Pair management ───────────────────────────────────────────
        def _add_pair(p: dict) -> dict:
            pair = str(p.get("pair", "")).upper().strip()
            if not pair:
                return {"ok": False, "error": "pair is required"}
            if pair not in orch.pairs:
                orch.pairs.append(pair)
                orch.config.setdefault("trading", {}).setdefault("pairs", []).append(pair)
                logger.info(f"📌 Pair added (runtime): {pair}. Active pairs: {orch.pairs}")
            return {"ok": True, "pair": pair, "all_pairs": list(orch.pairs)}

        ch.register_function("add_pair", _add_pair)

        def _remove_pair(p: dict) -> dict:
            pair = str(p.get("pair", "")).upper().strip()
            if pair in orch.pairs:
                orch.pairs.remove(pair)
                cfg_pairs = orch.config.get("trading", {}).get("pairs", [])
                if pair in cfg_pairs:
                    cfg_pairs.remove(pair)
                logger.info(f"🗑️ Pair removed (runtime): {pair}. Active pairs: {orch.pairs}")
            return {"ok": True, "pair": pair, "all_pairs": list(orch.pairs)}

        ch.register_function("remove_pair", _remove_pair)

        ch.register_function("blacklist_pair", lambda p: orch.rules.add_never_trade_pair(
            str(p.get("pair", ""))
        ))

        ch.register_function("unblacklist_pair", lambda p: orch.rules.remove_never_trade_pair(
            str(p.get("pair", ""))
        ))

        # ─── Trailing stops ────────────────────────────────────────────
        ch.register_function("get_trailing_stops", lambda p: {
            "trailing_stops": orch.trailing_stops.get_all_stops()
        })

        # ─── Pending swap management ───────────────────────────────────
        def _cancel_swap(p: dict) -> dict:
            swap_id = str(p.get("swap_id", ""))
            if swap_id in orch.rotator.pending_swaps:
                del orch.rotator.pending_swaps[swap_id]
                return {"ok": True, "cancelled": swap_id}
            return {"ok": False, "error": f"Swap {swap_id!r} not found"}

        ch.register_function("cancel_swap", _cancel_swap)

        # ─── Simulated Trades ──────────────────────────────────────────
        def _simulate_trade(p: dict) -> dict:
            pair = str(p.get("pair", "")).upper().strip()
            from_currency = str(p.get("from_currency", "")).upper().strip()
            notes = str(p.get("notes", ""))
            try:
                from_amount = float(p.get("from_amount", 0))
            except (TypeError, ValueError):
                return {"ok": False, "error": "from_amount must be a number"}

            if not pair or not from_currency or from_amount <= 0:
                return {"ok": False, "error": "pair, from_currency, and from_amount are required"}

            # Derive to_currency from pair
            parts = pair.split("-")
            if len(parts) != 2:
                return {"ok": False, "error": f"Invalid pair format: {pair!r}"}
            base, quote = parts
            to_currency = base if from_currency == quote else quote

            # Get live entry price
            try:
                entry_price = orch.exchange.get_current_price(pair)
            except Exception as e:
                return {"ok": False, "error": f"Cannot fetch price for {pair}: {e}"}

            if entry_price <= 0:
                return {"ok": False, "error": f"No live price available for {pair}"}

            quantity = from_amount / entry_price if from_currency == quote else from_amount * entry_price

            sim_id = orch.stats_db.record_simulated_trade(
                pair=pair,
                from_currency=from_currency,
                from_amount=from_amount,
                entry_price=entry_price,
                quantity=quantity,
                to_currency=to_currency,
                notes=notes,
            )
            return {
                "ok": True,
                "id": sim_id,
                "pair": pair,
                "from_currency": from_currency,
                "to_currency": to_currency,
                "from_amount": from_amount,
                "entry_price": entry_price,
                "quantity": round(quantity, 8),
                "notes": notes,
            }

        ch.register_function("simulate_trade", _simulate_trade)

        def _list_simulations(p: dict) -> dict:
            include_closed = bool(p.get("include_closed", False))
            rows = orch.stats_db.get_simulated_trades(include_closed=include_closed)
            # Enrich open rows with live PnL
            for row in rows:
                if row["status"] == "open":
                    try:
                        current_price = orch.exchange.get_current_price(row["pair"])
                    except Exception:
                        current_price = row["entry_price"]
                    if current_price > 0:
                        pnl_abs = (current_price - row["entry_price"]) * row["quantity"]
                        pnl_pct = ((current_price / row["entry_price"]) - 1) * 100 if row["entry_price"] > 0 else 0.0
                    else:
                        pnl_abs = 0.0
                        pnl_pct = 0.0
                    row["current_price"] = current_price
                    row["pnl_abs"] = round(pnl_abs, 4)
                    row["pnl_pct"] = round(pnl_pct, 2)
            return {"simulations": rows, "count": len(rows)}

        ch.register_function("list_simulations", _list_simulations)

        def _close_simulation(p: dict) -> dict:
            try:
                sim_id = int(p.get("sim_id", 0))
            except (TypeError, ValueError):
                return {"ok": False, "error": "sim_id must be an integer"}
            # Fetch current price for this sim
            rows = orch.stats_db.get_simulated_trades(include_closed=False)
            target = next((r for r in rows if r["id"] == sim_id), None)
            if not target:
                return {"ok": False, "error": f"No open simulation with id={sim_id}"}
            try:
                close_price = orch.exchange.get_current_price(target["pair"])
            except Exception:
                close_price = target["entry_price"]
            if close_price <= 0:
                close_price = target["entry_price"]
            result = orch.stats_db.close_simulated_trade(sim_id=sim_id, close_price=close_price)
            if not result:
                return {"ok": False, "error": f"Failed to close simulation {sim_id}"}
            return {"ok": True, **result}

        ch.register_function("close_simulation", _close_simulation)

        logger.info(f"🧠 Registered {len(ch._function_handlers)} chat functions ({len(ch._tool_defs)} with schemas)")

    # =========================================================================
    # Proactive Updates
    # =========================================================================

    def send_proactive_update(self) -> None:
        """Generate and send LLM-powered proactive updates via Telegram."""
        orch = self.orchestrator
        if not orch.telegram or not orch.chat_handler:
            return

        try:
            context = self.get_trading_context()
            update = orch.chat_handler.generate_proactive_update(context)
            if update:
                orch.telegram.send_message(update)
        except Exception as e:
            logger.debug(f"Proactive update skipped: {e}")

    def get_trading_context(self) -> dict:
        """Assemble trading context for the LLM."""
        orch = self.orchestrator
        s = orch.state
        sym = s.currency_symbol
        return {
            "portfolio_value": format_currency(s.portfolio_value, sym),
            "cash_balance": format_currency(s.cash_balance, sym),
            "return_pct": format_percentage(s.return_pct),
            "max_drawdown": format_percentage(s.max_drawdown),
            "total_trades": s.total_trades,
            "win_rate": format_percentage(s.win_rate),
            "total_pnl": format_currency(s.total_pnl, sym),
            "open_positions": {
                pair: {
                    "qty": qty,
                    "price": format_currency(s.current_prices.get(pair, 0), sym),
                }
                for pair, qty in s.open_positions.items()
            },
            "current_prices": {
                pair: format_currency(price, sym)
                for pair, price in s.current_prices.items()
            },
            "is_paused": s.is_paused,
            "circuit_breaker": s.circuit_breaker_triggered,
            "fear_greed": orch.fear_greed.get_current(),
            "high_stakes_active": orch.high_stakes.is_active,
            "pending_swaps": len(orch.rotator.pending_swaps),
            "trailing_stops": orch.trailing_stops.get_active_count(),
            # Raw numeric data (for proactive engine calculations + stats DB)
            "raw_prices": dict(s.current_prices),
            "raw_positions": dict(s.open_positions),
            "raw_portfolio_value": s.portfolio_value,
            "raw_cash_balance": s.cash_balance,
            "raw_return_pct": s.return_pct,
            "raw_total_pnl": s.total_pnl,
            "raw_max_drawdown": s.max_drawdown,
            "currency_symbol": s.currency_symbol,
        }

    # =========================================================================
    # Telegram Command Handling (legacy fallback)
    # =========================================================================

    def handle_telegram_command(self, command: str, data: dict) -> str:
        """Handle commands from Telegram."""
        handlers = {
            "status": self.cmd_status,
            "task": self.cmd_task,
            "rules": self.cmd_rules,
            "positions": self.cmd_positions,
            "trades": self.cmd_trades,
            "news": self.cmd_news,
            "balance": self.cmd_balance,
            "pause": self.cmd_pause,
            "resume": self.cmd_resume,
            "stop": self.cmd_stop,
            "approve_trade": self.cmd_approve_trade,
            "reject_trade": self.cmd_reject_trade,
            "highstakes": self.cmd_highstakes,
            "fees": self.cmd_fees,
            "swaps": self.cmd_swaps,
            "rotate": self.cmd_rotate,
            "message": self.cmd_message,
        }

        handler = handlers.get(command)
        if handler:
            return handler(data)
        return f"Unknown command: {command}"

    def cmd_status(self, data: dict) -> str:
        s = self.orchestrator.state
        return (
            f"📊 *Portfolio Status*\n\n"
            f"💰 Value: {format_currency(s.portfolio_value)}\n"
            f"💵 Cash: {format_currency(s.cash_balance)}\n"
            f"📈 Return: {format_percentage(s.return_pct)}\n"
            f"📉 Max Drawdown: {format_percentage(s.max_drawdown)}\n"
            f"🔄 Total Trades: {s.total_trades}\n"
            f"✅ Win Rate: {format_percentage(s.win_rate)}\n"
            f"💰 Total PnL: {format_currency(s.total_pnl)}\n"
            f"📊 Open Positions: {len(s.open_positions)}\n"
            f"{'⏸️ PAUSED' if s.is_paused else '▶️ Running'}\n"
            f"{'🛑 CIRCUIT BREAKER' if s.circuit_breaker_triggered else ''}"
        )

    def cmd_task(self, data: dict) -> str:
        from src.core.orchestrator import Task
        orch = self.orchestrator
        description = sanitize_input(data.get("description", ""), max_length=300)
        if not description:
            return "Please provide a task description."

        # Try to parse spending limit from the task
        max_spend = None
        spend_match = re.search(r"\$(\d+(?:\.\d+)?)", description)
        if spend_match:
            max_spend = float(spend_match.group(1))

        # Try to identify pair
        pair = None
        for p in orch.pairs:
            base = p.split("-")[0]
            if base.lower() in description.lower():
                pair = p
                break

        task = Task(description=description, max_spend=max_spend, pair=pair)
        # Evict completed/stale tasks; cap list at 20 to prevent unbounded growth
        orch.active_tasks = [t for t in orch.active_tasks if not t.completed][-20:]
        orch.active_tasks.append(task)

        return (
            f"📝 *Task Created*\n\n"
            f"ID: `{task.id}`\n"
            f"Description: {description}\n"
            f"Max Spend: {format_currency(max_spend) if max_spend else 'Not set'}\n"
            f"Pair: {pair or 'Any'}"
        )

    def cmd_rules(self, data: dict) -> str:
        orch = self.orchestrator
        rules_text = orch.rules.get_rules_text()
        status = orch.rules.get_status()
        return (
            f"{rules_text}\n"
            f"📊 *Today's Usage*\n"
            f"• Spent: {format_currency(status['daily_spend'])} / {format_currency(status['daily_spend_remaining'])} remaining\n"
            f"• Losses: {format_currency(status['daily_loss'])} / {format_currency(status['daily_loss_remaining'])} remaining\n"
            f"• Trades: {status['trades_today']} / {status['trades_remaining']} remaining"
        )

    def cmd_positions(self, data: dict) -> str:
        orch = self.orchestrator
        positions = orch.state.open_positions
        if not positions:
            return "📊 No open positions."

        lines = ["📊 *Open Positions*\n"]
        for pair, qty in positions.items():
            price = orch.state.current_prices.get(pair, 0)
            value = qty * price
            lines.append(
                f"• {pair}: {qty:.6f} ({format_currency(value)})"
            )
        return "\n".join(lines)

    def cmd_trades(self, data: dict) -> str:
        trades = self.orchestrator.state.recent_trades
        if not trades:
            return "📊 No recent trades."

        lines = ["📊 *Recent Trades*\n"]
        for trade in trades[-10:]:
            lines.append(trade.to_summary())
        return "\n".join(lines)

    def cmd_news(self, data: dict) -> str:
        orch = self.orchestrator
        if orch.news:
            headlines = orch.news.get_headlines(10)
            return f"📰 *Latest Crypto News*\n\n{headlines}"

        if orch.redis:
            try:
                cached = orch.redis.get("news:latest")
                if cached:
                    articles = json.loads(cached)
                    lines = [f"- {a.get('title', '')}" for a in articles[:10]]
                    return "📰 *Latest News*\n\n" + "\n".join(lines)
            except Exception:
                pass
        return "📰 No news available."

    def cmd_balance(self, data: dict) -> str:
        balance = self.orchestrator.exchange.balance
        lines = ["💰 *Account Balance*\n"]
        for currency, amount in balance.items():
            lines.append(f"• {currency}: {amount:,.6f}")
        return "\n".join(lines)

    def cmd_pause(self, data: dict) -> str:
        self.orchestrator.state.is_paused = True
        return "⏸️ Trading paused."

    def cmd_resume(self, data: dict) -> str:
        orch = self.orchestrator
        user_id = data.get("user_id", "telegram")
        was_circuit_breaker = orch.state.circuit_breaker_triggered
        orch.state.is_paused = False
        orch.state.circuit_breaker_triggered = False
        orch.audit.log_auth(user_id, authorized=True, command="resume_trading")
        if was_circuit_breaker:
            alert = f"⚠️ Circuit breaker manually reset and trading resumed by {user_id}."
            logger.warning(alert)
            if orch.telegram:
                orch.telegram.send_alert(alert)
        return "▶️ Trading resumed."

    def cmd_stop(self, data: dict) -> str:
        orch = self.orchestrator
        orch.state.is_running = False
        orch.state.is_paused = True
        return "🛑 Emergency stop activated."

    def cmd_approve_trade(self, data: dict) -> str:
        orch = self.orchestrator
        trade_id = data.get("trade_id", "")
        with orch._pending_approvals_lock:
            approved = orch._pending_approvals.pop(trade_id, None)
        if approved is not None:
            # Clear needs_approval so the executor does not short-circuit again
            approved = {**approved, "needs_approval": False}
            result = asyncio.run(orch.executor.execute({"approved_trade": approved}))
            if result.get("executed"):
                return "✅ Trade executed successfully!"
            return f"❌ Execution failed: {result.get('error', 'Unknown')}"
        return "Trade not found or already processed."

    def cmd_reject_trade(self, data: dict) -> str:
        orch = self.orchestrator
        trade_id = data.get("trade_id", "")
        with orch._pending_approvals_lock:
            removed = orch._pending_approvals.pop(trade_id, None)
        if removed is not None:
            return "Trade rejected."
        return "Trade not found or already processed."

    def cmd_message(self, data: dict) -> str:
        """Legacy fallback — only used if chat handler is not connected."""
        text = sanitize_input(data.get("text", ""), max_length=500)
        if not text:
            return "Empty message received."
        return f"📝 Noted: {text}\nI'll factor this into my decisions."

    def cmd_highstakes(self, data: dict) -> str:
        """
        /highstakes 4h     — Enable high-stakes mode for 4 hours
        /highstakes 2d     — Enable for 2 days
        /highstakes off    — Disable immediately
        /highstakes status — Show current status
        """
        orch = self.orchestrator
        arg = data.get("description", "").strip().lower()
        user_id = data.get("user_id", "unknown")

        if not arg or arg == "status":
            return orch.high_stakes.get_status()

        if arg == "off":
            orch.audit.log_auth(user_id, authorized=True, command="highstakes_off")
            return orch.high_stakes.deactivate(deactivated_by=user_id)

        # Activate with duration
        orch.audit.log_auth(user_id, authorized=True, command=f"highstakes_{arg}")
        success, msg = orch.high_stakes.activate(
            duration_str=arg,
            activated_by=user_id,
        )

        if success and orch.telegram:
            orch.telegram.send_alert(
                f"⚡ High-stakes mode activated by {user_id} for {arg}"
            )

        return msg

    def cmd_fees(self, data: dict) -> str:
        """Show current fee configuration and breakeven analysis."""
        return self.orchestrator.fee_manager.get_fee_summary()

    def cmd_swaps(self, data: dict) -> str:
        """Show pending swap proposals."""
        orch = self.orchestrator
        pending = orch.rotator.pending_swaps
        if not pending:
            return "🔄 No pending swap proposals."

        lines = ["🔄 *Pending Swaps*\n"]
        for swap_id, proposal in pending.items():
            lines.append(
                f"• `{swap_id}`\n"
                f"  {proposal.sell_pair} → {proposal.buy_pair}\n"
                f"  {format_currency(proposal.quote_amount)} | "
                f"net +{proposal.net_gain_pct*100:.2f}%\n"
            )
        lines.append("\nReply /approve <id> or /reject <id>")
        return "\n".join(lines)

    def cmd_rotate(self, data: dict) -> str:
        """Force a rotation check immediately."""
        orch = self.orchestrator
        held_pairs = list(orch.state.open_positions.keys())
        if not held_pairs:
            return "🔄 No open positions to rotate."

        proposals = asyncio.run(orch.rotator.evaluate_rotation(
            held_pairs=held_pairs,
            all_pairs=orch.pairs,
            current_prices=orch.state.current_prices,
            portfolio_value=orch.state.portfolio_value,
        ))

        return orch.rotator.get_rotation_summary(proposals)
