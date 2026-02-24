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
        <span style={{ fontWeight: 600, fontSize: 14, flex: 1, textAlign: 'left' }}>LLM Provider Chain</span>
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
          {/* Explanation */}
          <div style={{ fontSize: 12, color: '#8b949e', padding: '12px 0 8px', lineHeight: 1.5, display: 'flex', alignItems: 'flex-start', gap: 8 }}>
            <Info size={14} style={{ flexShrink: 0, marginTop: 1 }} />
            <span>
              Providers are tried <strong style={{ color: '#c9d1d9' }}>top-to-bottom</strong>. The first available provider handles each LLM call.
              If a provider hits rate limits or errors, it enters cooldown and the next one is tried.
              Drag to reorder priority. API keys are stored securely in <code style={codeStyle}>config/.env</code>.
            </span>
          </div>

          {/* Action bar */}
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

          {/* Provider cards */}
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
                {/* Provider header */}
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

                {/* Detail grid */}
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(170px, 1fr))', gap: '6px 20px', marginTop: 10, fontSize: 12 }}>
                  {/* Model */}
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

                  {/* API Key */}
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

                  {/* Tier (edit mode) */}
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

                  {/* OpenRouter Credits */}
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

                  {/* Free model indicator */}
                  {!editing && p.live_status?.is_free_model && (
                    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', borderBottom: '1px solid #21262d' }}>
                      <span style={{ color: '#8b949e' }}>Model Type</span>
                      <span style={{ color: '#22c55e', fontSize: 11, display: 'flex', alignItems: 'center', gap: 4 }}>
                        <Sparkles size={10} /> Free model
                      </span>
                    </div>
                  )}

                  {/* Timeout */}
                  <div style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', borderBottom: '1px solid #21262d' }}>
                    <span style={{ color: '#8b949e' }}>Timeout</span>
                    {editing ? (
                      <input type="number" value={p.timeout ?? 60} min={5} max={600}
                        onChange={e => updateField(idx, 'timeout', parseInt(e.target.value, 10))}
                        style={{ ...inputBase, width: 60, textAlign: 'right', padding: '2px 6px', fontSize: 12 }}
                      />
                    ) : <span style={{ color: '#e6edf3' }}>{p.timeout ?? 60}s</span>}
                  </div>

                  {/* Rate limits (cloud) */}
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
  const [toast, setToast] = useState<{ message: string; type: 'success' | 'error' } | null>(null)
  const searchRef = useRef<HTMLInputElement>(null)

  const handlePreset = async (preset: string) => {
    try {
      await mutation.mutateAsync({ preset })
      setToast({ message: `${preset.charAt(0).toUpperCase() + preset.slice(1)} preset applied — changes are live!`, type: 'success' })
    } catch (e: unknown) {
      setToast({ message: `Failed to apply preset: ${e instanceof Error ? e.message : String(e)}`, type: 'error' })
    }
  }

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
  const activePreset = useMemo(() => detectActivePreset(settings, presets), [settings, presets])

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

  return (
    <PageTransition>
    <div style={{ padding: '20px 24px', maxWidth: 960 }}>

      {/* ─── Header ─── */}
      <div style={{ marginBottom: 20 }}>
        <h1 style={{ fontSize: 22, fontWeight: 700, color: '#e6edf3', margin: 0 }}>Settings</h1>
        <p style={{ fontSize: 13, color: '#8b949e', margin: '4px 0 0' }}>
          All changes are validated, saved to disk, and applied to the running service instantly — no restarts needed.
        </p>
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

      {/* ─── RPM Entity Budget (trading & intelligence tabs) ─── */}
      {data.rpm_budget && !searchQuery && (activeTab === 'trading' || activeTab === 'intelligence') && (
        <RpmBudgetCard
          rpm_budget={data.rpm_budget}
          current_pairs={(settings.trading as Record<string, unknown>)?.pairs
            ? ((settings.trading as Record<string, unknown>).pairs as string[]).length
            : 0}
        />
      )}

      {/* ─── Quick Presets ─── */}
      <div style={{ marginBottom: 20 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
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
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          {Object.entries(PRESET_CONFIG).map(([key, cfg]) => {
            const isActive = key === activePreset
            return (
              <button key={key}
                onClick={() => !isActive && handlePreset(key)}
                onMouseEnter={() => setHoveredPreset(key)}
                onMouseLeave={() => setHoveredPreset(null)}
                style={{
                  display: 'flex', alignItems: 'center', gap: 8, position: 'relative',
                  background: isActive ? cfg.color + '14' : '#0d1117',
                  border: isActive ? `2px solid ${cfg.color}88` : `1px solid ${cfg.color}33`,
                  borderRadius: 10,
                  padding: isActive ? '10px 16px' : '11px 17px',
                  cursor: isActive ? 'default' : 'pointer',
                  color: '#e6edf3', minWidth: 150, transition: 'all 0.2s',
                  boxShadow: isActive ? `0 0 16px ${cfg.color}20` : 'none',
                }}
              >
                <span style={{ color: cfg.color }}>{cfg.icon}</span>
                <div style={{ textAlign: 'left' }}>
                  <div style={{ fontWeight: 600, fontSize: 13 }}>{cfg.label}</div>
                  <div style={{ fontSize: 11, color: isActive ? cfg.color + 'cc' : '#6e7681' }}>{cfg.desc}</div>
                </div>
                {isActive && (
                  <span style={{
                    position: 'absolute', top: -7, right: -7,
                    width: 20, height: 20, borderRadius: '50%',
                    background: cfg.color, display: 'flex', alignItems: 'center', justifyContent: 'center',
                    boxShadow: `0 0 8px ${cfg.color}60`,
                  }}><Check size={11} color="#fff" strokeWidth={3} /></span>
                )}
              </button>
            )
          })}
        </div>

        {/* Preset impact preview */}
        {panelKey && panelDiff.length > 0 && (
          <div style={{
            marginTop: 10, padding: '12px 16px',
            background: '#0d1117', border: `1px solid ${PRESET_CONFIG[panelKey]?.color ?? '#30363d'}22`,
            borderRadius: 10, transition: 'all 0.15s',
          }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={{ width: 6, height: 6, borderRadius: '50%', background: PRESET_CONFIG[panelKey]?.color ?? '#8b949e' }} />
              {isComparison
                ? `Switching to ${PRESET_CONFIG[panelKey]?.label} would change:`
                : `${PRESET_CONFIG[panelKey]?.label} preset values:`}
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(210px, 1fr))', gap: '4px 20px' }}>
              {panelDiff.map(row => (
                <div key={row.key} style={{
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                  padding: '4px 0', fontSize: 12, borderBottom: '1px solid #161b22',
                }}>
                  <span style={{ color: '#8b949e' }}>{row.label}</span>
                  {isComparison && row.changed ? (
                    <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                      <span style={{ color: '#484f58', textDecoration: 'line-through', fontSize: 11 }}>{formatFieldValue(row.key, row.current)}</span>
                      <ArrowRight size={10} style={{ color: PRESET_CONFIG[panelKey]?.color ?? '#8b949e' }} />
                      <span style={{ color: PRESET_CONFIG[panelKey]?.color ?? '#e6edf3', fontWeight: 600 }}>{formatFieldValue(row.key, row.target)}</span>
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
