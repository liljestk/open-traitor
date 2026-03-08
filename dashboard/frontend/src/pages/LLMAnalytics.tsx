/**
 * LLM Analytics — token usage, call volume, latency, provider status,
 * and per-agent breakdowns so you can understand and tune LLM costs.
 */
import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import dayjs from 'dayjs'
import {
  ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, CartesianGrid,
  BarChart, Bar, Cell,
} from 'recharts'
import {
  Brain, Zap, Clock, Hash, Activity, Server, AlertTriangle, CheckCircle,
  Settings, TrendingDown, History, ChevronDown, ChevronUp,
} from 'lucide-react'
import {
  fetchLLMAnalytics, fetchOptimizer, applyOptimizer,
  type LLMAgentStat, type LLMProviderRuntimeStat,
  type OptimizerData, type OptimizerAgentContext, type OptimizerHistoryEntry,
} from '../api'
import StatCard from '../components/StatCard'
import { SkeletonStatCards, SkeletonBlock } from '../components/Skeleton'
import EmptyState from '../components/EmptyState'
import PageTransition from '../components/PageTransition'
import { useLiveStore } from '../store'

// ─── Constants ──────────────────────────────────────────────────────────────

const TIME_RANGES = [
  { label: '24h', hours: 24 },
  { label: '7d', hours: 168 },
  { label: '30d', hours: 720 },
  { label: '90d', hours: 2160 },
]

const AGENT_COLORS: Record<string, string> = {
  market_analyst: '#3b82f6',
  strategist: '#8b5cf6',
  risk_manager: '#f59e0b',
  executor: '#22c55e',
  settings_advisor: '#06b6d4',
  portfolio_rotator: '#ec4899',
  universe_scanner: '#f97316',
  telegram: '#a78bfa',
}

function agentColor(name: string): string {
  const lower = name.toLowerCase()
  for (const [key, color] of Object.entries(AGENT_COLORS)) {
    if (lower.includes(key)) return color
  }
  return '#6b7280'
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function fmtK(v: number | null | undefined): string {
  if (v == null) return '—'
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(2)}M`
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}k`
  return String(v)
}

function fmtMs(v: number | null | undefined): string {
  if (v == null) return '—'
  if (v >= 1000) return `${(v / 1000).toFixed(1)}s`
  return `${Math.round(v)}ms`
}

function bucketLabel(bucket: string, bucketType: string): string {
  if (bucketType === 'hourly') return dayjs(bucket).format('HH:mm')
  if (bucketType === 'weekly') return `W${bucket.split('-')[1]}`
  return dayjs(bucket).format('MMM DD')
}

// ─── Charts ──────────────────────────────────────────────────────────────────

