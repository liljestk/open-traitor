/**
 * PatternsPage — Catalyst Pattern Engine viewer.
 *
 * Lists upcoming catalyst events for the active profile's universe and the
 * pattern-engine outcome derived from historical analogs. Click a row to see
 * top-k matches in detail.
 *
 * Domain separation: every useQuery key includes `profile` so cached data
 * never bleeds across exchanges.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'
import { Sparkles, TrendingUp, TrendingDown, Minus, RefreshCw, ChevronRight, AlertCircle } from 'lucide-react'
import {
  fetchUpcomingPatterns,
  fetchEventMatches,
  fetchPatternStatus,
  type UpcomingPatternRow,
  type PatternOutcomeSummary,
} from '../api'
import { useLiveStore } from '../store'
import PageTransition from '../components/PageTransition'
import { SkeletonBlock } from '../components/Skeleton'
import EmptyState from '../components/EmptyState'

function DirectionPill({ outcome }: { outcome: PatternOutcomeSummary }) {
  const dir = outcome.direction
  if (dir === 'bullish')
    return (
      <span className="inline-flex items-center gap-1 rounded bg-emerald-900/40 border border-emerald-700/40 px-2 py-0.5 text-[11px] font-medium text-emerald-300">
        <TrendingUp size={12} /> Bullish
      </span>
    )
  if (dir === 'bearish')
    return (
      <span className="inline-flex items-center gap-1 rounded bg-rose-900/40 border border-rose-700/40 px-2 py-0.5 text-[11px] font-medium text-rose-300">
        <TrendingDown size={12} /> Bearish
      </span>
    )
  return (
    <span className="inline-flex items-center gap-1 rounded bg-gray-800/70 border border-gray-700 px-2 py-0.5 text-[11px] font-medium text-gray-400">
      <Minus size={12} /> Neutral
    </span>
  )
}

function fmtPct(v: number | undefined | null): string {
  if (v === undefined || v === null || Number.isNaN(v)) return '—'
  return `${(v * 100).toFixed(2)}%`
}

function ConfidenceBar({ c }: { c: number }) {
  const pct = Math.max(0, Math.min(1, c)) * 100
  return (
    <div className="w-24 h-2 rounded bg-gray-800 overflow-hidden border border-gray-700/60">
      <div
        className="h-full bg-gradient-to-r from-brand-700 to-brand-400"
        style={{ width: `${pct}%` }}
      />
    </div>
  )
}

function MatchesPanel({ eventId, onClose }: { eventId: string; onClose: () => void }) {
  const profile = useLiveStore(s => s.profile)
  const { data, isLoading, error } = useQuery({
    queryKey: ['pattern-event-matches', profile, eventId],
    queryFn: () => fetchEventMatches(eventId),
    enabled: !!eventId,
  })

  return (
    <div className="rounded-xl border border-gray-800 bg-gray-950/80 p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-gray-200">Top historical analogs</h3>
        <button
          onClick={onClose}
          className="text-xs text-gray-500 hover:text-gray-300"
        >
          Close
        </button>
      </div>
      {isLoading && <SkeletonBlock style={{ height: 120 }} />}
      {error && (
        <div className="text-xs text-rose-400 flex items-center gap-1">
          <AlertCircle size={12} /> Failed to load matches
        </div>
      )}
      {data && (
        <>
          <div className="mb-3 grid grid-cols-3 gap-3 text-xs">
            <div>
              <div className="text-gray-500">Direction</div>
              <DirectionPill outcome={data.outcome} />
            </div>
            <div>
              <div className="text-gray-500">Confidence</div>
              <div className="flex items-center gap-2">
                <ConfidenceBar c={data.outcome.confidence} />
                <span className="text-gray-300 font-mono">
                  {(data.outcome.confidence * 100).toFixed(0)}%
                </span>
              </div>
            </div>
            <div>
              <div className="text-gray-500">N matches</div>
              <div className="text-gray-200 font-mono">{data.outcome.n_matches}</div>
            </div>
          </div>
          <div className="grid grid-cols-3 gap-3 text-xs mb-3">
            {Object.entries(data.outcome.expected_drift || {}).map(([h, v]) => (
              <div key={h}>
                <div className="text-gray-500">Drift {h}</div>
                <div className="font-mono text-gray-200">{fmtPct(v)}</div>
                <div className="text-[10px] text-gray-500">
                  ± {fmtPct(data.outcome.dispersion?.[h])}
                </div>
              </div>
            ))}
          </div>
          {Array.isArray(data.outcome.matches) && data.outcome.matches.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-gray-500">
                  <tr className="border-b border-gray-800">
                    <th className="text-left py-1 pr-2">Symbol</th>
                    <th className="text-left py-1 pr-2">Anchor</th>
                    <th className="text-right py-1 pr-2">Sim</th>
                    <th className="text-right py-1 pr-2">1d</th>
                    <th className="text-right py-1 pr-2">5d</th>
                    <th className="text-right py-1">20d</th>
                  </tr>
                </thead>
                <tbody className="text-gray-300">
                  {data.outcome.matches.slice(0, 20).map((m, i) => {
                    const sim = (m.similarity as number) ?? 0
                    const sym = (m.symbol as string) ?? '?'
                    const ts = m.anchor_ts ? dayjs(m.anchor_ts as string).format('YYYY-MM-DD') : '?'
                    const fr = (m.forward_returns as Record<string, number>) || {}
                    return (
                      <tr key={i} className="border-b border-gray-900/60">
                        <td className="py-1 pr-2 font-mono">{sym}</td>
                        <td className="py-1 pr-2">{ts}</td>
                        <td className="py-1 pr-2 text-right font-mono">{sim.toFixed(3)}</td>
                        <td className="py-1 pr-2 text-right font-mono">{fmtPct(fr['1d'])}</td>
                        <td className="py-1 pr-2 text-right font-mono">{fmtPct(fr['5d'])}</td>
                        <td className="py-1 text-right font-mono">{fmtPct(fr['20d'])}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  )
}

export default function PatternsPage() {
  const profile = useLiveStore(s => s.profile)
  const [horizonDays, setHorizonDays] = useState<number>(30)
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null)

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['patterns-upcoming', profile, horizonDays],
    queryFn: () => fetchUpcomingPatterns(horizonDays, 'ONE_DAY', 20, 50),
  })

  const { data: status } = useQuery({
    queryKey: ['patterns-status', profile],
    queryFn: () => fetchPatternStatus(),
    refetchInterval: 60_000,
  })

  return (
    <PageTransition>
      <div className="px-4 py-4 space-y-4">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <div className="flex items-center gap-2">
            <Sparkles size={18} className="text-violet-400" />
            <h1 className="text-lg font-semibold text-gray-100">Catalyst Patterns</h1>
            <span className="text-xs text-gray-500">
              Historical analogs around upcoming catalysts
            </span>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-gray-500">Horizon</label>
            <select
              value={horizonDays}
              onChange={e => setHorizonDays(Number(e.target.value))}
              className="bg-gray-900 border border-gray-800 rounded text-xs text-gray-200 px-2 py-1"
            >
              <option value={7}>7d</option>
              <option value={14}>14d</option>
              <option value={30}>30d</option>
              <option value={60}>60d</option>
              <option value={90}>90d</option>
            </select>
            <button
              onClick={() => refetch()}
              className="inline-flex items-center gap-1 rounded bg-gray-900 border border-gray-800 px-2 py-1 text-xs text-gray-300 hover:bg-gray-800"
            >
              <RefreshCw size={12} className={isFetching ? 'animate-spin' : ''} />
              Refresh
            </button>
          </div>
        </div>

        {isLoading && <SkeletonBlock style={{ height: 240 }} />}
        {error && (
          <div className="text-sm text-rose-400 flex items-center gap-1">
            <AlertCircle size={14} /> Failed to load patterns
          </div>
        )}

        {status && (
          <div className="rounded-xl border border-gray-800 bg-gray-950/60 px-4 py-3 text-xs text-gray-300 flex flex-wrap items-center gap-x-6 gap-y-1">
            <div className="flex items-center gap-1">
              <span className={status.ready ? 'text-emerald-400' : 'text-amber-400'}>
                ●
              </span>
              <span>{status.ready ? 'Engine ready' : 'Warming up'}</span>
            </div>
            <div>Catalysts: <span className="text-gray-100">{status.counts.catalyst_events}</span></div>
            <div>Fingerprints: <span className="text-gray-100">{status.counts.pattern_fingerprints}</span></div>
            <div>Symbols w/ history: <span className="text-gray-100">{status.counts.historical_candles_symbols}</span></div>
            <div>Backfill rows: <span className="text-gray-100">{status.counts.backfill_progress_rows}</span></div>
          </div>
        )}

        {data && data.items.length === 0 && !isLoading && (
          <EmptyState
            icon="chart"
            title="No upcoming catalysts"
            description={`No catalyst events scheduled in the next ${horizonDays} days for this profile's universe.`}
          />
        )}

        {data && data.items.length > 0 && (
          <div className="rounded-xl border border-gray-800 bg-gray-950/60 overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-900/60 text-gray-400 text-xs uppercase tracking-wide">
                <tr>
                  <th className="text-left px-3 py-2">Symbol</th>
                  <th className="text-left px-3 py-2">Catalyst</th>
                  <th className="text-left px-3 py-2">When</th>
                  <th className="text-left px-3 py-2">Direction</th>
                  <th className="text-right px-3 py-2">Drift 5d</th>
                  <th className="text-right px-3 py-2">N</th>
                  <th className="text-left px-3 py-2">Confidence</th>
                  <th className="px-3 py-2"></th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((row: UpcomingPatternRow) => {
                  const ev = row.upcoming_event
                  const drift5 = row.outcome.expected_drift?.['5d']
                  const isSel = selectedEventId === ev.id
                  return (
                    <tr
                      key={`${ev.id}`}
                      className={`border-t border-gray-800/60 ${isSel ? 'bg-brand-900/10' : 'hover:bg-gray-900/40'}`}
                    >
                      <td className="px-3 py-2 font-mono text-gray-200">{row.symbol}</td>
                      <td className="px-3 py-2 text-gray-300">{ev.event_type}</td>
                      <td className="px-3 py-2 text-gray-400 text-xs">
                        {dayjs(ev.event_ts).format('YYYY-MM-DD')}
                      </td>
                      <td className="px-3 py-2">
                        <DirectionPill outcome={row.outcome} />
                      </td>
                      <td className="px-3 py-2 text-right font-mono text-gray-200">
                        {fmtPct(drift5)}
                      </td>
                      <td className="px-3 py-2 text-right font-mono text-gray-300">
                        {row.outcome.n_matches}
                      </td>
                      <td className="px-3 py-2">
                        <div className="flex items-center gap-2">
                          <ConfidenceBar c={row.outcome.confidence} />
                          <span className="text-xs text-gray-400 font-mono">
                            {(row.outcome.confidence * 100).toFixed(0)}%
                          </span>
                        </div>
                      </td>
                      <td className="px-3 py-2 text-right">
                        <button
                          onClick={() =>
                            setSelectedEventId(isSel ? null : (ev.id as string))
                          }
                          className="inline-flex items-center gap-1 text-xs text-brand-400 hover:text-brand-300"
                        >
                          {isSel ? 'Hide' : 'Inspect'}
                          <ChevronRight
                            size={12}
                            className={`transition-transform ${isSel ? 'rotate-90' : ''}`}
                          />
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}

        {selectedEventId && (
          <MatchesPanel
            eventId={selectedEventId}
            onClose={() => setSelectedEventId(null)}
          />
        )}
      </div>
    </PageTransition>
  )
}
