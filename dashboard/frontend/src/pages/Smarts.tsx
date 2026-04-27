/**
 * Smarts — outcome attribution, bandit posteriors, lead-lag, calendar,
 * decision drift, LLM-judge verdicts, on-chain signals, shadow strategist.
 *
 * Domain separation: every useQuery key includes `profile` (enforced by
 * tests/test_domain_separation.py).
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Brain, Activity, Calendar, GitCompare, Eye, Zap } from 'lucide-react'
import {
  fetchSmartsFeatureBrier,
  fetchSmartsBandit,
  fetchSmartsCounterfactual,
  fetchSmartsLeadLag,
  fetchSmartsUpcomingEvents,
  fetchSmartsDecisionDrift,
  fetchSmartsReasoningJudge,
  fetchSmartsOnchain,
  fetchSmartsShadow,
} from '../api'
import { useLiveStore } from '../store'
import PageTransition from '../components/PageTransition'
import EmptyState from '../components/EmptyState'

type TabId =
  | 'attribution'
  | 'bandit'
  | 'counterfactual'
  | 'leadlag'
  | 'events'
  | 'drift'
  | 'judge'
  | 'onchain'
  | 'shadow'

function fmtNum(v: number | null | undefined, digits = 3): string {
  if (v == null || Number.isNaN(v)) return '—'
  return Number(v).toFixed(digits)
}

function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${(Number(v) * 100).toFixed(digits)}%`
}

function fmtDate(s: string | null | undefined): string {
  if (!s) return '—'
  try {
    return new Date(s).toLocaleString()
  } catch {
    return s
  }
}

const TABS: { id: TabId; label: string; icon: any }[] = [
  { id: 'attribution', label: 'Attribution', icon: Brain },
  { id: 'bandit', label: 'Bandit', icon: Activity },
  { id: 'counterfactual', label: 'Counterfactual', icon: GitCompare },
  { id: 'leadlag', label: 'Lead-Lag', icon: Zap },
  { id: 'events', label: 'Calendar', icon: Calendar },
  { id: 'drift', label: 'Drift', icon: Activity },
  { id: 'judge', label: 'LLM Judge', icon: Brain },
  { id: 'onchain', label: 'On-chain', icon: Activity },
  { id: 'shadow', label: 'Shadow', icon: Eye },
]

export default function Smarts() {
  const profile = useLiveStore(s => s.profile)
  const [tab, setTab] = useState<TabId>('attribution')
  const [follower, setFollower] = useState('BTC-USD')
  const [onchainAsset, setOnchainAsset] = useState('BTC')
  const [onchainMetric, setOnchainMetric] = useState('hashrate')

  return (
    <PageTransition>
      <div className="p-4 md:p-6 space-y-4">
        <header className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-emerald-300 flex items-center gap-2">
              <Brain className="w-6 h-6" />
              Smarts
            </h1>
            <p className="text-sm text-gray-400">
              Outcome attribution · bandit posteriors · lead-lag · calendar ·
              decision drift · LLM judge · on-chain · shadow strategist
            </p>
          </div>
        </header>

        <nav className="flex flex-wrap gap-2 border-b border-gray-800 pb-2">
          {TABS.map(t => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`px-3 py-1.5 rounded-md text-sm flex items-center gap-1.5 transition ${
                tab === t.id
                  ? 'bg-emerald-700/30 text-emerald-200 border border-emerald-600'
                  : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800'
              }`}
            >
              <t.icon className="w-3.5 h-3.5" />
              {t.label}
            </button>
          ))}
        </nav>

        {tab === 'attribution' && <AttributionTab profile={profile} />}
        {tab === 'bandit' && <BanditTab profile={profile} />}
        {tab === 'counterfactual' && <CounterfactualTab profile={profile} />}
        {tab === 'leadlag' && (
          <LeadLagTab profile={profile} follower={follower} setFollower={setFollower} />
        )}
        {tab === 'events' && <EventsTab profile={profile} />}
        {tab === 'drift' && <DriftTab profile={profile} />}
        {tab === 'judge' && <JudgeTab profile={profile} />}
        {tab === 'onchain' && (
          <OnchainTab
            profile={profile}
            asset={onchainAsset}
            metric={onchainMetric}
            setAsset={setOnchainAsset}
            setMetric={setOnchainMetric}
          />
        )}
        {tab === 'shadow' && <ShadowTab profile={profile} />}
      </div>
    </PageTransition>
  )
}

// ─── Attribution ────────────────────────────────────────────────────────

function AttributionTab({ profile }: { profile: string }) {
  const q = useQuery({
    queryKey: ['smarts', 'attribution', profile],
    queryFn: () => fetchSmartsFeatureBrier(200),
    staleTime: 60_000,
  })
  const rows = q.data?.rows ?? []
  if (q.isLoading) return <div className="text-gray-400">Loading attribution…</div>
  if (!rows.length)
    return <EmptyState title="No attribution rows yet" description="Nightly attribution job has not produced rows for this exchange." />
  return (
    <div className="overflow-x-auto rounded-md border border-gray-800">
      <table className="min-w-full text-sm">
        <thead className="bg-gray-900/60 text-gray-400">
          <tr>
            <Th>Feature</Th>
            <Th right>Samples</Th>
            <Th right>Brier ↓</Th>
            <Th right>Avg conf</Th>
            <Th>Last seen</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r: any, i: number) => (
            <tr key={i} className="border-t border-gray-800 hover:bg-gray-900/40">
              <Td>{r.feature_name}</Td>
              <Td right>{r.samples}</Td>
              <Td right className={brierClass(r.brier_score)}>{fmtNum(r.brier_score, 4)}</Td>
              <Td right>{fmtPct(r.avg_confidence)}</Td>
              <Td>{fmtDate(r.last_seen)}</Td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function brierClass(b: number | null | undefined): string {
  if (b == null) return ''
  if (b < 0.15) return 'text-emerald-300 font-semibold'
  if (b < 0.25) return 'text-emerald-400'
  if (b < 0.35) return 'text-amber-300'
  return 'text-rose-400'
}

// ─── Bandit ─────────────────────────────────────────────────────────────

function BanditTab({ profile }: { profile: string }) {
  const q = useQuery({
    queryKey: ['smarts', 'bandit', profile],
    queryFn: () => fetchSmartsBandit(),
    staleTime: 30_000,
  })
  const rows = q.data?.rows ?? []
  if (!rows.length) return <EmptyState title="Bandit not initialised" />
  return (
    <div className="overflow-x-auto rounded-md border border-gray-800">
      <table className="min-w-full text-sm">
        <thead className="bg-gray-900/60 text-gray-400">
          <tr>
            <Th>Regime</Th>
            <Th>Strategy</Th>
            <Th right>α</Th>
            <Th right>β</Th>
            <Th right>E[w]</Th>
            <Th right>Pulls</Th>
            <Th>Last update</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r: any, i: number) => {
            const exp = r.alpha / Math.max(r.alpha + r.beta, 1e-9)
            return (
              <tr key={i} className="border-t border-gray-800">
                <Td>{r.regime}</Td>
                <Td>{r.strategy}</Td>
                <Td right>{fmtNum(r.alpha, 2)}</Td>
                <Td right>{fmtNum(r.beta, 2)}</Td>
                <Td right className="text-emerald-300">{fmtPct(exp)}</Td>
                <Td right>{r.samples}</Td>
                <Td>{fmtDate(r.last_update)}</Td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// ─── Counterfactual ─────────────────────────────────────────────────────

function CounterfactualTab({ profile }: { profile: string }) {
  const q = useQuery({
    queryKey: ['smarts', 'counterfactual', profile],
    queryFn: () => fetchSmartsCounterfactual(100),
    staleTime: 60_000,
  })
  const rows = q.data?.rows ?? []
  if (!rows.length) return <EmptyState title="No counterfactual replays yet" />
  return (
    <div className="overflow-x-auto rounded-md border border-gray-800">
      <table className="min-w-full text-sm">
        <thead className="bg-gray-900/60 text-gray-400">
          <tr>
            <Th>Replayed</Th>
            <Th>Pair</Th>
            <Th>Live → Replay</Th>
            <Th right>Live conf</Th>
            <Th right>Replay conf</Th>
            <Th right>Live PnL</Th>
            <Th right>Replay PnL</Th>
            <Th>Agreed</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r: any, i: number) => (
            <tr key={i} className="border-t border-gray-800">
              <Td>{fmtDate(r.replayed_at)}</Td>
              <Td>{r.pair}</Td>
              <Td>
                <span className="text-gray-300">{r.actual_action}</span>
                <span className="mx-1 text-gray-600">→</span>
                <span className={r.agreed ? 'text-emerald-300' : 'text-amber-300'}>
                  {r.replay_action}
                </span>
              </Td>
              <Td right>{fmtPct(r.original_confidence)}</Td>
              <Td right>{fmtPct(r.replay_confidence)}</Td>
              <Td right>{fmtPct(r.actual_pnl_pct)}</Td>
              <Td right>{fmtPct(r.replay_pnl_pct)}</Td>
              <Td>{r.agreed ? '✓' : '✗'}</Td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ─── Lead-Lag ───────────────────────────────────────────────────────────

function LeadLagTab({
  profile,
  follower,
  setFollower,
}: {
  profile: string
  follower: string
  setFollower: (s: string) => void
}) {
  const q = useQuery({
    queryKey: ['smarts', 'leadlag', profile, follower],
    queryFn: () => fetchSmartsLeadLag(follower),
    staleTime: 60_000,
    enabled: !!follower,
  })
  const rows = q.data?.rows ?? []
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <label className="text-sm text-gray-400">Follower:</label>
        <input
          value={follower}
          onChange={e => setFollower(e.target.value.toUpperCase())}
          className="bg-gray-900 border border-gray-700 rounded px-2 py-1 text-sm w-40"
          placeholder="BTC-USD"
        />
      </div>
      {!rows.length ? (
        <EmptyState title="No lead-lag edges for this follower" />
      ) : (
        <div className="overflow-x-auto rounded-md border border-gray-800">
          <table className="min-w-full text-sm">
            <thead className="bg-gray-900/60 text-gray-400">
              <tr>
                <Th>Leader</Th>
                <Th right>Lag (min)</Th>
                <Th right>β</Th>
                <Th right>t-stat</Th>
                <Th right>R²</Th>
                <Th right>n</Th>
                <Th>Computed</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r: any, i: number) => (
                <tr key={i} className="border-t border-gray-800">
                  <Td>{r.leader}</Td>
                  <Td right>{r.lag_minutes}</Td>
                  <Td right>{fmtNum(r.beta, 4)}</Td>
                  <Td right className={Math.abs(r.t_stat) >= 2 ? 'text-emerald-300' : 'text-gray-400'}>
                    {fmtNum(r.t_stat, 2)}
                  </Td>
                  <Td right>{fmtNum(r.r_squared, 3)}</Td>
                  <Td right>{r.samples}</Td>
                  <Td>{fmtDate(r.computed_at)}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ─── Events ─────────────────────────────────────────────────────────────

function EventsTab({ profile }: { profile: string }) {
  const q = useQuery({
    queryKey: ['smarts', 'events', profile],
    queryFn: () => fetchSmartsUpcomingEvents(168),
    staleTime: 5 * 60_000,
  })
  const rows = q.data?.rows ?? []
  if (!rows.length) return <EmptyState title="No upcoming events" />
  return (
    <div className="overflow-x-auto rounded-md border border-gray-800">
      <table className="min-w-full text-sm">
        <thead className="bg-gray-900/60 text-gray-400">
          <tr>
            <Th>When</Th>
            <Th>Type</Th>
            <Th>Asset</Th>
            <Th right>Severity</Th>
            <Th>Source</Th>
            <Th>Title</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r: any, i: number) => (
            <tr key={i} className="border-t border-gray-800">
              <Td>{fmtDate(r.scheduled_at)}</Td>
              <Td className="text-emerald-300">{r.event_type}</Td>
              <Td>{r.asset}</Td>
              <Td right>{r.severity}</Td>
              <Td>{r.source}</Td>
              <Td className="text-gray-300">{r.title}</Td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ─── Drift ──────────────────────────────────────────────────────────────

function DriftTab({ profile }: { profile: string }) {
  const q = useQuery({
    queryKey: ['smarts', 'drift', profile],
    queryFn: () => fetchSmartsDecisionDrift(60),
    staleTime: 60_000,
  })
  const rows = q.data?.rows ?? []
  if (!rows.length) return <EmptyState title="No drift snapshots yet" />
  return (
    <div className="overflow-x-auto rounded-md border border-gray-800">
      <table className="min-w-full text-sm">
        <thead className="bg-gray-900/60 text-gray-400">
          <tr>
            <Th>Date</Th>
            <Th>Agent</Th>
            <Th right>n</Th>
            <Th right>P10</Th>
            <Th right>P50</Th>
            <Th right>P90</Th>
            <Th right>z</Th>
            <Th>Alert</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r: any, i: number) => (
            <tr key={i} className="border-t border-gray-800">
              <Td>{r.snapshot_date}</Td>
              <Td>{r.agent}</Td>
              <Td right>{r.n_decisions}</Td>
              <Td right>{fmtPct(r.p10_conf)}</Td>
              <Td right>{fmtPct(r.p50_conf)}</Td>
              <Td right>{fmtPct(r.p90_conf)}</Td>
              <Td right className={Math.abs(r.z_score ?? 0) >= 2 ? 'text-amber-300' : ''}>
                {fmtNum(r.z_score, 2)}
              </Td>
              <Td>{r.alert ? '⚠️' : '—'}</Td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ─── Judge ──────────────────────────────────────────────────────────────

function JudgeTab({ profile }: { profile: string }) {
  const q = useQuery({
    queryKey: ['smarts', 'judge', profile],
    queryFn: () => fetchSmartsReasoningJudge(100),
    staleTime: 60_000,
  })
  const rows = q.data?.rows ?? []
  if (!rows.length) return <EmptyState title="No judge verdicts yet" />
  return (
    <div className="overflow-x-auto rounded-md border border-gray-800">
      <table className="min-w-full text-sm">
        <thead className="bg-gray-900/60 text-gray-400">
          <tr>
            <Th>Judged</Th>
            <Th>Agent</Th>
            <Th>Pair</Th>
            <Th>Verdict</Th>
            <Th right>Score</Th>
            <Th>Rationale</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r: any, i: number) => (
            <tr key={i} className="border-t border-gray-800">
              <Td>{fmtDate(r.judged_at)}</Td>
              <Td>{r.agent}</Td>
              <Td>{r.pair}</Td>
              <Td className={
                r.verdict === 'actionable' ? 'text-emerald-300' :
                r.verdict === 'confused' ? 'text-rose-400' :
                'text-amber-300'
              }>{r.verdict}</Td>
              <Td right>{fmtNum(r.score, 2)}</Td>
              <Td className="max-w-md truncate text-gray-400">{r.rationale}</Td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ─── On-chain ───────────────────────────────────────────────────────────

function OnchainTab({
  profile, asset, metric, setAsset, setMetric,
}: {
  profile: string
  asset: string
  metric: string
  setAsset: (s: string) => void
  setMetric: (s: string) => void
}) {
  const q = useQuery({
    queryKey: ['smarts', 'onchain', profile, asset, metric],
    queryFn: () => fetchSmartsOnchain(asset, metric, 100),
    staleTime: 60_000,
    enabled: !!asset && !!metric,
  })
  const rows = q.data?.rows ?? []
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <label className="text-sm text-gray-400">Asset:</label>
        <input
          value={asset}
          onChange={e => setAsset(e.target.value.toUpperCase())}
          className="bg-gray-900 border border-gray-700 rounded px-2 py-1 text-sm w-24"
        />
        <label className="text-sm text-gray-400">Metric:</label>
        <select
          value={metric}
          onChange={e => setMetric(e.target.value)}
          className="bg-gray-900 border border-gray-700 rounded px-2 py-1 text-sm"
        >
          <option value="hashrate">hashrate</option>
          <option value="dominance">dominance</option>
          <option value="stablecoin_supply">stablecoin_supply</option>
        </select>
      </div>
      {!rows.length ? (
        <EmptyState title="No on-chain rows for this asset/metric" />
      ) : (
        <div className="overflow-x-auto rounded-md border border-gray-800">
          <table className="min-w-full text-sm">
            <thead className="bg-gray-900/60 text-gray-400">
              <tr>
                <Th>Observed</Th>
                <Th>Asset</Th>
                <Th>Metric</Th>
                <Th right>Value</Th>
                <Th>Source</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r: any, i: number) => (
                <tr key={i} className="border-t border-gray-800">
                  <Td>{fmtDate(r.observed_at)}</Td>
                  <Td>{r.asset}</Td>
                  <Td>{r.metric}</Td>
                  <Td right>{fmtNum(r.value, 4)}</Td>
                  <Td>{r.source}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ─── Shadow ─────────────────────────────────────────────────────────────

function ShadowTab({ profile }: { profile: string }) {
  const q = useQuery({
    queryKey: ['smarts', 'shadow', profile],
    queryFn: () => fetchSmartsShadow(200),
    staleTime: 60_000,
  })
  const rows = q.data?.rows ?? []
  if (!rows.length) return <EmptyState title="No shadow decisions yet" description="Configure shadow_strategists in your profile YAML." />
  const diffCount = rows.filter((r: any) => r.diff_action).length
  return (
    <div className="space-y-3">
      <div className="text-sm text-gray-400">
        {rows.length} shadow decisions · {diffCount} differ from live (
        {fmtPct(diffCount / Math.max(rows.length, 1))})
      </div>
      <div className="overflow-x-auto rounded-md border border-gray-800">
        <table className="min-w-full text-sm">
          <thead className="bg-gray-900/60 text-gray-400">
            <tr>
              <Th>Decided</Th>
              <Th>Variant</Th>
              <Th>Pair</Th>
              <Th>Live → Shadow</Th>
              <Th right>Live conf</Th>
              <Th right>Shadow conf</Th>
              <Th>Diff</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r: any, i: number) => (
              <tr key={i} className="border-t border-gray-800">
                <Td>{fmtDate(r.decided_at)}</Td>
                <Td className="text-emerald-300">{r.variant}</Td>
                <Td>{r.pair}</Td>
                <Td>
                  <span className="text-gray-300">{r.live_action ?? '—'}</span>
                  <span className="mx-1 text-gray-600">→</span>
                  <span className={r.diff_action ? 'text-amber-300' : 'text-emerald-300'}>
                    {r.action}
                  </span>
                </Td>
                <Td right>{fmtPct(r.live_confidence)}</Td>
                <Td right>{fmtPct(r.confidence)}</Td>
                <Td>{r.diff_action ? '⚠️' : '✓'}</Td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ─── Helpers ────────────────────────────────────────────────────────────

function Th({ children, right }: { children: any; right?: boolean }) {
  return (
    <th className={`px-3 py-2 font-medium ${right ? 'text-right' : 'text-left'}`}>
      {children}
    </th>
  )
}

function Td({
  children, right, className,
}: { children: any; right?: boolean; className?: string }) {
  return (
    <td className={`px-3 py-1.5 ${right ? 'text-right' : ''} ${className || ''}`}>
      {children}
    </td>
  )
}
