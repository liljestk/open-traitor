/**
 * API client helpers for the OpenTraitor dashboard.
 * All requests are relative so Vite's dev proxy (port 5173 → 8090)
 * works seamlessly; in Docker the frontend is served from the same port.
 */

import { useLiveStore } from './store'

const BASE = '/api'

// ─── Auth helpers ───────────────────────────────────────────────────────

// In-memory CSRF token (set after login, cleared on logout)
let _csrfToken = ''

// Legacy API key support (backward compat — will be removed)
const API_KEY_STORAGE_KEY = 'auto_traitor_api_key'

export function getApiKey(): string {
  return localStorage.getItem(API_KEY_STORAGE_KEY) || ''
}

export function setApiKey(key: string): void {
  localStorage.setItem(API_KEY_STORAGE_KEY, key)
}

export function setCsrfToken(token: string): void {
  _csrfToken = token
}

export function getCsrfToken(): string {
  return _csrfToken
}

// ─── Generic fetch wrapper ─────────────────────────────────────────────────

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const profile = useLiveStore.getState().profile
  const sep = path.includes('?') ? '&' : '?'
  const finalPath = profile ? `${path}${sep}profile=${encodeURIComponent(profile)}` : path

  const headers = new Headers(options?.headers)

  // Legacy API key support (will be removed)
  const apiKey = getApiKey()
  if (apiKey) {
    headers.set('X-API-Key', apiKey)
  }

  // CSRF token for mutating requests
  const method = (options?.method || 'GET').toUpperCase()
  const isMutating = ['POST', 'PUT', 'DELETE', 'PATCH'].includes(method)
  if (_csrfToken && isMutating) {
    headers.set('X-CSRF-Token', _csrfToken)
  }

  const res = await fetch(`${BASE}${finalPath}`, {
    ...options,
    headers,
    credentials: 'include',  // Send httpOnly cookies
  })

  // Auto-refresh CSRF token on 403 and retry once
  if (res.status === 403 && isMutating) {
    try {
      const statusRes = await fetch(`${BASE}/auth/status`, { credentials: 'include' })
      const status = await statusRes.json()
      if (status.csrf_token) {
        setCsrfToken(status.csrf_token)
        const retryHeaders = new Headers(options?.headers)
        if (apiKey) retryHeaders.set('X-API-Key', apiKey)
        retryHeaders.set('X-CSRF-Token', status.csrf_token)
        const retry = await fetch(`${BASE}${finalPath}`, {
          ...options,
          headers: retryHeaders,
          credentials: 'include',
        })
        if (!retry.ok) {
          const text = await retry.text()
          throw new Error(`HTTP ${retry.status}: ${text}`)
        }
        return retry.json() as Promise<T>
      }
    } catch {
      // If refresh fails, fall through to original error
    }
    const text = await res.text()
    throw new Error(`HTTP ${res.status}: ${text}`)
  }

  if (!res.ok) {
    const text = await res.text()
    throw new Error(`HTTP ${res.status}: ${text}`)
  }
  return res.json() as Promise<T>
}

// ─── Types ─────────────────────────────────────────────────────────────────

export interface SystemStatus {
  setup_complete: boolean
  auth_configured: boolean
  authenticated: boolean
}

export async function fetchSystemStatus(): Promise<SystemStatus> {
  const res = await fetch('/api/system/status', { credentials: 'include' })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

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
  cycle_id?: string | null
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

export const fetchSetupConfig = () =>
  apiFetch<Record<string, any>>('/setup')

export const fetchCycles = (pair?: string, limit = 50, offset = 0) =>
  apiFetch<{ cycles: CycleSummary[]; count: number }>(
    `/cycles?limit=${limit}&offset=${offset}${pair ? `&pair=${pair}` : ''}`
  )

export const fetchTrades = (pair?: string, limit = 500, hours = 168) =>
  apiFetch<{ trades: TradeFull[]; count: number }>(
    `/trades?limit=${limit}&hours=${hours}${pair ? `&pair=${pair}` : ''}`
  )

export const exportTradesUrl = (hours = 720) => `${BASE}/trades/export?hours=${hours}`

export const syncTrades = () =>
  apiFetch<{ synced: number; total_exchange: number; error?: string }>('/trades/sync', { method: 'POST' })

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
  rpm_budget?: RpmBudget
}

export interface SettingsUpdateResult {
  ok: boolean
  preset?: string
  section?: string
  changes: Record<string, any>
  trading_enabled: boolean
  confirmation_required?: boolean
  confirmation_token?: string
}

export interface PresetInfo {
  values: Record<string, Record<string, any>>
  summary: string
}

