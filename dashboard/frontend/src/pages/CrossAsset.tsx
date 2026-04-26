/**
 * CrossAsset — cross-asset correlation, cluster, and cascade explorer.
 *
 * Honesty contract: every number renders directly from the API payload —
 * no client-side fudging. Domain separation: every useQuery key includes
 * `profile` (enforced by tests/test_domain_separation.py).
 *
 * Tabs:
 *   • Clusters    — auto-detected high-correlation cohorts.
 *   • Correlations — top |pearson| pairs + lead-lag.
 *   • Cascade     — pick a driver event_type → see the persisted
 *                   cross-event regression predictions for related symbols.
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Network, RefreshCw, Search, AlertCircle, ArrowRight, Layers, Activity } from 'lucide-react'
import {
  fetchAssetClusters,
  fetchAssetCorrelations,
  fetchCrossEventRegressions,
  fetchCascade,
  type AssetCluster,
  type AssetCorrelationRow,
  type CrossEventRegressionRow,
  type CascadePrediction,
} from '../api'
import { useLiveStore } from '../store'
import PageTransition from '../components/PageTransition'
import { SkeletonBlock } from '../components/Skeleton'
import EmptyState from '../components/EmptyState'

type TabId = 'clusters' | 'correlations' | 'cascade'

function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${(v * 100).toFixed(digits)}%`
}

function fmtNum(v: number | null | undefined, digits = 3): string {
  if (v == null || Number.isNaN(v)) return '—'
  return v.toFixed(digits)
}

function pearsonClass(p: number | null | undefined): string {
  if (p == null) return 'text-gray-500'
  const a = Math.abs(p)
  if (a >= 0.8) return 'text-emerald-300 font-semibold'
  if (a >= 0.6) return 'text-emerald-400'
  if (a >= 0.4) return 'text-amber-300'
  return 'text-gray-400'
}

export default function CrossAsset() {
  const profile = useLiveStore(s => s.profile)
  const [tab, setTab] = useState<TabId>('clusters')
  const [search, setSearch] = useState('')

  // ── Clusters ──────────────────────────────────────────────────────────
  const clustersQ = useQuery({
    queryKey: ['cross-asset', 'clusters', profile],
    queryFn: () => fetchAssetClusters(),
    staleTime: 30_000,
  })

  // ── Correlations (top by |pearson|) ──────────────────────────────────
  const correlationsQ = useQuery({
    queryKey: ['cross-asset', 'correlations', profile],
    queryFn: () => fetchAssetCorrelations({ minAbsPearson: 0.3, limit: 200 }),
    staleTime: 30_000,
  })

  // ── Cross-event regressions (for cascade picker) ──────────────────────
  const regressionsQ = useQuery({
    queryKey: ['cross-asset', 'regressions', profile],
    queryFn: () => fetchCrossEventRegressions({ minSamples: 5, limit: 500 }),
    staleTime: 30_000,
  })

  // Driver picker derives from the regressions list.
  const drivers = useMemo(() => {
    const rows = regressionsQ.data?.rows ?? []
    const pairs = new Set<string>()
    rows.forEach(r => pairs.add(`${r.driver_symbol}::${r.driver_event_type}`))
    return Array.from(pairs).sort()
  }, [regressionsQ.data])

  const [selectedDriver, setSelectedDriver] = useState<string>('')
  const [horizonDays, setHorizonDays] = useState<number>(5)

  const cascadeQ = useQuery({
    queryKey: ['cross-asset', 'cascade', profile, selectedDriver, horizonDays],
    queryFn: () => {
      const [driverSymbol, driverEventType] = selectedDriver.split('::')
      return fetchCascade({
        driverSymbol, driverEventType,
        horizonDays, minR2: 0.0, minSamples: 5,
      })
    },
    enabled: tab === 'cascade' && Boolean(selectedDriver),
    staleTime: 30_000,
  })

  // ── Filtered views ────────────────────────────────────────────────────
  const filteredClusters = useMemo<AssetCluster[]>(() => {
    const all = clustersQ.data?.clusters ?? []
    if (!search) return all
    const needle = search.toUpperCase()
    return all.filter(c => c.members.some(m => m.toUpperCase().includes(needle))
                        || (c.label ?? '').toUpperCase().includes(needle))
  }, [clustersQ.data, search])

  const filteredCorrelations = useMemo<AssetCorrelationRow[]>(() => {
    const all = correlationsQ.data?.rows ?? []
    if (!search) return all
    const needle = search.toUpperCase()
    return all.filter(r => r.base_symbol.toUpperCase().includes(needle)
                        || r.peer_symbol.toUpperCase().includes(needle))
  }, [correlationsQ.data, search])

  const isLoading = clustersQ.isLoading || correlationsQ.isLoading

  return (
    <PageTransition>
      <div className="flex flex-col gap-4 p-4">
        {/* Header */}
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Network size={20} className="text-indigo-400" />
            <h1 className="text-lg font-semibold text-gray-100">Cross-Asset Analytics</h1>
          </div>
          <button
            onClick={() => {
              clustersQ.refetch()
              correlationsQ.refetch()
              regressionsQ.refetch()
              if (selectedDriver) cascadeQ.refetch()
            }}
            className="flex items-center gap-1 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-gray-300 hover:bg-gray-800"
          >
            <RefreshCw size={12} /> Refresh
          </button>
        </div>

        {/* Search */}
        <div className="flex items-center gap-2">
          <Search size={14} className="text-gray-500" />
          <input
            type="text"
            placeholder="Filter by symbol or label…"
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="w-64 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-sm text-gray-200 placeholder-gray-500"
          />
          <span className="text-xs text-gray-500">
            Profile: <strong className="text-gray-300">{profile}</strong>
          </span>
        </div>

        {/* Tabs */}
        <div className="flex gap-1 border-b border-gray-800">
          {(['clusters', 'correlations', 'cascade'] as const).map(t => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`flex items-center gap-1 px-3 py-2 text-xs font-medium transition-colors ${
                tab === t
                  ? 'border-b-2 border-indigo-500 text-indigo-300'
                  : 'text-gray-500 hover:text-gray-300'
              }`}
            >
              {t === 'clusters' && <Layers size={12} />}
              {t === 'correlations' && <Activity size={12} />}
              {t === 'cascade' && <ArrowRight size={12} />}
              {t.charAt(0).toUpperCase() + t.slice(1)}
            </button>
          ))}
        </div>

        {/* Body */}
        {isLoading && <SkeletonBlock />}

        {!isLoading && tab === 'clusters' && (
          <ClustersTab clusters={filteredClusters} />
        )}

        {!isLoading && tab === 'correlations' && (
          <CorrelationsTab rows={filteredCorrelations} />
        )}

        {tab === 'cascade' && (
          <CascadeTab
            drivers={drivers}
            selectedDriver={selectedDriver}
            onDriverChange={setSelectedDriver}
            horizonDays={horizonDays}
            onHorizonChange={setHorizonDays}
            data={cascadeQ.data?.predictions ?? []}
            isLoading={cascadeQ.isLoading}
          />
        )}
      </div>
    </PageTransition>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Clusters
