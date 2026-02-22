/**
 * Watchlist — Active pairs monitoring with live prices, scan results, and price charts.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'
import { Eye, TrendingUp, TrendingDown, BarChart2, RefreshCw, Zap } from 'lucide-react'
import { fetchWatchlist, fetchCandles } from '../api'
import { useCurrencyFormatter } from '../store'
import CandlestickChart from '../components/CandlestickChart'
import { SkeletonCards, SkeletonBlock } from '../components/Skeleton'
import EmptyState from '../components/EmptyState'
import PageTransition from '../components/PageTransition'

function PairCard({
  pair,
  price,
  onSelect,
  isSelected,
  fmt,
}: {
  pair: string
  price: number | null
  onSelect: () => void
  isSelected: boolean
  fmt: (v: number | null) => string
}) {
  return (
    <button
      onClick={onSelect}
      className={`w-full text-left rounded-xl border px-4 py-3 transition-all ${
        isSelected
          ? 'bg-brand-900/20 border-brand-600/40 shadow-[0_0_16px_rgba(34,197,94,0.08)]'
          : 'bg-gray-900/60 border-gray-800 hover:border-gray-700'
      }`}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Eye size={14} className={isSelected ? 'text-brand-400' : 'text-gray-600'} />
          <span className="text-sm font-semibold text-gray-200">{pair}</span>
        </div>
        <span className={`text-sm font-mono ${price ? 'text-gray-200' : 'text-gray-600'}`}>
          {price ? fmt(price) : '—'}
        </span>
      </div>
    </button>
  )
}

function TopMovers({ movers }: { movers: Array<{ pair: string; change_pct: number; volume: number }> }) {
  if (!Array.isArray(movers) || !movers.length) return null
  return (
    <div className="space-y-1.5">
      {movers.slice(0, 10).map((m) => (
        <div key={m.pair} className="flex items-center justify-between bg-gray-800/40 rounded-lg px-3 py-2">
          <div className="flex items-center gap-2">
            {m.change_pct >= 0 ? (
              <TrendingUp size={12} className="text-green-400" />
            ) : (
              <TrendingDown size={12} className="text-red-400" />
            )}
            <span className="text-xs font-medium text-gray-200">{m.pair}</span>
          </div>
          <div className="flex items-center gap-3 text-xs">
            <span className={m.change_pct >= 0 ? 'text-green-400' : 'text-red-400'}>
              {m.change_pct >= 0 ? '+' : ''}{m.change_pct.toFixed(2)}%
            </span>
            <span className="text-gray-600">Vol: {m.volume?.toLocaleString() ?? '—'}</span>
          </div>
        </div>
      ))}
    </div>
  )
}

export default function Watchlist() {
  const [selectedPair, setSelectedPair] = useState<string | null>(null)
  const fmtCurrency = useCurrencyFormatter()

  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ['watchlist'],
    queryFn: fetchWatchlist,
    refetchInterval: 30_000,
  })

  const { data: candleData, isLoading: candleLoading } = useQuery({
    queryKey: ['candles', selectedPair],
    queryFn: () => fetchCandles(selectedPair!, 'ONE_HOUR', 200),
    enabled: !!selectedPair,
    staleTime: 60_000,
  })

  const pairs = data?.active_pairs ?? []
  const prices = data?.live_prices ?? {}
  const scan = data?.scan
  const topMovers = (scan?.top_movers ?? []) as Array<{ pair: string; change_pct: number; volume: number }>

  return (
    <PageTransition>
      <div className="p-6 space-y-4">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <h2 className="text-xl font-bold text-gray-100">Watchlist</h2>
            <span className="text-xs text-gray-500">{pairs.length} active pairs</span>
          </div>
          <div className="flex items-center gap-2">
            {scan && (
              <span className="text-xs text-gray-500">
                Last scan: {dayjs(scan.ts).format('HH:mm')} · {scan.universe_size} pairs in universe
              </span>
            )}
            <button
              onClick={() => refetch()}
              disabled={isFetching}
              className="flex items-center gap-1.5 text-xs px-3 py-1.5 bg-gray-800 rounded-lg hover:bg-gray-700 text-gray-400"
            >
              <RefreshCw size={12} className={isFetching ? 'animate-spin' : ''} />
            </button>
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {/* Pair list */}
          <div className="space-y-4">
            <div className="space-y-1.5">
              <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider px-1">Active Pairs</h3>
              {isLoading ? (
                <SkeletonCards count={6} />
              ) : pairs.length === 0 ? (
                <EmptyState icon="search" title="No active pairs" description="Configure trading pairs in settings." />
              ) : (
                pairs.map((pair) => (
                  <PairCard
                    key={pair}
                    pair={pair}
                    price={prices[pair] ?? null}
                    onSelect={() => setSelectedPair(pair === selectedPair ? null : pair)}
                    isSelected={pair === selectedPair}
                    fmt={fmtCurrency}
                  />
                ))
              )}
            </div>

            {/* Top movers from scan */}
            {topMovers.length > 0 && (
              <div>
                <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider px-1 mb-2 flex items-center gap-1.5">
                  <Zap size={11} className="text-yellow-400" />
                  Top Movers
                </h3>
                <TopMovers movers={topMovers} />
              </div>
            )}
          </div>

          {/* Chart area */}
          <div className="lg:col-span-2">
            {selectedPair ? (
              <div className="space-y-4">
                <div className="flex items-center gap-2">
                  <BarChart2 size={14} className="text-brand-400" />
                  <h3 className="text-sm font-semibold text-gray-300">{selectedPair}</h3>
                  {prices[selectedPair] && (
                    <span className="text-sm font-mono text-gray-400 ml-auto">
                      {fmtCurrency(prices[selectedPair])}
                    </span>
                  )}
                </div>
                {candleLoading ? (
                  <SkeletonBlock className="h-[400px] rounded-xl" />
                ) : candleData?.candles?.length ? (
                  <CandlestickChart
                    candles={candleData.candles.map((c) => ({
                      time: c.start,
                      open: c.open,
                      high: c.high,
                      low: c.low,
                      close: c.close,
                    }))}
                    height={400}
                    pair={selectedPair}
                  />
                ) : (
                  <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-8 flex items-center justify-center h-[400px]">
                    <EmptyState icon="chart" title="No candle data" description="Exchange client may not be available or pair not found." />
                  </div>
                )}
              </div>
            ) : (
              <div className="bg-gray-900/30 border border-gray-800/50 rounded-xl p-8 flex items-center justify-center h-[400px]">
                <EmptyState
                  icon="chart"
                  title="Select a pair"
                  description="Click on a pair from the list to view its price chart."
                />
              </div>
            )}

            {/* Scan summary */}
            {scan?.summary_text && (
              <div className="bg-gray-900/50 border border-gray-800 rounded-xl p-4 mt-4">
                <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Scan Summary</h3>
                <p className="text-xs text-gray-400 leading-relaxed whitespace-pre-wrap">{scan.summary_text}</p>
              </div>
            )}
          </div>
        </div>
      </div>
    </PageTransition>
  )
}
