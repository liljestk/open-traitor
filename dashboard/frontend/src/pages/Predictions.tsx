/**
 * Predictions vs Actuals — Signal accuracy analysis with charts.
 * Shows how well the AI market analyst predicts price movements.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid,
  Cell, Line, AreaChart, Area,
} from 'recharts'
import { Target, TrendingUp, TrendingDown, Activity, Crosshair, BarChart2, Zap } from 'lucide-react'
import { fetchPredictionAccuracy, type PredictionAccuracyData } from '../api'
import StatCard from '../components/StatCard'
import { SkeletonStatCards, SkeletonBlock } from '../components/Skeleton'
import EmptyState from '../components/EmptyState'
import PageTransition from '../components/PageTransition'

const TIME_RANGES = [
  { label: '7d', days: 7 },
  { label: '30d', days: 30 },
  { label: '90d', days: 90 },
  { label: '1y', days: 365 },
]

const SIGNAL_COLORS: Record<string, string> = {
  strong_buy: '#22c55e',
  buy: '#4ade80',
  weak_buy: '#86efac',
  neutral: '#6b7280',
  weak_sell: '#fca5a5',
  sell: '#ef4444',
  strong_sell: '#dc2626',
}

const SIGNAL_LABELS: Record<string, string> = {
  strong_buy: 'Strong Buy',
  buy: 'Buy',
  weak_buy: 'Weak Buy',
  weak_sell: 'Weak Sell',
  sell: 'Sell',
  strong_sell: 'Strong Sell',
}

// ── Per-Pair Accuracy Heatmap ──────────────────────────────────────────────

function PairAccuracyGrid({ data }: { data: PredictionAccuracyData }) {
  const pairs = Object.entries(data.per_pair)
    .sort((a, b) => (b[1].total) - (a[1].total))
    .slice(0, 20)

  if (!pairs.length) return <EmptyState icon="chart" title="No pair data" description="Predictions will appear as the bot runs cycles." />

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-2">
      {pairs.map(([pair, stats]) => {
        const acc = stats.accuracy_24h_pct
        const bgColor = acc == null ? 'bg-gray-800/50' :
          acc >= 65 ? 'bg-green-900/40 border-green-700/50' :
          acc >= 50 ? 'bg-yellow-900/30 border-yellow-700/40' :
          'bg-red-900/30 border-red-700/40'
        const textColor = acc == null ? 'text-gray-500' :
          acc >= 65 ? 'text-green-400' :
          acc >= 50 ? 'text-yellow-400' :
          'text-red-400'

        return (
          <div key={pair} className={`rounded-lg border border-gray-800 p-3 ${bgColor} transition-colors`}>
            <p className="text-xs font-medium text-gray-300 truncate">{pair}</p>
            <p className={`text-lg font-bold ${textColor} mt-0.5`}>
              {acc != null ? `${acc}%` : '—'}
            </p>
            <p className="text-[10px] text-gray-500 mt-0.5">
              {stats.evaluated_24h}/{stats.total} evaluated
            </p>
          </div>
        )
      })}
    </div>
  )
}

// ── Confidence Calibration Chart ───────────────────────────────────────────

function CalibrationChart({ data }: { data: PredictionAccuracyData['confidence_calibration'] }) {
  if (!data.length) return null

  const chartData = data.map((b) => ({
    range: b.confidence_range,
    accuracy: b.accuracy_pct ?? 0,
    ideal: parseInt(b.confidence_range) + 10, // midpoint of bucket as ideal
    count: b.total,
  }))

  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={chartData} margin={{ top: 5, right: 10, bottom: 0, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
        <XAxis dataKey="range" tick={{ fontSize: 10, fill: '#6e7681' }} />
        <YAxis tick={{ fontSize: 10, fill: '#6e7681' }} tickFormatter={(v) => `${v}%`} domain={[0, 100]} />
        <Tooltip
          contentStyle={{ background: '#161b22', border: '1px solid #30363d', borderRadius: 8, fontSize: 12 }}
          formatter={(v: any, name?: string) => [`${Number(v ?? 0).toFixed(1)}%`, name === 'accuracy' ? 'Actual Accuracy' : 'Reference']}
        />
        <Bar dataKey="accuracy" name="Actual Accuracy" radius={[3, 3, 0, 0]}>
          {chartData.map((entry, i) => (
            <Cell key={i} fill={entry.accuracy >= 55 ? '#22c55e' : entry.accuracy >= 45 ? '#eab308' : '#ef4444'} opacity={0.85} />
          ))}
        </Bar>
        <Line type="monotone" dataKey="ideal" name="Reference" stroke="#6b7280" strokeDasharray="5 5" dot={false} />
      </BarChart>
    </ResponsiveContainer>
  )
}

// ── Daily Accuracy Trend ───────────────────────────────────────────────────

function DailyAccuracyChart({ data }: { data: PredictionAccuracyData['daily_accuracy'] }) {
  if (!data.length) return null

  const chartData = data.map((d) => ({
    date: dayjs(d.date).format('MMM DD'),
    accuracy: d.accuracy_pct ?? 0,
    total: d.total,
    correct: d.correct,
  }))

  return (
    <ResponsiveContainer width="100%" height={220}>
      <AreaChart data={chartData} margin={{ top: 5, right: 10, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id="accGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#3b82f6" stopOpacity={0.3} />
            <stop offset="100%" stopColor="#3b82f6" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
        <XAxis dataKey="date" tick={{ fontSize: 10, fill: '#6e7681' }} />
        <YAxis tick={{ fontSize: 10, fill: '#6e7681' }} tickFormatter={(v) => `${v}%`} domain={[0, 100]} />
        <Tooltip
          contentStyle={{ background: '#161b22', border: '1px solid #30363d', borderRadius: 8, fontSize: 12 }}
          formatter={(v: any, name?: string) => {
            if (name === 'accuracy') return [`${Number(v ?? 0).toFixed(1)}%`, 'Accuracy']
            return [v, name ?? '']
          }}
        />
        {/* 50% reference line */}
        <Area type="monotone" dataKey="accuracy" stroke="#3b82f6" strokeWidth={2} fill="url(#accGrad)" />
      </AreaChart>
    </ResponsiveContainer>
  )
}

