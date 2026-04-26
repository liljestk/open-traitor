/**
 * RegressionAI — Event–price regression viewer.
 *
 * Surfaces the OLS models fitted nightly by EventRegressionWorkflow.
 * For each (symbol, event_type, horizon_days) row we show the sample
 * size, R², mean / median forward return and hit-rate so the operator
 * can spot which catalysts historically move price in this universe.
 *
 * Domain separation: every useQuery key includes `profile` so cached
 * data never bleeds across exchanges (enforced by static test
 * TestFrontendQueryKeysIncludeProfile).
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'
import { Brain, RefreshCw, AlertCircle, TrendingUp, TrendingDown, Minus } from 'lucide-react'
import {
  fetchRegressionModels,
  type RegressionModelRow,
} from '../api'
import { useLiveStore } from '../store'
import PageTransition from '../components/PageTransition'
import { SkeletonBlock } from '../components/Skeleton'
import EmptyState from '../components/EmptyState'

function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${(v * 100).toFixed(digits)}%`
}

function fmtNum(v: number | null | undefined, digits = 3): string {
  if (v == null || Number.isNaN(v)) return '—'
  return v.toFixed(digits)
}

function DirectionIcon({ v }: { v: number | null | undefined }) {
  if (v == null || Number.isNaN(v) || Math.abs(v) < 1e-6)
    return <Minus size={12} className="text-gray-500" />
  return v > 0
    ? <TrendingUp size={12} className="text-emerald-400" />
    : <TrendingDown size={12} className="text-rose-400" />
}

function R2Pill({ r2 }: { r2: number | null | undefined }) {
  if (r2 == null || Number.isNaN(r2)) {
    return <span className="text-gray-500 text-xs">—</span>
  }
  const pct = Math.max(0, Math.min(1, r2)) * 100
  const color =
    r2 >= 0.3 ? 'bg-emerald-900/40 border-emerald-700/40 text-emerald-300'
    : r2 >= 0.1 ? 'bg-amber-900/40 border-amber-700/40 text-amber-300'
    : 'bg-gray-800/70 border-gray-700 text-gray-400'
  return (
    <span className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 text-[11px] font-mono ${color}`}>
      R²={pct.toFixed(1)}%
    </span>
  )
}

function NotesPill({ notes }: { notes: string }) {
  if (notes === 'ok')
    return <span className="text-emerald-400 text-[11px]">ok</span>
  if (notes === 'few_samples')
    return <span className="text-amber-400 text-[11px]">few samples</span>
  return <span className="text-gray-500 text-[11px]">{notes || '—'}</span>
}

export default function RegressionAI() {
  const profile = useLiveStore(s => s.profile)
  const [orderBy, setOrderBy] = useState<'r_squared' | 'samples' | 'computed_at' | 'abs_return'>('r_squared')
  const [minSamples, setMinSamples] = useState(8)
  const [eventType, setEventType] = useState('')
  const [symbolFilter, setSymbolFilter] = useState('')

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['regression-models', profile, orderBy, minSamples, eventType, symbolFilter],
    queryFn: () => fetchRegressionModels({
      orderBy,
      minSamples,
      eventType: eventType || undefined,
      symbol: symbolFilter || undefined,
      limit: 200,
    }),
  })

  const rows: RegressionModelRow[] = data?.rows ?? []

  // Derive event-type chips from result set for one-click filtering.
  const eventTypes = Array.from(new Set(rows.map(r => r.event_type))).sort()

  return (
    <PageTransition>
      <div className="space-y-4">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Brain size={18} className="text-brand-400" />
            <h1 className="text-lg font-semibold">Regression AI</h1>
            <span className="text-xs text-gray-500 ml-2">
              {data ? `${data.count} of ${data.total} models · ${data.exchange}` : ''}
            </span>
          </div>
          <button
            onClick={() => refetch()}
            disabled={isFetching}
            className="inline-flex items-center gap-1 rounded border border-gray-700 bg-gray-900 px-3 py-1 text-xs text-gray-300 hover:bg-gray-800 disabled:opacity-50"
          >
            <RefreshCw size={12} className={isFetching ? 'animate-spin' : ''} />
            Refresh
          </button>
        </div>

        <div className="rounded-xl border border-gray-800 bg-gray-950/60 p-3 flex flex-wrap items-center gap-3 text-xs">
          <label className="flex items-center gap-1">
            <span className="text-gray-500">Sort</span>
            <select
              value={orderBy}
              onChange={e => setOrderBy(e.target.value as typeof orderBy)}
              className="rounded border border-gray-700 bg-gray-900 px-2 py-1 text-gray-200"
            >
              <option value="r_squared">R² (best fit)</option>
              <option value="abs_return">|Mean return|</option>
              <option value="samples">Sample size</option>
              <option value="computed_at">Most recent</option>
            </select>
          </label>
          <label className="flex items-center gap-1">
            <span className="text-gray-500">Min samples</span>
            <input
              type="number"
              min={0}
              value={minSamples}
              onChange={e => setMinSamples(Number(e.target.value) || 0)}
              className="w-16 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-gray-200"
            />
          </label>
          <label className="flex items-center gap-1">
            <span className="text-gray-500">Symbol</span>
            <input
              type="text"
              value={symbolFilter}
              onChange={e => setSymbolFilter(e.target.value.toUpperCase())}
              placeholder="e.g. AAPL"
              className="w-28 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-gray-200"
            />
          </label>
          <div className="flex items-center gap-1 flex-wrap">
            <span className="text-gray-500">Event type</span>
            <button
              onClick={() => setEventType('')}
              className={`rounded px-2 py-0.5 border text-[11px] ${eventType === '' ? 'border-brand-500 bg-brand-900/40 text-brand-200' : 'border-gray-700 bg-gray-900 text-gray-400 hover:text-gray-200'}`}
            >all</button>
            {eventTypes.slice(0, 8).map(et => (
              <button
                key={et}
                onClick={() => setEventType(et === eventType ? '' : et)}
                className={`rounded px-2 py-0.5 border text-[11px] ${et === eventType ? 'border-brand-500 bg-brand-900/40 text-brand-200' : 'border-gray-700 bg-gray-900 text-gray-400 hover:text-gray-200'}`}
              >{et}</button>
            ))}
          </div>
        </div>

        {isLoading && <SkeletonBlock style={{ height: 240 }} />}

        {error && (
          <div className="rounded-xl border border-rose-900/60 bg-rose-950/30 p-4 text-sm text-rose-300 flex items-center gap-2">
            <AlertCircle size={14} /> Failed to load regression models
          </div>
        )}

        {!isLoading && !error && rows.length === 0 && (
          <EmptyState
            icon="search"
            title="No regression models yet"
            description="EventRegressionWorkflow runs nightly at 02:30 UTC. Trigger it manually from Temporal UI to see results sooner."
          />
        )}

        {rows.length > 0 && (
          <div className="overflow-x-auto rounded-xl border border-gray-800 bg-gray-950/60">
            <table className="w-full text-xs">
              <thead className="bg-gray-900/80 text-gray-400">
                <tr>
                  <th className="text-left px-3 py-2">Symbol</th>
                  <th className="text-left px-3 py-2">Event</th>
                  <th className="text-right px-3 py-2">Horizon</th>
                  <th className="text-right px-3 py-2">N</th>
                  <th className="text-right px-3 py-2">R²</th>
                  <th className="text-right px-3 py-2">Mean fwd</th>
                  <th className="text-right px-3 py-2">Median fwd</th>
                  <th className="text-right px-3 py-2">Hit-rate</th>
                  <th className="text-right px-3 py-2">β pre_ret</th>
                  <th className="text-right px-3 py-2">β pre_vol</th>
                  <th className="text-left px-3 py-2">Status</th>
                  <th className="text-right px-3 py-2">Updated</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800">
                {rows.map(r => (
                  <tr key={`${r.symbol}-${r.event_type}-${r.horizon_days}`} className="hover:bg-gray-900/40">
                    <td className="px-3 py-2 font-mono text-gray-200">{r.symbol}</td>
                    <td className="px-3 py-2 text-gray-300">{r.event_type}</td>
                    <td className="px-3 py-2 text-right text-gray-400">{r.horizon_days}d</td>
                    <td className="px-3 py-2 text-right text-gray-400">{r.sample_count}</td>
                    <td className="px-3 py-2 text-right"><R2Pill r2={r.r_squared} /></td>
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
                    <td className="px-3 py-2"><NotesPill notes={r.notes} /></td>
                    <td className="px-3 py-2 text-right text-gray-500">{dayjs(r.computed_at).format('MM/DD HH:mm')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </PageTransition>
  )
}
