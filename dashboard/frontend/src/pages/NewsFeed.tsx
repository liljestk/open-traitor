/**
 * NewsFeed — Aggregated news headlines with sentiment indicators.
 * Supports sorting (date, sentiment), full-text search, and ticker filtering.
 */
import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import {
  TrendingUp, TrendingDown, Minus, ExternalLink, RefreshCw,
  Search, ArrowUpDown, X, Tag,
} from 'lucide-react'
import { fetchNews, type NewsArticle } from '../api'
import { SkeletonCards } from '../components/Skeleton'
import EmptyState from '../components/EmptyState'
import PageTransition from '../components/PageTransition'

dayjs.extend(relativeTime)

/* ── Types ──────────────────────────────────────────────────────────────── */

type SortKey = 'date' | 'relevance' | 'sentiment'
type SentimentFilter = 'all' | 'bullish' | 'bearish' | 'neutral'

const SENTIMENT_CONFIG = {
  bullish: { icon: TrendingUp, color: 'text-green-400', bg: 'bg-green-900/30 border-green-800/50', label: 'Bullish', order: 1 },
  bearish: { icon: TrendingDown, color: 'text-red-400', bg: 'bg-red-900/30 border-red-800/50', label: 'Bearish', order: 3 },
  neutral: { icon: Minus, color: 'text-gray-400', bg: 'bg-gray-800/50 border-gray-700/50', label: 'Neutral', order: 2 },
} as const

/* ── Helper components ──────────────────────────────────────────────────── */

