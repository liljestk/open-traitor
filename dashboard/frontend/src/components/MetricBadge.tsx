/**
 * MetricBadge — small colored chip for deltas, sentiment, status.
 * Uses the .ot-chip utility classes from index.css.
 */
import type { ReactNode } from 'react'
import { ArrowDownRight, ArrowUpRight, Minus } from 'lucide-react'

export type MetricTone = 'positive' | 'negative' | 'warning' | 'info' | 'neutral'

interface BadgeProps {
  tone?: MetricTone
  icon?: ReactNode
  children: ReactNode
  title?: string
}

export default function MetricBadge({ tone = 'neutral', icon, children, title }: BadgeProps) {
  return (
    <span className={`ot-chip ot-chip-${tone}`} title={title}>
      {icon}
      {children}
    </span>
  )
}

interface DeltaProps {
  /** % change. Positive = up, negative = down. */
  pct?: number | null
  /** Absolute change in currency. Mutually compatible with pct. */
  abs?: number | null
  /** Provide a formatter for `abs`. */
  format?: (v: number) => string
  /** Treat zero as neutral (default). Set false to color zeros as positive. */
  zeroNeutral?: boolean
}

export function DeltaBadge({ pct, abs, format, zeroNeutral = true }: DeltaProps) {
  const value = pct != null ? pct : abs
  if (value == null || !Number.isFinite(value)) {
    return <MetricBadge tone="neutral">—</MetricBadge>
  }
  const tone: MetricTone =
    value > 0 ? 'positive' : value < 0 ? 'negative' : zeroNeutral ? 'neutral' : 'positive'
  const Icon = value > 0 ? ArrowUpRight : value < 0 ? ArrowDownRight : Minus
  const text =
    pct != null
      ? `${value > 0 ? '+' : ''}${value.toFixed(2)}%`
      : format
      ? `${value > 0 ? '+' : ''}${format(Math.abs(value)).replace(/^-/, '')}`
      : `${value > 0 ? '+' : ''}${value.toFixed(2)}`
  return (
    <MetricBadge tone={tone} icon={<Icon size={11} />}>
      {text}
    </MetricBadge>
  )
}

/** Sentiment chip: bullish / bearish / neutral. */
export function SentimentBadge({ score }: { score: number | null | undefined }) {
  if (score == null || !Number.isFinite(score)) {
    return <MetricBadge tone="neutral">—</MetricBadge>
  }
  if (score > 0.15) return <MetricBadge tone="positive">Bullish</MetricBadge>
  if (score < -0.15) return <MetricBadge tone="negative">Bearish</MetricBadge>
  return <MetricBadge tone="neutral">Neutral</MetricBadge>
}
