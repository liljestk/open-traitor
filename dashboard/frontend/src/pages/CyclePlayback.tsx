/**
 * CyclePlayback — animated waterfall timeline for a single trading cycle.
 * Shows every agent span, token stats, LLM outputs, and trade outcome.
 */
import { useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import dayjs from 'dayjs'
import { ChevronLeft, ExternalLink } from 'lucide-react'
import { fetchCycleFull, type AgentSpan } from '../api'
import SpanWaterfall from '../components/SpanWaterfall'

function JsonViewer({ data }: { data: unknown }) {
  return (
    <pre className="bg-gray-950 text-xs text-gray-300 p-3 rounded-lg overflow-auto max-h-72 leading-relaxed">
      {JSON.stringify(data, null, 2)}
    </pre>
  )
}

function SpanDetail({ span }: { span: AgentSpan }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl">
      <button
        className="w-full flex items-center justify-between px-4 py-3 text-left"
        onClick={() => setOpen((v) => !v)}
      >
        <div className="flex items-center gap-3">
          <span className="text-sm font-semibold text-gray-200 capitalize">{span.agent_name.replace('_', ' ')}</span>
          <span className="text-xs px-2 py-0.5 bg-gray-800 rounded text-gray-400">
            {span.latency_ms != null ? `${span.latency_ms.toFixed(0)} ms` : '—'}
          </span>
          {span.signal_type && (
            <span className="text-xs px-2 py-0.5 bg-brand-900/50 text-brand-400 rounded">{span.signal_type}</span>
          )}
          {span.confidence != null && (
            <span className="text-xs text-gray-500">{(span.confidence * 100).toFixed(0)}% conf.</span>
          )}
        </div>
        <div className="flex items-center gap-4 text-xs text-gray-500">
          {span.prompt_tokens != null && (
            <span>{span.prompt_tokens + (span.completion_tokens ?? 0)} tokens</span>
          )}
          <span className="text-gray-600">{open ? '▲' : '▼'}</span>
        </div>
      </button>

      {open && (
        <div className="border-t border-gray-800 px-4 py-4 space-y-4">
          {span.raw_prompt && (
            <div>
              <p className="text-xs text-gray-500 mb-1 uppercase tracking-wider">Prompt (truncated)</p>
              <pre className="bg-gray-950 text-xs text-gray-400 p-3 rounded-lg overflow-auto max-h-48 leading-relaxed whitespace-pre-wrap">
                {span.raw_prompt}
              </pre>
            </div>
          )}
          <div>
            <p className="text-xs text-gray-500 mb-1 uppercase tracking-wider">LLM Output</p>
            <JsonViewer data={span.reasoning_json} />
          </div>
          {span.langfuse_span_id && (
            <a
              href={`http://localhost:3000/trace/${span.langfuse_trace_id}`}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-xs text-brand-400 hover:underline"
            >
              <ExternalLink size={12} />
              View in Langfuse
            </a>
          )}
        </div>
      )}
    </div>
  )
}

export default function CyclePlayback() {
  const { cycleId } = useParams<{ cycleId: string }>()
  const { data: cycle, isLoading, error } = useQuery({
    queryKey: ['cycle', cycleId],
    queryFn: () => fetchCycleFull(cycleId!),
    enabled: !!cycleId,
  })

  if (isLoading) return <div className="p-8 text-gray-400">Loading cycle…</div>
  if (error || !cycle) return <div className="p-8 text-red-400">Cycle not found</div>

  const cycleStartMs = dayjs(cycle.started_at).valueOf()

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center gap-4">
        <Link to="/" className="flex items-center gap-1 text-gray-400 hover:text-gray-200 text-sm">
          <ChevronLeft size={16} /> Back
        </Link>
        <div>
          <h2 className="text-xl font-bold text-gray-100">
            {cycle.pair} — Cycle Playback
          </h2>
          <p className="text-xs text-gray-500 mt-0.5 font-mono">{cycle.cycle_id}</p>
        </div>
        {cycle.langfuse_trace_id && (
          <a
            href={`http://localhost:3000/trace/${cycle.langfuse_trace_id}`}
            target="_blank"
            rel="noreferrer"
            className="ml-auto flex items-center gap-1.5 text-xs text-brand-400 hover:underline"
          >
            <ExternalLink size={13} /> Langfuse Trace
          </a>
        )}
      </div>

      {/* Summary strip */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
        {[
          { label: 'Started', value: dayjs(cycle.started_at).format('YYYY-MM-DD HH:mm:ss') },
          { label: 'Total latency', value: `${cycle.total_latency_ms.toFixed(0)} ms` },
          { label: 'Total tokens', value: cycle.total_tokens },
          { label: 'Agents', value: cycle.spans.length },
        ].map(({ label, value }) => (
          <div key={label} className="bg-gray-900 border border-gray-800 rounded-xl px-4 py-3">
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">{label}</p>
            <p className="text-base font-semibold text-gray-200">{value}</p>
          </div>
        ))}
      </div>

      {/* Waterfall */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
        <h3 className="text-sm font-semibold text-gray-300 mb-4">Timeline Waterfall</h3>
        <SpanWaterfall
          spans={cycle.spans}
          cycleStartMs={cycleStartMs}
          totalLatencyMs={cycle.total_latency_ms}
        />
      </div>

      {/* Span details */}
      <div className="space-y-3">
        <h3 className="text-sm font-semibold text-gray-300">Agent Spans</h3>
        {cycle.spans.map((span) => (
          <SpanDetail key={span.id} span={span} />
        ))}
      </div>

      {/* Trade result */}
      {cycle.trade && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl px-5 py-4">
          <h3 className="text-sm font-semibold text-gray-300 mb-3">Trade Outcome</h3>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            {[
              { label: 'Action', value: cycle.trade.action },
              { label: 'Price', value: `$${cycle.trade.price?.toLocaleString()}` },
              { label: 'Amount', value: `$${cycle.trade.usd_amount?.toFixed(2)}` },
              {
                label: 'PnL',
                value: cycle.trade.pnl != null
                  ? `${cycle.trade.pnl >= 0 ? '+' : ''}$${cycle.trade.pnl.toFixed(2)}`
                  : '—',
              },
            ].map(({ label, value }) => (
              <div key={label}>
                <p className="text-xs text-gray-500 mb-0.5">{label}</p>
                <p className={`font-semibold ${label === 'PnL' && cycle.trade!.pnl != null
                  ? cycle.trade!.pnl >= 0 ? 'text-green-400' : 'text-red-400'
                  : 'text-gray-200'
                }`}>{value}</p>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