export const fetchSettings = () =>
  apiFetch<SettingsResponse>('/settings')

export async function updateSettings(data: { section?: string; updates?: Record<string, any>; preset?: string }): Promise<SettingsUpdateResult> {
  const result = await apiFetch<SettingsUpdateResult>('/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  // Auto-confirm: if backend requires confirmation, re-send with the token
  if (result.confirmation_required && result.confirmation_token) {
    return apiFetch<SettingsUpdateResult>('/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...data, confirmation_token: result.confirmation_token }),
    })
  }
  return result
}

export const fetchPresets = () =>
  apiFetch<{ presets: Record<string, PresetInfo>; current_enabled: boolean }>('/settings/presets')

// ─── Style Modifiers ─────────────────────────────────────────────────────

export interface StyleModifierMeta {
  label: string
  desc: string
  exchanges: string[]
  icon: string
}

export interface StyleModifiersResponse {
  modifiers: Record<string, StyleModifierMeta>
  active: string[]
  asset_class: string
}

export const fetchStyleModifiers = () =>
  apiFetch<StyleModifiersResponse>('/settings/style-modifiers')

// ─── LLM Providers ────────────────────────────────────────────────────────

export interface LLMProviderLiveStatus {
  name: string
  model: string
  is_local: boolean
  available: boolean
  tier?: string
  in_cooldown?: boolean
  cooldown_remaining_s?: number
  daily_tokens?: number
  daily_token_limit?: number
  daily_requests?: number
  daily_request_limit?: number
  rpm_limit?: number
  rpm_current?: number
  /** OpenRouter-specific: remaining credit balance */
  credits_remaining?: number | null
  /** OpenRouter-specific: whether current model is a free model */
  is_free_model?: boolean
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
  daily_request_limit?: number
  cooldown_seconds?: number
  is_local?: boolean
  tier?: string
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

export const updateApiKeys = async (keys: Record<string, string>): Promise<{ ok: boolean; updated: string[] }> => {
  // Step 1: request confirmation token
  const step1 = await apiFetch<{
    ok: boolean; confirmation_required?: boolean;
    confirmation_token?: string; updated?: string[];
  }>('/settings/api-keys', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ keys }),
  })
  if (step1.ok) return step1 as { ok: boolean; updated: string[] }
  if (!step1.confirmation_required || !step1.confirmation_token)
    throw new Error('Unexpected response from API key update')
  // Step 2: confirm with token
  return apiFetch<{ ok: boolean; updated: string[] }>('/settings/api-keys', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ keys, confirmation_token: step1.confirmation_token }),
  })
}

export interface OpenRouterCreditsInfo {
  ok: boolean
  error?: string
  credits_remaining?: number | null
  usage?: number
  is_free_tier?: boolean
  label?: string
}

export const fetchOpenRouterCredits = () =>
  apiFetch<OpenRouterCreditsInfo>('/settings/openrouter-credits')

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

// ─── Prediction Accuracy ───────────────────────────────────────────────────

export interface PredictionOutcome {
  actual_price: number
  pct_change: number
  correct: boolean
}

export interface PredictionRecord {
  ts: string
  pair: string
  signal_type: string
  confidence: number
  entry_price: number
  suggested_tp: number | null
  suggested_sl: number | null
  outcomes: Record<string, PredictionOutcome | null>
}

export interface PairAccuracy {
  total: number
  correct_24h: number
  correct_1h: number
  evaluated_24h: number
  evaluated_1h: number
  accuracy_24h_pct: number | null
  accuracy_1h_pct: number | null
}

export interface ConfidenceBucket {
  confidence_range: string
  total: number
  correct: number
  evaluated: number
  accuracy_pct: number | null
}

export interface DailyAccuracy {
  date: string
  total: number
  correct: number
  evaluated: number
  accuracy_pct: number | null
}

export interface PredictionAccuracyData {
  predictions: PredictionRecord[]
  per_pair: Record<string, PairAccuracy>
  overall: {
    total: number
    correct_24h: number
    evaluated_24h: number
    correct_1h: number
    evaluated_1h: number
    accuracy_24h_pct: number | null
    accuracy_1h_pct: number | null
  }
  by_signal_type: Record<string, { total: number; correct_24h: number; evaluated_24h: number; accuracy_pct: number | null; weight?: number }>
  confidence_calibration: ConfidenceBucket[]
  daily_accuracy: DailyAccuracy[]
}

export const fetchPredictionAccuracy = (days = 30) =>
  apiFetch<PredictionAccuracyData>(`/predictions/accuracy?days=${days}`)

