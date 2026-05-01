/**
 * Treemap — pure-SVG squarified treemap for portfolio allocation.
 * No external dependencies. Suitable for ≲50 items.
 */
import { useMemo } from 'react'

export interface TreemapItem {
  label: string
  value: number
  /** Optional override color; otherwise computed from index. */
  color?: string
  /** Secondary label shown on hover (title attr). */
  sub?: string
}

interface Props {
  items: TreemapItem[]
  width?: number
  height?: number
  /** Format the value for display inside cell + tooltip. */
  format?: (v: number) => string
}

interface Rect { x: number; y: number; w: number; h: number; item: TreemapItem }

const PALETTE = [
  '#22c55e', '#3b82f6', '#a855f7', '#f59e0b', '#06b6d4',
  '#ec4899', '#10b981', '#8b5cf6', '#f97316', '#14b8a6',
  '#eab308', '#6366f1', '#ef4444', '#84cc16', '#0ea5e9',
]

/** Squarify algorithm (Bruls et al. 2000) — simplified, in-place. */
function squarify(values: TreemapItem[], x: number, y: number, w: number, h: number): Rect[] {
  const rects: Rect[] = []
  const total = values.reduce((s, v) => s + Math.max(0, v.value), 0)
  if (!values.length || total <= 0 || w <= 0 || h <= 0) return rects
  // Scale values to area
  const scaled = values.map((v) => ({
    item: v,
    area: (Math.max(0, v.value) / total) * (w * h),
  }))
  scaled.sort((a, b) => b.area - a.area)

  let row: typeof scaled = []
  let cx = x, cy = y, cw = w, ch = h

  function worst(r: typeof scaled, side: number): number {
    if (!r.length) return Infinity
    const sum = r.reduce((s, v) => s + v.area, 0)
    const max = Math.max(...r.map((v) => v.area))
    const min = Math.min(...r.map((v) => v.area))
    return Math.max((side * side * max) / (sum * sum), (sum * sum) / (side * side * min))
  }

  function layoutRow(r: typeof scaled, side: number, horizontal: boolean) {
    const sum = r.reduce((s, v) => s + v.area, 0)
    const thickness = sum / side
    let pos = horizontal ? cx : cy
    for (const v of r) {
      const len = v.area / thickness
      if (horizontal) {
        rects.push({ x: pos, y: cy, w: len, h: thickness, item: v.item })
        pos += len
      } else {
        rects.push({ x: cx, y: pos, w: thickness, h: len, item: v.item })
        pos += len
      }
    }
    if (horizontal) { cy += thickness; ch -= thickness } else { cx += thickness; cw -= thickness }
  }

  while (scaled.length) {
    const horizontal = cw < ch
    const side = horizontal ? cw : ch
    const next = scaled[0]
    const trial = [...row, next]
    if (row.length === 0 || worst(trial, side) <= worst(row, side)) {
      row.push(scaled.shift()!)
    } else {
      layoutRow(row, side, horizontal)
      row = []
    }
  }
  if (row.length) {
    const horizontal = cw < ch
    layoutRow(row, horizontal ? cw : ch, horizontal)
  }
  return rects
}

export default function Treemap({ items, width = 600, height = 300, format }: Props) {
  const filtered = items.filter((i) => Number.isFinite(i.value) && i.value > 0)
  const rects = useMemo(
    () => squarify(filtered, 0, 0, width, height),
    [filtered, width, height],
  )
  const total = filtered.reduce((s, i) => s + i.value, 0)

  if (!rects.length) {
    return (
      <div
        style={{
          width, height,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          color: '#6e7681', fontSize: 12,
          background: '#0d1117', borderRadius: 12, border: '1px solid #21262d',
        }}
      >
        No allocation data
      </div>
    )
  }

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      style={{ width: '100%', height: 'auto', display: 'block', borderRadius: 12 }}
      preserveAspectRatio="none"
      role="img"
    >
      {rects.map((r, i) => {
        const color = r.item.color ?? PALETTE[i % PALETTE.length]
        const labelable = r.w > 60 && r.h > 28
        const valueLabelable = r.w > 60 && r.h > 44
        const pct = total > 0 ? (r.item.value / total) * 100 : 0
        const tooltip = `${r.item.label}: ${format ? format(r.item.value) : r.item.value.toFixed(2)} (${pct.toFixed(1)}%)${r.item.sub ? ` — ${r.item.sub}` : ''}`
        return (
          <g key={i}>
            <title>{tooltip}</title>
            <rect
              x={r.x + 1.5}
              y={r.y + 1.5}
              width={Math.max(0, r.w - 3)}
              height={Math.max(0, r.h - 3)}
              fill={color}
              fillOpacity={0.18}
              stroke={color}
              strokeOpacity={0.6}
              strokeWidth={1}
              rx={6}
            />
            {labelable && (
              <text
                x={r.x + 8}
                y={r.y + 18}
                fontSize={11}
                fontWeight={700}
                fill="#e6edf3"
                style={{ pointerEvents: 'none' }}
              >
                {r.item.label}
              </text>
            )}
            {valueLabelable && (
              <text
                x={r.x + 8}
                y={r.y + 34}
                fontSize={10}
                fill="#8b949e"
                style={{ pointerEvents: 'none' }}
              >
                {format ? format(r.item.value) : r.item.value.toFixed(2)} · {pct.toFixed(1)}%
              </text>
            )}
          </g>
        )
      })}
    </svg>
  )
}
