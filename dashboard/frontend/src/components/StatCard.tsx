import type { ReactNode } from 'react'
import Sparkline from './Sparkline'

interface Props {
  label: string
  value: ReactNode
  sub?: ReactNode
  accent?: 'green' | 'red' | 'blue' | 'gray'
  icon?: ReactNode
  /** Optional sparkline values shown below value. */
  sparkline?: number[]
  /** Optional delta value (% change). Shown next to value as +/-. */
  deltaPct?: number | null
  /** Optional delta absolute value with color tint. */
  delta?: ReactNode
}

const ACCENT_MAP: Record<string, { text: string; glow: string; border: string; spark: string }> = {
  green: {
    text: 'text-green-400',
    glow: '0 0 24px rgba(34,197,94,0.10)',
    border: 'border-green-900/40',
    spark: '#22c55e',
  },
  red: {
    text: 'text-red-400',
    glow: '0 0 24px rgba(248,81,73,0.10)',
    border: 'border-red-900/40',
    spark: '#ef4444',
  },
  blue: {
    text: 'text-brand-400',
    glow: '0 0 24px rgba(74,222,128,0.08)',
    border: 'border-brand-900/40',
    spark: '#4ade80',
  },
  gray: {
    text: 'text-gray-300',
    glow: 'none',
    border: 'border-gray-800',
    spark: '#6e7681',
  },
}

export default function StatCard({ label, value, sub, accent = 'gray', icon, sparkline, deltaPct, delta }: Props) {
  const cfg = ACCENT_MAP[accent] ?? ACCENT_MAP.gray
  const deltaTone =
    deltaPct == null
      ? 'text-gray-500'
      : deltaPct > 0
      ? 'text-green-400'
      : deltaPct < 0
      ? 'text-red-400'
      : 'text-gray-400'
  const deltaPrefix = deltaPct == null ? '' : deltaPct > 0 ? '+' : ''
  return (
    <div
      className={`relative rounded-xl px-5 py-4 border ${cfg.border} transition-all duration-200 hover:-translate-y-px hover:shadow-lg`}
      style={{
        background: 'linear-gradient(145deg, #0d1117 0%, #161b22 100%)',
        boxShadow: cfg.glow,
      }}
    >
      <div className="flex items-center justify-between mb-1.5">
        <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-widest">{label}</p>
        {icon && <span className="opacity-50">{icon}</span>}
      </div>
      <div className="flex items-baseline gap-2 flex-wrap">
        <p
          className={`text-2xl font-extrabold tracking-tight ${cfg.text}`}
          style={{ fontVariantNumeric: 'tabular-nums' }}
        >
          {value}
        </p>
        {deltaPct != null && Number.isFinite(deltaPct) && (
          <span
            className={`text-xs font-semibold ${deltaTone}`}
            style={{ fontVariantNumeric: 'tabular-nums' }}
          >
            {deltaPrefix}{deltaPct.toFixed(2)}%
          </span>
        )}
        {delta && <span className="text-xs">{delta}</span>}
      </div>
      {sub && <p className="text-xs text-gray-500 mt-1">{sub}</p>}
      {sparkline && sparkline.length >= 2 && (
        <div style={{ marginTop: 8 }}>
          <Sparkline values={sparkline} color={cfg.spark} width={140} height={28} />
        </div>
      )}
    </div>
  )
}
