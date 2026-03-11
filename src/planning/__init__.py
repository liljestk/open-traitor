"""
Planning package — Temporal-backed multi-horizon planning workflows.

Provides daily, weekly, and monthly planning workflows that run on cron
schedules, review trading history, call the LLM for strategic assessment,
and write their output to StatsDB. The fast execution loop (orchestrator)
reads the latest strategic context each cycle and injects it as soft prompt
context for the MarketAnalyst and Strategist agents.

Horizons:
  - daily:   midnight UTC, reviews last 7 days of trades/signals
  - weekly:  Monday midnight UTC, reviews last 30 days
  - monthly: 1st of month midnight UTC, reviews last 90 days + YTD
"""
