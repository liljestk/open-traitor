/**
 * API client helpers for the Auto-Traitor dashboard.
 * All requests are relative so Vite's dev proxy (port 5173 → 8090)
 * works seamlessly; in Docker the frontend is served from the same port.
 */

import { useLiveStore } from './store'

const BASE = '/api'

// ─── Generic fetch wrapper ─────────────────────────────────────────────────

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const profile = useLiveStore.getState().profile
  const sep = path.includes('?') ? '&' : '?'
  const finalPath = profile ? `${path}${sep}profile=${encodeURIComponent(profile)}` : path

  const res = await fetch(`${BASE}${finalPath}`, options)
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
  quote_amount: number | null
  price: number | null
  langfuse_trace_id: string | null
  langfuse_url: string | null
  total_prompt_tokens: number | null
  total_completion_tokens: number | null
  total_latency_ms: number | null
  cycle_duration_ms: number | null
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
  quote_amount: number
  pnl: number | null
  confidence: number | null
}

export interface TradeFull extends Trade {
  quantity: number
  fee_quote: number
  signal_type: string | null
  stop_loss: number | null
  take_profit: number | null
  reasoning: string | null
  is_rotation: number
  approved_by: string
}

export interface EventLog {
  id: number
  ts: string
  event_type: string
  severity: string
  pair: string | null
  message: string
  data: Record<string, unknown> | null
}

export interface CycleFull {
  cycle_id: string
  pair: string
  started_at: string
  finished_at: string
  total_latency_ms: number
  total_tokens: number
  langfuse_trace_id: string | null
  langfuse_url: string | null
  spans: AgentSpan[]
  trade: Trade | null
  decision_outcome: 'executed' | 'hold' | 'rejected' | 'pending_approval' | 'execution_failed'
  decision_reason: string
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
  portfolio?: { portfolio_value: number; total_pnl: number; ts: string }
}

