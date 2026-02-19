import { NavLink, Outlet } from 'react-router-dom'
import { BarChart2, Activity, BookOpen } from 'lucide-react'
import { useLiveStore } from '../store'

const NAV = [
  { to: '/', icon: <BarChart2 size={18} />, label: 'Cycle Explorer' },
  { to: '/live', icon: <Activity size={18} />, label: 'Live Monitor' },
  { to: '/planning', icon: <BookOpen size={18} />, label: 'Planning Audit' },
]

export default function Layout() {
  const connected = useLiveStore((s) => s.connected)

  return (
    <div className="flex min-h-screen bg-gray-950 text-gray-100">
      {/* Sidebar */}
      <aside className="w-56 flex-shrink-0 bg-gray-900 border-r border-gray-800 flex flex-col">
        <div className="px-4 py-5 border-b border-gray-800">
          <h1 className="text-lg font-bold text-brand-400 tracking-tight">Auto-Traitor</h1>
          <p className="text-xs text-gray-500 mt-0.5">LLM Trace Dashboard</p>
        </div>

        <nav className="flex-1 px-2 py-4 space-y-1">
          {NAV.map(({ to, icon, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                `flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                  isActive
                    ? 'bg-brand-600 text-white'
                    : 'text-gray-400 hover:text-gray-100 hover:bg-gray-800'
                }`
              }
            >
              {icon}
              {label}
            </NavLink>
          ))}
        </nav>

        {/* WS status indicator */}
        <div className="px-4 py-3 border-t border-gray-800 text-xs flex items-center gap-2">
          <span
            className={`inline-block w-2 h-2 rounded-full ${connected ? 'bg-green-400' : 'bg-gray-600'}`}
          />
          <span className="text-gray-500">{connected ? 'Live' : 'Disconnected'}</span>
        </div>
      </aside>

      {/* Main area */}
      <main className="flex-1 overflow-auto">
        <Outlet />
      </main>
    </div>
  )
}
