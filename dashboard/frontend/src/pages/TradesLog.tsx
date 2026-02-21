import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download, Search, RefreshCw, TrendingUp, TrendingDown, CircleDollarSign, Hash } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import { fetchTrades, exportTradesUrl } from '../api'
import { SkeletonTable } from '../components/Skeleton'
import EmptyState from '../components/EmptyState'
import PageTransition from '../components/PageTransition'
import { useCurrencyFormatter } from '../store'

const inputStyle: React.CSSProperties = {
    background: '#161b22',
    border: '1px solid #30363d',
    borderRadius: 6,
    padding: '6px 10px',
    color: '#e6edf3',
    fontSize: 13,
    outline: 'none',
    fontFamily: 'inherit',
}

const btnBase: React.CSSProperties = {
    display: 'flex', alignItems: 'center', gap: 6,
    padding: '6px 12px', borderRadius: 6, fontSize: 13,
    fontWeight: 500, cursor: 'pointer', border: '1px solid #30363d',
    background: '#21262d', color: '#8b949e',
    transition: 'all 0.15s', fontFamily: 'inherit',
}

const btnPrimary: React.CSSProperties = {
    ...btnBase, background: '#22c55e20', borderColor: '#22c55e50',
    color: '#22c55e',
}

export default function TradesLog() {
    const [pairFilter, setPairFilter] = useState('')
    const [hours, setHours] = useState(168)
    const [limit, setLimit] = useState(500)
    const fmtCurrency = useCurrencyFormatter()

    const { data, isLoading, isFetching, refetch } = useQuery({
        queryKey: ['trades', pairFilter, limit, hours],
        queryFn: () => fetchTrades(pairFilter || undefined, limit, hours),
        refetchInterval: 30000,
    })

    const fmtDate = (iso: string) => {
        try {
            const d = new Date(iso)
            return d.toLocaleDateString('en-US', { month: 'short', day: '2-digit' }) + ' ' +
                d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
        } catch { return iso }
    }

    const trades = data?.trades ?? []
    const wins = trades.filter(t => (t.pnl ?? 0) > 0).length
    const losses = trades.filter(t => (t.pnl ?? 0) < 0).length
    const netPnl = trades.reduce((a, b) => a + (b.pnl ?? 0), 0)
    const winRate = trades.length > 0 ? Math.round((wins / trades.length) * 100) : 0

    return (
        <PageTransition>
        <div style={{ padding: 24, display: 'flex', flexDirection: 'column', gap: 20, height: '100%', boxSizing: 'border-box' }}>

            {/* Toolbar */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                <div style={{ position: 'relative', flex: '0 0 auto' }}>
                    <Search size={13} style={{ position: 'absolute', left: 9, top: '50%', transform: 'translateY(-50%)', color: '#6e7681' }} />
                    <input
                        style={{ ...inputStyle, paddingLeft: 28, width: 200 }}
                        type="text"
                        placeholder="Filter by pair..."
                        value={pairFilter}
                        onChange={e => setPairFilter(e.target.value)}
                    />
                </div>
                <select
                    style={{ ...inputStyle, cursor: 'pointer' }}
                    value={hours}
                    onChange={e => setHours(Number(e.target.value))}
                >
                    <option value={24}>Last 24h</option>
                    <option value={168}>Last 7 days</option>
                    <option value={720}>Last 30 days</option>
                    <option value={8760}>All time</option>
                </select>

                <div style={{ flex: 1 }} />

                <button
                    style={{ ...btnBase, padding: '6px 8px' }}
                    onClick={() => refetch()}
                    title="Refresh"
                >
                    <RefreshCw size={14} className={isFetching ? 'animate-spin' : ''} style={{ color: isFetching ? '#22c55e' : undefined }} />
                </button>
                <button style={btnPrimary} onClick={() => window.open(exportTradesUrl(hours), '_blank')}>
                    <Download size={14} />
                    Export CSV
                </button>
            </div>

            {/* Stats strip */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12 }}>
                {[
                    { label: 'Total Trades', value: trades.length, icon: <Hash size={14} />, color: '#8b949e' },
                    { label: 'Win Rate', value: `${winRate}%`, icon: <TrendingUp size={14} />, color: '#22c55e' },
                    { label: 'Losing', value: losses, icon: <TrendingDown size={14} />, color: '#f85149' },
                    {
                        label: 'Net PnL',
                        value: fmtCurrency(netPnl),
                        icon: <CircleDollarSign size={14} />,
                        color: netPnl >= 0 ? '#22c55e' : '#f85149',
                        mono: true,
                    },
                ].map(card => (
                    <div key={card.label} style={{
                        background: '#0d1117', border: '1px solid #21262d',
                        borderRadius: 8, padding: '14px 16px',
                        display: 'flex', flexDirection: 'column', gap: 8,
                    }}>
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                            <span style={{ fontSize: 11, color: '#8b949e', fontWeight: 500, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                                {card.label}
                            </span>
                            <span style={{ color: card.color, opacity: 0.7 }}>{card.icon}</span>
                        </div>
                        <span style={{
                            fontSize: 20, fontWeight: 700, color: card.color,
                            fontFamily: card.mono ? 'JetBrains Mono, monospace' : undefined,
                        }}>
                            {card.value}
                        </span>
                    </div>
                ))}
            </div>

            {/* Table */}
            <div style={{
                flex: 1, background: '#0d1117', border: '1px solid #21262d',
                borderRadius: 8, overflow: 'hidden', display: 'flex', flexDirection: 'column', minHeight: 0,
            }}>
                <div style={{ overflowY: 'auto', flex: 1 }}>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                        <thead style={{ position: 'sticky', top: 0, background: '#161b22', zIndex: 1 }}>
                            <tr>
                                {['Time', 'Pair', 'Action', 'Price', 'Amount', 'PnL', 'Fee', 'Signal', 'Conf.'].map(h => (
                                    <th key={h} style={{
                                        padding: '10px 14px', textAlign: 'left', fontWeight: 600,
                                        fontSize: 11, color: '#6e7681', textTransform: 'uppercase',
                                        letterSpacing: '0.05em', borderBottom: '1px solid #21262d', whiteSpace: 'nowrap',
                                    }}>
                                        {h}
                                    </th>
                                ))}
                            </tr>
                        </thead>
                        <tbody>
                            {isLoading && <SkeletonTable rows={10} cols={9} />}
                            {!isLoading && trades.length === 0 && (
                                <tr><td colSpan={9}>
                                    <EmptyState
                                        icon="trades"
                                        title="No trades yet"
                                        description="Trades will appear here once the bot executes its first order."
                                    />
                                </td></tr>
                            )}
                            <AnimatePresence>
                                {trades.map((trade, i) => {
                                    const isWin = (trade.pnl ?? 0) >= 0
                                    const isBuy = trade.action?.toLowerCase() === 'buy'
                                    return (
                                        <motion.tr
                                            key={trade.id}
                                            initial={{ opacity: 0 }}
                                            animate={{ opacity: 1 }}
                                            transition={{ delay: Math.min(i * 0.01, 0.3) }}
                                            style={{
                                                borderBottom: '1px solid #161b22',
                                                cursor: 'default',
                                            }}
                                            onMouseEnter={e => (e.currentTarget.style.background = '#161b22')}
                                            onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                                        >
                                            <td style={{ padding: '9px 14px', color: '#8b949e', fontFamily: 'JetBrains Mono, monospace', fontSize: 12, whiteSpace: 'nowrap' }}>
                                                {fmtDate(trade.ts)}
                                            </td>
                                            <td style={{ padding: '9px 14px' }}>
                                                <span style={{
                                                    background: '#21262d', border: '1px solid #30363d',
                                                    borderRadius: 4, padding: '2px 7px', fontSize: 12,
                                                    fontFamily: 'JetBrains Mono, monospace', fontWeight: 500,
                                                    color: '#e6edf3',
                                                }}>{trade.pair}</span>
                                            </td>
                                            <td style={{ padding: '9px 14px' }}>
                                                <span style={{
                                                    padding: '2px 8px', borderRadius: 4, fontSize: 11,
                                                    fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.04em',
                                                    background: isBuy ? '#22c55e18' : '#f8514918',
                                                    color: isBuy ? '#22c55e' : '#f85149',
                                                    border: `1px solid ${isBuy ? '#22c55e30' : '#f8514930'}`,
                                                }}>
                                                    {trade.action}
                                                </span>
                                            </td>
                                            <td style={{ padding: '9px 14px', textAlign: 'right', fontFamily: 'JetBrains Mono, monospace', fontSize: 12, color: '#c9d1d9' }}>
                                                {fmtCurrency(trade.price)}
                                            </td>
                                            <td style={{ padding: '9px 14px', textAlign: 'right', fontFamily: 'JetBrains Mono, monospace', fontSize: 12, color: '#c9d1d9' }}>
                                                {fmtCurrency(trade.quote_amount)}
                                            </td>
                                            <td style={{ padding: '9px 14px', textAlign: 'right' }}>
                                                {trade.pnl != null ? (
                                                    <span style={{
                                                        display: 'inline-flex', alignItems: 'center', gap: 4,
                                                        fontFamily: 'JetBrains Mono, monospace', fontSize: 12,
                                                        fontWeight: 600, color: isWin ? '#22c55e' : '#f85149',
                                                    }}>
                                                        {isWin ? <TrendingUp size={12} /> : <TrendingDown size={12} />}
                                                        {isWin ? '+' : ''}{fmtCurrency(trade.pnl)}
                                                    </span>
                                                ) : <span style={{ color: '#484f58' }}>—</span>}
                                            </td>
                                            <td style={{ padding: '9px 14px', textAlign: 'right', fontFamily: 'JetBrains Mono, monospace', fontSize: 12, color: '#8b949e' }}>
                                                {fmtCurrency(trade.fee_quote)}
                                            </td>
                                            <td style={{ padding: '9px 14px', color: '#8b949e', fontSize: 12, maxWidth: 130 }}>
                                                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'block' }}
                                                    title={trade.signal_type ?? ''}>
                                                    {trade.signal_type ?? '—'}
                                                </span>
                                            </td>
                                            <td style={{ padding: '9px 14px', textAlign: 'right', fontFamily: 'JetBrains Mono, monospace', fontSize: 12 }}>
                                                {trade.confidence != null ? (
                                                    <span style={{
                                                        color: trade.confidence > 0.8 ? '#22c55e' : trade.confidence > 0.5 ? '#d29922' : '#8b949e'
                                                    }}>
                                                        {(trade.confidence * 100).toFixed(0)}%
                                                    </span>
                                                ) : <span style={{ color: '#484f58' }}>—</span>}
                                            </td>
                                        </motion.tr>
                                    )
                                })}
                            </AnimatePresence>
                        </tbody>
                    </table>
                </div>

                {/* Footer */}
                <div style={{
                    padding: '10px 16px', borderTop: '1px solid #21262d',
                    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                    background: '#0d1117', fontSize: 12, color: '#6e7681',
                }}>
                    <span>Showing {trades.length} of up to {limit} trades</span>
                    <button
                        onClick={() => setLimit(l => Math.min(l + 500, 5000))}
                        disabled={trades.length < limit}
                        style={{ ...btnBase, padding: '4px 10px', fontSize: 12 }}
                    >
                        Load more
                    </button>
                </div>
            </div>
        </div>
        </PageTransition>
    )
}
