"""
Smart data formatters — make raw data feel like a trader talking.
"""

from __future__ import annotations

import json


def _format_status(data: dict) -> str:
    """Format portfolio status using live exchange data."""
    sym = data.get("currency_symbol", "$")
    pv = data.get("portfolio_value", data.get("portfolio_value_usd", 0))
    pnl = data.get("bot_pnl", data.get("bot_pnl_usd", data.get("total_pnl", 0)))
    trades = data.get("bot_trades_executed", data.get("total_trades", 0))
    wr = data.get("win_rate", 0)
    dd = data.get("max_drawdown", 0)
    paused = data.get("is_paused", False)
    cb = data.get("circuit_breaker", False)
    fetched = data.get("data_fetched_at", "")

    pnl_emoji = "💰" if pnl > 0 else "🔻" if pnl < 0 else "➖"

    lines = [
        f"💼 *Portfolio: {sym}{pv:,.2f}* (live)",
        f"Bot PnL: {pnl_emoji} {sym}{pnl:,.2f} | Trades: {trades} | Win: {wr*100:.0f}%",
        f"Max DD: {dd*100:.1f}%",
    ]
    if fetched:
        lines.append(f"_Data: {fetched}_")

    # Show non-fiat holdings
    holdings = data.get("holdings", [])
    assets = [h for h in holdings if not h.get("is_fiat", False)][:6]
    if assets:
        lines.append(f"\n📊 *Holdings:*")
        for h in assets:
            val = h.get("native_value", 0)
            price = h.get("price", 0)
            lines.append(
                f"  • *{h['currency']}*: {h['amount']:.6g}"
                + (f" @ {sym}{price:,.4g} = {sym}{val:,.2f}" if price > 0 else f" = {sym}{val:,.2f}")
            )

    if paused:
        lines.append("\n⏸️ _Trading paused_")
    if cb:
        lines.append("\n🛑 _CIRCUIT BREAKER ACTIVE_")

    return "\n".join(lines)


def _format_balance(data: dict) -> str:
    sym = data.get("currency_symbol", "$")
    total = data.get("total_portfolio", data.get("total_portfolio_usd", data.get("portfolio_value", "?")))
    cash = data.get("fiat_cash", data.get("fiat_cash_usd", data.get("cash_balance", "?")))
    pnl = data.get("bot_pnl", data.get("bot_pnl_usd", data.get("total_pnl", 0)))
    fetched = data.get("fetched_at", data.get("data_fetched_at", ""))
    pnl_str = f"{sym}{pnl:,.2f}" if isinstance(pnl, (int, float)) else str(pnl)
    total_str = f"{sym}{total:,.2f}" if isinstance(total, (int, float)) else str(total)
    cash_str = f"{sym}{cash:,.2f}" if isinstance(cash, (int, float)) else str(cash)
    lines = [
        f"💼 *Portfolio (live): {total_str}*",
        f"💵 Fiat cash: {cash_str}",
        f"📊 Bot PnL: {pnl_str}",
    ]
    # Show fiat accounts if present
    for fa in data.get("fiat_accounts", []):
        lines.append(f"   {fa['currency']}: {fa['amount']:,.4g}")
    if fetched:
        lines.append(f"_Data fetched: {fetched}_")
    return "\n".join(lines)


def _format_positions(data: dict) -> str:
    """Format actual exchange holdings."""
    holdings = data.get("coinbase_holdings", data.get("holdings", []))
    fetched = data.get("fetched_at", "")
    if not holdings:
        # Fallback: legacy format
        positions = data.get("open_positions", {})
        if not positions:
            return "📭 No open positions found."
        lines = [f"📊 *{len(positions)} Bot Positions:*\n"]
        for pair, qty in positions.items():
            lines.append(f"• *{pair}*: {qty:.6f}")
        return "\n".join(lines)

    sym = data.get("currency_symbol", "$")
    total_val = data.get("total_crypto_value", data.get("total_crypto_usd", data.get("total_value", sum(h.get("native_value", 0) for h in holdings))))
    lines = [f"📊 *{len(holdings)} Holdings (live)* — {sym}{total_val:,.2f} total\n"]
    for h in holdings:
        price = h.get("price", 0)
        val = h.get("native_value", 0)
        amt = h.get("amount", 0)
        lines.append(
            f"• *{h['currency']}*: {amt:.6g}"
            + (f" @ {sym}{price:,.4g}" if price > 0 else "")
            + f" = *{sym}{val:,.2f}*"
        )
    if fetched:
        lines.append(f"\n_Data: {fetched}_")
    return "\n".join(lines)