export interface StrategicPlan {
  id: number
  horizon: string
  plan_json: Record<string, unknown>
  summary_text: string
  ts: string
  langfuse_trace_id: string | null
  langfuse_url: string | null
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

export interface SimulatedTrade {
  id: number
  ts: string
  pair: string
  from_currency: string
  to_currency: string
  from_amount: number
  entry_price: number
  current_price: number
  quantity: number
  pnl_abs: number
  pnl_pct: number
  status: 'open' | 'closed'
  closed_at: string | null
  close_price: number | null
  close_pnl_abs: number | null
  close_pnl_pct: number | null
  notes: string
}

// ─── API calls ─────────────────────────────────────────────────────────────

export const fetchCycles = (pair?: string, limit = 50, offset = 0) =>
  apiFetch<{ cycles: CycleSummary[]; count: number }>(
    `/cycles?limit=${limit}&offset=${offset}${pair ? `&pair=${pair}` : ''}`
  )

export const fetchTrades = (pair?: string, limit = 500, hours = 168) =>
  apiFetch<{ trades: TradeFull[]; count: number }>(
    `/trades?limit=${limit}&hours=${hours}${pair ? `&pair=${pair}` : ''}`
  )

export const exportTradesUrl = (hours = 720) => `${BASE}/trades/export?hours=${hours}`

export const fetchEvents = (eventType?: string, limit = 500, hours = 168) =>
  apiFetch<{ events: EventLog[]; count: number }>(
    `/events?limit=${limit}&hours=${hours}${eventType ? `&event_type=${eventType}` : ''}`
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

export const fetchMarketPrice = (pair: string) =>
  apiFetch<{ pair: string; price: number; ts: string }>(`/market/price?pair=${encodeURIComponent(pair)}`)

export interface CoinbaseProduct { id: string; base: string; quote: string }

export const fetchProducts = () =>
  apiFetch<{ products: CoinbaseProduct[] }>('/products')

export const createSimulatedTrade = (data: { pair: string; from_currency: string; from_amount: number; notes?: string }) =>
  apiFetch<SimulatedTrade>('/simulated-trades', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })

export const fetchSimulatedTrades = (includeClosed = false) =>
  apiFetch<{ simulations: SimulatedTrade[]; count: number }>(`/simulated-trades?include_closed=${includeClosed}`)

export const closeSimulatedTrade = (simId: number) =>
  apiFetch<SimulatedTrade>(`/simulated-trades/${simId}`, { method: 'DELETE' })

// ─── Settings ──────────────────────────────────────────────────────────────

export interface FieldSchema {
  type: string
  min?: number
  max?: number
  enum?: string[]
}

export interface SectionSchema {
  label: string
  telegram_tier: 'safe' | 'semi_safe' | 'blocked'
  fields?: Record<string, FieldSchema>
  nested?: Record<string, { fields: Record<string, FieldSchema> }>
}

export interface SettingsResponse {
  settings: Record<string, any>
  trading_enabled: boolean
  sections: string[]
  section_labels: Record<string, string>
  telegram_tiers: Record<string, { sections: string[]; description: string }>
  schema: Record<string, SectionSchema>
}

export interface SettingsUpdateResult {
  ok: boolean
  preset?: string
  section?: string
  changes: Record<string, any>
  trading_enabled: boolean
}

export interface PresetInfo {
  values: Record<string, Record<string, any>>
  summary: string
}

export const fetchSettings = () =>
  apiFetch<SettingsResponse>('/settings')

export const updateSettings = (data: { section?: string; updates?: Record<string, any>; preset?: string }) =>
  apiFetch<SettingsUpdateResult>('/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })

export const fetchPresets = () =>
  apiFetch<{ presets: Record<string, PresetInfo>; current_enabled: boolean }>('/settings/presets')

// ─── LLM Providers ────────────────────────────────────────────────────────

export interface LLMProviderLiveStatus {
  name: string
  model: string
  is_local: boolean
  available: boolean
  in_cooldown?: boolean
  cooldown_remaining_s?: number
  daily_tokens?: number
  daily_token_limit?: number
  rpm_limit?: number
  rpm_current?: number
}

export interface LLMProviderConfig {
  name: string
  enabled: boolean
  model: string
  base_url?: string
  base_url_env?: string
  api_key_env?: string
  model_env?: string
  timeout?: number
  rpm_limit?: number
  daily_token_limit?: number
  cooldown_seconds?: number
  is_local?: boolean
  api_key_set?: boolean
  live_status?: LLMProviderLiveStatus
}

export const fetchLLMProviders = () =>
  apiFetch<{ providers: LLMProviderConfig[] }>('/settings/llm-providers')

export const updateLLMProviders = (providers: LLMProviderConfig[]) =>
  apiFetch<{ ok: boolean; providers: LLMProviderConfig[] }>('/settings/llm-providers', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ providers }),
  })

export const updateApiKeys = (keys: Record<string, string>) =>
  apiFetch<{ ok: boolean; updated: string[] }>('/settings/api-keys', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ keys }),
  })

// ─── Portfolio & Analytics ─────────────────────────────────────────────────

export interface PortfolioSnapshot {
  ts: string
  portfolio_value: number
  return_pct: number
  total_pnl: number
}

export interface PortfolioRange {
  low: number
  high: number
  avg: number
  samples: number
}

export interface TradeStatsDetail {
  total_trades: number
  winning: number
  losing: number
  pending: number
  total_pnl: number
  best_pnl: number
  worst_pnl: number
  avg_pnl: number
  total_volume: number
  total_fees: number
  avg_confidence: number
}

export interface WinLossStats {
  win_rate: number
  avg_win: number
  avg_loss: number
  sample_size: number
}

export interface DailySummary {
  date: string
  ts: string
  opening_value: number
  closing_value: number
  high_value: number
  low_value: number
  total_trades: number
  winning_trades: number
  losing_trades: number
  total_pnl: number
  best_trade: number
  worst_trade: number
  events_count: number
  summary_text: string | null
  plan_text: string | null
}

export interface BestWorstTrade {
  pair: string
  action: string
  pnl: number
  price: number
  quote_amount: number
  ts: string
}

export interface AnalyticsData {
  performance: {
    trade_stats: TradeStatsDetail
    portfolio_range: PortfolioRange
    event_counts: Record<string, number>
    recent_trades: TradeFull[]
  }
  best_worst: {
    best: BestWorstTrade[]
    worst: BestWorstTrade[]
  }
  daily_summaries: DailySummary[]
  win_loss: WinLossStats
  portfolio_range: PortfolioRange
}

