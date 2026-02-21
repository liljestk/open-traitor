/**
 * PlanningAudit — Temporal planning workflow runs + strategic plan history.
 * Shows all 3 horizon types (daily / weekly / monthly) with replay links.
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import dayjs from 'dayjs'
import { ExternalLink, RefreshCw, ChevronDown, ChevronRight } from 'lucide-react'
import {
  fetchStrategic,
  fetchTemporalRuns,
  fetchTemporalReplay,
  triggerTemporalRerun,
  type StrategicPlan,
  type TemporalRun,
} from '../api'
import { SkeletonCards } from '../components/Skeleton'
import EmptyState from '../components/EmptyState'
import PageTransition from '../components/PageTransition'

const HORIZON_COLORS: Record<string, string> = {
  daily: 'bg-brand-900/40 text-brand-400',
  weekly: 'bg-purple-900/40 text-purple-400',
  monthly: 'bg-amber-900/40 text-amber-400',
}

const STATUS_COLORS: Record<string, string> = {
  Running: 'text-green-400',
  Completed: 'text-gray-400',
  Failed: 'text-red-400',
  Terminated: 'text-orange-400',
  TimedOut: 'text-yellow-400',
}

// ─── Plan row ─────────────────────────────────────────────────────────────

function PlanRow({ plan }: { plan: StrategicPlan }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
      <button className="w-full flex items-center justify-between px-4 py-3 text-left" onClick={() => setOpen((v) => !v)}>
        <div className="flex items-center gap-3 min-w-0">
          <span className={`text-xs px-2 py-0.5 rounded font-semibold ${HORIZON_COLORS[plan.horizon] ?? 'bg-gray-800 text-gray-400'}`}>
            {plan.horizon}
          </span>
          <span className="text-sm text-gray-300 truncate">{plan.summary_text || 'No summary'}</span>
        </div>
        <div className="flex items-center gap-3 flex-shrink-0 text-xs text-gray-500 ml-4">
          <span>{dayjs(plan.ts).format('MM-DD HH:mm')}</span>
          {plan.langfuse_url && (
            <a
              href={plan.langfuse_url}
              target="_blank"
              rel="noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="text-brand-400 hover:underline flex items-center gap-1"
            >
              <ExternalLink size={11} /> Langfuse
            </a>
          )}
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </div>
      </button>
      {open && (
        <div className="border-t border-gray-800 px-4 py-4 space-y-3">
          <div className="grid grid-cols-2 md:grid-cols-3 gap-3 text-sm">
            {['regime', 'risk_posture', 'today_focus', 'strategy', 'summary'].map((key) =>
              plan.plan_json[key] ? (
                <div key={key}>
                  <p className="text-xs text-gray-500 mb-0.5 capitalize">{key.replace('_', ' ')}</p>
                  <p className="text-gray-300 text-sm">{String(plan.plan_json[key])}</p>
                </div>
              ) : null,
            )}
          </div>
          {plan.temporal_workflow_id && (
            <p className="text-xs text-gray-600 font-mono mt-2">
              Workflow: {plan.temporal_workflow_id} / {plan.temporal_run_id}
            </p>
          )}
          <pre className="bg-gray-950 text-xs text-gray-400 p-3 rounded-lg overflow-auto max-h-48 leading-relaxed">
            {JSON.stringify(plan.plan_json, null, 2)}
          </pre>
        </div>
      )}
    </div>
  )
}

// ─── Temporal run row ─────────────────────────────────────────────────────

function TemporalRunRow({ run }: { run: TemporalRun }) {
  const [showReplay, setShowReplay] = useState(false)
  const qc = useQueryClient()

  const { data: replay, refetch: loadReplay, isFetching } = useQuery({
    queryKey: ['temporal-replay', run.workflow_id, run.run_id],
    queryFn: () => fetchTemporalReplay(run.workflow_id, run.run_id),
    enabled: false,
  })

  const rerunMutation = useMutation({
    mutationFn: () => triggerTemporalRerun(run.workflow_id, run.run_id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['temporal-runs'] })
    },
  })

  const statusLabel = run.status.replace('WORKFLOW_EXECUTION_STATUS_', '')

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3">
        <div className="flex items-center gap-3 min-w-0">
          <span className="text-xs bg-gray-800 px-2 py-0.5 rounded text-gray-400">{run.workflow_type.replace('Workflow', '')}</span>
          <span className="text-xs font-mono text-gray-500 truncate">{run.workflow_id}</span>
          <span className={`text-xs font-semibold ${STATUS_COLORS[statusLabel] ?? 'text-gray-400'}`}>{statusLabel}</span>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <span className="text-xs text-gray-500">{run.start_time ? dayjs(run.start_time).format('MM-DD HH:mm') : '—'}</span>
          <button
            className="text-xs px-2 py-1 bg-gray-800 rounded hover:bg-gray-700 flex items-center gap-1"
            onClick={() => { setShowReplay((v) => !v); if (!replay) loadReplay() }}
          >
            {showReplay ? 'Hide' : 'Replay'}
          </button>
          <button
            className="text-xs px-2 py-1 bg-brand-700 rounded hover:bg-brand-600 flex items-center gap-1 disabled:opacity-40"
            onClick={() => rerunMutation.mutate()}
            disabled={rerunMutation.isPending}
          >
            <RefreshCw size={11} /> Re-run
          </button>
        </div>
      </div>

      {showReplay && (
        <div className="border-t border-gray-800 px-4 py-3">
          {isFetching && <p className="text-xs text-gray-500">Loading events…</p>}
          {replay && (
            <div className="space-y-1.5">
              <div className="flex items-center gap-3 mb-2">
                <p className="text-xs text-gray-500">{replay.event_count} events</p>
                {replay.langfuse_url && (
                  <a href={replay.langfuse_url} target="_blank" rel="noreferrer" className="text-xs text-brand-400 hover:underline flex items-center gap-1">
                    <ExternalLink size={11} /> View in Langfuse
                  </a>
                )}
              </div>
              <div className="space-y-1 max-h-72 overflow-auto">
                {replay.events.map((ev) => (
                  <div key={ev.event_id} className="flex items-center gap-3 text-xs py-1 border-b border-gray-800/50">
                    <span className="text-gray-600 w-8 text-right flex-shrink-0">{ev.event_id}</span>
                    <span className="text-gray-400 flex-shrink-0 w-52 truncate">{ev.event_type.replace('EVENT_TYPE_', '')}</span>
                    <span className="text-gray-600 flex-shrink-0">
                      {ev.event_time ? dayjs(ev.event_time).format('HH:mm:ss.SSS') : ''}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ─── Page ─────────────────────────────────────────────────────────────────

export default function PlanningAudit() {
  const [horizon, setHorizon] = useState('')
  const [activeTab, setActiveTab] = useState<'plans' | 'temporal'>('plans')

  const { data: plansData, isLoading: plansLoading } = useQuery({
    queryKey: ['strategic', horizon],
    queryFn: () => fetchStrategic(horizon || undefined, 30),
    staleTime: 30_000,
  })

  const { data: runsData, isLoading: runsLoading, isError: runsIsError, error: runsError } = useQuery({
    queryKey: ['temporal-runs'],
    queryFn: () => fetchTemporalRuns(undefined, 50),
    staleTime: 15_000,
    enabled: activeTab === 'temporal',
  })

  return (
    <PageTransition>
    <div className="p-6 space-y-5">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-bold text-gray-100">Planning Audit</h2>
        <div className="flex items-center gap-2">
          <div className="flex bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
            {(['plans', 'temporal'] as const).map((tab) => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className={`px-4 py-1.5 text-sm capitalize ${activeTab === tab ? 'bg-gray-700 text-white' : 'text-gray-400 hover:text-gray-200'}`}
              >
                {tab === 'plans' ? 'Strategic Plans' : 'Temporal Runs'}
              </button>
            ))}
          </div>
        </div>
      </div>

      {activeTab === 'plans' && (
        <>
          <div className="flex items-center gap-2">
            {(['', 'daily', 'weekly', 'monthly'] as const).map((h) => (
              <button
                key={h}
                onClick={() => setHorizon(h)}
                className={`text-xs px-3 py-1.5 rounded-lg capitalize ${horizon === h ? 'bg-brand-600 text-white' : 'bg-gray-900 border border-gray-800 text-gray-400 hover:text-gray-200'}`}
              >
                {h || 'All'}
              </button>
            ))}
          </div>

          {plansLoading && <SkeletonCards count={4} />}
          <div className="space-y-3">
            {plansData?.plans.map((plan) => <PlanRow key={plan.id} plan={plan} />)}
            {!plansLoading && (plansData?.plans.length ?? 0) === 0 && (
              <EmptyState
                icon="planning"
                title="No plans found"
                description="Strategic plans are generated by Temporal workflows on a daily/weekly/monthly schedule."
              />
            )}
          </div>
        </>
      )}

      {activeTab === 'temporal' && (
        <>
          {runsLoading && <SkeletonCards count={4} />}
          {runsIsError && (
            <div className="bg-amber-900/20 border border-amber-800 rounded-xl px-4 py-3 text-sm text-amber-400">
              Temporal not available: {runsError?.message}
            </div>
          )}
          <div className="space-y-3">
            {runsData?.runs.map((run) => <TemporalRunRow key={`${run.workflow_id}-${run.run_id}`} run={run} />)}
            {!runsLoading && (runsData?.runs.length ?? 0) === 0 && !runsIsError && (
              <EmptyState
                icon="planning"
                title="No Temporal runs found"
                description="Temporal workflow runs will appear here once the planning worker is active."
              />
            )}
          </div>
        </>
      )}
    </div>
    </PageTransition>
  )
}