def _format_prices(data: dict) -> str:
    sym = data.get("currency_symbol", "$")
    prices = data.get("prices", {})
    fetched = data.get("fetched_at", "")
    if not prices:
        return "No price data yet."
    lines = ["💲 *Current Prices (live):*\n"]
    for pair, price in sorted(prices.items()):
        lines.append(f"• *{pair}*: {sym}{price:,.2f}")
    if fetched:
        lines.append(f"\n_Fetched: {fetched}_")
    return "\n".join(lines)


def _format_trades(data: dict) -> str:
    trades = data.get("trades", [])
    if not trades:
        return "📭 No trades yet."
    lines = [f"📋 *Last {len(trades)} Trades:*\n"]
    for t in trades[-8:]:  # Last 8 trades max
        if isinstance(t, str):
            lines.append(f"• {t}")
        else:
            lines.append(f"• {t}")
    return "\n".join(lines)


def _format_fear_greed(data: dict) -> str:
    fg = data.get("fear_greed", {})
    if isinstance(fg, dict):
        val = fg.get("value", "?")
        label = fg.get("label", "?")
        return f"😱 *Fear & Greed: {val}* — _{label}_"
    return f"😱 Fear & Greed: {fg}"


def _format_signals(data: dict) -> str:
    signals = data.get("signals", [])
    if not signals:
        return "📡 No recent signals."
    lines = ["📡 *Recent Signals:*\n"]
    for s in signals[-6:]:
        if isinstance(s, dict):
            conf = s.get("confidence", 0)
            emoji = "🟢" if conf > 0.7 else "🟡" if conf > 0.4 else "🔴"
            lines.append(
                f"{emoji} *{s.get('pair', '?')}* {s.get('signal_type', '?')} "
                f"({conf*100:.0f}%)"
            )
        else:
            lines.append(f"• {s}")
    return "\n".join(lines)


def _format_account_holdings(data: dict) -> str:
    """Format the raw live exchange account snapshot."""
    sym = data.get("currency_symbol", "$")
    holdings = data.get("holdings", [])
    total = data.get("total_portfolio", data.get("total_portfolio_usd", 0))
    fetched = data.get("fetch_ts", "")
    if not holdings:
        return "📭 No account holdings found."
    lines = [f"💼 *Account Holdings (live)* — {sym}{total:,.2f} total\n"]
    for h in holdings:
        price = h.get("price", 0)
        val = h.get("native_value", 0)
        amt = h.get("amount", 0)
        tag = " 💵" if h.get("is_fiat") else ""
        lines.append(
            f"• *{h['currency']}*{tag}: {amt:.6g}"
            + (f" @ {sym}{price:,.4g}" if price > 0 else "")
            + f" = *{sym}{val:,.2f}*"
        )
    if fetched:
        lines.append(f"\n_Data: {fetched}_")
    return "\n".join(lines)


def _format_simulations(data: dict) -> str:
    sims = data.get("simulations", [])
    if not sims:
        return "📭 No active simulations."
    lines = [f"🧪 *{len(sims)} Active Simulations:*\n"]
    for s in sims:
        pnl = s.get("pnl_abs", 0)
        pct = s.get("pnl_pct", 0)
        emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
        lines.append(
            f"• `{s['id']}` *{s['pair']}* "
            f"({s['from_amount']} {s['from_currency']} → {s['quantity']:.4g} {s['to_currency']})\n"
            f"  {emoji} PnL: {pnl:+.2f} ({pct:+.2f}%) | Entry: {s['entry_price']:.4g} → Now: {s.get('current_price', s['entry_price']):.4g}"
        )
    return "\n".join(lines)


def _format_settings_tiers(data: dict) -> str:
    """Format Telegram safety tiers as a readable message."""
    lines = ["🔒 **Telegram Settings Access Tiers**\n"]
    tier_icons = {"safe": "🟢", "semi_safe": "🟡", "blocked": "🔴"}
    for tier, sections in data.items():
        icon = tier_icons.get(tier, "⚪")
        label = tier.replace("_", " ").title()
        lines.append(f"{icon} **{label}**: {', '.join(sections)}")
    lines.append("\n_Use 'update settings' to change safe/semi-safe sections._")
    return "\n".join(lines)


# Map function names to formatters
DATA_FORMATTERS = {
    "get_status": _format_status,
    "get_balance": _format_balance,
    "get_positions": _format_positions,
    "get_current_prices": _format_prices,
    "get_recent_trades": _format_trades,
    "get_fear_greed": _format_fear_greed,
    "get_recent_signals": _format_signals,
    "get_account_holdings": _format_account_holdings,
    "list_simulations": _format_simulations,
    "get_settings_tiers": _format_settings_tiers,
}
