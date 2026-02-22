import { create } from 'zustand'
import type { LiveEvent } from './api'

const MAX_EVENTS = 200

/**
 * Maps known profiles to their native currency.
 * Override via localStorage key "auto_traitor_profile_currencies" (JSON object).
 */
const DEFAULT_PROFILE_CURRENCIES: Record<string, string> = {
  '': 'EUR',
  crypto: 'EUR',
  nordnet: 'SEK',
  ibkr: 'USD',
}

function loadProfileCurrencies(): Record<string, string> {
  try {
    const raw = localStorage.getItem('auto_traitor_profile_currencies')
    if (raw) return { ...DEFAULT_PROFILE_CURRENCIES, ...JSON.parse(raw) }
  } catch { /* ignore */ }
  return DEFAULT_PROFILE_CURRENCIES
}

function currencyForProfile(profile: string): string {
  const map = loadProfileCurrencies()
  return map[profile.toLowerCase()] ?? 'EUR'
}

export type Density = 'comfortable' | 'compact'

interface LiveStore {
  profile: string
  currency: string
  density: Density
  setProfile: (p: string) => void
  setDensity: (d: Density) => void
  events: LiveEvent[]
  connected: boolean
  setConnected: (v: boolean) => void
  addEvent: (e: LiveEvent) => void
  clearEvents: () => void
}

const initialProfile = localStorage.getItem('auto_traitor_profile') || ''
const initialDensity = (localStorage.getItem('auto_traitor_density') || 'comfortable') as Density

export const useLiveStore = create<LiveStore>((set) => ({
  profile: initialProfile,
  currency: currencyForProfile(initialProfile),
  density: initialDensity,
  setProfile: (profile) => {
    localStorage.setItem('auto_traitor_profile', profile)
    set({ profile, currency: currencyForProfile(profile) })
  },
  setDensity: (density) => {
    localStorage.setItem('auto_traitor_density', density)
    set({ density })
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
  const currency = useLiveStore((s) => s.currency)
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
  const currency = useLiveStore((s) => s.currency)
  const parts = new Intl.NumberFormat('en-US', { style: 'currency', currency }).formatToParts(0)
  return parts.find((p) => p.type === 'currency')?.value ?? currency
}
