/**
 * SpanWaterfall — animated waterfall timeline showing each agent's span
 * relative to the cycle start time.  Bars animate in sequentially using
 * framer-motion.
 */
import { motion } from 'framer-motion'
import type { AgentSpan } from '../api'
import dayjs from 'dayjs'

const AGENT_COLORS: Record<string, string> = {
  market_analyst: 'bg-brand-500',
  strategist: 'bg-purple-500',
  risk_manager: 'bg-amber-500',
  executor: 'bg-emerald-500',
}

interface Props {
  spans: AgentSpan[]
  cycleStartMs: number   // epoch ms of cycle start
  totalLatencyMs: number
}

export default function SpanWaterfall({ spans, cycleStartMs, totalLatencyMs }: Props) {
  const maxMs = Math.max(totalLatencyMs, 1)

  return (
    <div className="space-y-2">
      {spans.map((span, i) => {
        const spanOffsetMs = dayjs(span.ts).valueOf() - cycleStartMs
        const widthPct = ((span.latency_ms ?? 0) / maxMs) * 100
        const offsetPct = (spanOffsetMs / maxMs) * 100
        const color = AGENT_COLORS[span.agent_name] ?? 'bg-gray-500'

        return (
          <div key={span.id} className="flex items-center gap-3">
            {/* Agent name */}
            <div className="w-36 text-xs text-gray-400 truncate flex-shrink-0">{span.agent_name}</div>

            {/* Bar track */}
            <div className="flex-1 h-7 bg-gray-800 rounded relative overflow-hidden">
              <motion.div
                className={`absolute top-1 bottom-1 rounded ${color} opacity-80`}
                style={{ left: `${Math.max(0, Math.min(99, offsetPct))}%` }}
                initial={{ width: 0 }}
                animate={{ width: `${Math.max(1, Math.min(100 - offsetPct, widthPct))}%` }}
                transition={{ delay: i * 0.12, duration: 0.4, ease: 'easeOut' }}
              />
              <div className="absolute inset-0 flex items-center px-2">
                <span className="text-xs text-white font-medium drop-shadow">
                  {span.latency_ms != null ? `${span.latency_ms.toFixed(0)} ms` : '—'}
                </span>
              </div>
            </div>

            {/* Tokens */}
            <div className="w-24 text-xs text-gray-500 text-right flex-shrink-0">
              {span.prompt_tokens != null
                ? `${span.prompt_tokens + (span.completion_tokens ?? 0)} tok`
                : '—'}
            </div>
          </div>
        )
      })}
    </div>
  )
}
