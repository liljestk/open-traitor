import { useState, useMemo, useRef, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  fetchSettings, updateSettings, fetchPresets,
  fetchLLMProviders, updateLLMProviders, updateApiKeys,
  fetchOpenRouterCredits,
  type LLMProviderConfig, type OpenRouterCreditsInfo,
} from '../api'
import {
  ChevronDown, Save, X, AlertTriangle, Check,
  Info, ArrowRight, ArrowUp, ArrowDown, Zap,
  Server, Cloud, ToggleLeft, ToggleRight,
  Eye, EyeOff, Key, DollarSign, Sparkles,
  RefreshCw, Search, Settings2,
  Minus, Plus, Activity, ShieldCheck,
  TrendingUp, Target, Timer, Gauge, Sliders,
} from 'lucide-react'
import PageTransition from '../components/PageTransition'
import {
  SECTION_CATEGORIES, SECTION_ORDER, PRESET_CONFIG,
  TIER_COLORS, TIER_LABELS,
  type CategoryKey,
  detectActivePreset, buildPresetDiff, formatFieldValue,
  btnStyle, codeStyle, inputBase,
} from './settings/settingsData'
import {
  Toast, SectionCard, RpmBudgetCard,
  TelegramSetupGuide, DensityToggle,
} from './settings/SettingsComponents'

/* ═══════════════════════════════════════════════════════════════════════════
   LLM Providers Section
   ═══════════════════════════════════════════════════════════════════════════ */

