/**
 * RegressionAI — Per-symbol drill-down with domain-aware picker.
 *
 * Layout:
 *   ┌──────────────────────┬─────────────────────────────────────────────┐
 *   │  Picker (left rail)  │  Detail tabs (right panel)                  │
 *   │  • search filter     │  • Overview  · plain-English summary +      │
 *   │  • crypto / equity   │     KPI tiles + LIVE IMPACT panel           │
 *   │  • symbol list with  │  • Regression · OLS rows for this symbol    │
 *   │    R / P / T data    │  • Patterns   · upcoming catalysts          │
 *   │    flags             │  • Trades     · recent fills                │
 *   │                      │  • Reasoning  · agent_reasoning cycles      │
 *   └──────────────────────┴─────────────────────────────────────────────┘
 *
 * Honesty contract (no hallucinations):
 *   • Every number renders from the JSON payload — no client-side fudging.
 *   • The plain-English summary is built server-side via deterministic
 *     templates over the same numbers (no LLM in the data path).
 *   • The LIVE IMPACT panel hard-references file:line of the actual
 *     consumer code so observational data is never confused with active
 *     trading signals.
 *
 * Domain separation: every useQuery key includes `profile`.
 */
import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'
import {
  Brain, RefreshCw, AlertCircle, TrendingUp, TrendingDown, Minus,
  Play, Search, Activity, Calendar, FileText, BookOpen, Zap, ZapOff,
  CircleDot, Info,
} from 'lucide-react'
import {
  fetchSymbolList,
  fetchSymbolSummary,
  triggerRegressionRun,
  type SymbolListItem,
  type SymbolSummaryResponse,
  type RegressionModelRow,
} from '../api'
import { useLiveStore } from '../store'
import PageTransition from '../components/PageTransition'
import { SkeletonBlock } from '../components/Skeleton'
import EmptyState from '../components/EmptyState'

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${(v * 100).toFixed(digits)}%`
}

function fmtNum(v: number | null | undefined, digits = 3): string {
  if (v == null || Number.isNaN(v)) return '—'
  return v.toFixed(digits)
}

function fmtMoney(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  const sign = v >= 0 ? '+' : ''
  return `${sign}${v.toFixed(2)}`
}

function DirectionIcon({ v }: { v: number | null | undefined }) {
  if (v == null || Number.isNaN(v) || Math.abs(v) < 1e-6)
    return <Minus size={12} className="text-gray-500" />
  return v > 0
    ? <TrendingUp size={12} className="text-emerald-400" />
    : <TrendingDown size={12} className="text-rose-400" />
}

function QualityBadge({ r2, n }: { r2: number | null | undefined; n: number }) {
  if (n < 5) return <span className="rounded border border-gray-700 bg-gray-900 px-1.5 py-0.5 text-[10px] text-gray-500">N/A</span>
  const v = r2 == null || Number.isNaN(r2) ? 0 : r2
  if (v >= 0.3 && n >= 20)
    return <span className="rounded border border-emerald-700/60 bg-emerald-900/40 px-1.5 py-0.5 text-[10px] text-emerald-300 font-semibold">STRONG</span>
  if (v >= 0.1 && n >= 10)
    return <span className="rounded border border-amber-700/60 bg-amber-900/40 px-1.5 py-0.5 text-[10px] text-amber-300">MODERATE</span>
  return <span className="rounded border border-gray-700 bg-gray-900 px-1.5 py-0.5 text-[10px] text-gray-500">WEAK</span>
}

/** Three-dot data-availability indicator: R = regression, P = patterns, T = trades. */
function DataDots({ item }: { item: SymbolListItem }) {
  return (
    <span className="inline-flex items-center gap-0.5 font-mono text-[10px]">
      <span title={item.has_regression ? 'has regression model' : 'no regression model'}
        className={item.has_regression ? 'text-emerald-400' : 'text-gray-700'}>R</span>
      <span title={item.has_patterns ? 'has upcoming catalysts' : 'no upcoming catalysts'}
        className={item.has_patterns ? 'text-amber-400' : 'text-gray-700'}>P</span>
      <span title={item.has_trades ? 'recent trades (7d)' : 'no recent trades'}
        className={item.has_trades ? 'text-brand-400' : 'text-gray-700'}>T</span>
    </span>
  )
}

/** Live-impact pill — green if signal is consumed by trading loop, grey otherwise. */
function LiveImpactPill({
  label, on, hint,
}: { label: string; on: boolean; hint: string }) {
  return (
    <div
      className={`flex items-center justify-between gap-2 rounded-lg border px-3 py-2 ${
        on
          ? 'border-emerald-700/50 bg-emerald-950/30'
          : 'border-gray-800 bg-gray-950/40'
      }`}
      title={hint}
    >
      <div className="flex items-center gap-2 text-xs">
        {on
          ? <Zap size={12} className="text-emerald-400" />
          : <ZapOff size={12} className="text-gray-600" />}
        <span className={on ? 'text-emerald-200 font-medium' : 'text-gray-400'}>
          {label}
        </span>
      </div>
      <span className={`text-[10px] uppercase tracking-wide ${on ? 'text-emerald-400' : 'text-gray-600'}`}>
        {on ? 'live' : 'observational'}
      </span>
    </div>
  )
}

type TabKey = 'overview' | 'regression' | 'patterns' | 'trades' | 'reasoning'

const TABS: { key: TabKey; label: string; icon: typeof Brain }[] = [
  { key: 'overview', label: 'Overview', icon: Brain },
  { key: 'regression', label: 'Regression', icon: Activity },
  { key: 'patterns', label: 'Patterns', icon: Calendar },
  { key: 'trades', label: 'Trades', icon: FileText },
  { key: 'reasoning', label: 'Reasoning', icon: BookOpen },
]

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function RegressionAI() {
  const profile = useLiveStore(s => s.profile)
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [tab, setTab] = useState<TabKey>('overview')
  const [triggering, setTriggering] = useState(false)
  const [triggerMsg, setTriggerMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)

  // Picker
  const listQ = useQuery({
    queryKey: ['symbols-list', profile, search],
    queryFn: () => fetchSymbolList(search || undefined),
  })

  // Auto-select the first symbol with data when the list arrives.
  useEffect(() => {
    if (selected) return
    const first = listQ.data?.items?.find(
      i => i.has_regression || i.has_patterns || i.has_trades,
    ) ?? listQ.data?.items?.[0]
    if (first) setSelected(first.symbol)
  }, [listQ.data, selected])

  // Reset selection when profile changes (domain-aware).
  useEffect(() => {
    setSelected(null)
  }, [profile])

  const detailQ = useQuery({
    queryKey: ['symbol-summary', profile, selected],
    queryFn: () => selected ? fetchSymbolSummary(selected) : Promise.reject(new Error('no symbol')),
    enabled: !!selected,
  })

  const onTrigger = async () => {
    setTriggering(true)
    setTriggerMsg(null)
    try {
      const res = await triggerRegressionRun()
      setTriggerMsg({ kind: 'ok', text: `Started ${res.workflow_id} — refresh in ~1–2 min.` })
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      setTriggerMsg({ kind: 'err', text: `Trigger failed: ${msg}` })
    } finally {
      setTriggering(false)
    }
  }

  const filteredItems: SymbolListItem[] = listQ.data?.items ?? []
  const summaryStats = useMemo(() => {
    if (!filteredItems.length) return null
    return {
      total: filteredItems.length,
      withReg: filteredItems.filter(i => i.has_regression).length,
      withPat: filteredItems.filter(i => i.has_patterns).length,
      withTrades: filteredItems.filter(i => i.has_trades).length,
    }
  }, [filteredItems])

  return (
    <PageTransition>
      <div className="space-y-3">
        {/* Header */}
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Brain size={18} className="text-brand-400" />
            <h1 className="text-lg font-semibold">Regression AI</h1>
            {listQ.data && (
              <span className="text-xs text-gray-500 ml-2">
                {listQ.data.count} symbols · {listQ.data.exchange} ({listQ.data.domain})
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={onTrigger}
              disabled={triggering}
              className="inline-flex items-center gap-1 rounded border border-emerald-700/60 bg-emerald-900/30 px-3 py-1 text-xs text-emerald-200 hover:bg-emerald-900/50 disabled:opacity-50"
              title="Start an out-of-schedule EventRegressionWorkflow run for the active profile"
            >
              <Play size={12} className={triggering ? 'animate-pulse' : ''} />
              {triggering ? 'Starting…' : 'Run now'}
            </button>
            <button
              onClick={() => { listQ.refetch(); detailQ.refetch() }}
              disabled={listQ.isFetching || detailQ.isFetching}
              className="inline-flex items-center gap-1 rounded border border-gray-700 bg-gray-900 px-3 py-1 text-xs text-gray-300 hover:bg-gray-800 disabled:opacity-50"
            >
              <RefreshCw size={12} className={(listQ.isFetching || detailQ.isFetching) ? 'animate-spin' : ''} />
              Refresh
            </button>
          </div>
        </div>

        {triggerMsg && (
          <div
            className={`rounded-xl border px-3 py-2 text-xs ${
              triggerMsg.kind === 'ok'
                ? 'border-emerald-800/60 bg-emerald-950/40 text-emerald-200'
                : 'border-rose-800/60 bg-rose-950/40 text-rose-200'
            }`}
          >
            {triggerMsg.text}
          </div>
        )}

        {/* Picker stats strip */}
        {summaryStats && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
            <div className="rounded-xl border border-gray-800 bg-gray-950/60 p-2.5">
              <div className="text-gray-500">Symbols in universe</div>
              <div className="text-gray-100 text-base font-semibold">{summaryStats.total}</div>
            </div>
            <div className="rounded-xl border border-emerald-900/40 bg-emerald-950/20 p-2.5">
              <div className="text-emerald-500/80">w/ regression model</div>
              <div className="text-emerald-300 text-base font-semibold">{summaryStats.withReg}</div>
            </div>
            <div className="rounded-xl border border-amber-900/40 bg-amber-950/20 p-2.5">
              <div className="text-amber-500/80">w/ upcoming catalyst</div>
              <div className="text-amber-300 text-base font-semibold">{summaryStats.withPat}</div>
            </div>
            <div className="rounded-xl border border-brand-900/40 bg-brand-950/20 p-2.5">
              <div className="text-brand-400/80">traded last 7d</div>
              <div className="text-brand-300 text-base font-semibold">{summaryStats.withTrades}</div>
            </div>
          </div>
        )}

        {/* Two-column layout */}
        <div className="grid grid-cols-1 lg:grid-cols-[280px_1fr] gap-3">
          {/* ─── Left rail: picker ─────────────────────────────────────── */}
          <div className="rounded-xl border border-gray-800 bg-gray-950/60">
            <div className="p-2 border-b border-gray-800">
              <div className="relative">
                <Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-500" />
                <input
                  type="text"
                  value={search}
                  onChange={e => setSearch(e.target.value)}
                  placeholder="Filter symbols…"
                  className="w-full rounded border border-gray-800 bg-gray-900 pl-7 pr-2 py-1.5 text-xs text-gray-200 placeholder-gray-600 focus:border-brand-600 outline-none"
                />
              </div>
            </div>
            <div className="px-2 py-1 text-[10px] text-gray-500 flex justify-between">
              <span>Symbol</span>
              <span title="R = regression model · P = upcoming pattern · T = trade in last 7d">
                R P T
              </span>
            </div>
            <div className="max-h-[60vh] overflow-y-auto">
              {listQ.isLoading && <SkeletonBlock style={{ height: 240 }} />}
              {listQ.error && (
                <div className="p-3 text-xs text-rose-300 flex items-center gap-1">
                  <AlertCircle size={12} /> Failed to load
                </div>
              )}
              {filteredItems.length === 0 && !listQ.isLoading && !listQ.error && (
                <div className="p-3 text-xs text-gray-500">No symbols match.</div>
              )}
              <ul className="divide-y divide-gray-900">
                {filteredItems.map(item => {
                  const active = selected === item.symbol
                  return (
                    <li key={item.symbol}>
                      <button
                        onClick={() => { setSelected(item.symbol); setTab('overview') }}
                        className={`w-full flex items-center justify-between gap-2 px-3 py-1.5 text-left text-xs transition-colors ${
                          active
                            ? 'bg-brand-950/60 text-brand-100 border-l-2 border-brand-500'
                            : 'text-gray-300 hover:bg-gray-900/60 border-l-2 border-transparent'
                        }`}
                      >
                        <span className="flex items-center gap-2 min-w-0">
                          <CircleDot
                            size={8}
                            className={
                              item.regression_quality === 'strong' ? 'text-emerald-400'
                              : item.regression_quality === 'moderate' ? 'text-amber-400'
                              : 'text-gray-700'
                            }
                          />
                          <span className="font-mono truncate">{item.symbol}</span>
                        </span>
                        <DataDots item={item} />
                      </button>
                    </li>
                  )
                })}
              </ul>
            </div>
          </div>

          {/* ─── Right panel: detail ───────────────────────────────────── */}
          <div className="rounded-xl border border-gray-800 bg-gray-950/60 min-h-[60vh]">
            {!selected && (
              <EmptyState
                icon="search"
                title="Pick a symbol to start"
                description="The left rail shows every symbol in the active profile. Coloured dots flag what data is available (R=regression, P=upcoming pattern, T=recent trade)."
              />
            )}
            {selected && detailQ.isLoading && (
              <div className="p-4">
                <SkeletonBlock style={{ height: 320 }} />
              </div>
            )}
            {selected && detailQ.error && (
              <div className="m-4 rounded-xl border border-rose-900/60 bg-rose-950/30 p-4 text-sm text-rose-300 flex items-center gap-2">
                <AlertCircle size={14} /> Failed to load summary for {selected}
              </div>
            )}
            {selected && detailQ.data && (
              <DetailPanel data={detailQ.data} tab={tab} setTab={setTab} />
            )}
          </div>
        </div>
      </div>
    </PageTransition>
  )
}

// ---------------------------------------------------------------------------
// Detail panel (right column)
// ---------------------------------------------------------------------------

function DetailPanel({
  data, tab, setTab,
}: {
  data: SymbolSummaryResponse
  tab: TabKey
  setTab: (t: TabKey) => void
}) {
  return (
    <div className="flex flex-col">
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800">
        <div className="flex items-center gap-3">
          <div>
            <div className="text-xs text-gray-500 uppercase tracking-wide">{data.domain}</div>
            <div className="text-xl font-mono text-gray-100">{data.symbol}</div>
          </div>
          <div className="flex items-center gap-1 ml-4 text-xs">
            <span className="rounded border border-gray-800 bg-gray-900 px-2 py-1 text-gray-400">
              {data.regression.count} model(s)
            </span>
            <span className="rounded border border-gray-800 bg-gray-900 px-2 py-1 text-gray-400">
              {data.patterns.count} catalyst(s)
            </span>
            <span className="rounded border border-gray-800 bg-gray-900 px-2 py-1 text-gray-400">
              {data.trades.count} trade(s)
            </span>
          </div>
        </div>
      </div>

      <div className="flex items-center gap-1 px-3 py-2 border-b border-gray-800 overflow-x-auto">
        {TABS.map(({ key, label, icon: Icon }) => {
          const active = tab === key
          return (
            <button
              key={key}
              onClick={() => setTab(key)}
              className={`inline-flex items-center gap-1 rounded px-3 py-1 text-xs whitespace-nowrap transition ${
                active
                  ? 'bg-brand-900/40 text-brand-200 border border-brand-700/60'
                  : 'text-gray-400 hover:text-gray-200 border border-transparent'
              }`}
            >
              <Icon size={12} />
              {label}
            </button>
          )
        })}
      </div>

      <div className="p-4">
        {tab === 'overview' && <OverviewTab data={data} />}
        {tab === 'regression' && <RegressionTab rows={data.regression.rows} />}
        {tab === 'patterns' && <PatternsTab data={data} />}
        {tab === 'trades' && <TradesTab data={data} />}
        {tab === 'reasoning' && <ReasoningTab data={data} />}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

function OverviewTab({ data }: { data: SymbolSummaryResponse }) {
  const li = data.live_impact
  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-gray-800 bg-gray-950 p-3">
        <div className="flex items-center gap-1 text-[10px] uppercase tracking-wide text-gray-500 mb-1">
          <Info size={10} /> deterministic summary (no LLM)
        </div>
        <div className="text-sm text-gray-200 leading-relaxed">
          {data.plain_summary}
        </div>
      </div>

      <div className="rounded-xl border border-gray-800 bg-gray-950 p-3">
        <div className="flex items-center justify-between mb-2">
          <div className="text-xs font-semibold text-gray-200 inline-flex items-center gap-1">
            <Zap size={12} className="text-emerald-400" /> Live impact on trading
          </div>
          <span className="text-[10px] text-gray-500">
            green = consumed by the decision loop · grey = display only
          </span>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          <LiveImpactPill label="Pattern signals" on={li.patterns.in_decision_loop} hint={li.patterns.where} />
          <LiveImpactPill label="Regression factor" on={li.regressions.in_decision_loop} hint={`${li.regressions.where}${li.regressions.feature_flag ? ` · ${li.regressions.feature_flag}` : ''}`} />
          <LiveImpactPill label="Trade history (Kelly + edge library)" on={li.trades.in_decision_loop} hint={li.trades.where} />
          <LiveImpactPill label="Reasoning journal" on={li.reasoning_journal.in_decision_loop} hint={li.reasoning_journal.where} />
        </div>
        {li.regressions.feature_flag && (
          <div className="mt-2 text-[10px] text-gray-500">
            <span className="text-amber-400">Note:</span>&nbsp;
            Regression factor is gated by <span className="font-mono text-gray-400">{li.regressions.feature_flag}</span>.
            When disabled, the model is fitted nightly but not applied to position sizing.
          </div>
        )}
      </div>
    </div>
  )
}

function RegressionTab({ rows }: { rows: RegressionModelRow[] }) {
  if (!rows.length) {
    return (
      <EmptyState
        icon="search"
        title="No regression models for this symbol"
        description="EventRegressionWorkflow runs nightly. Trigger it from the header or wait for the next 02:30 UTC run."
      />
    )
  }
  return (
    <div className="overflow-x-auto rounded-xl border border-gray-800 bg-gray-950/60">
      <table className="w-full text-xs">
        <thead className="bg-gray-900/80 text-gray-400">
          <tr>
            <th className="text-left px-3 py-2 font-medium">Event</th>
            <th className="text-right px-3 py-2 font-medium">Horizon</th>
            <th className="text-right px-3 py-2 font-medium">N</th>
            <th className="text-left px-3 py-2 font-medium">Quality</th>
            <th className="text-right px-3 py-2 font-medium">R²</th>
            <th className="text-right px-3 py-2 font-medium">Mean fwd</th>
            <th className="text-right px-3 py-2 font-medium">Median fwd</th>
            <th className="text-right px-3 py-2 font-medium">Hit-rate</th>
            <th className="text-right px-3 py-2 font-medium">β pre_ret</th>
            <th className="text-right px-3 py-2 font-medium">β pre_vol</th>
            <th className="text-left px-3 py-2 font-medium">Status</th>
            <th className="text-right px-3 py-2 font-medium">Updated</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800">
          {rows.map(r => (
            <tr key={`${r.event_type}-${r.horizon_days}`} className="hover:bg-gray-900/40">
              <td className="px-3 py-2 text-gray-300">{r.event_type}</td>
              <td className="px-3 py-2 text-right text-gray-400">{r.horizon_days}d</td>
              <td className="px-3 py-2 text-right text-gray-400">{r.sample_count}</td>
              <td className="px-3 py-2"><QualityBadge r2={r.r_squared} n={r.sample_count} /></td>
              <td className="px-3 py-2 text-right font-mono text-gray-300">{fmtPct(r.r_squared, 1)}</td>
              <td className="px-3 py-2 text-right">
                <span className="inline-flex items-center gap-1 font-mono">
                  <DirectionIcon v={r.mean_forward_return} />
                  {fmtPct(r.mean_forward_return)}
                </span>
              </td>
              <td className="px-3 py-2 text-right font-mono">{fmtPct(r.median_forward_return)}</td>
              <td className="px-3 py-2 text-right font-mono">{fmtPct(r.hit_rate, 1)}</td>
              <td className="px-3 py-2 text-right font-mono text-gray-400">{fmtNum(r.coefficients_json?.pre_return_5)}</td>
              <td className="px-3 py-2 text-right font-mono text-gray-400">{fmtNum(r.coefficients_json?.pre_volatility_10)}</td>
              <td className="px-3 py-2 text-gray-500">{r.notes || '—'}</td>
              <td className="px-3 py-2 text-right text-gray-500">{dayjs(r.computed_at).format('MM/DD HH:mm')}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function PatternsTab({ data }: { data: SymbolSummaryResponse }) {
  const items = data.patterns.upcoming
  if (!items.length) {
    return (
      <EmptyState
        icon="search"
        title="No upcoming catalysts"
        description="The catalyst horizon is 30 days. Earnings, macro events, on-chain unlocks etc. will appear here when scheduled."
      />
    )
  }
  return (
    <div className="space-y-2">
      {items.map((ev, i) => (
        <div key={i} className="rounded-xl border border-gray-800 bg-gray-950/60 p-3">
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <Calendar size={12} className="text-amber-400" />
              <span className="text-sm text-gray-200 font-medium">{ev.event_type}</span>
              <span className="text-xs text-gray-500">{dayjs(ev.event_ts).format('YYYY-MM-DD HH:mm')}</span>
            </div>
            {ev.outcome && (
              <span
                className={`text-[10px] uppercase rounded px-2 py-0.5 border ${
                  ev.outcome.direction === 'bullish'
                    ? 'border-emerald-800/60 bg-emerald-950/40 text-emerald-300'
                    : ev.outcome.direction === 'bearish'
                    ? 'border-rose-800/60 bg-rose-950/40 text-rose-300'
                    : 'border-gray-800 bg-gray-900 text-gray-500'
                }`}
              >
                {ev.outcome.direction} · {ev.outcome.n_matches} match{ev.outcome.n_matches === 1 ? '' : 'es'}
              </span>
            )}
          </div>
          {ev.outcome?.expected_drift && (
            <div className="mt-2 text-[11px] text-gray-400 font-mono">
              expected drift:{' '}
              {Object.entries(ev.outcome.expected_drift).map(([k, v]) => (
                <span key={k} className="mr-2">
                  {k}={v == null ? '—' : `${(Number(v) * 100).toFixed(2)}%`}
                </span>
              ))}
            </div>
          )}
          {ev.outcome?.error && (
            <div className="mt-1 text-[11px] text-rose-400">{ev.outcome.error}</div>
          )}
        </div>
      ))}
    </div>
  )
}

function TradesTab({ data }: { data: SymbolSummaryResponse }) {
  const rows = data.trades.rows
  if (!rows.length) {
    return (
      <EmptyState
        icon="search"
        title="No trades for this symbol"
        description="No fills in the last 7 days for this pair."
      />
    )
  }
  return (
    <div className="overflow-x-auto rounded-xl border border-gray-800 bg-gray-950/60">
      <table className="w-full text-xs">
        <thead className="bg-gray-900/80 text-gray-400">
          <tr>
            <th className="text-left px-3 py-2 font-medium">When</th>
            <th className="text-left px-3 py-2 font-medium">Action</th>
            <th className="text-right px-3 py-2 font-medium">Qty</th>
            <th className="text-right px-3 py-2 font-medium">Price</th>
            <th className="text-right px-3 py-2 font-medium">PnL</th>
            <th className="text-left px-3 py-2 font-medium">Signal</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800">
          {rows.map((t, i) => {
            const ts = (t.ts as string | undefined) ?? ''
            const action = (t.action as string | undefined) ?? ''
            const qty = t.quantity as number | undefined
            const price = t.price as number | undefined
            const pnl = t.pnl as number | undefined
            const signal = (t.signal_type as string | undefined) ?? ''
            const pnlPos = (pnl ?? 0) > 0
            const pnlNeg = (pnl ?? 0) < 0
            return (
              <tr key={i} className="hover:bg-gray-900/40">
                <td className="px-3 py-2 text-gray-400">{ts ? dayjs(ts).format('MM/DD HH:mm') : '—'}</td>
                <td className="px-3 py-2">
                  <span className={`text-[11px] uppercase rounded px-1.5 py-0.5 border ${
                    action === 'buy'
                      ? 'border-emerald-800/60 bg-emerald-950/40 text-emerald-300'
                      : 'border-rose-800/60 bg-rose-950/40 text-rose-300'
                  }`}>{action || '—'}</span>
                </td>
                <td className="px-3 py-2 text-right font-mono text-gray-300">{fmtNum(qty, 4)}</td>
                <td className="px-3 py-2 text-right font-mono text-gray-300">{fmtNum(price, 2)}</td>
                <td className={`px-3 py-2 text-right font-mono ${pnlPos ? 'text-emerald-300' : pnlNeg ? 'text-rose-300' : 'text-gray-500'}`}>
                  {fmtMoney(pnl)}
                </td>
                <td className="px-3 py-2 text-gray-500">{signal || '—'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function ReasoningTab({ data }: { data: SymbolSummaryResponse }) {
  const cycles = data.reasoning.cycles
  if (!cycles.length) {
    return (
      <EmptyState
        icon="search"
        title="No agent reasoning recorded yet"
        description="Each trading cycle persists its agents' reasoning JSON. They appear here after the next pipeline run on this symbol."
      />
    )
  }
  return (
    <div className="space-y-2">
      {cycles.map(c => (
        <div key={c.cycle_id} className="rounded-xl border border-gray-800 bg-gray-950/60 p-3">
          <div className="flex items-center justify-between text-xs">
            <div className="flex items-center gap-2">
              <BookOpen size={12} className="text-brand-400" />
              <span className="text-gray-200 font-mono text-[11px]">{c.cycle_id}</span>
              <span className="text-gray-500">{dayjs(c.started_at).format('MM/DD HH:mm')}</span>
            </div>
            <div className="flex items-center gap-2 text-[11px]">
              {c.signal_type && <span className="rounded border border-gray-800 bg-gray-900 px-2 py-0.5 text-gray-400">{c.signal_type}</span>}
              {c.confidence != null && <span className="text-gray-400 font-mono">conf {(c.confidence * 100).toFixed(0)}%</span>}
              {c.action && (
                <span className={`uppercase rounded px-2 py-0.5 border ${
                  c.action === 'buy'
                    ? 'border-emerald-800/60 bg-emerald-950/40 text-emerald-300'
                    : c.action === 'sell'
                    ? 'border-rose-800/60 bg-rose-950/40 text-rose-300'
                    : 'border-gray-800 bg-gray-900 text-gray-500'
                }`}>{c.action}</span>
              )}
              {c.pnl != null && (
                <span className={`font-mono ${c.pnl > 0 ? 'text-emerald-300' : c.pnl < 0 ? 'text-rose-300' : 'text-gray-500'}`}>
                  pnl {fmtMoney(c.pnl)}
                </span>
              )}
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}