// ───────────────────────────────────────────────────────────────────────────

function ClustersTab({ clusters }: { clusters: AssetCluster[] }) {
  if (clusters.length === 0) {
    return (
      <EmptyState
        icon={<Layers size={28} className="text-gray-600" />}
        title="No clusters yet"
        description="Run the CrossAssetAnalyticsWorkflow (cron 02:15 UTC) or wait for tonight's snapshot."
      />
    )
  }
  return (
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-2 xl:grid-cols-3">
      {clusters.map(c => (
        <div key={c.cluster_id}
             className="rounded border border-gray-800 bg-gray-900/60 p-3">
          <div className="mb-2 flex items-center justify-between">
            <div className="text-xs text-gray-500">Cluster #{c.cluster_id}</div>
            <div className="text-xs text-gray-400">
              cohesion <span className={pearsonClass(c.cohesion)}>
                {fmtNum(c.cohesion, 3)}
              </span>
            </div>
          </div>
          <div className="mb-2 text-sm font-medium text-gray-200 truncate">
            {c.label ?? '(unlabeled)'}
          </div>
          <div className="flex flex-wrap gap-1">
            {c.members.map(m => (
              <span key={m}
                    className="rounded border border-indigo-800/40 bg-indigo-900/30 px-1.5 py-0.5 text-[10px] text-indigo-200">
                {m}
              </span>
            ))}
          </div>
          <div className="mt-2 text-[10px] text-gray-600">
            {c.members.length} members · {c.computed_at ?? '—'}
          </div>
        </div>
      ))}
    </div>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Correlations
// ───────────────────────────────────────────────────────────────────────────

function CorrelationsTab({ rows }: { rows: AssetCorrelationRow[] }) {
  if (rows.length === 0) {
    return (
      <EmptyState
        icon={<Activity size={28} className="text-gray-600" />}
        title="No correlation rows"
        description="Tonight's correlation matrix has not been computed yet."
      />
    )
  }
  return (
    <div className="overflow-x-auto rounded border border-gray-800">
      <table className="min-w-full text-xs">
        <thead className="bg-gray-900 text-gray-400">
          <tr>
            <th className="px-2 py-1 text-left">Pair</th>
            <th className="px-2 py-1 text-right">Pearson</th>
            <th className="px-2 py-1 text-right">Spearman</th>
            <th className="px-2 py-1 text-right">Lead-lag (d)</th>
            <th className="px-2 py-1 text-right">Lead-lag r</th>
            <th className="px-2 py-1 text-right">N</th>
            <th className="px-2 py-1 text-right">Window</th>
          </tr>
        </thead>
        <tbody className="bg-gray-950 text-gray-200">
          {rows.map(r => (
            <tr key={`${r.base_symbol}-${r.peer_symbol}-${r.window_days}`}
                className="border-t border-gray-800">
              <td className="px-2 py-1">
                <span className="text-indigo-300">{r.base_symbol}</span>
                <span className="mx-1 text-gray-600">↔</span>
                <span className="text-indigo-300">{r.peer_symbol}</span>
              </td>
              <td className={`px-2 py-1 text-right ${pearsonClass(r.pearson)}`}>
                {fmtNum(r.pearson, 3)}
              </td>
              <td className="px-2 py-1 text-right text-gray-400">{fmtNum(r.spearman, 3)}</td>
              <td className="px-2 py-1 text-right text-gray-400">{r.lead_lag_days}</td>
              <td className={`px-2 py-1 text-right ${pearsonClass(r.lead_lag_score)}`}>
                {fmtNum(r.lead_lag_score, 3)}
              </td>
              <td className="px-2 py-1 text-right text-gray-500">{r.sample_count}</td>
              <td className="px-2 py-1 text-right text-gray-500">{r.window_days}d</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Cascade
// ───────────────────────────────────────────────────────────────────────────

interface CascadeTabProps {
  drivers: string[]
  selectedDriver: string
  onDriverChange: (s: string) => void
  horizonDays: number
  onHorizonChange: (n: number) => void
  data: CascadePrediction[]
  isLoading: boolean
}

function CascadeTab({
  drivers, selectedDriver, onDriverChange,
  horizonDays, onHorizonChange, data, isLoading,
}: CascadeTabProps) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-gray-400">Driver event:</span>
        <select
          value={selectedDriver}
          onChange={e => onDriverChange(e.target.value)}
          className="rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-gray-200"
        >
          <option value="">— pick a driver/event —</option>
          {drivers.map(d => {
            const [sym, et] = d.split('::')
            return <option key={d} value={d}>{sym} · {et}</option>
          })}
        </select>
        <span className="ml-2 text-xs text-gray-400">Horizon:</span>
        {[1, 5, 20].map(h => (
          <button
            key={h}
            onClick={() => onHorizonChange(h)}
            className={`rounded border px-2 py-1 text-xs ${
              horizonDays === h
                ? 'border-indigo-500 bg-indigo-900/40 text-indigo-200'
                : 'border-gray-700 bg-gray-900 text-gray-400 hover:bg-gray-800'
            }`}
          >
            {h}d
          </button>
        ))}
      </div>

      {!selectedDriver && (
        <EmptyState
          icon={<ArrowRight size={28} className="text-gray-600" />}
          title="Pick a driver event to see predicted reactions"
          description="The cascade view shows persisted cross-event regression predictions per related symbol."
        />
      )}

      {selectedDriver && isLoading && <SkeletonBlock />}

      {selectedDriver && !isLoading && data.length === 0 && (
        <EmptyState
          icon={<AlertCircle size={28} className="text-gray-600" />}
          title="No predictions for this driver/horizon"
          description="No (target, horizon) pairs survived the min_samples filter."
        />
      )}

      {selectedDriver && !isLoading && data.length > 0 && (
        <div className="overflow-x-auto rounded border border-gray-800">
          <table className="min-w-full text-xs">
            <thead className="bg-gray-900 text-gray-400">
              <tr>
                <th className="px-2 py-1 text-left">Target</th>
                <th className="px-2 py-1 text-right">Beta</th>
                <th className="px-2 py-1 text-right">R²</th>
                <th className="px-2 py-1 text-right">Expected drift</th>
                <th className="px-2 py-1 text-right">Hit rate</th>
                <th className="px-2 py-1 text-right">Mean fwd ret</th>
                <th className="px-2 py-1 text-right">N</th>
              </tr>
            </thead>
            <tbody className="bg-gray-950 text-gray-200">
              {data.map(p => (
                <tr key={`${p.target_symbol}-${p.horizon_days}`}
                    className="border-t border-gray-800">
                  <td className="px-2 py-1 text-indigo-300">{p.target_symbol}</td>
                  <td className="px-2 py-1 text-right">{fmtNum(p.beta, 3)}</td>
                  <td className="px-2 py-1 text-right text-gray-400">{fmtNum(p.r_squared, 3)}</td>
                  <td className={`px-2 py-1 text-right ${
                    (p.expected_drift ?? 0) > 0
                      ? 'text-emerald-400'
                      : (p.expected_drift ?? 0) < 0 ? 'text-rose-400' : 'text-gray-500'
                  }`}>
                    {fmtPct(p.expected_drift, 2)}
                  </td>
                  <td className="px-2 py-1 text-right text-gray-400">{fmtPct(p.hit_rate, 1)}</td>
                  <td className="px-2 py-1 text-right text-gray-400">{fmtPct(p.mean_forward_return, 2)}</td>
                  <td className="px-2 py-1 text-right text-gray-500">{p.sample_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
