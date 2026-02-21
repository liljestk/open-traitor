import { useState, useMemo, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  fetchSettings, updateSettings, fetchPresets,
  fetchLLMProviders, updateLLMProviders,
  type SectionSchema, type FieldSchema, type PresetInfo,
  type LLMProviderConfig,
} from '../api'
import {
  Shield, ShieldAlert, ShieldOff, ToggleLeft, ToggleRight,
  ChevronDown, ChevronRight, Save, X, AlertTriangle, Check,
  Info, ArrowRight, ArrowUp, ArrowDown, Zap, Server, Cloud,
} from 'lucide-react'

// ── Preset button config ────────────────────────────────────────────────────

const PRESET_CONFIG: Record<string, { label: string; color: string; icon: ReactNode; desc: string }> = {
  disabled:     { label: 'Disabled',     color: '#6e7681', icon: <ShieldOff size={18} />,   desc: 'All trading stopped' },
  conservative: { label: 'Conservative', color: '#3b82f6', icon: <Shield size={18} />,      desc: 'Low risk, small trades' },
  moderate:     { label: 'Moderate',     color: '#22c55e', icon: <Shield size={18} />,      desc: 'Balanced risk/reward' },
  aggressive:   { label: 'Aggressive',   color: '#f59e0b', icon: <ShieldAlert size={18} />, desc: 'Higher limits, more trades' },
}

const TIER_COLORS: Record<string, string> = {
  safe: '#22c55e',
  semi_safe: '#f59e0b',
  blocked: '#ef4444',
}

const TIER_LABELS: Record<string, string> = {
  safe: 'Telegram Safe',
  semi_safe: 'Semi-Safe',
  blocked: 'Dashboard Only',
}

// ── Preset detection ────────────────────────────────────────────────────────

/** Human-friendly labels for preset fields */
const FIELD_LABELS: Record<string, string> = {
  max_single_trade: 'Max Single Trade',
  max_daily_spend: 'Max Daily Spend',
  max_daily_loss: 'Max Daily Loss',
  max_portfolio_risk_pct: 'Portfolio Risk %',
  require_approval_above: 'Approval Above',
  max_trades_per_day: 'Max Trades/Day',
  max_cash_per_trade_pct: 'Cash/Trade %',
  min_confidence: 'Min Confidence',
  max_open_positions: 'Max Open Positions',
}

function formatFieldValue(key: string, val: any): string {
  if (val === null || val === undefined) return '—'
  if (key.endsWith('_pct')) return `${(val * 100).toFixed(1)}%`
  if (typeof val === 'number') return val.toLocaleString()
  return String(val)
}

/**
 * Check if current settings match a preset's values exactly.
 * Returns the name of the matching preset or null.
 */
function detectActivePreset(
  settings: Record<string, any>,
  presets: Record<string, PresetInfo>,
): string | null {
  for (const [name, preset] of Object.entries(presets)) {
    let matches = true
    for (const [section, fields] of Object.entries(preset.values)) {
      const current = settings[section]
      if (!current) { matches = false; break }
      for (const [field, expected] of Object.entries(fields)) {
        if (JSON.stringify(current[field]) !== JSON.stringify(expected)) {
          matches = false
          break
        }
      }
      if (!matches) break
    }
    if (matches) return name
  }
  return null
}

/**
 * Build a flat diff of current values vs a preset's target values.
 * Returns entries: { field, section, label, current, target, changed }.
 */
