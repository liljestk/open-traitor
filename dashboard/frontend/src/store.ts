import { create } from 'zustand'
import type { LiveEvent } from './api'

const MAX_EVENTS = 200

interface LiveStore {
  profile: string
  setProfile: (p: string) => void
  events: LiveEvent[]
  connected: boolean
  setConnected: (v: boolean) => void
  addEvent: (e: LiveEvent) => void
  clearEvents: () => void
}

export const useLiveStore = create<LiveStore>((set) => ({
  profile: localStorage.getItem('auto_traitor_profile') || '',
  setProfile: (profile) => {
    localStorage.setItem('auto_traitor_profile', profile)
    set({ profile })
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
