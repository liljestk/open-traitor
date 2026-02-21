/**
 * LiveMonitor — real-time WebSocket stream of LLM span events.
 * Connects to /ws/live and renders events as they arrive.
 */
import { useEffect, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import dayjs from 'dayjs'
import type { LiveEvent } from '../api'
import { useLiveStore } from '../store'
import EmptyState from '../components/EmptyState'
import PageTransition from '../components/PageTransition'

const AGENT_COLORS: Record<string, string> = {
  market_analyst: 'border-l-brand-500',
  strategist: 'border-l-purple-500',
  risk_manager: 'border-l-amber-500',
  executor: 'border-l-emerald-500',
}

function EventCard({ event, index }: { event: LiveEvent; index: number }) {
  if (event.type === 'ping') return null
  const color = AGENT_COLORS[event.agent_name ?? ''] ?? 'border-l-gray-600'

  return (
    <motion.div
      key={index}
      initial={{ opacity: 0, y: -12 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.25 }}
      className={`bg-gray-900 border border-gray-800 border-l-4 ${color} rounded-lg px-4 py-3`}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="text-sm font-semibold text-gray-200 capitalize">
            {event.agent_name?.replace('_', ' ') ?? 'system'}
          </span>
          {event.pair && (
            <span className="text-xs font-mono text-brand-400">{event.pair}</span>
          )}
          {event.latency_ms != null && (
            <span className="text-xs bg-gray-800 px-2 py-0.5 rounded text-gray-400">
              {event.latency_ms.toFixed(0)} ms
            </span>
          )}
        </div>
        <span className="text-xs text-gray-500">
          {event.ts ? dayjs(event.ts).format('HH:mm:ss') : ''}
        </span>
      </div>

      <div className="mt-1.5 flex items-center gap-4 text-xs text-gray-500">
        {event.model && <span>model: {event.model}</span>}
        {event.prompt_tokens != null && (
          <span>{event.prompt_tokens + (event.completion_tokens ?? 0)} tokens</span>
        )}
        {event.langfuse_trace_id && (
          <span className="text-gray-600 font-mono" title="View trace via Cycle Playback">
            trace: {event.langfuse_trace_id.slice(0, 8)}…
          </span>
        )}
      </div>
    </motion.div>
  )
}

export default function LiveMonitor() {
  const { events, connected, clearEvents } = useLiveStore()
  const bottomRef = useRef<HTMLDivElement>(null)

  // Auto-scroll to latest
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [events.length])

  const spanEvents = events.filter((e) => e.type !== 'ping')

  return (
    <PageTransition>
    <div className="p-6 flex flex-col h-full">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-xl font-bold text-gray-100">Live Monitor</h2>
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2 text-sm">
            <span
              className={`inline-block w-2.5 h-2.5 rounded-full ${connected ? 'bg-green-400 animate-pulse' : 'bg-gray-600'}`}
            />
            <span className="text-gray-400">{connected ? 'Connected' : 'Reconnecting…'}</span>
          </div>
          <button
            onClick={clearEvents}
            className="text-xs px-3 py-1.5 bg-gray-800 rounded-lg hover:bg-gray-700 text-gray-400"
          >
            Clear
          </button>
        </div>
      </div>

      {spanEvents.length === 0 && (
        <div className="flex-1 flex items-center justify-center">
          <EmptyState
            icon="live"
            title="Waiting for LLM events"
            description="Events will stream in real-time as the trading bot processes cycles."
          />
        </div>
      )}

      <div className="flex-1 overflow-auto space-y-2 pr-1">
        <AnimatePresence initial={false}>
          {spanEvents.map((event, i) => (
            <EventCard key={i} event={event} index={i} />
          ))}
        </AnimatePresence>
        <div ref={bottomRef} />
      </div>
    </div>
    </PageTransition>
  )
}
