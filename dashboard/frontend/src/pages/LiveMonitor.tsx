/**
 * LiveMonitor — real-time WebSocket stream of LLM span events + HITL intervention panel.
 * Connects to /ws/live and renders events as they arrive.
 * Profile-aware: shows context for the currently selected exchange (crypto or stocks).
 */
import { useEffect, useRef, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import dayjs from 'dayjs'
import { AlertTriangle, Crosshair, PauseCircle, ShieldAlert, ChevronDown, ChevronUp, History, Activity, BarChart3 } from 'lucide-react'
import type { LiveEvent } from '../api'
import { fetchPortfolioExposure, fetchTrailingStops, fetchCommandHistory, sendTradeCommand, fetchStatsSummary } from '../api'
import { useLiveStore, useCurrencyFormatter } from '../store'
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

/* ────── Confirm modal ────── */
function ConfirmDialog({
  open,
  title,
  description,
  onConfirm,
  onCancel,
}: {
  open: boolean
  title: string
  description: string
  onConfirm: () => void
  onCancel: () => void
}) {
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="bg-gray-900 border border-gray-700 rounded-xl p-6 max-w-sm w-full shadow-xl">
        <div className="flex items-center gap-2 mb-2">
          <ShieldAlert size={18} className="text-red-400" />
          <h3 className="text-sm font-bold text-gray-100">{title}</h3>
        </div>
        <p className="text-xs text-gray-400 mb-5 leading-relaxed">{description}</p>
        <div className="flex gap-3 justify-end">
          <button
            onClick={onCancel}
            className="text-xs px-4 py-2 bg-gray-800 rounded-lg hover:bg-gray-700 text-gray-400"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            className="text-xs px-4 py-2 bg-red-600 rounded-lg hover:bg-red-500 text-white font-semibold"
          >
            Confirm
          </button>
        </div>
      </div>
    </div>
  )
}

