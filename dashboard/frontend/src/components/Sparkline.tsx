/**
 * Sparkline — tiny inline SVG line chart, no dependencies.
 * Renders a smooth path with optional area fill and end-dot marker.
 */
import { useMemo } from 'react'

interface Props {
  values: number[]
  width?: number
  height?: number
  /** Override stroke color; otherwise auto: green if ending up, red if down. */
  color?: string
  /** Show filled area under line. */
  fill?: boolean
  /** Show dot at last value. */
  dot?: boolean
  /** Add subtle drop-shadow glow to line. */
  glow?: boolean
  className?: string
}

export default function Sparkline({
  values,
  width = 80,
  height = 24,
  color,
  fill = true,
  dot = true,
  glow = false,
  className,
}: Props) {
  const path = useMemo(() => {
    const v = values.filter((n) => Number.isFinite(n))
    if (v.length < 2) return null
    const min = Math.min(...v)
    const max = Math.max(...v)
    const range = max - min || 1
    const stepX = width / (v.length - 1)
    const points = v.map((val, i) => {
      const x = i * stepX
      const y = height - ((val - min) / range) * height
      return [x, y] as const
    })
    const lineD = points.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(2)},${y.toFixed(2)}`).join(' ')
    const areaD = `${lineD} L${width.toFixed(2)},${height} L0,${height} Z`
    return { lineD, areaD, last: points[points.length - 1] }
  }, [values, width, height])

  if (!path) {
    return <div style={{ width, height }} aria-hidden />
  }

  const auto = values.length >= 2 && (values[values.length - 1] ?? 0) >= (values[0] ?? 0)
  const stroke = color ?? (auto ? '#22c55e' : '#ef4444')

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={className}
      style={{ color: stroke, display: 'block' }}
      aria-hidden
    >
      {fill && (
        <path
          d={path.areaD}
          fill={stroke}
          opacity={0.12}
        />
      )}
      <path
        d={path.lineD}
        fill="none"
        stroke={stroke}
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
        className={glow ? 'ot-spark-glow' : ''}
      />
      {dot && (
        <circle
          cx={path.last[0]}
          cy={path.last[1]}
          r={2}
          fill={stroke}
        />
      )}
    </svg>
  )
}
