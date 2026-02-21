/**
 * NewsFeed — Aggregated news headlines with sentiment indicators.
 * Shows articles from Reddit, RSS, and other sources ingested by the news worker.
 */
import { useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import { TrendingUp, TrendingDown, Minus, ExternalLink, RefreshCw } from 'lucide-react'
import { fetchNews } from '../api'
import { SkeletonCards } from '../components/Skeleton'
import EmptyState from '../components/EmptyState'
import PageTransition from '../components/PageTransition'

dayjs.extend(relativeTime)

const SENTIMENT_CONFIG = {
  bullish: { icon: TrendingUp, color: 'text-green-400', bg: 'bg-green-900/30 border-green-800/50', label: 'Bullish' },
  bearish: { icon: TrendingDown, color: 'text-red-400', bg: 'bg-red-900/30 border-red-800/50', label: 'Bearish' },
  neutral: { icon: Minus, color: 'text-gray-400', bg: 'bg-gray-800/50 border-gray-700/50', label: 'Neutral' },
} as const

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

export default function NewsFeed() {
  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ['news'],
    queryFn: () => fetchNews(50),
    refetchInterval: 120_000, // 2 min
  })

  const articles = data?.articles ?? []

  return (
    <PageTransition>
      <div className="p-6 space-y-4">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <h2 className="text-xl font-bold text-gray-100">Market Intelligence</h2>
            <span className="text-xs text-gray-500">{articles.length} articles</span>
          </div>
          <div className="flex items-center gap-3">
            {articles.length > 0 && <SentimentSummary articles={articles} />}
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
              title="No news articles"
              description="The news worker hasn't published any articles yet. Make sure the news worker is running and Redis is connected."
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
                    <div className="flex items-center gap-2 mb-1">
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
                      {article.title}
                    </h3>
                    {article.summary && (
                      <p className="text-xs text-gray-500 line-clamp-2 leading-relaxed">
                        {article.summary}
                      </p>
                    )}
                    {article.tags?.length > 0 && (
                      <div className="flex gap-1 mt-1.5">
                        {article.tags.slice(0, 5).map((tag) => (
                          <span key={tag} className="text-[10px] px-1.5 py-0.5 bg-gray-800 text-gray-500 rounded">
                            {tag}
                          </span>
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
