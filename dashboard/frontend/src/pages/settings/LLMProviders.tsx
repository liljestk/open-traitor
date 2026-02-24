import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  fetchLLMProviders, updateLLMProviders, updateApiKeys, fetchOpenRouterCredits,
  type LLMProviderConfig, type OpenRouterCreditsInfo,
} from '../../api'
import {
  ChevronDown, Save, X, AlertTriangle, Check, Info,
  ArrowUp, ArrowDown, Zap, Server, Cloud,
  ToggleLeft, ToggleRight, Eye, EyeOff, Key,
  DollarSign, Sparkles, Settings2, Activity,
  Wifi, WifiOff, Clock,
} from 'lucide-react'
import { btnStyle, codeStyle, inputBase } from './settingsData'

/* ─── Helpers ──────────────────────────────────────────────────────────────── */

function statusOf(p: LLMProviderConfig): { label: string; color: string; dot: string } {
  if (!p.enabled) return { label: 'Disabled', color: '#6e7681', dot: '#484f58' }
  if (!p.api_key_set && !p.is_local) return { label: 'No API Key', color: '#f59e0b', dot: '#f59e0b' }
  if (p.live_status?.in_cooldown) return { label: 'Cooldown', color: '#f59e0b', dot: '#f59e0b' }
  if (p.live_status?.available === false) return { label: 'Unavailable', color: '#ef4444', dot: '#ef4444' }
  return { label: 'Active', color: '#22c55e', dot: '#22c55e' }
}

function tierBadge(tier: string | undefined) {
  if (!tier) return null
  const isFree = tier === 'free'
  return (
    <span style={{
      fontSize: 9, padding: '1px 7px', borderRadius: 10, fontWeight: 600,
      background: isFree ? '#22c55e12' : '#58a6ff12',
      color: isFree ? '#22c55e' : '#58a6ff',
      border: `1px solid ${isFree ? '#22c55e22' : '#58a6ff22'}`,
    }}>
      {isFree ? '✦ FREE' : '◆ PAID'}
    </span>
  )
}

function UsageBar({ current, limit, label }: { current?: number; limit?: number; label: string }) {
  if (current === undefined && limit === undefined) return null
  const pct = (limit && limit > 0) ? Math.min((current ?? 0) / limit * 100, 100) : 0
  const color = pct > 85 ? '#ef4444' : pct > 60 ? '#f59e0b' : '#22c55e'
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 90 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: '#8b949e' }}>
        <span>{label}</span>
        <span style={{ color: '#c9d1d9' }}>
          {current ?? 0}{limit ? `/${limit > 10000 ? `${(limit / 1000).toFixed(0)}k` : limit}` : ''}
        </span>
      </div>
      {limit && limit > 0 ? (
        <div style={{ height: 3, background: '#21262d', borderRadius: 2, overflow: 'hidden' }}>
          <div style={{ width: `${pct}%`, height: '100%', background: color, borderRadius: 2, transition: 'width 0.3s' }} />
        </div>
      ) : (
        <div style={{ height: 3, background: '#21262d', borderRadius: 2 }}>
          <div style={{ width: '100%', height: '100%', background: '#21262d', borderRadius: 2 }} />
        </div>
      )}
    </div>
  )
}

/* ─── Provider Card (read mode) ────────────────────────────────────────────── */