/* ────── HITL intervention panel ────── */
function InterventionPanel() {
  const [expanded, setExpanded] = useState(true)
  const [showHistory, setShowHistory] = useState(false)
  const [confirm, setConfirm] = useState<{ pair: string; action: 'liquidate' | 'tighten_stop' | 'pause' } | null>(null)
  const fmtCurrency = useCurrencyFormatter()
  const qc = useQueryClient()
  const profile = useLiveStore((s) => s.profile)

  const { data: exposure } = useQuery({
    queryKey: ['exposure', profile],
    queryFn: fetchPortfolioExposure,
    refetchInterval: 15_000,
  })

  const { data: stops } = useQuery({
    queryKey: ['trailing-stops', profile],
    queryFn: fetchTrailingStops,
    refetchInterval: 15_000,
  })

  const { data: cmdHistory } = useQuery({
    queryKey: ['command-history', profile],
    queryFn: () => fetchCommandHistory(),
    refetchInterval: 10_000,
    enabled: showHistory,
  })

  const mutation = useMutation({
    mutationFn: (cmd: { pair: string; action: 'liquidate' | 'tighten_stop' | 'pause' }) => sendTradeCommand(cmd.pair, cmd.action),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['exposure', profile] })
      qc.invalidateQueries({ queryKey: ['trailing-stops', profile] })
      qc.invalidateQueries({ queryKey: ['command-history', profile] })
    },
  })

  const handleAction = (pair: string, action: 'liquidate' | 'tighten_stop' | 'pause') => setConfirm({ pair, action })

  const doConfirm = () => {
    if (confirm) mutation.mutate(confirm)
    setConfirm(null)
  }

  const positions = exposure?.exposure?.breakdown ?? []
  const stopsArr = Array.isArray(stops) ? stops : []
  const stopsMap = new Map(stopsArr.map((s) => [s.pair, s]))

  const actionLabel: Record<string, string> = {
    liquidate: 'Liquidate',
    tighten_stop: 'Tighten Stop',
    pause: 'Pause Pair',
  }

  return (
    <>
      <ConfirmDialog
        open={!!confirm}
        title={`${actionLabel[confirm?.action ?? ''] ?? confirm?.action} ${confirm?.pair ?? ''}?`}
        description={
          confirm?.action === 'liquidate'
            ? 'This will immediately market-sell the entire position. Cannot be undone.'
            : confirm?.action === 'tighten_stop'
              ? 'This will move the trailing stop to breakeven (entry price).'
              : 'This will add the pair to the never-trade list until manually re-enabled.'
        }
        onConfirm={doConfirm}
        onCancel={() => setConfirm(null)}
      />

      <div className="bg-gray-900/60 border border-gray-800 rounded-xl mb-4 overflow-hidden">
        <button
          onClick={() => setExpanded(!expanded)}
          className="w-full flex items-center justify-between px-4 py-3 hover:bg-gray-800/30 transition-colors"
        >
          <div className="flex items-center gap-2">
            <ShieldAlert size={14} className="text-yellow-400" />
            <span className="text-xs font-semibold text-gray-300">Intervention Panel</span>
            {positions.length > 0 && (
              <span className="text-[10px] bg-brand-600/20 text-brand-400 px-1.5 py-0.5 rounded-full">
                {positions.length} position{positions.length !== 1 ? 's' : ''}
              </span>
            )}
          </div>
          {expanded ? <ChevronUp size={14} className="text-gray-600" /> : <ChevronDown size={14} className="text-gray-600" />}
        </button>

        <AnimatePresence>
          {expanded && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="overflow-hidden"
            >
              <div className="px-4 pb-4 space-y-2">
                {positions.length === 0 ? (
                  <p className="text-xs text-gray-600 py-2">No open positions — HITL actions unavailable.</p>
                ) : (
                  positions.map((pos: import('../api').ExposureBreakdown) => {
                    const stop = stopsMap.get(pos.pair)
                    return (
                      <div key={pos.pair} className="flex items-center gap-3 bg-gray-800/40 rounded-lg px-3 py-2">
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="text-xs font-semibold text-gray-200">{pos.pair}</span>
                            <span className="text-[10px] text-gray-500 font-mono">{pos.pct_of_portfolio.toFixed(1)}%</span>
                          </div>
                          {stop && (
                            <div className="text-[10px] text-gray-500 mt-0.5">
                              Stop: {fmtCurrency(stop.stop_price)} · PnL: {stop.pnl_pct >= 0 ? '+' : ''}
                              {stop.pnl_pct.toFixed(2)}%
                            </div>
                          )}
                        </div>
                        <div className="flex gap-1.5">
                          <button
                            onClick={() => handleAction(pos.pair, 'liquidate')}
                            disabled={mutation.isPending}
                            className="text-[10px] px-2 py-1 bg-red-600/20 text-red-400 rounded hover:bg-red-600/40 transition flex items-center gap-1"
                            title="Emergency market sell"
                          >
                            <Crosshair size={10} /> Liquidate
                          </button>
                          {stop && (
                            <button
                              onClick={() => handleAction(pos.pair, 'tighten_stop')}
                              disabled={mutation.isPending}
                              className="text-[10px] px-2 py-1 bg-yellow-600/20 text-yellow-400 rounded hover:bg-yellow-600/40 transition flex items-center gap-1"
                              title="Move stop to breakeven"
                            >
                              <AlertTriangle size={10} /> Tighten
                            </button>
                          )}
                          <button
                            onClick={() => handleAction(pos.pair, 'pause')}
                            disabled={mutation.isPending}
                            className="text-[10px] px-2 py-1 bg-gray-700/50 text-gray-400 rounded hover:bg-gray-700 transition flex items-center gap-1"
                            title="Pause trading this pair"
                          >
                            <PauseCircle size={10} /> Pause
                          </button>
                        </div>
                      </div>
                    )
                  })
                )}

                {/* Mutation feedback */}
                {mutation.isSuccess && (
                  <p className="text-[10px] text-green-400 px-1">Command sent successfully.</p>
                )}
                {mutation.isError && (
                  <p className="text-[10px] text-red-400 px-1">Failed to send command.</p>
                )}

                {/* History toggle */}
                <button
                  onClick={() => setShowHistory(!showHistory)}
                  className="flex items-center gap-1 text-[10px] text-gray-500 hover:text-gray-400 mt-1"
                >
                  <History size={10} />
                  {showHistory ? 'Hide' : 'Show'} command history
                </button>

                {showHistory && cmdHistory && (
                  <div className="space-y-1 max-h-32 overflow-auto">
                    {cmdHistory.commands.length === 0 ? (
                      <p className="text-[10px] text-gray-600">No commands yet.</p>
                    ) : (
                      cmdHistory.commands.map((cmd, i) => (
                        <div key={i} className="flex items-center gap-2 text-[10px] text-gray-500 bg-gray-800/30 px-2 py-1 rounded">
                          <span className="text-gray-400 font-mono">{cmd.action}</span>
                          <span>{cmd.pair}</span>
                          <span className="ml-auto">{dayjs(cmd.ts).format('HH:mm:ss')}</span>
                        </div>
                      ))
                    )}
                  </div>
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </>
  )
}

/* ────── Agent Status Summary ────── */
function AgentStatusPanel() {
  const fmtCurrency = useCurrencyFormatter()
  const profile = useLiveStore((s) => s.profile)

  const { data: stats } = useQuery({
    queryKey: ['stats-summary', profile],
    queryFn: fetchStatsSummary,
    refetchInterval: 15_000,
  })

  if (!stats) return null

  const profileLabel = profile === 'ibkr' ? 'IBKR (Stocks)' : 'Coinbase (Crypto)'
  const portfolioVal = stats.portfolio?.portfolio_value

  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-xl mb-4 px-4 py-3">
      <div className="flex items-center gap-2 mb-2">
        <Activity size={14} className="text-brand-400" />
        <span className="text-xs font-semibold text-gray-300">Agent Status — {profileLabel}</span>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div>
          <span className="text-[10px] text-gray-500 uppercase tracking-wider">Portfolio</span>
          <p className="text-sm font-semibold text-gray-200">{portfolioVal != null ? fmtCurrency(portfolioVal) : '—'}</p>
        </div>
        <div>
          <span className="text-[10px] text-gray-500 uppercase tracking-wider">Cycles (24h)</span>
          <p className="text-sm font-semibold text-gray-200">{stats.cycles_24h ?? 0}</p>
        </div>
        <div>
          <span className="text-[10px] text-gray-500 uppercase tracking-wider">Active Pairs</span>
          <p className="text-sm font-semibold text-gray-200">{stats.active_pairs ?? 0}</p>
        </div>
        <div>
          <span className="text-[10px] text-gray-500 uppercase tracking-wider">Trades (24h)</span>
          <p className="text-sm font-semibold text-gray-200">{stats.trades_24h ?? 0}</p>
        </div>
      </div>
      {stats.total_trades > 0 && (
        <div className="mt-2 flex items-center gap-3 text-[10px] text-gray-500">
          <BarChart3 size={10} />
          <span>Win rate: {stats.win_rate != null ? `${stats.win_rate.toFixed(0)}%` : '—'}</span>
          <span>Total PnL: {stats.total_pnl != null ? fmtCurrency(stats.total_pnl) : '—'}</span>
        </div>
      )}
    </div>
  )
}

export default function LiveMonitor() {
  const { events, connected, clearEvents } = useLiveStore()
  const profile = useLiveStore((s) => s.profile)
  const bottomRef = useRef<HTMLDivElement>(null)

  // Auto-scroll to latest
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [events.length])

  const spanEvents = events.filter((e) => e.type !== 'ping')

  const profileLabel = profile === 'ibkr' ? 'IBKR (Stocks)' : 'Coinbase (Crypto)'

  return (
    <PageTransition>
    <div className="p-6 flex flex-col h-full">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <h2 className="text-xl font-bold text-gray-100">Live Monitor</h2>
          <span className="text-xs px-2 py-0.5 bg-gray-800 text-gray-400 rounded-lg">{profileLabel}</span>
        </div>
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

      {/* Agent status summary for current profile */}
      <AgentStatusPanel />

      {/* HITL intervention panel */}
      <InterventionPanel />

      {spanEvents.length === 0 && (
        <div className="flex-1 flex items-center justify-center">
          <EmptyState
            icon="live"
            title="Waiting for LLM events"
            description={`Events will stream in real-time as the ${profileLabel} agent processes trading cycles.`}
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
