import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
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

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
})

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/setup" element={<SetupWizard />} />
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
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  )
}
