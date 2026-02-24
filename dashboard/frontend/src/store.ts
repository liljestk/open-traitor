import { create } from 'zustand'
import type { LiveEvent } from './api'

const MAX_EVENTS = 200

/**
 * Fallback currency when nothing is known about a profile.
 * The actual currencies are fetched from the backend /api/setup endpoint
 * and stored in exchangeCurrencies.
 */
const FALLBACK_CURRENCY = 'EUR'

export type Density = 'comfortable' | 'compact'

interface LiveStore {
  profile: string
  currency: string
  density: Density
  availableExchanges: Record<string, boolean>
  exchangeCurrencies: Record<string, string>
  setProfile: (p: string) => void
  setDensity: (d: Density) => void
  setAvailableExchanges: (e: Record<string, boolean>) => void
  setExchangeCurrencies: (c: Record<string, string>) => void
  events: LiveEvent[]
  connected: boolean
  setConnected: (v: boolean) => void
  addEvent: (e: LiveEvent) => void
  clearEvents: () => void
}

function currencyForProfile(profile: string, currencies: Record<string, string>): string {
  const key = profile.toLowerCase()
  // 'crypto' profile maps to coinbase exchange
  const exchangeKey = key === '' || key === 'crypto' ? 'coinbase' : key
  return currencies[exchangeKey] ?? FALLBACK_CURRENCY
}

const initialProfile = localStorage.getItem('auto_traitor_profile') || ''
const initialDensity = (localStorage.getItem('auto_traitor_density') || 'comfortable') as Density

export const useLiveStore = create<LiveStore>((set, get) => ({
  profile: initialProfile,
  currency: FALLBACK_CURRENCY,
  density: initialDensity,
  availableExchanges: { coinbase: false, ibkr: false },
  exchangeCurrencies: {},
  setProfile: (profile) => {
    localStorage.setItem('auto_traitor_profile', profile)
    set({ profile, currency: currencyForProfile(profile, get().exchangeCurrencies) })
  },
  setDensity: (density) => {
    localStorage.setItem('auto_traitor_density', density)
    set({ density })
  },
  setAvailableExchanges: (availableExchanges) => set({ availableExchanges }),
  setExchangeCurrencies: (exchangeCurrencies) => {
    const currency = currencyForProfile(get().profile, exchangeCurrencies)
    set({ exchangeCurrencies, currency })
  },
  events: [],
  connected: false,
  setConnected: (connected) => set({ connected }),
  addEvent: (event) =>
    set((state) => ({
      events:
        state.events.length >= MAX_EVENTS
          ? [...state.events.slice(1), event]
          : [...state.events, event],
    })),
  clearEvents: () => set({ events: [] }),
}))

/** Format a number as currency using the active profile's currency. */
export function useCurrencyFormatter() {
  const currency = useLiveStore((s) => s.currency) || 'EUR'
  return (val: number | null | undefined): string => {
    if (val == null) return '—'
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(val)
  }
}

/** Return the currency symbol alone (e.g. "€", "kr"). */
export function useCurrencySymbol(): string {
  const currency = useLiveStore((s) => s.currency) || 'EUR'
  const parts = new Intl.NumberFormat('en-US', { style: 'currency', currency }).formatToParts(0)
  return parts.find((p) => p.type === 'currency')?.value ?? currency
}