function LLMProvidersSection() {
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

  const mutation = useMutation({ mutationFn: updateLLMProviders, onSuccess: () => {
    queryClient.invalidateQueries({ queryKey: ['llm-providers'] })
    queryClient.invalidateQueries({ queryKey: ['settings'] })
  }})
  const keysMutation = useMutation({ mutationFn: updateApiKeys, onSuccess: () => {
    queryClient.invalidateQueries({ queryKey: ['llm-providers'] })
  }})

  const startEdit = () => { setDraft(providers.map(p => ({ ...p }))); setKeyDrafts({}); setVisibleKeys({}); setEditing(true); setMsg(null) }
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
      setEditing(false); setKeyDrafts({}); setVisibleKeys({})
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

  const statusBadge = (p: LLMProviderConfig) => {
    if (!p.enabled) return { label: 'Disabled', color: '#6e7681' }
    if (!p.api_key_set && !p.is_local) return { label: 'No API Key', color: '#f59e0b' }
    if (p.live_status?.in_cooldown) return { label: 'Cooldown', color: '#f59e0b' }
    if (p.live_status?.available === false) return { label: 'Unavailable', color: '#ef4444' }
    return { label: 'Active', color: '#22c55e' }
  }

  return (
    <div style={{ background: '#0d1117', border: '1px solid #21262d', borderRadius: 10, marginBottom: 10, overflow: 'hidden' }}>
      <button onClick={() => setOpen(!open)} style={{
        width: '100%', background: open ? '#161b2240' : 'none', border: 'none', cursor: 'pointer',
        display: 'flex', alignItems: 'center', gap: 10, padding: '12px 16px', color: '#e6edf3', transition: 'background 0.15s',
      }}>
        <span style={{ color: '#8b949e', transition: 'transform 0.2s', transform: open ? 'rotate(0deg)' : 'rotate(-90deg)' }}>
          <ChevronDown size={14} />
        </span>
        <Zap size={15} style={{ color: '#f59e0b' }} />
        <span style={{ fontWeight: 600, fontSize: 14, flexShrink: 0 }}>LLM Provider Chain</span>
        {!open && (
          <div style={{ display: 'flex', gap: 4, flex: 1, alignItems: 'center' }}>
            {providers.filter(p => p.enabled).map(p => {
              const badge = statusBadge(p)
              return (
                <span key={p.name} style={{
                  fontSize: 10, padding: '2px 7px', borderRadius: 8,
                  background: '#161b22', border: '1px solid #21262d',
                  display: 'flex', alignItems: 'center', gap: 3, whiteSpace: 'nowrap',
                }}>
                  <span style={{ width: 5, height: 5, borderRadius: '50%', background: badge.color, flexShrink: 0 }} />
                  <span style={{ color: '#484f58' }}>{p.name}:</span>
                  <span style={{ color: '#c9d1d9', fontWeight: 500 }}>{badge.label}</span>
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
          <span style={{ fontSize: 11, color: '#8b949e' }}>
            {providers.filter(p => p.enabled && (p.api_key_set || p.is_local)).length}/{providers.length} active
          </span>
        )}
        {msg && (
          <span style={{ fontSize: 11, color: msg.ok ? '#22c55e' : '#ef4444', display: 'flex', alignItems: 'center', gap: 3 }}>
            {msg.ok ? <Check size={12} /> : <AlertTriangle size={12} />} {msg.text}
          </span>
        )}
      </button>

      {open && (
        <div style={{ padding: '0 16px 16px', borderTop: '1px solid #21262d' }}>
          <div style={{ fontSize: 12, color: '#8b949e', padding: '12px 0 8px', lineHeight: 1.5, display: 'flex', alignItems: 'flex-start', gap: 8 }}>
            <Info size={14} style={{ flexShrink: 0, marginTop: 1 }} />
            <span>
              Providers are tried <strong style={{ color: '#c9d1d9' }}>top-to-bottom</strong>. The first available provider handles each LLM call.
              If a provider hits rate limits or errors, it enters cooldown and the next one is tried.
              Drag to reorder priority. API keys are stored securely in <code style={codeStyle}>config/.env</code>.
            </span>
          </div>

          <div style={{ display: 'flex', gap: 8, padding: '4px 0 10px', justifyContent: 'flex-end' }}>
            {!editing ? (
              <button onClick={startEdit} style={{ ...btnStyle('#21262d'), borderColor: '#30363d' }}>
                <Settings2 size={12} /> Edit providers
              </button>
            ) : (
              <>
                <button onClick={cancel} style={btnStyle('#21262d')}><X size={12} /> Cancel</button>
                <button onClick={handleSave} disabled={saving} style={btnStyle('#238636')}>
                  <Save size={12} /> {saving ? 'Saving…' : 'Save & Hot-Reload'}
                </button>
              </>
            )}
          </div>

          {displayProviders.map((p, idx) => {
            const badge = statusBadge(p)
            const provIcon = p.is_local
              ? <Server size={16} style={{ color: '#8b949e' }} />
              : <Cloud size={16} style={{ color: '#58a6ff' }} />

            return (
              <div key={p.name} style={{
                background: '#161b22', border: `1px solid ${p.enabled ? '#21262d' : '#21262d80'}`,
                borderRadius: 10, padding: '12px 16px', marginBottom: 8,
                opacity: p.enabled ? 1 : 0.5, transition: 'all 0.15s',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <span style={{
                    width: 24, height: 24, borderRadius: '50%', background: '#30363d',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    fontSize: 11, fontWeight: 700, color: '#e6edf3',
                  }}>{idx + 1}</span>
                  {provIcon}
                  <span style={{ fontWeight: 600, fontSize: 14, color: '#e6edf3', flex: 1, display: 'flex', alignItems: 'center', gap: 6 }}>
                    {p.name}
                    {p.is_local && <span style={{ fontSize: 10, color: '#6e7681', fontWeight: 400 }}>local</span>}
                    {(p.tier || p.live_status?.tier) && (
                      <span style={{
                        fontSize: 9, padding: '1px 7px', borderRadius: 10, fontWeight: 600,
                        background: (p.tier || p.live_status?.tier) === 'free' ? '#22c55e12' : '#58a6ff12',
                        color: (p.tier || p.live_status?.tier) === 'free' ? '#22c55e' : '#58a6ff',
                        border: `1px solid ${(p.tier || p.live_status?.tier) === 'free' ? '#22c55e22' : '#58a6ff22'}`,
                      }}>{(p.tier || p.live_status?.tier)?.toUpperCase()}</span>
                    )}
                  </span>
                  <span style={{
                    fontSize: 10, padding: '3px 10px', borderRadius: 12,
                    background: badge.color + '18', color: badge.color, fontWeight: 600,
                    border: `1px solid ${badge.color}22`,
                  }}>{badge.label}</span>

                  {editing && (
                    <button onClick={() => updateField(idx, 'enabled', !p.enabled)} style={{
                      background: p.enabled ? '#22c55e18' : 'transparent',
                      border: `1px solid ${p.enabled ? '#22c55e44' : '#30363d'}`,
                      borderRadius: 20, cursor: 'pointer', padding: '3px 10px',
                      color: p.enabled ? '#22c55e' : '#6e7681',
                      display: 'flex', alignItems: 'center', gap: 4, fontSize: 11, fontWeight: 500,
                    }}>
                      {p.enabled ? <ToggleRight size={16} /> : <ToggleLeft size={16} />}
                      {p.enabled ? 'On' : 'Off'}
                    </button>
                  )}

                  {editing && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                      <button onClick={() => moveProvider(idx, -1)} disabled={idx === 0}
                        style={{ background: 'none', border: 'none', cursor: idx === 0 ? 'default' : 'pointer', padding: 0, color: idx === 0 ? '#21262d' : '#8b949e' }}
                        title="Move up (higher priority)"><ArrowUp size={14} /></button>
                      <button onClick={() => moveProvider(idx, 1)} disabled={idx === displayProviders.length - 1}
                        style={{ background: 'none', border: 'none', cursor: idx === displayProviders.length - 1 ? 'default' : 'pointer', padding: 0, color: idx === displayProviders.length - 1 ? '#21262d' : '#8b949e' }}
                        title="Move down (lower priority)"><ArrowDown size={14} /></button>
                    </div>
                  )}
                </div>

                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(170px, 1fr))', gap: '6px 20px', marginTop: 10, fontSize: 12 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', borderBottom: '1px solid #21262d' }}>
                    <span style={{ color: '#8b949e' }}>Model</span>
                    {editing ? (
                      <input type="text" value={p.model}
                        onChange={e => updateField(idx, 'model', e.target.value)}
                        style={{ ...inputBase, width: 130, textAlign: 'right', padding: '2px 8px', fontSize: 12 }}
                      />
                    ) : (
                      <span style={{ color: '#e6edf3', fontFamily: 'var(--font-mono, monospace)', fontSize: 11 }}>{p.model}</span>
                    )}
                  </div>

                  {!p.is_local && p.api_key_env && (
                    <div style={{
                      display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                      padding: '4px 0', borderBottom: '1px solid #21262d',
                      gridColumn: editing ? '1 / -1' : undefined,
                    }}>
                      <span style={{ color: '#8b949e', display: 'flex', alignItems: 'center', gap: 4 }}>
                        <Key size={11} /> API Key
                      </span>
                      {editing ? (
                        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                          <span style={{ fontSize: 10, color: '#484f58', fontFamily: 'var(--font-mono, monospace)' }}>{p.api_key_env}</span>
                          <input
                            type={visibleKeys[p.api_key_env!] ? 'text' : 'password'}
                            value={keyDrafts[p.api_key_env!] ?? ''}
                            onChange={e => setKeyDrafts(prev => ({ ...prev, [p.api_key_env!]: e.target.value }))}
                            placeholder={p.api_key_set ? '••••••••  (unchanged)' : 'Paste API key'}
                            style={{ ...inputBase, width: 220, padding: '3px 10px', fontSize: 12 }}
                          />
                          <button onClick={() => setVisibleKeys(prev => ({ ...prev, [p.api_key_env!]: !prev[p.api_key_env!] }))}
                            style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 2, color: '#8b949e' }}
                            title={visibleKeys[p.api_key_env!] ? 'Hide' : 'Show'}>
                            {visibleKeys[p.api_key_env!] ? <EyeOff size={13} /> : <Eye size={13} />}
                          </button>
                        </div>
                      ) : (
                        <span style={{ color: p.api_key_set ? '#22c55e' : '#f59e0b', display: 'flex', alignItems: 'center', gap: 4, fontSize: 11 }}>
                          {p.api_key_set ? <><Check size={11} /> Configured</> : <><AlertTriangle size={11} /> Not set</>}
                        </span>
                      )}
                    </div>
                  )}

                  {editing && !p.is_local && (
                    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', borderBottom: '1px solid #21262d' }}>
                      <span style={{ color: '#8b949e' }}>Tier</span>
                      <select
                        value={p.tier || ''}
                        onChange={e => updateField(idx, 'tier', e.target.value || undefined)}
                        style={{ ...inputBase, width: 90, textAlign: 'right', padding: '2px 6px', fontSize: 12 }}
                      >
                        <option value="">—</option>
                        <option value="free">Free</option>
                        <option value="paid">Paid</option>
                      </select>
                    </div>
                  )}

                  {!editing && p.name.toLowerCase().includes('openrouter') && p.enabled && orCredits?.ok && (
                    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', borderBottom: '1px solid #21262d' }}>
                      <span style={{ color: '#8b949e', display: 'flex', alignItems: 'center', gap: 4 }}>
                        <DollarSign size={11} /> Credits
                      </span>
                      <span style={{ color: orCredits.is_free_tier ? '#22c55e' : '#e6edf3', fontSize: 11, display: 'flex', alignItems: 'center', gap: 4 }}>
                        {orCredits.is_free_tier
                          ? <><Sparkles size={10} /> Free tier</>
                          : orCredits.credits_remaining != null
                            ? `$${orCredits.credits_remaining.toFixed(4)}`
                            : 'Unknown'}
                      </span>
                    </div>
                  )}

                  {!editing && p.live_status?.is_free_model && (
                    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', borderBottom: '1px solid #21262d' }}>
                      <span style={{ color: '#8b949e' }}>Model Type</span>
                      <span style={{ color: '#22c55e', fontSize: 11, display: 'flex', alignItems: 'center', gap: 4 }}>
                        <Sparkles size={10} /> Free model
                      </span>
                    </div>
                  )}

                  <div style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', borderBottom: '1px solid #21262d' }}>
                    <span style={{ color: '#8b949e' }}>Timeout</span>
                    {editing ? (
                      <input type="number" value={p.timeout ?? 60} min={5} max={600}
                        onChange={e => updateField(idx, 'timeout', parseInt(e.target.value, 10))}
                        style={{ ...inputBase, width: 60, textAlign: 'right', padding: '2px 6px', fontSize: 12 }}
                      />
                    ) : <span style={{ color: '#e6edf3' }}>{p.timeout ?? 60}s</span>}
                  </div>

                  {!p.is_local && (<>
                    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', borderBottom: '1px solid #21262d' }}>
                      <span style={{ color: '#8b949e' }}>RPM</span>
                      {editing ? (
                        <input type="number" value={p.rpm_limit ?? 0} min={0}
                          onChange={e => updateField(idx, 'rpm_limit', parseInt(e.target.value, 10))}
                          style={{ ...inputBase, width: 60, textAlign: 'right', padding: '2px 6px', fontSize: 12 }}
                        />
                      ) : (
                        <span style={{ color: '#e6edf3' }}>
                          {p.live_status?.rpm_current !== undefined
                            ? <>{p.live_status.rpm_current}<span style={{ color: '#484f58' }}>/{p.rpm_limit ?? 0}</span></>
                            : (p.rpm_limit ?? 0)}
                        </span>
                      )}
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', borderBottom: '1px solid #21262d' }}>
                      <span style={{ color: '#8b949e' }}>Daily Tokens</span>
                      {editing ? (
                        <input type="number" value={p.daily_token_limit ?? 0} min={0} step={10000}
                          onChange={e => updateField(idx, 'daily_token_limit', parseInt(e.target.value, 10))}
                          style={{ ...inputBase, width: 90, textAlign: 'right', padding: '2px 6px', fontSize: 12 }}
                        />
                      ) : (
                        <span style={{ color: '#e6edf3' }}>
                          {p.live_status?.daily_tokens !== undefined
                            ? <>{(p.live_status.daily_tokens / 1000).toFixed(0)}k<span style={{ color: '#484f58' }}> / {p.daily_token_limit ? `${(p.daily_token_limit / 1000).toFixed(0)}k` : '∞'}</span></>
                            : p.daily_token_limit ? `${(p.daily_token_limit / 1000).toFixed(0)}k` : '∞'}
                        </span>
                      )}
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', borderBottom: '1px solid #21262d' }}>
                      <span style={{ color: '#8b949e' }}>Daily Requests</span>
                      {editing ? (
                        <input type="number" value={p.daily_request_limit ?? 0} min={0} step={100}
                          onChange={e => updateField(idx, 'daily_request_limit', parseInt(e.target.value, 10))}
                          style={{ ...inputBase, width: 90, textAlign: 'right', padding: '2px 6px', fontSize: 12 }}
                        />
                      ) : (
                        <span style={{ color: '#e6edf3' }}>
                          {p.live_status?.daily_requests !== undefined
                            ? <>{p.live_status.daily_requests}<span style={{ color: '#484f58' }}> / {p.daily_request_limit ? p.daily_request_limit : '∞'}</span></>
                            : p.daily_request_limit ? p.daily_request_limit : '∞'}
                        </span>
                      )}
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', borderBottom: '1px solid #21262d' }}>
                      <span style={{ color: '#8b949e' }}>Cooldown</span>
                      {editing ? (
                        <input type="number" value={p.cooldown_seconds ?? 60} min={5}
                          onChange={e => updateField(idx, 'cooldown_seconds', parseInt(e.target.value, 10))}
                          style={{ ...inputBase, width: 60, textAlign: 'right', padding: '2px 6px', fontSize: 12 }}
                        />
                      ) : (
                        <span style={{ color: '#e6edf3' }}>
                          {p.live_status?.in_cooldown
                            ? <span style={{ color: '#f59e0b' }}>{p.live_status.cooldown_remaining_s}s left</span>
                            : `${p.cooldown_seconds ?? 60}s`}
                        </span>
                      )}
                    </div>
                  </>)}
                </div>
              </div>
            )
          })}

          {isLoading && <div style={{ padding: 16, color: '#8b949e', fontSize: 13, textAlign: 'center' }}>Loading providers…</div>}
        </div>
      )}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════════════════
   Quick Settings — always-visible panel for the 8 most critical settings
   ═══════════════════════════════════════════════════════════════════════════ */

interface QuickDraft {
  mode: string
  interval: number
  min_confidence: number
  max_active_pairs: number
  stop_loss_pct: number
  take_profit_pct: number
  max_single_trade: number
  max_daily_loss: number
}

function initQuickDraft(
  trading: Record<string, unknown>,
  risk: Record<string, unknown>,
  absolute: Record<string, unknown>,
): QuickDraft {
  return {
    mode: String(trading.mode ?? 'paper'),
    interval: Number(trading.interval ?? 120),
    min_confidence: Number(trading.min_confidence ?? 0.55),
    max_active_pairs: Number(trading.max_active_pairs ?? 5),
    stop_loss_pct: Number(risk.stop_loss_pct ?? 0.04),
    take_profit_pct: Number(risk.take_profit_pct ?? 0.06),
    max_single_trade: Number(absolute.max_single_trade ?? 500),
    max_daily_loss: Number(absolute.max_daily_loss ?? 500),
  }
}

function Stepper({
  value, onChange, min, max, step = 1, format,
}: { value: number; onChange: (v: number) => void; min: number; max: number; step?: number; format?: (v: number) => string }) {
  const stepBtn: React.CSSProperties = {
    width: 26, height: 26, borderRadius: 6, border: '1px solid #30363d',
    background: '#161b22', color: '#8b949e', cursor: 'pointer',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    fontSize: 12, fontWeight: 700, transition: 'all 0.1s', flexShrink: 0,
  }
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <button onClick={() => onChange(Math.max(min, parseFloat((value - step).toFixed(4))))}
        disabled={value <= min} style={{ ...stepBtn, opacity: value <= min ? 0.3 : 1 }}>
        <Minus size={11} />
      </button>
      <span style={{ fontSize: 14, fontWeight: 600, color: '#e6edf3', minWidth: 44, textAlign: 'center' }}>
        {format ? format(value) : value}
      </span>
      <button onClick={() => onChange(Math.min(max, parseFloat((value + step).toFixed(4))))}
        disabled={value >= max} style={{ ...stepBtn, opacity: value >= max ? 0.3 : 1 }}>
        <Plus size={11} />
      </button>
    </div>
  )
}

function SegmentedControl({
  value, options, onChange,
}: { value: string | number; options: Array<{ label: string; value: string | number }>; onChange: (v: string | number) => void }) {
  return (
    <div style={{ display: 'flex', background: '#0d1117', borderRadius: 8, padding: 2, border: '1px solid #21262d', gap: 2 }}>
      {options.map(opt => (
        <button key={String(opt.value)} onClick={() => onChange(opt.value)} style={{
          flex: 1, padding: '5px 8px', borderRadius: 6, border: 'none',
          background: value === opt.value ? '#21262d' : 'transparent',
          color: value === opt.value ? '#e6edf3' : '#6e7681',
          cursor: 'pointer', fontSize: 11, fontWeight: value === opt.value ? 600 : 400,
          transition: 'all 0.12s', whiteSpace: 'nowrap',
        }}>
          {opt.label}
        </button>
      ))}
    </div>
  )
}

function QuickSettings({
  settings,
  liveData,
  onSave,
}: {
  settings: Record<string, unknown>
  liveData: unknown
  onSave: (section: string, updates: Record<string, unknown>) => Promise<void>
}) {
  const trading = (settings.trading ?? {}) as Record<string, unknown>
  const risk = (settings.risk ?? {}) as Record<string, unknown>
  const absolute = (settings.absolute_rules ?? {}) as Record<string, unknown>

  const [draft, setDraft] = useState<QuickDraft>(() => initQuickDraft(trading, risk, absolute))
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  // Reset draft whenever live settings change (e.g. preset applied)
  useEffect(() => {
    setDraft(initQuickDraft(
      (settings.trading ?? {}) as Record<string, unknown>,
      (settings.risk ?? {}) as Record<string, unknown>,
      (settings.absolute_rules ?? {}) as Record<string, unknown>,
    ))
  }, [liveData]) // eslint-disable-line react-hooks/exhaustive-deps

  const set = <K extends keyof QuickDraft>(k: K, v: QuickDraft[K]) =>
    setDraft(prev => ({ ...prev, [k]: v }))

  const changedCount = useMemo(() => {
    let n = 0
    if (draft.mode !== String(trading.mode ?? 'paper')) n++
    if (draft.interval !== Number(trading.interval ?? 120)) n++
    if (draft.min_confidence !== Number(trading.min_confidence ?? 0.55)) n++
    if (draft.max_active_pairs !== Number(trading.max_active_pairs ?? 5)) n++
    if (draft.stop_loss_pct !== Number(risk.stop_loss_pct ?? 0.04)) n++
    if (draft.take_profit_pct !== Number(risk.take_profit_pct ?? 0.06)) n++
    if (draft.max_single_trade !== Number(absolute.max_single_trade ?? 500)) n++
    if (draft.max_daily_loss !== Number(absolute.max_daily_loss ?? 500)) n++
    return n
  }, [draft, trading, risk, absolute])

  const handleApply = async () => {
    setSaving(true)
    setMsg(null)
    try {
      const tradingChanges: Record<string, unknown> = {}
      if (draft.mode !== String(trading.mode ?? 'paper')) tradingChanges.mode = draft.mode
      if (draft.interval !== Number(trading.interval ?? 120)) tradingChanges.interval = draft.interval
      if (draft.min_confidence !== Number(trading.min_confidence ?? 0.55)) tradingChanges.min_confidence = draft.min_confidence
      if (draft.max_active_pairs !== Number(trading.max_active_pairs ?? 5)) tradingChanges.max_active_pairs = draft.max_active_pairs

      const riskChanges: Record<string, unknown> = {}
      if (draft.stop_loss_pct !== Number(risk.stop_loss_pct ?? 0.04)) riskChanges.stop_loss_pct = draft.stop_loss_pct
      if (draft.take_profit_pct !== Number(risk.take_profit_pct ?? 0.06)) riskChanges.take_profit_pct = draft.take_profit_pct

      const absoluteChanges: Record<string, unknown> = {}
      if (draft.max_single_trade !== Number(absolute.max_single_trade ?? 500)) absoluteChanges.max_single_trade = draft.max_single_trade
      if (draft.max_daily_loss !== Number(absolute.max_daily_loss ?? 500)) absoluteChanges.max_daily_loss = draft.max_daily_loss

      await Promise.all([
        Object.keys(tradingChanges).length ? onSave('trading', tradingChanges) : Promise.resolve(),
        Object.keys(riskChanges).length ? onSave('risk', riskChanges) : Promise.resolve(),
        Object.keys(absoluteChanges).length ? onSave('absolute_rules', absoluteChanges) : Promise.resolve(),
      ])
      setMsg({ ok: true, text: `${changedCount} setting${changedCount !== 1 ? 's' : ''} applied live` })
      setTimeout(() => setMsg(null), 3000)
    } catch (e: unknown) {
      setMsg({ ok: false, text: (e instanceof Error ? e.message : String(e)) || 'Apply failed' })
    } finally {
      setSaving(false)
    }
  }

  const handleReset = () => {
    setDraft(initQuickDraft(trading, risk, absolute))
    setMsg(null)
  }

  const isLive = draft.mode === 'live'
  const pctFmt = (v: number) => `${(v * 100).toFixed(1)}%`
  const labelStyle: React.CSSProperties = { fontSize: 11, color: '#6e7681', fontWeight: 500, marginBottom: 6, display: 'flex', alignItems: 'center', gap: 4 }
  const cellStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column' }
  const changedDot = (changed: boolean) => changed ? (
    <span style={{ width: 5, height: 5, borderRadius: '50%', background: '#f59e0b', display: 'inline-block', marginLeft: 2 }} title="Changed" />
  ) : null

  return (
    <div style={{
      background: 'linear-gradient(135deg, #0d111788, #0d1117)',
      border: '1px solid #21262d', borderRadius: 12, marginBottom: 16, overflow: 'hidden',
    }}>
      {/* Header */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8, padding: '12px 20px',
        borderBottom: '1px solid #21262d',
        background: 'linear-gradient(90deg, #58a6ff08, transparent)',
      }}>
        <Sliders size={15} style={{ color: '#58a6ff' }} />
        <span style={{ fontWeight: 700, fontSize: 14, color: '#e6edf3' }}>Quick Settings</span>
        <span style={{ fontSize: 11, color: '#6e7681', marginLeft: 4 }}>
          Most-used settings — change and apply without opening sections
        </span>
        <div style={{ flex: 1 }} />
        {msg && (
          <span style={{ fontSize: 11, color: msg.ok ? '#22c55e' : '#ef4444', display: 'flex', alignItems: 'center', gap: 4 }}>
            {msg.ok ? <Check size={12} /> : <AlertTriangle size={12} />} {msg.text}
          </span>
        )}
      </div>

      {/* Controls grid */}
      <div style={{ padding: '16px 20px' }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '16px 24px', marginBottom: 16 }}>

          {/* Trading Mode */}
          <div style={cellStyle}>
            <div style={labelStyle}>
              <TrendingUp size={11} /> Trading Mode
              {changedDot(draft.mode !== String(trading.mode ?? 'paper'))}
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <SegmentedControl
                value={draft.mode}
                options={[{ label: '📝 Paper', value: 'paper' }, { label: '⚡ Live', value: 'live' }]}
                onChange={v => set('mode', String(v))}
              />
              {isLive && (
                <span style={{ fontSize: 10, color: '#f59e0b', display: 'flex', alignItems: 'center', gap: 3 }}>
                  <AlertTriangle size={9} /> Real money at risk
                </span>
              )}
            </div>
          </div>

          {/* Cycle Interval */}
          <div style={cellStyle}>
            <div style={labelStyle}>
              <Timer size={11} /> Cycle Interval
              {changedDot(draft.interval !== Number(trading.interval ?? 120))}
            </div>
            <SegmentedControl
              value={draft.interval}
              options={[
                { label: '1m', value: 60 },
                { label: '2m', value: 120 },
                { label: '5m', value: 300 },
                { label: '10m', value: 600 },
              ]}
              onChange={v => set('interval', Number(v))}
            />
          </div>

          {/* Min Confidence */}
          <div style={cellStyle}>
            <div style={labelStyle}>
              <Activity size={11} /> Min Confidence
              {changedDot(draft.min_confidence !== Number(trading.min_confidence ?? 0.55))}
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <input
                  type="range" min={0.3} max={0.95} step={0.05}
                  value={draft.min_confidence}
                  onChange={e => set('min_confidence', parseFloat(e.target.value))}
                  style={{ flex: 1, accentColor: '#22c55e', cursor: 'pointer' }}
                />
                <span style={{
                  fontSize: 13, fontWeight: 700, color: '#e6edf3',
                  minWidth: 38, textAlign: 'right',
                }}>
                  {pctFmt(draft.min_confidence)}
                </span>
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9, color: '#484f58' }}>
                <span>30% permissive</span>
                <span>95% strict</span>
              </div>
            </div>
          </div>

          {/* Max Active Pairs */}
          <div style={cellStyle}>
            <div style={labelStyle}>
              <Gauge size={11} /> Max Active Pairs
              {changedDot(draft.max_active_pairs !== Number(trading.max_active_pairs ?? 5))}
            </div>
            <Stepper value={draft.max_active_pairs} min={1} max={30} onChange={v => set('max_active_pairs', v)} />
          </div>

          {/* Stop Loss */}
          <div style={cellStyle}>
            <div style={labelStyle}>
              <span style={{ fontSize: 10 }}>🛑</span> Stop Loss
              {changedDot(draft.stop_loss_pct !== Number(risk.stop_loss_pct ?? 0.04))}
            </div>
            <Stepper
              value={draft.stop_loss_pct}
              min={0.005}
              max={0.25}
              step={0.005}
              format={pctFmt}
              onChange={v => set('stop_loss_pct', v)}
            />
          </div>

          {/* Take Profit */}
          <div style={cellStyle}>
            <div style={labelStyle}>
              <Target size={11} /> Take Profit
              {changedDot(draft.take_profit_pct !== Number(risk.take_profit_pct ?? 0.06))}
            </div>
            <Stepper
              value={draft.take_profit_pct}
              min={0.005}
              max={0.5}
              step={0.005}
              format={pctFmt}
              onChange={v => set('take_profit_pct', v)}
            />
          </div>

          {/* Max Single Trade */}
          <div style={cellStyle}>
            <div style={labelStyle}>
              <DollarSign size={11} /> Max Single Trade
              {changedDot(draft.max_single_trade !== Number(absolute.max_single_trade ?? 500))}
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 0 }}>
              <span style={{
                padding: '5px 8px', background: '#161b22', border: '1px solid #30363d',
                borderRight: 'none', borderRadius: '6px 0 0 6px',
                fontSize: 12, color: '#6e7681', flexShrink: 0,
              }}>€</span>
              <input
                type="number" value={draft.max_single_trade} min={1} step={10}
                onChange={e => set('max_single_trade', Number(e.target.value))}
                style={{ ...inputBase, borderRadius: '0 6px 6px 0', width: '100%', fontSize: 13, fontWeight: 600 }}
              />
            </div>
          </div>

          {/* Max Daily Loss */}
          <div style={cellStyle}>
            <div style={labelStyle}>
              <span style={{ fontSize: 10 }}>📉</span> Max Daily Loss
              {changedDot(draft.max_daily_loss !== Number(absolute.max_daily_loss ?? 500))}
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 0 }}>
              <span style={{
                padding: '5px 8px', background: '#161b22', border: '1px solid #30363d',
                borderRight: 'none', borderRadius: '6px 0 0 6px',
                fontSize: 12, color: '#6e7681', flexShrink: 0,
              }}>€</span>
              <input
                type="number" value={draft.max_daily_loss} min={1} step={10}
                onChange={e => set('max_daily_loss', Number(e.target.value))}
                style={{ ...inputBase, borderRadius: '0 6px 6px 0', width: '100%', fontSize: 13, fontWeight: 600 }}
              />
            </div>
          </div>
        </div>

        {/* Footer action bar */}
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 8,
          paddingTop: 12, borderTop: '1px solid #21262d',
        }}>
          {changedCount > 0 && (
            <span style={{ fontSize: 11, color: '#f59e0b', display: 'flex', alignItems: 'center', gap: 4, marginRight: 'auto' }}>
              <AlertTriangle size={11} /> {changedCount} unsaved change{changedCount !== 1 ? 's' : ''}
            </span>
          )}
          <button onClick={handleReset} disabled={changedCount === 0} style={{
            ...btnStyle('#21262d'), borderColor: '#30363d',
            opacity: changedCount === 0 ? 0.4 : 1,
          }}>
            <RefreshCw size={12} /> Reset
          </button>
          <button
            onClick={handleApply}
            disabled={saving || changedCount === 0}
            style={{
              ...btnStyle(changedCount > 0 ? '#238636' : '#21262d'),
              opacity: changedCount === 0 ? 0.4 : 1,
              padding: '7px 18px', fontSize: 13, fontWeight: 600,
            }}
          >
            <Check size={13} />
            {saving ? 'Applying…' : changedCount > 0 ? `Apply ${changedCount} change${changedCount !== 1 ? 's' : ''}` : 'No changes'}
          </button>
        </div>
      </div>
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════════════════
   Config Health Panel — contextual warnings about current configuration
   ═══════════════════════════════════════════════════════════════════════════ */

