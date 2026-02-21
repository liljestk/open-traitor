/**
 * Risk Exposure — Portfolio concentration, trailing stops, VaR, drawdown metrics.
 */
import { useQuery } from '@tanstack/react-query'
import {
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
  Tooltip,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
} from 'recharts'
import { Shield, AlertTriangle, TrendingDown, Lock, Crosshair } from 'lucide-react'
import { fetchPortfolioExposure, fetchTrailingStops, fetchAnalytics } from '../api'
import { useCurrencyFormatter } from '../store'
import StatCard from '../components/StatCard'
import { SkeletonStatCards, SkeletonBlock } from '../components/Skeleton'
import EmptyState from '../components/EmptyState'
import PageTransition from '../components/PageTransition'

const COLORS = ['#22c55e', '#3b82f6', '#f59e0b', '#ef4444', '#a855f7', '#06b6d4', '#ec4899', '#84cc16']

function ExposurePie({ data }: { data: Array<{ name: string; value: number; pct: number }> }) {
  if (!data.length)
    return <EmptyState icon="chart" title="No open positions" description="No exposure data to show." />
  return (
    <div className="flex items-center gap-4">
      <ResponsiveContainer width="50%" height={280}>
        <PieChart>
          <Pie
            data={data}
            dataKey="value"
            nameKey="name"
            cx="50%"
            cy="50%"
            innerRadius={60}
            outerRadius={110}
            strokeWidth={1}
            stroke="#1a1a2e"
          >
            {data.map((_, i) => (
              <Cell key={i} fill={COLORS[i % COLORS.length]} />
            ))}
          </Pie>
          <Tooltip
            contentStyle={{ background: '#1a1a2e', border: '1px solid #2a2a3e', borderRadius: 8 }}
            formatter={(val: number | undefined) => [`${(val ?? 0).toFixed(2)}%`, 'Allocation']}
          />
        </PieChart>
      </ResponsiveContainer>
      <div className="flex-1 space-y-2">
        {data.map((d, i) => (
          <div key={d.name} className="flex items-center justify-between text-xs">
            <div className="flex items-center gap-2">
              <span className="w-3 h-3 rounded-full" style={{ backgroundColor: COLORS[i % COLORS.length] }} />
              <span className="text-gray-300">{d.name}</span>
            </div>
            <span className="text-gray-400 font-mono">{d.pct.toFixed(1)}%</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function TrailingStopsPanel({
  stops,
  fmt,
}: {
  stops: Array<{
    pair: string
    stop_price: number
    entry_price: number
    current_price: number
    pnl_pct: number
  }>
  fmt: (v: number | null) => string
}) {
  if (!stops.length) return <EmptyState icon="chart" title="No trailing stops" description="No active stops." />
  return (
    <div className="space-y-2">
      {stops.map((s) => {
        const riskPct = s.current_price > 0 ? ((s.current_price - s.stop_price) / s.current_price) * 100 : 0
        return (
          <div key={s.pair} className="bg-gray-800/50 rounded-lg p-3 border border-gray-800/60">
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs font-semibold text-gray-200">{s.pair}</span>
              <span className={`text-xs font-mono ${s.pnl_pct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                {s.pnl_pct >= 0 ? '+' : ''}
                {s.pnl_pct.toFixed(2)}%
              </span>
            </div>
            <div className="grid grid-cols-3 gap-3 text-xs">
              <div>
                <span className="text-gray-600 block">Entry</span>
                <span className="text-gray-400 font-mono">{fmt(s.entry_price)}</span>
              </div>
              <div>
                <span className="text-gray-600 block">Stop</span>
                <span className="text-red-400 font-mono">{fmt(s.stop_price)}</span>
              </div>
              <div>
                <span className="text-gray-600 block">Risk</span>
                <span className="text-yellow-400 font-mono">{riskPct.toFixed(1)}%</span>
              </div>
            </div>
            {/* Risk bar */}
            <div className="mt-2 w-full bg-gray-900 h-1.5 rounded-full overflow-hidden">
              <div
                className="h-full bg-gradient-to-r from-green-500 to-yellow-500 rounded-full"
                style={{ width: `${Math.min(100, Math.max(5, 100 - riskPct * 5))}%` }}
              />
            </div>
          </div>
        )
      })}
    </div>
  )
}

function DrawdownBar({
  dailySummaries,
}: {
  dailySummaries: Array<{ date: string; pnl: number }>
}) {
  if (!dailySummaries?.length) return null
  // Build drawdown series from daily PnL
  let peak = 0
  let cumulative = 0
  const dd = dailySummaries.map((d) => {
    cumulative += d.pnl
    if (cumulative > peak) peak = cumulative
    const drawdown = peak > 0 ? ((cumulative - peak) / peak) * 100 : 0
    return { date: d.date, drawdown: Math.min(0, drawdown) }
  })

  return (
    <ResponsiveContainer width="100%" height={160}>
      <BarChart data={dd.slice(-30)}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1a1a2e" />
        <XAxis
          dataKey="date"
          tick={{ fill: '#6b7280', fontSize: 10 }}
          tickFormatter={(v) => v?.slice(5)}
        />
        <YAxis
          tick={{ fill: '#6b7280', fontSize: 10 }}
          tickFormatter={(v) => `${v.toFixed(0)}%`}
        />
        <Bar dataKey="drawdown" radius={[2, 2, 0, 0]}>
          {dd.slice(-30).map((d, i) => (
            <Cell key={i} fill={d.drawdown < -10 ? '#ef4444' : d.drawdown < -5 ? '#f59e0b' : '#6b7280'} />
          ))}
        </Bar>
        <Tooltip
          contentStyle={{ background: '#1a1a2e', border: '1px solid #2a2a3e', borderRadius: 8, fontSize: 11 }}
          formatter={(v: number | undefined) => [`${(v ?? 0).toFixed(2)}%`, 'Drawdown']}
        />
      </BarChart>
    </ResponsiveContainer>
  )
}

export default function RiskExposure() {
  const fmtCurrency = useCurrencyFormatter()

  const { data: exposure, isLoading: expLoading } = useQuery({
    queryKey: ['exposure'],
    queryFn: fetchPortfolioExposure,
    refetchInterval: 30_000,
  })

  const { data: stops, isLoading: stopsLoading } = useQuery({
    queryKey: ['trailing-stops'],
    queryFn: fetchTrailingStops,
    refetchInterval: 15_000,
  })

  const { data: analytics, isLoading: analyticsLoading } = useQuery({
    queryKey: ['analytics', 720],
    queryFn: () => fetchAnalytics(720),
    staleTime: 120_000,
  })

  const isLoading = expLoading || stopsLoading || analyticsLoading

  const exposureObj = exposure?.exposure
  const exposureData = (exposureObj?.breakdown ?? []).map((p) => ({
    name: p.pair,
    value: p.pct_of_portfolio,
    pct: p.pct_of_portfolio,
  }))

  const stopsArray = Array.isArray(stops) ? stops : []
  const dailySummaries = (analytics?.daily_summaries ?? []).map((d) => ({
    date: d.date,
    pnl: d.total_pnl,
  }))

  // Compute summary risk metrics
  const totalExposure = exposureObj?.portfolio_value ?? 0
  const cashPct = exposureObj?.cash_pct ?? 100
  const maxSinglePct = exposureData.length ? Math.max(...exposureData.map((d) => d.pct)) : 0
  const winRate = analytics?.win_loss?.win_rate ?? 0

  return (
    <PageTransition>
      <div className="p-6 space-y-4">
        {/* Header */}
        <div className="flex items-center gap-3">
          <Shield size={18} className="text-yellow-400" />
          <h2 className="text-xl font-bold text-gray-100">Risk & Exposure</h2>
        </div>

        {/* Risk KPIs */}
        {isLoading ? (
          <SkeletonStatCards count={4} />
        ) : (
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <StatCard
              label="Total Exposure"
              value={fmtCurrency(totalExposure)}
              icon={<Crosshair size={14} />}
            />
            <StatCard
              label="Cash Reserve"
              value={`${cashPct.toFixed(1)}%`}
              icon={<Lock size={14} />}
              accent={cashPct > 30 ? 'green' : 'red'}
            />
            <StatCard
              label="Max Single Position"
              value={`${maxSinglePct.toFixed(1)}%`}
              icon={<AlertTriangle size={14} />}
              accent={maxSinglePct < 30 ? 'green' : 'red'}
            />
            <StatCard
              label="Win Rate"
              value={`${(winRate * 100).toFixed(1)}%`}
              icon={<TrendingDown size={14} />}
              accent={winRate >= 0.5 ? 'green' : 'red'}
            />
          </div>
        )}

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* Exposure chart */}
          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-300 mb-3">Portfolio Concentration</h3>
            {expLoading ? (
              <SkeletonBlock className="h-[280px]" />
            ) : (
              <ExposurePie data={exposureData} />
            )}
          </div>

          {/* Trailing stops */}
          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-300 mb-3">
              Active Trailing Stops ({stopsArray.length})
            </h3>
            {stopsLoading ? (
              <SkeletonBlock className="h-[280px]" />
            ) : (
              <TrailingStopsPanel stops={stopsArray} fmt={fmtCurrency} />
            )}
          </div>
        </div>

        {/* Drawdown chart */}
        <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
          <h3 className="text-sm font-semibold text-gray-300 mb-3">Daily Drawdown (30d)</h3>
          {analyticsLoading ? (
            <SkeletonBlock className="h-[160px]" />
          ) : dailySummaries.length > 0 ? (
            <DrawdownBar dailySummaries={dailySummaries} />
          ) : (
            <EmptyState icon="chart" title="No daily data" description="Need at least 2 days of trading history." />
          )}
        </div>
      </div>
    </PageTransition>
  )
}
