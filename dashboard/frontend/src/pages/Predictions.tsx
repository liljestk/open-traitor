/**
 * Predictions vs Actuals — Signal accuracy analysis with charts.
 * Shows how well the AI market analyst predicts price movements.
 * Separated by asset class (Crypto / Equity).
 * Includes per-pair prediction overlay chart and full Trader's View.
 */
import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid,
  Cell, AreaChart, Area, ComposedChart, Scatter, ReferenceLine,
} from 'recharts'
import {
  Target, TrendingUp, TrendingDown, Activity, Crosshair, BarChart2,
  Zap, Clock, Layers, Eye, Search, Newspaper, Shield, Brain,
  ArrowUpRight, ArrowDownRight, Minus, ExternalLink, DollarSign, X,
} from 'lucide-react'
import {
  fetchPredictionAccuracy, fetchTrackedPairs, fetchPairPredictionHistory,
  fetchCycles, fetchCycleFull, fetchTrades, fetchNews, fetchPortfolioExposure,
  fetchMarketPrice,
  type PredictionAccuracyData, type TrackedPairsData,
  type CycleFull, type NewsArticle,
} from '../api'
import StatCard from '../components/StatCard'
import { SkeletonStatCards, SkeletonBlock } from '../components/Skeleton'
import EmptyState from '../components/EmptyState'
import PageTransition from '../components/PageTransition'

dayjs.extend(relativeTime)

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

// Well-known crypto base symbols
const KNOWN_CRYPTO = new Set([
  'BTC', 'ETH', 'SOL', 'XRP', 'ADA', 'DOGE', 'DOT', 'AVAX', 'MATIC',
  'LINK', 'UNI', 'SHIB', 'LTC', 'BCH', 'ATOM', 'FIL', 'NEAR', 'APT',
  'ARB', 'OP', 'ICP', 'XLM', 'ALGO', 'AAVE', 'CRV', 'MKR', 'COMP',
  'SNX', 'PEPE', 'BONK', 'WIF', 'JUP', 'RENDER', 'FET', 'TAO', 'USDT',
  'USDC', 'DAI', 'BUSD', 'TUSD', 'RNDR', 'GRT', 'SUI', 'SEI', 'TIA',
  'INJ', 'PYTH', 'STX', 'HBAR', 'VET', 'EOS', 'TRX', 'XMR', 'ZEC',
])

// Equity-associated quote currencies
const EQUITY_QUOTES = new Set(['SEK', 'NOK', 'DKK', 'GBP', 'CHF'])

// Classify a pair as crypto or equity
function classifyPair(pair: string): 'crypto' | 'equity' {
  const upper = pair.toUpperCase()
  const parts = upper.split('-')
  const base = parts[0] ?? ''
  const quote = parts[1] ?? ''

  // Scandinavian / European equity exchange currencies → always equity
  if (EQUITY_QUOTES.has(quote)) return 'equity'
  // Known crypto symbols → crypto
  if (KNOWN_CRYPTO.has(base)) return 'crypto'
  // Short all-letter ticker not known as crypto → likely equity (e.g. AAPL-USD, MSFT-EUR)
  if (/^[A-Z]{1,5}$/.test(base)) return 'equity'
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

function TrackedPairsSection({ data, assetTab, onSelectPair, selectedPair }: {
  data: TrackedPairsData; assetTab: string; onSelectPair: (pair: string) => void; selectedPair: string | null
}) {
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
        const isSelected = selectedPair === p.pair
        return (
          <button
            key={p.pair}
            onClick={() => onSelectPair(p.pair)}
            className={`rounded-lg border p-2.5 transition-colors text-left ${isSelected
              ? 'border-brand-500 bg-brand-900/30 ring-1 ring-brand-500/50'
              : 'border-gray-800 bg-gray-900/40 hover:border-gray-700'
              }`}
          >
            <div className="flex items-center gap-1.5 mb-1">
              <span className={`w-1.5 h-1.5 rounded-full ${isEquity ? 'bg-blue-400' : 'bg-green-400'}`} />
              <span className="text-xs font-medium text-gray-200 truncate">{p.pair}</span>
              {isSelected && <Eye size={10} className="text-brand-400 ml-auto flex-shrink-0" />}
            </div>
            <p className="text-[10px] text-gray-500">
              {p.prediction_count} predictions
            </p>
            <p className="text-[10px] text-gray-600">
              Last: {dayjs(p.last_predicted).fromNow()}
            </p>
          </button>
        )
      })}
    </div>
  )
}

// ── Prediction Overlay Chart ───────────────────────────────────────────────

const OVERLAY_TIME_RANGES = [
  { label: '1d', days: 1 },
  { label: '7d', days: 7 },
  { label: '30d', days: 30 },
  { label: '90d', days: 90 },
]