interface HealthIssue {
  type: 'error' | 'warning' | 'info'
  icon: string
  message: string
  detail?: string
}

function ConfigHealthPanel({ settings }: { settings: Record<string, unknown> }) {
  const trading = (settings.trading ?? {}) as Record<string, unknown>
  const risk = (settings.risk ?? {}) as Record<string, unknown>
  const absolute = (settings.absolute_rules ?? {}) as Record<string, unknown>

  const issues = useMemo((): HealthIssue[] => {
    const list: HealthIssue[] = []

    const sl = Number(risk.stop_loss_pct ?? 0)
    const tp = Number(risk.take_profit_pct ?? 0)
    const conf = Number(trading.min_confidence ?? 0)
    const drawdown = Number(risk.max_drawdown_pct ?? 0)
    const activePairs = Number(trading.max_active_pairs ?? 0)
    const mode = String(trading.mode ?? 'paper')
    const dailyLoss = Number(absolute.max_daily_loss ?? 0)
    const dailySpend = Number(absolute.max_daily_spend ?? 0)

    if (sl > 0 && tp > 0 && sl >= tp) {
      list.push({
        type: 'error',
        icon: '🚨',
        message: `Stop Loss (${(sl * 100).toFixed(1)}%) ≥ Take Profit (${(tp * 100).toFixed(1)}%) — inverted risk/reward ratio`,
        detail: 'Every profitable trade could be cut short while losses run. Swap these values.',
      })
    }

    if (conf < 0.45) {
      list.push({
        type: 'warning',
        icon: '⚠️',
        message: `Very low confidence threshold (${(conf * 100).toFixed(0)}%) — expect high trade frequency with weaker signals`,
        detail: 'Consider raising to at least 0.50 to filter out noise.',
      })
    } else if (conf > 0.85) {
      list.push({
        type: 'info',
        icon: 'ℹ️',
        message: `High confidence threshold (${(conf * 100).toFixed(0)}%) — bot may trade infrequently`,
        detail: 'This is conservative and safe, but may miss opportunities in volatile markets.',
      })
    }

    if (drawdown > 0.25) {
      list.push({
        type: 'warning',
        icon: '⚠️',
        message: `Drawdown tolerance is very high (${(drawdown * 100).toFixed(0)}%) — significant portfolio loss allowed before halting`,
        detail: 'Consider lowering to 15–20% to protect capital.',
      })
    }

    if (mode === 'live' && activePairs > 15) {
      list.push({
        type: 'warning',
        icon: '⚠️',
        message: `${activePairs} active pairs in live mode — high monitoring cost and trade complexity`,
        detail: 'Start with fewer pairs in live mode and scale up gradually.',
      })
    }

    if (dailyLoss > 0 && dailySpend > 0 && dailyLoss > dailySpend * 0.9) {
      list.push({
        type: 'info',
        icon: 'ℹ️',
        message: `Daily loss cap (€${dailyLoss}) is ${Math.round((dailyLoss / dailySpend) * 100)}% of daily spend (€${dailySpend}) — effectively the same limit`,
        detail: 'Consider setting a meaningful gap between spend and loss limits.',
      })
    }

    if (mode === 'paper') {
      list.push({
        type: 'info',
        icon: '📝',
        message: 'Running in paper mode — all trades are simulated, no real money at risk',
        detail: undefined,
      })
    }

    return list
  }, [trading, risk, absolute])

  const typeColors: Record<string, string> = {
    error: '#ef4444',
    warning: '#f59e0b',
    info: '#58a6ff',
  }
  const typeBgs: Record<string, string> = {
    error: '#ef444410',
    warning: '#f59e0b10',
    info: '#58a6ff10',
  }

  const hasProblems = issues.some(i => i.type === 'error' || i.type === 'warning')

  if (issues.length === 0) {
    return (
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8, padding: '10px 16px',
        background: '#22c55e10', border: '1px solid #22c55e22', borderRadius: 10,
        marginBottom: 16,
      }}>
        <ShieldCheck size={14} style={{ color: '#22c55e', flexShrink: 0 }} />
        <span style={{ fontSize: 13, color: '#22c55e', fontWeight: 500 }}>Configuration looks healthy</span>
      </div>
    )
  }

  return (
    <div style={{
      border: `1px solid ${hasProblems ? '#f59e0b33' : '#21262d'}`,
      borderRadius: 10, marginBottom: 16, overflow: 'hidden',
      background: hasProblems ? '#f59e0b08' : '#0d111788',
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8, padding: '10px 16px',
        borderBottom: issues.length > 1 ? '1px solid #21262d' : undefined,
      }}>
        {hasProblems
          ? <AlertTriangle size={14} style={{ color: '#f59e0b', flexShrink: 0 }} />
          : <Info size={14} style={{ color: '#58a6ff', flexShrink: 0 }} />}
        <span style={{ fontSize: 13, fontWeight: 600, color: hasProblems ? '#f59e0b' : '#58a6ff' }}>
          {issues.filter(i => i.type === 'error').length > 0
            ? `${issues.filter(i => i.type === 'error').length} config error${issues.filter(i => i.type === 'error').length > 1 ? 's' : ''} detected`
            : hasProblems
              ? `${issues.filter(i => i.type === 'warning').length} config warning${issues.filter(i => i.type === 'warning').length > 1 ? 's' : ''}`
              : `${issues.length} configuration note${issues.length > 1 ? 's' : ''}`}
        </span>
      </div>
      <div style={{ padding: '6px 0' }}>
        {issues.map((issue, i) => (
          <div key={i} style={{
            display: 'flex', gap: 10, padding: '8px 16px',
            background: i % 2 === 0 ? 'transparent' : '#0d111720',
            borderLeft: `3px solid ${typeColors[issue.type]}`,
            marginLeft: 0,
          }}>
            <span style={{ fontSize: 13, flexShrink: 0, marginTop: 1 }}>{issue.icon}</span>
            <div>
              <div style={{
                fontSize: 12, color: '#c9d1d9', fontWeight: 500,
                background: typeBgs[issue.type], display: 'inline',
                padding: '1px 0',
              }}>
                {issue.message}
              </div>
              {issue.detail && (
                <div style={{ fontSize: 11, color: '#8b949e', marginTop: 3, lineHeight: 1.4 }}>
                  {issue.detail}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════════════════
   Main Settings Page
   ═══════════════════════════════════════════════════════════════════════════ */

export default function Settings() {
  const queryClient = useQueryClient()
  const { data, isLoading, error } = useQuery({ queryKey: ['settings'], queryFn: fetchSettings })
  const { data: presetsData } = useQuery({ queryKey: ['presets'], queryFn: fetchPresets })

  const mutation = useMutation({
    mutationFn: updateSettings,
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['settings'] }) },
  })

  const [activeTab, setActiveTab] = useState<CategoryKey>('trading')
  const [searchQuery, setSearchQuery] = useState('')
  const [hoveredPreset, setHoveredPreset] = useState<string | null>(null)
  const [pendingPreset, setPendingPreset] = useState<string | null>(null)
  const [toast, setToast] = useState<{ message: string; type: 'success' | 'error' } | null>(null)
  const searchRef = useRef<HTMLInputElement>(null)

  const handlePreset = async (preset: string) => {
    setPendingPreset(preset) // optimistic — highlight immediately
    try {
      await mutation.mutateAsync({ preset })
      setToast({ message: `${preset.charAt(0).toUpperCase() + preset.slice(1)} preset applied — changes are live!`, type: 'success' })
    } catch (e: unknown) {
      setPendingPreset(null) // revert on failure
      setToast({ message: `Failed to apply preset: ${e instanceof Error ? e.message : String(e)}`, type: 'error' })
    }
  }

  // Once fresh settings arrive, hand back to server-side detection
  useEffect(() => {
    if (pendingPreset !== null) setPendingPreset(null)
  }, [data]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleSaveSection = async (section: string, updates: Record<string, unknown>) => {
    const settings = data?.settings ?? {}
    const sectionData = settings[section]
    if (sectionData && typeof sectionData === 'object' && !Array.isArray(sectionData)) {
      const sectionSchema = data?.schema?.[section]
      if (sectionSchema && sectionSchema.nested) {
        for (const [subName, subUpdates] of Object.entries(updates)) {
          if (typeof subUpdates === 'object' && subUpdates !== null && !Array.isArray(subUpdates)) {
            const original = (sectionData as Record<string, Record<string, unknown>>)[subName] ?? {}
            const changes: Record<string, unknown> = {}
            for (const [k, v] of Object.entries(subUpdates as Record<string, unknown>))
              if (JSON.stringify(v) !== JSON.stringify(original[k])) changes[k] = v
            if (Object.keys(changes).length > 0)
              await mutation.mutateAsync({ section: `${section}.${subName}`, updates: changes })
          }
        }
        return
      }
    }
    await mutation.mutateAsync({ section, updates })
  }

  const settings = data?.settings ?? {}
  const presets = presetsData?.presets ?? {}
  const detectedPreset = useMemo(() => detectActivePreset(settings, presets), [settings, presets])
  // Use optimistic value while waiting for server confirmation, then fall back to detection
  const activePreset = pendingPreset ?? detectedPreset

  // Ctrl+K to focus search
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault(); searchRef.current?.focus() }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  /* Loading / error states */
  if (isLoading) return (
    <PageTransition>
      <div style={{ padding: 40, color: '#8b949e', textAlign: 'center' }}>
        <RefreshCw size={20} style={{ animation: 'spin 1s linear infinite' }} />
        <div style={{ marginTop: 12, fontSize: 14 }}>Loading settings…</div>
      </div>
    </PageTransition>
  )

  if (error) return (
    <PageTransition>
      <div style={{ padding: 40, textAlign: 'center' }}>
        <AlertTriangle size={24} style={{ color: '#ef4444', marginBottom: 12 }} />
        <div style={{ fontSize: 14, color: '#ef4444' }}>Failed to load settings</div>
        <div style={{ fontSize: 12, color: '#8b949e', marginTop: 4 }}>{(error as Error).message}</div>
      </div>
    </PageTransition>
  )

  if (!data) return null

  const { trading_enabled, section_labels, schema } = data
  const sortedSections = SECTION_ORDER.filter(s => settings[s] !== undefined)
  const visibleSections = searchQuery
    ? sortedSections
    : sortedSections.filter(s => SECTION_CATEGORIES[activeTab].sections.includes(s))

  // Preset diff panel
  const panelKey = hoveredPreset && hoveredPreset !== activePreset ? hoveredPreset : activePreset
  const panelPreset = panelKey ? presets[panelKey] : null
  const panelDiff = panelPreset ? buildPresetDiff(settings, panelPreset) : []
  const isComparison = hoveredPreset !== null && hoveredPreset !== activePreset

  // Status chips for header
  const tradingSettings = (settings.trading ?? {}) as Record<string, unknown>
  const riskSettings = (settings.risk ?? {}) as Record<string, unknown>
  const statusChips = [
    {
      label: tradingSettings.mode === 'live' ? '⚡ Live mode' : '📝 Paper mode',
      color: tradingSettings.mode === 'live' ? '#22c55e' : '#8b949e',
      bg: tradingSettings.mode === 'live' ? '#22c55e15' : '#8b949e15',
    },
    {
      label: `${tradingSettings.max_active_pairs ?? '?'} pairs`,
      color: '#58a6ff',
      bg: '#58a6ff12',
    },
    {
      label: `${((Number(tradingSettings.min_confidence ?? 0)) * 100).toFixed(0)}% confidence`,
      color: '#a78bfa',
      bg: '#a78bfa12',
    },
    {
      label: `every ${Number(tradingSettings.interval ?? 120) >= 60 ? `${Number(tradingSettings.interval ?? 120) / 60}m` : `${tradingSettings.interval}s`}`,
      color: '#f59e0b',
      bg: '#f59e0b12',
    },
    {
      label: `SL ${((Number(riskSettings.stop_loss_pct ?? 0)) * 100).toFixed(1)}% / TP ${((Number(riskSettings.take_profit_pct ?? 0)) * 100).toFixed(1)}%`,
      color: '#e6edf3',
      bg: '#e6edf312',
    },
  ]

  return (
    <PageTransition>
    <div style={{ padding: '20px 24px', maxWidth: 960 }}>

      {/* ─── Header ─── */}
      <div style={{ marginBottom: 16 }}>
        <h1 style={{ fontSize: 22, fontWeight: 700, color: '#e6edf3', margin: 0 }}>Settings</h1>
        <p style={{ fontSize: 13, color: '#8b949e', margin: '4px 0 10px' }}>
          All changes are validated, saved to disk, and applied to the running service instantly.
        </p>
        {/* Status chips */}
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {statusChips.map((chip, i) => (
            <span key={i} style={{
              fontSize: 11, padding: '3px 10px', borderRadius: 20, fontWeight: 500,
              color: chip.color, background: chip.bg, border: `1px solid ${chip.color}22`,
            }}>
              {chip.label}
            </span>
          ))}
        </div>
      </div>

      {/* ─── Trading Status Banner ─── */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 12, padding: '14px 18px',
        background: trading_enabled
          ? 'linear-gradient(135deg, #22c55e08, #22c55e15)'
          : 'linear-gradient(135deg, #ef444408, #ef444415)',
        border: `1px solid ${trading_enabled ? '#22c55e33' : '#ef444433'}`,
        borderRadius: 10, marginBottom: 16,
      }}>
        <span style={{
          width: 10, height: 10, borderRadius: '50%',
          background: trading_enabled ? '#22c55e' : '#ef4444',
          boxShadow: `0 0 8px ${trading_enabled ? '#22c55e60' : '#ef444460'}`,
          animation: trading_enabled ? 'pulse 2s infinite' : undefined,
        }} />
        <div style={{ flex: 1 }}>
          <span style={{ fontWeight: 600, fontSize: 14, color: '#e6edf3' }}>
            Trading is {trading_enabled ? 'ENABLED' : 'DISABLED'}
          </span>
          <span style={{ fontSize: 11, color: '#8b949e', marginLeft: 10 }}>
            {trading_enabled ? 'The bot is actively analyzing markets and executing trades' : 'All trading activity is halted'}
          </span>
        </div>
        <button onClick={() => handlePreset(trading_enabled ? 'disabled' : 'moderate')} style={{
          ...btnStyle(trading_enabled ? '#21262d' : '#238636'),
          padding: '8px 18px', fontSize: 13,
          borderColor: trading_enabled ? '#30363d' : '#238636',
        }}>
          {trading_enabled ? 'Disable Trading' : 'Enable Trading'}
        </button>
      </div>

      {/* ─── Quick Presets ─── */}
      <div style={{ marginBottom: 20 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
          <span style={{ fontSize: 12, fontWeight: 600, color: '#8b949e', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            Quick Presets
          </span>
          {activePreset ? (
            <span style={{
              fontSize: 10, padding: '2px 8px', borderRadius: 10,
              background: PRESET_CONFIG[activePreset].color + '18',
              color: PRESET_CONFIG[activePreset].color, fontWeight: 600,
              border: `1px solid ${PRESET_CONFIG[activePreset].color}22`,
            }}>{PRESET_CONFIG[activePreset].label} active</span>
          ) : (
            <span style={{
              fontSize: 10, padding: '2px 8px', borderRadius: 10,
              background: '#8b949e18', color: '#8b949e', fontWeight: 600, border: '1px solid #8b949e22',
            }}>Custom configuration</span>
          )}
        </div>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          {Object.entries(PRESET_CONFIG).map(([key, cfg]) => {
            const isActive = key === activePreset
            const isHovered = hoveredPreset === key
            return (
              <button key={key}
                onClick={() => !isActive && handlePreset(key)}
                onMouseEnter={() => setHoveredPreset(key)}
                onMouseLeave={() => setHoveredPreset(null)}
                style={{
                  display: 'flex', alignItems: 'center', gap: 10, position: 'relative',
                  background: isActive
                    ? `linear-gradient(135deg, ${cfg.color}12, ${cfg.color}20)`
                    : isHovered ? `${cfg.color}08` : '#0d1117',
                  border: isActive ? `2px solid ${cfg.color}99` : `1px solid ${cfg.color}44`,
                  borderRadius: 12,
                  padding: isActive ? '12px 18px' : '13px 19px',
                  cursor: isActive ? 'default' : 'pointer',
                  color: '#e6edf3', minWidth: 160, transition: 'all 0.2s',
                  boxShadow: isActive ? `0 0 24px ${cfg.color}25, inset 0 1px 0 ${cfg.color}20` : 'none',
                }}
              >
                <span style={{ color: cfg.color, fontSize: 22 }}>{cfg.icon}</span>
                <div style={{ textAlign: 'left' }}>
                  <div style={{ fontWeight: 700, fontSize: 14 }}>{cfg.label}</div>
                  <div style={{ fontSize: 11, color: isActive ? cfg.color + 'cc' : '#6e7681', marginTop: 1 }}>{cfg.desc}</div>
                </div>
                {isActive && (
                  <span style={{
                    position: 'absolute', top: -8, right: -8,
                    width: 22, height: 22, borderRadius: '50%',
                    background: cfg.color, display: 'flex', alignItems: 'center', justifyContent: 'center',
                    boxShadow: `0 0 10px ${cfg.color}70, 0 0 4px ${cfg.color}`,
                  }}><Check size={12} color="#fff" strokeWidth={3} /></span>
                )}
              </button>
            )
          })}
        </div>

        {/* Preset impact preview */}
        {panelKey && panelDiff.length > 0 && (
          <div style={{
            marginTop: 12, padding: '14px 18px',
            background: '#0d1117', border: `1px solid ${PRESET_CONFIG[panelKey]?.color ?? '#30363d'}33`,
            borderRadius: 10, transition: 'all 0.15s',
          }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 12, display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={{ width: 7, height: 7, borderRadius: '50%', background: PRESET_CONFIG[panelKey]?.color ?? '#8b949e' }} />
              {isComparison
                ? `Switching to ${PRESET_CONFIG[panelKey]?.label} would change ${panelDiff.filter(r => r.changed).length} setting${panelDiff.filter(r => r.changed).length !== 1 ? 's' : ''}:`
                : `${PRESET_CONFIG[panelKey]?.label} preset values:`}
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: '4px 20px' }}>
              {panelDiff.map(row => (
                <div key={row.key} style={{
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                  padding: '5px 0', fontSize: 12, borderBottom: '1px solid #161b22',
                }}>
                  <span style={{ color: row.changed && isComparison ? '#c9d1d9' : '#8b949e' }}>{row.label}</span>
                  {isComparison && row.changed ? (
                    <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                      <span style={{ color: '#484f58', textDecoration: 'line-through', fontSize: 11 }}>{formatFieldValue(row.key, row.current)}</span>
                      <ArrowRight size={10} style={{ color: PRESET_CONFIG[panelKey]?.color ?? '#8b949e' }} />
                      <span style={{ color: PRESET_CONFIG[panelKey]?.color ?? '#e6edf3', fontWeight: 700, fontSize: 13 }}>{formatFieldValue(row.key, row.target)}</span>
                    </span>
                  ) : (
                    <span style={{ color: row.changed ? '#f59e0b' : '#c9d1d9', fontWeight: row.changed ? 600 : 400 }}>
                      {formatFieldValue(row.key, row.target)}
                      {!isComparison && row.changed && <span style={{ fontSize: 10, color: '#f59e0b', marginLeft: 4 }} title="Differs from current">*</span>}
                    </span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* ─── RPM Entity Budget (trading & intelligence tabs) ─── */}
      {data.rpm_budget && !searchQuery && (activeTab === 'trading' || activeTab === 'intelligence') && (
        <RpmBudgetCard
          rpm_budget={data.rpm_budget}
          current_pairs={(settings.trading as Record<string, unknown>)?.pairs
            ? ((settings.trading as Record<string, unknown>).pairs as string[]).length
            : 0}
        />
      )}

      {/* ─── Quick Settings ─── */}
      {!searchQuery && (
        <QuickSettings
          settings={settings}
          liveData={data}
          onSave={handleSaveSection}
        />
      )}

      {/* ─── Config Health ─── */}
      {!searchQuery && <ConfigHealthPanel settings={settings} />}

      {/* ─── Search bar ─── */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '10px 14px', background: '#0d1117', border: '1px solid #21262d',
        borderRadius: 10, marginBottom: 16,
      }}>
        <Search size={14} style={{ color: '#484f58' }} />
        <input ref={searchRef} type="text" value={searchQuery}
          onChange={e => setSearchQuery(e.target.value)}
          placeholder="Search settings… (Ctrl+K)"
          style={{ background: 'transparent', border: 'none', color: '#e6edf3', fontSize: 13, flex: 1, outline: 'none' }}
        />
        {searchQuery && (
          <button onClick={() => setSearchQuery('')}
            style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#6e7681', padding: 0 }}>
            <X size={14} />
          </button>
        )}
        <span style={{ fontSize: 10, color: '#484f58', padding: '2px 6px', background: '#161b22', borderRadius: 4 }}>Ctrl+K</span>
      </div>

      {/* ─── Category tabs ─── */}
      {!searchQuery && (
        <div style={{ display: 'flex', gap: 4, marginBottom: 16, borderBottom: '1px solid #21262d', paddingBottom: 0 }}>
          {(Object.entries(SECTION_CATEGORIES) as [CategoryKey, typeof SECTION_CATEGORIES[CategoryKey]][]).map(([key, cat]) => (
            <button key={key} onClick={() => setActiveTab(key)} style={{
              display: 'flex', alignItems: 'center', gap: 6,
              padding: '10px 16px', fontSize: 13, fontWeight: 500,
              background: 'transparent', border: 'none',
              color: activeTab === key ? '#e6edf3' : '#6e7681',
              borderBottom: activeTab === key ? '2px solid #22c55e' : '2px solid transparent',
              cursor: 'pointer', transition: 'all 0.15s', marginBottom: -1,
            }}>
              {cat.icon} {cat.label}
            </button>
          ))}
        </div>
      )}

      {/* ─── Telegram safety legend (Trading & Infra tabs) ─── */}
      {!searchQuery && (activeTab === 'trading' || activeTab === 'infra') && (
        <div style={{
          display: 'flex', gap: 16, marginBottom: 14, padding: '8px 14px',
          background: '#0d111788', borderRadius: 8, fontSize: 11, color: '#6e7681',
          alignItems: 'center', flexWrap: 'wrap', border: '1px solid #21262d',
        }}>
          <Info size={12} style={{ flexShrink: 0 }} />
          <span>Telegram access tiers:</span>
          {Object.entries(TIER_LABELS).map(([tier, label]) => (
            <span key={tier} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
              <span style={{ width: 8, height: 8, borderRadius: 3, background: TIER_COLORS[tier] }} /> {label}
            </span>
          ))}
        </div>
      )}

      {/* ─── Search results info ─── */}
      {searchQuery && (
        <div style={{ fontSize: 12, color: '#8b949e', marginBottom: 12, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Search size={12} />
          Showing all sections matching &quot;<strong style={{ color: '#e6edf3' }}>{searchQuery}</strong>&quot;
        </div>
      )}

      {/* ─── Appearance tab content ─── */}
      {!searchQuery && activeTab === 'appearance' && <DensityToggle />}

      {/* ─── LLM Providers (AI tab or search mode) ─── */}
      {(searchQuery || activeTab === 'intelligence') && <LLMProvidersSection />}

      {/* ─── Telegram Setup Guide (Infra tab) ─── */}
      {!searchQuery && activeTab === 'infra' && <TelegramSetupGuide />}

      {/* ─── Setting sections ─── */}
      {visibleSections.map(sectionName => {
        const sectionSchema = schema?.[sectionName]
        const telegramTier = sectionSchema?.telegram_tier ?? 'blocked'
        return (
          <SectionCard
            key={sectionName}
            name={sectionName}
            label={section_labels[sectionName] ?? sectionName}
            values={(settings[sectionName] ?? {}) as Record<string, unknown>}
            schema={sectionSchema}
            telegramTier={telegramTier}
            onSave={handleSaveSection}
            searchQuery={searchQuery}
          />
        )
      })}

      {/* Empty search */}
      {searchQuery && visibleSections.length === 0 && (
        <div style={{ padding: 40, textAlign: 'center', color: '#6e7681' }}>
          <Search size={24} style={{ marginBottom: 12, opacity: 0.5 }} />
          <div style={{ fontSize: 14 }}>No settings match &quot;{searchQuery}&quot;</div>
          <div style={{ fontSize: 12, marginTop: 4 }}>Try a different search term</div>
        </div>
      )}

      {/* Toast */}
      {toast && <Toast message={toast.message} type={toast.type} onDismiss={() => setToast(null)} />}
    </div>

    {/* Keyframe animations */}
    <style>{`
      @keyframes toastSlideIn { from { transform: translateY(20px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
      @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.6; } }
      @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
    `}</style>
    </PageTransition>
  )
}
