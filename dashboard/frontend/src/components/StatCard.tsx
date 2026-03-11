import type { ReactNode } from 'react'

interface Props {
  label: string
  value: ReactNode
  sub?: ReactNode
  accent?: 'green' | 'red' | 'blue' | 'gray'
  icon?: ReactNode
}

const ACCENT_MAP: Record<string, { text: string; glow: string; border: string }> = {
  green: {
    text: 'text-green-400',
    glow: '0 0 24px rgba(34,197,94,0.08)',
    border: 'border-green-900/40',
  },
  red: {
    text: 'text-red-400',
    glow: '0 0 24px rgba(248,81,73,0.08)',
    border: 'border-red-900/40',
  },
  blue: {
    text: 'text-brand-400',
    glow: '0 0 24px rgba(74,222,128,0.06)',
    border: 'border-brand-900/40',
  },
  gray: {
    text: 'text-gray-300',
    glow: 'none',
    border: 'border-gray-800',
  },
}

export default function StatCard({ label, value, sub, accent = 'gray', icon }: Props) {
  const cfg = ACCENT_MAP[accent] ?? ACCENT_MAP.gray
  return (
    <div
      className={`relative rounded-xl px-5 py-4 border ${cfg.border} transition-shadow duration-300 hover:shadow-lg`}
      style={{
        background: 'linear-gradient(145deg, #0d1117 0%, #161b22 100%)',
        boxShadow: cfg.glow,
      }}
    >
      <div className="flex items-center justify-between mb-1.5">
        <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-widest">{label}</p>
        {icon && <span className="opacity-50">{icon}</span>}
      </div>
      <p className={`text-2xl font-extrabold tracking-tight ${cfg.text}`}>{value}</p>
      {sub && <p className="text-xs text-gray-500 mt-1">{sub}</p>}
    </div>
  )
}
