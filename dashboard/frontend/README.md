# OpenTraitor Dashboard

Real-time trading dashboard for the OpenTraitor system. Built with **Vue 3**, **TypeScript**, and **Vite**. Served by the FastAPI backend at port 8090.

## Features

- **Profile-Isolated Views** — Crypto (Coinbase) and Equities (IBKR) are fully separated; no combined views
- **Real-Time Updates** — WebSocket streaming for live trade events, signals, and alerts
- **Trade Management** — View open/closed positions, P&L, trade history with CSV export
- **Portfolio Analytics** — Daily/monthly performance, drawdown charts, win rate tracking
- **Strategy Monitor** — Prediction accuracy, strategy ensemble weights, learning curves
- **Planning Dashboard** — Daily/weekly/monthly strategic plans from Temporal workflows
- **LLM Analytics** — Token usage, provider routing, cost tracking via Langfuse
- **News Feed** — Ticker-specific news with sentiment indicators
- **Settings Control** — Runtime parameter adjustment with signed request verification
- **Manual Commands** — Execute trades and parameter updates with HMAC request signing

## Tech Stack

- **Framework:** Vue 3 (Composition API)
- **Language:** TypeScript
- **Build:** Vite
- **State:** Pinia store with profile context
- **Styling:** TailwindCSS + PostCSS
- **API Client:** Axios with request signing + CSRF protection
- **Query Cache:** TanStack Query (all queryKeys include `profile` — enforced by CI)

## Domain Separation

Every `useQuery` call **must** include `profile` in its `queryKey` array. This is enforced by `test_domain_separation.py` and blocks commits via pre-commit hook.

```typescript
// ✅ Correct
useQuery({ queryKey: ['trades', profile], ... })

// ❌ Blocked by CI
useQuery({ queryKey: ['trades'], ... })
```

## Development

```bash
npm install
npm run dev          # Dev server with HMR
npm run build        # Production build
npm run lint         # ESLint check
```

The dev server proxies API requests to the FastAPI backend at `localhost:8090`.
