import { create } from 'zustand'
import type { LiveEvent } from './api'

const MAX_EVENTS = 200

interface LiveStore {
  events: LiveEvent[]
  connected: boolean
  setConnected: (v: boolean) => void
  addEvent: (e: LiveEvent) => void
  clearEvents: () => void
}

export const useLiveStore = create<LiveStore>((set) => ({
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
