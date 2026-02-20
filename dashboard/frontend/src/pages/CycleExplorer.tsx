/**
 * CycleExplorer — paginated list of trading cycles with quick stats.
 * Clicking a row navigates to the Cycle Playback page.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import dayjs from 'dayjs'
import { fetchCycles, fetchStatsSummary, type CycleSummary } from '../api'
import StatCard from '../components/StatCard'

const PAGE_SIZE = 50

function pnlColor(pnl: number | null): string {
  if (pnl == null) return 'text-gray-400'
  return pnl >= 0 ? 'text-green-400' : 'text-red-400'
}

function fmtPnl(pnl: number | null): string {
  if (pnl == null) return '—'
  const sign = pnl >= 0 ? '+' : ''
  return `${sign}€${pnl.toFixed(2)}`
}

export default function CycleExplorer() {
  const [pair, setPair] = useState('')
  const [offset, setOffset] = useState(0)
  const navigate = useNavigate()

  const pairs = ['', 'BTC-EUR', 'ETH-EUR', 'SOL-EUR', 'XRP-EUR']

  const { data: cyclesData, isLoading: cyclesLoading } = useQuery({
    queryKey: ['cycles', pair, offset],
    queryFn: () => fetchCycles(pair || undefined, PAGE_SIZE, offset),
    staleTime: 10_000,
  })

  const { data: stats } = useQuery({
    queryKey: ['stats-summary'],
    queryFn: fetchStatsSummary,
    staleTime: 30_000,
  })

  const cycles = cyclesData?.cycles ?? []

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-bold text-gray-100">Cycle Explorer</h2>
        <select
          className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200"
          value={pair}
          onChange={(e) => { setPair(e.target.value); setOffset(0) }}
        >
          {pairs.map((p) => (
            <option key={p} value={p}>{p || 'All pairs'}</option>
          ))}
        </select>
      </div>

      {/* Stats row */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-6 gap-4">
          <StatCard label="Win rate" value={stats.win_rate != null ? `${stats.win_rate}%` : '—'} accent="green" />
          <StatCard label="Total PnL" value={fmtPnl(stats.total_pnl)} accent={stats.total_pnl != null && stats.total_pnl >= 0 ? 'green' : 'red'} />
          <StatCard label="24h trades" value={stats.trades_24h} accent="blue" />
          <StatCard label="24h cycles" value={stats.cycles_24h} accent="blue" />
          <StatCard label="Active pairs" value={stats.active_pairs} />
          {stats.portfolio && (
            <StatCard label="Portfolio" value={`€${stats.portfolio.portfolio_value.toFixed(2)}`} accent="blue" />
          )}
        </div>
      )}

      {/* Table */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-800 text-xs text-gray-500 uppercase">
              {['Time', 'Pair', 'Signal', 'Action', 'Confidence', 'PnL', 'Tokens', 'Latency', 'Trace'].map((h) => (
                <th key={h} className="px-4 py-3 text-left font-medium">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {cyclesLoading && (
              <tr><td colSpan={9} className="px-4 py-8 text-center text-gray-500">Loading...</td></tr>
            )}
            {!cyclesLoading && cycles.length === 0 && (
              <tr><td colSpan={9} className="px-4 py-8 text-center text-gray-500">No cycles found</td></tr>
            )}
            {cycles.map((c: CycleSummary) => (
              <tr
                key={c.cycle_id}
                className="border-b border-gray-800/50 hover:bg-gray-800/50 cursor-pointer transition-colors"
                onClick={() => navigate(`/cycle/${encodeURIComponent(c.cycle_id)}`)}
              >
                <td className="px-4 py-2.5 text-gray-400 whitespace-nowrap">{dayjs(c.started_at).format('MM-DD HH:mm:ss')}</td>
                <td className="px-4 py-2.5 font-mono text-brand-400">{c.pair}</td>
                <td className="px-4 py-2.5 text-gray-300">{c.signal_type ?? '—'}</td>
                <td className="px-4 py-2.5">
                  <span className={`px-2 py-0.5 rounded text-xs font-semibold ${
                    c.action === 'BUY' ? 'bg-green-900/60 text-green-400' :
                    c.action === 'SELL' ? 'bg-red-900/60 text-red-400' :
                    'bg-gray-800 text-gray-400'
                  }`}>{c.action ?? 'HOLD'}</span>
                </td>
                <td className="px-4 py-2.5 text-gray-300">{c.confidence != null ? `${(c.confidence * 100).toFixed(0)}%` : '—'}</td>
                <td className={`px-4 py-2.5 font-medium ${pnlColor(c.pnl)}`}>{fmtPnl(c.pnl)}</td>
                <td className="px-4 py-2.5 text-gray-400">{c.total_prompt_tokens != null ? (c.total_prompt_tokens + (c.total_completion_tokens ?? 0)) : '—'}</td>
                <td className="px-4 py-2.5 text-gray-400">{c.total_latency_ms != null ? `${c.total_latency_ms.toFixed(0)} ms` : '—'}</td>
                <td className="px-4 py-2.5">
                  {c.langfuse_url ? (
                    <a
                      href={c.langfuse_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-brand-400 hover:underline text-xs"
                      onClick={(e) => e.stopPropagation()}
                    >
                      Open ↗
                    </a>
                  ) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {/* Pagination */}
        <div className="px-4 py-3 border-t border-gray-800 flex items-center gap-4 text-sm">
          <button
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            className="px-3 py-1 bg-gray-800 rounded disabled:opacity-40 hover:bg-gray-700"
          >
            ← Prev
          </button>
          <span className="text-gray-500">Offset {offset}</span>
          <button
            disabled={cycles.length < PAGE_SIZE}
            onClick={() => setOffset(offset + PAGE_SIZE)}
            className="px-3 py-1 bg-gray-800 rounded disabled:opacity-40 hover:bg-gray-700"
          >
            Next →
          </button>
        </div>
      </div>
    </div>
  )
}
