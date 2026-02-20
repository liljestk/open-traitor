import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from src.utils.logger import get_logger
from src.utils.helpers import format_currency, format_percentage
from src.utils.tracer import get_llm_tracer

logger = get_logger("core.pipeline")

class PipelineManager:
    """Manages the execution of the main trading pipeline across all pairs."""
    
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    async def run_pipeline(self, pair: str) -> None:
        """Run the full analysis → strategy → risk → execute pipeline for a pair asynchronously."""
        # Unpack dependencies from orchestrator for brevity
        orch = self.orchestrator
        
        logger.info(f"🔍 Analyzing {pair}...")

        if orch.ws_feed:
            with orch._ws_trigger_lock:
                ws_now = orch.ws_feed.get_price(pair)
                if ws_now > 0:
                    orch._ws_last_prices[pair] = ws_now

        cycle_id = str(uuid.uuid4())
        strategic_context = orch._get_strategic_context()

        # Run synchronous blocking functions in executor if necessary
        orch._maybe_refresh_holdings()

        tracer = get_llm_tracer()
        trace_ctx = tracer.start_trace(
            cycle_id=cycle_id,
            pair=pair,
            metadata={"strategic_context_preview": strategic_context[:200]},
        ) if tracer else None

        # Data fetching (synchronous -> use asyncio.to_thread if we want true non-blocking,
        # but for now we let it block slightly since Coinbase REST is fast)
        orch.rate_limiter.wait("coinbase_rest")
        candles = await asyncio.to_thread(
            orch.coinbase.get_candles,
            pair,
            granularity=orch.config.get("analysis", {}).get("technical", {}).get(
                "candle_granularity", "ONE_HOUR"
            ),
        )

        if orch.ws_feed:
            price = orch.ws_feed.get_price(pair)
            if price <= 0:
                orch.rate_limiter.wait("coinbase_rest")
                price = await asyncio.to_thread(orch.coinbase.get_current_price, pair)
        else:
            orch.rate_limiter.wait("coinbase_rest")
            price = await asyncio.to_thread(orch.coinbase.get_current_price, pair)

        if price <= 0:
            logger.warning(
                f"⚠️ Skipping pipeline for {pair}: price is {price} "
                "(both WebSocket and REST returned an invalid value)"
            )
            return

        orch.state.update_price(pair, price)

        news_headlines = "No news available."
        if orch.news:
            news_headlines = await asyncio.to_thread(
                orch.news.get_headlines,
                orch.config.get("news", {}).get("articles_for_analysis", 15)
            )
        elif orch.redis:
            try:
                cached = orch.redis.get("news:latest")
                if cached:
                    articles = json.loads(cached)
                    news_headlines = "\n".join(
                        f"- [{a.get('source', '?')}] {a.get('title', '')}"
                        for a in articles[:15]
                    )
            except Exception:
                pass

        fg_prompt = ""
        try:
            fg_prompt = orch.fear_greed.get_for_prompt()
        except Exception as e:
            logger.debug(f"Fear & Greed unavailable: {e}")

        mtf_prompt = ""
        try:
            mtf_prompt = orch.multi_tf.get_for_prompt(pair)
        except Exception as e:
            logger.debug(f"Multi-TF unavailable: {e}")

        recent_outcomes = ""
        try:
            recent_outcomes = await asyncio.to_thread(
                orch.stats_db.get_recent_outcomes,
                pair, n=10, currency_symbol=orch.state.currency_symbol
            )
        except Exception as e:
            logger.debug(f"Failed to load recent outcomes: {e}")

        # Step 2: Market Analysis
        analysis_result = await orch.market_analyst.execute({
            "pair": pair,
            "candles": candles,
            "news_headlines": news_headlines,
            "fear_greed": fg_prompt,
            "multi_timeframe": mtf_prompt,
            "strategic_context": strategic_context,
            "currency_symbol": orch.state.currency_symbol,
            "cycle_id": cycle_id,
            "stats_db": orch.stats_db,
            "trace_ctx": trace_ctx,
        })

        signal = analysis_result.get("signal", {})
        if "error" in analysis_result:
            logger.warning(f"Analysis failed for {pair}: {analysis_result.get('error')}")
            orch.journal.log_decision("analysis_failed", pair, "none", {"error": analysis_result.get('error')})
            if trace_ctx is not None:
                trace_ctx.finish(metadata={"action": "analysis_failed", "error": analysis_result.get("error", "")})
            return

        confidence = signal.get("confidence", 0)
        notify_threshold = orch.config.get("telegram", {}).get("notify_on_signal_confidence", 0.8)
        if confidence >= notify_threshold and orch.telegram:
            signal_obj = orch.state.signals[-1] if orch.state.signals else None
            if signal_obj:
                orch.telegram.send_signal_notification(signal_obj.to_summary())

        # Step 3: Strategy Generation
        strategy_result = await orch.strategist.execute({
            "signal": signal,
            "active_tasks": [t.to_dict() for t in orch.active_tasks if not t.completed],
            "current_balance": orch.coinbase.balance if hasattr(orch.coinbase, 'balance') else {},
            "open_positions": orch.state.open_positions,
            "recent_trades": [t.to_summary() for t in orch.state.recent_trades],
            "recent_outcomes": recent_outcomes,
            "strategic_context": strategic_context,
            "live_holdings_summary": orch.state.holdings_summary,
            "native_currency": orch.state.native_currency,
            "currency_symbol": orch.state.currency_symbol,
            "cycle_id": cycle_id,
            "stats_db": orch.stats_db,
            "trace_ctx": trace_ctx,
        })

        if strategy_result.get("action") == "hold":
            logger.info(f"📊 {pair}: HOLD — {strategy_result.get('reason', strategy_result.get('reasoning', 'No action'))}")
            orch.journal.log_decision("hold", pair, "hold", {"signal": signal, "reasoning": strategy_result.get('reason', '')})
            if trace_ctx is not None:
                trace_ctx.finish(metadata={"action": "hold", "reason": strategy_result.get("reason", "")})
            return

        strategy_result["current_price"] = price

        # Step 4: Risk Validation
        risk_portfolio_value = (
            orch.state.live_portfolio_value if orch.state.live_portfolio_value > 0 else orch.state.portfolio_value
        )
        risk_cash_balance = (
            sum(orch.state.live_cash_balances.values()) if orch.state.live_cash_balances else orch.state.cash_balance
        )
        risk_result = await orch.risk_manager.execute({
            "proposal": strategy_result,
            "portfolio_value": risk_portfolio_value,
            "cash_balance": risk_cash_balance,
            "cycle_id": cycle_id,
            "stats_db": orch.stats_db,
            "trace_ctx": trace_ctx,
        })

        if not risk_result.get("approved"):
            logger.info(f"🚫 {pair}: Trade rejected — {risk_result.get('reason', 'Unknown')}")
            orch.journal.log_decision("trade_rejected", pair, strategy_result.get("action", "unknown"), {
                "reason": risk_result.get("reason", "Unknown"),
                "proposal": strategy_result,
            })
            orch.audit.log_rule_check("risk_validation", passed=False, details=risk_result.get('reason', 'Unknown'))
            if trace_ctx is not None:
                trace_ctx.finish(metadata={"action": "rejected", "reason": risk_result.get("reason", "")})
            return

        # Step 5: Handle approval or execute
        if risk_result.get("needs_approval"):
            trade_desc = (
                f"{risk_result['action'].upper()} {risk_result['pair']}\n"
                f"Amount: {format_currency(risk_result['quote_amount'])}\n"
                f"Price: {format_currency(risk_result['price'])}\n"
                f"Stop-Loss: {format_currency(risk_result.get('stop_loss', 0))}\n"
                f"Take-Profit: {format_currency(risk_result.get('take_profit', 0))}\n"
                f"Confidence: {format_percentage(risk_result.get('confidence', 0))}"
            )
            trade_id = f"pending_{uuid.uuid4().hex[:8]}"
            with orch._pending_approvals_lock:
                risk_result["_queued_at"] = datetime.now(timezone.utc).isoformat()
                orch._pending_approvals[trade_id] = risk_result

            if orch.telegram:
                orch.telegram.request_approval(trade_desc, trade_id)
            if trace_ctx is not None:
                trace_ctx.finish(metadata={"action": "pending_approval", "trade_id": trade_id})
            return

        # Step 6: Execute Trade
        exec_result = await orch.executor.execute({
            "approved_trade": risk_result,
        })

        if exec_result.get("executed"):
            # Persist trade to StatsDB
            try:
                stats_trade_id = await asyncio.to_thread(
                    orch.stats_db.record_trade,
                    pair=risk_result.get('pair', pair),
                    action=risk_result.get('action', 'unknown'),
                    price=risk_result.get('price', price),
                    quantity=(
                        exec_result.get('trade', {}).get('filled_quantity')
                        or exec_result.get('trade', {}).get('quantity')
                        or 0
                    ),
                    quote_amount=risk_result.get('quote_amount', risk_result.get('usd_amount', 0)),
                    confidence=risk_result.get('confidence', 0),
                    signal_type=signal.get('signal_type', ''),
                    stop_loss=risk_result.get('stop_loss', 0),
                    take_profit=risk_result.get('take_profit', 0),
                    reasoning=risk_result.get('reasoning', ''),
                )
            except Exception as e:
                logger.debug(f"Failed to record trade in StatsDB: {e}")

            orch.journal.log_trade(
                pair=risk_result.get('pair', pair),
                action=risk_result.get('action', 'unknown'),
                quantity=(
                    exec_result.get('trade', {}).get('filled_quantity')
                    or exec_result.get('trade', {}).get('quantity')
                    or 0
                ),
                price=risk_result.get('price', price),
                quote_amount=risk_result.get('quote_amount', risk_result.get('usd_amount', 0)),
                confidence=risk_result.get('confidence', 0),
                signal_type=signal.get('signal_type', ''),
                stop_loss=risk_result.get('stop_loss', 0),
                take_profit=risk_result.get('take_profit', 0),
                reasoning=risk_result.get('reasoning', ''),
                fear_greed=orch.fear_greed.last_value or 0,
                rsi=signal.get('rsi', 0),
                macd_signal=signal.get('macd_signal', ''),
            )
            orch.audit.log_trade(
                pair=risk_result.get('pair', pair),
                action=risk_result.get('action', 'unknown'),
                amount=risk_result.get('quote_amount', risk_result.get('usd_amount', 0)),
                price=risk_result.get('price', price),
            )

            if risk_result.get('action') == 'buy':
                orch.trailing_stops.add_stop(
                    pair=risk_result.get('pair', pair),
                    entry_price=risk_result.get('price', price),
                    initial_stop=risk_result.get('stop_loss'),
                )

            trade_event = (
                f"{'BUY' if risk_result['action'] == 'buy' else 'SELL'} "
                f"{risk_result['pair']} — "
                f"{format_currency(risk_result.get('quote_amount', risk_result.get('usd_amount', 0)))} "
                f"at {format_currency(risk_result['price'])} "
                f"(confidence: {format_percentage(risk_result.get('confidence', 0))})"
            )
            orch.chat_handler.queue_event(f"Trade executed: {trade_event}")

            if orch.telegram and orch.config.get("telegram", {}).get("notify_on_trade", True):
                orch.telegram.send_trade_notification(trade_event)

            if trace_ctx is not None:
                try:
                    trace_ctx.finish(metadata={
                        "trade_executed": True,
                        "action": risk_result.get("action"),
                        "quote_amount": risk_result.get("quote_amount"),
                        "confidence": risk_result.get("confidence"),
                    })
                except Exception:
                    pass
