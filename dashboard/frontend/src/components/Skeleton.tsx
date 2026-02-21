/**
 * Skeleton loading components for premium loading states.
 * Replaces plain "Loading..." text with animated pulsing placeholders.
 */

interface SkeletonProps {
  className?: string
  style?: React.CSSProperties
}

/** Generic pulsing block */
export function SkeletonBlock({ className = '', style }: SkeletonProps) {
  return (
    <div
      className={`animate-pulse rounded bg-surface-700/60 ${className}`}
      style={style}
    />
  )
}

/** Skeleton row matching a table structure */
function SkeletonTableRow({ cols }: { cols: number }) {
  return (
    <tr className="border-b border-gray-800/30">
      {Array.from({ length: cols }).map((_, i) => (
        <td key={i} className="px-4 py-3">
          <SkeletonBlock
            className="h-3.5"
            style={{ width: `${50 + Math.random() * 40}%` }}
          />
        </td>
      ))}
    </tr>
  )
}

/** Full skeleton table with configurable rows and columns */
export function SkeletonTable({ rows = 8, cols = 6 }: { rows?: number; cols?: number }) {
  return (
    <tbody>
      {Array.from({ length: rows }).map((_, i) => (
        <SkeletonTableRow key={i} cols={cols} />
      ))}
    </tbody>
  )
}

/** Skeleton for StatCard grid */
export function SkeletonStatCards({ count = 6 }: { count?: number }) {
  return (
    <>
      {Array.from({ length: count }).map((_, i) => (
        <div
          key={i}
          className="rounded-xl border border-gray-800/60 px-5 py-4 animate-pulse"
          style={{ background: '#0d1117' }}
        >
          <SkeletonBlock className="h-2.5 w-16 mb-3" />
          <SkeletonBlock className="h-6 w-20 mb-2" />
          <SkeletonBlock className="h-2 w-12" />
        </div>
      ))}
    </>
  )
}

/** Skeleton for card-list layouts (PlanningAudit, LiveMonitor) */
export function SkeletonCards({ count = 5 }: { count?: number }) {
  return (
    <div className="space-y-3">
      {Array.from({ length: count }).map((_, i) => (
        <div
          key={i}
          className="rounded-xl border border-gray-800/60 px-4 py-4 animate-pulse"
          style={{ background: '#0d1117' }}
        >
          <div className="flex items-center gap-3 mb-3">
            <SkeletonBlock className="h-4 w-16 rounded" />
            <SkeletonBlock className="h-3.5 flex-1 max-w-xs" />
          </div>
          <SkeletonBlock className="h-3 w-24" />
        </div>
      ))}
    </div>
  )
}

/** Skeleton for log entries */
export function SkeletonLogEntries({ count = 12 }: { count?: number }) {
  return (
    <div style={{ padding: '8px 0' }}>
      {Array.from({ length: count }).map((_, i) => (
        <div
          key={i}
          className="animate-pulse"
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            padding: '8px 14px',
            borderBottom: '1px solid #161b22',
          }}
        >
          <SkeletonBlock style={{ width: 110, height: 12, flexShrink: 0 }} />
          <SkeletonBlock style={{ width: 42, height: 16, flexShrink: 0, borderRadius: 3 }} />
          <SkeletonBlock style={{ width: 85, height: 12, flexShrink: 0 }} />
          <SkeletonBlock style={{ height: 12, flex: 1, maxWidth: 300 }} />
        </div>
      ))}
    </div>
  )
}
