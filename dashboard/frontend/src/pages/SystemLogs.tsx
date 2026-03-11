import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Search, RefreshCw, Info, AlertTriangle, AlertCircle, XCircle } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import { fetchEvents } from '../api'
import { SkeletonLogEntries } from '../components/Skeleton'
import EmptyState from '../components/EmptyState'
import PageTransition from '../components/PageTransition'

type Severity = 'info' | 'warning' | 'error' | 'critical' | string

const SEV_CONFIG: Record<string, { color: string; bg: string; icon: React.ReactNode; label: string }> = {
    info: { color: '#58a6ff', bg: '#58a6ff18', icon: <Info size={13} />, label: 'INFO' },
    warning: { color: '#d29922', bg: '#d2992218', icon: <AlertTriangle size={13} />, label: 'WARN' },
    error: { color: '#f85149', bg: '#f8514918', icon: <AlertCircle size={13} />, label: 'ERR' },
    critical: { color: '#ff6e6e', bg: '#ff6e6e18', icon: <XCircle size={13} />, label: 'CRIT' },
}

function getSevConfig(sev: Severity) {
    return SEV_CONFIG[sev?.toLowerCase()] ?? SEV_CONFIG.info
}

const inputStyle: React.CSSProperties = {
    background: '#161b22', border: '1px solid #30363d',
    borderRadius: 6, padding: '6px 10px', color: '#e6edf3',
    fontSize: 13, outline: 'none', fontFamily: 'inherit',
}

const btnBase: React.CSSProperties = {
    display: 'flex', alignItems: 'center', gap: 6,
    padding: '6px 8px', borderRadius: 6, fontSize: 13, fontWeight: 500,
    cursor: 'pointer', border: '1px solid #30363d', background: '#21262d',
    color: '#8b949e', transition: 'all 0.15s', fontFamily: 'inherit',
}