// ── Tracked Pairs ─────────────────────────────────────────────────────

export interface TrackedPair {
  pair: string
  prediction_count: number
  last_predicted: string
  signal_types: string[]
  source: 'ai' | 'human' | 'both'
}

export interface TrackedPairsData {
  crypto: TrackedPair[]
  equity: TrackedPair[]
  total_pairs: number
}

export const fetchTrackedPairs = () =>
  apiFetch<TrackedPairsData>(`/predictions/tracked-pairs`)

// ── Per-Pair Prediction History (overlay chart) ───────────────────────────

export interface PricePoint {
  ts: string
  price: number
}

export interface PredictionMarker {
  ts: string
  signal_type: string
  confidence: number
  entry_price: number
  suggested_tp: number | null
  suggested_sl: number | null
  is_bullish: boolean
  outcomes: Record<string, PredictionOutcome | null>
}

export interface PairPredictionHistory {
  pair: string
  price_history: PricePoint[]
  predictions: PredictionMarker[]
  total_predictions: number
}

export const fetchPairPredictionHistory = (pair: string, days = 30) =>
  apiFetch<PairPredictionHistory>(`/predictions/pair-history?pair=${encodeURIComponent(pair)}&days=${days}`)

export const cleanupPortfolioSnapshots = () =>
  apiFetch<{ deleted: number; status: string }>(`/portfolio/cleanup`, { method: 'POST' })

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

// ─── LLM Analytics ─────────────────────────────────────────────────────────

export interface LLMAnalyticsSummary {
  total_calls: number
  total_prompt_tokens: number
  total_completion_tokens: number
  total_tokens: number
  avg_latency_ms: number | null
  avg_prompt_tokens: number | null
  avg_completion_tokens: number | null
  avg_total_tokens: number | null
  max_latency_ms: number | null
  min_latency_ms: number | null
  p50_latency_ms: number | null
  p90_latency_ms: number | null
  p99_latency_ms: number | null
  unique_pairs: number
  total_cycles: number
  runtime_total_calls: number | null
  runtime_total_tokens: number | null
}

export interface LLMTimeBucket {
  bucket: string
  calls: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  avg_latency_ms: number | null
}

export interface LLMAgentStat {
  agent_name: string
  calls: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  avg_latency_ms: number | null
  avg_prompt_tokens: number | null
  avg_completion_tokens: number | null
}

export interface LLMExchangeStat {
  exchange: string
  calls: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}

export interface LLMPairStat {
  pair: string
  calls: number
  total_tokens: number
  avg_latency_ms: number | null
}

export interface LLMProviderRuntimeStat {
  name: string
  enabled: boolean
  model: string | null
  daily_tokens_used: number
  daily_tokens_budget: number | null
  daily_requests_used: number
  daily_requests_budget: number | null
  rpm_limit: number | null
  in_cooldown: boolean
  credits_remaining: number | null
}

export interface LLMAnalyticsData {
  summary: LLMAnalyticsSummary
  time_series: LLMTimeBucket[]
  by_agent: LLMAgentStat[]
  by_exchange: LLMExchangeStat[]
  top_pairs: LLMPairStat[]
  providers: LLMProviderRuntimeStat[]
  hours: number
  bucket: 'hourly' | 'daily' | 'weekly'
}

export const fetchLLMAnalytics = (hours = 168) =>
  apiFetch<LLMAnalyticsData>(`/llm-analytics?hours=${hours}`)

// ─── LLM Optimizer ─────────────────────────────────────────────────────────

export interface OptimizerParamMeta {
  label: string
  description: string
  type: 'int' | 'multiselect'
  min?: number
  max?: number
  step?: number
  options?: string[]
  impact_category: string
  token_weight?: number
}

export interface OptimizerAgentContext {
  agent_name: string
  calls: number
  avg_prompt_tokens: number
  avg_completion_tokens: number
  total_prompt_tokens: number
}

export interface OptimizerSignalDist {
  signal_type: string
  count: number
  avg_confidence: number
}

export interface OptimizerHistoryEntry {
  ts: string
  changed_by: string
  changes: Record<string, { from: unknown; to: unknown }>
  snapshot: Record<string, unknown>
}

export interface OptimizerData {
  settings: Record<string, unknown>
  defaults: Record<string, unknown>
  param_meta: Record<string, OptimizerParamMeta>
  history: OptimizerHistoryEntry[]
  context: {
    by_agent: OptimizerAgentContext[]
    signal_distribution: OptimizerSignalDist[]
    total_strategist_calls: number
    totals: { total_prompt_tokens: number; total_completion_tokens: number; total_calls: number }
  }
  hours: number
}