function buildPresetDiff(
  settings: Record<string, any>,
  preset: PresetInfo,
): Array<{ key: string; section: string; label: string; current: any; target: any; changed: boolean }> {
  const rows: Array<{ key: string; section: string; label: string; current: any; target: any; changed: boolean }> = []
  for (const [section, fields] of Object.entries(preset.values)) {
    for (const [field, target] of Object.entries(fields)) {
      const current = settings[section]?.[field]
      rows.push({
        key: `${section}.${field}`,
        section,
        label: FIELD_LABELS[field] ?? formatKey(field),
        current,
        target,
        changed: JSON.stringify(current) !== JSON.stringify(target),
      })
    }
  }
  return rows
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function formatKey(key: string): string {
  return key.replace(/_/g, ' ').replace(/\bpct\b/g, '%').replace(/\b\w/g, c => c.toUpperCase())
}

function renderFieldInput(
  key: string,
  value: any,
  schema: FieldSchema | undefined,
  onChange: (key: string, val: any) => void,
) {
  const type = schema?.type ?? (typeof value)

  if (type === 'bool' || typeof value === 'boolean') {
    return (
      <button
        onClick={() => onChange(key, !value)}
        style={{
          background: 'none', border: 'none', cursor: 'pointer', padding: 0,
          color: value ? '#22c55e' : '#6e7681',
        }}
        title={value ? 'Enabled' : 'Disabled'}
      >
        {value ? <ToggleRight size={22} /> : <ToggleLeft size={22} />}
      </button>
    )
  }

  if (schema?.enum) {
    return (
      <select
        value={String(value)}
        onChange={(e) => onChange(key, e.target.value)}
        style={{
          background: '#161b22', color: '#e6edf3', border: '1px solid #30363d',
          borderRadius: 4, padding: '3px 8px', fontSize: 13, minWidth: 100,
        }}
      >
        {schema.enum.map(opt => <option key={opt} value={opt}>{opt}</option>)}
      </select>
    )
  }

  if (type === 'list' || Array.isArray(value)) {
    return (
      <input
        type="text"
        value={Array.isArray(value) ? value.join(', ') : String(value ?? '')}
        onChange={(e) => onChange(key, e.target.value.split(',').map(s => s.trim()).filter(Boolean))}
        style={{
          background: '#161b22', color: '#e6edf3', border: '1px solid #30363d',
          borderRadius: 4, padding: '3px 8px', fontSize: 13, width: '100%', minWidth: 150,
        }}
        title="Comma-separated list"
      />
    )
  }

  if (type === 'str' || type === 'string') {
    const isLong = typeof value === 'string' && value.length > 60
    if (isLong) {
      return (
        <textarea
          value={String(value ?? '')}
          onChange={(e) => onChange(key, e.target.value)}
          rows={3}
          style={{
            background: '#161b22', color: '#e6edf3', border: '1px solid #30363d',
            borderRadius: 4, padding: '4px 8px', fontSize: 13, width: '100%',
            resize: 'vertical', fontFamily: 'inherit',
          }}
        />
      )
    }
    return (
      <input
        type="text"
        value={String(value ?? '')}
        onChange={(e) => onChange(key, e.target.value)}
        style={{
          background: '#161b22', color: '#e6edf3', border: '1px solid #30363d',
          borderRadius: 4, padding: '3px 8px', fontSize: 13, minWidth: 150,
        }}
      />
    )
  }

  // number (int / float)
  return (
    <input
      type="number"
      value={value ?? ''}
      step={type === 'float' ? 0.01 : 1}
      min={schema?.min}
      max={schema?.max}
      onChange={(e) => {
        const v = e.target.value
        onChange(key, v === '' ? '' : type === 'int' ? parseInt(v, 10) : parseFloat(v))
      }}
      style={{
        background: '#161b22', color: '#e6edf3', border: '1px solid #30363d',
        borderRadius: 4, padding: '3px 8px', fontSize: 13, width: 110,
      }}
    />
  )
}

// ── Section Card ─────────────────────────────────────────────────────────────

function SectionCard({
  name, label, values, schema, telegramTier, onSave,
}: {
  name: string
  label: string
  values: Record<string, any>
  schema?: SectionSchema
  telegramTier: string
  onSave: (section: string, updates: Record<string, any>) => Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<Record<string, any>>({})
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const startEdit = () => {
    setDraft({ ...values })
    setEditing(true)
    setMsg(null)
  }

  const cancel = () => { setEditing(false); setMsg(null) }

  const handleChange = (key: string, val: any) => {
    setDraft(prev => ({ ...prev, [key]: val }))
  }

  const handleSave = async () => {
    // Find only changed keys
    const changes: Record<string, any> = {}
    for (const [k, v] of Object.entries(draft)) {
      if (JSON.stringify(v) !== JSON.stringify(values[k])) {
        changes[k] = v
      }
    }
    if (Object.keys(changes).length === 0) {
      setEditing(false)
      return
    }
    setSaving(true)
    try {
      await onSave(name, changes)
      setEditing(false)
      setMsg({ ok: true, text: 'Saved & applied' })
      setTimeout(() => setMsg(null), 3000)
    } catch (e: any) {
      setMsg({ ok: false, text: e.message || 'Save failed' })
    } finally {
      setSaving(false)
    }
  }

  const fields = schema?.fields ?? {}
  const fieldEntries = Object.entries(editing ? draft : values)
  const tierColor = TIER_COLORS[telegramTier] ?? '#6e7681'

  // Handle nested sections (e.g. analysis with technical/sentiment)
  const nested = schema && 'nested' in schema ? (schema as any).nested as Record<string, { fields: Record<string, FieldSchema> }> : null

  return (
    <div style={{
      background: '#0d1117', border: '1px solid #21262d', borderRadius: 8,
      marginBottom: 8, overflow: 'hidden',
    }}>
      {/* Header */}
      <button
        onClick={() => setOpen(!open)}
        style={{
          width: '100%', background: 'none', border: 'none', cursor: 'pointer',
          display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px',
          color: '#e6edf3',
        }}
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span style={{ fontWeight: 600, fontSize: 14, flex: 1, textAlign: 'left' }}>{label}</span>
        <span style={{
          fontSize: 10, padding: '2px 6px', borderRadius: 4,
          background: tierColor + '22', color: tierColor, fontWeight: 600,
        }}>
          {TIER_LABELS[telegramTier] ?? telegramTier}
        </span>
        {msg && (
          <span style={{ fontSize: 11, color: msg.ok ? '#22c55e' : '#ef4444', display: 'flex', alignItems: 'center', gap: 3 }}>
            {msg.ok ? <Check size={12} /> : <AlertTriangle size={12} />} {msg.text}
          </span>
        )}
      </button>

      {/* Body */}
      {open && (
        <div style={{ padding: '0 14px 14px', borderTop: '1px solid #21262d' }}>
          {/* Action bar */}
          <div style={{ display: 'flex', gap: 8, padding: '10px 0 6px', justifyContent: 'flex-end' }}>
            {!editing ? (
              <button onClick={startEdit} style={btnStyle('#30363d')}>Edit</button>
            ) : (
              <>
                <button onClick={cancel} style={btnStyle('#30363d')}><X size={12} /> Cancel</button>
                <button onClick={handleSave} disabled={saving} style={btnStyle('#238636')}>
                  <Save size={12} /> {saving ? 'Saving…' : 'Save & Apply'}
                </button>
              </>
            )}
          </div>

          {/* Fields table */}
          {!nested ? (
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <tbody>
                {fieldEntries.map(([key, val]) => (
                  <tr key={key} style={{ borderBottom: '1px solid #161b22' }}>
                    <td style={{ padding: '5px 8px 5px 0', fontSize: 13, color: '#8b949e', whiteSpace: 'nowrap', width: '40%' }}>
                      {formatKey(key)}
                      {fields[key]?.min !== undefined && (
                        <span style={{ fontSize: 10, color: '#484f58', marginLeft: 4 }}>
                          [{fields[key].min}–{fields[key].max}]
                        </span>
                      )}
                    </td>
                    <td style={{ padding: '5px 0', fontSize: 13, color: '#e6edf3' }}>
                      {editing
                        ? renderFieldInput(key, val, fields[key], handleChange)
                        : <span>{renderValue(val)}</span>
                      }
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            // Nested sections (analysis.technical, analysis.sentiment)
            Object.entries(values as Record<string, any>).map(([subName, subValues]) => {
              const subSchema = nested[subName]
              const subFields = subSchema?.fields ?? {}
              return (
                <div key={subName} style={{ marginBottom: 12 }}>
                  <div style={{ fontSize: 12, fontWeight: 600, color: '#8b949e', textTransform: 'uppercase', letterSpacing: '0.06em', padding: '8px 0 4px' }}>
                    {formatKey(subName)}
                  </div>
                  <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                    <tbody>
                      {Object.entries(subValues as Record<string, any>).map(([key, val]) => (
                        <tr key={key} style={{ borderBottom: '1px solid #161b22' }}>
                          <td style={{ padding: '5px 8px 5px 12px', fontSize: 13, color: '#8b949e', whiteSpace: 'nowrap', width: '40%' }}>
                            {formatKey(key)}
                            {subFields[key]?.min !== undefined && (
                              <span style={{ fontSize: 10, color: '#484f58', marginLeft: 4 }}>
                                [{subFields[key].min}–{subFields[key].max}]
                              </span>
                            )}
                          </td>
                          <td style={{ padding: '5px 0', fontSize: 13, color: '#e6edf3' }}>
                            {editing
                              ? renderFieldInput(key, editing ? (draft[subName] as any)?.[key] ?? val : val, subFields[key], (k, v) => {
                                  setDraft(prev => ({
                                    ...prev,
                                    [subName]: { ...(prev[subName] as any ?? subValues), [k]: v },
                                  }))
                                })
                              : <span>{renderValue(val)}</span>
                            }
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )
            })
          )}
        </div>
      )}
    </div>
  )
}

function renderValue(val: any): string {
  if (typeof val === 'boolean') return val ? '✓ Enabled' : '✗ Disabled'
  if (Array.isArray(val)) return val.length ? val.join(', ') : '(empty)'
  if (val === null || val === undefined) return '—'
  return String(val)
}

function btnStyle(bg: string): React.CSSProperties {
  return {
    display: 'inline-flex', alignItems: 'center', gap: 4,
    background: bg, color: '#e6edf3', border: '1px solid #30363d',
    borderRadius: 6, padding: '5px 12px', fontSize: 12, fontWeight: 500,
    cursor: 'pointer',
  }
}

// ── LLM Providers Section ────────────────────────────────────────────────────

function LLMProvidersSection() {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({ queryKey: ['llm-providers'], queryFn: fetchLLMProviders })
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<LLMProviderConfig[]>([])
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const mutation = useMutation({
    mutationFn: updateLLMProviders,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['llm-providers'] })
      queryClient.invalidateQueries({ queryKey: ['settings'] })
    },
  })

  const providers = data?.providers ?? []

  const startEdit = () => {
    setDraft(providers.map(p => ({ ...p })))
    setEditing(true)
    setMsg(null)
  }

  const cancel = () => { setEditing(false); setMsg(null) }

  const handleSave = async () => {
    setSaving(true)
    try {
      await mutation.mutateAsync(draft)
      setEditing(false)
      setMsg({ ok: true, text: 'Saved & applied' })
      setTimeout(() => setMsg(null), 3000)
    } catch (e: any) {
      setMsg({ ok: false, text: e.message || 'Save failed' })
    } finally {
      setSaving(false)
    }
  }

  const moveProvider = (idx: number, dir: -1 | 1) => {
    const next = [...draft]
    const target = idx + dir
    if (target < 0 || target >= next.length) return
    ;[next[idx], next[target]] = [next[target], next[idx]]
    setDraft(next)
  }

  const updateDraftField = (idx: number, field: string, value: any) => {
    setDraft(prev => prev.map((p, i) => i === idx ? { ...p, [field]: value } : p))
  }

  const displayProviders = editing ? draft : providers

  const getStatusBadge = (p: LLMProviderConfig) => {
    if (!p.enabled) return { label: 'Disabled', color: '#6e7681' }
    if (!p.api_key_set && !p.is_local) return { label: 'No API Key', color: '#f59e0b' }
    if (p.live_status?.in_cooldown) return { label: 'Cooldown', color: '#f59e0b' }
    if (p.live_status?.available === false) return { label: 'Unavailable', color: '#ef4444' }
    return { label: 'Active', color: '#22c55e' }
  }

  return (
    <div style={{
      background: '#0d1117', border: '1px solid #21262d', borderRadius: 8,
      marginBottom: 8, overflow: 'hidden',
    }}>
      <button
        onClick={() => setOpen(!open)}
        style={{
          width: '100%', background: 'none', border: 'none', cursor: 'pointer',
          display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px',
          color: '#e6edf3',
        }}
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <Zap size={14} style={{ color: '#f59e0b' }} />
        <span style={{ fontWeight: 600, fontSize: 14, flex: 1, textAlign: 'left' }}>LLM Provider Chain</span>
        <span style={{
          fontSize: 10, padding: '2px 6px', borderRadius: 4,
          background: '#ef444422', color: '#ef4444', fontWeight: 600,
        }}>
          Dashboard Only
        </span>
        {!isLoading && (
          <span style={{ fontSize: 11, color: '#8b949e' }}>
            {providers.filter(p => p.enabled && (p.api_key_set || p.is_local)).length} active
          </span>
        )}
        {msg && (
          <span style={{ fontSize: 11, color: msg.ok ? '#22c55e' : '#ef4444', display: 'flex', alignItems: 'center', gap: 3 }}>
            {msg.ok ? <Check size={12} /> : <AlertTriangle size={12} />} {msg.text}
          </span>
        )}
      </button>

      {open && (
        <div style={{ padding: '0 14px 14px', borderTop: '1px solid #21262d' }}>
          {/* Action bar */}
          <div style={{ display: 'flex', gap: 8, padding: '10px 0 6px', justifyContent: 'flex-end', alignItems: 'center' }}>
            <span style={{ flex: 1, fontSize: 11, color: '#8b949e' }}>
              Providers are tried top-to-bottom. First available provider handles each call.
            </span>
            {!editing ? (
              <button onClick={startEdit} style={btnStyle('#30363d')}>Edit</button>
            ) : (
              <>
                <button onClick={cancel} style={btnStyle('#30363d')}><X size={12} /> Cancel</button>
                <button onClick={handleSave} disabled={saving} style={btnStyle('#238636')}>
                  <Save size={12} /> {saving ? 'Saving...' : 'Save & Apply'}
                </button>
              </>
            )}
          </div>

          {/* Provider cards */}
          {displayProviders.map((p, idx) => {
            const badge = getStatusBadge(p)
            const icon = p.is_local
              ? <Server size={16} style={{ color: '#8b949e' }} />
              : <Cloud size={16} style={{ color: '#58a6ff' }} />

            return (
              <div key={p.name} style={{
                background: '#161b22', border: '1px solid #21262d', borderRadius: 8,
                padding: '10px 14px', marginBottom: 6,
                opacity: p.enabled ? 1 : 0.5,
              }}>
                {/* Provider header */}
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  {/* Priority number */}
                  <span style={{
                    width: 22, height: 22, borderRadius: '50%',
                    background: '#30363d', display: 'flex', alignItems: 'center', justifyContent: 'center',
                    fontSize: 11, fontWeight: 700, color: '#e6edf3',
                  }}>
                    {idx + 1}
                  </span>

                  {icon}

                  <span style={{ fontWeight: 600, fontSize: 14, color: '#e6edf3', flex: 1 }}>
                    {p.name}
                  </span>

                  {/* Status badge */}
                  <span style={{
                    fontSize: 10, padding: '2px 8px', borderRadius: 10,
                    background: badge.color + '22', color: badge.color, fontWeight: 600,
                  }}>
                    {badge.label}
                  </span>

                  {/* Toggle */}
                  {editing && (
                    <button
                      onClick={() => updateDraftField(idx, 'enabled', !p.enabled)}
                      style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, color: p.enabled ? '#22c55e' : '#6e7681' }}
                    >
                      {p.enabled ? <ToggleRight size={22} /> : <ToggleLeft size={22} />}
                    </button>
                  )}

                  {/* Reorder buttons */}
                  {editing && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                      <button
                        onClick={() => moveProvider(idx, -1)}
                        disabled={idx === 0}
                        style={{
                          background: 'none', border: 'none', cursor: idx === 0 ? 'default' : 'pointer',
                          padding: 0, color: idx === 0 ? '#30363d' : '#8b949e',
                        }}
                      >
                        <ArrowUp size={14} />
                      </button>
                      <button
                        onClick={() => moveProvider(idx, 1)}
                        disabled={idx === displayProviders.length - 1}
                        style={{
                          background: 'none', border: 'none',
                          cursor: idx === displayProviders.length - 1 ? 'default' : 'pointer',
                          padding: 0,
                          color: idx === displayProviders.length - 1 ? '#30363d' : '#8b949e',
                        }}
                      >
                        <ArrowDown size={14} />
                      </button>
                    </div>
                  )}
                </div>

                {/* Provider details */}
                <div style={{
                  display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))',
                  gap: '4px 16px', marginTop: 8, fontSize: 12,
                }}>
                  {/* Model */}
                  <div style={{ display: 'flex', justifyContent: 'space-between', padding: '3px 0', borderBottom: '1px solid #21262d' }}>
                    <span style={{ color: '#8b949e' }}>Model</span>
                    {editing ? (
                      <input
                        type="text" value={p.model}
                        onChange={(e) => updateDraftField(idx, 'model', e.target.value)}
                        style={{
                          background: '#0d1117', color: '#e6edf3', border: '1px solid #30363d',
                          borderRadius: 4, padding: '1px 6px', fontSize: 12, width: 130, textAlign: 'right',
                        }}
                      />
                    ) : (
                      <span style={{ color: '#e6edf3', fontFamily: 'monospace' }}>{p.model}</span>
                    )}
                  </div>

                  {/* API Key */}
                  {!p.is_local && (
                    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '3px 0', borderBottom: '1px solid #21262d' }}>
                      <span style={{ color: '#8b949e' }}>API Key</span>
                      <span style={{ color: p.api_key_set ? '#22c55e' : '#f59e0b' }}>
                        {p.api_key_set ? 'Set' : `${p.api_key_env} not set`}
                      </span>
                    </div>
                  )}

                  {/* Timeout */}
                  <div style={{ display: 'flex', justifyContent: 'space-between', padding: '3px 0', borderBottom: '1px solid #21262d' }}>
                    <span style={{ color: '#8b949e' }}>Timeout</span>
                    {editing ? (
                      <input
                        type="number" value={p.timeout ?? 60} min={5} max={600}
                        onChange={(e) => updateDraftField(idx, 'timeout', parseInt(e.target.value, 10))}
                        style={{
                          background: '#0d1117', color: '#e6edf3', border: '1px solid #30363d',
                          borderRadius: 4, padding: '1px 6px', fontSize: 12, width: 60, textAlign: 'right',
                        }}
                      />
                    ) : (
                      <span style={{ color: '#e6edf3' }}>{p.timeout ?? 60}s</span>
                    )}
                  </div>

                  {/* Rate limits (cloud only) */}
                  {!p.is_local && (
                    <>
                      <div style={{ display: 'flex', justifyContent: 'space-between', padding: '3px 0', borderBottom: '1px solid #21262d' }}>
                        <span style={{ color: '#8b949e' }}>RPM Limit</span>
                        {editing ? (
                          <input
                            type="number" value={p.rpm_limit ?? 0} min={0}
                            onChange={(e) => updateDraftField(idx, 'rpm_limit', parseInt(e.target.value, 10))}
                            style={{
                              background: '#0d1117', color: '#e6edf3', border: '1px solid #30363d',
                              borderRadius: 4, padding: '1px 6px', fontSize: 12, width: 60, textAlign: 'right',
                            }}
                          />
                        ) : (
                          <span style={{ color: '#e6edf3' }}>
                            {p.live_status?.rpm_current !== undefined
                              ? `${p.live_status.rpm_current}/${p.rpm_limit ?? 0}`
                              : p.rpm_limit ?? 0
                            }
                          </span>
                        )}
                      </div>

                      <div style={{ display: 'flex', justifyContent: 'space-between', padding: '3px 0', borderBottom: '1px solid #21262d' }}>
                        <span style={{ color: '#8b949e' }}>Daily Tokens</span>
                        {editing ? (
                          <input
                            type="number" value={p.daily_token_limit ?? 0} min={0} step={10000}
                            onChange={(e) => updateDraftField(idx, 'daily_token_limit', parseInt(e.target.value, 10))}
                            style={{
                              background: '#0d1117', color: '#e6edf3', border: '1px solid #30363d',
                              borderRadius: 4, padding: '1px 6px', fontSize: 12, width: 90, textAlign: 'right',
                            }}
                          />
                        ) : (
                          <span style={{ color: '#e6edf3' }}>
                            {p.live_status?.daily_tokens !== undefined
                              ? `${(p.live_status.daily_tokens / 1000).toFixed(0)}k / ${p.daily_token_limit ? `${(p.daily_token_limit / 1000).toFixed(0)}k` : 'unlimited'}`
                              : p.daily_token_limit ? `${(p.daily_token_limit / 1000).toFixed(0)}k` : 'unlimited'
                            }
                          </span>
                        )}
                      </div>

                      <div style={{ display: 'flex', justifyContent: 'space-between', padding: '3px 0', borderBottom: '1px solid #21262d' }}>
                        <span style={{ color: '#8b949e' }}>Cooldown</span>
                        {editing ? (
                          <input
                            type="number" value={p.cooldown_seconds ?? 60} min={5}
                            onChange={(e) => updateDraftField(idx, 'cooldown_seconds', parseInt(e.target.value, 10))}
                            style={{
                              background: '#0d1117', color: '#e6edf3', border: '1px solid #30363d',
                              borderRadius: 4, padding: '1px 6px', fontSize: 12, width: 60, textAlign: 'right',
                            }}
                          />
                        ) : (
                          <span style={{ color: '#e6edf3' }}>
                            {p.live_status?.in_cooldown
                              ? `${p.live_status.cooldown_remaining_s}s remaining`
                              : `${p.cooldown_seconds ?? 60}s`
                            }
                          </span>
                        )}
                      </div>
                    </>
                  )}
                </div>
              </div>
            )
          })}

          {isLoading && <div style={{ padding: 12, color: '#8b949e', fontSize: 13 }}>Loading providers...</div>}
        </div>
      )}
    </div>
  )
}


