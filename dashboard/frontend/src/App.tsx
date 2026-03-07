import { useState, useEffect } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom'
import Layout from './components/Layout'
import CycleExplorer from './pages/CycleExplorer'
import CyclePlayback from './pages/CyclePlayback'
import LiveMonitor from './pages/LiveMonitor'
import PlanningAudit from './pages/PlanningAudit'
import TradesLog from './pages/TradesLog'
import SystemLogs from './pages/SystemLogs'
import SimulatedTrades from './pages/SimulatedTrades'
import Settings from './pages/Settings'
import Analytics from './pages/Analytics'
import NewsFeed from './pages/NewsFeed'
import Watchlist from './pages/Watchlist'
import RiskExposure from './pages/RiskExposure'
import Predictions from './pages/Predictions'
import SetupWizard from './pages/SetupWizard'
import LLMAnalytics from './pages/LLMAnalytics'
import { fetchSystemStatus, type SystemStatus } from './api'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
})

/** Checks system status and redirects to /setup on first run. */
function AppRoutes() {
  const [status, setStatus] = useState<SystemStatus | null>(null)
  const location = useLocation()
  const navigate = useNavigate()

  useEffect(() => {
    fetchSystemStatus()
      .then(s => {
        setStatus(s)
        // First run: redirect to /setup unless already there
        if (!s.setup_complete && location.pathname !== '/setup') {
          navigate('/setup', { replace: true })
        }
        // Setup complete but trying to access /setup: redirect to dashboard
        if (s.setup_complete && location.pathname === '/setup') {
          navigate('/', { replace: true })
        }
      })
      .catch(() => {
        // If status endpoint fails (dev mode / no backend), allow navigation as-is
        setStatus({ setup_complete: true, auth_configured: false, authenticated: true })
      })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Show nothing while checking status (avoids flash of wrong page)
  if (status === null) return null

  return (
    <Routes>
      <Route path="/setup" element={
        status.setup_complete
          ? <Navigate to="/" replace />
          : <SetupWizard />
      } />
      <Route element={<Layout />}>
        <Route index element={<CycleExplorer />} />
        <Route path="/cycle/:cycleId" element={<CyclePlayback />} />
        <Route path="/trades" element={<TradesLog />} />
        <Route path="/analytics" element={<Analytics />} />
        <Route path="/predictions" element={<Predictions />} />
        <Route path="/watchlist" element={<Watchlist />} />
        <Route path="/simulations" element={<SimulatedTrades />} />
        <Route path="/logs" element={<SystemLogs />} />
        <Route path="/live" element={<LiveMonitor />} />
        <Route path="/risk" element={<RiskExposure />} />
        <Route path="/news" element={<NewsFeed />} />
        <Route path="/planning" element={<PlanningAudit />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/llm-analytics" element={<LLMAnalytics />} />
      </Route>
    </Routes>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </QueryClientProvider>
  )
}