export const fetchPortfolioHistory = (hours = 720) =>
  apiFetch<{ history: PortfolioSnapshot[]; count: number }>(`/portfolio/history?hours=${hours}`)

export const fetchAnalytics = (hours = 720) =>
  apiFetch<AnalyticsData>(`/analytics?hours=${hours}`)

// ─── Portfolio Exposure ────────────────────────────────────────────────────

export interface ExposureBreakdown {
  pair: string
  quantity: number
  entry_price: number
  current_price: number
  value: number
  pct_of_portfolio: number
  pnl_pct: number
}

export interface PortfolioExposure {
  portfolio_value: number
  cash_balance: number
  return_pct: number
  total_pnl: number
  max_drawdown: number
  fear_greed_value: number | null
  high_stakes_active: boolean | null
  breakdown: ExposureBreakdown[]
  cash_pct: number
  allocated_pct: number
  ts: string
}

export const fetchPortfolioExposure = () =>
  apiFetch<{ exposure: PortfolioExposure | null }>('/portfolio/exposure')

// ─── News ──────────────────────────────────────────────────────────────────

export interface NewsArticle {
  id: string
  title: string
  summary: string
  source: string
  url: string
  published: string
  sentiment: 'bullish' | 'bearish' | 'neutral'
  relevance_score: number
  tags: string[]
}

export const fetchNews = (count = 30) =>
  apiFetch<{ articles: NewsArticle[]; count: number; source: string }>(`/news?count=${count}`)

// ─── Watchlist ─────────────────────────────────────────────────────────────

export interface ScanResult {
  ts: string
  universe_size: number
  scanned_pairs: number
  results_json: Record<string, unknown>
  top_movers: Array<{ pair: string; change_pct: number; volume: number }>
  summary_text: string
}

export interface WatchlistData {
  active_pairs: string[]
  live_prices: Record<string, number>
  scan: ScanResult | null
  pair_count: number
}

export const fetchWatchlist = () =>
  apiFetch<WatchlistData>('/watchlist')

// ─── Candles / Charts ──────────────────────────────────────────────────────

export interface CandleData {
  start: string
  low: number
  high: number
  open: number
  close: number
  volume: number
}

export const fetchCandles = (pair: string, granularity = 'ONE_HOUR', limit = 200) =>
  apiFetch<{ candles: CandleData[]; pair: string }>(`/candles?pair=${encodeURIComponent(pair)}&granularity=${granularity}&limit=${limit}`)

// ─── HITL (Human-in-the-Loop) Commands ─────────────────────────────────────

export interface TradeCommand {
  action: string
  pair: string
  ts: string
  source: string
}

export const sendTradeCommand = (pair: string, action: 'liquidate' | 'tighten_stop' | 'pause') =>
  apiFetch<{ status: string; action: string; pair: string }>(`/trade/${encodeURIComponent(pair)}/command?action=${action}`, { method: 'POST' })

export const fetchCommandHistory = (limit = 20) =>
  apiFetch<{ commands: TradeCommand[] }>(`/trade/commands/history?limit=${limit}`)

// ─── Trailing Stops ────────────────────────────────────────────────────────

export interface TrailingStopData {
  pair: string
  entry_price: number
  trail_pct: number
  stop_price: number
  triggered: boolean
  highest_price: number
  total_quantity: number
  remaining_quantity: number
  tiers: Array<{
    trigger_pct: number
    exit_fraction: number
    triggered: boolean
    trigger_price: number | null
  }>
}

export const fetchTrailingStops = () =>
  apiFetch<{ stops: Record<string, TrailingStopData>; source: string }>('/trailing-stops')

// ─── WebSocket ─────────────────────────────────────────────────────────────

export function openLiveSocket(onMessage: (event: LiveEvent) => void, onClose?: () => void): WebSocket {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const host = window.location.hostname
  const port = window.location.port || (proto === 'wss' ? '443' : '80')
  const profile = useLiveStore.getState().profile
  const qs = profile ? `?profile=${encodeURIComponent(profile)}` : ''
  const ws = new WebSocket(`${proto}://${host}:${port}/ws/live${qs}`)
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
