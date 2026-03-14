/**
 * Backtesting — Historical strategy performance analysis.
 * Shows past backtest runs, WFO optimization history, parameter promotions,
 * and allows on-demand backtest triggering with live WebSocket progress.
 * Separated by asset class (Crypto / Equity) via profile.
 */
import { useState, useMemo, useRef, useCallback, useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import {
  ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, CartesianGrid,
  ReferenceLine, ComposedChart, Line, Bar, BarChart, Cell,
} from 'recharts'
import {
  TrendingUp, BarChart2, Zap, Search,
  ArrowUpRight, Play, ChevronLeft,
  Target, Shield, Bot, User, Settings, X, Loader, Radar, Trash2,
} from 'lucide-react'
import {
  fetchBacktestHistory, fetchBacktestRun, fetchWFOHistory,
  fetchBacktestPairs, openBacktestSocket, deleteBacktestRun,
  fetchBacktestInterpretation,
  type BacktestRunDetail, type BacktestPairInfo, type BacktestProgressEvent,
} from '../api'
import StatCard from '../components/StatCard'
import { SkeletonStatCards, SkeletonBlock } from '../components/Skeleton'
import EmptyState from '../components/EmptyState'
import PageTransition from '../components/PageTransition'
import { useLiveStore } from '../store'

dayjs.extend(relativeTime)

const TIME_RANGES = [
  { label: '30d', days: 30 },
  { label: '90d', days: 90 },
  { label: '1y', days: 365 },
]

// ─── Helpers ────────────────────────────────────────────────────────────────

function fmtPct(v: number | null | undefined): string {
  if (v == null) return '—'
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
}

function fmtNum(v: number | null | undefined, decimals = 2): string {
  if (v == null) return '—'
  return v.toFixed(decimals)
}

function fmtCurrency(v: number | null | undefined): string {
  if (v == null) return '—'
  return `$${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

const EXIT_REASON_LABELS: Record<string, { label: string; color: string }> = {
  stop_loss: { label: 'Stop Loss', color: '#ef4444' },
  trailing_stop: { label: 'Trailing Stop', color: '#f59e0b' },
  take_profit: { label: 'Take Profit', color: '#22c55e' },
  backtest_end: { label: 'End of Period', color: '#6b7280' },
}

const SOURCE_BADGE: Record<string, { label: string; icon: typeof Bot; color: string }> = {
  config: { label: 'Config', icon: Settings, color: '#6b7280' },
  human: { label: 'You', icon: User, color: '#3b82f6' },
  llm: { label: 'AI', icon: Bot, color: '#8b5cf6' },
  scan: { label: 'Scan', icon: Radar, color: '#f59e0b' },
}

// ─── Main Component ────────────────────────────────────────────────────────

export default function Backtesting() {
  const profile = useLiveStore((s) => s.profile)
  const queryClient = useQueryClient()
  const [days, setDays] = useState(90)
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null)
  const [pairFilter, setPairFilter] = useState('')
  const [showTrigger, setShowTrigger] = useState(false)
  const [triggerPair, setTriggerPair] = useState('')

  // ── Data queries ──────────────────────────────────────────────────────

  const historyQ = useQuery({
    queryKey: ['backtest-history', days, pairFilter, profile],
    queryFn: () => fetchBacktestHistory(days, pairFilter),
    refetchInterval: 60_000,
  })

  const wfoQ = useQuery({
    queryKey: ['backtest-wfo', days, pairFilter, profile],
    queryFn: () => fetchWFOHistory(days, pairFilter),
  })

  const pairsQ = useQuery({
    queryKey: ['backtest-pairs', profile],
    queryFn: fetchBacktestPairs,
    staleTime: 60_000,
  })

  const detailQ = useQuery({
    queryKey: ['backtest-run', selectedRunId, profile],
    queryFn: () => fetchBacktestRun(selectedRunId!),
    enabled: selectedRunId != null,
  })

  const runs = historyQ.data?.runs ?? []
  const wfoRuns = wfoQ.data?.runs ?? []
  const followedPairs = pairsQ.data?.pairs ?? []

  // ── Aggregate stats ───────────────────────────────────────────────────

  const stats = useMemo(() => {
    if (!runs.length) return null
    const avgReturn = runs.reduce((s, r) => s + r.total_return_pct, 0) / runs.length
    const bestSharpe = Math.max(...runs.map(r => r.sharpe_ratio))
    const avgWinRate = runs.reduce((s, r) => s + r.win_rate, 0) / runs.length
    const avgDrawdown = runs.reduce((s, r) => s + r.max_drawdown_pct, 0) / runs.length
    return { total: runs.length, avgReturn, bestSharpe, avgWinRate, avgDrawdown }
  }, [runs])

  // ── Unique pairs for filter (from actual runs) ────────────────────────

  const uniquePairs = useMemo(() => {
    const pairs = new Set(runs.map(r => r.pair))
    return Array.from(pairs).sort()
  }, [runs])

  // ── Handlers ──────────────────────────────────────────────────────────

  const handleRunBacktest = useCallback((pair: string) => {
    setTriggerPair(pair)
    setShowTrigger(true)
  }, [])

  const handleBacktestComplete = useCallback((runId: number | null) => {
    setShowTrigger(false)
    setTriggerPair('')
    queryClient.invalidateQueries({ queryKey: ['backtest-history', days, pairFilter, profile] })
    queryClient.invalidateQueries({ queryKey: ['backtest-pairs', profile] })
    if (runId) setSelectedRunId(runId)
  }, [queryClient, days, pairFilter, profile])

  const handleDeleteRun = useCallback(async (runId: number, e?: React.MouseEvent) => {
    e?.stopPropagation()
    try {
      await deleteBacktestRun(runId)
      if (selectedRunId === runId) setSelectedRunId(null)
      queryClient.invalidateQueries({ queryKey: ['backtest-history', days, pairFilter, profile] })
      queryClient.invalidateQueries({ queryKey: ['backtest-pairs', profile] })
    } catch { /* toast? */ }
  }, [queryClient, days, pairFilter, profile, selectedRunId])

  // ── Render ────────────────────────────────────────────────────────────

  if (selectedRunId != null) {
    return (
      <PageTransition>
        <RunDetailView
          detail={detailQ.data ?? null}
          isLoading={detailQ.isLoading}
          onBack={() => setSelectedRunId(null)}
          onDelete={() => handleDeleteRun(selectedRunId)}
        />
      </PageTransition>
    )
  }

  return (
    <PageTransition>
      <div className="p-6" style={{ display: 'flex', flexDirection: 'column', gap: 24 }}>

        {/* ── Controls Bar ─────────────────────────────────────── */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12 }}>
          <div style={{ display: 'flex', gap: 4 }}>
            {TIME_RANGES.map(tr => (
              <button
                key={tr.days}
                onClick={() => setDays(tr.days)}
                style={{
                  padding: '4px 10px', borderRadius: 6, border: 'none', cursor: 'pointer',
                  fontSize: 12, fontWeight: 500,
                  background: days === tr.days ? 'var(--accent, #3b82f6)' : 'rgba(255,255,255,0.06)',
                  color: days === tr.days ? '#fff' : 'rgba(255,255,255,0.5)',
                }}
              >
                {tr.label}
              </button>
            ))}
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <select
              value={pairFilter}
              onChange={e => setPairFilter(e.target.value)}
              style={{
                padding: '6px 10px', borderRadius: 6, border: '1px solid rgba(255,255,255,0.1)',
                background: 'rgba(255,255,255,0.06)', color: '#fff', fontSize: 12,
              }}
            >
              <option value="">All Pairs</option>
              {uniquePairs.map(p => <option key={p} value={p}>{p}</option>)}
            </select>
            <button
              onClick={() => setShowTrigger(true)}
              style={{
                padding: '6px 14px', borderRadius: 6, border: 'none', cursor: 'pointer',
                background: 'var(--accent, #3b82f6)', color: '#fff', fontSize: 12, fontWeight: 600,
                display: 'flex', alignItems: 'center', gap: 4,
              }}
            >
              <Play size={14} /> Run Backtest
            </button>
          </div>
        </div>

        {/* ── Followed Pairs Quick-Select ─────────────────────── */}
        <PairQuickSelect
          pairs={followedPairs}
          isLoading={pairsQ.isLoading}
          onSelect={handleRunBacktest}
        />

        {/* ── Stat Cards ──────────────────────────────────────── */}
        {historyQ.isLoading ? (
          <SkeletonStatCards count={6} />
        ) : stats ? (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 12 }}>
            <StatCard label="Total Runs" value={stats.total} icon={<BarChart2 size={16} />} />
            <StatCard label="Avg Return" value={fmtPct(stats.avgReturn)} icon={<TrendingUp size={16} />}
              accent={stats.avgReturn >= 0 ? 'green' : 'red'} />
            <StatCard label="Best Sharpe" value={fmtNum(stats.bestSharpe, 3)} icon={<Zap size={16} />}
              accent={stats.bestSharpe >= 1 ? 'green' : 'blue'} />
            <StatCard label="Avg Win Rate" value={fmtPct(stats.avgWinRate)} icon={<Target size={16} />}
              accent={stats.avgWinRate >= 50 ? 'green' : 'red'} />
            <StatCard label="Avg Drawdown" value={fmtPct(stats.avgDrawdown)} icon={<Shield size={16} />}
              accent={stats.avgDrawdown > -10 ? 'blue' : 'red'} />
          </div>
        ) : (
          <EmptyState icon="chart" title="No backtest runs yet" description="Select a pair above or click 'Run Backtest' to get started." />
        )}

        {/* ── Runs Table ──────────────────────────────────────── */}
        {historyQ.isLoading ? (
          <SkeletonBlock className="h-72" />
        ) : runs.length > 0 ? (
          <div style={{ background: 'rgba(255,255,255,0.03)', borderRadius: 12, border: '1px solid rgba(255,255,255,0.06)', overflow: 'hidden' }}>
            <div style={{ padding: '12px 16px', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
              <h3 style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>Runs</h3>
            </div>
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                <thead>
                  <tr style={{ borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
                    {['Pair', 'Date', 'Days', 'Return', 'Sharpe', 'Win Rate', 'Trades', 'Max DD', 'Alpha', 'WFO', ''].map(h => (
                      <th key={h || '_del'} style={{ padding: '8px 12px', textAlign: 'left', fontSize: 11, color: 'rgba(255,255,255,0.4)', fontWeight: 500 }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {runs.map(r => (
                    <tr
                      key={r.id}
                      onClick={() => setSelectedRunId(r.id)}
                      style={{
                        cursor: 'pointer', borderBottom: '1px solid rgba(255,255,255,0.03)',
                        transition: 'background 0.15s',
                      }}
                      onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.04)')}
                      onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                    >
                      <td style={{ padding: '8px 12px', fontWeight: 600 }}>{r.pair}</td>
                      <td style={{ padding: '8px 12px', color: 'rgba(255,255,255,0.5)' }}>{dayjs(r.run_ts).format('MMM D, HH:mm')}</td>
                      <td style={{ padding: '8px 12px', color: 'rgba(255,255,255,0.5)' }}>{r.days}d</td>
                      <td style={{ padding: '8px 12px', color: r.total_return_pct >= 0 ? '#22c55e' : '#ef4444', fontWeight: 600 }}>{fmtPct(r.total_return_pct)}</td>
                      <td style={{ padding: '8px 12px' }}>{fmtNum(r.sharpe_ratio, 3)}</td>
                      <td style={{ padding: '8px 12px' }}>{fmtPct(r.win_rate)}</td>
                      <td style={{ padding: '8px 12px' }}>{r.total_trades}</td>
                      <td style={{ padding: '8px 12px', color: '#f59e0b' }}>{fmtPct(r.max_drawdown_pct)}</td>
                      <td style={{ padding: '8px 12px', color: r.alpha >= 0 ? '#22c55e' : '#ef4444' }}>{fmtPct(r.alpha)}</td>
                      <td style={{ padding: '8px 12px' }}>
                        {r.is_wfo && (
                          <span style={{
                            padding: '2px 6px', borderRadius: 4, fontSize: 10, fontWeight: 600,
                            background: r.wfo_wfe != null && r.wfo_wfe >= 0.5 ? 'rgba(34,197,94,0.15)' : 'rgba(245,158,11,0.15)',
                            color: r.wfo_wfe != null && r.wfo_wfe >= 0.5 ? '#22c55e' : '#f59e0b',
                          }}>
                            WFE {fmtNum(r.wfo_wfe, 2)}
                          </span>
                        )}
                      </td>
                      <td style={{ padding: '8px 12px' }}>
                        <button
                          onClick={(e) => handleDeleteRun(r.id, e)}
                          title="Delete run"
                          style={{
                            background: 'transparent', border: 'none', cursor: 'pointer',
                            color: 'rgba(255,255,255,0.25)', padding: 4, borderRadius: 4, display: 'flex',
                          }}
                          onMouseEnter={e => (e.currentTarget.style.color = '#ef4444')}
                          onMouseLeave={e => (e.currentTarget.style.color = 'rgba(255,255,255,0.25)')}
                        >
                          <Trash2 size={14} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}

        {/* ── WFO Timeline Chart ──────────────────────────────── */}
        {wfoRuns.length > 0 && (
          <div style={{ background: 'rgba(255,255,255,0.03)', borderRadius: 12, border: '1px solid rgba(255,255,255,0.06)', padding: 16 }}>
            <h3 style={{ margin: '0 0 12px', fontSize: 14, fontWeight: 600 }}>Walk-Forward Efficiency Over Time</h3>
            <ResponsiveContainer width="100%" height={250}>
              <AreaChart data={wfoRuns.slice().reverse().map(r => ({
                date: dayjs(r.run_ts).format('MMM D'),
                pair: r.pair,
                wfe: r.wfo_wfe ?? 0,
                sharpe: r.sharpe_ratio,
              }))}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" />
                <XAxis dataKey="date" tick={{ fontSize: 11, fill: 'rgba(255,255,255,0.4)' }} />
                <YAxis tick={{ fontSize: 11, fill: 'rgba(255,255,255,0.4)' }} domain={[0, 'auto']} />
                <Tooltip
                  contentStyle={{ background: '#1a1a2e', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 8, fontSize: 12 }}
                  labelStyle={{ color: 'rgba(255,255,255,0.6)' }}
                />
                <ReferenceLine y={0.5} stroke="#f59e0b" strokeDasharray="5 5" label={{ value: 'Robust (0.5)', fill: '#f59e0b', fontSize: 10 }} />
                <Area type="monotone" dataKey="wfe" stroke="#8b5cf6" fill="rgba(139,92,246,0.15)" name="WFE" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        )}



      </div>

      {/* ── Trigger Modal ───────────────────────────────────── */}
      {showTrigger && (
        <TriggerPanel
          initialPair={triggerPair}
          followedPairs={followedPairs}
          onClose={() => { setShowTrigger(false); setTriggerPair('') }}
          onComplete={handleBacktestComplete}
        />
      )}
    </PageTransition>
  )
}


// ═══════════════════════════════════════════════════════════════════════════
// Pair Quick-Select — horizontal cards of followed pairs
// ═══════════════════════════════════════════════════════════════════════════

function PairQuickSelect({ pairs, isLoading, onSelect }: {
  pairs: BacktestPairInfo[]
  isLoading: boolean
  onSelect: (pair: string) => void
}) {
  if (isLoading) return <SkeletonBlock className="h-20" />
  if (!pairs.length) return null

  return (
    <div style={{ background: 'rgba(255,255,255,0.03)', borderRadius: 12, border: '1px solid rgba(255,255,255,0.06)', padding: '12px 16px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
        <Target size={14} style={{ color: 'rgba(255,255,255,0.4)' }} />
        <span style={{ fontSize: 12, fontWeight: 600, color: 'rgba(255,255,255,0.5)' }}>Your Pairs — click to backtest</span>
      </div>
      <div style={{ display: 'flex', gap: 8, overflowX: 'auto', paddingBottom: 4 }}>
        {pairs.map(p => {
          const badge = SOURCE_BADGE[p.source] ?? SOURCE_BADGE.config
          const Icon = badge.icon
          return (
            <button
              key={p.pair}
              onClick={() => onSelect(p.pair)}
              style={{
                flex: '0 0 auto',
                padding: '8px 14px', borderRadius: 10,
                border: '1px solid rgba(255,255,255,0.08)',
                background: 'rgba(255,255,255,0.04)',
                color: '#fff', cursor: 'pointer',
                display: 'flex', flexDirection: 'column', gap: 4,
                minWidth: 120, transition: 'all 0.15s',
              }}
              onMouseEnter={e => {
                e.currentTarget.style.background = 'rgba(255,255,255,0.08)'
                e.currentTarget.style.borderColor = badge.color
              }}
              onMouseLeave={e => {
                e.currentTarget.style.background = 'rgba(255,255,255,0.04)'
                e.currentTarget.style.borderColor = 'rgba(255,255,255,0.08)'
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, justifyContent: 'space-between' }}>
                <span style={{ fontSize: 13, fontWeight: 700 }}>{p.pair}</span>
                <span style={{
                  display: 'flex', alignItems: 'center', gap: 3,
                  fontSize: 9, fontWeight: 600, color: badge.color,
                  background: `${badge.color}20`, padding: '1px 5px', borderRadius: 4,
                }}>
                  <Icon size={9} /> {badge.label}
                </span>
              </div>
              {p.last_run_ts ? (
                <div style={{ display: 'flex', gap: 8, fontSize: 10, color: 'rgba(255,255,255,0.4)' }}>
                  <span style={{ color: (p.last_return_pct ?? 0) >= 0 ? '#22c55e' : '#ef4444' }}>
                    {fmtPct(p.last_return_pct)}
                  </span>
                  <span>S: {fmtNum(p.last_sharpe, 2)}</span>
                  <span>{dayjs(p.last_run_ts).fromNow()}</span>
                </div>
              ) : (
                <span style={{ fontSize: 10, color: 'rgba(255,255,255,0.25)' }}>No runs yet</span>
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}


// ═══════════════════════════════════════════════════════════════════════════
// Run Detail View
// ═══════════════════════════════════════════════════════════════════════════

function RunDetailView({ detail, isLoading, onBack, onDelete }: {
  detail: BacktestRunDetail | null
  isLoading: boolean
  onBack: () => void
  onDelete: () => void
}) {
  const profile = useLiveStore((s) => s.profile)
  const [showInterpretation, setShowInterpretation] = useState(false)
  const interpQ = useQuery({
    queryKey: ['backtest-interpretation', detail?.id, profile],
    queryFn: () => fetchBacktestInterpretation(detail!.id),
    enabled: showInterpretation && detail != null,
    staleTime: 5 * 60 * 1000, // cache for 5 minutes
    retry: 1,
  })

  if (isLoading) return <SkeletonBlock className="h-96" />
  if (!detail) return <EmptyState icon="chart" title="Run not found" />

  const r = detail.result_json
  const equityCurve = r.equity_curve ?? []
  const trades = r.trades ?? []
  const costSens = r.cost_sensitivity ?? []

  return (
    <div className="p-6" style={{ display: 'flex', flexDirection: 'column', gap: 24 }}>

      {/* ── Header ──────────────────────────────────────────── */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <button
          onClick={onBack}
          style={{
            padding: '6px 10px', borderRadius: 6, border: '1px solid rgba(255,255,255,0.1)',
            background: 'transparent', color: '#fff', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 4,
          }}
        >
          <ChevronLeft size={14} /> Back
        </button>
        <h2 style={{ margin: 0, fontSize: 20, fontWeight: 600 }}>{detail.pair}</h2>
        <span style={{ color: 'rgba(255,255,255,0.4)', fontSize: 13 }}>
          {dayjs(detail.run_ts).format('MMM D, YYYY HH:mm')} &middot; {detail.days}d
        </span>
        {detail.is_wfo && detail.wfo_wfe != null && (
          <span style={{
            padding: '2px 8px', borderRadius: 4, fontSize: 11, fontWeight: 600,
            background: detail.wfo_wfe >= 0.5 ? 'rgba(34,197,94,0.15)' : 'rgba(245,158,11,0.15)',
            color: detail.wfo_wfe >= 0.5 ? '#22c55e' : '#f59e0b',
          }}>
            WFO &middot; WFE {detail.wfo_wfe.toFixed(2)}
          </span>
        )}
        <div style={{ marginLeft: 'auto' }}>
          <button
            onClick={onDelete}
            title="Delete this backtest run"
            style={{
              padding: '6px 10px', borderRadius: 6, border: '1px solid rgba(239,68,68,0.3)',
              background: 'rgba(239,68,68,0.08)', color: '#ef4444', cursor: 'pointer',
              display: 'flex', alignItems: 'center', gap: 4, fontSize: 13,
            }}
          >
            <Trash2 size={14} /> Delete
          </button>
        </div>
      </div>

      {/* ── Key Metrics Row ─────────────────────────────────── */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 10 }}>
        <MetricCard label="Return" value={fmtPct(r.total_return_pct)} color={r.total_return_pct >= 0 ? '#22c55e' : '#ef4444'} />
        <MetricCard label="Sim. P&L" value={fmtCurrency(r.final_balance - r.initial_balance)} color={(r.final_balance - r.initial_balance) >= 0 ? '#22c55e' : '#ef4444'} />
        <MetricCard label="Sharpe" value={fmtNum(r.sharpe_ratio, 3)} color={r.sharpe_ratio >= 1 ? '#22c55e' : '#f59e0b'} />
        <MetricCard label="Sortino" value={fmtNum(r.sortino_ratio, 3)} color={r.sortino_ratio >= 1 ? '#22c55e' : '#f59e0b'} />
        <MetricCard label="Calmar" value={fmtNum(r.calmar_ratio, 3)} color={r.calmar_ratio >= 1 ? '#22c55e' : '#f59e0b'} />
        <MetricCard label="Profit Factor" value={fmtNum(r.profit_factor, 2)} color={r.profit_factor >= 1 ? '#22c55e' : '#ef4444'} />
        <MetricCard label="Win Rate" value={fmtPct(r.win_rate)} color={r.win_rate >= 50 ? '#22c55e' : '#ef4444'} />
        <MetricCard label="Max Drawdown" value={fmtPct(r.max_drawdown_pct)} color="#f59e0b" />
        <MetricCard label="Alpha" value={fmtPct(r.alpha)} color={r.alpha >= 0 ? '#22c55e' : '#ef4444'} />
        <MetricCard label="Buy & Hold" value={fmtPct(r.benchmark_return_pct)} color="rgba(255,255,255,0.6)" />
      </div>

      {/* ── Equity Curve Chart ──────────────────────────────── */}
      {equityCurve.length > 0 && (
        <div style={{ background: 'rgba(255,255,255,0.03)', borderRadius: 12, border: '1px solid rgba(255,255,255,0.06)', padding: 16 }}>
          <h3 style={{ margin: '0 0 12px', fontSize: 14, fontWeight: 600 }}>Equity Curve</h3>
          <ResponsiveContainer width="100%" height={300}>
            <ComposedChart data={equityCurve.map((p, i) => {
              // Candle timestamps are Unix seconds (string or number); dayjs expects ms
              const raw = p.time
              const ts = raw ? Number(raw) * 1000 : 0
              return {
                idx: i,
                time: ts ? dayjs(ts).format(detail.days <= 2 ? 'MMM D HH:mm' : 'MMM D') : `${i}`,
                equity: p.equity,
                drawdown: Math.abs(p.drawdown ?? 0) * 100,
                benchmark: r.initial_balance * (1 + (r.benchmark_return_pct / 100) * (i / Math.max(equityCurve.length - 1, 1))),
              }
            })}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" />
              <XAxis dataKey="time" tick={{ fontSize: 10, fill: 'rgba(255,255,255,0.4)' }} interval={equityCurve.length > Math.min(detail.days, 12) ? Math.floor(equityCurve.length / Math.min(detail.days, 12)) - 1 : 0} />
              <YAxis yAxisId="eq" tick={{ fontSize: 10, fill: 'rgba(255,255,255,0.4)' }} domain={['auto', 'auto']} />
              <YAxis yAxisId="dd" orientation="right" tick={{ fontSize: 10, fill: 'rgba(255,255,255,0.2)' }} domain={[0, 'auto']} hide />
              <Tooltip
                contentStyle={{ background: '#1a1a2e', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 8, fontSize: 12 }}
                formatter={((value: number | undefined, name: string | undefined) => {
                  if (value == null) return ['—', name ?? '']
                  if (name === 'Drawdown') return [`${value.toFixed(2)}%`, name]
                  return [fmtCurrency(value), name ?? '']
                }) as any}
              />
              <Area yAxisId="dd" type="monotone" dataKey="drawdown" stroke="none" fill="rgba(239,68,68,0.10)" name="Drawdown" />
              <Line yAxisId="eq" type="monotone" dataKey="benchmark" stroke="rgba(255,255,255,0.15)" strokeDasharray="5 5" dot={false} name="Buy & Hold" />
              <Line yAxisId="eq" type="monotone" dataKey="equity" stroke="#3b82f6" dot={false} strokeWidth={2} name="Strategy" />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* ── Trade Statistics ────────────────────────────────── */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 12 }}>
        <div style={{ background: 'rgba(255,255,255,0.03)', borderRadius: 12, border: '1px solid rgba(255,255,255,0.06)', padding: 16 }}>
          <h4 style={{ margin: '0 0 10px', fontSize: 13, fontWeight: 600, color: 'rgba(255,255,255,0.6)' }}>Trade Stats</h4>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, fontSize: 13 }}>
            <StatRow label="Total Trades" value={r.total_trades} />
            <StatRow label="Winners" value={r.winning_trades} color="#22c55e" />
            <StatRow label="Losers" value={r.losing_trades} color="#ef4444" />
            <StatRow label="Avg Win" value={fmtCurrency(r.avg_win)} color="#22c55e" />
            <StatRow label="Avg Loss" value={fmtCurrency(r.avg_loss)} color="#ef4444" />
            <StatRow label="Largest Win" value={fmtCurrency(r.largest_win)} color="#22c55e" />
            <StatRow label="Largest Loss" value={fmtCurrency(r.largest_loss)} color="#ef4444" />
            <StatRow label="Avg Hold" value={`${fmtNum(r.avg_hold_time_hours, 1)}h`} />
          </div>
        </div>

        {/* ── Exit Reason Breakdown ─────────────────────────── */}
        <div style={{ background: 'rgba(255,255,255,0.03)', borderRadius: 12, border: '1px solid rgba(255,255,255,0.06)', padding: 16 }}>
          <h4 style={{ margin: '0 0 10px', fontSize: 13, fontWeight: 600, color: 'rgba(255,255,255,0.6)' }}>Exit Reasons</h4>
          <ExitReasonChart trades={trades} />
        </div>

        {/* ── Parameters Used ───────────────────────────────── */}
        {detail.params_json && (
          <div style={{ background: 'rgba(255,255,255,0.03)', borderRadius: 12, border: '1px solid rgba(255,255,255,0.06)', padding: 16 }}>
            <h4 style={{ margin: '0 0 10px', fontSize: 13, fontWeight: 600, color: 'rgba(255,255,255,0.6)' }}>Parameters</h4>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, fontSize: 13 }}>
              <StatRow label="Position Size" value={`${(detail.params_json.position_size_pct * 100).toFixed(0)}%`} />
              <StatRow label="Trailing Stop" value={`${(detail.params_json.trailing_stop_pct * 100).toFixed(1)}%`} />
              <StatRow label="Entry Threshold" value={fmtNum(detail.params_json.entry_threshold, 2)} />
              <StatRow label="Fee" value={`${(detail.params_json.fee_pct * 100).toFixed(2)}%`} />
              <StatRow label="Slippage" value={`${(detail.params_json.slippage_pct * 100).toFixed(2)}%`} />
            </div>
          </div>
        )}
      </div>

      {/* ── Cost Sensitivity Heatmap ────────────────────────── */}
      {costSens.length > 0 && (
        <div style={{ background: 'rgba(255,255,255,0.03)', borderRadius: 12, border: '1px solid rgba(255,255,255,0.06)', padding: 16 }}>
          <h3 style={{ margin: '0 0 12px', fontSize: 14, fontWeight: 600 }}>Cost Sensitivity</h3>
          <CostSensitivityGrid data={costSens} />
        </div>
      )}

      {/* ── AI Analysis ─────────────────────────────────────── */}
      <div style={{ background: 'rgba(139,92,246,0.04)', borderRadius: 12, border: '1px solid rgba(139,92,246,0.15)', padding: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: showInterpretation && interpQ.data ? 12 : 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Bot size={16} style={{ color: '#8b5cf6' }} />
            <h3 style={{ margin: 0, fontSize: 14, fontWeight: 600, color: '#c4b5fd' }}>AI Analysis</h3>
          </div>
          {!showInterpretation && (
            <button
              onClick={() => setShowInterpretation(true)}
              style={{
                padding: '6px 14px', borderRadius: 6, border: '1px solid rgba(139,92,246,0.3)',
                background: 'rgba(139,92,246,0.1)', color: '#a78bfa', cursor: 'pointer',
                fontSize: 12, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 6,
              }}
            >
              <Zap size={12} /> Interpret Results
            </button>
          )}
        </div>
        {showInterpretation && interpQ.isLoading && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'rgba(255,255,255,0.4)', fontSize: 13, padding: '12px 0' }}>
            <Loader size={14} className="animate-spin" /> Generating analysis...
          </div>
        )}
        {showInterpretation && interpQ.isError && (
          <div style={{ color: '#ef4444', fontSize: 13, padding: '8px 0' }}>
            Failed to generate analysis. <button onClick={() => interpQ.refetch()} style={{ color: '#a78bfa', background: 'none', border: 'none', cursor: 'pointer', textDecoration: 'underline', fontSize: 13 }}>Retry</button>
          </div>
        )}
        {showInterpretation && interpQ.data && (
          <SimpleMarkdown text={interpQ.data.interpretation} />
        )}
      </div>

      {/* ── Trade Log ───────────────────────────────────────── */}
      {trades.length > 0 && (
        <div style={{ background: 'rgba(255,255,255,0.03)', borderRadius: 12, border: '1px solid rgba(255,255,255,0.06)', overflow: 'hidden' }}>
          <div style={{ padding: '12px 16px', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
            <h3 style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>Trade Log ({trades.length} trades)</h3>
          </div>
          <div style={{ overflowX: 'auto', maxHeight: 400 }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead>
                <tr style={{ borderBottom: '1px solid rgba(255,255,255,0.06)', position: 'sticky', top: 0, background: '#0f0f1a' }}>
                  {['Entry Time', 'Exit Time', 'Entry Price', 'Exit Price', 'Qty', 'PnL', 'PnL %', 'Exit Reason'].map(h => (
                    <th key={h} style={{ padding: '8px 10px', textAlign: 'left', fontSize: 10, color: 'rgba(255,255,255,0.4)', fontWeight: 500 }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {trades.map((t, i) => {
                  const er = EXIT_REASON_LABELS[t.exit_reason] ?? { label: t.exit_reason, color: '#6b7280' }
                  return (
                    <tr key={i} style={{ borderBottom: '1px solid rgba(255,255,255,0.03)' }}>
                      <td style={{ padding: '6px 10px', color: 'rgba(255,255,255,0.5)' }}>{t.entry_time ? dayjs(Number(t.entry_time) * 1000).format('MMM D HH:mm') : '—'}</td>
                      <td style={{ padding: '6px 10px', color: 'rgba(255,255,255,0.5)' }}>{t.exit_time ? dayjs(Number(t.exit_time) * 1000).format('MMM D HH:mm') : '—'}</td>
                      <td style={{ padding: '6px 10px' }}>{fmtCurrency(t.entry_price)}</td>
                      <td style={{ padding: '6px 10px' }}>{fmtCurrency(t.exit_price)}</td>
                      <td style={{ padding: '6px 10px' }}>{fmtNum(t.quantity, 6)}</td>
                      <td style={{ padding: '6px 10px', color: t.pnl >= 0 ? '#22c55e' : '#ef4444', fontWeight: 600 }}>{fmtCurrency(t.pnl)}</td>
                      <td style={{ padding: '6px 10px', color: t.pnl_pct >= 0 ? '#22c55e' : '#ef4444' }}>{fmtPct(t.pnl_pct)}</td>
                      <td style={{ padding: '6px 10px' }}>
                        <span style={{ padding: '1px 5px', borderRadius: 3, fontSize: 10, fontWeight: 600, background: `${er.color}20`, color: er.color }}>
                          {er.label}
                        </span>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}


// ═══════════════════════════════════════════════════════════════════════════
// Sub-components
// ═══════════════════════════════════════════════════════════════════════════

/** Safe markdown renderer — handles **bold**, *italic*, `code`, - bullet lists, and paragraphs. No dangerouslySetInnerHTML. */
function SimpleMarkdown({ text }: { text: string }) {
  const elements = useMemo(() => {
    const lines = text.split('\n')
    const result: React.ReactNode[] = []
    let listItems: string[] = []

    const flushList = () => {
      if (listItems.length) {
        result.push(
          <ul key={`ul-${result.length}`} style={{ margin: '6px 0', paddingLeft: 20 }}>
            {listItems.map((item, i) => (
              <li key={i} style={{ marginBottom: 3 }}>{inlineFormat(item)}</li>
            ))}
          </ul>
        )
        listItems = []
      }
    }

    for (const line of lines) {
      const trimmed = line.trim()
      if (!trimmed) {
        flushList()
        continue
      }
      // Bullet lists
      if (/^[-*•]\s+/.test(trimmed)) {
        listItems.push(trimmed.replace(/^[-*•]\s+/, ''))
        continue
      }
      // Numbered lists
      if (/^\d+[.)]\s+/.test(trimmed)) {
        listItems.push(trimmed.replace(/^\d+[.)]\s+/, ''))
        continue
      }
      flushList()
      // Headers
      if (trimmed.startsWith('### ')) {
        result.push(<h5 key={`h-${result.length}`} style={{ margin: '8px 0 4px', fontSize: 13, fontWeight: 700, color: '#c4b5fd' }}>{inlineFormat(trimmed.slice(4))}</h5>)
      } else if (trimmed.startsWith('## ')) {
        result.push(<h4 key={`h-${result.length}`} style={{ margin: '8px 0 4px', fontSize: 14, fontWeight: 700, color: '#c4b5fd' }}>{inlineFormat(trimmed.slice(3))}</h4>)
      } else {
        result.push(<p key={`p-${result.length}`} style={{ margin: '4px 0', lineHeight: 1.7 }}>{inlineFormat(trimmed)}</p>)
      }
    }
    flushList()
    return result
  }, [text])

  return <div style={{ fontSize: 13, color: 'rgba(255,255,255,0.75)' }}>{elements}</div>
}

/** Parse inline markdown: **bold**, *italic*, `code` — returns React nodes */
function inlineFormat(text: string): React.ReactNode {
  const parts: React.ReactNode[] = []
  let remaining = text
  let key = 0

  while (remaining.length > 0) {
    // Bold: **text**
    const boldMatch = remaining.match(/^(.*?)\*\*(.+?)\*\*(.*)$/s)
    if (boldMatch) {
      if (boldMatch[1]) parts.push(inlineFormat(boldMatch[1]))
      parts.push(<strong key={key++} style={{ color: '#e2e8f0', fontWeight: 600 }}>{boldMatch[2]}</strong>)
      remaining = boldMatch[3]
      continue
    }
    // Inline code: `text`
    const codeMatch = remaining.match(/^(.*?)`(.+?)`(.*)$/s)
    if (codeMatch) {
      if (codeMatch[1]) parts.push(codeMatch[1])
      parts.push(<code key={key++} style={{ background: 'rgba(255,255,255,0.08)', padding: '1px 4px', borderRadius: 3, fontSize: 12 }}>{codeMatch[2]}</code>)
      remaining = codeMatch[3]
      continue
    }
    // Italic: *text* (but not **)
    const italicMatch = remaining.match(/^(.*?)\*(.+?)\*(.*)$/s)
    if (italicMatch) {
      if (italicMatch[1]) parts.push(italicMatch[1])
      parts.push(<em key={key++}>{italicMatch[2]}</em>)
      remaining = italicMatch[3]
      continue
    }
    // Plain text
    parts.push(remaining)
    break
  }
  return parts.length === 1 ? parts[0] : <>{parts}</>
}

function MetricCard({ label, value, color }: { label: string; value: string | number; color: string }) {
  return (
    <div style={{
      background: 'rgba(255,255,255,0.03)', borderRadius: 10, border: '1px solid rgba(255,255,255,0.06)',
      padding: '10px 14px', display: 'flex', flexDirection: 'column', gap: 2,
    }}>
      <span style={{ fontSize: 11, color: 'rgba(255,255,255,0.4)', fontWeight: 500 }}>{label}</span>
      <span style={{ fontSize: 18, fontWeight: 700, color }}>{value}</span>
    </div>
  )
}

function StatRow({ label, value, color }: { label: string; value: string | number; color?: string }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
      <span style={{ color: 'rgba(255,255,255,0.4)' }}>{label}</span>
      <span style={{ fontWeight: 600, color: color ?? '#fff' }}>{value}</span>
    </div>
  )
}

function ExitReasonChart({ trades }: { trades: Array<{ exit_reason: string }> }) {
  const counts = useMemo(() => {
    const map: Record<string, number> = {}
    trades.forEach(t => {
      const reason = t.exit_reason || 'unknown'
      map[reason] = (map[reason] || 0) + 1
    })
    return Object.entries(map).map(([reason, count]) => ({
      reason,
      count,
      label: EXIT_REASON_LABELS[reason]?.label ?? reason,
      color: EXIT_REASON_LABELS[reason]?.color ?? '#6b7280',
    }))
  }, [trades])

  if (!counts.length) return <div style={{ color: 'rgba(255,255,255,0.3)', fontSize: 12 }}>No trades</div>

  return (
    <ResponsiveContainer width="100%" height={120}>
      <BarChart data={counts} layout="vertical">
        <XAxis type="number" tick={{ fontSize: 10, fill: 'rgba(255,255,255,0.4)' }} />
        <YAxis type="category" dataKey="label" tick={{ fontSize: 11, fill: 'rgba(255,255,255,0.6)' }} width={90} />
        <Tooltip contentStyle={{ background: '#1a1a2e', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 8, fontSize: 12 }} />
        <Bar dataKey="count" name="Trades" radius={[0, 4, 4, 0]}>
          {counts.map((c, i) => <Cell key={i} fill={c.color} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

function CostSensitivityGrid({ data }: { data: Array<{ fee_pct: number; slippage_pct: number; return_pct: number; profitable: boolean }> }) {
  // Build a matrix: rows = fee, cols = slippage
  const fees = [...new Set(data.map(d => d.fee_pct))].sort()
  const slippages = [...new Set(data.map(d => d.slippage_pct))].sort()
  const lookup = new Map(data.map(d => [`${d.fee_pct}-${d.slippage_pct}`, d]))

  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ borderCollapse: 'collapse', fontSize: 12 }}>
        <thead>
          <tr>
            <th style={{ padding: '6px 8px', fontSize: 10, color: 'rgba(255,255,255,0.4)' }}>Fee \ Slip</th>
            {slippages.map(s => (
              <th key={s} style={{ padding: '6px 8px', fontSize: 10, color: 'rgba(255,255,255,0.4)' }}>{(s * 100).toFixed(2)}%</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {fees.map(f => (
            <tr key={f}>
              <td style={{ padding: '6px 8px', fontSize: 11, color: 'rgba(255,255,255,0.5)', fontWeight: 600 }}>{(f * 100).toFixed(2)}%</td>
              {slippages.map(s => {
                const entry = lookup.get(`${f}-${s}`)
                const ret = entry?.return_pct ?? 0
                const bg = ret > 0
                  ? `rgba(34, 197, 94, ${Math.min(Math.abs(ret) / 20, 0.4)})`
                  : `rgba(239, 68, 68, ${Math.min(Math.abs(ret) / 20, 0.4)})`
                return (
                  <td key={s} style={{
                    padding: '6px 8px', textAlign: 'center', fontWeight: 600,
                    background: bg, color: ret >= 0 ? '#22c55e' : '#ef4444',
                    borderRadius: 2,
                  }}>
                    {fmtPct(ret)}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}


// ═══════════════════════════════════════════════════════════════════════════
// Trigger Panel (Modal) — Pair Selector + Params + WebSocket Progress
// ═══════════════════════════════════════════════════════════════════════════

type TriggerPhase = 'select' | 'configure' | 'running' | 'complete' | 'error'

function TriggerPanel({ initialPair, followedPairs, onClose, onComplete }: {
  initialPair: string
  followedPairs: BacktestPairInfo[]
  onClose: () => void
  onComplete: (runId: number | null) => void
}) {
  const [phase, setPhase] = useState<TriggerPhase>(initialPair ? 'configure' : 'select')
  const [pair, setPair] = useState(initialPair)
  const [search, setSearch] = useState('')
  const [trigDays, setTrigDays] = useState(60)
  const [positionSize, setPositionSize] = useState(0.10)
  const [trailingStop, setTrailingStop] = useState(0.03)
  const [entryThreshold, setEntryThreshold] = useState(0.4)
  const [fee, setFee] = useState(0.006)
  const [slippage] = useState(0.001)
  const [progressPct, setProgressPct] = useState(0)
  const [statusText, setStatusText] = useState('')
  const [error, setError] = useState('')
  const [errorCode, setErrorCode] = useState('')
  const [errorMeta, setErrorMeta] = useState<{ pair?: string; candles?: number; days?: number }>({})
  const [resultId, setResultId] = useState<number | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const phaseRef = useRef(phase)
  phaseRef.current = phase

  // Filter pairs by search
  const filteredPairs = useMemo(() => {
    if (!search) return followedPairs
    const q = search.toUpperCase()
    return followedPairs.filter(p =>
      p.pair.toUpperCase().includes(q)
    )
  }, [followedPairs, search])

  // Focus search input when in select phase
  useEffect(() => {
    if (phase === 'select' && searchInputRef.current) {
      searchInputRef.current.focus()
    }
  }, [phase])

  // Cleanup WS on unmount
  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close()
        wsRef.current = null
      }
    }
  }, [])

  const handleSelectPair = (p: string) => {
    setPair(p)
    setPhase('configure')
  }

  const handleRun = () => {
    setPhase('running')
    setProgressPct(0)
    setStatusText('Connecting...')
    setError('')

    let receivedResult = false

    const ws = openBacktestSocket((event: BacktestProgressEvent) => {
      switch (event.type) {
        case 'status':
          if (event.phase === 'fetching_candles') {
            setStatusText(`Fetching candles for ${event.pair}...`)
            setProgressPct(10)
          } else if (event.phase === 'running_backtest') {
            setStatusText(`Running backtest on ${event.total_candles} candles...`)
            setProgressPct(20)
          }
          break
        case 'progress':
          setProgressPct(20 + (event.pct ?? 0) * 0.7)
          break
        case 'complete':
          receivedResult = true
          setProgressPct(100)
          setStatusText('Backtest complete!')
          setResultId(event.id ?? null)
          setPhase('complete')
          break
        case 'error':
          receivedResult = true
          setError(event.detail ?? 'Backtest failed')
          setErrorCode(event.code ?? '')
          setErrorMeta({ pair: event.pair, candles: event.candles_found, days: event.days })
          setPhase('error')
          break
      }
    }, () => {
      if (!receivedResult) {
        setError(prev => prev || 'Connection lost')
        setPhase('error')
      }
    })

    wsRef.current = ws

    ws.onopen = () => {
      setStatusText('Sending parameters...')
      ws.send(JSON.stringify({
        pair: pair.trim().toUpperCase(),
        days: trigDays,
        position_size_pct: positionSize,
        trailing_stop_pct: trailingStop,
        entry_threshold: entryThreshold,
        fee_pct: fee,
        slippage_pct: slippage,
      }))
    }
  }

  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)', backdropFilter: 'blur(4px)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
    }} onClick={onClose}>
      <div
        onClick={e => e.stopPropagation()}
        style={{
          background: '#1a1a2e', borderRadius: 16, border: '1px solid rgba(255,255,255,0.1)',
          padding: 24, width: phase === 'select' ? 520 : 440, maxWidth: '95vw',
          maxHeight: '85vh', display: 'flex', flexDirection: 'column', gap: 16,
          overflow: 'hidden',
        }}
      >
        {/* ── Close button ─────────────────────────────────── */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h3 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>
            {phase === 'select' && 'Select a Pair'}
            {phase === 'configure' && `Backtest ${pair}`}
            {phase === 'running' && `Running ${pair}...`}
            {phase === 'complete' && `${pair} Complete`}
            {phase === 'error' && 'Backtest Failed'}
          </h3>
          <button onClick={onClose} style={{
            background: 'transparent', border: 'none', color: 'rgba(255,255,255,0.4)',
            cursor: 'pointer', padding: 4,
          }}>
            <X size={18} />
          </button>
        </div>

        {/* ── Phase: SELECT ────────────────────────────────── */}
        {phase === 'select' && (
          <>
            <div style={{ position: 'relative' }}>
              <Search size={14} style={{
                position: 'absolute', left: 12, top: '50%', transform: 'translateY(-50%)',
                color: 'rgba(255,255,255,0.3)',
              }} />
              <input
                ref={searchInputRef}
                value={search}
                onChange={e => setSearch(e.target.value)}
                placeholder="Search pairs..."
                style={{
                  ...inputStyle,
                  paddingLeft: 34,
                }}
              />
            </div>
            <div style={{ overflowY: 'auto', maxHeight: '50vh', display: 'flex', flexDirection: 'column', gap: 4 }}>
              {filteredPairs.length === 0 ? (
                <div style={{ textAlign: 'center', padding: 20, color: 'rgba(255,255,255,0.3)', fontSize: 13 }}>
                  No matching pairs found
                </div>
              ) : (
                filteredPairs.map(p => {
                  const badge = SOURCE_BADGE[p.source] ?? SOURCE_BADGE.config
                  const Icon = badge.icon
                  return (
                    <button
                      key={p.pair}
                      onClick={() => handleSelectPair(p.pair)}
                      style={{
                        padding: '10px 14px', borderRadius: 10,
                        border: '1px solid rgba(255,255,255,0.06)',
                        background: 'rgba(255,255,255,0.03)',
                        color: '#fff', cursor: 'pointer', textAlign: 'left',
                        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                        transition: 'all 0.15s',
                      }}
                      onMouseEnter={e => {
                        e.currentTarget.style.background = 'rgba(255,255,255,0.07)'
                        e.currentTarget.style.borderColor = 'rgba(255,255,255,0.15)'
                      }}
                      onMouseLeave={e => {
                        e.currentTarget.style.background = 'rgba(255,255,255,0.03)'
                        e.currentTarget.style.borderColor = 'rgba(255,255,255,0.06)'
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                        <span style={{ fontSize: 14, fontWeight: 700 }}>{p.pair}</span>
                        <div style={{ display: 'flex', gap: 4 }}>
                          <span style={{
                            display: 'flex', alignItems: 'center', gap: 3,
                            fontSize: 9, fontWeight: 600, color: badge.color,
                            background: `${badge.color}20`, padding: '2px 6px', borderRadius: 4,
                          }}>
                            <Icon size={9} /> {badge.label}
                          </span>
                          {p.followed_by_human && p.source !== 'human' && (
                            <span style={{
                              display: 'flex', alignItems: 'center', gap: 3,
                              fontSize: 9, fontWeight: 600, color: '#3b82f6',
                              background: 'rgba(59,130,246,0.12)', padding: '2px 6px', borderRadius: 4,
                            }}>
                              <User size={9} />
                            </span>
                          )}
                          {p.followed_by_llm && p.source !== 'llm' && (
                            <span style={{
                              display: 'flex', alignItems: 'center', gap: 3,
                              fontSize: 9, fontWeight: 600, color: '#8b5cf6',
                              background: 'rgba(139,92,246,0.12)', padding: '2px 6px', borderRadius: 4,
                            }}>
                              <Bot size={9} />
                            </span>
                          )}
                        </div>
                      </div>
                      <div style={{ display: 'flex', gap: 10, alignItems: 'center', fontSize: 11 }}>
                        {p.last_run_ts ? (
                          <>
                            <span style={{ color: (p.last_return_pct ?? 0) >= 0 ? '#22c55e' : '#ef4444', fontWeight: 600 }}>
                              {fmtPct(p.last_return_pct)}
                            </span>
                            <span style={{ color: 'rgba(255,255,255,0.3)' }}>
                              {dayjs(p.last_run_ts).fromNow()}
                            </span>
                          </>
                        ) : (
                          <span style={{ color: 'rgba(255,255,255,0.2)', fontSize: 10 }}>No runs</span>
                        )}
                        <ArrowUpRight size={14} style={{ color: 'rgba(255,255,255,0.2)' }} />
                      </div>
                    </button>
                  )
                })
              )}
            </div>
          </>
        )}

        {/* ── Phase: CONFIGURE ─────────────────────────────── */}
        {phase === 'configure' && (
          <>
            {/* Selected pair header */}
            <div style={{
              display: 'flex', alignItems: 'center', gap: 8,
              padding: '8px 12px', borderRadius: 8,
              background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.08)',
            }}>
              <span style={{ fontSize: 15, fontWeight: 700 }}>{pair}</span>
              <button
                onClick={() => { setPhase('select'); setPair('') }}
                style={{
                  marginLeft: 'auto', padding: '2px 8px', borderRadius: 4,
                  border: 'none', background: 'rgba(255,255,255,0.06)',
                  color: 'rgba(255,255,255,0.4)', cursor: 'pointer', fontSize: 11,
                }}
              >
                Change
              </button>
            </div>

            <FormField label={`Period: ${trigDays} days`}>
              <input type="range" min={7} max={365} value={trigDays} onChange={e => setTrigDays(Number(e.target.value))} style={{ width: '100%' }} />
            </FormField>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
              <FormField label={`Position Size: ${(positionSize * 100).toFixed(0)}%`}>
                <input type="range" min={1} max={50} value={positionSize * 100} onChange={e => setPositionSize(Number(e.target.value) / 100)} style={{ width: '100%' }} />
              </FormField>
              <FormField label={`Trailing Stop: ${(trailingStop * 100).toFixed(1)}%`}>
                <input type="range" min={0.5} max={20} step={0.5} value={trailingStop * 100} onChange={e => setTrailingStop(Number(e.target.value) / 100)} style={{ width: '100%' }} />
              </FormField>
              <FormField label={`Entry Threshold: ${entryThreshold.toFixed(2)}`}>
                <input type="range" min={10} max={90} value={entryThreshold * 100} onChange={e => setEntryThreshold(Number(e.target.value) / 100)} style={{ width: '100%' }} />
              </FormField>
              <FormField label={`Fee: ${(fee * 100).toFixed(2)}%`}>
                <input type="range" min={0} max={5} step={0.1} value={fee * 100} onChange={e => setFee(Number(e.target.value) / 100)} style={{ width: '100%' }} />
              </FormField>
            </div>

            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button onClick={onClose} style={{ ...btnStyle, background: 'rgba(255,255,255,0.06)', color: 'rgba(255,255,255,0.6)' }}>Cancel</button>
              <button onClick={handleRun} style={{ ...btnStyle, background: 'var(--accent, #3b82f6)', color: '#fff' }}>
                <Play size={14} /> Run Backtest
              </button>
            </div>
          </>
        )}

        {/* ── Phase: RUNNING ───────────────────────────────── */}
        {phase === 'running' && (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 16, padding: '20px 0' }}>
            <Loader size={32} style={{ color: 'var(--accent, #3b82f6)', animation: 'spin 1s linear infinite' }} />
            <div style={{ width: '100%' }}>
              <div style={{
                height: 6, borderRadius: 3, background: 'rgba(255,255,255,0.06)',
                overflow: 'hidden',
              }}>
                <div style={{
                  height: '100%', borderRadius: 3,
                  background: 'var(--accent, #3b82f6)',
                  width: `${progressPct}%`,
                  transition: 'width 0.3s ease',
                }} />
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 6 }}>
                <span style={{ fontSize: 12, color: 'rgba(255,255,255,0.4)' }}>{statusText}</span>
                <span style={{ fontSize: 12, color: 'rgba(255,255,255,0.4)' }}>{Math.round(progressPct)}%</span>
              </div>
            </div>
          </div>
        )}

        {/* ── Phase: COMPLETE ──────────────────────────────── */}
        {phase === 'complete' && (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 16, padding: '20px 0' }}>
            <div style={{
              width: 48, height: 48, borderRadius: '50%',
              background: 'rgba(34,197,94,0.15)', display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <TrendingUp size={24} style={{ color: '#22c55e' }} />
            </div>
            <span style={{ fontSize: 14, fontWeight: 600 }}>Backtest Complete</span>
            <div style={{ display: 'flex', gap: 8 }}>
              <button onClick={onClose} style={{ ...btnStyle, background: 'rgba(255,255,255,0.06)', color: 'rgba(255,255,255,0.6)' }}>Close</button>
              <button onClick={() => onComplete(resultId)} style={{ ...btnStyle, background: 'var(--accent, #3b82f6)', color: '#fff' }}>
                View Results
              </button>
            </div>
          </div>
        )}

        {/* ── Phase: ERROR ─────────────────────────────────── */}
        {phase === 'error' && (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 16, padding: '20px 0' }}>
            <div style={{
              width: 48, height: 48, borderRadius: '50%',
              background: 'rgba(239,68,68,0.15)', display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <X size={24} style={{ color: '#ef4444' }} />
            </div>
            <div style={{ textAlign: 'center', maxWidth: 360 }}>
              <div style={{ fontSize: 14, fontWeight: 600, color: '#ef4444', marginBottom: 8 }}>
                {errorCode === 'no_candles'
                  ? `No data available for ${errorMeta.pair ?? pair}`
                  : errorCode === 'insufficient_candles'
                    ? `Not enough data for ${errorMeta.pair ?? pair}`
                    : 'Backtest failed'}
              </div>
              <div style={{ fontSize: 12, color: 'rgba(255,255,255,0.5)', lineHeight: 1.5 }}>
                {errorCode === 'no_candles' ? (
                  <>The exchange returned no candle history for this pair. It may be delisted, not yet listed, or unavailable on your exchange.</>
                ) : errorCode === 'insufficient_candles' ? (
                  <>Only {errorMeta.candles ?? 0} candles were found — at least 100 are needed. Try a longer time range to collect more data.</>
                ) : (
                  <>{error}</>
                )}
              </div>
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <button onClick={onClose} style={{ ...btnStyle, background: 'rgba(255,255,255,0.06)', color: 'rgba(255,255,255,0.6)' }}>Close</button>
              {errorCode === 'insufficient_candles' ? (
                <button
                  onClick={() => {
                    setTrigDays(Math.min(365, (errorMeta.days ?? trigDays) * 2))
                    setPhase('configure')
                  }}
                  style={{ ...btnStyle, background: 'var(--accent, #3b82f6)', color: '#fff' }}
                >
                  Increase Period & Retry
                </button>
              ) : (
                <button onClick={() => { setPhase('select'); setPair('') }} style={{ ...btnStyle, background: 'var(--accent, #3b82f6)', color: '#fff' }}>
                  Pick Another Pair
                </button>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Spinner animation */}
      <style>{`
        @keyframes spin {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  )
}

function FormField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      <label style={{ fontSize: 12, fontWeight: 500, color: 'rgba(255,255,255,0.5)' }}>
        {label}
      </label>
      {children}
    </div>
  )
}

const inputStyle: React.CSSProperties = {
  padding: '8px 12px', borderRadius: 8, border: '1px solid rgba(255,255,255,0.1)',
  background: 'rgba(255,255,255,0.06)', color: '#fff', fontSize: 13, outline: 'none',
  width: '100%', boxSizing: 'border-box',
}

const btnStyle: React.CSSProperties = {
  padding: '8px 16px', borderRadius: 8, border: 'none', cursor: 'pointer',
  fontSize: 13, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 6,
}