function ProviderReadCard({
  p, idx, orCredits,
}: { p: LLMProviderConfig; idx: number; orCredits?: OpenRouterCreditsInfo }) {
  const status = statusOf(p)
  const tier = p.tier ?? p.live_status?.tier
  const isOR = p.name.toLowerCase().includes('openrouter')

  return (
    <div style={{
      background: p.enabled ? '#161b22' : '#0d111788',
      border: `1px solid ${p.enabled ? '#21262d' : '#21262d55'}`,
      borderRadius: 10, padding: '12px 16px',
      opacity: p.enabled ? 1 : 0.65,
      transition: 'all 0.15s',
    }}>
      {/* Header row */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: p.enabled ? 10 : 0 }}>
        {/* Priority badge */}
        <span style={{
          width: 22, height: 22, borderRadius: '50%',
          background: p.enabled ? '#22262d' : '#161b22',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 11, fontWeight: 700, color: '#8b949e', flexShrink: 0,
        }}>{idx + 1}</span>

        {/* Cloud/local icon */}
        {p.is_local
          ? <Server size={14} style={{ color: '#8b949e', flexShrink: 0 }} />
          : <Cloud size={14} style={{ color: '#58a6ff', flexShrink: 0 }} />}

        {/* Name */}
        <span style={{ fontWeight: 600, fontSize: 13, color: '#e6edf3', flex: 1 }}>
          {p.name}
          {p.is_local && <span style={{ fontSize: 10, color: '#6e7681', fontWeight: 400, marginLeft: 5 }}>local</span>}
        </span>

        {/* Tier */}
        {tier && tierBadge(tier)}

        {/* Status */}
        <span style={{
          display: 'flex', alignItems: 'center', gap: 5,
          fontSize: 10, padding: '2px 9px', borderRadius: 10,
          background: status.color + '18', color: status.color, fontWeight: 600,
          border: `1px solid ${status.color}30`,
        }}>
          <span style={{
            width: 5, height: 5, borderRadius: '50%', background: status.dot,
            boxShadow: status.label === 'Active' ? `0 0 4px ${status.dot}` : undefined,
          }} />
          {status.label}
        </span>
      </div>

      {/* Details (only when enabled) */}
      {p.enabled && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {/* Model line */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{
              fontFamily: 'var(--font-mono, monospace)', fontSize: 11,
              color: '#79c0ff', background: '#161b2288', padding: '2px 8px',
              borderRadius: 4, border: '1px solid #21262d', maxWidth: '100%',
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}>{p.model}</span>
          </div>

          {/* Stats row */}
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'center' }}>
            {/* API Key status */}
            <span style={{
              fontSize: 11, color: p.api_key_set ? '#22c55e' : '#f59e0b',
              display: 'flex', alignItems: 'center', gap: 4,
            }}>
              <Key size={10} />
              {p.api_key_set ? 'Key set' : 'Key missing'}
            </span>

            {/* Timeout */}
            {p.timeout && (
              <span style={{ fontSize: 11, color: '#8b949e', display: 'flex', alignItems: 'center', gap: 4 }}>
                <Clock size={10} /> {p.timeout}s
              </span>
            )}

            {/* Cooldown indicator */}
            {p.live_status?.in_cooldown && (
              <span style={{ fontSize: 11, color: '#f59e0b', display: 'flex', alignItems: 'center', gap: 4 }}>
                <Clock size={10} /> Cooldown: {p.live_status.cooldown_remaining_s}s left
              </span>
            )}

            {/* OpenRouter credits */}
            {isOR && orCredits?.ok && (
              <span style={{ fontSize: 11, color: orCredits.is_free_tier ? '#22c55e' : '#e6edf3', display: 'flex', alignItems: 'center', gap: 4 }}>
                <DollarSign size={10} />
                {orCredits.is_free_tier
                  ? <><Sparkles size={9} /> Free tier</>
                  : orCredits.credits_remaining != null
                    ? `$${orCredits.credits_remaining.toFixed(4)} credits`
                    : 'Credits unknown'}
              </span>
            )}

            {/* Free model badge */}
            {p.live_status?.is_free_model && (
              <span style={{ fontSize: 11, color: '#22c55e', display: 'flex', alignItems: 'center', gap: 4 }}>
                <Sparkles size={10} /> Free model
              </span>
            )}
          </div>

          {/* Usage bars (only if live status available) */}
          {p.live_status && !p.is_local && (
            <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
              <UsageBar
                current={p.live_status.rpm_current}
                limit={p.rpm_limit}
                label="RPM"
              />
              <UsageBar
                current={p.live_status.daily_requests}
                limit={p.daily_request_limit || undefined}
                label="Req/day"
              />
              {(p.daily_token_limit ?? 0) > 0 && (
                <UsageBar
                  current={p.live_status.daily_tokens}
                  limit={p.daily_token_limit || undefined}
                  label="Tokens/day"
                />
              )}
            </div>
          )}

          {/* Rate limits summary (no live status) */}
          {!p.live_status && !p.is_local && (p.rpm_limit || p.daily_request_limit) && (
            <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', fontSize: 11, color: '#6e7681' }}>
              {p.rpm_limit ? <span>RPM: {p.rpm_limit}</span> : null}
              {(p.daily_request_limit ?? 0) > 0 ? <span>Daily req: {p.daily_request_limit}</span> : null}
              {p.cooldown_seconds ? <span>Cooldown: {p.cooldown_seconds}s</span> : null}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/* ─── Provider Card (edit mode) ────────────────────────────────────────────── */

function ProviderEditCard({
  p, idx, total,
  keyDraft, visibleKey, onToggle, onMove, onField, onKeyDraft, onKeyVisibility,
}: {
  p: LLMProviderConfig; idx: number; total: number
  keyDraft: string; visibleKey: boolean
  onToggle: () => void
  onMove: (dir: -1 | 1) => void
  onField: (field: string, value: unknown) => void
  onKeyDraft: (v: string) => void
  onKeyVisibility: (v: boolean) => void
}) {
  const [showRateLimits, setShowRateLimits] = useState(false)

  const fieldRow = (label: string, children: React.ReactNode) => (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '6px 0', borderBottom: '1px solid #0d1117' }}>
      <span style={{ fontSize: 11, color: '#8b949e', minWidth: 90, flexShrink: 0 }}>{label}</span>
      {children}
    </div>
  )

  return (
    <div style={{
      background: p.enabled ? '#161b22' : '#0d111788',
      border: `2px solid ${p.enabled ? '#30363d' : '#21262d55'}`,
      borderRadius: 10, overflow: 'hidden',
      opacity: p.enabled ? 1 : 0.6,
      transition: 'all 0.15s',
    }}>
      {/* Edit card header */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px',
        background: p.enabled ? '#1c2128' : '#0d1117',
        borderBottom: '1px solid #21262d',
      }}>
        <span style={{
          width: 22, height: 22, borderRadius: '50%', background: '#21262d',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 11, fontWeight: 700, color: '#8b949e', flexShrink: 0,
        }}>{idx + 1}</span>
        {p.is_local ? <Server size={14} style={{ color: '#8b949e' }} /> : <Cloud size={14} style={{ color: '#58a6ff' }} />}
        <span style={{ fontWeight: 600, fontSize: 13, color: '#e6edf3', flex: 1 }}>
          {p.name}
          {p.is_local && <span style={{ fontSize: 10, color: '#6e7681', marginLeft: 5 }}>local</span>}
        </span>

        {/* Enable/disable */}
        <button onClick={onToggle} style={{
          background: p.enabled ? '#22c55e18' : 'transparent',
          border: `1px solid ${p.enabled ? '#22c55e44' : '#30363d'}`,
          borderRadius: 20, cursor: 'pointer', padding: '3px 12px',
          color: p.enabled ? '#22c55e' : '#6e7681',
          display: 'flex', alignItems: 'center', gap: 5, fontSize: 11, fontWeight: 500,
          transition: 'all 0.15s',
        }}>
          {p.enabled ? <ToggleRight size={16} /> : <ToggleLeft size={16} />}
          {p.enabled ? 'Enabled' : 'Disabled'}
        </button>

        {/* Reorder */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
          <button onClick={() => onMove(-1)} disabled={idx === 0}
            style={{ background: 'none', border: 'none', cursor: idx === 0 ? 'default' : 'pointer', padding: '1px 3px', color: idx === 0 ? '#21262d' : '#8b949e' }}
            title="Higher priority"><ArrowUp size={13} /></button>
          <button onClick={() => onMove(1)} disabled={idx === total - 1}
            style={{ background: 'none', border: 'none', cursor: idx === total - 1 ? 'default' : 'pointer', padding: '1px 3px', color: idx === total - 1 ? '#21262d' : '#8b949e' }}
            title="Lower priority"><ArrowDown size={13} /></button>
        </div>
      </div>

      {/* Edit fields */}
      <div style={{ padding: '8px 14px 12px' }}>

        {/* Model */}
        {fieldRow('Model',
          <input type="text" value={p.model}
            onChange={e => onField('model', e.target.value)}
            style={{ ...inputBase, flex: 1, fontSize: 12, fontFamily: 'var(--font-mono, monospace)' }}
            placeholder="provider/model-name"
          />
        )}

        {/* API Key (cloud providers only) */}
        {!p.is_local && p.api_key_env && fieldRow('API Key',
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1 }}>
            <span style={{ fontSize: 10, color: '#484f58', fontFamily: 'var(--font-mono, monospace)', flexShrink: 0 }}>
              {p.api_key_env}
            </span>
            <div style={{ flex: 1, display: 'flex', alignItems: 'center', gap: 4 }}>
              <input
                type={visibleKey ? 'text' : 'password'}
                value={keyDraft}
                onChange={e => onKeyDraft(e.target.value)}
                placeholder={p.api_key_set ? '••••••••  (keep current)' : 'Paste API key…'}
                style={{ ...inputBase, flex: 1, fontSize: 12 }}
              />
              <button onClick={() => onKeyVisibility(!visibleKey)}
                style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#8b949e', padding: '2px 4px', flexShrink: 0 }}
                title={visibleKey ? 'Hide' : 'Show key'}>
                {visibleKey ? <EyeOff size={13} /> : <Eye size={13} />}
              </button>
            </div>
            <span style={{
              fontSize: 10, color: p.api_key_set ? '#22c55e' : '#f59e0b',
              display: 'flex', alignItems: 'center', gap: 3, flexShrink: 0,
            }}>
              {p.api_key_set ? <Check size={10} /> : <AlertTriangle size={10} />}
              {p.api_key_set ? 'Set' : 'Not set'}
            </span>
          </div>
        )}

        {/* Tier (cloud only) */}
        {!p.is_local && fieldRow('Tier',
          <select value={p.tier || ''}
            onChange={e => onField('tier', e.target.value || undefined)}
            style={{ ...inputBase, width: 100, fontSize: 12, cursor: 'pointer' }}>
            <option value="">—</option>
            <option value="free">Free</option>
            <option value="paid">Paid</option>
          </select>
        )}

        {/* Rate limits toggle */}
        {!p.is_local && (
          <>
            <button onClick={() => setShowRateLimits(v => !v)} style={{
              background: 'none', border: 'none', cursor: 'pointer', padding: '8px 0 4px',
              color: '#6e7681', fontSize: 11, display: 'flex', alignItems: 'center', gap: 4,
              width: '100%',
            }}>
              <ChevronDown size={12} style={{ transform: showRateLimits ? 'rotate(0deg)' : 'rotate(-90deg)', transition: 'transform 0.15s' }} />
              Rate limits &amp; timeouts
            </button>

            {showRateLimits && (
              <div style={{ paddingTop: 4 }}>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 16px' }}>
                  {fieldRow('Timeout (s)',
                    <input type="number" value={p.timeout ?? 60} min={5} max={600}
                      onChange={e => onField('timeout', parseInt(e.target.value, 10))}
                      style={{ ...inputBase, width: 80, fontSize: 12 }} />
                  )}
                  {fieldRow('Cooldown (s)',
                    <input type="number" value={p.cooldown_seconds ?? 60} min={5}
                      onChange={e => onField('cooldown_seconds', parseInt(e.target.value, 10))}
                      style={{ ...inputBase, width: 80, fontSize: 12 }} />
                  )}
                  {fieldRow('RPM limit',
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <input type="number" value={p.rpm_limit ?? 0} min={0}
                        onChange={e => onField('rpm_limit', parseInt(e.target.value, 10))}
                        style={{ ...inputBase, width: 80, fontSize: 12 }} />
                      <span style={{ fontSize: 10, color: '#484f58' }}>0 = unlimited</span>
                    </div>
                  )}
                  {fieldRow('Daily requests',
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <input type="number" value={p.daily_request_limit ?? 0} min={0} step={100}
                        onChange={e => onField('daily_request_limit', parseInt(e.target.value, 10))}
                        style={{ ...inputBase, width: 80, fontSize: 12 }} />
                      <span style={{ fontSize: 10, color: '#484f58' }}>0 = unlimited</span>
                    </div>
                  )}
                  {fieldRow('Daily tokens',
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <input type="number" value={p.daily_token_limit ?? 0} min={0} step={10000}
                        onChange={e => onField('daily_token_limit', parseInt(e.target.value, 10))}
                        style={{ ...inputBase, width: 100, fontSize: 12 }} />
                      <span style={{ fontSize: 10, color: '#484f58' }}>0 = unlimited</span>
                    </div>
                  )}
                </div>
              </div>
            )}
          </>
        )}

        {/* Ollama: base URL env var info */}
        {p.is_local && p.base_url_env && (
          <div style={{ fontSize: 11, color: '#6e7681', marginTop: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
            <Info size={11} />
            Base URL from <code style={codeStyle}>{p.base_url_env}</code>
            {p.model_env && <> · Model override: <code style={codeStyle}>{p.model_env}</code></>}
          </div>
        )}
      </div>
    </div>
  )
}

/* ─── Main LLMProvidersSection ─────────────────────────────────────────────── */

export function LLMProvidersSection() {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({ queryKey: ['llm-providers'], queryFn: fetchLLMProviders })
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<LLMProviderConfig[]>([])
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [keyDrafts, setKeyDrafts] = useState<Record<string, string>>({})
  const [visibleKeys, setVisibleKeys] = useState<Record<string, boolean>>({})

  const providers = data?.providers ?? []

  const { data: orCredits } = useQuery<OpenRouterCreditsInfo>({
    queryKey: ['openrouter-credits'],
    queryFn: fetchOpenRouterCredits,
    refetchInterval: 300_000,
    enabled: providers.some(p => p.enabled && p.name.toLowerCase().includes('openrouter')),
  })

  const mutation = useMutation({
    mutationFn: updateLLMProviders, onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['llm-providers'] })
      queryClient.invalidateQueries({ queryKey: ['settings'] })
    },
  })
  const keysMutation = useMutation({ mutationFn: updateApiKeys, onSuccess: () => {
    queryClient.invalidateQueries({ queryKey: ['llm-providers'] })
  }})

  const startEdit = () => {
    setDraft(providers.map(p => ({ ...p })))
    setKeyDrafts({})
    setVisibleKeys({})
    setEditing(true)
    setMsg(null)
  }
  const cancel = () => { setEditing(false); setKeyDrafts({}); setVisibleKeys({}); setMsg(null) }

  const handleSave = async () => {
    setSaving(true)
    try {
      await mutation.mutateAsync(draft)
      const keysToSave: Record<string, string> = {}
      for (const [envVar, val] of Object.entries(keyDrafts))
        if (val.trim()) keysToSave[envVar] = val.trim()
      if (Object.keys(keysToSave).length > 0)
        await keysMutation.mutateAsync(keysToSave)
      setEditing(false)
      setKeyDrafts({})
      setVisibleKeys({})
      setMsg({ ok: true, text: 'Provider chain saved & hot-reloaded' })
      setTimeout(() => setMsg(null), 4000)
    } catch (e: unknown) {
      setMsg({ ok: false, text: (e instanceof Error ? e.message : String(e)) || 'Save failed' })
    } finally { setSaving(false) }
  }

  const moveProvider = (idx: number, dir: -1 | 1) => {
    const next = [...draft]; const target = idx + dir
    if (target < 0 || target >= next.length) return
    ;[next[idx], next[target]] = [next[target], next[idx]]
    setDraft(next)
  }

  const updateField = (idx: number, field: string, value: unknown) =>
    setDraft(prev => prev.map((p, i) => i === idx ? { ...p, [field]: value } : p))

  const displayProviders = editing ? draft : providers
  const activeCount = providers.filter(p => p.enabled && (p.api_key_set || p.is_local)).length

  return (
    <div style={{ background: '#0d1117', border: '1px solid #21262d', borderRadius: 10, marginBottom: 10, overflow: 'hidden' }}>
      {/* ── Section header ── */}
      <button onClick={() => setOpen(!open)} style={{
        width: '100%', background: open ? '#161b2240' : 'none', border: 'none', cursor: 'pointer',
        display: 'flex', alignItems: 'center', gap: 10, padding: '12px 16px',
        color: '#e6edf3', transition: 'background 0.15s',
      }}>
        <span style={{ color: '#8b949e', transition: 'transform 0.2s', transform: open ? 'rotate(0deg)' : 'rotate(-90deg)' }}>
          <ChevronDown size={14} />
        </span>
        <Zap size={15} style={{ color: '#f59e0b' }} />
        <span style={{ fontWeight: 600, fontSize: 14, flexShrink: 0 }}>LLM Provider Chain</span>

        {/* Status chips when collapsed */}
        {!open && (
          <div style={{ display: 'flex', gap: 4, flex: 1, alignItems: 'center' }}>
            {providers.filter(p => p.enabled).map(p => {
              const s = statusOf(p)
              return (
                <span key={p.name} style={{
                  fontSize: 10, padding: '2px 7px', borderRadius: 8,
                  background: '#161b22', border: '1px solid #21262d',
                  display: 'flex', alignItems: 'center', gap: 3, whiteSpace: 'nowrap',
                }}>
                  <span style={{ width: 5, height: 5, borderRadius: '50%', background: s.dot, flexShrink: 0 }} />
                  <span style={{ color: '#484f58' }}>{p.name}:</span>
                  <span style={{ color: '#c9d1d9', fontWeight: 500 }}>{s.label}</span>
                </span>
              )
            })}
          </div>
        )}
        {open && <div style={{ flex: 1 }} />}

        <span style={{
          fontSize: 9, padding: '2px 7px', borderRadius: 10,
          background: '#22c55e15', color: '#22c55e', fontWeight: 600,
          display: 'flex', alignItems: 'center', gap: 3, border: '1px solid #22c55e22',
        }}><Zap size={8} /> Live reload</span>
        <span style={{
          fontSize: 9, padding: '2px 7px', borderRadius: 10,
          background: '#ef444415', color: '#ef4444', fontWeight: 600, border: '1px solid #ef444422',
        }}>Dashboard Only</span>
        {!isLoading && (
          <span style={{ fontSize: 11, color: '#8b949e', whiteSpace: 'nowrap' }}>
            {activeCount}/{providers.length} active
          </span>
        )}
        {msg && (
          <span style={{ fontSize: 11, color: msg.ok ? '#22c55e' : '#ef4444', display: 'flex', alignItems: 'center', gap: 3, whiteSpace: 'nowrap' }}>
            {msg.ok ? <Check size={12} /> : <AlertTriangle size={12} />} {msg.text}
          </span>
        )}
      </button>

      {/* ── Expanded body ── */}
      {open && (
        <div style={{ padding: '12px 16px 16px', borderTop: '1px solid #21262d' }}>
          {/* Info banner */}
          <div style={{
            fontSize: 12, color: '#8b949e', padding: '10px 14px', marginBottom: 12,
            background: '#161b22', borderRadius: 8, lineHeight: 1.6,
            display: 'flex', alignItems: 'flex-start', gap: 8, border: '1px solid #21262d',
          }}>
            <Info size={13} style={{ flexShrink: 0, marginTop: 1, color: '#58a6ff' }} />
            <span>
              Providers are tried <strong style={{ color: '#c9d1d9' }}>top-to-bottom</strong>.
              The first available provider with capacity handles each LLM call.
              When a provider hits rate limits or errors, it enters cooldown and the next one is tried.
              API keys are stored securely in <code style={codeStyle}>config/.env</code>.
            </span>
          </div>

          {/* Action bar */}
          <div style={{ display: 'flex', gap: 8, marginBottom: 12, justifyContent: 'flex-end', alignItems: 'center' }}>
            {!editing ? (
              <>
                {providers.some(p => !p.api_key_set && !p.is_local && p.enabled) && (
                  <span style={{ fontSize: 11, color: '#f59e0b', display: 'flex', alignItems: 'center', gap: 4, marginRight: 'auto' }}>
                    <AlertTriangle size={11} /> Some enabled providers are missing API keys
                  </span>
                )}
                <button onClick={startEdit} style={{ ...btnStyle('#21262d'), borderColor: '#30363d' }}>
                  <Settings2 size={12} /> Edit provider chain
                </button>
              </>
            ) : (
              <>
                <button onClick={cancel} style={btnStyle('#21262d')}><X size={12} /> Cancel</button>
                <button onClick={handleSave} disabled={saving} style={btnStyle('#238636')}>
                  <Save size={12} /> {saving ? 'Saving…' : 'Save & Hot-Reload'}
                </button>
              </>
            )}
          </div>

          {/* Provider cards */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {displayProviders.map((p, idx) =>
              editing ? (
                <ProviderEditCard
                  key={p.name}
                  p={p} idx={idx} total={displayProviders.length}
                  keyDraft={keyDrafts[p.api_key_env ?? ''] ?? ''}
                  visibleKey={visibleKeys[p.api_key_env ?? ''] ?? false}
                  onToggle={() => updateField(idx, 'enabled', !p.enabled)}
                  onMove={dir => moveProvider(idx, dir)}
                  onField={(field, value) => updateField(idx, field, value)}
                  onKeyDraft={v => setKeyDrafts(prev => ({ ...prev, [p.api_key_env!]: v }))}
                  onKeyVisibility={v => setVisibleKeys(prev => ({ ...prev, [p.api_key_env!]: v }))}
                />
              ) : (
                <ProviderReadCard key={p.name} p={p} idx={idx} orCredits={orCredits} />
              )
            )}
          </div>

          {isLoading && (
            <div style={{ padding: 16, color: '#8b949e', fontSize: 13, textAlign: 'center' }}>
              <Activity size={14} style={{ animation: 'spin 1s linear infinite', display: 'inline' }} /> Loading providers…
            </div>
          )}

          {/* Legend */}
          {!editing && (
            <div style={{
              marginTop: 12, display: 'flex', gap: 16, fontSize: 10, color: '#484f58',
              padding: '8px 12px', background: '#0d111788', borderRadius: 6, flexWrap: 'wrap',
            }}>
              {[
                { icon: <Wifi size={9} />, label: 'Active — provider is healthy and available' },
                { icon: <Clock size={9} />, label: 'Cooldown — hit rate limit, recovering' },
                { icon: <WifiOff size={9} />, label: 'Unavailable — not responding' },
              ].map(({ icon, label }, i) => (
                <span key={i} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                  {icon} {label}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
