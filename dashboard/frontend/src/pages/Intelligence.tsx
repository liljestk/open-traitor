/**
 * Model Intelligence — unified shell that groups the four model-diagnostic
 * pages (Signals, Patterns, Regressions, Cross-Asset) under one route with
 * a sticky tab bar.
 *
 * Each sub-page is lazy-loaded so initial paint only fetches data for the
 * active tab. Tab selection is synced to the URL via `?tab=` so deep links
 * (and the legacy `/predictions`, `/patterns`, `/regression`, `/cross-asset`
 * redirects in App.tsx) keep working.
 *
 * Domain separation: each child page already includes `profile` in its
 * useQuery keys — this shell adds no new queries.
 */
import { Suspense, lazy, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Crosshair, Sparkles, Brain, Network } from 'lucide-react'

const Predictions = lazy(() => import('./Predictions'))
const PatternsPage = lazy(() => import('./PatternsPage'))
const RegressionAI = lazy(() => import('./RegressionAI'))
const CrossAsset = lazy(() => import('./CrossAsset'))

type TabId = 'signals' | 'patterns' | 'regressions' | 'cross-asset'

const TABS: { id: TabId; label: string; icon: ReactNode; hint: string }[] = [
  { id: 'signals',     label: 'Signals',     icon: <Crosshair size={14} />, hint: 'Per-pair prediction accuracy' },
  { id: 'patterns',    label: 'Patterns',    icon: <Sparkles size={14} />,  hint: 'Catalyst → historical analogs' },
  { id: 'regressions', label: 'Regressions', icon: <Brain size={14} />,     hint: 'Per-symbol model drill-down' },
  { id: 'cross-asset', label: 'Cross-Asset', icon: <Network size={14} />,   hint: 'Clusters, correlations, cascades' },
]

function isTab(v: string | null): v is TabId {
  return v === 'signals' || v === 'patterns' || v === 'regressions' || v === 'cross-asset'
}

export default function Intelligence() {
  const [params, setParams] = useSearchParams()
  const raw = params.get('tab')
  const active: TabId = isTab(raw) ? raw : 'signals'

  const setTab = (id: TabId) => {
    const next = new URLSearchParams(params)
    next.set('tab', id)
    setParams(next, { replace: true })
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: '100%' }}>
      {/* Sticky tab strip */}
      <div
        style={{
          position: 'sticky',
          top: 0,
          zIndex: 10,
          background: '#0d1117',
          borderBottom: '1px solid #21262d',
          padding: '10px 20px 0',
          display: 'flex',
          gap: 4,
          overflowX: 'auto',
        }}
      >
        {TABS.map((t) => {
          const isActive = t.id === active
          return (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              title={t.hint}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                padding: '8px 14px',
                background: 'transparent',
                color: isActive ? '#e6edf3' : '#8b949e',
                border: 'none',
                borderBottom: isActive ? '2px solid #22c55e' : '2px solid transparent',
                cursor: 'pointer',
                fontSize: 13,
                fontWeight: isActive ? 600 : 500,
                whiteSpace: 'nowrap',
                marginBottom: -1,
                transition: 'color 0.1s',
              }}
            >
              {t.icon}
              {t.label}
            </button>
          )
        })}
      </div>

      {/* Tab body */}
      <div style={{ flex: 1, minHeight: 0 }}>
        <Suspense
          fallback={
            <div style={{ padding: 40, color: '#8b949e', fontSize: 13 }}>Loading…</div>
          }
        >
          {active === 'signals' && <Predictions />}
          {active === 'patterns' && <PatternsPage />}
          {active === 'regressions' && <RegressionAI />}
          {active === 'cross-asset' && <CrossAsset />}
        </Suspense>
      </div>
    </div>
  )
}