export default function SystemLogs() {
    const [eventTypeFilter, setEventTypeFilter] = useState('')
    const [hours, setHours] = useState(168)
    const [limit, setLimit] = useState(500)
    const [expandedId, setExpandedId] = useState<number | null>(null)

    const { data, isLoading, isFetching, refetch } = useQuery({
        queryKey: ['events', eventTypeFilter, limit, hours],
        queryFn: () => fetchEvents(eventTypeFilter || undefined, limit, hours),
        refetchInterval: 10000,
    })

    const events = data?.events ?? []

    const fmtDate = (iso: string) => {
        try {
            const d = new Date(iso)
            return d.toLocaleDateString('en-US', { month: 'short', day: '2-digit' }) + ' ' +
                d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
        } catch { return iso }
    }

    return (
        <PageTransition>
        <div style={{ padding: 24, display: 'flex', flexDirection: 'column', gap: 20, height: '100%', boxSizing: 'border-box' }}>

            {/* Toolbar */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                <div style={{ position: 'relative' }}>
                    <Search size={13} style={{ position: 'absolute', left: 9, top: '50%', transform: 'translateY(-50%)', color: '#6e7681' }} />
                    <input
                        style={{ ...inputStyle, paddingLeft: 28, width: 220 }}
                        type="text"
                        placeholder="Filter by event type..."
                        value={eventTypeFilter}
                        onChange={e => setEventTypeFilter(e.target.value)}
                    />
                </div>
                <select style={{ ...inputStyle, cursor: 'pointer' }} value={hours} onChange={e => setHours(Number(e.target.value))}>
                    <option value={24}>Last 24h</option>
                    <option value={168}>Last 7 days</option>
                    <option value={720}>Last 30 days</option>
                </select>

                <div style={{ flex: 1 }} />

                <button style={btnBase} onClick={() => refetch()} title="Refresh">
                    <RefreshCw size={14} className={isFetching ? 'animate-spin' : ''} style={{ color: isFetching ? '#22c55e' : undefined }} />
                </button>
            </div>

            {/* Log pane */}
            <div style={{
                flex: 1, background: '#0d1117', border: '1px solid #21262d',
                borderRadius: 8, overflow: 'hidden', display: 'flex', flexDirection: 'column', minHeight: 0,
            }}>
                {/* Terminal titlebar */}
                <div style={{
                    background: '#161b22', borderBottom: '1px solid #21262d',
                    padding: '8px 14px', display: 'flex', alignItems: 'center', gap: 8,
                    flexShrink: 0,
                }}>
                    <div style={{ width: 10, height: 10, borderRadius: '50%', background: '#f85149', opacity: 0.8 }} />
                    <div style={{ width: 10, height: 10, borderRadius: '50%', background: '#d29922', opacity: 0.8 }} />
                    <div style={{ width: 10, height: 10, borderRadius: '50%', background: '#22c55e', opacity: 0.8 }} />
                    <span style={{ marginLeft: 8, fontSize: 11, color: '#6e7681', fontFamily: 'JetBrains Mono, monospace', letterSpacing: '0.05em' }}>
                        opentraitor :: event log
                    </span>
                    <div style={{ flex: 1 }} />
                    <span style={{ fontSize: 11, color: '#6e7681' }}>{events.length} events</span>
                </div>

                {/* Entries */}
                <div style={{ flex: 1, overflowY: 'auto', padding: '8px 0' }}>
                    {isLoading && <SkeletonLogEntries count={15} />}
                    {!isLoading && events.length === 0 && (
                        <EmptyState
                            icon="logs"
                            title="No events found"
                            description="System events will appear here as the bot runs."
                        />
                    )}

                    <AnimatePresence>
                        {events.map((evt, i) => {
                            const sev = getSevConfig(evt.severity)
                            const isExpanded = expandedId === evt.id
                            const hasData = evt.data && Object.keys(evt.data).length > 0

                            return (
                                <motion.div
                                    key={evt.id}
                                    initial={{ opacity: 0, x: -4 }}
                                    animate={{ opacity: 1, x: 0 }}
                                    transition={{ delay: Math.min(i * 0.005, 0.2) }}
                                    style={{
                                        display: 'flex', flexDirection: 'column',
                                        padding: '6px 14px',
                                        borderBottom: '1px solid #161b22',
                                        cursor: hasData ? 'pointer' : 'default',
                                        background: isExpanded ? '#161b22' : 'transparent',
                                        transition: 'background 0.1s',
                                    }}
                                    onMouseEnter={e => { if (!isExpanded) e.currentTarget.style.background = '#0f1318' }}
                                    onMouseLeave={e => { if (!isExpanded) e.currentTarget.style.background = 'transparent' }}
                                    onClick={() => hasData && setExpandedId(isExpanded ? null : evt.id)}
                                >
                                    <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
                                        {/* Timestamp */}
                                        <span style={{
                                            fontFamily: 'JetBrains Mono, monospace', fontSize: 11,
                                            color: '#6e7681', flexShrink: 0, width: 130,
                                        }}>
                                            {fmtDate(evt.ts)}
                                        </span>

                                        {/* Severity badge */}
                                        <span style={{
                                            fontSize: 10, fontWeight: 700, letterSpacing: '0.06em',
                                            padding: '1px 6px', borderRadius: 3,
                                            background: sev.bg, color: sev.color,
                                            flexShrink: 0, display: 'flex', alignItems: 'center', gap: 3,
                                        }}>
                                            {sev.icon} {sev.label}
                                        </span>

                                        {/* Event type */}
                                        <span style={{
                                            fontFamily: 'JetBrains Mono, monospace', fontSize: 11,
                                            color: '#8b949e', flexShrink: 0,
                                        }}>
                                            [{evt.event_type}]
                                        </span>

                                        {/* Pair badge */}
                                        {evt.pair && (
                                            <span style={{
                                                fontSize: 11, fontFamily: 'JetBrains Mono, monospace',
                                                background: '#22c55e15', color: '#22c55e',
                                                border: '1px solid #22c55e25', borderRadius: 3,
                                                padding: '0px 5px', flexShrink: 0,
                                            }}>
                                                {evt.pair}
                                            </span>
                                        )}

                                        {/* Message */}
                                        <span style={{ fontSize: 13, color: sev.color, flex: 1, wordBreak: 'break-word' }}>
                                            {evt.message}
                                        </span>

                                        {/* Expand indicator */}
                                        {hasData && (
                                            <span style={{ color: '#484f58', fontSize: 11, flexShrink: 0 }}>
                                                {isExpanded ? '▲' : '▼'}
                                            </span>
                                        )}
                                    </div>

                                    {/* Expanded JSON */}
                                    {isExpanded && hasData && (
                                        <div style={{
                                            marginTop: 8, marginLeft: 140,
                                            background: '#080c10', border: '1px solid #30363d',
                                            borderRadius: 6, padding: '10px 14px',
                                            fontFamily: 'JetBrains Mono, monospace', fontSize: 11,
                                            color: '#8b949e', overflowX: 'auto',
                                            lineHeight: 1.6,
                                        }}>
                                            <pre style={{ margin: 0 }}>{JSON.stringify(evt.data, null, 2)}</pre>
                                        </div>
                                    )}
                                </motion.div>
                            )
                        })}
                    </AnimatePresence>
                </div>

                {/* Footer */}
                <div style={{
                    padding: '8px 14px', borderTop: '1px solid #21262d',
                    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                    background: '#0d1117', fontSize: 12, color: '#6e7681', flexShrink: 0,
                }}>
                    <span>Showing {events.length} of up to {limit} events</span>
                    <button
                        onClick={() => setLimit(l => Math.min(l + 500, 5000))}
                        disabled={events.length < limit}
                        style={{ ...btnBase, padding: '4px 10px', fontSize: 12 }}
                    >
                        Load older
                    </button>
                </div>
            </div>
        </div>
        </PageTransition>
    )
}