// ── Section ordering ─────────────────────────────────────────────────────────

const SECTION_ORDER = [
  'absolute_rules', 'trading', 'risk', 'rotation', 'fees', 'high_stakes',
  'telegram', 'news', 'fear_greed', 'multi_timeframe',
  'llm', 'analysis', 'logging', 'journal', 'audit', 'health', 'dashboard',
]

// ── Main page ────────────────────────────────────────────────────────────────

export default function Settings() {
  const queryClient = useQueryClient()
  const { data, isLoading, error } = useQuery({ queryKey: ['settings'], queryFn: fetchSettings })
  const { data: presetsData } = useQuery({ queryKey: ['presets'], queryFn: fetchPresets })

  const mutation = useMutation({
    mutationFn: updateSettings,
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['settings'] }) },
  })

  const [presetMsg, setPresetMsg] = useState<string | null>(null)
  const [hoveredPreset, setHoveredPreset] = useState<string | null>(null)

  const handlePreset = async (preset: string) => {
    try {
      await mutation.mutateAsync({ preset })
      setPresetMsg(`${preset.charAt(0).toUpperCase() + preset.slice(1)} preset applied!`)
      setTimeout(() => setPresetMsg(null), 3000)
    } catch (e: any) {
      setPresetMsg(`Error: ${e.message}`)
    }
  }

  const handleSaveSection = async (section: string, updates: Record<string, any>) => {
    // For nested sections (analysis), flatten to sub-section calls
    const settings = data?.settings ?? {}
    const sectionData = settings[section]
    if (sectionData && typeof sectionData === 'object' && !Array.isArray(sectionData)) {
      // Check if this section has nested sub-sections
      const schema = data?.schema?.[section]
      if (schema && 'nested' in schema) {
        // Send sub-section updates separately
        for (const [subName, subUpdates] of Object.entries(updates)) {
          if (typeof subUpdates === 'object' && subUpdates !== null && !Array.isArray(subUpdates)) {
            const original = (sectionData as any)[subName] ?? {}
            const changes: Record<string, any> = {}
            for (const [k, v] of Object.entries(subUpdates as Record<string, any>)) {
              if (JSON.stringify(v) !== JSON.stringify(original[k])) {
                changes[k] = v
              }
            }
            if (Object.keys(changes).length > 0) {
              await mutation.mutateAsync({ section: `${section}.${subName}`, updates: changes })
            }
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

  if (isLoading) return <div style={{ padding: 24, color: '#8b949e' }}>Loading settings…</div>
  if (error) return <div style={{ padding: 24, color: '#ef4444' }}>Failed to load settings: {(error as Error).message}</div>
  if (!data) return null

  const { trading_enabled, section_labels, schema } = data
  const sortedSections = SECTION_ORDER.filter(s => settings[s] !== undefined)

  // Determine which preset to show in the impact panel: hovered (if different) or active
  const panelPresetKey = hoveredPreset && hoveredPreset !== activePreset ? hoveredPreset : activePreset
  const panelPreset = panelPresetKey ? presets[panelPresetKey] : null
  const panelDiff = panelPreset ? buildPresetDiff(settings, panelPreset) : []
  const isShowingComparison = hoveredPreset !== null && hoveredPreset !== activePreset

  return (
    <div style={{ padding: '20px 24px', maxWidth: 900 }}>
      {/* Trading Status Banner */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 12, padding: '12px 16px',
        background: trading_enabled ? '#22c55e18' : '#ef444418',
        border: `1px solid ${trading_enabled ? '#22c55e44' : '#ef444444'}`,
        borderRadius: 8, marginBottom: 16,
      }}>
        <span style={{
          width: 10, height: 10, borderRadius: '50%',
          background: trading_enabled ? '#22c55e' : '#ef4444',
        }} />
        <span style={{ fontWeight: 600, fontSize: 14, color: '#e6edf3' }}>
          Trading is {trading_enabled ? 'ENABLED' : 'DISABLED'}
        </span>
        <div style={{ flex: 1 }} />
        <button
          onClick={() => handlePreset(trading_enabled ? 'disabled' : 'moderate')}
          style={{
            ...btnStyle(trading_enabled ? '#6e7681' : '#238636'),
            padding: '6px 16px', fontSize: 13,
          }}
        >
          {trading_enabled ? 'Disable Trading' : 'Enable Trading'}
        </button>
      </div>

      {/* Presets */}
      <div style={{ marginBottom: 20 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          <span style={{ fontSize: 12, fontWeight: 600, color: '#8b949e', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            Quick Presets
          </span>
          {activePreset ? (
            <span style={{
              fontSize: 10, padding: '2px 8px', borderRadius: 10,
              background: PRESET_CONFIG[activePreset].color + '22',
              color: PRESET_CONFIG[activePreset].color,
              fontWeight: 600,
            }}>
              {PRESET_CONFIG[activePreset].label} active
            </span>
          ) : (
            <span style={{
              fontSize: 10, padding: '2px 8px', borderRadius: 10,
              background: '#8b949e22', color: '#8b949e', fontWeight: 600,
            }}>
              Custom
            </span>
          )}
        </div>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          {Object.entries(PRESET_CONFIG).map(([key, cfg]) => {
            const isActive = key === activePreset
            return (
              <button
                key={key}
                onClick={() => handlePreset(key)}
                onMouseEnter={() => setHoveredPreset(key)}
                onMouseLeave={() => setHoveredPreset(null)}
                style={{
                  display: 'flex', alignItems: 'center', gap: 8, position: 'relative',
                  background: isActive ? cfg.color + '18' : '#161b22',
                  border: isActive ? `2px solid ${cfg.color}` : `1px solid ${cfg.color}44`,
                  borderRadius: 8,
                  padding: isActive ? '9px 15px' : '10px 16px',
                  cursor: isActive ? 'default' : 'pointer',
                  color: '#e6edf3', minWidth: 140, transition: 'all 0.15s',
                  boxShadow: isActive ? `0 0 12px ${cfg.color}30` : 'none',
                }}
              >
                <span style={{ color: cfg.color }}>{cfg.icon}</span>
                <div style={{ textAlign: 'left' }}>
                  <div style={{ fontWeight: 600, fontSize: 13 }}>{cfg.label}</div>
                  <div style={{ fontSize: 11, color: isActive ? cfg.color + 'cc' : '#8b949e' }}>{cfg.desc}</div>
                </div>
                {isActive && (
                  <span style={{
                    position: 'absolute', top: -6, right: -6,
                    width: 18, height: 18, borderRadius: '50%',
                    background: cfg.color, display: 'flex', alignItems: 'center', justifyContent: 'center',
                  }}>
                    <Check size={11} color="#fff" strokeWidth={3} />
                  </span>
                )}
              </button>
            )
          })}
        </div>
        {presetMsg && (
          <div style={{ marginTop: 8, fontSize: 12, color: presetMsg.startsWith('Error') ? '#ef4444' : '#22c55e', display: 'flex', alignItems: 'center', gap: 4 }}>
            <Check size={12} /> {presetMsg}
          </div>
        )}

        {/* Preset Impact Panel */}
        {panelPresetKey && panelDiff.length > 0 && (
          <div style={{
            marginTop: 10, padding: '10px 14px',
            background: '#0d1117', border: `1px solid ${PRESET_CONFIG[panelPresetKey]?.color ?? '#30363d'}33`,
            borderRadius: 8, transition: 'all 0.15s',
          }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={{
                width: 6, height: 6, borderRadius: '50%',
                background: PRESET_CONFIG[panelPresetKey]?.color ?? '#8b949e',
              }} />
              {isShowingComparison
                ? `Switching to ${PRESET_CONFIG[panelPresetKey]?.label} would change:`
                : `${PRESET_CONFIG[panelPresetKey]?.label} preset values:`
              }
            </div>
            <div style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
              gap: '4px 16px',
            }}>
              {panelDiff.map(row => (
                <div key={row.key} style={{
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                  padding: '3px 0', fontSize: 12,
                  borderBottom: '1px solid #161b22',
                }}>
                  <span style={{ color: '#8b949e' }}>{row.label}</span>
                  {isShowingComparison && row.changed ? (
                    <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                      <span style={{ color: '#6e7681', textDecoration: 'line-through', fontSize: 11 }}>
                        {formatFieldValue(row.key, row.current)}
                      </span>
                      <ArrowRight size={10} style={{ color: PRESET_CONFIG[panelPresetKey]?.color ?? '#8b949e' }} />
                      <span style={{ color: PRESET_CONFIG[panelPresetKey]?.color ?? '#e6edf3', fontWeight: 600 }}>
                        {formatFieldValue(row.key, row.target)}
                      </span>
                    </span>
                  ) : (
                    <span style={{
                      color: row.changed ? '#f59e0b' : '#e6edf3',
                      fontWeight: row.changed ? 600 : 400,
                    }}>
                      {formatFieldValue(row.key, row.target)}
                      {!isShowingComparison && row.changed && (
                        <span style={{ fontSize: 10, color: '#f59e0b', marginLeft: 4 }} title="Differs from current">*</span>
                      )}
                    </span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Telegram Safety Legend */}
      <div style={{
        display: 'flex', gap: 16, marginBottom: 16, padding: '8px 12px',
        background: '#161b22', borderRadius: 6, fontSize: 11, color: '#8b949e',
        alignItems: 'center', flexWrap: 'wrap',
      }}>
        <Info size={12} />
        <span>Telegram access tiers:</span>
        {Object.entries(TIER_LABELS).map(([tier, label]) => (
          <span key={tier} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <span style={{ width: 8, height: 8, borderRadius: 2, background: TIER_COLORS[tier] }} />
            {label}
          </span>
        ))}
      </div>

      {/* LLM Provider Chain */}
      <LLMProvidersSection />

      {/* Settings Sections */}
      {sortedSections.map(sectionName => {
        const sectionSchema = schema?.[sectionName]
        const telegramTier = sectionSchema?.telegram_tier ?? 'blocked'
        return (
          <SectionCard
            key={sectionName}
            name={sectionName}
            label={section_labels[sectionName] ?? sectionName}
            values={settings[sectionName] ?? {}}
            schema={sectionSchema}
            telegramTier={telegramTier}
            onSave={handleSaveSection}
          />
        )
      })}
    </div>
  )
}