export const fetchOptimizer = (hours = 168) =>
  apiFetch<OptimizerData>(`/llm-analytics/optimizer?hours=${hours}`)

export const applyOptimizer = (settings: Record<string, unknown>) =>
  apiFetch<{ ok: boolean; applied: Record<string, unknown>; changes: Record<string, { from: unknown; to: unknown }> }>(
    '/llm-analytics/optimizer/apply',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings }),
    }
  )

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

export const fetchNews = (count = 30, profile = '') =>
  apiFetch<{ articles: NewsArticle[]; count: number; source: string }>(
    `/news?count=${count}${profile ? `&profile=${encodeURIComponent(profile)}` : ''}`
  )

// ─── Watchlist ─────────────────────────────────────────────────────────────

export interface ScanResult {
  ts: string
  universe_size: number
  scanned_pairs: number
  results_json: Record<string, unknown>
  top_movers: Array<{ pair: string; change_pct: number; volume: number }>
  summary_text: string
}

export interface PairInfo {
  pair: string
  followed_by_llm: boolean
  followed_by_human: boolean
  price: number | null
}

export interface RpmBudget {
  provider: string
  model?: string
  tier?: string
  rpm: number
  interval: number
  available_per_cycle: number
  overhead: number
  entity_budget: number
  calls_per_entity?: number
  max_entities: number
  configured_max: number
  effective_max: number
  note?: string
}

export interface WatchlistData {
  active_pairs: string[]
  human_followed_pairs: string[]
  pair_info: PairInfo[]
  live_prices: Record<string, number>
  scan: ScanResult | null
  pair_count: number
  rpm_budget: RpmBudget | null
}

export const fetchWatchlist = () =>
  apiFetch<WatchlistData>('/watchlist')

export const followPair = (pair: string, exchange = '') =>
  apiFetch<{ ok: boolean; pair: string; followed_by: string; exchange: string }>('/watchlist/follow', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pair, exchange }),
  })

export const unfollowPair = (pair: string) =>
  apiFetch<{ ok: boolean; pair: string; unfollowed: boolean }>(`/watchlist/follow/${encodeURIComponent(pair)}`, {
    method: 'DELETE',
  })

// ─── Product Search (pair lookup) ──────────────────────────────────────────

export interface ProductResult {
  id: string
  base: string
  quote: string
  display_name: string
  volume_24h: number
  price_change_24h: number
}

export const searchProducts = (q: string) =>
  apiFetch<{ results: ProductResult[]; query: string }>(`/products/search?q=${encodeURIComponent(q)}`)

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

// ─── Authentication ────────────────────────────────────────────────────────

export interface AuthStatus {
  authenticated: boolean
  auth_configured: boolean
  has_password: boolean
  session_ttl: number
}

export interface LoginResult {
  status: string
  csrf_token?: string
  error?: string
}

export const fetchAuthStatus = async (): Promise<AuthStatus> => {
  const res = await fetch(`${BASE}/auth/status`, { credentials: 'include' })
  return res.json()
}

export const login = async (password: string): Promise<LoginResult> => {
  const res = await fetch(`${BASE}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
    credentials: 'include',
  })
  const data = await res.json()
  if (data.status === 'ok' && data.csrf_token) {
    setCsrfToken(data.csrf_token)
  }
  return data
}

export const logout = async (): Promise<void> => {
  await fetch(`${BASE}/auth/logout`, {
    method: 'POST',
    credentials: 'include',
    headers: _csrfToken ? { 'X-CSRF-Token': _csrfToken } : {},
  })
  setCsrfToken('')
}

export const setPassword = async (password: string): Promise<{ ok: boolean; error?: string }> => {
  const res = await fetch(`${BASE}/auth/set-password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
    credentials: 'include',
  })
  return res.json()
}

// ─── WebSocket ─────────────────────────────────────────────────────────────

export function openLiveSocket(onMessage: (event: LiveEvent) => void, onClose?: () => void): WebSocket {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const host = window.location.hostname
  const port = window.location.port || (proto === 'wss' ? '443' : '80')
  const profile = useLiveStore.getState().profile
  const qs = profile ? `?profile=${encodeURIComponent(profile)}` : ''
  // Send API key via Sec-WebSocket-Protocol (browsers can't set custom WS headers)
  const apiKey = getApiKey()
  const protocols = apiKey ? [`apikey.${btoa(apiKey)}`] : undefined
  const ws = new WebSocket(`${proto}://${host}:${port}/ws/live${qs}`, protocols)
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