function TokenUsageChart({
  data, bucket,
}: {
  data: Array<{ bucket: string; prompt_tokens: number; completion_tokens: number }>
  bucket: string
}) {
  if (!data.length) return (
    <EmptyState icon="chart" title="No token data" description="Appears once LLM calls are made." />
  )
  const chartData = data.map((d) => ({
    label: bucketLabel(d.bucket, bucket),
    input: d.prompt_tokens,
    output: d.completion_tokens,
  }))
  return (
    <ResponsiveContainer width="100%" height={200}>
      <AreaChart data={chartData} margin={{ top: 5, right: 10, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id="inputGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#3b82f6" stopOpacity={0.4} />
            <stop offset="100%" stopColor="#3b82f6" stopOpacity={0.02} />
          </linearGradient>
          <linearGradient id="outputGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#22c55e" stopOpacity={0.4} />
            <stop offset="100%" stopColor="#22c55e" stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
        <XAxis dataKey="label" tick={{ fontSize: 10, fill: '#6e7681' }} interval="preserveStartEnd" />
        <YAxis tick={{ fontSize: 10, fill: '#6e7681' }} tickFormatter={fmtK} width={52} />
        <Tooltip
          contentStyle={{ background: '#161b22', border: '1px solid #30363d', borderRadius: 8, fontSize: 12 }}
          formatter={(v: any, name: string | undefined) => [fmtK(v as number), name === 'input' ? 'Input tokens' : 'Output tokens']}
        />
        <Area type="monotone" dataKey="input" stroke="#3b82f6" strokeWidth={1.5} fill="url(#inputGrad)" stackId="a" />
        <Area type="monotone" dataKey="output" stroke="#22c55e" strokeWidth={1.5} fill="url(#outputGrad)" stackId="a" />
      </AreaChart>
    </ResponsiveContainer>
  )
}

function CallVolumeChart({
  data, bucket,
}: {
  data: Array<{ bucket: string; calls: number; avg_latency_ms: number | null }>
  bucket: string
}) {
  if (!data.length) return null
  const chartData = data.map((d) => ({
    label: bucketLabel(d.bucket, bucket),
    calls: d.calls,
    latency: d.avg_latency_ms ?? 0,
  }))
  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={chartData} margin={{ top: 5, right: 10, bottom: 0, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
        <XAxis dataKey="label" tick={{ fontSize: 10, fill: '#6e7681' }} interval="preserveStartEnd" />
        <YAxis tick={{ fontSize: 10, fill: '#6e7681' }} allowDecimals={false} width={36} />
        <Tooltip
          contentStyle={{ background: '#161b22', border: '1px solid #30363d', borderRadius: 8, fontSize: 12 }}
          formatter={(v: any, name: string | undefined) => [
            name === 'calls' ? v : fmtMs(v as number),
            name === 'calls' ? 'LLM calls' : 'Avg latency',
          ]}
        />
        <Bar dataKey="calls" fill="#8b5cf6" radius={[3, 3, 0, 0]} opacity={0.85} />
      </BarChart>
    </ResponsiveContainer>
  )
}

function AgentBreakdownChart({ agents }: { agents: LLMAgentStat[] }) {
  if (!agents.length) return null
  const data = agents.map((a) => ({
    name: a.agent_name.replace(/_agent$/, '').replace(/_/g, ' '),
    tokens: a.total_tokens,
    calls: a.calls,
    color: agentColor(a.agent_name),
  }))
  return (
    <ResponsiveContainer width="100%" height={Math.max(120, data.length * 36)}>
      <BarChart data={data} layout="vertical" margin={{ top: 0, right: 16, bottom: 0, left: 0 }}>
        <XAxis type="number" tick={{ fontSize: 10, fill: '#6e7681' }} tickFormatter={fmtK} />
        <YAxis type="category" dataKey="name" tick={{ fontSize: 11, fill: '#8b949e' }} width={110} />
        <Tooltip
          contentStyle={{ background: '#161b22', border: '1px solid #30363d', borderRadius: 8, fontSize: 12 }}
          formatter={(v: any, name: string | undefined) => [fmtK(v as number), name === 'tokens' ? 'Tokens' : 'Calls']}
        />
        <Bar dataKey="tokens" radius={[0, 3, 3, 0]}>
          {data.map((d, i) => <Cell key={i} fill={d.color} opacity={0.85} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

// ─── Provider Card ────────────────────────────────────────────────────────────

function ProviderCard({ p }: { p: LLMProviderRuntimeStat }) {
  const tokenPct = p.daily_tokens_budget
    ? Math.min(100, (p.daily_tokens_used / p.daily_tokens_budget) * 100)
    : null
  const reqPct = p.daily_requests_budget
    ? Math.min(100, (p.daily_requests_used / p.daily_requests_budget) * 100)
    : null

  return (
    <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-4 space-y-3">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-semibold text-gray-200">{p.name}</p>
          {p.model && <p className="text-xs text-gray-500 mt-0.5">{p.model}</p>}
        </div>
        <div className="flex items-center gap-2">
          {p.in_cooldown && (
            <span className="flex items-center gap-1 text-xs text-yellow-400 bg-yellow-400/10 border border-yellow-400/20 px-2 py-0.5 rounded-full">
              <AlertTriangle size={10} /> Cooldown
            </span>
          )}
          <span className={`flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border ${p.enabled
            ? 'text-green-400 bg-green-400/10 border-green-400/20'
            : 'text-gray-500 bg-gray-800 border-gray-700'}`}>
            {p.enabled ? <CheckCircle size={10} /> : null}
            {p.enabled ? 'Active' : 'Disabled'}
          </span>
        </div>
      </div>

      {/* Daily token usage */}
      {p.daily_tokens_budget != null && (
        <div>
          <div className="flex justify-between text-xs text-gray-500 mb-1">
            <span>Daily tokens</span>
            <span>{fmtK(p.daily_tokens_used)} / {fmtK(p.daily_tokens_budget)}</span>
          </div>
          <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full transition-all ${tokenPct! > 90 ? 'bg-red-500' : tokenPct! > 70 ? 'bg-yellow-500' : 'bg-blue-500'}`}
              style={{ width: `${tokenPct}%` }}
            />
          </div>
        </div>
      )}

      {/* Daily request usage */}
      {p.daily_requests_budget != null && (
        <div>
          <div className="flex justify-between text-xs text-gray-500 mb-1">
            <span>Daily requests</span>
            <span>{p.daily_requests_used} / {p.daily_requests_budget}</span>
          </div>
          <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full transition-all ${reqPct! > 90 ? 'bg-red-500' : reqPct! > 70 ? 'bg-yellow-500' : 'bg-purple-500'}`}
              style={{ width: `${reqPct}%` }}
            />
          </div>
        </div>
      )}

      {/* Footer stats */}
      <div className="flex gap-4 text-xs text-gray-500 pt-1 border-t border-gray-800">
        {p.rpm_limit != null && <span>RPM limit: <span className="text-gray-300">{p.rpm_limit}</span></span>}
        {p.credits_remaining != null && (
          <span>Credits: <span className={`font-semibold ${p.credits_remaining < 1 ? 'text-red-400' : 'text-green-400'}`}>
            ${p.credits_remaining.toFixed(3)}
          </span></span>
        )}
        {p.daily_tokens_budget == null && p.daily_tokens_used > 0 && (
          <span>Used today: <span className="text-gray-300">{fmtK(p.daily_tokens_used)} tokens</span></span>
        )}
      </div>
    </div>
  )
}

// ─── Optimizer helpers ────────────────────────────────────────────────────────

function agentCtx(byAgent: OptimizerAgentContext[], name: string): OptimizerAgentContext | undefined {
  return byAgent.find((a) => a.agent_name === name)
}

function calcSimulation(
  draft: Record<string, unknown>,
  current: Record<string, unknown>,
  optData: OptimizerData | undefined,
) {
  if (!optData) return null
  const { by_agent, signal_distribution, totals } = optData.context
  const analyst = agentCtx(by_agent, 'market_analyst')
  const strat = agentCtx(by_agent, 'strategist')
  const totalCurrentPrompt = totals?.total_prompt_tokens ?? 0
  if (!totalCurrentPrompt) return null

  // news cap savings (market analyst only)
  const oldNews = Number(current.news_max_chars ?? 1500)
  const newNews = Number(draft.news_max_chars ?? oldNews)
  const newsWeight = 0.22
  const newsSaved = analyst
    ? analyst.calls * analyst.avg_prompt_tokens * newsWeight * Math.max(0, 1 - newNews / Math.max(oldNews, 1))
    : 0

  // strategic context savings (both agents)
  const oldCtx = Number(current.strategic_context_max_chars ?? 800)
  const newCtx = Number(draft.strategic_context_max_chars ?? oldCtx)
  const ctxWeight = 0.12
  const ctxCalls = (analyst?.calls ?? 0) + (strat?.calls ?? 0)
  const ctxAvgPrompt = ctxCalls > 0
    ? ((analyst?.total_prompt_tokens ?? 0) + (strat?.total_prompt_tokens ?? 0)) / ctxCalls
    : 0
  const ctxSaved = ctxCalls * ctxAvgPrompt * ctxWeight * Math.max(0, 1 - newCtx / Math.max(oldCtx, 1))

  // recent outcomes savings (strategist only)
  const oldOutcomes = Number(current.recent_outcomes_n ?? 10)
  const newOutcomes = Number(draft.recent_outcomes_n ?? oldOutcomes)
  const outcomesWeight = 0.10
  const outcomesSaved = strat
    ? strat.calls * strat.avg_prompt_tokens * outcomesWeight * Math.max(0, 1 - newOutcomes / Math.max(oldOutcomes, 1))
    : 0

  // articles savings (analyst)
  const oldArticles = Number(current.articles_for_analysis ?? 8)
  const newArticles = Number(draft.articles_for_analysis ?? oldArticles)
  const articlesWeight = 0.08
  const articlesSaved = analyst
    ? analyst.calls * analyst.avg_prompt_tokens * articlesWeight * Math.max(0, 1 - newArticles / Math.max(oldArticles, 1))
    : 0

  // skip savings: newly skipped signals
  const oldSkip = new Set((current.strategist_skip_signals as string[] | undefined) ?? [])
  const newSkip = new Set((draft.strategist_skip_signals as string[] | undefined) ?? [])
  const totalStratCalls = optData.context.total_strategist_calls || 1
  let skipSaved = 0
  for (const sig of newSkip) {
    if (!oldSkip.has(sig)) {
      const dist = signal_distribution.find((d) => d.signal_type === sig)
      if (dist && strat) {
        skipSaved += (dist.count / totalStratCalls) * strat.total_prompt_tokens
      }
    }
  }
  // Newly UN-skipped signals cost tokens back
  for (const sig of oldSkip) {
    if (!newSkip.has(sig)) {
      const dist = signal_distribution.find((d) => d.signal_type === sig)
      if (dist && strat) {
        skipSaved -= (dist.count / totalStratCalls) * strat.total_prompt_tokens
      }
    }
  }

  const totalSaved = newsSaved + ctxSaved + outcomesSaved + articlesSaved + skipSaved
  const newTotal = Math.max(0, totalCurrentPrompt - totalSaved)
  const savingsPct = totalCurrentPrompt > 0 ? (totalSaved / totalCurrentPrompt) * 100 : 0

  // Quality risk
  const riskySkips = (draft.strategist_skip_signals as string[] | undefined ?? [])
    .filter((s) => s === 'buy' || s === 'sell' || s === 'strong_buy' || s === 'strong_sell')
  const qualityRisk = riskySkips.length > 0 ? 'high'
    : newNews < 400 ? 'medium'
    : newCtx < 200 ? 'medium'
    : newOutcomes === 0 ? 'low'
    : 'minimal'

  return {
    totalCurrentPrompt,
    newTotal: Math.round(newTotal),
    totalSaved: Math.round(totalSaved),
    savingsPct,
    breakdown: [
      { name: 'News cap', saved: Math.round(newsSaved), color: '#3b82f6' },
      { name: 'Context cap', saved: Math.round(ctxSaved), color: '#8b5cf6' },
      { name: 'Signal skip', saved: Math.round(skipSaved), color: '#22c55e' },
      { name: 'Outcomes', saved: Math.round(outcomesSaved), color: '#f59e0b' },
      { name: 'Articles', saved: Math.round(articlesSaved), color: '#06b6d4' },
    ].filter((b) => Math.abs(b.saved) > 10),
    qualityRisk,
  }
}

// ─── OptimizerTab ─────────────────────────────────────────────────────────────

function OptimizerTab({ hours }: { hours: number }) {
  const qc = useQueryClient()
  const profile = useLiveStore((s) => s.profile)
  const { data: optData, isLoading } = useQuery({
    queryKey: ['llm-optimizer', hours, profile],
    queryFn: () => fetchOptimizer(hours),
    refetchInterval: 30_000,
  })

  const current = optData?.settings ?? {}
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const effective = useMemo(() => ({ ...current, ...draft }), [current, draft])

  const sim = useMemo(() => calcSimulation(effective, current, optData), [effective, current, optData])

  const [showHistory, setShowHistory] = useState(false)

  const mutation = useMutation({
    mutationFn: () => applyOptimizer(draft),
    onSuccess: () => {
      setDraft({})
      qc.invalidateQueries({ queryKey: ['llm-optimizer', hours, profile] })
    },
  })

  const hasDraft = Object.keys(draft).length > 0

  function setVal(key: string, value: unknown) {
    setDraft((d) => ({ ...d, [key]: value }))
  }

  function resetKey(key: string) {
    setDraft((d) => { const n = { ...d }; delete n[key]; return n })
  }

  function resetAll() {
    setDraft({})
  }

  if (isLoading) return (
    <div className="space-y-4">
      <SkeletonBlock className="h-32" />
      <SkeletonBlock className="h-48" />
    </div>
  )

  const meta = optData?.param_meta ?? {}
  const history = optData?.history ?? []

  return (
    <div className="space-y-6">
      {/* Parameters */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Integer sliders */}
        {(['news_max_chars', 'strategic_context_max_chars', 'recent_outcomes_n', 'articles_for_analysis'] as const).map((key) => {
          const m = meta[key]
          if (!m || m.type !== 'int') return null
          const curVal = Number(current[key] ?? optData?.defaults[key] ?? 0)
          const val = Number(effective[key] ?? curVal)
          const isDirty = key in draft
          return (
            <div
              key={key}
              className={`bg-gray-900/50 border rounded-xl p-5 transition-colors ${isDirty ? 'border-brand-600/50' : 'border-gray-800'}`}
            >
              <div className="flex items-start justify-between mb-1">
                <div>
                  <p className="text-sm font-semibold text-gray-200">{m.label}</p>
                  <p className="text-xs text-gray-500 mt-0.5">{m.description}</p>
                </div>
                {isDirty && (
                  <button onClick={() => resetKey(key)} className="text-xs text-gray-500 hover:text-gray-300 ml-3 shrink-0">reset</button>
                )}
              </div>
              <div className="flex items-center gap-3 mt-3">
                <input
                  type="range"
                  min={m.min}
                  max={m.max}
                  step={m.step}
                  value={val}
                  onChange={(e) => setVal(key, Number(e.target.value))}
                  className="flex-1 accent-brand-500 h-1"
                />
                <span className={`text-sm font-mono w-16 text-right ${isDirty ? 'text-brand-400' : 'text-gray-300'}`}>
                  {val.toLocaleString()}
                </span>
              </div>
              <div className="flex justify-between text-[10px] text-gray-600 mt-1">
                <span>{(m.min ?? 0).toLocaleString()}</span>
                <span className="text-gray-500">current: {curVal.toLocaleString()}</span>
                <span>{(m.max ?? 0).toLocaleString()}</span>
              </div>
            </div>
          )
        })}

        {/* Multiselect for strategist_skip_signals */}
        {(() => {
          const key = 'strategist_skip_signals'
          const m = meta[key]
          if (!m) return null
          const curVal = (current[key] as string[] | undefined) ?? (optData?.defaults[key] as string[]) ?? []
          const val = (effective[key] as string[] | undefined) ?? curVal
          const isDirty = key in draft
          return (
            <div
              key={key}
              className={`bg-gray-900/50 border rounded-xl p-5 transition-colors ${isDirty ? 'border-brand-600/50' : 'border-gray-800'} lg:col-span-2`}
            >
              <div className="flex items-start justify-between mb-3">
                <div>
                  <p className="text-sm font-semibold text-gray-200">{m.label}</p>
                  <p className="text-xs text-gray-500 mt-0.5">{m.description}</p>
                </div>
                {isDirty && (
                  <button onClick={() => resetKey(key)} className="text-xs text-gray-500 hover:text-gray-300 ml-3 shrink-0">reset</button>
                )}
              </div>
              <div className="flex flex-wrap gap-2">
                {(m.options ?? []).map((opt) => {
                  const isChecked = val.includes(opt)
                  const isHighRisk = opt === 'buy' || opt === 'sell'
                  return (
                    <button
                      key={opt}
                      onClick={() => {
                        const next = isChecked ? val.filter((v) => v !== opt) : [...val, opt]
                        setVal(key, next)
                      }}
                      className={`px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors ${
                        isChecked
                          ? isHighRisk
                            ? 'bg-red-900/30 border-red-600/50 text-red-400'
                            : 'bg-brand-900/30 border-brand-600/50 text-brand-400'
                          : 'bg-gray-800/50 border-gray-700 text-gray-400 hover:border-gray-600'
                      }`}
                    >
                      {isChecked ? '✓ ' : ''}{opt}
                      {isHighRisk && isChecked && ' ⚠'}
                    </button>
                  )
                })}
              </div>
              <p className="text-[10px] text-gray-600 mt-2">
                Current: [{curVal.join(', ')}]
              </p>
            </div>
          )
        })()}
      </div>

      {/* Simulation + Apply */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Simulated impact */}
        <div className="lg:col-span-2 bg-gray-900/50 border border-gray-800 rounded-xl p-5">
          <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-4">
            <TrendingDown size={14} className="text-green-400" />
            Simulated Impact
          </h3>
          {sim ? (
            <div className="space-y-4">
              <div className="grid grid-cols-3 gap-3">
                <div className="text-center">
                  <p className="text-[10px] text-gray-500 uppercase tracking-wider">Current</p>
                  <p className="text-lg font-bold text-gray-300">{fmtK(sim.totalCurrentPrompt)}</p>
                  <p className="text-[10px] text-gray-600">input tokens/{hours}h</p>
                </div>
                <div className="text-center">
                  <p className="text-[10px] text-gray-500 uppercase tracking-wider">Saved</p>
                  <p className={`text-lg font-bold ${sim.totalSaved > 0 ? 'text-green-400' : sim.totalSaved < 0 ? 'text-red-400' : 'text-gray-400'}`}>
                    {sim.totalSaved > 0 ? '-' : ''}{fmtK(Math.abs(sim.totalSaved))}
                  </p>
                  <p className={`text-[10px] ${sim.savingsPct > 0 ? 'text-green-600' : sim.savingsPct < 0 ? 'text-red-600' : 'text-gray-600'}`}>
                    {sim.savingsPct > 0 ? '−' : '+'}{Math.abs(sim.savingsPct).toFixed(1)}%
                  </p>
                </div>
                <div className="text-center">
                  <p className="text-[10px] text-gray-500 uppercase tracking-wider">New Total</p>
                  <p className="text-lg font-bold text-brand-400">{fmtK(sim.newTotal)}</p>
                  <p className="text-[10px] text-gray-600">input tokens/{hours}h</p>
                </div>
              </div>

              {sim.breakdown.length > 0 && (
                <div>
                  <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-2">Savings by category</p>
                  <ResponsiveContainer width="100%" height={120}>
                    <BarChart data={sim.breakdown} layout="vertical" margin={{ left: 0, right: 10, top: 0, bottom: 0 }}>
                      <XAxis type="number" tick={{ fontSize: 10, fill: '#6e7681' }} tickFormatter={fmtK} />
                      <YAxis type="category" dataKey="name" tick={{ fontSize: 10, fill: '#6e7681' }} width={80} />
                      <Tooltip
                        contentStyle={{ background: '#161b22', border: '1px solid #30363d', borderRadius: 8, fontSize: 12 }}
                        formatter={(v: any) => [fmtK(v as number), 'Tokens saved']}
                      />
                      <Bar dataKey="saved" radius={[0, 4, 4, 0]}>
                        {sim.breakdown.map((b) => (
                          <Cell key={b.name} fill={b.saved < 0 ? '#ef4444' : b.color} />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              )}

              <div className={`flex items-center gap-2 text-xs px-3 py-2 rounded-lg border ${
                sim.qualityRisk === 'high' ? 'bg-red-900/20 border-red-800 text-red-400'
                  : sim.qualityRisk === 'medium' ? 'bg-yellow-900/20 border-yellow-800 text-yellow-400'
                  : 'bg-green-900/20 border-green-800 text-green-400'
              }`}>
                {sim.qualityRisk === 'high' ? <AlertTriangle size={12} /> : <CheckCircle size={12} />}
                Quality risk: <strong>{sim.qualityRisk}</strong>
                {sim.qualityRisk === 'high' && ' — skipping actionable signals (buy/sell) may miss trades'}
                {sim.qualityRisk === 'medium' && ' — very low context limits may reduce signal quality'}
                {sim.qualityRisk === 'minimal' && ' — settings look safe'}
              </div>
            </div>
          ) : (
            <p className="text-sm text-gray-500">Adjust parameters above to see simulated impact.</p>
          )}
        </div>

        {/* Apply panel */}
        <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5 flex flex-col gap-4">
          <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2">
            <Settings size={14} className="text-brand-400" />
            Apply Changes
          </h3>
          {hasDraft ? (
            <div className="space-y-2 flex-1">
              {Object.entries(draft).map(([k, v]) => (
                <div key={k} className="flex flex-col text-xs">
                  <span className="text-gray-400 font-medium">{meta[k]?.label ?? k}</span>
                  <span className="text-gray-600">
                    {JSON.stringify(current[k])} → <span className="text-brand-400">{JSON.stringify(v)}</span>
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs text-gray-500 flex-1">No pending changes. Adjust sliders above.</p>
          )}
          <div className="flex flex-col gap-2 mt-auto">
            <button
              disabled={!hasDraft || mutation.isPending}
              onClick={() => mutation.mutate()}
              className="w-full px-4 py-2.5 bg-brand-600 hover:bg-brand-500 disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm font-semibold rounded-lg transition-colors"
            >
              {mutation.isPending ? 'Applying…' : 'Apply Now'}
            </button>
            {hasDraft && (
              <button
                onClick={resetAll}
                className="w-full px-4 py-2 text-xs text-gray-500 hover:text-gray-300 border border-gray-800 rounded-lg transition-colors"
              >
                Discard changes
              </button>
            )}
            {mutation.isSuccess && (
              <p className="text-xs text-green-400 flex items-center gap-1">
                <CheckCircle size={11} /> Changes applied — takes effect within 30s
              </p>
            )}
            {mutation.isError && (
              <p className="text-xs text-red-400 flex items-center gap-1">
                <AlertTriangle size={11} /> {(mutation.error as Error).message}
              </p>
            )}
          </div>
        </div>
      </div>

      {/* Change history */}
      <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
        <button
          onClick={() => setShowHistory((v) => !v)}
          className="w-full flex items-center justify-between text-sm font-semibold text-gray-300"
        >
          <span className="flex items-center gap-2">
            <History size={14} className="text-gray-500" />
            Change History
            {history.length > 0 && (
              <span className="text-xs bg-gray-800 text-gray-400 px-1.5 py-0.5 rounded">{history.length}</span>
            )}
          </span>
          {showHistory ? <ChevronUp size={14} className="text-gray-500" /> : <ChevronDown size={14} className="text-gray-500" />}
        </button>
        {showHistory && (
          <div className="mt-4 space-y-3">
            {history.length === 0 ? (
              <p className="text-xs text-gray-600">No changes recorded yet.</p>
            ) : (
              [...history].reverse().map((entry: OptimizerHistoryEntry, i) => (
                <div key={i} className="flex gap-3 text-xs border-l-2 border-gray-800 pl-3">
                  <div className="min-w-0 flex-1">
                    <p className="text-gray-400 font-medium">{dayjs(entry.ts).format('MMM D HH:mm')}</p>
                    {Object.entries(entry.changes).map(([k, c]) => (
                      <p key={k} className="text-gray-600">
                        {meta[k]?.label ?? k}: <span className="text-gray-500">{JSON.stringify(c.from)}</span>
                        {' → '}<span className="text-brand-400">{JSON.stringify(c.to)}</span>
                      </p>
                    ))}
                  </div>
                </div>
              ))
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// ─── Main Component ───────────────────────────────────────────────────────────

export default function LLMAnalytics() {
  const [hours, setHours] = useState(168)
  const [tab, setTab] = useState<'overview' | 'optimizer'>('overview')
  const profile = useLiveStore((s) => s.profile)

  const { data, isLoading } = useQuery({
    queryKey: ['llm-analytics', hours, profile],
    queryFn: () => fetchLLMAnalytics(hours),
    refetchInterval: 60_000,
  })

  const s = data?.summary
  const hasData = (s?.total_calls ?? 0) > 0

  return (
    <PageTransition>
      <div className="p-6 space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div className="flex items-center gap-3">
            <h2 className="text-xl font-bold text-gray-100">LLM Analytics</h2>
            {/* Tab switcher */}
            <div className="flex gap-1 bg-gray-800/60 rounded-lg p-0.5">
              <button
                onClick={() => setTab('overview')}
                className={`px-3 py-1 text-xs rounded-md font-medium transition-colors ${tab === 'overview'
                  ? 'bg-gray-700 text-gray-100'
                  : 'text-gray-500 hover:text-gray-300'}`}
              >
                Overview
              </button>
              <button
                onClick={() => setTab('optimizer')}
                className={`px-3 py-1 text-xs rounded-md font-medium flex items-center gap-1 transition-colors ${tab === 'optimizer'
                  ? 'bg-gray-700 text-gray-100'
                  : 'text-gray-500 hover:text-gray-300'}`}
              >
                <Settings size={11} />
                Optimizer
              </button>
            </div>
          </div>
          <div className="flex gap-1">
            {TIME_RANGES.map((r) => (
              <button
                key={r.hours}
                onClick={() => setHours(r.hours)}
                className={`px-3 py-1.5 text-xs rounded-lg font-medium transition-colors ${hours === r.hours
                  ? 'bg-brand-600/30 text-brand-400 border border-brand-600/50'
                  : 'bg-gray-800/50 text-gray-400 border border-gray-800 hover:border-gray-700'
                  }`}
              >
                {r.label}
              </button>
            ))}
          </div>
        </div>

        {/* Optimizer tab */}
        {tab === 'optimizer' && <OptimizerTab hours={hours} />}

        {tab === 'overview' && <>
        {/* ── Summary cards ── */}
        <div>
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wider mb-2">Usage Overview</p>
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
            {isLoading ? (
              <SkeletonStatCards count={6} />
            ) : (
              <>
                <StatCard
                  label="Total Calls"
                  value={hasData ? fmtK(s!.total_calls) : '—'}
                  accent={hasData ? 'blue' : 'gray'}
                  icon={<Brain size={14} />}
                  sub={hasData ? `${s!.total_cycles} cycles` : 'No calls yet'}
                />
                <StatCard
                  label="Input Tokens"
                  value={hasData ? fmtK(s!.total_prompt_tokens) : '—'}
                  accent={hasData ? 'blue' : 'gray'}
                  icon={<Hash size={14} />}
                  sub={hasData ? `avg ${fmtK(s!.avg_prompt_tokens)}/call` : undefined}
                />
                <StatCard
                  label="Output Tokens"
                  value={hasData ? fmtK(s!.total_completion_tokens) : '—'}
                  accent={hasData ? 'green' : 'gray'}
                  icon={<Hash size={14} />}
                  sub={hasData ? `avg ${fmtK(s!.avg_completion_tokens)}/call` : undefined}
                />
                <StatCard
                  label="Total Tokens"
                  value={hasData ? fmtK(s!.total_tokens) : '—'}
                  accent={hasData ? 'blue' : 'gray'}
                  icon={<Zap size={14} />}
                  sub={hasData ? `avg ${fmtK(s!.avg_total_tokens)}/call` : undefined}
                />
                <StatCard
                  label="Avg Latency"
                  value={hasData ? fmtMs(s!.avg_latency_ms) : '—'}
                  accent={hasData ? (s!.avg_latency_ms! > 5000 ? 'red' : 'green') : 'gray'}
                  icon={<Clock size={14} />}
                  sub={hasData ? `p90: ${fmtMs(s!.p90_latency_ms)}` : undefined}
                />
                <StatCard
                  label="Unique Pairs"
                  value={hasData ? String(s!.unique_pairs) : '—'}
                  accent={hasData ? 'blue' : 'gray'}
                  icon={<Activity size={14} />}
                  sub={hasData ? `${data!.by_agent.length} agents` : undefined}
                />
              </>
            )}
          </div>
        </div>

        {/* ── Token usage over time + Call volume ── */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-4">
              <Hash size={14} className="text-blue-400" />
              Token Usage
              <span className="ml-auto text-xs text-gray-600">
                <span className="inline-block w-2 h-2 rounded-sm bg-blue-500 mr-1" />input
                <span className="inline-block w-2 h-2 rounded-sm bg-green-500 mx-1 ml-2" />output
              </span>
            </h3>
            {isLoading ? (
              <SkeletonBlock className="h-[200px]" />
            ) : (
              <TokenUsageChart data={data?.time_series ?? []} bucket={data?.bucket ?? 'daily'} />
            )}
          </div>

          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-4">
              <Brain size={14} className="text-purple-400" />
              Call Volume
            </h3>
            {isLoading ? (
              <SkeletonBlock className="h-[200px]" />
            ) : hasData ? (
              <CallVolumeChart data={data!.time_series} bucket={data!.bucket} />
            ) : (
              <EmptyState icon="chart" title="No call data" description="Appears after the first LLM cycle." />
            )}
          </div>
        </div>

        {/* ── By-agent breakdown ── */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* Chart */}
          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-4">
              <Activity size={14} className="text-brand-400" />
              Tokens by Agent
            </h3>
            {isLoading ? (
              <SkeletonBlock className="h-[180px]" />
            ) : data?.by_agent.length ? (
              <AgentBreakdownChart agents={data.by_agent} />
            ) : (
              <EmptyState icon="chart" title="No agent data" />
            )}
          </div>

          {/* Table */}
          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-4">
              <Activity size={14} className="text-brand-400" />
              Agent Details
            </h3>
            {isLoading ? (
              <SkeletonBlock className="h-[180px]" />
            ) : data?.by_agent.length ? (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-gray-800">
                      <th className="text-left text-gray-500 font-medium pb-2 pr-3">Agent</th>
                      <th className="text-right text-gray-500 font-medium pb-2 px-2">Calls</th>
                      <th className="text-right text-gray-500 font-medium pb-2 px-2">In</th>
                      <th className="text-right text-gray-500 font-medium pb-2 px-2">Out</th>
                      <th className="text-right text-gray-500 font-medium pb-2 pl-2">Latency</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-800/50">
                    {data.by_agent.map((a) => (
                      <tr key={a.agent_name} className="hover:bg-gray-800/20 transition-colors">
                        <td className="py-2 pr-3">
                          <div className="flex items-center gap-1.5">
                            <span
                              className="w-2 h-2 rounded-full flex-shrink-0"
                              style={{ background: agentColor(a.agent_name) }}
                            />
                            <span className="text-gray-300 truncate max-w-[120px]">
                              {a.agent_name.replace(/_agent$/, '').replace(/_/g, ' ')}
                            </span>
                          </div>
                        </td>
                        <td className="text-right text-gray-400 py-2 px-2">{a.calls}</td>
                        <td className="text-right text-blue-400 py-2 px-2">{fmtK(a.prompt_tokens)}</td>
                        <td className="text-right text-green-400 py-2 px-2">{fmtK(a.completion_tokens)}</td>
                        <td className="text-right text-gray-400 py-2 pl-2">{fmtMs(a.avg_latency_ms)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState icon="chart" title="No agent data" />
            )}
          </div>
        </div>

        {/* ── Latency percentiles + Exchange split ── */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* Latency breakdown */}
          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-4">
              <Clock size={14} className="text-yellow-400" />
              Latency Breakdown
            </h3>
            {isLoading ? (
              <SkeletonBlock className="h-[120px]" />
            ) : hasData ? (
              <div className="grid grid-cols-2 gap-3">
                {[
                  { label: 'Min', value: s!.min_latency_ms, color: 'text-green-400' },
                  { label: 'p50', value: s!.p50_latency_ms, color: 'text-blue-400' },
                  { label: 'p90', value: s!.p90_latency_ms, color: 'text-yellow-400' },
                  { label: 'p99 / Max', value: s!.p99_latency_ms ?? s!.max_latency_ms, color: 'text-red-400' },
                ].map(({ label, value, color }) => (
                  <div key={label} className="bg-gray-800/50 rounded-lg p-3 text-center">
                    <p className="text-xs text-gray-500 mb-1">{label}</p>
                    <p className={`text-xl font-bold ${color}`}>{fmtMs(value)}</p>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState icon="chart" title="No latency data" />
            )}
          </div>

          {/* Exchange split */}
          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-4">
              <Server size={14} className="text-cyan-400" />
              By Exchange
            </h3>
            {isLoading ? (
              <SkeletonBlock className="h-[120px]" />
            ) : data?.by_exchange.length ? (
              <div className="space-y-3">
                {data.by_exchange.map((ex) => {
                  const total = data.by_exchange.reduce((acc, e) => acc + e.calls, 0)
                  const pct = total > 0 ? (ex.calls / total) * 100 : 0
                  return (
                    <div key={ex.exchange}>
                      <div className="flex justify-between text-xs mb-1">
                        <span className="text-gray-300 capitalize font-medium">{ex.exchange}</span>
                        <span className="text-gray-500">{ex.calls} calls · {fmtK(ex.total_tokens)} tokens</span>
                      </div>
                      <div className="h-2 bg-gray-800 rounded-full overflow-hidden">
                        <div
                          className="h-full bg-cyan-500 rounded-full"
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                    </div>
                  )
                })}
              </div>
            ) : (
              <EmptyState icon="chart" title="No exchange data" />
            )}
          </div>
        </div>

        {/* ── Top pairs by LLM calls ── */}
        {hasData && data!.top_pairs.length > 0 && (
          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-4">
              <Zap size={14} className="text-yellow-400" />
              Most Analyzed Pairs
            </h3>
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-2">
              {data!.top_pairs.slice(0, 15).map((p, i) => {
                const max = data!.top_pairs[0].calls
                const pct = max > 0 ? (p.calls / max) * 100 : 0
                return (
                  <div key={p.pair} className="bg-gray-800/50 rounded-lg p-3">
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-xs font-semibold text-gray-200">{p.pair}</span>
                      <span className="text-xs text-gray-500">#{i + 1}</span>
                    </div>
                    <div className="h-1 bg-gray-700 rounded-full mb-2 overflow-hidden">
                      <div className="h-full bg-purple-500 rounded-full" style={{ width: `${pct}%` }} />
                    </div>
                    <p className="text-xs text-gray-500">
                      <span className="text-gray-300 font-medium">{p.calls}</span> calls
                    </p>
                    <p className="text-xs text-gray-600">{fmtK(p.total_tokens)} tokens</p>
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {/* ── Provider status ── */}
        {(data?.providers.length ?? 0) > 0 && (
          <div>
            <p className="text-xs font-medium text-gray-500 uppercase tracking-wider mb-2">
              Live Provider Status
            </p>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
              {data!.providers.map((p) => (
                <ProviderCard key={p.name} p={p} />
              ))}
            </div>
          </div>
        )}

        {/* ── No data empty state ── */}
        {!isLoading && !hasData && (
          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-12">
            <EmptyState
              icon="chart"
              title="No LLM calls recorded yet"
              description="Stats appear once the trading bot runs its first analysis cycle."
            />
          </div>
        )}
        </>}
      </div>
    </PageTransition>
  )
}