// ── Signal Type Breakdown ──────────────────────────────────────────────────

function SignalTypeChart({ data }: { data: PredictionAccuracyData['by_signal_type'] }) {
  const entries = Object.entries(data).sort((a, b) => b[1].total - a[1].total)
  if (!entries.length) return null

  const chartData = entries.map(([type, stats]) => ({
    name: SIGNAL_LABELS[type] ?? type,
    accuracy: stats.accuracy_pct ?? 0,
    total: stats.total,
    color: SIGNAL_COLORS[type] ?? '#6b7280',
  }))

  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={chartData} layout="vertical" margin={{ top: 5, right: 10, bottom: 5, left: 70 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
        <XAxis type="number" tick={{ fontSize: 10, fill: '#6e7681' }} tickFormatter={(v) => `${v}%`} domain={[0, 100]} />
        <YAxis type="category" dataKey="name" tick={{ fontSize: 11, fill: '#d1d5db' }} width={65} />
        <Tooltip
          contentStyle={{ background: '#161b22', border: '1px solid #30363d', borderRadius: 8, fontSize: 12 }}
          formatter={(v: any) => [`${Number(v ?? 0).toFixed(1)}%`, 'Accuracy']}
        />
        <Bar dataKey="accuracy" radius={[0, 3, 3, 0]}>
          {chartData.map((entry, i) => (
            <Cell key={i} fill={entry.color} opacity={0.85} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

// ── Recent Predictions Table ───────────────────────────────────────────────

function RecentPredictions({ data }: { data: PredictionAccuracyData }) {
  const predictions = [...data.predictions].reverse().slice(0, 50)

  if (!predictions.length) return <EmptyState icon="chart" title="No predictions yet" description="AI signal predictions will appear as cycles run." />

  return (
    <div className="overflow-x-auto rounded-lg border border-gray-800">
      <table className="min-w-full text-xs">
        <thead>
          <tr className="border-b border-gray-800 bg-gray-900/50">
            {['Time', 'Pair', 'Signal', 'Conf.', 'Entry', 'TP', 'SL', '1h', '4h', '24h', '7d'].map((h) => (
              <th key={h} className="px-3 py-2 text-left font-medium text-gray-400">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {predictions.map((p, i) => (
            <tr key={i} className="border-b border-gray-800/50 hover:bg-gray-800/30 transition-colors">
              <td className="px-3 py-2 text-gray-400">{dayjs(p.ts).format('MMM DD HH:mm')}</td>
              <td className="px-3 py-2 font-medium text-gray-200">{p.pair}</td>
              <td className="px-3 py-2">
                <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold"
                  style={{ color: SIGNAL_COLORS[p.signal_type] ?? '#6b7280', background: `${SIGNAL_COLORS[p.signal_type] ?? '#6b7280'}15` }}>
                  {SIGNAL_LABELS[p.signal_type] ?? p.signal_type}
                </span>
              </td>
              <td className="px-3 py-2 text-gray-300">{(p.confidence * 100).toFixed(0)}%</td>
              <td className="px-3 py-2 text-gray-300 font-mono">{p.entry_price.toFixed(p.entry_price < 1 ? 6 : 2)}</td>
              <td className="px-3 py-2 text-green-400/70 font-mono">
                {p.suggested_tp ? (p.suggested_tp < 1 ? p.suggested_tp.toFixed(6) : p.suggested_tp.toFixed(2)) : '—'}
              </td>
              <td className="px-3 py-2 text-red-400/70 font-mono">
                {p.suggested_sl ? (p.suggested_sl < 1 ? p.suggested_sl.toFixed(6) : p.suggested_sl.toFixed(2)) : '—'}
              </td>
              {['1h', '4h', '24h', '7d'].map((h) => {
                const o = p.outcomes[h]
                if (!o) return <td key={h} className="px-3 py-2 text-gray-600">—</td>
                return (
                  <td key={h} className="px-3 py-2">
                    <span className={`inline-flex items-center gap-0.5 ${o.correct ? 'text-green-400' : 'text-red-400'}`}>
                      {o.correct ? '✓' : '✗'}
                      <span className="text-[10px] opacity-70">{o.pct_change > 0 ? '+' : ''}{o.pct_change.toFixed(2)}%</span>
                    </span>
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

// ── Main Page ──────────────────────────────────────────────────────────────

export default function Predictions() {
  const [days, setDays] = useState(30)

  const { data, isLoading } = useQuery({
    queryKey: ['prediction-accuracy', days],
    queryFn: () => fetchPredictionAccuracy(days),
    refetchInterval: 120_000,
  })

  const overall = data?.overall

  return (
    <PageTransition>
      <div className="p-6 space-y-6">
        {/* Header + time range */}
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-xl font-bold text-gray-100">Predictions vs Actuals</h2>
            <p className="text-xs text-gray-500 mt-0.5">How well does the AI predict price movements?</p>
          </div>
          <div className="flex gap-1">
            {TIME_RANGES.map((r) => (
              <button
                key={r.days}
                onClick={() => setDays(r.days)}
                className={`px-3 py-1.5 text-xs rounded-lg font-medium transition-colors ${
                  days === r.days
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
                label="Total Predictions"
                value={overall?.total?.toString() ?? '0'}
                accent="blue"
                icon={<Crosshair size={14} />}
                sub={`${overall?.evaluated_24h ?? 0} evaluated`}
              />
              <StatCard
                label="24h Accuracy"
                value={overall?.accuracy_24h_pct != null ? `${overall.accuracy_24h_pct}%` : '—'}
                accent={(overall?.accuracy_24h_pct ?? 0) >= 55 ? 'green' : (overall?.accuracy_24h_pct ?? 0) >= 45 ? 'gray' : 'red'}
                icon={<Target size={14} />}
                sub={`${overall?.correct_24h ?? 0}/${overall?.evaluated_24h ?? 0} correct`}
              />
              <StatCard
                label="1h Accuracy"
                value={overall?.accuracy_1h_pct != null ? `${overall.accuracy_1h_pct}%` : '—'}
                accent={(overall?.accuracy_1h_pct ?? 0) >= 55 ? 'green' : (overall?.accuracy_1h_pct ?? 0) >= 45 ? 'gray' : 'red'}
                icon={<Zap size={14} />}
                sub={`${overall?.correct_1h ?? 0}/${overall?.evaluated_1h ?? 0} correct`}
              />
              <StatCard
                label="Bullish Signals"
                value={data ? Object.entries(data.by_signal_type)
                  .filter(([k]) => ['strong_buy', 'buy', 'weak_buy'].includes(k))
                  .reduce((s, [, v]) => s + v.total, 0)
                  .toString() : '0'}
                accent="green"
                icon={<TrendingUp size={14} />}
              />
              <StatCard
                label="Bearish Signals"
                value={data ? Object.entries(data.by_signal_type)
                  .filter(([k]) => ['strong_sell', 'sell', 'weak_sell'].includes(k))
                  .reduce((s, [, v]) => s + v.total, 0)
                  .toString() : '0'}
                accent="red"
                icon={<TrendingDown size={14} />}
              />
              <StatCard
                label="Pairs Analyzed"
                value={data ? Object.keys(data.per_pair).length.toString() : '0'}
                accent="blue"
                icon={<BarChart2 size={14} />}
              />
            </>
          )}
        </div>

        {/* Charts row 1: Daily trend + Signal type breakdown */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-3">
              <Activity size={14} className="text-blue-400" />
              Daily Accuracy Trend
            </h3>
            {isLoading ? (
              <SkeletonBlock className="h-[220px]" />
            ) : data?.daily_accuracy?.length ? (
              <DailyAccuracyChart data={data.daily_accuracy} />
            ) : (
              <EmptyState icon="chart" title="No daily data" description="Accuracy data will appear after predictions are evaluated." />
            )}
          </div>

          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-3">
              <BarChart2 size={14} className="text-purple-400" />
              Accuracy by Signal Type
            </h3>
            {isLoading ? (
              <SkeletonBlock className="h-[220px]" />
            ) : data?.by_signal_type && Object.keys(data.by_signal_type).length ? (
              <SignalTypeChart data={data.by_signal_type} />
            ) : (
              <EmptyState icon="chart" title="No signal data" description="Signal type breakdown will appear as predictions accumulate." />
            )}
          </div>
        </div>

        {/* Charts row 2: Confidence calibration + Per-pair heatmap */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-3">
              <Target size={14} className="text-amber-400" />
              Confidence Calibration
            </h3>
            <p className="text-[10px] text-gray-600 mb-2">Bars = actual accuracy at each confidence level. Dashed = ideal (well-calibrated).</p>
            {isLoading ? (
              <SkeletonBlock className="h-[220px]" />
            ) : data?.confidence_calibration?.length ? (
              <CalibrationChart data={data.confidence_calibration} />
            ) : (
              <EmptyState icon="chart" title="No calibration data" description="Calibration data will appear as predictions are evaluated." />
            )}
          </div>

          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-3">
              <Crosshair size={14} className="text-teal-400" />
              Per-Pair Accuracy (24h)
            </h3>
            <p className="text-[10px] text-gray-600 mb-2">
              Green = ≥65% accurate · Yellow = 50-65% · Red = &lt;50%
            </p>
            {isLoading ? (
              <SkeletonBlock className="h-[220px]" />
            ) : data ? (
              <PairAccuracyGrid data={data} />
            ) : null}
          </div>
        </div>

        {/* Recent predictions table */}
        <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
          <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-3">
            <Crosshair size={14} className="text-cyan-400" />
            Recent Predictions
          </h3>
          {isLoading ? (
            <SkeletonBlock className="h-[300px]" />
          ) : data ? (
            <RecentPredictions data={data} />
          ) : null}
        </div>
      </div>
    </PageTransition>
  )
}
