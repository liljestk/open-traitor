import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import CycleExplorer from './pages/CycleExplorer'
import CyclePlayback from './pages/CyclePlayback'
import LiveMonitor from './pages/LiveMonitor'
import PlanningAudit from './pages/PlanningAudit'

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
          <Route element={<Layout />}>
            <Route index element={<CycleExplorer />} />
            <Route path="/cycle/:cycleId" element={<CyclePlayback />} />
            <Route path="/live" element={<LiveMonitor />} />
            <Route path="/planning" element={<PlanningAudit />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  )
}
