from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from src.utils.qc_filter import qc_where


class ReasoningMixin:
    """Mixin supplying agent-reasoning and strategic-context persistence."""

    # --- Agent Reasoning ----------------------------------------------------

    def save_reasoning(
        self,
        cycle_id: str,
        pair: str,
        agent_name: str,
        reasoning_json: dict,
        signal_type: str = "",
        confidence: float = 0.0,
        trade_id: Optional[int] = None,
        langfuse_trace_id: Optional[str] = None,
        langfuse_span_id: Optional[str] = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: float = 0.0,
        raw_prompt: str = "",
        exchange: str = "coinbase",
    ) -> int:
        """Persist a full LLM reasoning trace for one agent call."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO agent_reasoning
                   (exchange, cycle_id, pair, agent_name, reasoning_json, signal_type, confidence,
                    trade_id, langfuse_trace_id, langfuse_span_id,
                    prompt_tokens, completion_tokens, latency_ms, raw_prompt)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    exchange, cycle_id, pair, agent_name,
                    json.dumps(reasoning_json, default=str),
                    signal_type, confidence, trade_id,
                    langfuse_trace_id, langfuse_span_id,
                    prompt_tokens, completion_tokens, latency_ms, raw_prompt,
                ),
            )
            row = cursor.fetchone()
            conn.commit()
            return row["id"]

    def backfill_reasoning_trade_id(self, cycle_id: str, trade_id: int) -> None:
        """Link all reasoning rows for a cycle to the trade that resulted from it."""
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE agent_reasoning SET trade_id = %s WHERE cycle_id = %s AND trade_id IS NULL",
                (trade_id, cycle_id),
            )
            conn.commit()

    def get_recent_outcomes(self, pair: str, n: int = 10, currency_symbol: str = "$") -> str:
        """
        Return a human-readable summary of the last N closed trades for a pair,
        with the reasoning that produced them. Used for outcome feedback injection
        into agent prompts.
        """
        sym = currency_symbol
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT
                    t.ts, t.action, t.price, t.pnl, t.confidence, t.signal_type,
                    ar.reasoning_json, ar.agent_name
                   FROM trades t
                   LEFT JOIN agent_reasoning ar
                       ON ar.trade_id = t.id AND ar.agent_name = 'market_analyst'
                   WHERE t.pair = %s AND t.pnl IS NOT NULL
                   ORDER BY t.ts DESC
                   LIMIT %s""",
                (pair, n),
            ).fetchall()

        if not rows:
            return "No closed trade history for this pair yet."

        lines = []
        for r in rows:
            pnl_str = f"+{sym}{r['pnl']:.2f}" if r["pnl"] >= 0 else f"-{sym}{abs(r['pnl']):.2f}"
            outcome = "WIN" if r["pnl"] >= 0 else "LOSS"
            key_factors = "N/A"
            if r["reasoning_json"]:
                try:
                    rj = json.loads(r["reasoning_json"])
                    factors = rj.get("key_factors", [])
                    if factors:
                        key_factors = ", ".join(str(f) for f in factors[:3])
                except Exception:
                    pass
            lines.append(
                f"[{r['ts'][:10]}] {outcome} {pnl_str} | {r['action'].upper()} "
                f"@ {sym}{r['price']:,.2f} | signal={r['signal_type']} "
                f"conf={r['confidence']:.0%} | factors: {key_factors}"
            )

        return "\n".join(lines)

    def get_cycles(
        self,
        pair: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        quote_currency: str | list[str] | None = None,
        exchange: str | None = None,
    ) -> list[dict]:
        """
        Return a paginated list of trading cycles with outcome summary.
        Each row represents one unique cycle_id with its final trade outcome.
        Used by the dashboard Cycle Explorer page.
        """
        with self._get_conn() as conn:
            _select = """SELECT
                        ar.cycle_id,
                        ar.pair,
                        MIN(ar.ts) as started_at,
                        MAX(ar.ts) as finished_at,
                        COUNT(DISTINCT ar.agent_name) as agent_count,
                        MAX(CASE WHEN ar.agent_name='market_analyst' THEN ar.signal_type END) as signal_type,
                        MAX(CASE WHEN ar.agent_name='market_analyst' THEN ar.confidence END) as confidence,
                        MAX(CASE WHEN ar.agent_name='strategist' THEN
                            ar.reasoning_json::json->>'action' END) as action,
                        t.id as trade_id,
                        t.pnl,
                        t.quote_amount,
                        t.price,
                        ar.langfuse_trace_id,
                        SUM(ar.prompt_tokens) as total_prompt_tokens,
                        SUM(ar.completion_tokens) as total_completion_tokens,
                        SUM(ar.latency_ms) as total_latency_ms
                       FROM agent_reasoning ar
                       LEFT JOIN trades t ON t.id = ar.trade_id"""
            _group = " GROUP BY ar.cycle_id, ar.pair, t.id, t.pnl, t.quote_amount, t.price, ar.langfuse_trace_id ORDER BY started_at DESC LIMIT %s OFFSET %s"

            if pair:
                rows = conn.execute(
                    _select + " WHERE ar.pair = %s" + _group,
                    (pair, limit, offset),
                ).fetchall()
            else:
                qc_frag, qc_params = qc_where(quote_currency, col="ar.pair")
                exch_frag = " AND ar.exchange = %s" if exchange else ""
                exch_params = [exchange] if exchange else []
                rows = conn.execute(
                    _select + " WHERE 1=1" + qc_frag + exch_frag + _group,
                    (*qc_params, *exch_params, limit, offset),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_cycle_full(self, cycle_id: str) -> Optional[dict]:
        """
        Return the complete trace for one cycle: all agent spans + trade outcome.
        Used by the dashboard Cycle Playback page and the REST API.
        """
        with self._get_conn() as conn:
            spans = conn.execute(
                """SELECT
                    ar.id, ar.ts, ar.agent_name, ar.reasoning_json,
                    ar.signal_type, ar.confidence, ar.langfuse_trace_id,
                    ar.langfuse_span_id, ar.prompt_tokens, ar.completion_tokens,
                    ar.latency_ms, ar.raw_prompt, ar.pair
                   FROM agent_reasoning ar
                   WHERE ar.cycle_id = %s
                   ORDER BY ar.ts ASC""",
                (cycle_id,),
            ).fetchall()

            if not spans:
                return None

            # Parse JSON fields
            spans_list = []
            for s in spans:
                row = dict(s)
                try:
                    row["reasoning_json"] = json.loads(row["reasoning_json"] or "{}")
                except Exception:
                    pass
                spans_list.append(row)

            # Trade outcome (if one resulted from this cycle)
            trade_row = conn.execute(
                """SELECT t.* FROM trades t
                   INNER JOIN agent_reasoning ar ON ar.trade_id = t.id
                   WHERE ar.cycle_id = %s
                   LIMIT 1""",
                (cycle_id,),
            ).fetchone()

        first = spans_list[0]
        last = spans_list[-1]
        total_latency = sum(s["latency_ms"] or 0 for s in spans_list)
        total_tokens = sum((s["prompt_tokens"] or 0) + (s["completion_tokens"] or 0) for s in spans_list)

        # Derive decision outcome + reason from the spans
        decision_outcome = "executed" if trade_row else "hold"
        decision_reason = ""

        # Check agent spans for more specific outcomes
        risk_span = next((s for s in spans_list if s["agent_name"] == "risk_manager"), None)
        strategist_span = next((s for s in spans_list if s["agent_name"] == "strategist"), None)

        if trade_row:
            decision_outcome = "executed"
            decision_reason = "Trade passed all checks and was executed."
        elif risk_span:
            rj = risk_span.get("reasoning_json") or {}
            if not rj.get("approved", True):
                decision_outcome = "rejected"
                decision_reason = rj.get("reason", "Rejected by risk manager.")
            elif rj.get("needs_approval"):
                decision_outcome = "pending_approval"
                decision_reason = "Trade queued for Telegram approval."
            else:
                decision_outcome = "execution_failed"
                decision_reason = "Risk manager approved but trade was not recorded."
        elif strategist_span:
            rj = strategist_span.get("reasoning_json") or {}
            if rj.get("action") == "hold":
                decision_outcome = "hold"
                decision_reason = rj.get("reasoning") or rj.get("reason") or "Strategist recommended hold."
        else:
            decision_outcome = "hold"
            decision_reason = "No strategy generated."

        return {
            "cycle_id": cycle_id,
            "pair": first["pair"],
            "started_at": first["ts"],
            "finished_at": last["ts"],
            "total_latency_ms": round(total_latency, 1),
            "total_tokens": total_tokens,
            "langfuse_trace_id": first.get("langfuse_trace_id"),
            "spans": spans_list,
            "trade": dict(trade_row) if trade_row else None,
            "decision_outcome": decision_outcome,
            "decision_reason": decision_reason,
        }

    def get_reasoning_for_review(self, days: int = 7, pair: Optional[str] = None) -> list[dict]:
        """Fetch reasoning+outcome rows for use in planning workflow LLM review."""
        with self._get_conn() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            if pair:
                rows = conn.execute(
                    """SELECT ar.ts, ar.pair, ar.agent_name, ar.reasoning_json,
                              ar.signal_type, ar.confidence,
                              t.action, t.pnl, t.price
                       FROM agent_reasoning ar
                       LEFT JOIN trades t ON t.id = ar.trade_id
                       WHERE ar.ts >= %s AND ar.pair = %s
                       ORDER BY ar.ts DESC LIMIT 200""",
                    (cutoff, pair),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT ar.ts, ar.pair, ar.agent_name, ar.reasoning_json,
                              ar.signal_type, ar.confidence,
                              t.action, t.pnl, t.price
                       FROM agent_reasoning ar
                       LEFT JOIN trades t ON t.id = ar.trade_id
                       WHERE ar.ts >= %s
                       ORDER BY ar.ts DESC LIMIT 200""",
                    (cutoff,),
                ).fetchall()
            return [dict(r) for r in rows]

    # --- Strategic Context --------------------------------------------------

    def save_strategic_context(
        self,
        horizon: str,
        plan_json: dict,
        summary_text: str = "",
        langfuse_trace_id: Optional[str] = None,
        temporal_workflow_id: Optional[str] = None,
        temporal_run_id: Optional[str] = None,
    ) -> int:
        """Persist a planning workflow output (daily / weekly / monthly)."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO strategic_context
                   (horizon, plan_json, summary_text,
                    langfuse_trace_id, temporal_workflow_id, temporal_run_id)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    horizon, json.dumps(plan_json, default=str), summary_text,
                    langfuse_trace_id, temporal_workflow_id, temporal_run_id,
                ),
            )
            row = cursor.fetchone()
            conn.commit()
            return row["id"]

    def get_latest_strategic_context(self, horizon: Optional[str] = None) -> list[dict]:
        """Get the most recent strategic context, optionally filtered by horizon."""
        with self._get_conn() as conn:
            if horizon:
                rows = conn.execute(
                    """SELECT * FROM strategic_context WHERE horizon = %s
                       ORDER BY ts DESC LIMIT 1""",
                    (horizon,),
                ).fetchall()
            else:
                # Latest one per horizon
                rows = conn.execute(
                    """SELECT sc.* FROM strategic_context sc
                       INNER JOIN (
                           SELECT horizon, MAX(ts) as max_ts
                           FROM strategic_context GROUP BY horizon
                       ) latest ON sc.horizon = latest.horizon AND sc.ts = latest.max_ts
                       ORDER BY sc.horizon""",
                ).fetchall()
            return [dict(r) for r in rows]

    def write_daily_plan(self, date: str, plan_text: str) -> None:
        """Write the daily plan text into the daily_summaries table."""
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO daily_summaries (date, plan_text)
                   VALUES (%s, %s)
                   ON CONFLICT(date) DO UPDATE SET plan_text = EXCLUDED.plan_text""",
                (date, plan_text),
            )
            conn.commit()
