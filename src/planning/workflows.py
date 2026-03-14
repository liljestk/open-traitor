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
