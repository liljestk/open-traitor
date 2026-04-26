/**
 * Recommendations — Backtest-derived parameter change inbox.
 *
 * Surfaces the rows persisted by ``run_nightly_backtests``. Each row is a
 * single pending change (e.g. "add pair X to active rotation") that the
 * operator can approve or reject. Approval is *advisory only* — it never
 * mutates live config; downstream deployment is operator-owned.
 *
 * Domain separation: every useQuery key includes `profile`.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import dayjs from 'dayjs'
import {
  Lightbulb,
  RefreshCw,
  Check,
  X,
  AlertCircle,
  Clock,
} from 'lucide-react'
import {
  fetchRecommendations,
  decideRecommendation,
  type RecommendationRow,
  type RecommendationStatus,
} from '../api'
import { useLiveStore } from '../store'
import PageTransition from '../components/PageTransition'
import { SkeletonBlock } from '../components/Skeleton'
import EmptyState from '../components/EmptyState'

const STATUS_FILTERS: ReadonlyArray<{ key: RecommendationStatus | ''; label: string }> = [
  { key: 'pending', label: 'Pending' },
  { key: 'approved', label: 'Approved' },
  { key: 'rejected', label: 'Rejected' },
  { key: 'expired', label: 'Expired' },
  { key: '', label: 'All' },
]

function StatusPill({ status }: { status: RecommendationStatus }) {
  const map: Record<RecommendationStatus, string> = {
    pending: 'bg-amber-900/40 border-amber-700/40 text-amber-300',
    approved: 'bg-emerald-900/40 border-emerald-700/40 text-emerald-300',
    rejected: 'bg-rose-900/40 border-rose-700/40 text-rose-300',
    expired: 'bg-gray-800 border-gray-700 text-gray-400',
  }
  return (
    <span
      className={`inline-flex items-center rounded border px-2 py-0.5 text-[11px] font-mono uppercase ${map[status]}`}
    >
      {status}
    </span>
  )
}

function MetricPill({ name, value }: { name: string; value: number | null }) {
  if (!name || value == null || Number.isNaN(value)) return null
  return (
    <span className="inline-flex items-center gap-1 rounded border border-gray-700 bg-gray-900 px-2 py-0.5 text-[11px] font-mono text-gray-300">
      {name}={value.toFixed(2)}
    </span>
  )
}

export default function RecommendationsPage() {
  const profile = useLiveStore(s => s.profile)
  const qc = useQueryClient()
  const [statusFilter, setStatusFilter] = useState<RecommendationStatus | ''>('pending')

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['backtest-recommendations', profile, statusFilter],
    queryFn: () => fetchRecommendations({ status: statusFilter || undefined, limit: 200 }),
  })

  const decideMutation = useMutation({
    mutationFn: ({ id, status }: { id: number; status: 'approved' | 'rejected' }) =>
      decideRecommendation(id, status),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['backtest-recommendations', profile] })
    },
  })

  const rows: RecommendationRow[] = data?.rows ?? []
  const counts = data?.counts ?? {}

  return (
    <PageTransition>
      <div className="space-y-4">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Lightbulb size={22} className="text-amber-400" />
            <div>
              <h1 className="text-xl font-semibold text-gray-100">Recommendations</h1>
              <p className="text-xs text-gray-400">
                Backtest-derived parameter change suggestions. Approval is advisory —
                live config is operator-applied.
              </p>
            </div>
          </div>
          <button
            onClick={() => refetch()}
            disabled={isFetching}
            className="inline-flex items-center gap-2 rounded border border-gray-700 bg-gray-800 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-700 disabled:opacity-50"
          >
            <RefreshCw size={14} className={isFetching ? 'animate-spin' : ''} />
            Refresh
          </button>
        </div>

        {/* Status filter chips */}
        <div className="flex flex-wrap gap-2">
          {STATUS_FILTERS.map(({ key, label }) => {
            const n = key === '' ? (counts.total ?? 0) : (counts[key] ?? 0)
            const active = key === statusFilter
            return (
              <button
                key={key || 'all'}
                onClick={() => setStatusFilter(key)}
                className={`inline-flex items-center gap-2 rounded border px-3 py-1 text-xs ${
                  active
                    ? 'border-blue-700 bg-blue-900/40 text-blue-200'
                    : 'border-gray-700 bg-gray-900 text-gray-300 hover:bg-gray-800'
                }`}
              >
                {label}
                <span className="rounded bg-gray-800/80 px-1.5 py-0.5 font-mono text-[10px] text-gray-400">
                  {n}
                </span>
              </button>
            )
          })}
        </div>

        {/* Body */}
        {isLoading ? (
          <SkeletonBlock />
        ) : error ? (
          <div className="rounded border border-rose-800/40 bg-rose-950/30 p-3 text-rose-300">
            <div className="flex items-center gap-2 text-sm">
              <AlertCircle size={14} />
              Failed to load recommendations: {(error as Error).message}
            </div>
          </div>
        ) : rows.length === 0 ? (
          <EmptyState
            icon="search"
            title="No recommendations"
            description={
              statusFilter === 'pending'
                ? 'Nightly backtests will surface candidate changes here. Run nightly_backtest manually if you want to populate this view sooner.'
                : `No ${statusFilter || 'matching'} recommendations.`
            }
          />
        ) : (
          <ul className="space-y-2">
            {rows.map(row => (
              <li
                key={row.id}
                className="rounded border border-gray-800 bg-gray-900/40 p-3"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <StatusPill status={row.status} />
                      <span className="rounded border border-gray-700 bg-gray-900 px-2 py-0.5 font-mono text-[11px] text-gray-300 uppercase">
                        {row.kind}
                      </span>
                      {row.symbol && (
                        <span className="rounded border border-gray-700 bg-gray-900 px-2 py-0.5 font-mono text-[11px] text-gray-200">
                          {row.symbol}
                        </span>
                      )}
                      <MetricPill name={row.metric_name} value={row.metric_value} />
                      <span className="text-[11px] text-gray-500">
                        from {row.source}
                      </span>
                    </div>
                    <p className="mt-2 text-sm text-gray-100">{row.summary}</p>
                    {row.rationale && (
                      <p className="mt-1 text-xs text-gray-400">{row.rationale}</p>
                    )}
                    <div className="mt-2 flex flex-wrap items-center gap-3 text-[11px] text-gray-500">
                      <span className="inline-flex items-center gap-1">
                        <Clock size={10} />
                        created {dayjs(row.created_at).fromNow()}
                      </span>
                      {row.status === 'pending' && (
                        <span>expires {dayjs(row.expires_at).fromNow()}</span>
                      )}
                      {row.decided_at && (
                        <span>
                          {row.status} by {row.decided_by || 'unknown'}{' '}
                          {dayjs(row.decided_at).fromNow()}
                        </span>
                      )}
                    </div>
                  </div>
                  {row.status === 'pending' && (
                    <div className="flex shrink-0 flex-col gap-1">
                      <button
                        onClick={() =>
                          decideMutation.mutate({ id: row.id, status: 'approved' })
                        }
                        disabled={decideMutation.isPending}
                        className="inline-flex items-center gap-1 rounded border border-emerald-800/60 bg-emerald-900/40 px-2 py-1 text-xs text-emerald-200 hover:bg-emerald-900/60 disabled:opacity-50"
                      >
                        <Check size={12} />
                        Approve
                      </button>
                      <button
                        onClick={() =>
                          decideMutation.mutate({ id: row.id, status: 'rejected' })
                        }
                        disabled={decideMutation.isPending}
                        className="inline-flex items-center gap-1 rounded border border-rose-800/60 bg-rose-900/40 px-2 py-1 text-xs text-rose-200 hover:bg-rose-900/60 disabled:opacity-50"
                      >
                        <X size={12} />
                        Reject
                      </button>
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </PageTransition>
  )
}
