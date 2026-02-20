import { useEffect, useRef } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { BarChart2, Activity, BookOpen, List, Terminal, Zap, Radio, FlaskConical } from 'lucide-react'
import { useLiveStore } from '../store'
import { openLiveSocket } from '../api'

const NAV = [
  {
    section: 'Trading',
    items: [
      { to: '/', icon: <BarChart2 size={16} />, label: 'Cycle Explorer' },
      { to: '/trades', icon: <List size={16} />, label: 'Trades Log' },
      { to: '/simulations', icon: <FlaskConical size={16} />, label: 'Simulate Trade' },
    ]
  },
  {
    section: 'System',
    items: [
      { to: '/logs', icon: <Terminal size={16} />, label: 'System Logs' },
      { to: '/live', icon: <Activity size={16} />, label: 'Live Monitor' },
      { to: '/planning', icon: <BookOpen size={16} />, label: 'Planning Audit' },
    ]
  },
]

const PAGE_TITLES: Record<string, string> = {
  '/': 'Cycle Explorer',
  '/trades': 'Trades Log',
  '/simulations': 'Simulate Trade',
  '/logs': 'System Logs',
  '/live': 'Live Monitor',
  '/planning': 'Planning Audit',
}

export default function Layout() {
  const connected = useLiveStore((s) => s.connected)
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
    <div style={{ display: 'flex', height: '100vh', overflow: 'hidden', background: '#080c10' }}>
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
