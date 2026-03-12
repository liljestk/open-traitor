"""Financial Calendar routes — earnings, dividends, macro events + AI summaries."""
from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone

from fastapi import APIRouter, Query

import src.dashboard.deps as deps
from src.utils.logger import get_logger

logger = get_logger("dashboard.financial_calendar")

router = APIRouter(tags=["Financial Calendar"])


def _get_tickers_for_profile(profile: str) -> list[str]:
    """Extract Yahoo Finance tickers from a profile's trading pairs.

    Falls back to Redis-published watched pairs when config pairs list is
    empty (dynamic pair discovery mode).
    """
    cfg = deps.get_config_for_profile(profile)
    pairs = cfg.get("trading", {}).get("pairs", [])

    # Fallback: read dynamically discovered pairs from Redis
    if not pairs and deps.redis_client:
        try:
            raw = deps.redis_client.get(f"{profile}:news:watched_pairs")
            if raw:
                parsed = json.loads(raw if isinstance(raw, str) else raw.decode())
                if isinstance(parsed, list):
                    pairs = parsed
        except Exception:
            pass

    try:
        from src.core.equity_feed import pair_to_yahoo
        return [pair_to_yahoo(p) for p in pairs if p]
    except Exception:
        return []


@router.get("/api/financial-calendar", summary="Upcoming earnings, dividends, and macro events")
def get_financial_calendar(
    profile: str = Query("", description="Exchange profile (e.g. 'ibkr')"),
    days_ahead: int = Query(90, ge=7, le=365, description="Look-ahead window in days"),
):
    """Return a unified financial calendar for the equity watchlist.

    Combines earnings dates, ex-dividend dates, macro events (ECB/FOMC),
    and earnings season context. Returns empty data for crypto profiles.
    """
    resolved = deps.resolve_profile(profile)
    cfg = deps.get_config_for_profile(resolved)
    exchange = cfg.get("trading", {}).get("exchange", "").lower()

    # Only meaningful for equity profiles
    if exchange not in ("ibkr", "equity"):
        return {
            "domain": "crypto",
            "events": [],
            "earnings": {},
            "dividends": {},
            "macro": [],
            "earnings_season": {},
        }

    # Try Redis cache first
    redis = deps.redis_client
    cache_key = f"financial_calendar:{resolved}:{days_ahead}"
    if redis:
        try:
            cached = redis.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

    tickers = _get_tickers_for_profile(resolved)
    if not tickers:
        return {
            "domain": "equity",
            "events": [],
            "earnings": {},
            "dividends": {},
            "macro": [],
            "earnings_season": {},
        }

    try:
        from src.core.equity_feed import (
            get_earnings_calendar,
            get_dividend_calendar,
            get_macro_calendar,
            get_earnings_season_context,
        )

        earnings = get_earnings_calendar(tickers, days_ahead=days_ahead)
        dividends = get_dividend_calendar(tickers, days_ahead=days_ahead)
        macro = get_macro_calendar(days_ahead=days_ahead)
        earnings_season = get_earnings_season_context()

        # Build a unified event list for timeline display
        events: list[dict] = []

        for ticker, info in earnings.items():
            events.append({
                "type": "earnings",
                "ticker": ticker,
                "date": info["earnings_date"],
                "days_away": info["days_away"],
                "details": info,
                "importance": "high" if info["days_away"] <= 14 else "medium",
            })

        for ticker, info in dividends.items():
            events.append({
                "type": "dividend",
                "ticker": ticker,
                "date": info["ex_div_date"],
                "days_away": info["days_away"],
                "details": info,
                "importance": "medium" if info["days_away"] <= 14 else "low",
            })

        for macro_event in macro:
            events.append({
                "type": "macro",
                "ticker": "",
                "date": macro_event["date"],
                "days_away": macro_event["days_away"],
                "details": macro_event,
                "importance": macro_event.get("importance", "high"),
            })

        events.sort(key=lambda e: e["days_away"])

        result = {
            "domain": "equity",
            "events": events,
            "earnings": earnings,
            "dividends": dividends,
            "macro": macro,
            "earnings_season": earnings_season,
        }

        # Cache for 1 hour
        if redis:
            try:
                redis.set(cache_key, json.dumps(result), ex=3600)
            except Exception as e:
                logger.debug(f"Redis cache write failed: {e}")

        return result

    except Exception as e:
        logger.warning(f"Financial calendar fetch failed: {e}")
        return {
            "domain": "equity",
            "events": [],
            "earnings": {},
            "dividends": {},
            "macro": [],
            "earnings_season": {},
            "error": "Failed to fetch financial calendar",
        }


