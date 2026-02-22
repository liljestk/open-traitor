/**
 * Predictions vs Actuals — Signal accuracy analysis with charts.
 * Shows how well the AI market analyst predicts price movements.
 * Separated by asset class (Crypto / Equity).
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid,
  Cell, AreaChart, Area,
} from 'recharts'
import { Target, TrendingUp, TrendingDown, Activity, Crosshair, BarChart2, Zap, Clock, Layers } from 'lucide-react'
import {
  fetchPredictionAccuracy, fetchTrackedPairs,
  type PredictionAccuracyData, type TrackedPairsData,
} from '../api'
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

const ASSET_TABS = [
  { id: 'all', label: 'All Assets' },
  { id: 'crypto', label: 'Crypto' },
  { id: 'equity', label: 'Shares' },
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
  neutral: 'Neutral',
  weak_sell: 'Weak Sell',
  sell: 'Sell',
  strong_sell: 'Strong Sell',
}

// Classify a pair as crypto or equity
function classifyPair(pair: string): 'crypto' | 'equity' {
  const upper = pair.toUpperCase()
  if (upper.endsWith('-SEK') || upper.endsWith('-NOK') || upper.endsWith('-DKK')) return 'equity'
  return 'crypto'
}

// Filter predictions by asset class
function filterByAsset(data: PredictionAccuracyData, tab: string): PredictionAccuracyData {
  if (tab === 'all') return data

  const filteredPredictions = data.predictions.filter(p => classifyPair(p.pair) === tab)
  const filteredPerPair: typeof data.per_pair = {}
  for (const [pair, stats] of Object.entries(data.per_pair)) {
    if (classifyPair(pair) === tab) filteredPerPair[pair] = stats
  }

  // Recompute overall from filtered
  const overall = { total: 0, correct_24h: 0, evaluated_24h: 0, correct_1h: 0, evaluated_1h: 0, accuracy_24h_pct: null as number | null, accuracy_1h_pct: null as number | null }
  for (const p of filteredPredictions) {
    overall.total++
    if (p.outcomes['24h']) { overall.evaluated_24h++; if (p.outcomes['24h'].correct) overall.correct_24h++ }
    if (p.outcomes['1h']) { overall.evaluated_1h++; if (p.outcomes['1h'].correct) overall.correct_1h++ }
  }
  overall.accuracy_24h_pct = overall.evaluated_24h ? Math.round(overall.correct_24h / overall.evaluated_24h * 1000) / 10 : null
  overall.accuracy_1h_pct = overall.evaluated_1h ? Math.round(overall.correct_1h / overall.evaluated_1h * 1000) / 10 : null

  // Recompute by_signal_type from filtered
  const bySignal: typeof data.by_signal_type = {}
  for (const p of filteredPredictions) {
    if (!bySignal[p.signal_type]) bySignal[p.signal_type] = { total: 0, correct_24h: 0, evaluated_24h: 0, accuracy_pct: null }
    bySignal[p.signal_type].total++
    if (p.outcomes['24h']) { bySignal[p.signal_type].evaluated_24h++; if (p.outcomes['24h'].correct) bySignal[p.signal_type].correct_24h++ }
  }
  for (const st of Object.keys(bySignal)) {
    const s = bySignal[st]
    s.accuracy_pct = s.evaluated_24h ? Math.round(s.correct_24h / s.evaluated_24h * 1000) / 10 : null
  }

  return {
    predictions: filteredPredictions,
    per_pair: filteredPerPair,
    overall,
    by_signal_type: bySignal,
    confidence_calibration: data.confidence_calibration,
    daily_accuracy: data.daily_accuracy,
  }
}

// ── LLM-Tracked Pairs Section ──────────────────────────────────────────────

function TrackedPairsSection({ data, assetTab }: { data: TrackedPairsData; assetTab: string }) {
  const pairs = assetTab === 'equity' ? data.equity
    : assetTab === 'crypto' ? data.crypto
    : [...data.crypto, ...data.equity]

  if (!pairs.length) {
    return (
      <EmptyState
        icon="chart"
        title={assetTab === 'equity' ? 'No equity pairs tracked' : 'No pairs tracked yet'}
        description="The AI will start tracking pairs as it runs analysis cycles."
      />
    )
  }

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-2">
      {pairs.sort((a, b) => b.prediction_count - a.prediction_count).map((p) => {
        const isEquity = classifyPair(p.pair) === 'equity'
        return (
          <div key={p.pair} className="rounded-lg border border-gray-800 bg-gray-900/40 p-2.5 hover:border-gray-700 transition-colors">
            <div className="flex items-center gap-1.5 mb-1">
              <span className={`w-1.5 h-1.5 rounded-full ${isEquity ? 'bg-blue-400' : 'bg-green-400'}`} />
              <span className="text-xs font-medium text-gray-200 truncate">{p.pair}</span>
            </div>
            <p className="text-[10px] text-gray-500">
              {p.prediction_count} predictions
            </p>
            <p className="text-[10px] text-gray-600">
              Last: {dayjs(p.last_predicted).fromNow?.() ?? dayjs(p.last_predicted).format('MMM DD HH:mm')}
            </p>
          </div>
        )
      })}
    </div>
  )
}

// ── Per-Pair Accuracy Heatmap ──────────────────────────────────────────────

function PairAccuracyGrid({ data }: { data: PredictionAccuracyData }) {
  const pairs = Object.entries(data.per_pair)
    .sort((a, b) => (b[1].total) - (a[1].total))
    .slice(0, 24)

  if (!pairs.length) return <EmptyState icon="chart" title="No pair data" description="Predictions will appear as the bot runs cycles." />

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-2">
      {pairs.map(([pair, stats]) => {
        // Prefer 24h accuracy, fall back to 1h
        const acc = stats.accuracy_24h_pct ?? stats.accuracy_1h_pct
        const horizon = stats.accuracy_24h_pct != null ? '24h' : '1h'
        const evaluated = stats.accuracy_24h_pct != null ? stats.evaluated_24h : stats.evaluated_1h
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
              {evaluated}/{stats.total} eval ({horizon})
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
          formatter={(v: any) => [`${Number(v ?? 0).toFixed(1)}%`, 'Actual Accuracy']}
        />
        <Bar dataKey="accuracy" name="Actual Accuracy" radius={[3, 3, 0, 0]}>
          {chartData.map((entry, i) => (
            <Cell key={i} fill={entry.accuracy >= 55 ? '#22c55e' : entry.accuracy >= 45 ? '#eab308' : '#ef4444'} opacity={0.85} />
          ))}
        </Bar>
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
    evaluated: d.evaluated,
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
            {['Time', 'Pair', 'Type', 'Signal', 'Conf.', 'Entry', 'TP', 'SL', '1h', '4h', '24h', '7d'].map((h) => (
              <th key={h} className="px-2.5 py-2 text-left font-medium text-gray-400">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {predictions.map((p, i) => (
            <tr key={i} className="border-b border-gray-800/50 hover:bg-gray-800/30 transition-colors">
              <td className="px-2.5 py-2 text-gray-400">{dayjs(p.ts).format('MMM DD HH:mm')}</td>
              <td className="px-2.5 py-2 font-medium text-gray-200">{p.pair}</td>
              <td className="px-2.5 py-2">
                <span className={`inline-block w-1.5 h-1.5 rounded-full mr-1 ${classifyPair(p.pair) === 'equity' ? 'bg-blue-400' : 'bg-green-400'}`} />
                <span className="text-[10px] text-gray-500">{classifyPair(p.pair) === 'equity' ? 'EQ' : 'CR'}</span>
              </td>
              <td className="px-2.5 py-2">
                <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold"
                  style={{ color: SIGNAL_COLORS[p.signal_type] ?? '#6b7280', background: `${SIGNAL_COLORS[p.signal_type] ?? '#6b7280'}15` }}>
                  {SIGNAL_LABELS[p.signal_type] ?? p.signal_type}
                </span>
              </td>
              <td className="px-2.5 py-2 text-gray-300">{(p.confidence * 100).toFixed(0)}%</td>
              <td className="px-2.5 py-2 text-gray-300 font-mono">{p.entry_price.toFixed(p.entry_price < 1 ? 6 : 2)}</td>
              <td className="px-2.5 py-2 text-green-400/70 font-mono">
                {p.suggested_tp ? (p.suggested_tp < 1 ? p.suggested_tp.toFixed(6) : p.suggested_tp.toFixed(2)) : '—'}
              </td>
              <td className="px-2.5 py-2 text-red-400/70 font-mono">
                {p.suggested_sl ? (p.suggested_sl < 1 ? p.suggested_sl.toFixed(6) : p.suggested_sl.toFixed(2)) : '—'}
              </td>
              {['1h', '4h', '24h', '7d'].map((h) => {
                const o = p.outcomes[h]
                if (!o) return (
                  <td key={h} className="px-2.5 py-2">
                    <span className="text-gray-600 text-[10px] flex items-center gap-0.5">
                      <Clock size={8} className="opacity-50" />pending
                    </span>
                  </td>
                )
                return (
                  <td key={h} className="px-2.5 py-2">
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
  const [assetTab, setAssetTab] = useState('all')

  const { data: rawData, isLoading } = useQuery({
    queryKey: ['prediction-accuracy', days],
    queryFn: () => fetchPredictionAccuracy(days),
    refetchInterval: 120_000,
  })

  const { data: trackedPairs } = useQuery({
    queryKey: ['tracked-pairs'],
    queryFn: fetchTrackedPairs,
    refetchInterval: 300_000,
  })

  const data = rawData ? filterByAsset(rawData, assetTab) : undefined
  const overall = data?.overall

  // Count pending (unevaluated) predictions
  const pendingCount = data ? data.predictions.filter(p => !p.outcomes['1h']).length : 0

  return (
    <PageTransition>
      <div className="p-6 space-y-6">
        {/* Header + controls */}
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
          <div>
            <h2 className="text-xl font-bold text-gray-100">Predictions vs Actuals</h2>
            <p className="text-xs text-gray-500 mt-0.5">
              Live AI signal accuracy — data from {rawData?.overall?.total ?? 0} real predictions
            </p>
          </div>
          <div className="flex gap-2 flex-wrap">
            {/* Asset class tabs */}
            <div className="flex gap-0.5 bg-gray-800/50 rounded-lg p-0.5">
              {ASSET_TABS.map((t) => (
                <button
                  key={t.id}
                  onClick={() => setAssetTab(t.id)}
                  className={`px-2.5 py-1 text-[11px] rounded-md font-medium transition-colors ${
                    assetTab === t.id
                      ? 'bg-gray-700 text-gray-100'
                      : 'text-gray-500 hover:text-gray-300'
                  }`}
                >
                  {t.label}
                </button>
              ))}
            </div>
            {/* Time range */}
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
                sub={pendingCount > 0 ? `${pendingCount} pending eval` : `${overall?.evaluated_1h ?? 0} evaluated`}
              />
              <StatCard
                label="24h Accuracy"
                value={overall?.accuracy_24h_pct != null ? `${overall.accuracy_24h_pct}%` : '—'}
                accent={(overall?.accuracy_24h_pct ?? 0) >= 55 ? 'green' : 'red'}
                icon={<Target size={14} />}
                sub={`${overall?.correct_24h ?? 0}/${overall?.evaluated_24h ?? 0} correct`}
              />
              <StatCard
                label="1h Accuracy"
                value={overall?.accuracy_1h_pct != null ? `${overall.accuracy_1h_pct}%` : '—'}
                accent={(overall?.accuracy_1h_pct ?? 0) >= 55 ? 'green' : 'red'}
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

        {/* LLM-Tracked Pairs */}
        <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
          <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-1">
            <Layers size={14} className="text-violet-400" />
            AI-Tracked Pairs
            {trackedPairs && (
              <span className="text-[10px] font-normal text-gray-600">
                {trackedPairs.crypto.length} crypto · {trackedPairs.equity.length} equity
              </span>
            )}
          </h3>
          <p className="text-[10px] text-gray-600 mb-3">
            Pairs the LLM system has autonomously chosen to analyze and predict.
            <span className="inline-flex items-center gap-1 ml-2">
              <span className="w-1.5 h-1.5 rounded-full bg-green-400 inline-block" /> Crypto
              <span className="w-1.5 h-1.5 rounded-full bg-blue-400 inline-block ml-1" /> Equity
            </span>
          </p>
          {trackedPairs ? (
            <TrackedPairsSection data={trackedPairs} assetTab={assetTab} />
          ) : (
            <SkeletonBlock className="h-[100px]" />
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
            <p className="text-[10px] text-gray-600 mb-2">Bars = actual accuracy at each confidence level.</p>
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
              Per-Pair Accuracy
            </h3>
            <p className="text-[10px] text-gray-600 mb-2">
              Green = ≥65% · Yellow = 50-65% · Red = &lt;50% · Best available horizon shown.
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
          <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-1">
            <Crosshair size={14} className="text-cyan-400" />
            Recent Predictions
          </h3>
          <p className="text-[10px] text-gray-600 mb-3">
            Live predictions from the AI market analyst. Outcomes update automatically as time passes.
            <Clock size={8} className="inline ml-1 opacity-50" /> = awaiting evaluation window.
          </p>
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
