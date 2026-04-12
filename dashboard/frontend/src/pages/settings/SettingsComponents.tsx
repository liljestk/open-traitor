import { useState, useMemo, useEffect, useCallback, type ReactNode } from 'react'
import {
  ToggleLeft, ToggleRight, ChevronDown, Save, X,
  AlertTriangle, Check, Zap, Settings2,
  Gauge, Bot, ExternalLink, Lock,
  Maximize2, Minimize2, Bell, TrendingUp, DollarSign, Clock, Calendar,
  ShieldCheck, ShieldOff, Copy, RefreshCw, KeyRound,
} from 'lucide-react'
import type { SectionSchema, FieldSchema, RpmBudget, TwoFAStatus, TwoFASetupResult } from '../../api'
import { fetch2FAStatus, setup2FA, enable2FA, disable2FA, regenerateBackupCodes } from '../../api'
import { useLiveStore, type Density } from '../../store'
import {
  SECTION_ICONS, TIER_COLORS, TIER_LABELS, SECTION_SUMMARY,
  formatKey, getFieldDesc, renderValue, formatSummaryValue,
  btnStyle, codeStyle, inputBase,
} from './settingsData'

/* ═══════════════════════════════════════════════════════════════════════════
   Toast notification
   ═══════════════════════════════════════════════════════════════════════════ */

