/**
 * Analytics — Equity curve, performance metrics, drawdown, daily summaries,
 * and AI prediction accuracy.
 *
 * Trade-dependent metrics (win rate, best trade, etc.) show "—" when there
 * are no closed trades rather than misleading zeros.
 *
 * Daily PnL is computed live from portfolio snapshots instead of the
 * end-of-day daily_summaries table, so today's bar is always current.
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'
import {
  ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, CartesianGrid,
  BarChart, Bar, Cell,
} from 'recharts'
import {
  TrendingUp, TrendingDown, Activity, Target, BarChart2, Calendar, Brain, Zap,
} from 'lucide-react'
import { fetchPortfolioHistory, fetchAnalytics, fetchPredictionAccuracy } from '../api'
import { useCurrencyFormatter } from '../store'
import StatCard from '../components/StatCard'
import { SkeletonStatCards, SkeletonBlock } from '../components/Skeleton'
import EmptyState from '../components/EmptyState'
import PageTransition from '../components/PageTransition'

const TIME_RANGES = [
  { label: '24h', hours: 24 },
  { label: '7d', hours: 168 },
  { label: '30d', hours: 720 },
  { label: '90d', hours: 2160 },
  { label: '1y', hours: 8760 },
]

// ─── Helpers ───────────────────────────────────────────────────────────────

function pct(v: number | null | undefined, decimals = 1): string {
  return v != null ? `${v.toFixed(decimals)}%` : '—'
}

function accuracyColor(v: number | null | undefined): string {
  if (v == null) return 'text-gray-500'
  if (v >= 60) return 'text-green-400'
  if (v >= 45) return 'text-yellow-400'
  return 'text-red-400'
}

const SIGNAL_COLORS: Record<string, string> = {
  strong_buy: '#16a34a',
  buy: '#22c55e',
  weak_buy: '#86efac',
  neutral: '#6b7280',
  weak_sell: '#fca5a5',
  sell: '#ef4444',
  strong_sell: '#b91c1c',
}

// ─── Sub-charts ───────────────────────────────────────────────────────────

function EquityCurve({ data, fmt }: {
  data: Array<{ ts: string; portfolio_value: number }>
  fmt: (v: number | null) => string
}) {
  if (!data.length) {
    return <EmptyState icon="chart" title="No portfolio data" description="Portfolio snapshots appear as the bot runs cycles." />
  }
  const chartData = data.map((d) => ({
    time: dayjs(d.ts).format('MMM DD HH:mm'),
    value: d.portfolio_value,
  }))
  const isUp = (chartData.at(-1)?.value ?? 0) >= (chartData[0]?.value ?? 0)

  return (
    <ResponsiveContainer width="100%" height={280}>
      <AreaChart data={chartData} margin={{ top: 10, right: 10, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={isUp ? '#22c55e' : '#ef4444'} stopOpacity={0.25} />
            <stop offset="100%" stopColor={isUp ? '#22c55e' : '#ef4444'} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
        <XAxis dataKey="time" tick={{ fontSize: 10, fill: '#6e7681' }} interval="preserveStartEnd" />
        <YAxis tick={{ fontSize: 10, fill: '#6e7681' }} tickFormatter={(v) => fmt(v)} width={80} />
        <Tooltip
          contentStyle={{ background: '#161b22', border: '1px solid #30363d', borderRadius: 8, fontSize: 12 }}
          labelStyle={{ color: '#8b949e' }}
          formatter={(v: any) => [fmt(v as number), 'Portfolio Value']}
        />
        <Area type="monotone" dataKey="value" stroke={isUp ? '#22c55e' : '#ef4444'} strokeWidth={2} fill="url(#eqGrad)" />
      </AreaChart>
    </ResponsiveContainer>
  )
}

function DrawdownChart({ data }: { data: Array<{ ts: string; portfolio_value: number }> }) {
  if (!data.length) return null
  let peak = data[0]?.portfolio_value ?? 0
  const ddData = data.map((d) => {
    if (d.portfolio_value > peak) peak = d.portfolio_value
    const dd = peak > 0 ? ((d.portfolio_value - peak) / peak) * 100 : 0
    return { time: dayjs(d.ts).format('MMM DD HH:mm'), drawdown: dd }
  })
  return (
    <ResponsiveContainer width="100%" height={180}>
      <AreaChart data={ddData} margin={{ top: 5, right: 10, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id="ddGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#ef4444" stopOpacity={0} />
            <stop offset="100%" stopColor="#ef4444" stopOpacity={0.3} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
        <XAxis dataKey="time" tick={{ fontSize: 10, fill: '#6e7681' }} interval="preserveStartEnd" />
        <YAxis tick={{ fontSize: 10, fill: '#6e7681' }} tickFormatter={(v) => `${v.toFixed(1)}%`} width={50} />
        <Tooltip
          contentStyle={{ background: '#161b22', border: '1px solid #30363d', borderRadius: 8, fontSize: 12 }}
          formatter={(v: any) => [`${(v as number).toFixed(2)}%`, 'Drawdown']}
        />
        <Area type="monotone" dataKey="drawdown" stroke="#ef4444" strokeWidth={1.5} fill="url(#ddGrad)" />
      </AreaChart>
    </ResponsiveContainer>
  )
}

function DailyPnLChart({ data, fmt }: {
  data: Array<{ date: string; pnl: number }>
  fmt: (v: number | null) => string
}) {
  if (!data.length) return <EmptyState icon="chart" title="No daily data" description="Appears once portfolio snapshots span multiple days." />
  return (
    <ResponsiveContainer width="100%" height={180}>
      <BarChart data={data} margin={{ top: 5, right: 10, bottom: 0, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
        <XAxis dataKey="date" tick={{ fontSize: 10, fill: '#6e7681' }} tickFormatter={(d) => dayjs(d).format('MMM DD')} />
        <YAxis tick={{ fontSize: 10, fill: '#6e7681' }} tickFormatter={(v) => fmt(v)} width={72} />
        <Tooltip
          contentStyle={{ background: '#161b22', border: '1px solid #30363d', borderRadius: 8, fontSize: 12 }}
          formatter={(v: any) => [fmt(v as number), 'Day PnL']}
          labelFormatter={(d) => dayjs(d).format('MMM DD')}
        />
        <Bar dataKey="pnl" radius={[3, 3, 0, 0]}>
          {data.map((entry, i) => (
            <Cell key={i} fill={entry.pnl >= 0 ? '#22c55e' : '#ef4444'} opacity={0.8} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

function SignalDistributionChart({ data }: {
  data: Array<{ signal: string; count: number }>
}) {
  if (!data.length) return null
  return (
    <ResponsiveContainer width="100%" height={160}>
      <BarChart data={data} layout="vertical" margin={{ top: 0, right: 16, bottom: 0, left: 0 }}>
        <XAxis type="number" tick={{ fontSize: 10, fill: '#6e7681' }} />
        <YAxis type="category" dataKey="signal" tick={{ fontSize: 10, fill: '#6e7681' }} width={80} />
        <Tooltip
          contentStyle={{ background: '#161b22', border: '1px solid #30363d', borderRadius: 8, fontSize: 12 }}
          formatter={(v: any) => [v, 'predictions']}
        />
        <Bar dataKey="count" radius={[0, 3, 3, 0]}>
          {data.map((entry, i) => (
            <Cell key={i} fill={SIGNAL_COLORS[entry.signal] ?? '#6b7280'} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

// ─── Main Component ───────────────────────────────────────────────────────

export default function Analytics() {
  const [range, setRange] = useState(720)
  const fmtCurrency = useCurrencyFormatter()

  const days = Math.max(1, Math.ceil(range / 24))

  const { data: history, isLoading: histLoading } = useQuery({
    queryKey: ['portfolio-history', range],
    queryFn: () => fetchPortfolioHistory(range),
    refetchInterval: 30_000,
  })

  const { data: analytics, isLoading: anlLoading } = useQuery({
    queryKey: ['analytics', range],
    queryFn: () => fetchAnalytics(range),
    refetchInterval: 30_000,
  })

  const { data: predStats, isLoading: predLoading } = useQuery({
    queryKey: ['prediction-accuracy', days],
    queryFn: () => fetchPredictionAccuracy(days),
    refetchInterval: 60_000,
  })

  const isLoading = histLoading || anlLoading

  // ── Derived values ──────────────────────────────────────────────────────

  const perf = analytics?.performance?.trade_stats
  const wl = analytics?.win_loss
  const pRange = analytics?.portfolio_range
  const hasTrades = (wl?.sample_size ?? 0) > 0

  // Sharpe from daily snapshot returns
  const sharpeRatio = useMemo(() => {
    const pts = history?.history ?? []
    if (pts.length < 4) return null
    const byDay = new Map<string, number[]>()
    for (const p of pts) {
      const d = dayjs(p.ts).format('YYYY-MM-DD')
      byDay.set(d, [...(byDay.get(d) ?? []), p.portfolio_value])
    }
    const opens = Array.from(byDay.values()).map((vs) => vs[0])
    const closes = Array.from(byDay.values()).map((vs) => vs[vs.length - 1])
    const returns = opens.map((o, i) => (closes[i] - o) / (o || 1)).filter((_, i) => opens[i] > 0)
    if (returns.length < 3) return null
    const mean = returns.reduce((a, b) => a + b, 0) / returns.length
    const std = Math.sqrt(returns.reduce((a, r) => a + (r - mean) ** 2, 0) / (returns.length - 1))
    return std > 0 ? (mean / std) * Math.sqrt(252) : null
  }, [history])

  // Daily PnL computed live from portfolio snapshots (first vs last value per day)
  const dailyPnL = useMemo(() => {
    const pts = history?.history ?? []
    if (!pts.length) return []
    const byDay = new Map<string, { open: number; close: number }>()
    for (const p of pts) {
      const day = dayjs(p.ts).format('YYYY-MM-DD')
      if (!byDay.has(day)) byDay.set(day, { open: p.portfolio_value, close: p.portfolio_value })
      else byDay.get(day)!.close = p.portfolio_value
    }
    return Array.from(byDay.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([date, { open, close }]) => ({ date, pnl: close - open }))
  }, [history])

  // Signal distribution for bar chart
  const signalDist = useMemo(() => {
    const bySignal = predStats?.by_signal_type ?? {}
    return Object.entries(bySignal)
      .map(([signal, v]) => ({ signal, count: v.total }))
      .filter((s) => s.count > 0)
      .sort((a, b) => {
        const order = ['strong_buy', 'buy', 'weak_buy', 'neutral', 'weak_sell', 'sell', 'strong_sell']
        return order.indexOf(a.signal) - order.indexOf(b.signal)
      })
  }, [predStats])

  const overall = predStats?.overall

  return (
    <PageTransition>
      <div className="p-6 space-y-6">
        {/* Header + time range */}
        <div className="flex items-center justify-between">
          <h2 className="text-xl font-bold text-gray-100">Analytics</h2>
          <div className="flex gap-1">
            {TIME_RANGES.map((r) => (
              <button
                key={r.hours}
                onClick={() => setRange(r.hours)}
                className={`px-3 py-1.5 text-xs rounded-lg font-medium transition-colors ${range === r.hours
                    ? 'bg-brand-600/30 text-brand-400 border border-brand-600/50'
                    : 'bg-gray-800/50 text-gray-400 border border-gray-800 hover:border-gray-700'
                  }`}
              >
                {r.label}
              </button>
            ))}
          </div>
        </div>

        {/* ── Trade performance stat cards ── */}
        <div>
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wider mb-2">Trade Performance</p>
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
            {isLoading ? (
              <SkeletonStatCards count={6} />
            ) : (
              <>
                <StatCard
                  label="Total PnL"
                  value={hasTrades ? fmtCurrency(perf?.total_pnl) : '—'}
                  accent={hasTrades ? ((perf?.total_pnl ?? 0) >= 0 ? 'green' : 'red') : 'gray'}
                  icon={<TrendingUp size={14} />}
                  sub={hasTrades ? `${perf?.total_trades ?? 0} trades` : 'No closed trades yet'}
                />
                <StatCard
                  label="Win Rate"
                  value={hasTrades ? pct((wl!.win_rate) * 100) : '—'}
                  accent={hasTrades ? (wl!.win_rate >= 0.5 ? 'green' : 'red') : 'gray'}
                  icon={<Target size={14} />}
                  sub={hasTrades ? `${wl?.sample_size} samples` : undefined}
                />
                <StatCard
                  label="Best Trade"
                  value={hasTrades ? fmtCurrency(perf?.best_pnl) : '—'}
                  accent={hasTrades ? 'green' : 'gray'}
                  icon={<TrendingUp size={14} />}
                />
                <StatCard
                  label="Worst Trade"
                  value={hasTrades ? fmtCurrency(perf?.worst_pnl) : '—'}
                  accent={hasTrades ? 'red' : 'gray'}
                  icon={<TrendingDown size={14} />}
                />
                <StatCard
                  label="Avg Win"
                  value={hasTrades ? fmtCurrency(wl?.avg_win) : '—'}
                  accent={hasTrades ? 'green' : 'gray'}
                  sub={hasTrades ? `Avg Loss: ${fmtCurrency(wl?.avg_loss)}` : undefined}
                />
                <StatCard
                  label="Sharpe Ratio"
                  value={sharpeRatio != null ? sharpeRatio.toFixed(2) : '—'}
                  accent={sharpeRatio != null && sharpeRatio > 0 ? 'green' : 'gray'}
                  icon={<Activity size={14} />}
                  sub={sharpeRatio != null ? (sharpeRatio > 1 ? 'Good' : sharpeRatio > 0 ? 'Moderate' : 'Poor') : undefined}
                />
              </>
            )}
          </div>
        </div>

        {/* ── AI Prediction Performance ── */}
        <div>
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wider mb-2">AI Prediction Performance</p>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {/* Accuracy by timeframe */}
            <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
              <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-4">
                <Brain size={14} className="text-brand-400" />
                Accuracy by Timeframe
                {overall && <span className="ml-auto text-xs text-gray-500">{overall.total} predictions</span>}
              </h3>
              {predLoading ? (
                <SkeletonBlock className="h-24" />
              ) : (
                <div className="grid grid-cols-3 gap-3">
                  {[
                    { label: '1h', p: overall?.accuracy_1h_pct, n: overall?.evaluated_1h },
                    { label: '24h', p: overall?.accuracy_24h_pct, n: overall?.evaluated_24h },
                    { label: 'Total', p: null, n: overall?.total, raw: overall?.total != null ? String(overall.total) : '—' },
                  ].map(({ label, p, n, raw }) => (
                    <div key={label} className="bg-gray-800/50 rounded-lg p-3 text-center">
                      <p className="text-xs text-gray-500 mb-1">{label}</p>
                      <p className={`text-2xl font-bold ${raw ? 'text-gray-200' : accuracyColor(p)}`}>
                        {raw ?? (p != null ? `${p.toFixed(1)}%` : '—')}
                      </p>
                      {n != null && n > 0 && !raw && (
                        <p className="text-xs text-gray-600 mt-0.5">{n} evaluated</p>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Signal distribution */}
            <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
              <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-4">
                <Zap size={14} className="text-yellow-400" />
                Signal Distribution
              </h3>
              {predLoading ? (
                <SkeletonBlock className="h-[160px]" />
              ) : signalDist.length ? (
                <SignalDistributionChart data={signalDist} />
              ) : (
                <EmptyState icon="chart" title="No predictions yet" description="Signals appear after the first analysis cycle." />
              )}
            </div>
          </div>
        </div>

        {/* ── Equity curve ── */}
        <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2">
              <BarChart2 size={14} className="text-brand-400" />
              Equity Curve
            </h3>
            {pRange && (
              <span className="text-xs text-gray-500">
                Range: {fmtCurrency(pRange.low)} — {fmtCurrency(pRange.high)}
              </span>
            )}
          </div>
          {isLoading ? (
            <SkeletonBlock className="h-[280px]" />
          ) : (
            <EquityCurve data={history?.history ?? []} fmt={fmtCurrency} />
          )}
        </div>

        {/* ── Drawdown + Daily PnL ── */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-3">
              <TrendingDown size={14} className="text-red-400" />
              Drawdown from Peak
            </h3>
            {isLoading ? <SkeletonBlock className="h-[180px]" /> : <DrawdownChart data={history?.history ?? []} />}
          </div>

          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-3">
              <Calendar size={14} className="text-blue-400" />
              Daily PnL
            </h3>
            {isLoading ? (
              <SkeletonBlock className="h-[180px]" />
            ) : (
              <DailyPnLChart data={dailyPnL} fmt={fmtCurrency} />
            )}
          </div>
        </div>

        {/* ── Best / Worst trades ── */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-green-400 mb-3">Top 3 Best Trades</h3>
            {analytics?.best_worst?.best?.length ? (
              <div className="space-y-2">
                {analytics.best_worst.best.map((t, i) => (
                  <div key={i} className="flex items-center justify-between bg-gray-800/50 rounded-lg px-3 py-2">
                    <div>
                      <span className="text-sm font-medium text-gray-200">{t.pair}</span>
                      <span className="text-xs text-gray-500 ml-2">{dayjs(t.ts).format('MMM DD')}</span>
                    </div>
                    <span className="text-sm font-bold text-green-400">+{fmtCurrency(t.pnl)}</span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-xs text-gray-600">No closed trades yet</p>
            )}
          </div>
          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-red-400 mb-3">Top 3 Worst Trades</h3>
            {analytics?.best_worst?.worst?.length ? (
              <div className="space-y-2">
                {analytics.best_worst.worst.map((t, i) => (
                  <div key={i} className="flex items-center justify-between bg-gray-800/50 rounded-lg px-3 py-2">
                    <div>
                      <span className="text-sm font-medium text-gray-200">{t.pair}</span>
                      <span className="text-xs text-gray-500 ml-2">{dayjs(t.ts).format('MMM DD')}</span>
                    </div>
                    <span className="text-sm font-bold text-red-400">{fmtCurrency(t.pnl)}</span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-xs text-gray-600">No closed trades yet</p>
            )}
          </div>
        </div>

        {/* ── Volume & fees (only if trades exist) ── */}
        {hasTrades && perf && (
          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-300 mb-3">Volume & Fee Summary</h3>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
              <div>
                <p className="text-xs text-gray-500 mb-0.5">Total Volume</p>
                <p className="font-semibold text-gray-200">{fmtCurrency(perf.total_volume)}</p>
              </div>
              <div>
                <p className="text-xs text-gray-500 mb-0.5">Total Fees</p>
                <p className="font-semibold text-gray-200">{fmtCurrency(perf.total_fees)}</p>
              </div>
              <div>
                <p className="text-xs text-gray-500 mb-0.5">Avg Confidence</p>
                <p className="font-semibold text-gray-200">
                  {perf.avg_confidence != null ? `${(perf.avg_confidence * 100).toFixed(0)}%` : '—'}
                </p>
              </div>
              <div>
                <p className="text-xs text-gray-500 mb-0.5">Fee Ratio</p>
                <p className="font-semibold text-gray-200">
                  {perf.total_volume ? `${((perf.total_fees / perf.total_volume) * 100).toFixed(3)}%` : '—'}
                </p>
              </div>
            </div>
          </div>
        )}
      </div>
    </PageTransition>
  )
}
