import type { ReactNode } from 'react'

interface Props {
  label: string
  value: ReactNode
  sub?: ReactNode
  accent?: 'green' | 'red' | 'blue' | 'gray'
}

const accentColors: Record<string, string> = {
  green: 'text-green-400',
  red: 'text-red-400',
  blue: 'text-brand-400',
  gray: 'text-gray-300',
}

export default function StatCard({ label, value, sub, accent = 'gray' }: Props) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl px-5 py-4">
      <p className="text-xs text-gray-500 uppercase tracking-widest mb-1">{label}</p>
      <p className={`text-2xl font-bold ${accentColors[accent]}`}>{value}</p>
      {sub && <p className="text-xs text-gray-500 mt-1">{sub}</p>}
    </div>
  )
}
