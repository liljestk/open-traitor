/**
 * Watchlist — Active pairs monitoring with live prices, scan results, price charts,
 * and human follow/unfollow management.
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import dayjs from 'dayjs'
import {
  Eye, TrendingUp, TrendingDown, BarChart2, RefreshCw, Zap,
  Bot, UserRound, Plus,
} from 'lucide-react'
import { fetchWatchlist, fetchCandles, followPair, unfollowPair, type PairInfo } from '../api'
import { useCurrencyFormatter } from '../store'
import CandlestickChart from '../components/CandlestickChart'
import { SkeletonCards, SkeletonBlock } from '../components/Skeleton'
import EmptyState from '../components/EmptyState'
import PageTransition from '../components/PageTransition'

/* ── Follow-source badges ───────────────────────────────────────────────── */

function SourceBadge({ llm, human }: { llm: boolean; human: boolean }) {
  return (
    <div className="flex items-center gap-1">
      {llm && (
        <span
          title="Followed by LLM"
          className="inline-flex items-center gap-0.5 rounded bg-violet-900/50 border border-violet-700/40 px-1.5 py-0.5 text-[10px] font-medium text-violet-300"
        >
          <Bot size={10} /> LLM
        </span>
      )}
      {human && (
        <span
          title="Followed by you"
          className="inline-flex items-center gap-0.5 rounded bg-sky-900/50 border border-sky-700/40 px-1.5 py-0.5 text-[10px] font-medium text-sky-300"
        >
          <UserRound size={10} /> You
        </span>
      )}
    </div>
  )
}

/* ── Pair card ──────────────────────────────────────────────────────────── */

function PairCard({
  info,
  onSelect,
  isSelected,
  fmt,
  onToggleFollow,
  isToggling,
}: {
  info: PairInfo
  onSelect: () => void
  isSelected: boolean
  fmt: (v: number | null) => string
  onToggleFollow: () => void
  isToggling: boolean
}) {
  return (
    <div
      className={`w-full rounded-xl border px-4 py-3 transition-all ${
        isSelected
          ? 'bg-brand-900/20 border-brand-600/40 shadow-[0_0_16px_rgba(34,197,94,0.08)]'
          : 'bg-gray-900/60 border-gray-800 hover:border-gray-700'
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        {/* Left: click to view chart */}
        <button onClick={onSelect} className="flex-1 text-left flex items-center gap-2 min-w-0">
          <Eye size={14} className={isSelected ? 'text-brand-400' : 'text-gray-600'} />
          <span className="text-sm font-semibold text-gray-200 truncate">{info.pair}</span>
          <SourceBadge llm={info.followed_by_llm} human={info.followed_by_human} />
        </button>

        {/* Right: price + follow toggle */}
        <div className="flex items-center gap-2 flex-shrink-0">
          <span className={`text-sm font-mono ${info.price ? 'text-gray-200' : 'text-gray-600'}`}>
            {info.price ? fmt(info.price) : '—'}
          </span>
          <button
            onClick={(e) => { e.stopPropagation(); onToggleFollow() }}
            disabled={isToggling}
            title={info.followed_by_human ? 'Unfollow' : 'Follow'}
            className={`p-1 rounded transition-colors ${
              info.followed_by_human
                ? 'text-sky-400 hover:text-sky-300 hover:bg-sky-900/30'
                : 'text-gray-600 hover:text-gray-400 hover:bg-gray-800'
            } disabled:opacity-40`}
          >
            {info.followed_by_human ? <UserRound size={14} /> : <Plus size={14} />}
          </button>
        </div>
      </div>
    </div>
  )
}

/* ── Top movers ─────────────────────────────────────────────────────────── */

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

/* ── Add-pair input ─────────────────────────────────────────────────────── */

function AddPairInput({ onAdd, isAdding }: { onAdd: (pair: string) => void; isAdding: boolean }) {
  const [value, setValue] = useState('')
  const submit = () => {
    const trimmed = value.trim().toUpperCase()
    if (trimmed && trimmed.includes('-')) {
      onAdd(trimmed)
      setValue('')
    }
  }
  return (
    <div className="flex items-center gap-1.5">
      <input
        type="text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && submit()}
        placeholder="AAPL-USD"
        className="flex-1 bg-gray-800/60 border border-gray-700 rounded-lg px-3 py-1.5 text-xs text-gray-200 placeholder-gray-600 focus:outline-none focus:border-brand-600"
      />
      <button
        onClick={submit}
        disabled={isAdding || !value.trim().includes('-')}
        className="flex items-center gap-1 text-xs px-3 py-1.5 bg-brand-700 hover:bg-brand-600 text-white rounded-lg disabled:opacity-40 transition-colors"
      >
        <Plus size={12} /> Follow
      </button>
    </div>
  )
}

/* ── Main page ──────────────────────────────────────────────────────────── */

export default function Watchlist() {
  const [selectedPair, setSelectedPair] = useState<string | null>(null)
  const fmtCurrency = useCurrencyFormatter()
  const qc = useQueryClient()

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

  /* Mutations for follow / unfollow */
  const followMut = useMutation({
    mutationFn: (pair: string) => followPair(pair),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['watchlist'] }),
  })
  const unfollowMut = useMutation({
    mutationFn: (pair: string) => unfollowPair(pair),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['watchlist'] }),
  })
  const isToggling = followMut.isPending || unfollowMut.isPending

  const pairInfos = data?.pair_info ?? []
  const prices = data?.live_prices ?? {}
  const scan = data?.scan
  const topMovers = (scan?.top_movers ?? []) as Array<{ pair: string; change_pct: number; volume: number }>

  const handleToggleFollow = (info: PairInfo) => {
    if (info.followed_by_human) {
      unfollowMut.mutate(info.pair)
    } else {
      followMut.mutate(info.pair)
    }
  }

  return (
    <PageTransition>
      <div className="p-6 space-y-4">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <h2 className="text-xl font-bold text-gray-100">Watchlist</h2>
            <span className="text-xs text-gray-500">{pairInfos.length} pairs</span>
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
            {/* Add pair input */}
            <AddPairInput
              onAdd={(pair) => followMut.mutate(pair)}
              isAdding={followMut.isPending}
            />

            <div className="space-y-1.5">
              <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider px-1">Active Pairs</h3>
              {isLoading ? (
                <SkeletonCards count={6} />
              ) : pairInfos.length === 0 ? (
                <EmptyState icon="search" title="No active pairs" description="Configure trading pairs in settings or follow a pair above." />
              ) : (
                pairInfos.map((info) => (
                  <PairCard
                    key={info.pair}
                    info={info}
                    onSelect={() => setSelectedPair(info.pair === selectedPair ? null : info.pair)}
                    isSelected={info.pair === selectedPair}
                    fmt={fmtCurrency}
                    onToggleFollow={() => handleToggleFollow(info)}
                    isToggling={isToggling}
                  />
                ))
              )}
            </div>

            {/* Legend */}
            <div className="flex items-center gap-3 px-1 text-[10px] text-gray-600">
              <span className="flex items-center gap-1"><Bot size={10} className="text-violet-400" /> = LLM-selected</span>
              <span className="flex items-center gap-1"><UserRound size={10} className="text-sky-400" /> = Your follow</span>
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
