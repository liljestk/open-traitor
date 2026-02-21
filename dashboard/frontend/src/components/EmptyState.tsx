import type { ReactNode } from 'react'

interface Props {
  icon?: 'chart' | 'trades' | 'logs' | 'live' | 'planning' | 'search'
  title: string
  description?: string
  action?: { label: string; onClick: () => void }
}

const ICONS: Record<string, ReactNode> = {
  chart: (
    <svg width="48" height="48" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect x="4" y="36" width="6" height="8" rx="1.5" fill="url(#gBrand)" opacity="0.3" />
      <rect x="14" y="28" width="6" height="16" rx="1.5" fill="url(#gBrand)" opacity="0.5" />
      <rect x="24" y="20" width="6" height="24" rx="1.5" fill="url(#gBrand)" opacity="0.7" />
      <rect x="34" y="8" width="6" height="36" rx="1.5" fill="url(#gBrand)" />
      <defs>
        <linearGradient id="gBrand" x1="0" y1="0" x2="0" y2="1">
          <stop stopColor="#4ade80" />
          <stop offset="1" stopColor="#22c55e" />
        </linearGradient>
      </defs>
    </svg>
  ),
  trades: (
    <svg width="48" height="48" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M8 36L18 24L26 30L40 12" stroke="url(#gTrade)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="40" cy="12" r="3" fill="#22c55e" opacity="0.6" />
      <circle cx="18" cy="24" r="2.5" fill="#22c55e" opacity="0.4" />
      <circle cx="26" cy="30" r="2.5" fill="#22c55e" opacity="0.4" />
      <circle cx="8" cy="36" r="2.5" fill="#22c55e" opacity="0.3" />
      <defs>
        <linearGradient id="gTrade" x1="8" y1="36" x2="40" y2="12">
          <stop stopColor="#22c55e" stopOpacity="0.4" />
          <stop offset="1" stopColor="#4ade80" />
        </linearGradient>
      </defs>
    </svg>
  ),
  logs: (
    <svg width="48" height="48" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect x="6" y="10" width="36" height="4" rx="2" fill="#22c55e" opacity="0.15" />
      <rect x="6" y="18" width="28" height="4" rx="2" fill="#22c55e" opacity="0.25" />
      <rect x="6" y="26" width="32" height="4" rx="2" fill="#22c55e" opacity="0.35" />
      <rect x="6" y="34" width="24" height="4" rx="2" fill="#22c55e" opacity="0.2" />
    </svg>
  ),
  live: (
    <svg width="48" height="48" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="24" cy="24" r="6" fill="#22c55e" opacity="0.3" />
      <circle cx="24" cy="24" r="12" stroke="#22c55e" strokeWidth="1.5" opacity="0.2" />
      <circle cx="24" cy="24" r="18" stroke="#22c55e" strokeWidth="1" opacity="0.1" />
      <circle cx="24" cy="24" r="3" fill="#22c55e" opacity="0.8" />
    </svg>
  ),
  planning: (
    <svg width="48" height="48" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect x="8" y="8" width="32" height="32" rx="4" stroke="#22c55e" strokeWidth="1.5" opacity="0.3" />
      <path d="M16 18H32M16 24H28M16 30H24" stroke="#22c55e" strokeWidth="1.5" strokeLinecap="round" opacity="0.5" />
    </svg>
  ),
  search: (
    <svg width="48" height="48" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="22" cy="22" r="10" stroke="#22c55e" strokeWidth="1.5" opacity="0.4" />
      <path d="M30 30L38 38" stroke="#22c55e" strokeWidth="2" strokeLinecap="round" opacity="0.3" />
    </svg>
  ),
}

export default function EmptyState({ icon = 'chart', title, description, action }: Props) {
  return (
    <div className="flex flex-col items-center justify-center py-16 px-6 text-center select-none">
      <div className="mb-4 opacity-80">{ICONS[icon]}</div>
      <h3 className="text-sm font-semibold text-gray-400 mb-1">{title}</h3>
      {description && (
        <p className="text-xs text-gray-600 max-w-xs leading-relaxed">{description}</p>
      )}
      {action && (
        <button
          onClick={action.onClick}
          className="mt-4 text-xs px-4 py-1.5 rounded-lg bg-brand-600/20 border border-brand-600/40 text-brand-400 hover:bg-brand-600/30 transition-colors"
        >
          {action.label}
        </button>
      )}
    </div>
  )
}
