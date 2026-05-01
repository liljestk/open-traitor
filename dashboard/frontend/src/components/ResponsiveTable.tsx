/**
 * ResponsiveTable — renders a real `<table>` on desktop and a stack of
 * mobile cards on small screens. Strict types ensure cells are described once.
 *
 * Usage:
 *   <ResponsiveTable
 *     rows={trades}
 *     getKey={(t) => t.id}
 *     columns={[
 *       { header: 'Pair',   render: (t) => t.pair },
 *       { header: 'PnL',    render: (t) => fmt(t.pnl), align: 'right' },
 *     ]}
 *     mobileTitle={(t) => t.pair}
 *     mobileMeta={(t) => fmt(t.pnl)}
 *   />
 */
import type { ReactNode } from 'react'
import { useIsMobile } from '../store'

export interface Column<T> {
  header: string
  render: (row: T) => ReactNode
  align?: 'left' | 'right' | 'center'
  /** Hide entirely on mobile (when desktop fallback path is used). */
  desktopOnly?: boolean
  className?: string
  /** Width hint for desktop. */
  width?: string | number
}

interface Props<T> {
  rows: T[]
  columns: Column<T>[]
  getKey: (row: T, index: number) => string | number
  /** Mobile card primary title. */
  mobileTitle?: (row: T) => ReactNode
  /** Mobile card right-hand metric. */
  mobileMeta?: (row: T) => ReactNode
  /** Mobile card secondary line below title. */
  mobileSubtitle?: (row: T) => ReactNode
  /** Click handler — applies on both layouts. */
  onRowClick?: (row: T) => void
  /** Render when rows is empty. */
  empty?: ReactNode
  /** Additional class on wrapper. */
  className?: string
}

export default function ResponsiveTable<T>({
  rows,
  columns,
  getKey,
  mobileTitle,
  mobileMeta,
  mobileSubtitle,
  onRowClick,
  empty,
  className,
}: Props<T>) {
  const isMobile = useIsMobile()

  if (!rows.length) {
    return <>{empty ?? <div className="t-label" style={{ padding: 16 }}>No data</div>}</>
  }

  if (isMobile) {
    return (
      <div className={`ot-card-list ${className ?? ''}`}>
        {rows.map((row, i) => {
          const key = getKey(row, i)
          return (
            <div
              key={key}
              className="ot-card-list-item"
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              role={onRowClick ? 'button' : undefined}
              tabIndex={onRowClick ? 0 : undefined}
            >
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginBottom: 4 }}>
                <div style={{ flex: 1, minWidth: 0, fontWeight: 600, fontSize: 13, color: '#e6edf3', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {mobileTitle ? mobileTitle(row) : columns[0].render(row)}
                </div>
                {mobileMeta && (
                  <div style={{ fontWeight: 700, fontSize: 13, fontVariantNumeric: 'tabular-nums', color: '#e6edf3' }}>
                    {mobileMeta(row)}
                  </div>
                )}
              </div>
              {mobileSubtitle && (
                <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 8 }}>
                  {mobileSubtitle(row)}
                </div>
              )}
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 16px', fontSize: 11, color: '#b1bac4' }}>
                {columns.filter((c) => !c.desktopOnly).map((col) => (
                  <div key={col.header} style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                    <span className="t-label" style={{ fontSize: 9 }}>{col.header}:</span>
                    <span style={{ fontVariantNumeric: 'tabular-nums' }}>{col.render(row)}</span>
                  </div>
                ))}
              </div>
            </div>
          )
        })}
      </div>
    )
  }

  return (
    <div className={`ot-table-wrap ${className ?? ''}`}>
      <table className="ot-table">
        <thead>
          <tr>
            {columns.map((col) => (
              <th
                key={col.header}
                style={{
                  textAlign: col.align ?? 'left',
                  width: col.width,
                }}
                className={col.className}
              >
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr
              key={getKey(row, i)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              style={onRowClick ? { cursor: 'pointer' } : undefined}
            >
              {columns.map((col) => (
                <td
                  key={col.header}
                  style={{ textAlign: col.align ?? 'left' }}
                  className={col.className}
                >
                  {col.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
