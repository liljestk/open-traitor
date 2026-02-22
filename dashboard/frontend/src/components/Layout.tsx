import { useEffect, useRef } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { BarChart2, Activity, BookOpen, List, Terminal, Zap, Radio, FlaskConical, Sliders, ChevronDown, TrendingUp, Newspaper, Eye, Shield } from 'lucide-react'
import { useLiveStore } from '../store'
import { openLiveSocket } from '../api'

const PROFILES = [
  { id: '', label: 'Default', sub: 'All Systems' },
  { id: 'crypto', label: 'Crypto', sub: 'EUR' },
  { id: 'nordnet', label: 'Equities', sub: 'SEK' },
  { id: 'ibkr', label: 'Equities', sub: 'USD' },
]

const NAV = [
  {
    section: 'Trading',
    items: [
      { to: '/', icon: <BarChart2 size={16} />, label: 'Cycle Explorer' },
      { to: '/trades', icon: <List size={16} />, label: 'Trades Log' },
      { to: '/analytics', icon: <TrendingUp size={16} />, label: 'Analytics' },
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

const PAGE_TITLES: Record<string, string> = {
  '/': 'Cycle Explorer',
  '/trades': 'Trades Log',
  '/analytics': 'Analytics',
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
  const { setConnected, addEvent } = useLiveStore()
  const wsRef = useRef<WebSocket | null>(null)
  const location = useLocation()

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

  const pageTitle = Object.entries(PAGE_TITLES)
    .reverse()
    .find(([path]) => location.pathname.startsWith(path))?.[1] ?? 'Dashboard'

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
              {PROFILES.map((p) => (
                <option key={p.id} value={p.id} style={{ background: '#161b22', color: '#e6edf3' }}>
                  {p.label} ({p.sub})
                </option>
              ))}
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