export function Toast({ message, type, onDismiss }: { message: string; type: 'success' | 'error'; onDismiss: () => void }) {
  useEffect(() => { const t = setTimeout(onDismiss, 4000); return () => clearTimeout(t) }, [onDismiss])
  return (
    <div style={{
      position: 'fixed', bottom: 24, right: 24, zIndex: 9999,
      display: 'flex', alignItems: 'center', gap: 8,
      padding: '12px 20px', borderRadius: 10,
      background: type === 'success' ? '#16291f' : '#2d1318',
      border: `1px solid ${type === 'success' ? '#22c55e55' : '#ef444455'}`,
      color: type === 'success' ? '#4ade80' : '#f87171',
      fontSize: 13, fontWeight: 500,
      boxShadow: `0 8px 32px ${type === 'success' ? '#22c55e20' : '#ef444420'}`,
      animation: 'toastSlideIn 0.3s ease-out',
    }}>
      {type === 'success' ? <Check size={14} /> : <AlertTriangle size={14} />}
      {message}
      <button onClick={onDismiss} style={{
        background: 'none', border: 'none', color: 'inherit', cursor: 'pointer',
        padding: '0 0 0 8px', opacity: 0.6,
      }}><X size={12} /></button>
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════════════════
   Field input renderer
   ═══════════════════════════════════════════════════════════════════════════ */

function FieldInput({ fieldKey, value, schema, onChange }: {
  fieldKey: string; value: unknown; schema: FieldSchema | undefined
  onChange: (key: string, val: unknown) => void
}) {
  const type = schema?.type ?? (typeof value)

  if (type === 'bool' || typeof value === 'boolean') {
    return (
      <button onClick={() => onChange(fieldKey, !value)} style={{
        background: value ? '#22c55e18' : '#6e768118',
        border: `1px solid ${value ? '#22c55e44' : '#30363d'}`,
        borderRadius: 20, cursor: 'pointer', padding: '3px 12px',
        color: value ? '#22c55e' : '#6e7681',
        display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, fontWeight: 500,
        transition: 'all 0.15s',
      }} title={value ? 'Click to disable' : 'Click to enable'}>
        {value ? <ToggleRight size={18} /> : <ToggleLeft size={18} />}
        {value ? 'Enabled' : 'Disabled'}
      </button>
    )
  }

  if (schema?.enum) {
    return (
      <select value={String(value)} onChange={e => onChange(fieldKey, e.target.value)}
        style={{ ...inputBase, minWidth: 120, cursor: 'pointer' }}>
        {schema.enum.map(o => <option key={o} value={o}>{o}</option>)}
      </select>
    )
  }

  if (Array.isArray(value) && value.length > 0 && typeof value[0] === 'object') {
    return null
  }

  if (type === 'list' || Array.isArray(value)) {
    return (
      <input type="text"
        value={Array.isArray(value) ? value.join(', ') : String(value ?? '')}
        onChange={e => onChange(fieldKey, e.target.value.split(',').map(s => s.trim()).filter(Boolean))}
        placeholder="Comma-separated values"
        style={{ ...inputBase, width: '100%', minWidth: 180 }}
      />
    )
  }

  if (type === 'str' || type === 'string') {
    const isLong = typeof value === 'string' && value.length > 60
    if (isLong)
      return (
        <textarea value={String(value ?? '')}
          onChange={e => onChange(fieldKey, e.target.value)} rows={3}
          style={{ ...inputBase, width: '100%', resize: 'vertical', fontFamily: 'inherit', lineHeight: 1.5 }}
        />
      )
    return (
      <input type="text" value={String(value ?? '')}
        onChange={e => onChange(fieldKey, e.target.value)}
        style={{ ...inputBase, minWidth: 180 }}
      />
    )
  }

  // Number (int / float)
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <input type="number" value={value as number ?? ''}
        step={type === 'float' ? 0.01 : 1} min={schema?.min} max={schema?.max}
        onChange={e => {
          const v = e.target.value
          onChange(fieldKey, v === '' ? '' : type === 'int' ? parseInt(v, 10) : parseFloat(v))
        }}
        style={{ ...inputBase, width: 120 }}
      />
      {schema?.min !== undefined && schema?.max !== undefined && (
        <span style={{ fontSize: 10, color: '#484f58', whiteSpace: 'nowrap' }}>
          {schema.min}–{schema.max}
        </span>
      )}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════════════════
   Section Card — collapsible with descriptions, change indicators
   ═══════════════════════════════════════════════════════════════════════════ */

export function SectionCard({ name, label, values, schema, telegramTier, onSave, searchQuery }: {
  name: string; label: string; values: Record<string, unknown>
  schema?: SectionSchema; telegramTier: string
  onSave: (section: string, updates: Record<string, unknown>) => Promise<void>
  searchQuery: string
}) {
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const startEdit = () => { setDraft(JSON.parse(JSON.stringify(values))); setEditing(true); setMsg(null) }
  const cancel = () => { setEditing(false); setMsg(null) }
  const handleChange = (key: string, val: unknown) => setDraft(prev => ({ ...prev, [key]: val }))

  const changedCount = useMemo(() => {
    if (!editing) return 0
    return Object.entries(draft).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(values[k])).length
  }, [editing, draft, values])

  const handleSave = async () => {
    const changes: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(draft))
      if (JSON.stringify(v) !== JSON.stringify(values[k])) changes[k] = v
    if (!Object.keys(changes).length) { setEditing(false); return }
    setSaving(true)
    try {
      await onSave(name, changes)
      setEditing(false)
      setMsg({ ok: true, text: `${Object.keys(changes).length} setting${Object.keys(changes).length > 1 ? 's' : ''} saved & applied live` })
      setTimeout(() => setMsg(null), 4000)
    } catch (e: unknown) {
      setMsg({ ok: false, text: (e instanceof Error ? e.message : String(e)) || 'Save failed' })
    } finally { setSaving(false) }
  }

  const fields = schema?.fields ?? {}
  const fieldEntries = Object.entries(editing ? draft : values)
    .filter(([, v]) => !(Array.isArray(v) && v.length > 0 && typeof v[0] === 'object'))
  const tierColor = TIER_COLORS[telegramTier] ?? '#6e7681'
  const icon = SECTION_ICONS[name]
  const nested = schema?.nested ?? null

  // Filter by search
  const q = searchQuery.toLowerCase()
  const filteredEntries = searchQuery
    ? fieldEntries.filter(([key]) =>
        key.toLowerCase().includes(q) || formatKey(key).toLowerCase().includes(q) ||
        (getFieldDesc(name, key) ?? '').toLowerCase().includes(q)
      )
    : fieldEntries

  // Auto-open on search match
  useEffect(() => {
    if (searchQuery && filteredEntries.length > 0 && !open) setOpen(true)
  }, [searchQuery]) // eslint-disable-line react-hooks/exhaustive-deps

  if (searchQuery && filteredEntries.length === 0 && !nested) return null

  return (
    <div style={{
      background: '#0d1117', border: '1px solid #21262d', borderRadius: 10,
      marginBottom: 10, overflow: 'hidden', transition: 'border-color 0.2s',
      ...(editing ? { borderColor: '#30363d' } : {}),
    }}>
      {/* Header */}
      <button onClick={() => setOpen(!open)} style={{
        width: '100%', background: open ? '#161b2240' : 'none', border: 'none', cursor: 'pointer',
        display: 'flex', alignItems: 'center', gap: 10, padding: '12px 16px',
        color: '#e6edf3', transition: 'background 0.15s',
      }}>
        <span style={{ color: '#8b949e', transition: 'transform 0.2s', transform: open ? 'rotate(0deg)' : 'rotate(-90deg)' }}>
          <ChevronDown size={14} />
        </span>
        <span style={{ color: tierColor + 'cc' }}>{icon}</span>
        <span style={{ fontWeight: 600, fontSize: 14, flexShrink: 0 }}>{label}</span>

        {/* Summary chips — visible only when collapsed */}
        {!open && !editing && SECTION_SUMMARY[name] && (
          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', flex: 1, alignItems: 'center' }}>
            {SECTION_SUMMARY[name].map(({ key, label: chipLabel }) => {
              const val = values[key]
              if (val === undefined) return null
              return (
                <span key={key} style={{
                  fontSize: 10, padding: '2px 7px', borderRadius: 8,
                  background: '#161b22', color: '#8b949e', border: '1px solid #21262d',
                  display: 'flex', alignItems: 'center', gap: 3, whiteSpace: 'nowrap',
                }}>
                  <span style={{ color: '#484f58' }}>{chipLabel}:</span>
                  <span style={{ color: '#c9d1d9', fontWeight: 500 }}>{formatSummaryValue(key, val)}</span>
                </span>
              )
            })}
          </div>
        )}
        {(open || editing) && <div style={{ flex: 1 }} />}

        {/* Live-reload badge */}
        <span style={{
          fontSize: 9, padding: '2px 7px', borderRadius: 10,
          background: '#22c55e15', color: '#22c55e', fontWeight: 600,
          display: 'flex', alignItems: 'center', gap: 3, border: '1px solid #22c55e22',
        }}><Zap size={8} /> Live reload</span>

        {/* Telegram tier */}
        <span style={{
          fontSize: 9, padding: '2px 7px', borderRadius: 10,
          background: tierColor + '15', color: tierColor, fontWeight: 600,
          border: `1px solid ${tierColor}22`,
        }}>{TIER_LABELS[telegramTier] ?? telegramTier}</span>

        {msg && (
          <span style={{ fontSize: 11, color: msg.ok ? '#22c55e' : '#ef4444', display: 'flex', alignItems: 'center', gap: 3 }}>
            {msg.ok ? <Check size={12} /> : <AlertTriangle size={12} />} {msg.text}
          </span>
        )}
      </button>

      {/* Body */}
      {open && (
        <div style={{ padding: '0 16px 16px', borderTop: '1px solid #21262d' }}>
          {/* Action bar */}
          <div style={{ display: 'flex', gap: 8, padding: '12px 0 8px', justifyContent: 'flex-end', alignItems: 'center' }}>
            {editing && changedCount > 0 && (
              <span style={{ fontSize: 11, color: '#f59e0b', marginRight: 'auto', display: 'flex', alignItems: 'center', gap: 4 }}>
                <AlertTriangle size={11} /> {changedCount} unsaved change{changedCount > 1 ? 's' : ''}
              </span>
            )}
            {!editing ? (
              <button onClick={startEdit} style={{ ...btnStyle('#21262d'), borderColor: '#30363d' }}>
                <Settings2 size={12} /> Edit settings
              </button>
            ) : (
              <>
                <button onClick={cancel} style={btnStyle('#21262d')}><X size={12} /> Cancel</button>
                <button onClick={handleSave} disabled={saving || changedCount === 0}
                  style={{ ...btnStyle(changedCount > 0 ? '#238636' : '#21262d'), opacity: changedCount === 0 ? 0.5 : 1 }}>
                  <Save size={12} /> {saving ? 'Saving…' : `Save & Apply${changedCount > 0 ? ` (${changedCount})` : ''}`}
                </button>
              </>
            )}
          </div>

          {/* Fields (flat section) */}
          {!nested ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              {filteredEntries.map(([key, val]) => {
                const desc = getFieldDesc(name, key)
                const isChanged = editing && JSON.stringify(draft[key]) !== JSON.stringify(values[key])
                return (
                  <div key={key} style={{
                    display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, padding: '8px 8px',
                    borderRadius: 6, background: isChanged ? '#f59e0b08' : 'transparent',
                    borderLeft: isChanged ? '2px solid #f59e0b' : '2px solid transparent',
                    transition: 'all 0.15s',
                  }}>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 2, justifyContent: 'center' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <span style={{ fontSize: 13, color: '#e6edf3', fontWeight: 500 }}>{formatKey(key)}</span>
                        {fields[key]?.min !== undefined && (
                          <span style={{ fontSize: 9, color: '#484f58', padding: '1px 5px', background: '#161b22', borderRadius: 4 }}>
                            {fields[key].min}–{fields[key].max}
                          </span>
                        )}
                      </div>
                      {desc && <span style={{ fontSize: 11, color: '#6e7681', lineHeight: 1.3 }}>{desc}</span>}
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end' }}>
                      {editing
                        ? <FieldInput fieldKey={key} value={draft[key] ?? val} schema={fields[key]} onChange={handleChange} />
                        : <span style={{ fontSize: 13, color: '#c9d1d9' }}>{renderValue(val)}</span>}
                    </div>
                  </div>
                )
              })}
            </div>
          ) : (
            /* Nested sections (e.g. analysis → technical + sentiment) */
            Object.entries(values as Record<string, Record<string, unknown>>).map(([subName, subValues]) => {
              const subFields = nested[subName]?.fields ?? {}
              return (
                <div key={subName} style={{ marginBottom: 14 }}>
                  <div style={{
                    fontSize: 11, fontWeight: 600, color: '#8b949e', textTransform: 'uppercase',
                    letterSpacing: '0.06em', padding: '10px 0 6px',
                    borderBottom: '1px solid #161b22', marginBottom: 4,
                  }}>{formatKey(subName)}</div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                    {Object.entries(subValues).map(([key, val]) => {
                      const desc = getFieldDesc(`${name}.${subName}`, key)
                      const isChanged = editing && JSON.stringify((draft[subName] as Record<string, unknown>)?.[key]) !== JSON.stringify(val)
                      return (
                        <div key={key} style={{
                          display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8,
                          padding: '8px 8px 8px 16px', borderRadius: 6,
                          background: isChanged ? '#f59e0b08' : 'transparent',
                          borderLeft: isChanged ? '2px solid #f59e0b' : '2px solid transparent',
                        }}>
                          <div style={{ display: 'flex', flexDirection: 'column', gap: 2, justifyContent: 'center' }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                              <span style={{ fontSize: 13, color: '#e6edf3', fontWeight: 500 }}>{formatKey(key)}</span>
                              {subFields[key]?.min !== undefined && (
                                <span style={{ fontSize: 9, color: '#484f58', padding: '1px 5px', background: '#161b22', borderRadius: 4 }}>
                                  {subFields[key].min}–{subFields[key].max}
                                </span>
                              )}
                            </div>
                            {desc && <span style={{ fontSize: 11, color: '#6e7681', lineHeight: 1.3 }}>{desc}</span>}
                          </div>
                          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end' }}>
                            {editing
                              ? <FieldInput fieldKey={key}
                                  value={editing ? (draft[subName] as Record<string, unknown>)?.[key] ?? val : val}
                                  schema={subFields[key]}
                                  onChange={(k, v) => setDraft(prev => ({
                                    ...prev,
                                    [subName]: { ...(prev[subName] as Record<string, unknown> ?? subValues), [k]: v },
                                  }))}
                                />
                              : <span style={{ fontSize: 13, color: '#c9d1d9' }}>{renderValue(val)}</span>}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </div>
              )
            })
          )}
        </div>
      )}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════════════════
   RPM Budget Card — shows entity tracking capacity based on LLM RPM limits
   ═══════════════════════════════════════════════════════════════════════════ */

export function RpmBudgetCard({ rpm_budget, current_pairs }: { rpm_budget: RpmBudget; current_pairs: number }) {
  const effective = rpm_budget.effective_max
  const usagePct = effective > 0 ? Math.round((current_pairs / effective) * 100) : 0
  const isAtLimit = current_pairs >= effective
  const isOverLimit = current_pairs > effective
  const isLocal = rpm_budget.provider === 'local-only'
  const barColor = isOverLimit ? '#ef4444' : isAtLimit ? '#f59e0b' : '#22c55e'

  return (
    <div style={{
      padding: '16px 20px', background: '#0d111788', border: `1px solid ${isOverLimit ? '#ef444433' : '#21262d'}`,
      borderRadius: 10, marginBottom: 16,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Gauge size={16} style={{ color: barColor }} />
          <span style={{ fontWeight: 700, fontSize: 14, color: '#e6edf3' }}>RPM Entity Budget</span>
          <span style={{
            fontSize: 10, padding: '2px 8px', borderRadius: 10,
            background: `${barColor}18`, color: barColor, fontWeight: 600,
            border: `1px solid ${barColor}22`,
          }}>
            {current_pairs} / {effective} pairs
          </span>
        </div>
      </div>

      {/* Progress bar */}
      <div style={{ height: 6, background: '#161b22', borderRadius: 3, overflow: 'hidden', marginBottom: 12 }}>
        <div style={{
          height: '100%', borderRadius: 3, background: barColor,
          width: `${Math.min(usagePct, 100)}%`, transition: 'width 0.3s ease',
        }} />
      </div>

      {/* Stats grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, fontSize: 12 }}>
        <div>
          <div style={{ color: '#6e7681', marginBottom: 2 }}>Provider</div>
          <div style={{ color: '#e6edf3', fontWeight: 600 }}>{rpm_budget.provider}</div>
        </div>
        <div>
          <div style={{ color: '#6e7681', marginBottom: 2 }}>RPM Limit</div>
          <div style={{ color: '#e6edf3', fontWeight: 600 }}>{isLocal ? '∞ (local)' : rpm_budget.rpm}</div>
        </div>
        <div>
          <div style={{ color: '#6e7681', marginBottom: 2 }}>Cycle Interval</div>
          <div style={{ color: '#e6edf3', fontWeight: 600 }}>{rpm_budget.interval}s</div>
        </div>
        <div>
          <div style={{ color: '#6e7681', marginBottom: 2 }}>Max from RPM</div>
          <div style={{ color: '#e6edf3', fontWeight: 600 }}>{rpm_budget.max_entities}</div>
        </div>
      </div>

      {/* Explanation */}
      <div style={{ marginTop: 12, fontSize: 11, color: '#6e7681', lineHeight: 1.5 }}>
        {isLocal ? (
          <>Local models (Ollama) have no RPM limit — entity cap is set to <strong style={{ color: '#8b949e' }}>max_active_pairs ({rpm_budget.configured_max})</strong> from your settings.</>
        ) : (
          <>
            Your <strong style={{ color: '#8b949e' }}>{rpm_budget.provider}</strong> provider allows{' '}
            <strong style={{ color: '#8b949e' }}>{rpm_budget.rpm} requests/min</strong>.
            With a <strong style={{ color: '#8b949e' }}>{rpm_budget.interval}s</strong> cycle,
            that's ~{rpm_budget.available_per_cycle} calls/cycle — minus {rpm_budget.overhead} overhead = budget for{' '}
            <strong style={{ color: '#8b949e' }}>{rpm_budget.max_entities} entities</strong> (2 LLM calls each).
            {rpm_budget.configured_max < rpm_budget.max_entities && (
              <> Your <strong style={{ color: '#8b949e' }}>max_active_pairs = {rpm_budget.configured_max}</strong> setting further limits this to <strong style={{ color: '#22c55e' }}>{effective}</strong>.</>
            )}
            {rpm_budget.configured_max > rpm_budget.max_entities && (
              <> Your <strong style={{ color: '#f59e0b' }}>max_active_pairs = {rpm_budget.configured_max}</strong> exceeds the RPM budget, so the system auto-clamps to <strong style={{ color: '#f59e0b' }}>{effective}</strong>.</>
            )}
          </>
        )}
      </div>

      {isOverLimit && (
        <div style={{
          marginTop: 10, padding: '8px 12px', background: '#ef444415',
          border: '1px solid #ef444433', borderRadius: 8,
          fontSize: 12, color: '#f87171', display: 'flex', alignItems: 'center', gap: 8,
        }}>
          <AlertTriangle size={14} />
          <span>You have <strong>{current_pairs - effective} pair(s)</strong> over the limit. The agent will only actively trade the top {effective}. Remove excess pairs or upgrade your LLM provider for more capacity.</span>
        </div>
      )}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════════════════
   Telegram Setup Guide
   ═══════════════════════════════════════════════════════════════════════════ */

export function TelegramSetupGuide() {
  const [open, setOpen] = useState(false)

  const Step = ({ n, title, children, done }: { n: number | string; title: string; children: ReactNode; done?: boolean }) => (
    <div style={{ marginBottom: 20 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
        <span style={{
          width: 26, height: 26, borderRadius: '50%',
          background: done ? '#22c55e22' : '#58a6ff22',
          color: done ? '#22c55e' : '#58a6ff',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 12, fontWeight: 700, flexShrink: 0,
        }}>{n}</span>
        <span style={{ fontWeight: 600, fontSize: 14, color: '#e6edf3' }}>{title}</span>
      </div>
      <div style={{ paddingLeft: 36, color: '#8b949e', fontSize: 13, lineHeight: 1.7 }}>{children}</div>
    </div>
  )

  return (
    <div style={{ background: '#0d1117', border: '1px solid #21262d', borderRadius: 10, marginBottom: 10, overflow: 'hidden' }}>
      <button onClick={() => setOpen(!open)} style={{
        width: '100%', background: open ? '#161b2240' : 'none', border: 'none', cursor: 'pointer',
        display: 'flex', alignItems: 'center', gap: 10, padding: '12px 16px', color: '#e6edf3', transition: 'background 0.15s',
      }}>
        <span style={{ color: '#8b949e', transition: 'transform 0.2s', transform: open ? 'rotate(0deg)' : 'rotate(-90deg)' }}>
          <ChevronDown size={14} />
        </span>
        <Bot size={15} style={{ color: '#58a6ff' }} />
        <span style={{ fontWeight: 600, fontSize: 14, flex: 1, textAlign: 'left' }}>Telegram Bot Setup Guide</span>
        <span style={{
          fontSize: 9, padding: '2px 7px', borderRadius: 10,
          background: '#58a6ff15', color: '#58a6ff', fontWeight: 600, border: '1px solid #58a6ff22',
        }}>Tutorial</span>
      </button>

      {open && (
        <div style={{ padding: '0 16px 20px', borderTop: '1px solid #21262d' }}>
          <div style={{ padding: '16px 0' }}>

            <Step n={1} title="Create a Bot with BotFather">
              <p style={{ margin: '0 0 6px' }}>
                Open Telegram and search for{' '}
                <a href="https://t.me/BotFather" target="_blank" rel="noopener noreferrer"
                  style={{ color: '#58a6ff', textDecoration: 'none' }}>
                  @BotFather <ExternalLink size={10} style={{ display: 'inline', verticalAlign: 'middle' }} />
                </a>
              </p>
              <p style={{ margin: '0 0 6px' }}>Send <code style={codeStyle}>/newbot</code> and follow the prompts:</p>
              <ul style={{ margin: '0 0 6px', paddingLeft: 20 }}>
                <li>Choose a display name (e.g. &quot;OpenTraitor Bot&quot;)</li>
                <li>Choose a username ending in &quot;bot&quot; (e.g. &quot;opentraitor_bot&quot;)</li>
              </ul>
              <p style={{ margin: 0 }}>
                BotFather will give you a <strong style={{ color: '#c9d1d9' }}>Bot Token</strong> like{' '}
                <code style={codeStyle}>123456:ABC-DEF1234ghIkl</code> — copy it.
              </p>
            </Step>

            <Step n={2} title="Get Your Chat ID">
              <p style={{ margin: '0 0 6px' }}>Send any message to your new bot, then visit:</p>
              <code style={{ ...codeStyle, display: 'block', padding: '8px 12px', marginBottom: 6 }}>
                https://api.telegram.org/bot&lt;YOUR_TOKEN&gt;/getUpdates
              </code>
              <p style={{ margin: '0 0 6px' }}>
                Look for <code style={codeStyle}>&quot;chat&quot;: {'{'}&quot;id&quot;: 123456789{'}'}</code> — that number is your <strong style={{ color: '#c9d1d9' }}>Chat ID</strong>.
              </p>
              <p style={{ margin: 0 }}>For a group chat, add the bot to the group and use the group&apos;s negative ID.</p>
            </Step>

            <Step n={3} title="Configure Environment Variables">
              <p style={{ margin: '0 0 8px' }}>Add these to your <code style={codeStyle}>.env</code> file or environment:</p>
              <div style={{
                background: '#161b22', border: '1px solid #21262d', borderRadius: 8,
                padding: '10px 14px', fontFamily: 'var(--font-mono, monospace)', fontSize: 12, lineHeight: 1.8,
              }}>
                <div><span style={{ color: '#79c0ff' }}>TELEGRAM_BOT_TOKEN</span>=<span style={{ color: '#a5d6ff' }}>your-bot-token-here</span></div>
                <div><span style={{ color: '#79c0ff' }}>TELEGRAM_CHAT_ID</span>=<span style={{ color: '#a5d6ff' }}>your-chat-id-here</span></div>
                <div><span style={{ color: '#79c0ff' }}>TELEGRAM_AUTHORIZED_USERS</span>=<span style={{ color: '#a5d6ff' }}>your-user-id</span></div>
              </div>
            </Step>

            <Step n={4} title="Get Your User ID (for Authorization)">
              <p style={{ margin: '0 0 6px' }}>
                Send a message to{' '}
                <a href="https://t.me/userinfobot" target="_blank" rel="noopener noreferrer"
                  style={{ color: '#58a6ff', textDecoration: 'none' }}>
                  @userinfobot <ExternalLink size={10} style={{ display: 'inline', verticalAlign: 'middle' }} />
                </a>
                {' '}to get your numeric user ID.
              </p>
              <p style={{ margin: 0 }}>
                Add this to <code style={codeStyle}>TELEGRAM_AUTHORIZED_USERS</code>. Multiple users: comma-separated.
                Only authorized users can send commands —{' '}
                <strong style={{ color: '#f59e0b' }}>this is mandatory for security</strong>.
              </p>
            </Step>

            <Step n="✓" title="Configure Notifications Below" done>
              <p style={{ margin: '0 0 6px' }}>
                Once set up, use the <strong style={{ color: '#c9d1d9' }}>Telegram section</strong> below to configure:
              </p>
              <ul style={{ margin: 0, paddingLeft: 20 }}>
                <li>Trade notifications (get alerted on every buy/sell)</li>
                <li>Daily summaries (scheduled performance reports)</li>
                <li>Signal alerts (high-confidence signal notifications)</li>
                <li>Status update frequency</li>
              </ul>
            </Step>

            {/* Security reminder */}
            <div style={{
              marginTop: 4, padding: '10px 14px', borderRadius: 8,
              background: '#f59e0b10', border: '1px solid #f59e0b22',
              display: 'flex', gap: 10, alignItems: 'flex-start',
            }}>
              <Lock size={14} style={{ color: '#f59e0b', flexShrink: 0, marginTop: 2 }} />
              <div style={{ fontSize: 12, color: '#f59e0b', lineHeight: 1.5 }}>
                <strong>Security Note:</strong> The <code style={{ ...codeStyle, color: '#f59e0b' }}>TELEGRAM_AUTHORIZED_USERS</code> environment
                variable is mandatory. Without it, the bot will reject all commands. Never add fallback auth paths.
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════════════════
   Density Toggle
   ═══════════════════════════════════════════════════════════════════════════ */

export function DensityToggle() {
  const density = useLiveStore(s => s.density)
  const setDensity = useLiveStore(s => s.setDensity)

  return (
    <div style={{
      background: '#0d1117', border: '1px solid #21262d', borderRadius: 10, padding: '16px 20px',
    }}>
      <div style={{ fontSize: 12, fontWeight: 600, color: '#8b949e', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 12 }}>
        UI Density
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        {([
          ['comfortable', 'Comfortable', <Maximize2 key="c" size={14} />, 'More spacious layout with larger elements'],
          ['compact', 'Compact', <Minimize2 key="m" size={14} />, 'Tighter spacing, more data visible at once'],
        ] as const).map(([val, label, icon, desc]) => (
          <button key={val} onClick={() => setDensity(val as Density)} style={{
            display: 'flex', alignItems: 'center', gap: 8, flex: 1,
            padding: '10px 16px', fontSize: 13, fontWeight: 500, borderRadius: 8,
            border: density === val ? '1px solid #22c55e55' : '1px solid #30363d',
            background: density === val ? '#22c55e12' : '#161b22',
            color: density === val ? '#22c55e' : '#8b949e',
            cursor: 'pointer', transition: 'all 0.15s', textAlign: 'left',
          }}>
            {icon}
            <div>
              <div style={{ fontWeight: 600 }}>{label}</div>
              <div style={{ fontSize: 11, opacity: 0.7, marginTop: 2 }}>{desc}</div>
            </div>
          </button>
        ))}
      </div>
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════════════════
   Telegram Notifications Card — grouped, intuitive notification config
   ═══════════════════════════════════════════════════════════════════════════ */

type TgValues = Record<string, unknown>

function Toggle({ value, onChange }: { value: boolean; onChange: (v: boolean) => void }) {
  return (
    <button onClick={() => onChange(!value)} style={{
      background: value ? '#22c55e18' : '#6e768118',
      border: `1px solid ${value ? '#22c55e44' : '#30363d'}`,
      borderRadius: 20, cursor: 'pointer', padding: '3px 10px',
      color: value ? '#22c55e' : '#6e7681',
      display: 'flex', alignItems: 'center', gap: 5, fontSize: 12, fontWeight: 500,
      transition: 'all 0.15s', flexShrink: 0,
    }}>
      {value ? <ToggleRight size={16} /> : <ToggleLeft size={16} />}
      {value ? 'On' : 'Off'}
    </button>
  )
}

function NumInput({ value, onChange, min, max, step = 1, suffix }: {
  value: number; onChange: (v: number) => void
  min?: number; max?: number; step?: number; suffix?: string
}) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
      <input type="number" value={value} min={min} max={max} step={step}
        onChange={e => {
          const v = parseFloat(e.target.value)
          if (!isNaN(v)) onChange(v)
        }}
        style={{
          ...inputBase, width: 72, textAlign: 'right', padding: '4px 8px', fontSize: 13,
        }}
      />
      {suffix && <span style={{ fontSize: 11, color: '#6e7681', whiteSpace: 'nowrap' }}>{suffix}</span>}
    </div>
  )
}

interface NotifGroup {
  icon: ReactNode
  label: string
  desc: string
  rows: ReactNode[]
}

function NotifGroupCard({ icon, label, desc, rows }: NotifGroup) {
  return (
    <div style={{
      background: '#161b22', border: '1px solid #21262d', borderRadius: 8, overflow: 'hidden',
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px',
        borderBottom: '1px solid #21262d', background: '#0d111780',
      }}>
        <span style={{ color: '#58a6ff' }}>{icon}</span>
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, color: '#e6edf3' }}>{label}</div>
          {desc && <div style={{ fontSize: 11, color: '#6e7681', marginTop: 1 }}>{desc}</div>}
        </div>
      </div>
      <div style={{ padding: '4px 0' }}>
        {rows}
      </div>
    </div>
  )
}

function NotifRow({ label, desc, right, changed }: {
  label: string; desc?: string; right: ReactNode; changed?: boolean
}) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12,
      padding: '8px 14px',
      borderLeft: `3px solid ${changed ? '#f59e0b' : 'transparent'}`,
      background: changed ? '#f59e0b06' : 'transparent',
      transition: 'all 0.15s',
    }}>
      <div>
        <div style={{ fontSize: 13, color: '#c9d1d9', fontWeight: 500 }}>{label}</div>
        {desc && <div style={{ fontSize: 11, color: '#6e7681', marginTop: 2, lineHeight: 1.4 }}>{desc}</div>}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
        {right}
      </div>
    </div>
  )
}

function AlwaysOnBadge({ label }: { label: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <span style={{
        fontSize: 11, padding: '2px 8px', borderRadius: 10,
        background: '#ef444415', color: '#f87171', fontWeight: 500, border: '1px solid #ef444430',
      }}>Always On</span>
      <span style={{ fontSize: 11, color: '#6e7681' }}>{label}</span>
    </div>
  )
}

export function TelegramNotificationsCard({ values, onSave, searchQuery }: {
  values: TgValues
  onSave: (section: string, updates: TgValues) => Promise<void>
  searchQuery: string
}) {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState<TgValues>({})
  const [editing, setEditing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const startEdit = () => { setDraft(JSON.parse(JSON.stringify(values))); setEditing(true); setMsg(null) }
  const cancel = () => { setEditing(false); setMsg(null) }
  const cur = editing ? draft : values

  const get = <T,>(key: string, def: T): T => (cur[key] as T) ?? def
  const set = (key: string, val: unknown) => setDraft(prev => ({ ...prev, [key]: val }))

  const changedKeys = useMemo(() => {
    if (!editing) return []
    return Object.entries(draft).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(values[k])).map(([k]) => k)
  }, [editing, draft, values])
  const changedCount = changedKeys.length

  const isChanged = (key: string) => editing && changedKeys.includes(key)

  const handleSave = async () => {
    const changes: TgValues = {}
    for (const k of changedKeys) changes[k] = draft[k]
    if (!Object.keys(changes).length) { setEditing(false); return }
    setSaving(true)
    try {
      await onSave('telegram', changes)
      setEditing(false)
      setMsg({ ok: true, text: `${changedCount} setting${changedCount > 1 ? 's' : ''} saved & applied live` })
      setTimeout(() => setMsg(null), 4000)
    } catch (e: unknown) {
      setMsg({ ok: false, text: (e instanceof Error ? e.message : String(e)) || 'Save failed' })
    } finally { setSaving(false) }
  }

  // Auto-open when search matches any telegram field
  const q = searchQuery.toLowerCase()
  const hasSearchMatch = q && [
    'notify_on_trade', 'notify_on_signal', 'notify_on_signal_confidence',
    'notify_on_big_win', 'big_win_threshold', 'notify_on_big_loss', 'big_loss_threshold',
    'notify_on_price_move', 'price_move_threshold_pct', 'price_move_cooldown_minutes',
    'notify_morning_plan', 'notify_evening_summary', 'notify_periodic_update',
    'status_update_interval', 'daily_summary', 'daily_summary_hour',
    'telegram', 'trade', 'signal', 'price', 'win', 'loss', 'morning', 'evening', 'summary', 'notification',
  ].some(k => k.includes(q))

  useEffect(() => {
    if (hasSearchMatch && !open) setOpen(true)
  }, [searchQuery]) // eslint-disable-line react-hooks/exhaustive-deps

  if (searchQuery && !hasSearchMatch) return null

  // Summary chips shown when collapsed
  const summaryItems = [
    { key: 'notify_on_trade',     label: 'Trades',      val: get<boolean>('notify_on_trade', true) },
    { key: 'notify_on_signal',    label: 'Signals',     val: get<boolean>('notify_on_signal', true) },
    { key: 'notify_on_price_move',label: 'Price Moves', val: get<boolean>('notify_on_price_move', true) },
    { key: 'notify_morning_plan', label: 'Morning',     val: get<boolean>('notify_morning_plan', true) },
  ]

  return (
    <div style={{
      background: '#0d1117', border: '1px solid #21262d', borderRadius: 10,
      marginBottom: 10, overflow: 'hidden',
      ...(editing ? { borderColor: '#30363d' } : {}),
    }}>
      {/* Header */}
      <button onClick={() => setOpen(!open)} style={{
        width: '100%', background: open ? '#161b2240' : 'none', border: 'none', cursor: 'pointer',
        display: 'flex', alignItems: 'center', gap: 10, padding: '12px 16px',
        color: '#e6edf3', transition: 'background 0.15s',
      }}>
        <span style={{ color: '#8b949e', transition: 'transform 0.2s', transform: open ? 'rotate(0deg)' : 'rotate(-90deg)' }}>
          <ChevronDown size={14} />
        </span>
        <span style={{ color: '#22c55ecc' }}><Bell size={15} /></span>
        <span style={{ fontWeight: 600, fontSize: 14, flexShrink: 0 }}>Telegram Notifications</span>

        {/* Summary chips when collapsed */}
        {!open && !editing && (
          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', flex: 1, alignItems: 'center' }}>
            {summaryItems.map(({ key, label, val }) => (
              <span key={key} style={{
                fontSize: 10, padding: '2px 7px', borderRadius: 8,
                background: val ? '#22c55e12' : '#ef444412',
                color: val ? '#4ade80' : '#f87171', border: `1px solid ${val ? '#22c55e22' : '#ef444422'}`,
                display: 'flex', alignItems: 'center', gap: 3, whiteSpace: 'nowrap',
              }}>
                <span style={{ color: '#484f58' }}>{label}:</span>
                <span style={{ fontWeight: 600 }}>{val ? 'On' : 'Off'}</span>
              </span>
            ))}
          </div>
        )}
        {(open || editing) && <div style={{ flex: 1 }} />}

        <span style={{
          fontSize: 9, padding: '2px 7px', borderRadius: 10,
          background: '#22c55e15', color: '#22c55e', fontWeight: 600,
          display: 'flex', alignItems: 'center', gap: 3, border: '1px solid #22c55e22',
        }}><Zap size={8} /> Live reload</span>

        <span style={{
          fontSize: 9, padding: '2px 7px', borderRadius: 10,
          background: '#22c55e15', color: '#22c55e', fontWeight: 600, border: '1px solid #22c55e22',
        }}>Telegram Safe</span>

        {msg && (
          <span style={{ fontSize: 11, color: msg.ok ? '#22c55e' : '#ef4444', display: 'flex', alignItems: 'center', gap: 3 }}>
            {msg.ok ? <Check size={12} /> : <AlertTriangle size={12} />} {msg.text}
          </span>
        )}
      </button>

      {/* Body */}
      {open && (
        <div style={{ padding: '0 16px 16px', borderTop: '1px solid #21262d' }}>

          {/* Action bar */}
          <div style={{ display: 'flex', gap: 8, padding: '12px 0 12px', justifyContent: 'flex-end', alignItems: 'center' }}>
            {editing && changedCount > 0 && (
              <span style={{ fontSize: 11, color: '#f59e0b', marginRight: 'auto', display: 'flex', alignItems: 'center', gap: 4 }}>
                <AlertTriangle size={11} /> {changedCount} unsaved change{changedCount > 1 ? 's' : ''}
              </span>
            )}
            {!editing ? (
              <button onClick={startEdit} style={{ ...btnStyle('#21262d'), borderColor: '#30363d' }}>
                <Settings2 size={12} /> Edit notifications
              </button>
            ) : (
              <>
                <button onClick={cancel} style={btnStyle('#21262d')}><X size={12} /> Cancel</button>
                <button onClick={handleSave} disabled={saving || changedCount === 0}
                  style={{ ...btnStyle(changedCount > 0 ? '#238636' : '#21262d'), opacity: changedCount === 0 ? 0.5 : 1 }}>
                  <Save size={12} /> {saving ? 'Saving…' : `Save & Apply${changedCount > 0 ? ` (${changedCount})` : ''}`}
                </button>
              </>
            )}
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>

            {/* ── Group 1: Trades & Signals ── */}
            <NotifGroupCard
              icon={<TrendingUp size={14} />}
              label="Trades & Signals"
              desc="Notifications fired immediately when the bot executes a trade or detects a signal"
              rows={[
                <NotifRow key="trade"
                  label="Trade Executed"
                  desc="Message sent whenever a buy or sell order is filled"
                  changed={isChanged('notify_on_trade')}
                  right={editing
                    ? <Toggle value={get<boolean>('notify_on_trade', true)} onChange={v => set('notify_on_trade', v)} />
                    : <span style={{ fontSize: 12, color: get<boolean>('notify_on_trade', true) ? '#22c55e' : '#6e7681' }}>
                        {get<boolean>('notify_on_trade', true) ? '✓ On' : '✗ Off'}
                      </span>
                  }
                />,
                <NotifRow key="signal"
                  label="Signal Detected"
                  desc="Message sent when an AI signal reaches the minimum confidence threshold"
                  changed={isChanged('notify_on_signal') || isChanged('notify_on_signal_confidence')}
                  right={
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      {editing && get<boolean>('notify_on_signal', true) && (
                        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                          <span style={{ fontSize: 11, color: '#6e7681', whiteSpace: 'nowrap' }}>min confidence</span>
                          <NumInput
                            value={Math.round(get<number>('notify_on_signal_confidence', 0.65) * 100)}
                            onChange={v => set('notify_on_signal_confidence', v / 100)}
                            min={0} max={100} step={5} suffix="%"
                          />
                        </div>
                      )}
                      {!editing && (
                        <span style={{ fontSize: 11, color: '#6e7681' }}>
                          ≥{Math.round(get<number>('notify_on_signal_confidence', 0.65) * 100)}%
                        </span>
                      )}
                      {editing
                        ? <Toggle value={get<boolean>('notify_on_signal', true)} onChange={v => set('notify_on_signal', v)} />
                        : <span style={{ fontSize: 12, color: get<boolean>('notify_on_signal', true) ? '#22c55e' : '#6e7681' }}>
                            {get<boolean>('notify_on_signal', true) ? '✓ On' : '✗ Off'}
                          </span>
                      }
                    </div>
                  }
                />,
                <NotifRow key="approval"
                  label="Approval Requests"
                  desc="Inline keyboard sent when a trade requires manual approval"
                  right={<AlwaysOnBadge label="cannot be disabled" />}
                />,
                <NotifRow key="circuit"
                  label="Circuit Breaker / Emergency"
                  desc="🚨 Alerts for circuit breaker trips and emergency stops"
                  right={<AlwaysOnBadge label="safety-critical" />}
                />,
              ]}
            />

            {/* ── Group 2: Win / Loss Highlights ── */}
            <NotifGroupCard
              icon={<DollarSign size={14} />}
              label="Win / Loss Highlights"
              desc="Celebratory or analytical messages triggered by significant trade results"
              rows={[
                <NotifRow key="bigwin"
                  label="Big Win Alert"
                  desc="Sent when a single trade's profit exceeds the threshold"
                  changed={isChanged('notify_on_big_win') || isChanged('big_win_threshold')}
                  right={
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      {editing && get<boolean>('notify_on_big_win', true) && (
                        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                          <span style={{ fontSize: 11, color: '#6e7681' }}>above</span>
                          <NumInput
                            value={get<number>('big_win_threshold', 50)}
                            onChange={v => set('big_win_threshold', v)}
                            min={1} max={100000} step={10} suffix="$"
                          />
                        </div>
                      )}
                      {!editing && (
                        <span style={{ fontSize: 11, color: '#6e7681' }}>
                          &gt;${get<number>('big_win_threshold', 50)}
                        </span>
                      )}
                      {editing
                        ? <Toggle value={get<boolean>('notify_on_big_win', true)} onChange={v => set('notify_on_big_win', v)} />
                        : <span style={{ fontSize: 12, color: get<boolean>('notify_on_big_win', true) ? '#22c55e' : '#6e7681' }}>
                            {get<boolean>('notify_on_big_win', true) ? '✓ On' : '✗ Off'}
                          </span>
                      }
                    </div>
                  }
                />,
                <NotifRow key="bigloss"
                  label="Big Loss Alert"
                  desc="Sent when a single trade's loss exceeds the threshold"
                  changed={isChanged('notify_on_big_loss') || isChanged('big_loss_threshold')}
                  right={
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      {editing && get<boolean>('notify_on_big_loss', true) && (
                        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                          <span style={{ fontSize: 11, color: '#6e7681' }}>above</span>
                          <NumInput
                            value={get<number>('big_loss_threshold', 50)}
                            onChange={v => set('big_loss_threshold', v)}
                            min={1} max={100000} step={10} suffix="$"
                          />
                        </div>
                      )}
                      {!editing && (
                        <span style={{ fontSize: 11, color: '#6e7681' }}>
                          &gt;${get<number>('big_loss_threshold', 50)}
                        </span>
                      )}
                      {editing
                        ? <Toggle value={get<boolean>('notify_on_big_loss', true)} onChange={v => set('notify_on_big_loss', v)} />
                        : <span style={{ fontSize: 12, color: get<boolean>('notify_on_big_loss', true) ? '#22c55e' : '#6e7681' }}>
                            {get<boolean>('notify_on_big_loss', true) ? '✓ On' : '✗ Off'}
                          </span>
                      }
                    </div>
                  }
                />,
              ]}
            />

            {/* ── Group 3: Price Movement Alerts ── */}
            <NotifGroupCard
              icon={<Bell size={14} />}
              label="Price Movement Alerts"
              desc="Alerts when a held asset's price moves significantly (per-pair cooldown applied)"
              rows={[
                <NotifRow key="pricemove"
                  label="Price Movements"
                  desc="Alert when any open position moves by the configured % since last alert"
                  changed={isChanged('notify_on_price_move') || isChanged('price_move_threshold_pct') || isChanged('price_move_cooldown_minutes')}
                  right={
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                      {editing && get<boolean>('notify_on_price_move', true) && (
                        <>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                            <span style={{ fontSize: 11, color: '#6e7681' }}>threshold</span>
                            <NumInput
                              value={get<number>('price_move_threshold_pct', 5)}
                              onChange={v => set('price_move_threshold_pct', v)}
                              min={0.5} max={50} step={0.5} suffix="%"
                            />
                          </div>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                            <span style={{ fontSize: 11, color: '#6e7681' }}>cooldown</span>
                            <NumInput
                              value={get<number>('price_move_cooldown_minutes', 20)}
                              onChange={v => set('price_move_cooldown_minutes', Math.round(v))}
                              min={1} max={1440} step={5} suffix="min"
                            />
                          </div>
                        </>
                      )}
                      {!editing && (
                        <span style={{ fontSize: 11, color: '#6e7681' }}>
                          ≥{get<number>('price_move_threshold_pct', 5)}% · {get<number>('price_move_cooldown_minutes', 20)}min cooldown
                        </span>
                      )}
                      {editing
                        ? <Toggle value={get<boolean>('notify_on_price_move', true)} onChange={v => set('notify_on_price_move', v)} />
                        : <span style={{ fontSize: 12, color: get<boolean>('notify_on_price_move', true) ? '#22c55e' : '#6e7681' }}>
                            {get<boolean>('notify_on_price_move', true) ? '✓ On' : '✗ Off'}
                          </span>
                      }
                    </div>
                  }
                />,
              ]}
            />

            {/* ── Group 4: Scheduled Messages ── */}
            <NotifGroupCard
              icon={<Calendar size={14} />}
              label="Scheduled Messages"
              desc="Time-based messages sent on a schedule — independent of trade activity"
              rows={[
                <NotifRow key="morning"
                  label="Morning Briefing"
                  desc="Overnight recap and day plan, sent once between 06:00–09:00 UTC"
                  changed={isChanged('notify_morning_plan')}
                  right={editing
                    ? <Toggle value={get<boolean>('notify_morning_plan', true)} onChange={v => set('notify_morning_plan', v)} />
                    : <span style={{ fontSize: 12, color: get<boolean>('notify_morning_plan', true) ? '#22c55e' : '#6e7681' }}>
                        {get<boolean>('notify_morning_plan', true) ? '✓ On' : '✗ Off'}
                      </span>
                  }
                />,
                <NotifRow key="evening"
                  label="Evening Summary"
                  desc="Day wrap-up with P&L, sent once between 20:00–22:00 UTC"
                  changed={isChanged('notify_evening_summary')}
                  right={editing
                    ? <Toggle value={get<boolean>('notify_evening_summary', true)} onChange={v => set('notify_evening_summary', v)} />
                    : <span style={{ fontSize: 12, color: get<boolean>('notify_evening_summary', true) ? '#22c55e' : '#6e7681' }}>
                        {get<boolean>('notify_evening_summary', true) ? '✓ On' : '✗ Off'}
                      </span>
                  }
                />,
                <NotifRow key="daily"
                  label="Daily Summary"
                  desc="Daily performance snapshot sent at a configured hour"
                  changed={isChanged('daily_summary') || isChanged('daily_summary_hour')}
                  right={
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      {editing && get<boolean>('daily_summary', true) && (
                        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                          <span style={{ fontSize: 11, color: '#6e7681' }}>at hour</span>
                          <NumInput
                            value={get<number>('daily_summary_hour', 8)}
                            onChange={v => set('daily_summary_hour', Math.round(v))}
                            min={0} max={23} step={1} suffix="UTC"
                          />
                        </div>
                      )}
                      {!editing && (
                        <span style={{ fontSize: 11, color: '#6e7681' }}>
                          {String(get<number>('daily_summary_hour', 8)).padStart(2, '0')}:00 UTC
                        </span>
                      )}
                      {editing
                        ? <Toggle value={get<boolean>('daily_summary', true)} onChange={v => set('daily_summary', v)} />
                        : <span style={{ fontSize: 12, color: get<boolean>('daily_summary', true) ? '#22c55e' : '#6e7681' }}>
                            {get<boolean>('daily_summary', true) ? '✓ On' : '✗ Off'}
                          </span>
                      }
                    </div>
                  }
                />,
              ]}
            />

            {/* ── Group 5: Periodic Check-ins ── */}
            <NotifGroupCard
              icon={<Clock size={14} />}
              label="Periodic Check-ins"
              desc="LLM-generated status updates sent at regular intervals (only when there's something worth saying)"
              rows={[
                <NotifRow key="periodic"
                  label="Periodic Updates"
                  desc="AI-written check-in messages based on recent events and market activity"
                  changed={isChanged('notify_periodic_update') || isChanged('status_update_interval')}
                  right={
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      {editing && get<boolean>('notify_periodic_update', true) && (
                        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                          <span style={{ fontSize: 11, color: '#6e7681' }}>every</span>
                          <NumInput
                            value={Math.round(get<number>('status_update_interval', 3600) / 60)}
                            onChange={v => set('status_update_interval', Math.round(v) * 60)}
                            min={5} max={1440} step={15} suffix="min"
                          />
                        </div>
                      )}
                      {!editing && (
                        <span style={{ fontSize: 11, color: '#6e7681' }}>
                          every {Math.round(get<number>('status_update_interval', 3600) / 60)} min
                        </span>
                      )}
                      {editing
                        ? <Toggle value={get<boolean>('notify_periodic_update', true)} onChange={v => set('notify_periodic_update', v)} />
                        : <span style={{ fontSize: 12, color: get<boolean>('notify_periodic_update', true) ? '#22c55e' : '#6e7681' }}>
                            {get<boolean>('notify_periodic_update', true) ? '✓ On' : '✗ Off'}
                          </span>
                      }
                    </div>
                  }
                />,
              ]}
            />

          </div>

          {/* Bottom action bar */}
          {editing && (
            <div style={{ display: 'flex', gap: 8, padding: '14px 0 0', justifyContent: 'flex-end', alignItems: 'center', borderTop: '1px solid #21262d', marginTop: 12 }}>
              {changedCount > 0 && (
                <span style={{ fontSize: 11, color: '#f59e0b', marginRight: 'auto', display: 'flex', alignItems: 'center', gap: 4 }}>
                  <AlertTriangle size={11} /> {changedCount} unsaved change{changedCount > 1 ? 's' : ''}
                </span>
              )}
              <button onClick={cancel} style={btnStyle('#21262d')}><X size={12} /> Cancel</button>
              <button onClick={handleSave} disabled={saving || changedCount === 0}
                style={{ ...btnStyle(changedCount > 0 ? '#238636' : '#21262d'), opacity: changedCount === 0 ? 0.5 : 1 }}>
                <Save size={12} /> {saving ? 'Saving…' : `Save & Apply${changedCount > 0 ? ` (${changedCount})` : ''}`}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════════════════
   Security Card — 2FA management
   ═══════════════════════════════════════════════════════════════════════════ */

type SecurityStep = 'idle' | 'setup' | 'confirm' | 'disable' | 'regen'

export function SecurityCard() {
  const [status, setStatus] = useState<TwoFAStatus | null>(null)
  const [step, setStep] = useState<SecurityStep>('idle')
  const [setupData, setSetupData] = useState<TwoFASetupResult | null>(null)
  const [code, setCode] = useState('')
  const [backupCodes, setBackupCodes] = useState<string[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [copied, setCopied] = useState(false)

  const loadStatus = useCallback(async () => {
    try {
      const s = await fetch2FAStatus()
      setStatus(s)
    } catch { /* ignore if not authed */ }
  }, [])

  useEffect(() => { loadStatus() }, [loadStatus])

  const handleSetup = async () => {
    setLoading(true); setError('')
    try {
      const data = await setup2FA()
      setSetupData(data)
      setStep('setup')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Setup failed')
    } finally { setLoading(false) }
  }

  const handleEnable = async () => {
    if (!code.trim()) return
    setLoading(true); setError('')
    try {
      await enable2FA(code.trim())
      setSuccess('2FA enabled successfully')
      setStep('idle'); setCode(''); setSetupData(null)
      await loadStatus()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Invalid code')
    } finally { setLoading(false) }
  }

  const handleDisable = async () => {
    if (!code.trim()) return
    setLoading(true); setError('')
    try {
      await disable2FA(code.trim())
      setSuccess('2FA disabled. You will need to log in again.')
      setStep('idle'); setCode('')
      await loadStatus()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Invalid code')
    } finally { setLoading(false) }
  }

  const handleRegenerate = async () => {
    if (!code.trim()) return
    setLoading(true); setError('')
    try {
      const result = await regenerateBackupCodes(code.trim())
      setBackupCodes(result.backup_codes)
      setStep('idle'); setCode('')
      setSuccess('Backup codes regenerated — save them now!')
      await loadStatus()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Invalid code')
    } finally { setLoading(false) }
  }

  const copyBackupCodes = (codes: string[]) => {
    navigator.clipboard.writeText(codes.join('\n')).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  useEffect(() => {
    if (success) { const t = setTimeout(() => setSuccess(''), 4000); return () => clearTimeout(t) }
  }, [success])

  const cardStyle: React.CSSProperties = {
    background: '#0d1117', border: '1px solid #21262d', borderRadius: 12,
    padding: '20px 24px', marginBottom: 16,
  }

  const inputStyle: React.CSSProperties = {
    ...inputBase, fontSize: 20, fontFamily: 'monospace', letterSpacing: 6,
    textAlign: 'center' as const, width: '100%', boxSizing: 'border-box' as const,
  }

  return (
    <div style={cardStyle}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16 }}>
        {status?.enabled
          ? <ShieldCheck size={16} style={{ color: '#22c55e' }} />
          : <ShieldOff size={16} style={{ color: '#6e7681' }} />}
        <span style={{ fontSize: 15, fontWeight: 700, color: '#e6edf3' }}>Two-Factor Authentication</span>
        {status?.enabled && (
          <span style={{
            fontSize: 10, fontWeight: 600, padding: '2px 8px', borderRadius: 10,
            background: '#22c55e18', color: '#22c55e', marginLeft: 'auto',
          }}>ENABLED</span>
        )}
      </div>

      {/* Success banner */}
      {success && (
        <div style={{
          padding: '8px 12px', background: 'rgba(34,197,94,0.1)', border: '1px solid rgba(34,197,94,0.3)',
          borderRadius: 8, fontSize: 12, color: '#4ade80', marginBottom: 14,
          display: 'flex', alignItems: 'center', gap: 6,
        }}>
          <Check size={12} /> {success}
        </div>
      )}

      {/* Error banner */}
      {error && (
        <div style={{
          padding: '8px 12px', background: 'rgba(248,81,73,0.1)', border: '1px solid rgba(248,81,73,0.3)',
          borderRadius: 8, fontSize: 12, color: '#f85149', marginBottom: 14,
          display: 'flex', alignItems: 'center', gap: 6,
        }}>
          <AlertTriangle size={12} /> {error}
          <button onClick={() => setError('')} style={{
            background: 'none', border: 'none', color: 'inherit', cursor: 'pointer', marginLeft: 'auto', padding: 0,
          }}><X size={12} /></button>
        </div>
      )}

      {/* ── Idle view ── */}
      {step === 'idle' && (
        <>
          <p style={{ fontSize: 13, color: '#8b949e', margin: '0 0 16px', lineHeight: 1.5 }}>
            {status?.enabled
              ? `2FA is active. You have ${status.backup_codes_remaining} backup code${status.backup_codes_remaining !== 1 ? 's' : ''} remaining.`
              : 'Add an extra layer of security by requiring a code from your authenticator app on every login.'}
          </p>

          {/* Backup codes display (after regen) */}
          {backupCodes && (
            <div style={{
              background: '#161b22', border: '1px solid #30363d', borderRadius: 8,
              padding: 16, marginBottom: 16,
            }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                <span style={{ fontSize: 12, fontWeight: 600, color: '#f59e0b' }}>
                  <KeyRound size={12} style={{ marginRight: 4, verticalAlign: -2 }} />
                  Save these backup codes
                </span>
                <button onClick={() => copyBackupCodes(backupCodes)} style={{
                  ...btnStyle('#21262d'), padding: '4px 10px', fontSize: 11,
                }}>
                  {copied ? <><Check size={11} /> Copied</> : <><Copy size={11} /> Copy</>}
                </button>
              </div>
              <div style={{
                display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 4,
                fontFamily: 'monospace', fontSize: 13, color: '#e6edf3',
              }}>
                {backupCodes.map((c, i) => (
                  <span key={i} style={{ padding: '4px 8px', background: '#0d1117', borderRadius: 4 }}>{c}</span>
                ))}
              </div>
              <button onClick={() => setBackupCodes(null)} style={{
                ...btnStyle('#21262d'), width: '100%', marginTop: 12, fontSize: 12,
              }}>
                I&apos;ve saved my codes
              </button>
            </div>
          )}

          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {status?.enabled ? (
              <>
                <button onClick={() => { setStep('disable'); setCode(''); setError('') }}
                  style={{ ...btnStyle('#21262d'), fontSize: 12 }}>
                  <ShieldOff size={12} /> Disable 2FA
                </button>
                <button onClick={() => { setStep('regen'); setCode(''); setError('') }}
                  style={{ ...btnStyle('#21262d'), fontSize: 12 }}>
                  <RefreshCw size={12} /> Regenerate Backup Codes
                </button>
              </>
            ) : (
              <button onClick={handleSetup} disabled={loading}
                style={{ ...btnStyle('#238636'), fontSize: 12, opacity: loading ? 0.6 : 1 }}>
                <ShieldCheck size={12} /> {loading ? 'Setting up…' : 'Enable 2FA'}
              </button>
            )}
          </div>
        </>
      )}

      {/* ── Setup view — show QR code ── */}
      {step === 'setup' && setupData && (
        <>
          <div style={{ fontSize: 13, color: '#8b949e', marginBottom: 14, lineHeight: 1.5 }}>
            Scan this QR code with your authenticator app (Google Authenticator, Authy, etc.):
          </div>

          <div style={{ textAlign: 'center', marginBottom: 16 }}>
            <img src={setupData.qr_code} alt="2FA QR Code" style={{
              width: 200, height: 200, borderRadius: 8,
              background: '#fff', padding: 8,
            }} />
          </div>

          <div style={{ marginBottom: 16 }}>
            <span style={{ fontSize: 11, color: '#6e7681', display: 'block', marginBottom: 4 }}>
              Or enter this key manually:
            </span>
            <code style={{
              ...codeStyle, display: 'block', padding: '8px 12px',
              fontSize: 13, letterSpacing: 2, wordBreak: 'break-all',
              userSelect: 'all',
            }}>
              {setupData.secret}
            </code>
          </div>

          {/* Backup codes */}
          <div style={{
            background: '#161b22', border: '1px solid #30363d', borderRadius: 8,
            padding: 14, marginBottom: 16,
          }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
              <span style={{ fontSize: 12, fontWeight: 600, color: '#f59e0b' }}>
                <KeyRound size={12} style={{ marginRight: 4, verticalAlign: -2 }} />
                Backup codes — save these now!
              </span>
              <button onClick={() => copyBackupCodes(setupData.backup_codes)} style={{
                ...btnStyle('#21262d'), padding: '4px 10px', fontSize: 11,
              }}>
                {copied ? <><Check size={11} /> Copied</> : <><Copy size={11} /> Copy</>}
              </button>
            </div>
            <div style={{
              display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 4,
              fontFamily: 'monospace', fontSize: 13, color: '#e6edf3',
            }}>
              {setupData.backup_codes.map((c, i) => (
                <span key={i} style={{ padding: '4px 8px', background: '#0d1117', borderRadius: 4 }}>{c}</span>
              ))}
            </div>
          </div>

          <div style={{ fontSize: 12, color: '#8b949e', marginBottom: 10 }}>
            Enter the 6-digit code from your authenticator to confirm:
          </div>
          <input
            type="text"
            inputMode="numeric"
            maxLength={6}
            value={code}
            onChange={e => setCode(e.target.value.replace(/\D/g, ''))}
            placeholder="000000"
            autoFocus
            style={{ ...inputStyle, marginBottom: 14 }}
          />
          <div style={{ display: 'flex', gap: 8 }}>
            <button onClick={() => { setStep('idle'); setSetupData(null); setCode(''); setError('') }}
              style={btnStyle('#21262d')}>
              <X size={12} /> Cancel
            </button>
            <button onClick={handleEnable} disabled={loading || code.length < 6}
              style={{ ...btnStyle('#238636'), opacity: code.length < 6 ? 0.5 : 1 }}>
              <Check size={12} /> {loading ? 'Verifying…' : 'Confirm & Enable'}
            </button>
          </div>
        </>
      )}

      {/* ── Disable view ── */}
      {step === 'disable' && (
        <>
          <div style={{ fontSize: 13, color: '#f59e0b', marginBottom: 14, lineHeight: 1.5 }}>
            <AlertTriangle size={13} style={{ verticalAlign: -2, marginRight: 4 }} />
            Enter your current 2FA code to disable two-factor authentication. All sessions will be revoked.
          </div>
          <input
            type="text"
            inputMode="numeric"
            maxLength={6}
            value={code}
            onChange={e => setCode(e.target.value.replace(/\D/g, ''))}
            placeholder="000000"
            autoFocus
            style={{ ...inputStyle, marginBottom: 14 }}
          />
          <div style={{ display: 'flex', gap: 8 }}>
            <button onClick={() => { setStep('idle'); setCode(''); setError('') }}
              style={btnStyle('#21262d')}>
              <X size={12} /> Cancel
            </button>
            <button onClick={handleDisable} disabled={loading || code.length < 6}
              style={{ ...btnStyle('#da3633'), opacity: code.length < 6 ? 0.5 : 1 }}>
              <ShieldOff size={12} /> {loading ? 'Disabling…' : 'Disable 2FA'}
            </button>
          </div>
        </>
      )}

      {/* ── Regenerate backup codes view ── */}
      {step === 'regen' && (
        <>
          <div style={{ fontSize: 13, color: '#8b949e', marginBottom: 14, lineHeight: 1.5 }}>
            Enter your current 2FA code to generate new backup codes. Old codes will be invalidated.
          </div>
          <input
            type="text"
            inputMode="numeric"
            maxLength={6}
            value={code}
            onChange={e => setCode(e.target.value.replace(/\D/g, ''))}
            placeholder="000000"
            autoFocus
            style={{ ...inputStyle, marginBottom: 14 }}
          />
          <div style={{ display: 'flex', gap: 8 }}>
            <button onClick={() => { setStep('idle'); setCode(''); setError('') }}
              style={btnStyle('#21262d')}>
              <X size={12} /> Cancel
            </button>
            <button onClick={handleRegenerate} disabled={loading || code.length < 6}
              style={{ ...btnStyle('#238636'), opacity: code.length < 6 ? 0.5 : 1 }}>
              <RefreshCw size={12} /> {loading ? 'Generating…' : 'Regenerate'}
            </button>
          </div>
        </>
      )}
    </div>
  )
}
