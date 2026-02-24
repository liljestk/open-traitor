import { useState, useMemo, useRef, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  fetchSettings, updateSettings, fetchPresets,
} from '../api'
import {
  X, AlertTriangle, Check,
  Info, ArrowRight, Zap,
  DollarSign,
  RefreshCw, Search,
  Minus, Plus, Activity, ShieldCheck,
  Sliders,
} from 'lucide-react'
import { LLMProvidersSection } from './settings/LLMProviders'
import PageTransition from '../components/PageTransition'
import {
  SECTION_CATEGORIES, SECTION_ORDER, PRESET_CONFIG,
  TIER_COLORS, TIER_LABELS,
  type CategoryKey,
  detectActivePreset, buildPresetDiff, formatFieldValue,
  btnStyle, inputBase,
} from './settings/settingsData'
import {
  Toast, SectionCard, RpmBudgetCard,
  TelegramSetupGuide, DensityToggle,
} from './settings/SettingsComponents'

/* ═══════════════════════════════════════════════════════════════════════════
   Quick Settings helpers
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

function initQuickDraft(settings: Record<string, unknown>): QuickDraft {
  const t = (settings.trading ?? {}) as Record<string, unknown>
  const r = (settings.risk ?? {}) as Record<string, unknown>
  const a = (settings.absolute_rules ?? {}) as Record<string, unknown>
  return {
    mode: String(t.mode ?? 'paper'),
    interval: Number(t.interval ?? 120),
    min_confidence: Number(t.min_confidence ?? 0.55),
    max_active_pairs: Number(t.max_active_pairs ?? 5),
    stop_loss_pct: Number(r.stop_loss_pct ?? 0.04),
    take_profit_pct: Number(r.take_profit_pct ?? 0.06),
    max_single_trade: Number(a.max_single_trade ?? 500),
    max_daily_loss: Number(a.max_daily_loss ?? 500),
  }
}

function Stepper({ value, onChange, min, max, step, format }: {
  value: number; onChange: (v: number) => void
  min?: number; max?: number; step?: number
  format?: (v: number) => string
}) {
  const s = step ?? 1
  const fmt = format ?? String
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <button onClick={() => onChange(Math.max(min ?? -Infinity, parseFloat((value - s).toFixed(6))))}
        style={{ ...btnStyle('#21262d'), padding: '4px 8px' }}>
        <Minus size={12} />
      </button>
      <span style={{ minWidth: 54, textAlign: 'center', fontSize: 13, fontWeight: 600, color: '#e6edf3' }}>
        {fmt(value)}
      </span>
      <button onClick={() => onChange(Math.min(max ?? Infinity, parseFloat((value + s).toFixed(6))))}
        style={{ ...btnStyle('#21262d'), padding: '4px 8px' }}>
        <Plus size={12} />
      </button>
    </div>
  )
}

function SegmentedControl({ value, options, onChange }: {
  value: string | number
  options: Array<{ value: string | number; label: string }>
  onChange: (v: string | number) => void
}) {
  return (
    <div style={{ display: 'flex', gap: 2, background: '#0d1117', borderRadius: 8, padding: 2, border: '1px solid #21262d' }}>
      {options.map(opt => (
        <button key={String(opt.value)} onClick={() => onChange(opt.value)} style={{
          padding: '5px 10px', fontSize: 11, fontWeight: 600, border: 'none', borderRadius: 6,
          cursor: 'pointer', transition: 'all 0.15s',
          background: value === opt.value ? '#21262d' : 'transparent',
          color: value === opt.value ? '#e6edf3' : '#6e7681',
          boxShadow: value === opt.value ? '0 1px 3px #00000040' : 'none',
        }}>{opt.label}</button>
      ))}
    </div>
  )
}

function QuickSettings({ settings, liveData, onSave }: {
  settings: Record<string, unknown>
  liveData: { settings: Record<string, unknown> }
  onSave: (section: string, updates: Record<string, unknown>) => Promise<void>
}) {
  const [draft, setDraft] = useState<QuickDraft>(() => initQuickDraft(settings))
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    setDraft(initQuickDraft(liveData.settings))
  }, [liveData]) // eslint-disable-line react-hooks/exhaustive-deps

  const live = initQuickDraft(settings)
  const changedKeys = (Object.keys(draft) as (keyof QuickDraft)[]).filter(
    k => String(draft[k]) !== String(live[k])
  )
  const changedCount = changedKeys.length

  function set<K extends keyof QuickDraft>(key: K, val: QuickDraft[K]) {
    setDraft(d => ({ ...d, [key]: val }))
  }

  const handleApply = async () => {
    if (!changedCount) return
    setSaving(true)
    try {
      const tradingChanges: Record<string, unknown> = {}
      const riskChanges: Record<string, unknown> = {}
      const absoluteChanges: Record<string, unknown> = {}
      for (const key of changedKeys) {
        if (['mode', 'interval', 'min_confidence', 'max_active_pairs'].includes(key))
          tradingChanges[key] = draft[key]
        else if (['stop_loss_pct', 'take_profit_pct'].includes(key))
          riskChanges[key] = draft[key]
        else if (['max_single_trade', 'max_daily_loss'].includes(key))
          absoluteChanges[key] = draft[key]
      }
      await Promise.all([
        Object.keys(tradingChanges).length ? onSave('trading', tradingChanges) : null,
        Object.keys(riskChanges).length ? onSave('risk', riskChanges) : null,
        Object.keys(absoluteChanges).length ? onSave('absolute_rules', absoluteChanges) : null,
      ].filter(Boolean))
    } finally {
      setSaving(false)
    }
  }

  const field = (label: string, key: keyof QuickDraft, control: React.ReactNode) => {
    const changed = String(draft[key]) !== String(live[key])
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
          <span style={{ fontSize: 11, color: '#8b949e', fontWeight: 500 }}>{label}</span>
          {changed && <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#f59e0b', flexShrink: 0 }} />}
        </div>
        {control}
      </div>
    )
  }

  return (
    <div style={{
      background: '#0d1117', border: '1px solid #21262d', borderRadius: 12,
      padding: '16px 20px', marginBottom: 16,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Sliders size={14} style={{ color: '#58a6ff' }} />
          <span style={{ fontSize: 12, fontWeight: 700, color: '#e6edf3', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            Quick Settings
          </span>
          {changedCount > 0 && (
            <span style={{ fontSize: 10, padding: '2px 7px', borderRadius: 10, background: '#f59e0b18', color: '#f59e0b', fontWeight: 600 }}>
              {changedCount} unsaved
            </span>
          )}
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          {changedCount > 0 && (
            <button onClick={() => setDraft(initQuickDraft(settings))}
              style={{ ...btnStyle('#21262d'), padding: '6px 12px', fontSize: 12 }}>
              Reset
            </button>
          )}
          <button onClick={handleApply} disabled={!changedCount || saving} style={{
            ...btnStyle(changedCount ? '#1f6feb' : '#21262d'),
            padding: '6px 14px', fontSize: 12, fontWeight: 600,
            opacity: changedCount ? 1 : 0.5, cursor: changedCount ? 'pointer' : 'default',
          }}>
            {saving ? 'Saving…' : changedCount ? `Apply ${changedCount} change${changedCount !== 1 ? 's' : ''}` : 'No changes'}
          </button>
        </div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '14px 20px' }}>
        {field('Trading Mode', 'mode',
          <SegmentedControl value={draft.mode}
            options={[{ value: 'paper', label: '📝 Paper' }, { value: 'live', label: '⚡ Live' }]}
            onChange={v => set('mode', String(v))} />
        )}
        {field('Cycle Interval', 'interval',
          <SegmentedControl value={draft.interval}
            options={[{ value: 60, label: '1m' }, { value: 120, label: '2m' }, { value: 300, label: '5m' }, { value: 600, label: '10m' }]}
            onChange={v => set('interval', Number(v))} />
        )}
        {field(`Confidence: ${(draft.min_confidence * 100).toFixed(0)}%`, 'min_confidence',
          <input type="range" min={0.3} max={0.95} step={0.05} value={draft.min_confidence}
            onChange={e => set('min_confidence', parseFloat(e.target.value))}
            style={{ width: '100%', accentColor: '#58a6ff', height: 4, cursor: 'pointer' }} />
        )}
        {field('Max Active Pairs', 'max_active_pairs',
          <Stepper value={draft.max_active_pairs} onChange={v => set('max_active_pairs', v)} min={1} max={50} />
        )}
        {field('Stop Loss', 'stop_loss_pct',
          <Stepper value={draft.stop_loss_pct} onChange={v => set('stop_loss_pct', v)}
            min={0.005} max={0.5} step={0.005} format={v => `${(v * 100).toFixed(1)}%`} />
        )}
        {field('Take Profit', 'take_profit_pct',
          <Stepper value={draft.take_profit_pct} onChange={v => set('take_profit_pct', v)}
            min={0.005} max={1.0} step={0.005} format={v => `${(v * 100).toFixed(1)}%`} />
        )}
        {field('Max Single Trade', 'max_single_trade',
          <div style={{ display: 'flex', alignItems: 'center', gap: 4, background: '#161b22', borderRadius: 6, padding: '4px 8px', border: '1px solid #21262d' }}>
            <DollarSign size={11} style={{ color: '#8b949e' }} />
            <input type="number" value={draft.max_single_trade}
              onChange={e => set('max_single_trade', Number(e.target.value))}
              style={{ ...inputBase, width: 70, padding: 0, border: 'none', fontSize: 13, fontWeight: 600 }} />
          </div>
        )}
        {field('Max Daily Loss', 'max_daily_loss',
          <div style={{ display: 'flex', alignItems: 'center', gap: 4, background: '#161b22', borderRadius: 6, padding: '4px 8px', border: '1px solid #21262d' }}>
            <DollarSign size={11} style={{ color: '#8b949e' }} />
            <input type="number" value={draft.max_daily_loss}
              onChange={e => set('max_daily_loss', Number(e.target.value))}
              style={{ ...inputBase, width: 70, padding: 0, border: 'none', fontSize: 13, fontWeight: 600 }} />
          </div>
        )}
      </div>
    </div>
  )
}

function ConfigHealthPanel({ settings }: { settings: Record<string, unknown> }) {
  const t = (settings.trading ?? {}) as Record<string, unknown>
  const r = (settings.risk ?? {}) as Record<string, unknown>
  const a = (settings.absolute_rules ?? {}) as Record<string, unknown>
  const sl = Number(r.stop_loss_pct ?? 0)
  const tp = Number(r.take_profit_pct ?? 0)
  const conf = Number(t.min_confidence ?? 0)
  const drawdown = Number(r.max_drawdown_pct ?? 0)
  const pairs = Number(t.max_active_pairs ?? 0)
  const mode = String(t.mode ?? 'paper')
  const dailyLoss = Number(a.max_daily_loss ?? 0)
  const dailySpend = Number(a.max_daily_spend ?? 0)

  type Severity = 'error' | 'warning' | 'info'
  const issues: Array<{ severity: Severity; message: string }> = []
  if (sl > 0 && tp > 0 && sl >= tp)
    issues.push({ severity: 'error', message: 'Stop Loss ≥ Take Profit — inverted risk/reward ratio' })
  if (conf > 0 && conf < 0.45)
    issues.push({ severity: 'warning', message: 'Very low confidence threshold — expect many low-quality signals' })
  if (conf > 0.85)
    issues.push({ severity: 'info', message: 'Very high confidence threshold — bot may trade infrequently' })
  if (drawdown > 0.25)
    issues.push({ severity: 'warning', message: `Max drawdown tolerance is very high (${(drawdown * 100).toFixed(0)}%)` })
  if (mode === 'live' && pairs > 15)
    issues.push({ severity: 'warning', message: `${pairs} active pairs in live mode — consider reducing for tighter risk control` })
  if (dailySpend > 0 && dailyLoss > dailySpend * 0.9)
    issues.push({ severity: 'info', message: 'Daily loss limit is nearly equal to daily spend cap' })

  const colors: Record<Severity, string> = { error: '#ef4444', warning: '#f59e0b', info: '#58a6ff' }
  const icons: Record<Severity, React.ReactNode> = {
    error: <AlertTriangle size={13} />,
    warning: <Zap size={13} />,
    info: <Info size={13} />,
  }
  const hasWarnings = issues.some(i => i.severity === 'error' || i.severity === 'warning')

  if (!issues.length) return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 8, padding: '10px 16px',
      background: '#22c55e08', border: '1px solid #22c55e22', borderRadius: 10, marginBottom: 16,
      fontSize: 12, color: '#22c55e',
    }}>
      <ShieldCheck size={14} />
      Configuration looks healthy
    </div>
  )

  return (
    <div style={{
      background: '#0d1117', border: `1px solid ${hasWarnings ? '#f59e0b22' : '#21262d'}`,
      borderRadius: 12, padding: '12px 16px', marginBottom: 16,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 10 }}>
        <Activity size={13} style={{ color: hasWarnings ? '#f59e0b' : '#58a6ff' }} />
        <span style={{ fontSize: 11, fontWeight: 700, color: hasWarnings ? '#f59e0b' : '#58a6ff', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
          Config Health
        </span>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {issues.map((issue, i) => (
          <div key={i} style={{
            display: 'flex', alignItems: 'center', gap: 8, fontSize: 12,
            color: colors[issue.severity], padding: '4px 0',
          }}>
            {icons[issue.severity]}
            {issue.message}
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