function SentimentBadge({ sentiment }: { sentiment: string }) {
  const cfg = SENTIMENT_CONFIG[sentiment as keyof typeof SENTIMENT_CONFIG] ?? SENTIMENT_CONFIG.neutral
  const Icon = cfg.icon
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold border ${cfg.bg} ${cfg.color}`}>
      <Icon size={10} />
      {cfg.label}
    </span>
  )
}

function SentimentSummary({ articles }: { articles: Array<{ sentiment: string }> }) {
  const counts = { bullish: 0, bearish: 0, neutral: 0 }
  articles.forEach((a) => {
    const s = a.sentiment as keyof typeof counts
    if (s in counts) counts[s]++
  })
  const total = articles.length || 1
  return (
    <div className="flex items-center gap-4 text-xs">
      <div className="flex items-center gap-1.5">
        <div className="w-16 h-2 rounded-full overflow-hidden bg-gray-800 flex">
          <div className="bg-green-500 h-full" style={{ width: `${(counts.bullish / total) * 100}%` }} />
          <div className="bg-gray-500 h-full" style={{ width: `${(counts.neutral / total) * 100}%` }} />
          <div className="bg-red-500 h-full" style={{ width: `${(counts.bearish / total) * 100}%` }} />
        </div>
      </div>
      <span className="text-green-400">{counts.bullish} bullish</span>
      <span className="text-gray-400">{counts.neutral} neutral</span>
      <span className="text-red-400">{counts.bearish} bearish</span>
    </div>
  )
}

/* ── Ticker extraction from tags ────────────────────────────────────────── */

function extractTickers(articles: NewsArticle[]): string[] {
  const counts = new Map<string, number>()
  for (const a of articles) {
    for (const tag of a.tags ?? []) {
      const t = tag.toUpperCase()
      if (/^[A-Z]{1,6}$/.test(t)) {
        counts.set(t, (counts.get(t) ?? 0) + 1)
      }
    }
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 20)
    .map(([t]) => t)
}

/* ── Sort / filter logic ────────────────────────────────────────────────── */

function applyFilters(
  articles: NewsArticle[],
  search: string,
  sentiment: SentimentFilter,
  tickers: Set<string>,
  sort: SortKey,
): NewsArticle[] {
  /** Strip IBKR metadata prefix like {A:800015:L:en:K:n/a:C:0.90...} from titles */
  const cleanTitle = (t: string) => t.replace(/^\{[^}]+\}\s*/g, '').trim()
  let filtered = articles

  if (search.trim()) {
    const q = search.toLowerCase()
    filtered = filtered.filter(
      (a) =>
        cleanTitle(a.title).toLowerCase().includes(q) ||
        (a.summary ?? '').toLowerCase().includes(q) ||
        (a.source ?? '').toLowerCase().includes(q) ||
        (a.tags ?? []).some((t) => t.toLowerCase().includes(q)),
    )
  }

  if (sentiment !== 'all') {
    filtered = filtered.filter((a) => a.sentiment === sentiment)
  }

  if (tickers.size > 0) {
    filtered = filtered.filter((a) => {
      const tags = new Set((a.tags ?? []).map((t) => t.toUpperCase()))
      for (const ticker of tickers) {
        if (tags.has(ticker)) return true
      }
      return false
    })
  }

  const sorted = [...filtered]
  switch (sort) {
    case 'date':
      sorted.sort((a, b) => dayjs(b.published).valueOf() - dayjs(a.published).valueOf())
      break
    case 'relevance':
      sorted.sort((a, b) => (b.relevance_score ?? 0) - (a.relevance_score ?? 0))
      break
    case 'sentiment': {
      const order = (s: string) => (SENTIMENT_CONFIG[s as keyof typeof SENTIMENT_CONFIG]?.order ?? 2)
      sorted.sort((a, b) => order(a.sentiment) - order(b.sentiment))
      break
    }
  }
  return sorted
}

/* ── Sort button ────────────────────────────────────────────────────────── */

const SORT_LABELS: Record<SortKey, string> = { date: 'Date', relevance: 'Relevance', sentiment: 'Sentiment' }
const SORT_CYCLE: SortKey[] = ['date', 'relevance', 'sentiment']

/* ── Main page ──────────────────────────────────────────────────────────── */

export default function NewsFeed() {
  const [search, setSearch] = useState('')
  const [sortKey, setSortKey] = useState<SortKey>('date')
  const [sentimentFilter, setSentimentFilter] = useState<SentimentFilter>('all')
  const [selectedTickers, setSelectedTickers] = useState<Set<string>>(new Set())

  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ['news'],
    queryFn: () => fetchNews(100),
    refetchInterval: 120_000,
  })

  const rawArticles = data?.articles ?? []
  const tickers = useMemo(() => extractTickers(rawArticles), [rawArticles])

  const articles = useMemo(
    () => applyFilters(rawArticles, search, sentimentFilter, selectedTickers, sortKey),
    [rawArticles, search, sentimentFilter, selectedTickers, sortKey],
  )

  const toggleTicker = (t: string) => {
    setSelectedTickers((prev) => {
      const next = new Set(prev)
      if (next.has(t)) next.delete(t)
      else next.add(t)
      return next
    })
  }

  const cycleSortKey = () => {
    const idx = SORT_CYCLE.indexOf(sortKey)
    setSortKey(SORT_CYCLE[(idx + 1) % SORT_CYCLE.length])
  }

  const hasFilters = search || sentimentFilter !== 'all' || selectedTickers.size > 0
  const clearFilters = () => {
    setSearch('')
    setSentimentFilter('all')
    setSelectedTickers(new Set())
  }

  return (
    <PageTransition>
      <div className="p-6 space-y-4">
        {/* Header row */}
        <div className="flex items-center justify-between flex-wrap gap-2">
          <div className="flex items-center gap-3">
            <h2 className="text-xl font-bold text-gray-100">Market Intelligence</h2>
            <span className="text-xs text-gray-500">
              {articles.length}{rawArticles.length !== articles.length ? ` / ${rawArticles.length}` : ''} articles
            </span>
          </div>
          <div className="flex items-center gap-3">
            {rawArticles.length > 0 && <SentimentSummary articles={rawArticles} />}
            <button
              onClick={() => refetch()}
              disabled={isFetching}
              className="flex items-center gap-1.5 text-xs px-3 py-1.5 bg-gray-800 rounded-lg hover:bg-gray-700 text-gray-400 disabled:opacity-50"
            >
              <RefreshCw size={12} className={isFetching ? 'animate-spin' : ''} />
              Refresh
            </button>
          </div>
        </div>

        {/* Search + sort + sentiment filter toolbar */}
        <div className="flex flex-wrap items-center gap-2">
          {/* Search input */}
          <div className="relative flex-1 min-w-[200px] max-w-md">
            <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-500 pointer-events-none" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search headlines, tags, sources…"
              className="w-full bg-gray-800/60 border border-gray-700 rounded-lg pl-8 pr-8 py-1.5 text-xs text-gray-200 placeholder-gray-600 focus:outline-none focus:border-brand-600"
            />
            {search && (
              <button
                onClick={() => setSearch('')}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300"
              >
                <X size={12} />
              </button>
            )}
          </div>

          {/* Sort toggle */}
          <button
            onClick={cycleSortKey}
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 bg-gray-800 border border-gray-700 rounded-lg hover:bg-gray-700 text-gray-400"
          >
            <ArrowUpDown size={12} />
            {SORT_LABELS[sortKey]}
          </button>

          {/* Sentiment filter pills */}
          {(['all', 'bullish', 'neutral', 'bearish'] as const).map((s) => (
            <button
              key={s}
              onClick={() => setSentimentFilter(s)}
              className={`text-xs px-2.5 py-1 rounded-lg border transition-colors ${sentimentFilter === s
                  ? s === 'bullish' ? 'bg-green-900/40 border-green-700/60 text-green-400'
                    : s === 'bearish' ? 'bg-red-900/40 border-red-700/60 text-red-400'
                      : s === 'neutral' ? 'bg-gray-700/60 border-gray-600 text-gray-300'
                        : 'bg-brand-900/40 border-brand-700/60 text-brand-400'
                  : 'bg-gray-800/40 border-gray-800 text-gray-500 hover:text-gray-400 hover:border-gray-700'
                }`}
            >
              {s === 'all' ? 'All' : s.charAt(0).toUpperCase() + s.slice(1)}
            </button>
          ))}

          {/* Clear filters */}
          {hasFilters && (
            <button
              onClick={clearFilters}
              className="flex items-center gap-1 text-[10px] text-gray-500 hover:text-gray-300"
            >
              <X size={10} /> Clear
            </button>
          )}
        </div>

        {/* Ticker chips */}
        {tickers.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            <Tag size={11} className="text-gray-600 mt-0.5 mr-0.5" />
            {tickers.map((t) => (
              <button
                key={t}
                onClick={() => toggleTicker(t)}
                className={`text-[10px] px-2 py-0.5 rounded-full border transition-colors ${selectedTickers.has(t)
                    ? 'bg-brand-900/50 border-brand-600/50 text-brand-300'
                    : 'bg-gray-800/50 border-gray-700/50 text-gray-500 hover:text-gray-400 hover:border-gray-600'
                  }`}
              >
                {t}
              </button>
            ))}
          </div>
        )}

        {data?.source === 'unavailable' && (
          <div className="bg-yellow-900/20 border border-yellow-800/40 rounded-lg px-4 py-3 text-xs text-yellow-400">
            Redis not connected — news feed requires the news worker and Redis.
          </div>
        )}

        {isLoading ? (
          <SkeletonCards count={8} />
        ) : articles.length === 0 ? (
          <div className="flex items-center justify-center py-16">
            <EmptyState
              icon="search"
              title={hasFilters ? 'No matching articles' : 'No news articles'}
              description={
                hasFilters
                  ? 'Try adjusting your search or filters.'
                  : "The news worker hasn't published any articles yet. Make sure the news worker is running and Redis is connected."
              }
            />
          </div>
        ) : (
          <div className="space-y-2">
            {articles.map((article, i) => (
              <div
                key={article.id || i}
                className="bg-gray-900/60 border border-gray-800 rounded-xl px-4 py-3 hover:border-gray-700 transition-colors group"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1 flex-wrap">
                      <SentimentBadge sentiment={article.sentiment} />
                      <span className="text-[10px] text-gray-600 uppercase tracking-wider">{article.source}</span>
                      <span className="text-[10px] text-gray-600">
                        {article.published ? dayjs(article.published).fromNow() : ''}
                      </span>
                      {article.relevance_score > 0.7 && (
                        <span className="text-[10px] px-1.5 py-0.5 bg-brand-900/40 text-brand-400 rounded">
                          High relevance
                        </span>
                      )}
                    </div>
                    <h3 className="text-sm font-medium text-gray-200 mb-1 leading-snug">
                      {article.title.replace(/^\{[^}]+\}\s*/g, '').trim()}
                    </h3>
                    {article.summary && (
                      <p className="text-xs text-gray-500 line-clamp-2 leading-relaxed">
                        {article.summary}
                      </p>
                    )}
                    {Array.isArray(article.tags) && article.tags.length > 0 && (
                      <div className="flex gap-1 mt-1.5 flex-wrap">
                        {article.tags.slice(0, 8).map((tag) => (
                          <button
                            key={tag}
                            onClick={() => {
                              const upper = tag.toUpperCase()
                              if (/^[A-Z]{1,6}$/.test(upper)) toggleTicker(upper)
                              else setSearch(tag)
                            }}
                            className={`text-[10px] px-1.5 py-0.5 rounded transition-colors ${selectedTickers.has(tag.toUpperCase())
                                ? 'bg-brand-900/50 text-brand-300'
                                : 'bg-gray-800 text-gray-500 hover:text-gray-400 hover:bg-gray-700'
                              }`}
                          >
                            {tag}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                  {article.url && (
                    <a
                      href={article.url}
                      target="_blank"
                      rel="noreferrer"
                      className="flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity text-gray-500 hover:text-gray-300"
                    >
                      <ExternalLink size={14} />
                    </a>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </PageTransition>
  )
}
