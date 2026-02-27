import { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { BarChart2, Activity, BookOpen, List, Terminal, Zap, Radio, FlaskConical, Sliders, ChevronDown, TrendingUp, Newspaper, Eye, Shield, Crosshair, MoreHorizontal, X } from 'lucide-react'
import { useLiveStore, useIsMobile } from '../store'
import { openLiveSocket, fetchSetupConfig } from '../api'

/**
 * All possible profiles.  Filtered at render-time to only show
 * exchanges that are actually configured on the backend.
 * The `sub` label is dynamically resolved from exchangeCurrencies.
 */
const ALL_PROFILES: { id: string; label: string; exchange: string | null }[] = [
  { id: '', label: 'Default', exchange: null },
  { id: 'crypto', label: 'Crypto', exchange: 'coinbase' },
  { id: 'ibkr', label: 'Equities', exchange: 'ibkr' },
]

const NAV = [
  {
    section: 'Trading',
    items: [
      { to: '/', icon: <BarChart2 size={16} />, label: 'Cycle Explorer' },
      { to: '/trades', icon: <List size={16} />, label: 'Trades Log' },
      { to: '/analytics', icon: <TrendingUp size={16} />, label: 'Analytics' },
      { to: '/predictions', icon: <Crosshair size={16} />, label: 'Predictions' },
      { to: '/watchlist', icon: <Eye size={16} />, label: 'Watchlist' },
      { to: '/simulations', icon: <FlaskConical size={16} />, label: 'Simulate Trade' },
    ]
  },
  {
    section: 'System',
    items: [
      { to: '/logs', icon: <Terminal size={16} />, label: 'System Logs' },
      { to: '/live', icon: <Activity size={16} />, label: 'Live Monitor' },
      { to: '/risk', icon: <Shield size={16} />, label: 'Risk & Exposure' },
      { to: '/news', icon: <Newspaper size={16} />, label: 'News Feed' },
      { to: '/planning', icon: <BookOpen size={16} />, label: 'Planning Audit' },
      { to: '/settings', icon: <Sliders size={16} />, label: 'Settings' },
    ]
  },
]

// Bottom tab bar — 4 primary routes + "More"
const BOTTOM_TABS = [
  { to: '/', icon: <BarChart2 size={20} />, label: 'Cycles' },
  { to: '/trades', icon: <List size={20} />, label: 'Trades' },
  { to: '/live', icon: <Activity size={20} />, label: 'Live' },
  { to: '/analytics', icon: <TrendingUp size={20} />, label: 'Analytics' },
]

const PAGE_TITLES: Record<string, string> = {
  '/': 'Cycle Explorer',
  '/trades': 'Trades Log',
  '/analytics': 'Analytics',
  '/predictions': 'Predictions',
  '/watchlist': 'Watchlist',
  '/simulations': 'Simulate Trade',
  '/logs': 'System Logs',
  '/live': 'Live Monitor',
  '/risk': 'Risk & Exposure',
  '/news': 'News Feed',
  '/planning': 'Planning Audit',
  '/settings': 'Settings',
}

export default function Layout() {
  const connected = useLiveStore((s) => s.connected)
  const density = useLiveStore((s) => s.density)
  const availableExchanges = useLiveStore((s) => s.availableExchanges)
  const exchangeCurrencies = useLiveStore((s) => s.exchangeCurrencies)
  const { setConnected, addEvent, setAvailableExchanges, setExchangeCurrencies } = useLiveStore()
  const wsRef = useRef<WebSocket | null>(null)
  const location = useLocation()
  const isMobile = useIsMobile()
  const [moreOpen, setMoreOpen] = useState(false)

  // Fetch configured exchanges once on mount
  useEffect(() => {
    fetchSetupConfig().then((cfg) => {
      if (cfg?.exchanges) setAvailableExchanges(cfg.exchanges)
      if (cfg?.exchangeCurrencies) setExchangeCurrencies(cfg.exchangeCurrencies)
    }).catch(() => { /* keep defaults */ })
  }, [])

  // App-wide WebSocket connection so sidebar always shows live status
  useEffect(() => {
    let reconnectTimer: ReturnType<typeof setTimeout>
    function connect() {
      const ws = openLiveSocket(
        (e) => { setConnected(true); addEvent(e) },
        () => { setConnected(false); reconnectTimer = setTimeout(connect, 5000) },
      )
      wsRef.current = ws
      setConnected(true)
    }
    connect()
    return () => {
      clearTimeout(reconnectTimer)
      wsRef.current?.close()
      setConnected(false)
    }
  }, [])

  // Close "More" sheet on navigation
  useEffect(() => { setMoreOpen(false) }, [location.pathname])

  const pageTitle = Object.entries(PAGE_TITLES)
    .reverse()
    .find(([path]) => location.pathname.startsWith(path))?.[1] ?? 'Dashboard'

  const profileOptions = ALL_PROFILES.filter(
    (p) => p.exchange === null || availableExchanges[p.exchange]
  )

  // ─── Mobile layout ────────────────────────────────────────────────────────
  if (isMobile) {
    return (
      <div
        className={density === 'compact' ? 'density-compact' : ''}
        style={{ display: 'flex', flexDirection: 'column', minHeight: '100dvh', background: '#080c10' }}
      >
        {/* Mobile header */}
        <header style={{
          height: 52,
          flexShrink: 0,
          background: '#0d1117',
          borderBottom: '1px solid #21262d',
          display: 'flex',
          alignItems: 'center',
          padding: '0 16px',
          gap: 10,
          position: 'sticky',
          top: 0,
          zIndex: 30,
        }}>
          <div style={{
            width: 28, height: 28, borderRadius: 7,
            background: 'linear-gradient(135deg, #22c55e, #16a34a)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            flexShrink: 0,
          }}>
            <Zap size={14} color="white" />
          </div>
          <span style={{ fontWeight: 700, fontSize: 14, color: '#e6edf3', letterSpacing: '-0.01em' }}>
            {pageTitle}
          </span>
          <div style={{ flex: 1 }} />
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: connected ? '#22c55e' : '#6e7681' }}>
            <span style={{
              width: 6, height: 6, borderRadius: '50%',
              background: connected ? '#22c55e' : '#6e7681',
              display: 'inline-block',
            }} />
            {connected ? 'Live' : 'Off'}
          </div>
        </header>

        {/* Page content */}
        <main style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', paddingBottom: 'calc(56px + env(safe-area-inset-bottom))' }}>
          <Outlet />
        </main>

        {/* Bottom tab bar */}
        <nav style={{
          position: 'fixed',
          bottom: 0,
          left: 0,
          right: 0,
          height: 'calc(56px + env(safe-area-inset-bottom))',
          paddingBottom: 'env(safe-area-inset-bottom)',
          background: '#0d1117',
          borderTop: '1px solid #21262d',
          display: 'flex',
          alignItems: 'stretch',
          zIndex: 40,
        }}>
          {BOTTOM_TABS.map(({ to, icon, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              style={({ isActive }) => ({
                flex: 1,
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 3,
                fontSize: 10,
                fontWeight: isActive ? 600 : 400,
                color: isActive ? '#22c55e' : '#6e7681',
                textDecoration: 'none',
                transition: 'color 0.1s',
                WebkitTapHighlightColor: 'transparent',
              })}
            >
              {icon}
              {label}
            </NavLink>
          ))}

          {/* More button */}
          <button
            onClick={() => setMoreOpen(true)}
            style={{
              flex: 1,
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 3,
              fontSize: 10,
              fontWeight: 400,
              color: moreOpen ? '#22c55e' : '#6e7681',
              background: 'none',
              border: 'none',
              cursor: 'pointer',
              WebkitTapHighlightColor: 'transparent',
            }}
          >
            <MoreHorizontal size={20} />
            More
          </button>
        </nav>

        {/* "More" slide-up sheet */}
        {moreOpen && (
          <>
            {/* Backdrop */}
            <div
              onClick={() => setMoreOpen(false)}
              style={{
                position: 'fixed', inset: 0,
                background: 'rgba(0,0,0,0.6)',
                zIndex: 50,
              }}
            />
            {/* Sheet */}
            <div style={{
              position: 'fixed',
              left: 0, right: 0, bottom: 0,
              background: '#0d1117',
              borderTop: '1px solid #30363d',
              borderRadius: '16px 16px 0 0',
              zIndex: 60,
              paddingBottom: 'env(safe-area-inset-bottom)',
              maxHeight: '80dvh',
              overflowY: 'auto',
            }}>
              {/* Sheet handle + close */}
              <div style={{ display: 'flex', alignItems: 'center', padding: '14px 16px 8px', borderBottom: '1px solid #21262d' }}>
                <span style={{ fontSize: 13, fontWeight: 600, color: '#e6edf3' }}>All Pages</span>
                <div style={{ flex: 1 }} />
                <button
                  onClick={() => setMoreOpen(false)}
                  style={{ background: 'none', border: 'none', color: '#8b949e', cursor: 'pointer', padding: 4 }}
                >
                  <X size={18} />
                </button>
              </div>

              {/* Profile switcher */}
              <div style={{ padding: '10px 16px 4px' }}>
                <div style={{ fontSize: 10, fontWeight: 600, color: '#6e7681', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 6 }}>
                  Profile
                </div>
                <div style={{ position: 'relative' }}>
                  <select
                    value={useLiveStore.getState().profile}
                    onChange={(e) => {
                      useLiveStore.getState().setProfile(e.target.value)
                      window.location.reload()
                    }}
                    style={{
                      width: '100%',
                      appearance: 'none',
                      background: '#161b22',
                      border: '1px solid #30363d',
                      borderRadius: 8,
                      padding: '9px 32px 9px 10px',
                      color: '#e6edf3',
                      fontSize: 13,
                      fontWeight: 600,
                      cursor: 'pointer',
                      outline: 'none',
                      fontFamily: 'inherit',
                    }}
                  >
                    {profileOptions.map((p) => {
                      const sub = p.exchange === null
                        ? 'All Systems'
                        : (exchangeCurrencies[p.exchange] ?? 'EUR')
                      return (
                        <option key={p.id} value={p.id} style={{ background: '#161b22', color: '#e6edf3' }}>
                          {p.label} ({sub})
                        </option>
                      )
                    })}
                  </select>
                  <ChevronDown size={13} style={{ position: 'absolute', right: 10, top: '50%', transform: 'translateY(-50%)', color: '#6e7681', pointerEvents: 'none' }} />
                </div>
              </div>

              {/* Nav sections */}
              {NAV.map(({ section, items }) => (
                <div key={section} style={{ padding: '8px 16px' }}>
                  <div style={{ fontSize: 10, fontWeight: 600, color: '#6e7681', textTransform: 'uppercase', letterSpacing: '0.08em', padding: '4px 0 6px' }}>
                    {section}
                  </div>
                  {items.map(({ to, icon, label }) => (
                    <NavLink
                      key={to}
                      to={to}
                      end={to === '/'}
                      style={({ isActive }) => ({
                        display: 'flex',
                        alignItems: 'center',
                        gap: 12,
                        padding: '10px 10px',
                        borderRadius: 8,
                        fontSize: 14,
                        fontWeight: isActive ? 600 : 400,
                        color: isActive ? '#e6edf3' : '#8b949e',
                        background: isActive ? '#21262d' : 'transparent',
                        textDecoration: 'none',
                        marginBottom: 2,
                        borderLeft: isActive ? '2px solid #22c55e' : '2px solid transparent',
                        WebkitTapHighlightColor: 'transparent',
                      })}
                    >
                      <span style={{ color: 'inherit', opacity: 0.85 }}>{icon}</span>
                      {label}
                    </NavLink>
                  ))}
                </div>
              ))}

              {/* Connection status */}
              <div style={{ padding: '10px 16px 16px', display: 'flex', alignItems: 'center', gap: 8 }}>
                <Radio size={12} color={connected ? '#22c55e' : '#6e7681'} />
                <span style={{ fontSize: 12, color: connected ? '#22c55e' : '#6e7681', fontWeight: 500 }}>
                  {connected ? 'Live Feed Connected' : 'Feed Disconnected'}
                </span>
              </div>
            </div>
          </>
        )}
      </div>
    )
  }

  // ─── Desktop layout (unchanged) ───────────────────────────────────────────
  return (
    <div className={density === 'compact' ? 'density-compact' : ''} style={{ display: 'flex', height: '100vh', overflow: 'hidden', background: '#080c10' }}>
      {/* Sidebar */}
      <aside style={{
        width: '220px',
        flexShrink: 0,
        background: '#0d1117',
        borderRight: '1px solid #21262d',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
      }}>
        {/* Logo */}
        <div style={{ padding: '20px 16px 16px', borderBottom: '1px solid #21262d' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <div style={{
              width: 32, height: 32, borderRadius: 8,
              background: 'linear-gradient(135deg, #22c55e, #16a34a)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              flexShrink: 0,
            }}>
              <Zap size={16} color="white" />
            </div>
            <div>
              <div style={{ fontWeight: 700, fontSize: 14, color: '#e6edf3', letterSpacing: '-0.01em' }}>Auto-Traitor</div>
              <div style={{ fontSize: 11, color: '#8b949e', marginTop: 1 }}>LLM Trading Ops</div>
            </div>
          </div>
        </div>

        {/* Profile Switcher */}
        <div style={{ padding: '10px 8px 4px' }}>
          <div style={{ position: 'relative' }}>
            <select
              value={useLiveStore.getState().profile}
              onChange={(e) => {
                useLiveStore.getState().setProfile(e.target.value)
                window.location.reload()
              }}
              style={{
                width: '100%',
                appearance: 'none',
                background: 'linear-gradient(145deg, #161b22, #0d1117)',
                border: '1px solid #30363d',
                borderRadius: 8,
                padding: '8px 32px 8px 10px',
                color: '#e6edf3',
                fontSize: 12,
                fontWeight: 600,
                cursor: 'pointer',
                outline: 'none',
                fontFamily: 'inherit',
              }}
            >
              {ALL_PROFILES
                .filter((p) => p.exchange === null || availableExchanges[p.exchange])
                .map((p) => {
                const sub = p.exchange === null
                  ? 'All Systems'
                  : (exchangeCurrencies[p.exchange] ?? 'EUR')
                return (
                <option key={p.id} value={p.id} style={{ background: '#161b22', color: '#e6edf3' }}>
                  {p.label} ({sub})
                </option>
              )})
              }
            </select>
            <ChevronDown
              size={13}
              style={{
                position: 'absolute',
                right: 10,
                top: '50%',
                transform: 'translateY(-50%)',
                color: '#6e7681',
                pointerEvents: 'none',
              }}
            />
          </div>
        </div>

        {/* Nav */}
        <nav style={{ flex: 1, overflowY: 'auto', padding: '12px 8px' }}>
          {NAV.map(({ section, items }) => (
            <div key={section} style={{ marginBottom: 16 }}>
              <div style={{ fontSize: 10, fontWeight: 600, color: '#6e7681', textTransform: 'uppercase', letterSpacing: '0.08em', padding: '4px 8px 8px' }}>
                {section}
              </div>
              {items.map(({ to, icon, label }) => (
                <NavLink
                  key={to}
                  to={to}
                  end={to === '/'}
                  style={({ isActive }) => ({
                    display: 'flex',
                    alignItems: 'center',
                    gap: 10,
                    padding: '7px 10px',
                    borderRadius: 6,
                    fontSize: 13,
                    fontWeight: isActive ? 600 : 400,
                    color: isActive ? '#e6edf3' : '#8b949e',
                    background: isActive ? '#21262d' : 'transparent',
                    textDecoration: 'none',
                    marginBottom: 1,
                    transition: 'all 0.1s',
                    borderLeft: isActive ? '2px solid #22c55e' : '2px solid transparent',
                  })}
                >
                  <span style={{ color: 'inherit', opacity: 0.85 }}>{icon}</span>
                  {label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>

        {/* Connection status */}
        <div style={{
          padding: '10px 16px',
          borderTop: '1px solid #21262d',
          display: 'flex',
          alignItems: 'center',
          gap: 8,
        }}>
          <Radio size={12} color={connected ? '#22c55e' : '#6e7681'} />
          <span style={{ fontSize: 12, color: connected ? '#22c55e' : '#6e7681', fontWeight: 500 }}>
            {connected ? 'Live Feed Connected' : 'Feed Disconnected'}
          </span>
        </div>
      </aside>

      {/* Right panel */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', minWidth: 0 }}>
        {/* Topbar */}
        <header style={{
          height: 52,
          flexShrink: 0,
          background: '#0d1117',
          borderBottom: '1px solid #21262d',
          display: 'flex',
          alignItems: 'center',
          padding: '0 24px',
          gap: 12,
        }}>
          <h1 style={{ margin: 0, fontSize: 15, fontWeight: 600, color: '#e6edf3' }}>{pageTitle}</h1>
          <div style={{ flex: 1 }} />
          <div style={{
            display: 'flex', alignItems: 'center', gap: 6,
            fontSize: 12, color: '#8b949e',
          }}>
            <span style={{
              width: 6, height: 6, borderRadius: '50%',
              background: connected ? '#22c55e' : '#6e7681',
              display: 'inline-block',
            }} />
            {connected ? 'Real-time' : 'Polling'}
          </div>
        </header>

        {/* Page content */}
        <main style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden' }}>
          <Outlet />
        </main>
      </div>
    </div>
  )
}
