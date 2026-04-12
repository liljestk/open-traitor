import { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { BarChart2, Activity, BookOpen, List, Terminal, Zap, Radio, FlaskConical, Sliders, TrendingUp, Newspaper, Eye, Shield, Crosshair, MoreHorizontal, X, Cpu } from 'lucide-react'
import { useLiveStore, useIsMobile } from '../store'
import { openLiveSocket, fetchSetupConfig } from '../api'

/**
 * Strict two-mode profiles — no combined "All Systems" view.
 * Each profile maps to exactly one exchange backend.
 */
const ALL_PROFILES: { id: string; label: string; exchange: string }[] = [
  { id: 'crypto', label: 'Crypto', exchange: 'coinbase' },
  { id: 'ibkr', label: 'Equities', exchange: 'ibkr' },
]

/** Domain accent colors — always consistent across the entire UI */
const DOMAIN_COLORS: Record<string, { accent: string; bg: string; label: string }> = {
  crypto: { accent: '#22c55e', bg: 'rgba(34,197,94,0.10)', label: 'CRYPTO' },
  ibkr:   { accent: '#3b82f6', bg: 'rgba(59,130,246,0.10)', label: 'EQUITIES' },
}

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
      { to: '/backtesting', icon: <Zap size={16} />, label: 'Backtesting' },
    ]
  },
  {
    section: 'System',
    items: [
      { to: '/logs', icon: <Terminal size={16} />, label: 'System Logs' },
      { to: '/live', icon: <Activity size={16} />, label: 'Live Monitor' },
      { to: '/llm-analytics', icon: <Cpu size={16} />, label: 'LLM Analytics' },
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
  '/llm-analytics': 'LLM Analytics',
  '/risk': 'Risk & Exposure',
  '/news': 'News Feed',
  '/planning': 'Planning Audit',
  '/settings': 'Settings',
  '/backtesting': 'Backtesting',
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
        (code) => {
          setConnected(false)
          // 1008 = Policy Violation (auth failed) — session expired, force re-login
          if (code === 1008) {
            window.location.reload()
            return
          }
          reconnectTimer = setTimeout(connect, 5000)
        },
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
    (p) => availableExchanges[p.exchange]
  )

  // Resolve current profile to a domain key (never empty)
  const currentProfile = useLiveStore.getState().profile || 'crypto'
  const domain = DOMAIN_COLORS[currentProfile] ?? DOMAIN_COLORS.crypto
  const currentProfileObj = ALL_PROFILES.find((p) => p.id === currentProfile) ?? ALL_PROFILES[0]

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
          borderBottom: `2px solid ${domain.accent}`,
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
            background: `linear-gradient(135deg, ${domain.accent}, ${domain.accent}aa)`,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            flexShrink: 0,
          }}>
            <Zap size={14} color="white" />
          </div>
          <span style={{ fontWeight: 700, fontSize: 14, color: '#e6edf3', letterSpacing: '-0.01em' }}>
            {pageTitle}
          </span>
          <span style={{
            fontSize: 9, fontWeight: 700, letterSpacing: '0.1em',
            color: domain.accent, background: domain.bg,
            padding: '2px 6px', borderRadius: 4,
          }}>
            {domain.label}
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

              {/* Domain switcher — segmented control */}
              <div style={{ padding: '10px 16px 4px' }}>
                <div style={{ fontSize: 10, fontWeight: 600, color: '#6e7681', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 6 }}>
                  Domain
                </div>
                <div style={{
                  display: 'flex', gap: 0, borderRadius: 8, overflow: 'hidden',
                  border: '1px solid #30363d', background: '#161b22',
                }}>
                  {profileOptions.map((p) => {
                    const isActive = currentProfile === p.id
                    const dc = DOMAIN_COLORS[p.id] ?? DOMAIN_COLORS.crypto
                    const ccy = exchangeCurrencies[p.exchange] ?? 'EUR'
                    return (
                      <button
                        key={p.id}
                        onClick={() => {
                          if (!isActive) {
                            useLiveStore.getState().setProfile(p.id)
                            window.location.reload()
                          }
                        }}
                        style={{
                          flex: 1,
                          padding: '10px 0',
                          background: isActive ? dc.bg : 'transparent',
                          border: 'none',
                          borderRight: '1px solid #30363d',
                          color: isActive ? dc.accent : '#8b949e',
                          fontSize: 13, fontWeight: isActive ? 700 : 500,
                          cursor: isActive ? 'default' : 'pointer',
                          fontFamily: 'inherit',
                          transition: 'all 0.15s',
                        }}
                      >
                        {p.label}
                        <span style={{ fontSize: 10, opacity: 0.7, marginLeft: 4 }}>({ccy})</span>
                      </button>
                    )
                  })}
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
                        borderLeft: isActive ? `2px solid ${domain.accent}` : '2px solid transparent',
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
        {/* Logo + domain badge */}
        <div style={{ padding: '20px 16px 16px', borderBottom: `2px solid ${domain.accent}` }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <img src="/logo.png" alt="OpenTraitor" style={{
              width: 32, height: 32, borderRadius: 8, flexShrink: 0,
            }} />
            <div>
              <div style={{ fontWeight: 700, fontSize: 14, color: '#e6edf3', letterSpacing: '-0.01em' }}>OpenTraitor</div>
              <div style={{ fontSize: 11, color: '#8b949e', marginTop: 1 }}>LLM Trading Ops</div>
            </div>
          </div>
          {/* Domain indicator strip */}
          <div style={{
            marginTop: 12,
            display: 'flex', alignItems: 'center', gap: 6,
            padding: '5px 8px', borderRadius: 6,
            background: domain.bg,
          }}>
            <span style={{
              width: 7, height: 7, borderRadius: '50%',
              background: domain.accent, display: 'inline-block',
            }} />
            <span style={{ fontSize: 10, fontWeight: 700, color: domain.accent, letterSpacing: '0.1em' }}>
              {domain.label}
            </span>
            <span style={{ fontSize: 10, color: '#8b949e', marginLeft: 'auto' }}>
              {exchangeCurrencies[currentProfileObj.exchange] ?? 'EUR'}
            </span>
          </div>
        </div>

        {/* Domain Switcher — segmented toggle */}
        <div style={{ padding: '10px 8px 4px' }}>
          <div style={{
            display: 'flex', gap: 0, borderRadius: 8, overflow: 'hidden',
            border: '1px solid #30363d', background: '#161b22',
          }}>
            {ALL_PROFILES
              .filter((p) => availableExchanges[p.exchange])
              .map((p, i, arr) => {
              const isActive = currentProfile === p.id
              const dc = DOMAIN_COLORS[p.id] ?? DOMAIN_COLORS.crypto
              const ccy = exchangeCurrencies[p.exchange] ?? 'EUR'
              return (
                <button
                  key={p.id}
                  onClick={() => {
                    if (!isActive) {
                      useLiveStore.getState().setProfile(p.id)
                      window.location.reload()
                    }
                  }}
                  style={{
                    flex: 1,
                    padding: '8px 0',
                    background: isActive ? dc.bg : 'transparent',
                    border: 'none',
                    borderRight: i < arr.length - 1 ? '1px solid #30363d' : 'none',
                    color: isActive ? dc.accent : '#6e7681',
                    fontSize: 11, fontWeight: isActive ? 700 : 500,
                    cursor: isActive ? 'default' : 'pointer',
                    fontFamily: 'inherit',
                    transition: 'all 0.15s',
                    letterSpacing: '0.02em',
                  }}
                >
                  {p.label}
                  <span style={{ fontSize: 9, opacity: 0.7, marginLeft: 3 }}>({ccy})</span>
                </button>
              )
            })}
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
                    borderLeft: isActive ? `2px solid ${domain.accent}` : '2px solid transparent',
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
          <span style={{
            fontSize: 9, fontWeight: 700, letterSpacing: '0.1em',
            color: domain.accent, background: domain.bg,
            padding: '3px 8px', borderRadius: 4,
          }}>
            {domain.label}
          </span>
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
