/**
 * Analytics — Equity curve, performance metrics, drawdown, daily summaries.
 * The "pro trading analytics hub" from the dashboard makeover.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'
import {
  ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, CartesianGrid,
  BarChart, Bar, Cell,
} from 'recharts'
import { TrendingUp, TrendingDown, Activity, Target, BarChart2, Calendar } from 'lucide-react'
import { fetchPortfolioHistory, fetchAnalytics } from '../api'
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

function EquityCurve({ data, fmt }: { data: Array<{ ts: string; portfolio_value: number; total_pnl: number }>; fmt: (v: number | null) => string }) {
  if (!data.length) return <EmptyState icon="chart" title="No portfolio data" description="Portfolio snapshots will appear as the bot runs cycles." />

  const chartData = data.map((d) => ({
    time: dayjs(d.ts).format('MMM DD HH:mm'),
    value: d.portfolio_value,
    pnl: d.total_pnl,
  }))

  const startVal = chartData[0]?.value ?? 0
  const endVal = chartData[chartData.length - 1]?.value ?? 0
  const isUp = endVal >= startVal

  return (
    <ResponsiveContainer width="100%" height={300}>
      <AreaChart data={chartData} margin={{ top: 10, right: 10, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id="equityGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={isUp ? '#22c55e' : '#ef4444'} stopOpacity={0.3} />
            <stop offset="100%" stopColor={isUp ? '#22c55e' : '#ef4444'} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
        <XAxis dataKey="time" tick={{ fontSize: 10, fill: '#6e7681' }} interval="preserveStartEnd" />
        <YAxis tick={{ fontSize: 10, fill: '#6e7681' }} tickFormatter={(v) => fmt(v)} width={80} />
        <Tooltip
          contentStyle={{ background: '#161b22', border: '1px solid #30363d', borderRadius: 8, fontSize: 12 }}
          labelStyle={{ color: '#8b949e' }}
          formatter={(v: number | undefined) => [fmt(v ?? 0), 'Portfolio Value']}
        />
        <Area
          type="monotone"
          dataKey="value"
          stroke={isUp ? '#22c55e' : '#ef4444'}
          strokeWidth={2}
          fill="url(#equityGrad)"
        />
      </AreaChart>
    </ResponsiveContainer>
  )
}

function DrawdownChart({ data }: { data: Array<{ ts: string; portfolio_value: number }> }) {
  if (!data.length) return null

  // Calculate drawdown from peak
  let peak = data[0]?.portfolio_value ?? 0
  const ddData = data.map((d) => {
    if (d.portfolio_value > peak) peak = d.portfolio_value
    const dd = peak > 0 ? ((d.portfolio_value - peak) / peak) * 100 : 0
    return { time: dayjs(d.ts).format('MMM DD HH:mm'), drawdown: dd }
  })

  return (
    <ResponsiveContainer width="100%" height={160}>
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
          formatter={(v: number | undefined) => [`${(v ?? 0).toFixed(2)}%`, 'Drawdown']}
        />
        <Area type="monotone" dataKey="drawdown" stroke="#ef4444" strokeWidth={1.5} fill="url(#ddGrad)" />
      </AreaChart>
    </ResponsiveContainer>
  )
}

function DailyPnLChart({ data, fmt }: { data: Array<{ date: string; total_pnl: number }>; fmt: (v: number | null) => string }) {
  if (!data.length) return null
  const chartData = data.map((d) => ({
    date: dayjs(d.date).format('MMM DD'),
    pnl: d.total_pnl ?? 0,
  }))

  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={chartData} margin={{ top: 5, right: 10, bottom: 0, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
        <XAxis dataKey="date" tick={{ fontSize: 10, fill: '#6e7681' }} />
        <YAxis tick={{ fontSize: 10, fill: '#6e7681' }} tickFormatter={(v) => fmt(v)} width={70} />
        <Tooltip
          contentStyle={{ background: '#161b22', border: '1px solid #30363d', borderRadius: 8, fontSize: 12 }}
          formatter={(v: number | undefined) => [fmt(v ?? 0), 'Daily PnL']}
        />
        <Bar dataKey="pnl" radius={[3, 3, 0, 0]}>
          {chartData.map((entry, i) => (
            <Cell key={i} fill={entry.pnl >= 0 ? '#22c55e' : '#ef4444'} opacity={0.8} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

export default function Analytics() {
  const [range, setRange] = useState(720)
  const fmtCurrency = useCurrencyFormatter()

  const { data: history, isLoading: histLoading } = useQuery({
    queryKey: ['portfolio-history', range],
    queryFn: () => fetchPortfolioHistory(range),
    refetchInterval: 60_000,
  })

  const { data: analytics, isLoading: anlLoading } = useQuery({
    queryKey: ['analytics', range],
    queryFn: () => fetchAnalytics(range),
    refetchInterval: 60_000,
  })

  const isLoading = histLoading || anlLoading
  const perf = analytics?.performance?.trade_stats
  const wl = analytics?.win_loss
  const pRange = analytics?.portfolio_range

  // Sharpe ratio approximation (daily returns)
  const dailySummaries = analytics?.daily_summaries ?? []
  let sharpeRatio: number | null = null
  if (dailySummaries.length > 2) {
    const returns = dailySummaries
      .filter((d) => d.opening_value && d.closing_value)
      .map((d) => (d.closing_value - d.opening_value) / d.opening_value)
    if (returns.length > 2) {
      const mean = returns.reduce((a, b) => a + b, 0) / returns.length
      const std = Math.sqrt(returns.reduce((a, r) => a + (r - mean) ** 2, 0) / (returns.length - 1))
      sharpeRatio = std > 0 ? (mean / std) * Math.sqrt(252) : null // annualized
    }
  }

  return (
    <PageTransition>
      <div className="p-6 space-y-6">
        {/* Header + time range selector */}
        <div className="flex items-center justify-between">
          <h2 className="text-xl font-bold text-gray-100">Analytics Hub</h2>
          <div className="flex gap-1">
            {TIME_RANGES.map((r) => (
              <button
                key={r.hours}
                onClick={() => setRange(r.hours)}
                className={`px-3 py-1.5 text-xs rounded-lg font-medium transition-colors ${
                  range === r.hours
                    ? 'bg-brand-600/30 text-brand-400 border border-brand-600/50'
                    : 'bg-gray-800/50 text-gray-400 border border-gray-800 hover:border-gray-700'
                }`}
              >
                {r.label}
              </button>
            ))}
          </div>
        </div>

        {/* Key metrics */}
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
          {isLoading ? (
            <SkeletonStatCards count={6} />
          ) : (
            <>
              <StatCard
                label="Total PnL"
                value={fmtCurrency(perf?.total_pnl)}
                accent={(perf?.total_pnl ?? 0) >= 0 ? 'green' : 'red'}
                icon={<TrendingUp size={14} />}
                sub={`${perf?.total_trades ?? 0} trades`}
              />
              <StatCard
                label="Win Rate"
                value={wl?.win_rate != null ? `${wl.win_rate.toFixed(1)}%` : '—'}
                accent={(wl?.win_rate ?? 0) >= 50 ? 'green' : 'red'}
                icon={<Target size={14} />}
                sub={`${wl?.sample_size ?? 0} samples`}
              />
              <StatCard
                label="Best Trade"
                value={fmtCurrency(perf?.best_pnl)}
                accent="green"
                icon={<TrendingUp size={14} />}
              />
              <StatCard
                label="Worst Trade"
                value={fmtCurrency(perf?.worst_pnl)}
                accent="red"
                icon={<TrendingDown size={14} />}
              />
              <StatCard
                label="Avg Win"
                value={fmtCurrency(wl?.avg_win)}
                accent="green"
                sub={`Avg Loss: ${fmtCurrency(wl?.avg_loss)}`}
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

        {/* Equity curve */}
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
            <SkeletonBlock className="h-[300px]" />
          ) : (
            <EquityCurve data={history?.history ?? []} fmt={fmtCurrency} />
          )}
        </div>

        {/* Drawdown + Daily PnL row */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-3">
              <TrendingDown size={14} className="text-red-400" />
              Drawdown from Peak
            </h3>
            {isLoading ? (
              <SkeletonBlock className="h-[160px]" />
            ) : (
              <DrawdownChart data={history?.history ?? []} />
            )}
          </div>

          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-3">
              <Calendar size={14} className="text-blue-400" />
              Daily PnL
            </h3>
            {isLoading ? (
              <SkeletonBlock className="h-[200px]" />
            ) : (
              <DailyPnLChart data={dailySummaries} fmt={fmtCurrency} />
            )}
          </div>
        </div>

        {/* Best / Worst trades */}
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
              <p className="text-xs text-gray-600">No trades yet</p>
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
              <p className="text-xs text-gray-600">No trades yet</p>
            )}
          </div>
        </div>

        {/* Volume & fees summary */}
        {perf && (
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
                <p className="font-semibold text-gray-200">{perf.avg_confidence != null ? `${(perf.avg_confidence * 100).toFixed(0)}%` : '—'}</p>
              </div>
              <div>
                <p className="text-xs text-gray-500 mb-0.5">Fee Ratio</p>
                <p className="font-semibold text-gray-200">
                  {perf.total_volume ? ((perf.total_fees / perf.total_volume) * 100).toFixed(3) + '%' : '—'}
                </p>
              </div>
            </div>
          </div>
        )}
      </div>
    </PageTransition>
  )
}
