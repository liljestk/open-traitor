"""
Temporal Workflows for multi-horizon strategic planning.

Three cron-scheduled workflows:
  - DailyPlanWorkflow:    runs at midnight UTC every day
  - WeeklyReviewWorkflow: runs at midnight UTC every Monday
  - MonthlyReviewWorkflow: runs at midnight UTC on the 1st of each month

Each workflow:
  1. Fetches trade + portfolio history from StatsDB via activities
  2. Fetches domain-specific context (equity calendar or crypto universe)
  3. Calls the LLM to produce a structured plan (domain-aware prompts)
  4. Writes the plan back to StatsDB (strategic_context + daily_summaries)

Domain routing:
  - fetch_equity_events is called unconditionally; it returns a no-op
    {"domain": "crypto"} dict for non-equity profiles, so workflow code
    needs no branching.
  - fetch_pair_universe now accepts a profile arg and routes internally.

The fast orchestrator loop reads strategic_context on each cycle and injects
it as soft prompt context for agents -- no hard overrides.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from src.planning.activities import (
        evaluate_previous_plan,
        fetch_backtest_summary,
        fetch_score_divergence,
        fetch_trade_history,
        fetch_portfolio_history,
        call_planning_llm,
        write_strategic_context,
        write_daily_plan,
        fetch_pair_universe,
        fetch_universe_scan_summary,
        fetch_equity_events,
        run_nightly_backtests,
        run_event_regressions,
        run_regressions_for_followed_assets,
        run_price_backfill,
        run_finetune_export,
        run_taxonomy_seed,
        run_correlation_matrix,
        run_cross_event_regressions,
        run_outcome_attribution,
        run_counterfactual_replay,
        run_lead_lag_matrix,
        run_event_calendar_sync,
        run_decision_drift,
        run_reasoning_judge,
        run_onchain_sync,
    )


_ACTIVITY_TIMEOUT = timedelta(minutes=10)
_RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=10))


@workflow.defn
class DailyPlanWorkflow:
    """
    Daily strategic plan: reviews last 7 days, produces a regime assessment
    and focus areas for today. Writes to strategic_context (horizon='daily')
    and daily_summaries.plan_text.

    Cron: '0 0 * * *' (midnight UTC, every day)
    """

    @workflow.run
    async def run(self, profile: str = "") -> dict:
        workflow.logger.info(f"DailyPlanWorkflow: starting daily review (profile={profile!r})")

        workflow_id = workflow.info().workflow_id
        run_id = workflow.info().run_id

        # Evaluate how well yesterday's plan performed
        evaluation = await workflow.execute_activity(
            evaluate_previous_plan,
            args=["daily", profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        # Fetch last 7 days of trade + portfolio data
        portfolio_data = await workflow.execute_activity(
            fetch_portfolio_history,
            args=[7, profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        portfolio_data["previous_plan_evaluation"] = evaluation

        # Universe scan summary (crypto: pair movers; equity: no-op returns empty)
        scan_summary = await workflow.execute_activity(
            fetch_universe_scan_summary,
            args=[profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )
        portfolio_data["universe_scan"] = scan_summary

        # Domain-specific forward-looking context.
        # For equity: earnings dates, ex-div dates, ECB/FOMC events.
        # For crypto: returns {"domain": "crypto"} immediately (no external calls).
        equity_events = await workflow.execute_activity(
            fetch_equity_events,
            args=[profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )
        portfolio_data["equity_events"] = equity_events
        portfolio_data["domain"] = equity_events.get("domain", "crypto")

        # Backtest insights: recent simulation results per pair
        backtest_summary = await workflow.execute_activity(
            fetch_backtest_summary,
            args=[profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )
        portfolio_data["backtest_summary"] = backtest_summary

        # Score divergence: live entry_score vs backtest threshold
        score_divergence = await workflow.execute_activity(
            fetch_score_divergence,
            args=[profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )
        portfolio_data["score_divergence"] = score_divergence

        plan = await workflow.execute_activity(
            call_planning_llm,
            args=["daily", portfolio_data],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        summary = plan.get("summary", "No summary generated.")

        await workflow.execute_activity(
            write_strategic_context,
            args=["daily", plan, summary, workflow_id, run_id, profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        today = str(workflow.now().date())
        plan_text = (
            f"[{today}] DAILY PLAN | Regime: {plan.get('regime', '?')} | "
            f"Risk: {plan.get('risk_posture', '?')} | "
            f"Focus: {plan.get('today_focus', summary)}"
        )
        await workflow.execute_activity(
            write_daily_plan,
            args=[today, plan_text, profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        workflow.logger.info(f"DailyPlanWorkflow: complete --- {summary[:100]}")
        return plan


@workflow.defn
class WeeklyReviewWorkflow:
    """
    Weekly strategy review: reviews last 30 days, produces strategy adjustments,
    pair preferences, and risk posture. Writes to strategic_context (horizon='weekly').

    Cron: '0 0 * * 1' (midnight UTC, every Monday)
    """

    @workflow.run
    async def run(self, profile: str = "") -> dict:
        workflow.logger.info(f"WeeklyReviewWorkflow: starting weekly review (profile={profile!r})")

        workflow_id = workflow.info().workflow_id
        run_id = workflow.info().run_id

        evaluation = await workflow.execute_activity(
            evaluate_previous_plan,
            args=["weekly", profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        portfolio_data = await workflow.execute_activity(
            fetch_portfolio_history,
            args=[30, profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        trade_history = await workflow.execute_activity(
            fetch_trade_history,
            args=[30, None, profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        review_data = {
            **portfolio_data,
            "recent_trades": trade_history[:50],
            "previous_plan_evaluation": evaluation,
        }

        # Pair universe (crypto: Coinbase catalog; equity: EU large-cap list via equity_feed)
        universe_data = await workflow.execute_activity(
            fetch_pair_universe,
            args=[profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )
        scan_summary = await workflow.execute_activity(
            fetch_universe_scan_summary,
            args=[profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )
        review_data["universe"] = universe_data
        review_data["universe_scan"] = scan_summary

        # Forward-looking equity events (no-op for crypto)
        equity_events = await workflow.execute_activity(
            fetch_equity_events,
            args=[profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )
        review_data["equity_events"] = equity_events
        review_data["domain"] = equity_events.get("domain", "crypto")

        # Backtest insights: recent simulation results per pair
        backtest_summary = await workflow.execute_activity(
            fetch_backtest_summary,
            args=[profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )
        review_data["backtest_summary"] = backtest_summary

        plan = await workflow.execute_activity(
            call_planning_llm,
            args=["weekly", review_data],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        summary = plan.get("summary", "No summary generated.")

        await workflow.execute_activity(
            write_strategic_context,
            args=["weekly", plan, summary, workflow_id, run_id, profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        workflow.logger.info(f"WeeklyReviewWorkflow: complete --- {summary[:100]}")
        return plan


@workflow.defn
class MonthlyReviewWorkflow:
    """
    Monthly portfolio review: reviews last 90 days + YTD, produces macro regime
    assessment, portfolio allocation targets, and strategic themes.
    Writes to strategic_context (horizon='monthly').

    Cron: '0 0 1 * *' (midnight UTC, 1st of each month)
    """

    @workflow.run
    async def run(self, profile: str = "") -> dict:
        workflow.logger.info(f"MonthlyReviewWorkflow: starting monthly review (profile={profile!r})")

        workflow_id = workflow.info().workflow_id
        run_id = workflow.info().run_id

        portfolio_90d = await workflow.execute_activity(
            fetch_portfolio_history,
            args=[90, profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        portfolio_ytd = await workflow.execute_activity(
            fetch_portfolio_history,
            args=[365, profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        review_data = {
            **portfolio_90d,
            "ytd_stats": portfolio_ytd.get("trade_stats", {}),
            "ytd_pair_breakdown": portfolio_ytd.get("pair_breakdown", []),
        }

        # Forward-looking equity events give the monthly plan seasonal/macro context
        # (earnings season phase, ECB meeting schedule, upcoming ex-div dates).
        # No-op for crypto profiles.
        equity_events = await workflow.execute_activity(
            fetch_equity_events,
            args=[profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )
        review_data["equity_events"] = equity_events
        review_data["domain"] = equity_events.get("domain", "crypto")

        # Backtest insights: recent simulation results per pair
        backtest_summary = await workflow.execute_activity(
            fetch_backtest_summary,
            args=[profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )
        review_data["backtest_summary"] = backtest_summary

        plan = await workflow.execute_activity(
            call_planning_llm,
            args=["monthly", review_data],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        summary = plan.get("summary", "No summary generated.")

        await workflow.execute_activity(
            write_strategic_context,
            args=["monthly", plan, summary, workflow_id, run_id, profile],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY,
        )

        workflow.logger.info(f"MonthlyReviewWorkflow: complete --- {summary[:100]}")
        return plan


@workflow.defn
class NightlyBacktestWorkflow:
    """
    Nightly backtest runner: runs 30-day backtests on all followed pairs,
    saves results to backtest_runs for the daily planning prompt.

    Cron: '0 2 * * *' (2 AM UTC, every day)
    """

    @workflow.run
    async def run(self, profile: str = "") -> dict:
        workflow.logger.info(f"NightlyBacktestWorkflow: starting (profile={profile!r})")

        result = await workflow.execute_activity(
            run_nightly_backtests,
            args=[profile],
            start_to_close_timeout=timedelta(minutes=30),
            retry_policy=_RETRY,
        )

        workflow.logger.info(
            f"NightlyBacktestWorkflow: complete — "
            f"{result.get('saved', 0)}/{result.get('ran', 0)} pairs backtested"
        )
        return result


@workflow.defn
class EventRegressionWorkflow:
    """Nightly event–price regression refit.

    Re-fits OLS forward-return regressions for every (symbol, event_type,
    horizon) the system has data for. Cron: '30 2 * * *' (02:30 UTC).
    """

    @workflow.run
    async def run(self, profile: str = "") -> dict:
        workflow.logger.info(
            f"EventRegressionWorkflow: starting (profile={profile!r})"
        )
        event_result = await workflow.execute_activity(
            run_event_regressions,
            args=[profile],
            start_to_close_timeout=timedelta(minutes=45),
            retry_policy=_RETRY,
        )
        # Followed-asset coverage step: fits factor regressions for every
        # human/LLM follow so symbols without catalysts still land a row.
        followed_result = await workflow.execute_activity(
            run_regressions_for_followed_assets,
            args=[profile],
            start_to_close_timeout=timedelta(minutes=45),
            retry_policy=_RETRY,
        )
        workflow.logger.info(
            f"EventRegressionWorkflow: complete — "
            f"event_fitted={event_result.get('fitted')} "
            f"event_ok={event_result.get('ok')} "
            f"followed_symbols={len(followed_result.get('symbols', []))} "
            f"factor_rows={followed_result.get('factor_rows', 0)}"
        )
        return {
            "profile": profile,
            "event": event_result,
            "followed": followed_result,
        }


@workflow.defn
class NightlyPriceBackfillWorkflow:
    """Nightly OHLCV backfill that keeps ``historical_candles`` fresh.

    Runs at 01:00 UTC — strictly *before* the 02:30 UTC event-regression
    refit so the regression always sees up-to-date prices. First run for
    a symbol seeds up to ``PRICE_BACKFILL_LOOKBACK_YEARS`` (default 5y,
    cap 10y) of daily candles from trusted public sources; subsequent
    runs only walk the missing tail via ``backfill_progress`` resume.
    """

    @workflow.run
    async def run(self, profile: str = "") -> dict:
        workflow.logger.info(
            f"NightlyPriceBackfillWorkflow: starting (profile={profile!r})"
        )
        # Generous timeout: first run for a hundred symbols at 5y daily
        # candles can take a while; the activity heartbeats per symbol so
        # Temporal won't kill it as long as it's making progress.
        result = await workflow.execute_activity(
            run_price_backfill,
            args=[profile],
            start_to_close_timeout=timedelta(hours=4),
            heartbeat_timeout=timedelta(minutes=10),
            retry_policy=_RETRY,
        )
        workflow.logger.info(
            f"NightlyPriceBackfillWorkflow: complete \u2014 "
            f"symbols={result.get('symbols')} "
            f"rows_written={result.get('rows_written')}"
        )
        return result


@workflow.defn
class FinetuneExportWorkflow:
    """Monthly fine-tuning dataset export.

    Curates trade reasoning samples (with human label upweighting) into the
    Ollama / OpenAI fine-tuning JSONL files. Cron: ``'0 4 1 * *'``
    (04:00 UTC on the 1st of every month).

    Closes the compounded-learning loop: trades → reasoning → labels →
    dataset → operator-driven model retrain.
    """

    @workflow.run
    async def run(self, profile: str = "", window_days: int = 90) -> dict:
        workflow.logger.info(
            f"FinetuneExportWorkflow: starting (profile={profile!r}, "
            f"window_days={window_days})"
        )
        result = await workflow.execute_activity(
            run_finetune_export,
            args=[profile, window_days],
            start_to_close_timeout=timedelta(hours=1),
            retry_policy=_RETRY,
        )
        workflow.logger.info(
            f"FinetuneExportWorkflow: complete — "
            f"examples={result.get('total_examples', 0)} "
            f"skipped={result.get('skipped', False)}"
        )
        return result


@workflow.defn
class CrossAssetAnalyticsWorkflow:
    """Nightly cross-asset analytics: taxonomy + correlation matrix +
    cluster snapshot + cross-event regressions.

    Runs at 02:00 UTC — *after* the 01:00 price backfill (fresh candles)
    and *before* the 02:30 event regressions (so per-symbol regressions
    can downstream consume the cluster snapshot if they wish). All four
    sub-steps run in sequence inside one workflow so a single Temporal
    schedule covers the full pipeline per profile.
    """

    @workflow.run
    async def run(self, profile: str = "") -> dict:
        workflow.logger.info(
            f"CrossAssetAnalyticsWorkflow: starting (profile={profile!r})"
        )
        taxonomy = await workflow.execute_activity(
            run_taxonomy_seed,
            args=[profile],
            start_to_close_timeout=timedelta(minutes=30),
            retry_policy=_RETRY,
        )
        correlations = await workflow.execute_activity(
            run_correlation_matrix,
            args=[profile],
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=_RETRY,
        )
        cross_events = await workflow.execute_activity(
            run_cross_event_regressions,
            args=[profile],
            start_to_close_timeout=timedelta(hours=1),
            retry_policy=_RETRY,
        )
        workflow.logger.info(
            f"CrossAssetAnalyticsWorkflow: complete — "
            f"taxonomy={taxonomy.get('rows_written', 0)} "
            f"pairs={correlations.get('pairs', 0)} "
            f"clusters={correlations.get('clusters', 0)} "
            f"cross_regressions={cross_events.get('regressions', 0)}"
        )
        return {
            "profile": profile,
            "taxonomy": taxonomy,
            "correlations": correlations,
            "cross_events": cross_events,
        }


@workflow.defn
class SmartsNightlyWorkflow:
    """Phase 1/3/6 nightly: attribution + counterfactual + lead-lag + drift."""

    @workflow.run
    async def run(self, profile: str = "") -> dict:
        workflow.logger.info(f"SmartsNightlyWorkflow: starting (profile={profile!r})")
        attribution = await workflow.execute_activity(
            run_outcome_attribution, args=[profile],
            start_to_close_timeout=timedelta(minutes=20), retry_policy=_RETRY,
        )
        replay = await workflow.execute_activity(
            run_counterfactual_replay, args=[profile],
            start_to_close_timeout=timedelta(minutes=30), retry_policy=_RETRY,
        )
        lead_lag = await workflow.execute_activity(
            run_lead_lag_matrix, args=[profile],
            start_to_close_timeout=timedelta(minutes=20), retry_policy=_RETRY,
        )
        drift = await workflow.execute_activity(
            run_decision_drift, args=[profile],
            start_to_close_timeout=timedelta(minutes=10), retry_policy=_RETRY,
        )
        return {"profile": profile, "attribution": attribution, "replay": replay,
                "lead_lag": lead_lag, "drift": drift}


@workflow.defn
class SmartsHourlyWorkflow:
    """Phase 4/7 hourly: macro/event-calendar refresh + on-chain signals."""

    @workflow.run
    async def run(self, profile: str = "") -> dict:
        workflow.logger.info(f"SmartsHourlyWorkflow: starting (profile={profile!r})")
        events = await workflow.execute_activity(
            run_event_calendar_sync, args=[profile],
            start_to_close_timeout=timedelta(minutes=10), retry_policy=_RETRY,
        )
        onchain = await workflow.execute_activity(
            run_onchain_sync, args=[profile],
            start_to_close_timeout=timedelta(minutes=10), retry_policy=_RETRY,
        )
        return {"profile": profile, "events": events, "onchain": onchain}


@workflow.defn
class SmartsJudgeWorkflow:
    """Phase 6 (every 6h): LLM-judge a sample of recent reasoning."""

    @workflow.run
    async def run(self, profile: str = "") -> dict:
        workflow.logger.info(f"SmartsJudgeWorkflow: starting (profile={profile!r})")
        judged = await workflow.execute_activity(
            run_reasoning_judge, args=[profile],
            start_to_close_timeout=timedelta(minutes=30), retry_policy=_RETRY,
        )
        return {"profile": profile, "judged": judged}