@router.get("/api/financial-calendar/summary", summary="AI-generated financial overview for a ticker")
async def get_financial_summary(
    ticker: str = Query(..., description="Yahoo Finance ticker (e.g. 'NOKIA.HE')"),
    profile: str = Query("", description="Exchange profile"),
):
    """Generate an AI overview of a company's financial position.

    Fetches key financial data from Yahoo Finance and uses the LLM to produce
    a concise summary covering: recent earnings, valuation, dividend policy,
    upcoming catalysts, and risk factors.

    Results are cached in Redis for 4 hours to avoid redundant LLM calls.
    """
    # Sanitize ticker
    ticker = ticker.strip().upper()
    if not ticker or len(ticker) > 20:
        return {"ticker": ticker, "summary": "", "error": "Invalid ticker"}

    # Redis cache check
    redis = deps.redis_client
    cache_key = f"financial_summary:{ticker}"
    if redis:
        try:
            cached = redis.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

    # Fetch financial data from Yahoo Finance
    try:
        from src.core.equity_feed import _fetch_quote_summary
    except ImportError:
        return {"ticker": ticker, "summary": "", "error": "equity_feed unavailable"}

    modules = "calendarEvents,summaryDetail,financialData,earnings,price"
    raw = _fetch_quote_summary(ticker, modules)
    if not raw:
        return {"ticker": ticker, "summary": "Financial data unavailable for this ticker.", "data": {}}

    # Extract key metrics
    price_data = raw.get("price", {})
    fin_data = raw.get("financialData", {})
    summary_detail = raw.get("summaryDetail", {})
    calendar = raw.get("calendarEvents", {})

    company_name = price_data.get("longName") or price_data.get("shortName") or ticker

    def _raw(d: dict, k: str):
        v = d.get(k, {})
        return v.get("raw") if isinstance(v, dict) else v

    metrics = {
        "company": company_name,
        "ticker": ticker,
        "market_cap": _raw(price_data, "marketCap"),
        "currency": price_data.get("currency", ""),
        "current_price": _raw(price_data, "regularMarketPrice"),
        "52w_high": _raw(summary_detail, "fiftyTwoWeekHigh"),
        "52w_low": _raw(summary_detail, "fiftyTwoWeekLow"),
        "pe_ratio": _raw(summary_detail, "trailingPE"),
        "forward_pe": _raw(summary_detail, "forwardPE"),
        "dividend_yield": _raw(summary_detail, "dividendYield"),
        "dividend_rate": _raw(summary_detail, "dividendRate"),
        "payout_ratio": _raw(summary_detail, "payoutRatio"),
        "revenue_growth": _raw(fin_data, "revenueGrowth"),
        "earnings_growth": _raw(fin_data, "earningsGrowth"),
        "profit_margins": _raw(fin_data, "profitMargins"),
        "return_on_equity": _raw(fin_data, "returnOnEquity"),
        "debt_to_equity": _raw(fin_data, "debtToEquity"),
        "free_cashflow": _raw(fin_data, "freeCashflow"),
        "recommendation": fin_data.get("recommendationKey"),
    }

    # Earnings dates
    earnings_dates = calendar.get("earnings", {}).get("earningsDate", [])
    if earnings_dates:
        import datetime as dt_mod
        upcoming = []
        for ed in earnings_dates:
            raw_ts = ed.get("raw")
            if raw_ts:
                d = datetime.fromtimestamp(raw_ts, tz=timezone.utc)
                upcoming.append(d.strftime("%Y-%m-%d"))
        metrics["upcoming_earnings"] = upcoming

    # Ex-dividend date
    ex_div = calendar.get("exDividendDate", {})
    if isinstance(ex_div, dict) and ex_div.get("raw"):
        d = datetime.fromtimestamp(ex_div["raw"], tz=timezone.utc)
        metrics["ex_dividend_date"] = d.strftime("%Y-%m-%d")

    # Format metrics for LLM
    def _fmt(val, pct=False, currency=""):
        if val is None:
            return "N/A"
        if pct:
            return f"{val * 100:.1f}%"
        if isinstance(val, (int, float)):
            if abs(val) >= 1e9:
                return f"{currency}{val / 1e9:.1f}B"
            if abs(val) >= 1e6:
                return f"{currency}{val / 1e6:.1f}M"
            return f"{currency}{val:,.2f}"
        return str(val)

    ccy = metrics["currency"]
    data_block = f"""Company: {metrics['company']} ({metrics['ticker']})
Price: {_fmt(metrics['current_price'], currency=ccy)} | 52W Range: {_fmt(metrics['52w_low'], currency=ccy)} – {_fmt(metrics['52w_high'], currency=ccy)}
Market Cap: {_fmt(metrics['market_cap'], currency=ccy)}
P/E (TTM): {_fmt(metrics['pe_ratio'])} | Forward P/E: {_fmt(metrics['forward_pe'])}
Dividend Yield: {_fmt(metrics['dividend_yield'], pct=True)} | Payout Ratio: {_fmt(metrics['payout_ratio'], pct=True)}
Revenue Growth: {_fmt(metrics['revenue_growth'], pct=True)} | Earnings Growth: {_fmt(metrics['earnings_growth'], pct=True)}
Profit Margins: {_fmt(metrics['profit_margins'], pct=True)} | ROE: {_fmt(metrics['return_on_equity'], pct=True)}
Debt/Equity: {_fmt(metrics['debt_to_equity'])} | Free Cash Flow: {_fmt(metrics['free_cashflow'], currency=ccy)}
Analyst Consensus: {metrics['recommendation'] or 'N/A'}
Upcoming Earnings: {', '.join(metrics.get('upcoming_earnings', [])) or 'N/A'}
Ex-Dividend Date: {metrics.get('ex_dividend_date', 'N/A')}"""

    # Generate AI summary via LLM
    summary_text = ""
    try:
        from src.core.llm_client import LLMClient, build_providers

        providers_cfg = deps.get_config().get("llm_providers", [])
        providers = build_providers(providers_cfg)

        llm = LLMClient(
            providers=providers,
            temperature=0.3,
            max_tokens=600,
        )

        system_prompt = (
            "You are a concise equity research analyst. Given key financial metrics "
            "for a publicly traded company, write a brief 3-4 paragraph overview covering: "
            "1) Current valuation & price context, 2) Growth & profitability outlook, "
            "3) Dividend policy (if applicable), 4) Key risks and upcoming catalysts. "
            "Be factual and data-driven. Use the metrics provided — do not fabricate numbers. "
            "Keep it under 200 words."
        )

        summary_text = await llm.chat(
            system_prompt=system_prompt,
            user_message=f"Provide a financial overview for:\n\n{data_block}",
            agent_name="financial_summary",
            priority="low",
        )
    except Exception as e:
        logger.warning(f"LLM summary generation failed for {ticker}: {e}")
        summary_text = (
            f"{metrics['company']} trades at {_fmt(metrics['current_price'], currency=ccy)} "
            f"with a P/E of {_fmt(metrics['pe_ratio'])}. "
            f"Dividend yield: {_fmt(metrics['dividend_yield'], pct=True)}."
        )

    result = {
        "ticker": ticker,
        "company": company_name,
        "summary": summary_text,
        "metrics": deps.sanitize_floats(metrics),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Cache for 4 hours
    if redis:
        try:
            redis.set(cache_key, json.dumps(result), ex=14400)
        except Exception:
            pass

    return result