function PredictionOverlayChart({ pair }: { pair: string }) {
  const [overlayDays, setOverlayDays] = useState(7)

  const { data: history, isLoading } = useQuery({
    queryKey: ['pair-prediction-history', pair, overlayDays],
    queryFn: () => fetchPairPredictionHistory(pair, overlayDays),
    enabled: !!pair,
    refetchInterval: 120_000,
  })

  if (isLoading) return <SkeletonBlock className="h-[350px]" />

  if (!history || !history.price_history.length) {
    return <EmptyState icon="chart" title={`No price data for ${pair}`} description="Price history will appear as the bot collects data." />
  }

  // Build merged chart data: prices + prediction markers
  type ChartPoint = {
    ts: string
    fullTs: string
    price: number | undefined
    forecastPrice: number | undefined
    forecastHigh: number | undefined
    forecastLow: number | undefined
    buyMarker: number | undefined
    sellMarker: number | undefined
    isForecast?: boolean
  }

  const chartData: ChartPoint[] = history.price_history.map((ph) => ({
    ts: dayjs(ph.ts).format(overlayDays <= 1 ? 'HH:mm' : overlayDays <= 7 ? 'ddd HH:mm' : 'MMM DD'),
    fullTs: ph.ts,
    price: ph.price,
    forecastPrice: undefined,
    forecastHigh: undefined,
    forecastLow: undefined,
    buyMarker: undefined,
    sellMarker: undefined,
  }))

  // Overlay prediction markers on closest price points
  for (const pred of history.predictions) {
    const predTime = dayjs(pred.ts)
    let closestIdx = 0
    let closestDiff = Infinity
    for (let i = 0; i < chartData.length; i++) {
      const diff = Math.abs(dayjs(chartData[i].fullTs).diff(predTime, 'minute'))
      if (diff < closestDiff) {
        closestDiff = diff
        closestIdx = i
      }
    }
    if (closestDiff < 120) { // within 2 hours
      if (pred.is_bullish) {
        chartData[closestIdx].buyMarker = pred.entry_price
      } else {
        chartData[closestIdx].sellMarker = pred.entry_price
      }
    }
  }

  // Stats for this pair
  const bullish = history.predictions.filter(p => p.is_bullish).length
  const bearish = history.predictions.filter(p => !p.is_bullish).length
  const evaluatedPreds = history.predictions.filter(p => p.outcomes['24h'] || p.outcomes['1h'])
  const correctPreds = evaluatedPreds.filter(p =>
    (p.outcomes['24h']?.correct) || (p.outcomes['1h']?.correct)
  )
  const accuracy = evaluatedPreds.length > 0
    ? Math.round(correctPreds.length / evaluatedPreds.length * 1000) / 10
    : null

  // TP/SL reference lines from the latest prediction
  const latestPred = history.predictions[history.predictions.length - 1]

  // ── Future Forecast Projection ─────────────────────────────────────────
  // Extend the chart beyond the last known price toward the predicted target.
  // Shows a dashed forecast line with a TP/SL confidence band.
  const lastPrice = chartData.length > 0 ? chartData[chartData.length - 1].price : undefined
  const lastTs = chartData.length > 0 ? chartData[chartData.length - 1].fullTs : undefined
  const hasForecast = !!(latestPred && lastPrice && lastTs && (latestPred.suggested_tp || latestPred.suggested_sl))

  if (hasForecast && lastPrice && lastTs) {
    const tp = latestPred!.suggested_tp
    const sl = latestPred!.suggested_sl
    const isBullish = latestPred!.is_bullish
    const confidence = latestPred!.confidence ?? 0.5

    // Target price = TP if bullish, SL if bearish (direction of predicted move)
    const targetPrice = isBullish
      ? (tp ?? lastPrice * (1 + 0.02 * confidence))
      : (sl ?? lastPrice * (1 - 0.02 * confidence))

    // Number of forecast steps based on time range
    const forecastSteps = overlayDays <= 1 ? 6 : overlayDays <= 7 ? 8 : 6
    // Time step in hours
    const hoursPerStep = overlayDays <= 1 ? 1 : overlayDays <= 7 ? 3 : 24

    // Bridge point: last actual price also appears as first forecast point
    const bridgeTs = dayjs(lastTs)
    chartData[chartData.length - 1].forecastPrice = lastPrice

    for (let i = 1; i <= forecastSteps; i++) {
      const t = i / forecastSteps
      const futureTs = bridgeTs.add(hoursPerStep * i, 'hour')
      const forecastPrice = lastPrice + (targetPrice - lastPrice) * t

      // Widen the band as we go further into the future
      const bandSpread = Math.abs(
        ((tp ?? lastPrice * 1.02) - (sl ?? lastPrice * 0.98))
      ) * 0.5 * t + Math.abs(targetPrice - lastPrice) * 0.1 * t
      const highBound = tp
        ? Math.max(forecastPrice + bandSpread * 0.3, isBullish ? forecastPrice : forecastPrice + bandSpread)
        : forecastPrice + bandSpread
      const lowBound = sl
        ? Math.min(forecastPrice - bandSpread * 0.3, isBullish ? forecastPrice - bandSpread : forecastPrice)
        : forecastPrice - bandSpread

      chartData.push({
        ts: futureTs.format(overlayDays <= 1 ? 'HH:mm' : overlayDays <= 7 ? 'ddd HH:mm' : 'MMM DD'),
        fullTs: futureTs.toISOString(),
        price: undefined,
        forecastPrice: Math.round(forecastPrice * 1e8) / 1e8,
        forecastHigh: Math.round(highBound * 1e8) / 1e8,
        forecastLow: Math.round(lowBound * 1e8) / 1e8,
        buyMarker: undefined,
        sellMarker: undefined,
        isForecast: true,
      })
    }
  }

  // Price range for Y axis domain — include forecast band if present
  const allValues = chartData.flatMap(d => [d.price, d.forecastPrice, d.forecastHigh, d.forecastLow].filter((v): v is number => v != null && v > 0))
  const minPrice = Math.min(...allValues) * 0.997
  const maxPrice = Math.max(...allValues) * 1.003

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-3">
          <div className="flex gap-1">
            {OVERLAY_TIME_RANGES.map((r) => (
              <button
                key={r.days}
                onClick={() => setOverlayDays(r.days)}
                className={`px-2.5 py-1 text-[11px] rounded-md font-medium transition-colors ${overlayDays === r.days
                  ? 'bg-brand-600/30 text-brand-400 border border-brand-600/50'
                  : 'bg-gray-800/50 text-gray-500 border border-gray-800 hover:border-gray-700'
                  }`}
              >
                {r.label}
              </button>
            ))}
          </div>
          <span className="text-[10px] text-gray-500">
            {history.total_predictions} predictions · {accuracy != null ? `${accuracy}% accurate` : 'pending eval'}
          </span>
        </div>
        <div className="flex items-center gap-3 text-[10px]">
          <span className="flex items-center gap-1">
            <span className="w-2 h-2 rounded-full bg-green-500" /> Buy ({bullish})
          </span>
          <span className="flex items-center gap-1">
            <span className="w-2 h-2 rounded-full bg-red-500" /> Sell ({bearish})
          </span>
          {hasForecast && (
            <span className="flex items-center gap-1">
              <span className="w-2 h-2 rounded-sm bg-amber-500/60" /> Forecast
            </span>
          )}
        </div>
      </div>

      <ResponsiveContainer width="100%" height={320}>
        <ComposedChart data={chartData} margin={{ top: 10, right: 15, bottom: 0, left: 5 }}>
          <defs>
            <linearGradient id="priceGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#3b82f6" stopOpacity={0.2} />
              <stop offset="100%" stopColor="#3b82f6" stopOpacity={0} />
            </linearGradient>
            <linearGradient id="forecastBandGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#f59e0b" stopOpacity={0.15} />
              <stop offset="50%" stopColor="#f59e0b" stopOpacity={0.08} />
              <stop offset="100%" stopColor="#f59e0b" stopOpacity={0.15} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
          <XAxis
            dataKey="ts"
            tick={{ fontSize: 9, fill: '#6e7681' }}
            interval="preserveStartEnd"
          />
          <YAxis
            tick={{ fontSize: 9, fill: '#6e7681' }}
            domain={[minPrice, maxPrice]}
            tickFormatter={(v: number) => v < 1 ? v.toFixed(4) : v < 100 ? v.toFixed(2) : v.toFixed(0)}
          />
          <Tooltip
            contentStyle={{ background: '#161b22', border: '1px solid #30363d', borderRadius: 8, fontSize: 12 }}
            formatter={(value: any, name: string | undefined) => {
              const fmt = (v: number) => v < 1 ? v.toFixed(6) : v.toFixed(2)
              if (name === 'price') return [typeof value === 'number' ? fmt(value) : value, 'Price']
              if (name === 'forecastPrice') return [typeof value === 'number' ? fmt(value) : value, 'Forecast']
              if (name === 'forecastHigh') return [typeof value === 'number' ? fmt(value) : value, 'Forecast High']
              if (name === 'forecastLow') return [typeof value === 'number' ? fmt(value) : value, 'Forecast Low']
              if (name === 'buyMarker') return [typeof value === 'number' ? fmt(value) : value, 'Buy Signal']
              if (name === 'sellMarker') return [typeof value === 'number' ? fmt(value) : value, 'Sell Signal']
              return [value, name]
            }}
          />
          {latestPred?.suggested_tp && (
            <ReferenceLine
              y={latestPred.suggested_tp}
              stroke="#22c55e"
              strokeDasharray="5 5"
              strokeOpacity={0.5}
              label={{ value: 'TP', fill: '#22c55e', fontSize: 9, position: 'right' }}
            />
          )}
          {latestPred?.suggested_sl && (
            <ReferenceLine
              y={latestPred.suggested_sl}
              stroke="#ef4444"
              strokeDasharray="5 5"
              strokeOpacity={0.5}
              label={{ value: 'SL', fill: '#ef4444', fontSize: 9, position: 'right' }}
            />
          )}
          {/* Actual price line */}
          <Area
            type="monotone"
            dataKey="price"
            stroke="#3b82f6"
            strokeWidth={1.5}
            fill="url(#priceGrad)"
            dot={false}
            connectNulls={false}
          />
          {/* Forecast confidence band (TP/SL range) */}
          {hasForecast && (
            <Area
              type="monotone"
              dataKey="forecastHigh"
              stroke="none"
              fill="url(#forecastBandGrad)"
              dot={false}
              connectNulls={false}
              activeDot={false}
            />
          )}
          {hasForecast && (
            <Area
              type="monotone"
              dataKey="forecastLow"
              stroke="none"
              fill="#161b22"
              dot={false}
              connectNulls={false}
              activeDot={false}
            />
          )}
          {/* Forecast price line (dashed) */}
          {hasForecast && (
            <Area
              type="monotone"
              dataKey="forecastPrice"
              stroke="#f59e0b"
              strokeWidth={2}
              strokeDasharray="6 3"
              fill="none"
              dot={false}
              connectNulls={false}
            />
          )}
          <Scatter
            dataKey="buyMarker"
            fill="#22c55e"
            shape="triangle"
          />
          <Scatter
            dataKey="sellMarker"
            fill="#ef4444"
            shape="diamond"
          />
        </ComposedChart>
      </ResponsiveContainer>

      {/* Forecast summary below chart */}
      {hasForecast && latestPred && (
        <div className="mt-2 flex items-center gap-4 text-[10px] text-gray-500 border-t border-gray-800/50 pt-2">
          <span className="flex items-center gap-1">
            <span className={`w-1.5 h-1.5 rounded-full ${latestPred.is_bullish ? 'bg-green-500' : 'bg-red-500'}`} />
            Latest: <span className="text-gray-300 font-medium">{latestPred.is_bullish ? 'Bullish' : 'Bearish'}</span>
            <span className="text-gray-600">({(latestPred.confidence * 100).toFixed(0)}% conf)</span>
          </span>
          {latestPred.suggested_tp && (
            <span>Target: <span className="text-green-400 font-mono">{latestPred.suggested_tp < 1 ? latestPred.suggested_tp.toFixed(6) : latestPred.suggested_tp.toFixed(2)}</span></span>
          )}
          {latestPred.suggested_sl && (
            <span>Stop: <span className="text-red-400 font-mono">{latestPred.suggested_sl < 1 ? latestPred.suggested_sl.toFixed(6) : latestPred.suggested_sl.toFixed(2)}</span></span>
          )}
          <span className="text-gray-600 italic ml-auto">Dashed line = AI forecast projection</span>
        </div>
      )}
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

// ── Trader's View Components ───────────────────────────────────────────────

/** Extract base ticker from pair, e.g. "BTC" from "BTC-USD" */
function pairBaseTicker(pair: string): string {
  return pair.split('-')[0].toUpperCase()
}

/** Parse technical indicators from market_analyst raw_prompt text */
function parseIndicatorsFromPrompt(rawPrompt: string | null): Record<string, string> | null {
  if (!rawPrompt) return null
  const indicators: Record<string, string> = {}
  const patterns: [string, RegExp][] = [
    ['rsi', /RSI:\s*([^\n]+)/i],
    ['macd', /MACD:\s*([^\n]+)/i],
    ['bb', /Bollinger Bands:\s*([^\n]+)/i],
    ['ema_signal', /EMA Signal:\s*([^\n]+)/i],
    ['ema_values', /EMA 9:\s*([^\n]+)/i],
    ['volume', /Volume:\s*([^\n]+)/i],
    ['support_resistance', /Support:\s*([^\n]+)/i],
    ['atr', /ATR:\s*([^\n]+)/i],
    ['price_1h', /1 hour:\s*([^\n]+)/i],
    ['price_24h', /24 hours:\s*([^\n]+)/i],
  ]
  for (const [key, re] of patterns) {
    const m = rawPrompt.match(re)
    if (m) indicators[key] = m[1].trim()
  }
  return Object.keys(indicators).length > 0 ? indicators : null
}

/** Determine colour for an indicator signal string */
function indicatorColor(text: string): string {
  const t = text.toLowerCase()
  if (t.includes('oversold') || t.includes('strongly_bullish') || t.includes('strong_buy')) return 'text-green-400'
  if (t.includes('bullish') || t.includes('buy')) return 'text-green-400/80'
  if (t.includes('overbought') || t.includes('strongly_bearish') || t.includes('strong_sell')) return 'text-red-400'
  if (t.includes('bearish') || t.includes('sell')) return 'text-red-400/80'
  return 'text-gray-400'
}

function indicatorBg(text: string): string {
  const t = text.toLowerCase()
  if (t.includes('oversold') || t.includes('strongly_bullish')) return 'bg-green-900/30 border-green-800/50'
  if (t.includes('bullish') || t.includes('buy')) return 'bg-green-900/20 border-green-800/30'
  if (t.includes('overbought') || t.includes('strongly_bearish')) return 'bg-red-900/30 border-red-800/50'
  if (t.includes('bearish') || t.includes('sell')) return 'bg-red-900/20 border-red-800/30'
  return 'bg-gray-800/30 border-gray-700/30'
}

// ── Trader View Header ─────────────────────────────────────────────────────

function TraderViewHeader({ pair, onClose }: {
  pair: string; onClose: () => void
}) {
  const { data: priceData } = useQuery({
    queryKey: ['market-price', pair],
    queryFn: () => fetchMarketPrice(pair),
    enabled: !!pair,
    refetchInterval: 30_000,
  })

  const { data: exposure } = useQuery({
    queryKey: ['portfolio-exposure-trader'],
    queryFn: fetchPortfolioExposure,
    enabled: !!pair,
    refetchInterval: 60_000,
  })

  const position = useMemo(() => {
    if (!exposure?.exposure?.breakdown) return null
    return exposure.exposure.breakdown.find(
      (b) => b.pair.toUpperCase() === pair.toUpperCase()
    )
  }, [exposure, pair])

  const fmtPrice = (v: number) => v < 1 ? v.toFixed(6) : v < 100 ? v.toFixed(2) : v.toLocaleString(undefined, { maximumFractionDigits: 2 })

  return (
    <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <span className={`w-2.5 h-2.5 rounded-full ${classifyPair(pair) === 'equity' ? 'bg-blue-400' : 'bg-green-400'}`} />
            <h3 className="text-lg font-bold text-gray-100">{pair}</h3>
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-500 font-medium">
              {classifyPair(pair) === 'equity' ? 'EQUITY' : 'CRYPTO'}
            </span>
          </div>
          {priceData && (
            <span className="text-xl font-bold text-gray-200 font-mono">
              {fmtPrice(priceData.price)}
            </span>
          )}
        </div>
        <button
          onClick={onClose}
          className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-gray-300 px-3 py-1.5 rounded-lg border border-gray-800 hover:border-gray-700 transition-colors"
        >
          <X size={12} />
          Close
        </button>
      </div>

      {/* Position stats row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="rounded-lg border border-gray-800 bg-gray-900/60 p-3">
          <p className="text-[10px] text-gray-500 font-medium uppercase tracking-wider mb-0.5">Live Price</p>
          <p className="text-lg font-bold text-gray-200 font-mono">
            {priceData ? fmtPrice(priceData.price) : '—'}
          </p>
        </div>
        {position ? (
          <>
            <div className="rounded-lg border border-gray-800 bg-gray-900/60 p-3">
              <p className="text-[10px] text-gray-500 font-medium uppercase tracking-wider mb-0.5">Position</p>
              <p className="text-lg font-bold text-gray-200 font-mono">{position.quantity.toFixed(position.quantity < 1 ? 6 : 2)}</p>
              <p className="text-[10px] text-gray-500">Entry: {fmtPrice(position.entry_price)}</p>
            </div>
            <div className="rounded-lg border border-gray-800 bg-gray-900/60 p-3">
              <p className="text-[10px] text-gray-500 font-medium uppercase tracking-wider mb-0.5">PnL</p>
              <p className={`text-lg font-bold font-mono ${position.pnl_pct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                {position.pnl_pct >= 0 ? '+' : ''}{position.pnl_pct.toFixed(2)}%
              </p>
              <p className="text-[10px] text-gray-500">Value: {position.value.toFixed(2)}</p>
            </div>
            <div className="rounded-lg border border-gray-800 bg-gray-900/60 p-3">
              <p className="text-[10px] text-gray-500 font-medium uppercase tracking-wider mb-0.5">Allocation</p>
              <p className="text-lg font-bold text-brand-400 font-mono">{position.pct_of_portfolio.toFixed(1)}%</p>
              <p className="text-[10px] text-gray-500">of portfolio</p>
            </div>
          </>
        ) : (
          <div className="col-span-3 rounded-lg border border-gray-800 bg-gray-900/60 p-3 flex items-center">
            <p className="text-xs text-gray-500">No open position for {pairBaseTicker(pair)}</p>
          </div>
        )}
      </div>
    </div>
  )
}

// ── Technical Indicators Panel ──────────────────────────────────────────────

function TechnicalIndicatorsPanel({ indicators }: { indicators: Record<string, string> }) {
  const items: { label: string; key: string; icon: React.ReactNode; hint: string }[] = [
    { label: 'RSI', key: 'rsi', icon: <Activity size={12} />, hint: 'Relative Strength Index — below 30 = oversold (buy signal), above 70 = overbought (sell signal)' },
    { label: 'MACD', key: 'macd', icon: <BarChart2 size={12} />, hint: 'Moving Average Convergence/Divergence — shows momentum direction and trend changes' },
    { label: 'Bollinger', key: 'bb', icon: <Layers size={12} />, hint: 'Bollinger Bands — price near lower band suggests undervalued, near upper band suggests overvalued' },
    { label: 'EMA Signal', key: 'ema_signal', icon: <TrendingUp size={12} />, hint: 'Exponential Moving Average alignment — shows short vs long-term trend direction' },
    { label: 'Volume', key: 'volume', icon: <BarChart2 size={12} />, hint: 'Trading volume relative to average — high volume confirms trend, low volume suggests weak moves' },
  ]

  return (
    <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
      <h4 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-3">
        <Activity size={14} className="text-cyan-400" />
        Technical Indicators
      </h4>
      <div className="space-y-2">
        {items.map(({ label, key, icon, hint }) => {
          const value = indicators[key]
          if (!value) return null
          return (
            <div key={key} className={`px-3 py-2 rounded-lg border ${indicatorBg(value)}`}>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="text-gray-500">{icon}</span>
                  <span className="text-xs font-medium text-gray-400">{label}</span>
                </div>
                <span className={`text-xs font-semibold font-mono ${indicatorColor(value)}`}>
                  {value}
                </span>
              </div>
              <p className="text-[10px] text-gray-600 mt-0.5 pl-[20px]">{hint}</p>
            </div>
          )
        })}
        {/* EMA values row */}
        {indicators.ema_values && (
          <div className="px-3 py-2 rounded-lg border border-gray-800/30 bg-gray-800/20">
            <span className="text-[10px] text-gray-500 font-mono">{indicators.ema_values}</span>
            <p className="text-[10px] text-gray-600 mt-0.5">Short-term (9) crossing above long-term (50) = bullish, below = bearish</p>
          </div>
        )}
        {/* Support / Resistance */}
        {indicators.support_resistance && (
          <div className="px-3 py-2 rounded-lg border border-gray-800/30 bg-gray-800/20">
            <div className="flex items-center gap-2">
              <span className="text-xs text-gray-500">S/R:</span>
              <span className="text-[10px] text-gray-400 font-mono">{indicators.support_resistance}</span>
            </div>
            <p className="text-[10px] text-gray-600 mt-0.5">Support = price floor where buyers step in · Resistance = ceiling where sellers emerge</p>
          </div>
        )}
        {/* Price changes */}
        {(indicators.price_1h || indicators.price_24h) && (
          <div className="flex gap-2 mt-1">
            {indicators.price_1h && (
              <div className={`flex-1 px-3 py-1.5 rounded-lg border text-center ${indicatorBg(indicators.price_1h)}`}>
                <span className="text-[10px] text-gray-500">1h: </span>
                <span className={`text-xs font-semibold font-mono ${indicatorColor(indicators.price_1h)}`}>{indicators.price_1h}</span>
              </div>
            )}
            {indicators.price_24h && (
              <div className={`flex-1 px-3 py-1.5 rounded-lg border text-center ${indicatorBg(indicators.price_24h)}`}>
                <span className="text-[10px] text-gray-500">24h: </span>
                <span className={`text-xs font-semibold font-mono ${indicatorColor(indicators.price_24h)}`}>{indicators.price_24h}</span>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// ── AI Assessment Card ──────────────────────────────────────────────────────

function AIAssessmentCard({ cycle }: { cycle: CycleFull }) {
  // Find market_analyst and risk_manager spans
  const analystSpan = cycle.spans.find(s => s.agent_name === 'market_analyst')
  const riskSpan = cycle.spans.find(s => s.agent_name === 'risk_manager')
  const strategistSpan = cycle.spans.find(s => s.agent_name === 'strategist')

  const analystReasoning = analystSpan?.reasoning_json as Record<string, unknown> | undefined
  const riskReasoning = riskSpan?.reasoning_json as Record<string, unknown> | undefined
  const stratReasoning = strategistSpan?.reasoning_json as Record<string, unknown> | undefined

  const signalType = (analystReasoning?.signal_type as string) ?? analystSpan?.signal_type ?? 'neutral'
  const confidence = (analystReasoning?.confidence as number) ?? analystSpan?.confidence ?? 0
  const reasoning = (analystReasoning?.reasoning as string) ?? ''
  const keyFactors = (analystReasoning?.key_factors as string[]) ?? []
  const marketCondition = (analystReasoning?.market_condition as string) ?? ''
  const sentimentScore = (analystReasoning?.sentiment_score as number) ?? 0
  const sentimentOverall = (analystReasoning?.sentiment_overall as string) ?? ''

  const riskApproved = riskReasoning?.approved as boolean | undefined
  const riskReason = (riskReasoning?.reason as string) ?? ''
  const riskAction = (stratReasoning?.action as string) ?? (riskReasoning?.action as string) ?? ''

  const signalColor = SIGNAL_COLORS[signalType] ?? '#6b7280'

  return (
    <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
      <h4 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-3">
        <Brain size={14} className="text-violet-400" />
        AI Assessment
        <span className="text-[10px] text-gray-600 font-normal ml-auto">
          {dayjs(cycle.started_at).fromNow()}
        </span>
      </h4>

      {/* Signal badge + confidence */}
      <div className="flex items-center gap-3 mb-3">
        <span
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-bold border"
          style={{ color: signalColor, borderColor: `${signalColor}40`, background: `${signalColor}10` }}
        >
          {signalType.includes('buy') ? <ArrowUpRight size={14} /> : signalType.includes('sell') ? <ArrowDownRight size={14} /> : <Minus size={14} />}
          {SIGNAL_LABELS[signalType] ?? signalType}
        </span>
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-0.5">
            <span className="text-[10px] text-gray-500">Confidence</span>
            <span className="text-xs font-bold" style={{ color: signalColor }}>
              {(confidence * 100).toFixed(0)}%
            </span>
          </div>
          <div className="w-full h-1.5 bg-gray-800 rounded-full overflow-hidden">
            <div
              className="h-full rounded-full transition-all"
              style={{ width: `${confidence * 100}%`, background: signalColor }}
            />
          </div>
        </div>
      </div>

      {/* Market condition + sentiment */}
      {(marketCondition || sentimentOverall) && (
        <div className="flex flex-wrap gap-2 mb-3">
          {marketCondition && (
            <span className={`text-[10px] px-2 py-0.5 rounded border ${indicatorBg(marketCondition)} ${indicatorColor(marketCondition)}`}>
              Market: {marketCondition.replace(/_/g, ' ')}
            </span>
          )}
          {sentimentOverall && (
            <span className={`text-[10px] px-2 py-0.5 rounded border ${indicatorBg(sentimentOverall)} ${indicatorColor(sentimentOverall)}`}>
              Sentiment: {sentimentOverall} ({sentimentScore > 0 ? '+' : ''}{sentimentScore.toFixed(2)})
            </span>
          )}
        </div>
      )}

      {/* Key factors */}
      {keyFactors.length > 0 && (
        <div className="mb-3">
          <p className="text-[10px] text-gray-500 font-medium uppercase tracking-wider mb-1">Key Factors</p>
          <div className="flex flex-wrap gap-1.5">
            {keyFactors.map((f, i) => (
              <span key={i} className="text-[10px] px-2 py-0.5 rounded-full bg-gray-800/60 border border-gray-700/40 text-gray-300">
                {f}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* AI reasoning text */}
      {reasoning && (
        <div className="mb-3">
          <p className="text-[10px] text-gray-500 font-medium uppercase tracking-wider mb-1">Reasoning</p>
          <p className="text-xs text-gray-400 leading-relaxed bg-gray-800/30 rounded-lg p-3 border border-gray-800/50">
            {reasoning}
          </p>
        </div>
      )}

      {/* Risk verdict */}
      <div className="border-t border-gray-800/50 pt-3">
        <div className="flex items-center gap-2">
          <Shield size={12} className={riskApproved === false ? 'text-red-400' : riskApproved === true ? 'text-green-400' : 'text-gray-500'} />
          <span className="text-[10px] text-gray-500 font-medium uppercase tracking-wider">Risk Verdict</span>
          {riskAction && (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-400 font-mono ml-auto">
              {riskAction}
            </span>
          )}
        </div>
        {riskApproved === true && (
          <p className="text-xs text-green-400/80 mt-1 flex items-center gap-1">✓ Approved{riskReason ? ` — ${riskReason}` : ''}</p>
        )}
        {riskApproved === false && (
          <p className="text-xs text-red-400/80 mt-1 flex items-center gap-1">✗ Rejected — {riskReason}</p>
        )}
        {riskApproved == null && (
          <p className="text-xs text-gray-500 mt-1">No risk assessment (hold signal)</p>
        )}
      </div>
    </div>
  )
}

// ── Pair News Feed ──────────────────────────────────────────────────────────

function PairNewsFeed({ pair, articles }: { pair: string; articles: NewsArticle[] }) {
  const ticker = pairBaseTicker(pair).toLowerCase()

  /** Strip IBKR metadata prefix like {A:800015:L:en:K:n/a:C:0.90...} from titles */
  const cleanTitle = (t: string) => t.replace(/^\{[^}]+\}\s*/g, '').trim()

  const filtered = useMemo(() => {
    return articles.filter(a => {
      const tags = (a.tags ?? []).map(t => t.toLowerCase())
      if (tags.includes(ticker)) return true
      const title = cleanTitle(a.title).toLowerCase()
      if (title.includes(ticker.toLowerCase())) return true
      // Also match the full pair name
      if (title.includes(pair.toLowerCase().replace('-', ' '))) return true
      return false
    }).slice(0, 15)
  }, [articles, ticker, pair])

  const sentimentIcon = (s: string) => {
    if (s === 'bullish') return <TrendingUp size={10} className="text-green-400" />
    if (s === 'bearish') return <TrendingDown size={10} className="text-red-400" />
    return <Minus size={10} className="text-gray-400" />
  }

  const sentimentBg = (s: string) => {
    if (s === 'bullish') return 'border-green-800/30'
    if (s === 'bearish') return 'border-red-800/30'
    return 'border-gray-800'
  }

  return (
    <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
      <h4 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-3">
        <Newspaper size={14} className="text-amber-400" />
        News — {pairBaseTicker(pair)}
        <span className="text-[10px] text-gray-600 font-normal">{filtered.length} articles</span>
      </h4>

      {filtered.length === 0 ? (
        <p className="text-xs text-gray-500 py-4 text-center">No recent news for {pairBaseTicker(pair)}</p>
      ) : (
        <div className="space-y-1.5 max-h-[380px] overflow-y-auto pr-1 scrollbar-thin">
          {filtered.map((a, i) => (
            <div key={a.id || i} className={`px-3 py-2 rounded-lg border ${sentimentBg(a.sentiment)} hover:bg-gray-800/30 transition-colors group`}>
              <div className="flex items-start gap-2">
                <div className="mt-0.5 flex-shrink-0">{sentimentIcon(a.sentiment)}</div>
                <div className="flex-1 min-w-0">
                  <p className="text-xs text-gray-200 leading-snug line-clamp-2">{cleanTitle(a.title)}</p>
                  <div className="flex items-center gap-2 mt-0.5">
                    <span className="text-[10px] text-gray-600 uppercase">{a.source}</span>
                    <span className="text-[10px] text-gray-600">{dayjs(a.published).fromNow()}</span>
                  </div>
                </div>
                {a.url && (
                  <a href={a.url} target="_blank" rel="noreferrer"
                    className="flex-shrink-0 opacity-0 group-hover:opacity-100 text-gray-600 hover:text-gray-400 transition-opacity">
                    <ExternalLink size={10} />
                  </a>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ── Pair Trade History ──────────────────────────────────────────────────────

function PairTradeHistory({ pair }: { pair: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ['pair-trades', pair],
    queryFn: () => fetchTrades(pair, 20, 720),
    enabled: !!pair,
    refetchInterval: 60_000,
  })

  const trades = data?.trades ?? []

  if (isLoading) return <SkeletonBlock className="h-[200px]" />

  return (
    <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
      <h4 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-3">
        <DollarSign size={14} className="text-green-400" />
        Trade History — {pair}
        <span className="text-[10px] text-gray-600 font-normal">{trades.length} trades (30d)</span>
      </h4>

      {trades.length === 0 ? (
        <p className="text-xs text-gray-500 py-4 text-center">No trades for {pair} in the last 30 days</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-gray-800">
          <table className="min-w-full text-xs">
            <thead>
              <tr className="border-b border-gray-800 bg-gray-900/50">
                {['Time', 'Action', 'Price', 'Amount', 'PnL', 'Confidence', 'Reasoning'].map(h => (
                  <th key={h} className="px-2.5 py-2 text-left font-medium text-gray-400">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {trades.slice(0, 10).map((t) => (
                <tr key={t.id} className="border-b border-gray-800/50 hover:bg-gray-800/30 transition-colors">
                  <td className="px-2.5 py-2 text-gray-400">{dayjs(t.ts).format('MMM DD HH:mm')}</td>
                  <td className="px-2.5 py-2">
                    <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold ${t.action === 'buy' ? 'bg-green-900/30 text-green-400' : 'bg-red-900/30 text-red-400'
                      }`}>
                      {t.action === 'buy' ? <ArrowUpRight size={10} /> : <ArrowDownRight size={10} />}
                      {t.action.toUpperCase()}
                    </span>
                  </td>
                  <td className="px-2.5 py-2 text-gray-300 font-mono">{t.price < 1 ? t.price.toFixed(6) : t.price.toFixed(2)}</td>
                  <td className="px-2.5 py-2 text-gray-300 font-mono">{t.quote_amount.toFixed(2)}</td>
                  <td className="px-2.5 py-2">
                    {t.pnl != null ? (
                      <span className={`font-mono ${t.pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                        {t.pnl >= 0 ? '+' : ''}{t.pnl.toFixed(2)}
                      </span>
                    ) : <span className="text-gray-600">—</span>}
                  </td>
                  <td className="px-2.5 py-2 text-gray-300">{t.confidence ? `${(t.confidence * 100).toFixed(0)}%` : '—'}</td>
                  <td className="px-2.5 py-2 text-gray-500 max-w-[200px] truncate">{t.reasoning ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ── Recent Predictions Table ───────────────────────────────────────────────

function RecentPredictions({ data, selectedPair }: { data: PredictionAccuracyData; selectedPair?: string | null }) {
  const allPredictions = [...data.predictions].reverse().slice(0, 50)
  const predictions = selectedPair
    ? allPredictions.filter(p => p.pair === selectedPair)
    : allPredictions

  if (!predictions.length) return <EmptyState icon="chart" title={selectedPair ? `No predictions for ${selectedPair}` : 'No predictions yet'} description="AI signal predictions will appear as cycles run." />

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
  const [selectedPair, setSelectedPair] = useState<string | null>(null)
  const [pairSearch, setPairSearch] = useState('')

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

  // ── Trader's View data fetches (only when pair selected) ──
  const { data: cyclesData } = useQuery({
    queryKey: ['pair-cycles', selectedPair],
    queryFn: () => fetchCycles(selectedPair!, 1),
    enabled: !!selectedPair,
    refetchInterval: 120_000,
  })

  const latestCycleId = cyclesData?.cycles?.[0]?.cycle_id ?? null

  const { data: cycleFull } = useQuery({
    queryKey: ['cycle-full', latestCycleId],
    queryFn: () => fetchCycleFull(latestCycleId!),
    enabled: !!latestCycleId,
    refetchInterval: 120_000,
  })

  const { data: newsData } = useQuery({
    queryKey: ['trader-news'],
    queryFn: () => fetchNews(100),
    enabled: !!selectedPair,
    refetchInterval: 300_000,
  })

  // Parse technical indicators from the latest market_analyst raw_prompt
  const techIndicators = useMemo(() => {
    if (!cycleFull) return null
    const analystSpan = cycleFull.spans.find(s => s.agent_name === 'market_analyst')
    return parseIndicatorsFromPrompt(analystSpan?.raw_prompt ?? null)
  }, [cycleFull])

  const data = rawData ? filterByAsset(rawData, assetTab) : undefined
  const overall = data?.overall

  // Count pending (unevaluated) predictions
  const pendingCount = data ? data.predictions.filter(p => !p.outcomes['1h']).length : 0

  // Build pair list for the overlay selector from both tracked pairs and prediction data
  const allPairOptions = useMemo(() => {
    const pairs = new Set<string>()
    if (trackedPairs) {
      for (const p of [...trackedPairs.crypto, ...trackedPairs.equity]) pairs.add(p.pair)
    }
    if (data) {
      for (const pair of Object.keys(data.per_pair)) pairs.add(pair)
    }
    return Array.from(pairs).sort()
  }, [trackedPairs, data])

  const filteredPairOptions = pairSearch
    ? allPairOptions.filter(p => p.toLowerCase().includes(pairSearch.toLowerCase()))
    : allPairOptions

  return (
    <PageTransition>
      <div className="p-6 space-y-6">
        {/* Header + controls */}
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
          <div>
            <h2 className="text-xl font-bold text-gray-100">
              {selectedPair ? `Trader View — ${selectedPair}` : 'Predictions vs Actuals'}
            </h2>
            <p className="text-xs text-gray-500 mt-0.5">
              {selectedPair
                ? 'Complete AI analysis, technical indicators, news, and trade history'
                : `Live AI signal accuracy — data from ${rawData?.overall?.total ?? 0} real predictions`
              }
            </p>
          </div>
          <div className="flex gap-2 flex-wrap">
            {/* Asset class tabs */}
            <div className="flex gap-0.5 bg-gray-800/50 rounded-lg p-0.5">
              {ASSET_TABS.map((t) => (
                <button
                  key={t.id}
                  onClick={() => setAssetTab(t.id)}
                  className={`px-2.5 py-1 text-[11px] rounded-md font-medium transition-colors ${assetTab === t.id
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
                  className={`px-3 py-1.5 text-xs rounded-lg font-medium transition-colors ${days === r.days
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
            <TrackedPairsSection data={trackedPairs} assetTab={assetTab} onSelectPair={setSelectedPair} selectedPair={selectedPair} />
          ) : (
            <SkeletonBlock className="h-[100px]" />
          )}
        </div>

        {/* ═══════════════════════════════════════════════════════════════════
            TRADER'S VIEW — shown when a pair is selected
            ═══════════════════════════════════════════════════════════════════ */}
        {selectedPair && (
          <div className="space-y-4">
            {/* Header: pair name + live price + position stats */}
            <TraderViewHeader pair={selectedPair} onClose={() => setSelectedPair(null)} />

            {/* Row 1: Technical Indicators + AI Assessment */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {techIndicators && (
                <TechnicalIndicatorsPanel indicators={techIndicators} />
              )}
              {cycleFull && (
                <AIAssessmentCard cycle={cycleFull} />
              )}
              {/* If only one panel, fill the gap */}
              {!techIndicators && !cycleFull && (
                <div className="col-span-2 bg-gray-900/50 border border-gray-800 rounded-xl p-5 text-center">
                  <p className="text-xs text-gray-500 py-4">No AI analysis data yet for {selectedPair}. Waiting for the next analysis cycle.</p>
                </div>
              )}
            </div>

            {/* Row 2: Prediction Overlay + News */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
                <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-1">
                  <Eye size={14} className="text-brand-400" />
                  Prediction Overlay
                </h3>
                <p className="text-[10px] text-gray-600 mb-3">
                  Price chart with AI signal markers. ▲ = buy · ◆ = sell · Dashed = forecast.
                </p>
                <PredictionOverlayChart pair={selectedPair} />
              </div>

              <PairNewsFeed pair={selectedPair} articles={newsData?.articles ?? []} />
            </div>

            {/* Row 3: Trade History */}
            <PairTradeHistory pair={selectedPair} />
          </div>
        )}

        {/* Quick pair selector (when no pair selected) */}
        {!selectedPair && allPairOptions.length > 0 && (
          <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-3">
              <Eye size={14} className="text-brand-400" />
              Prediction Overlay
            </h3>
            <p className="text-[10px] text-gray-600 mb-3">
              Select any tracked pair above or search below to view predictions overlaid on actual price.
            </p>
            <div className="relative max-w-sm">
              <Search size={12} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
              <input
                type="text"
                value={pairSearch}
                onChange={(e) => setPairSearch(e.target.value)}
                placeholder="Search pairs..."
                className="w-full pl-8 pr-3 py-2 bg-gray-800/50 border border-gray-700 rounded-lg text-xs text-gray-200 placeholder-gray-600 focus:border-brand-500 focus:outline-none"
              />
            </div>
            {pairSearch && filteredPairOptions.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {filteredPairOptions.slice(0, 20).map((p) => (
                  <button
                    key={p}
                    onClick={() => { setSelectedPair(p); setPairSearch('') }}
                    className="px-2.5 py-1 text-[11px] bg-gray-800/50 border border-gray-700 rounded-md text-gray-300 hover:border-brand-500 hover:text-brand-400 transition-colors"
                  >
                    {p}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Charts row 1: Daily trend + Signal type breakdown */}
        {!selectedPair && (
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
        )}

        {/* Charts row 2: Confidence calibration + Per-pair heatmap */}
        {!selectedPair && (
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
        )}

        {/* Recent predictions table */}
        <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-5">
          <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-1">
            <Crosshair size={14} className="text-cyan-400" />
            {selectedPair ? `Predictions — ${selectedPair}` : 'Recent Predictions'}
          </h3>
          <p className="text-[10px] text-gray-600 mb-3">
            {selectedPair
              ? `Filtered predictions for ${selectedPair}. Outcomes update automatically.`
              : 'Live predictions from the AI market analyst. Outcomes update automatically as time passes.'
            }
            <Clock size={8} className="inline ml-1 opacity-50" /> = awaiting evaluation window.
          </p>
          {isLoading ? (
            <SkeletonBlock className="h-[300px]" />
          ) : data ? (
            <RecentPredictions data={data} selectedPair={selectedPair} />
          ) : null}
        </div>
      </div>
    </PageTransition>
  )
}

