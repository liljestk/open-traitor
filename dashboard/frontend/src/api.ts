/**
 * API client helpers for the Auto-Traitor dashboard.
 * All requests are relative so Vite's dev proxy (port 5173 → 8090)
 * works seamlessly; in Docker the frontend is served from the same port.
 */

const BASE = '/api'

// ─── Generic fetch wrapper ─────────────────────────────────────────────────

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, options)
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`HTTP ${res.status}: ${text}`)
  }
  return res.json() as Promise<T>
}

// ─── Types ─────────────────────────────────────────────────────────────────

export interface CycleSummary {
  cycle_id: string
  pair: string
  started_at: string
  finished_at: string
  agent_count: number
  signal_type: string | null
  confidence: number | null
  action: string | null
  trade_id: number | null
  pnl: number | null
  usd_amount: number | null
  price: number | null
  langfuse_trace_id: string | null
  total_prompt_tokens: number | null
  total_completion_tokens: number | null
  total_latency_ms: number | null
}

export interface AgentSpan {
  id: number
  ts: string
  agent_name: string
  reasoning_json: Record<string, unknown>
  signal_type: string | null
  confidence: number | null
  langfuse_trace_id: string | null
  langfuse_span_id: string | null
  prompt_tokens: number | null
  completion_tokens: number | null
  latency_ms: number | null
  raw_prompt: string | null
  pair: string
}

export interface Trade {
  id: number
  ts: string
  pair: string
  action: string
  price: number
  usd_amount: number
  pnl: number | null
  confidence: number | null
}

export interface CycleFull {
  cycle_id: string
  pair: string
  started_at: string
  finished_at: string
  total_latency_ms: number
  total_tokens: number
  langfuse_trace_id: string | null
  spans: AgentSpan[]
  trade: Trade | null
}

export interface StatsSummary {
  total_trades: number
  wins: number
  losses: number
  total_pnl: number | null
  avg_pnl: number | null
  best_trade: number | null
  worst_trade: number | null
  trades_24h: number
  pnl_24h: number | null
  active_pairs: number
  cycles_24h: number
  win_rate: number | null
  portfolio?: { total_value_usd: number; total_pnl_usd: number; ts: string }
}

export interface StrategicPlan {
  id: number
  horizon: string
  plan_json: Record<string, unknown>
  summary_text: string
  ts: string
  langfuse_trace_id: string | null
  temporal_workflow_id: string | null
  temporal_run_id: string | null
}

export interface TemporalRun {
  workflow_id: string
  run_id: string
  workflow_type: string
  status: string
  start_time: string | null
  close_time: string | null
}

export interface TemporalReplay {
  workflow_id: string
  run_id: string
  event_count: number
  langfuse_trace_id: string | null
  langfuse_url: string | null
  events: Array<{
    event_id: number
    event_type: string
    event_time: string | null
    attributes: Record<string, string>
  }>
}

export interface LiveEvent {
  type: string
  cycle_id?: string
  pair?: string
  agent_name?: string
  model?: string
  latency_ms?: number
  prompt_tokens?: number
  completion_tokens?: number
  langfuse_trace_id?: string
  ts?: string
}

// ─── API calls ─────────────────────────────────────────────────────────────

export const fetchCycles = (pair?: string, limit = 50, offset = 0) =>
  apiFetch<{ cycles: CycleSummary[]; count: number }>(
    `/cycles?limit=${limit}&offset=${offset}${pair ? `&pair=${pair}` : ''}`
  )

export const fetchCycleFull = (cycleId: string) =>
  apiFetch<CycleFull>(`/cycles/${encodeURIComponent(cycleId)}`)

export const fetchStatsSummary = () =>
  apiFetch<StatsSummary>('/stats/summary')

export const fetchStrategic = (horizon?: string, limit = 20) =>
  apiFetch<{ plans: StrategicPlan[]; count: number }>(
    `/strategic?limit=${limit}${horizon ? `&horizon=${horizon}` : ''}`
  )

export const fetchTemporalRuns = (workflowType?: string, limit = 50) =>
  apiFetch<{ runs: TemporalRun[]; count: number }>(
    `/temporal/runs?limit=${limit}${workflowType ? `&workflow_type=${workflowType}` : ''}`
  )

export const fetchTemporalReplay = (workflowId: string, runId: string) =>
  apiFetch<TemporalReplay>(`/temporal/replay/${encodeURIComponent(workflowId)}/${encodeURIComponent(runId)}`)

export const triggerTemporalRerun = (workflowId: string, runId: string) =>
  apiFetch(`/temporal/rerun/${encodeURIComponent(workflowId)}/${encodeURIComponent(runId)}`, { method: 'POST' })

// ─── WebSocket ─────────────────────────────────────────────────────────────

export function openLiveSocket(onMessage: (event: LiveEvent) => void, onClose?: () => void): WebSocket {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const host = window.location.hostname
  const port = window.location.port || (proto === 'wss' ? '443' : '80')
  const ws = new WebSocket(`${proto}://${host}:${port}/ws/live`)
  ws.onmessage = (e) => {
    try {
      onMessage(JSON.parse(e.data))
    } catch {
      // silently ignore unparseable messages
    }
  }
  ws.onclose = () => onClose?.()
  return ws
}
