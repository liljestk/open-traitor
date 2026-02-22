import { useState, useMemo, useRef, useEffect, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  fetchSettings, updateSettings, fetchPresets,
  fetchLLMProviders, updateLLMProviders, updateApiKeys,
  fetchOpenRouterCredits,
  type SectionSchema, type FieldSchema, type PresetInfo,
  type LLMProviderConfig, type OpenRouterCreditsInfo,
} from '../api'
import {
  Shield, ShieldAlert, ShieldOff, ToggleLeft, ToggleRight,
  ChevronDown, Save, X, AlertTriangle, Check,
  Info, ArrowRight, ArrowUp, ArrowDown, Zap, Server, Cloud, Eye, EyeOff, Key,
  Maximize2, Minimize2, Search,
  Activity, Gauge, TrendingUp, DollarSign,
  Newspaper, BookOpen, FileText, Heart, Layers, Settings2,
  Cpu, BarChart3, MonitorDot, Bot, Wifi, MessageSquare,
  Sparkles, Lock, RefreshCw, ExternalLink,
} from 'lucide-react'
import PageTransition from '../components/PageTransition'
import { useLiveStore, type Density } from '../store'

/* ═══════════════════════════════════════════════════════════════════════════
   Field descriptions — human-readable explanations for every setting
   ═══════════════════════════════════════════════════════════════════════════ */

const FIELD_DESCRIPTIONS: Record<string, Record<string, string>> = {
  absolute_rules: {
    max_single_trade: 'Maximum amount (in quote currency) for any single trade. Acts as a hard safety cap.',
    max_daily_spend: 'Maximum total amount that can be spent on buys in a single day.',
    max_daily_loss: 'Circuit-breaker — trading halts for the day if cumulative losses exceed this.',
    max_portfolio_risk_pct: 'Maximum percentage of total portfolio value exposed to risk at any time.',
    require_approval_above: 'Trades above this amount require manual Telegram approval before execution.',
    never_trade_pairs: 'Pairs that are completely blocked from trading, regardless of signals.',
    only_trade_pairs: 'If non-empty, only these pairs can be traded. Leave empty for no restriction.',
    min_trade_interval_seconds: 'Minimum seconds between trades on the same pair to prevent overtrading.',
    max_trades_per_day: 'Hard limit on total number of trade executions per day across all pairs.',
    max_cash_per_trade_pct: 'Maximum percentage of available cash that can be used in a single trade.',
    emergency_stop_portfolio: 'Emergency stop — all trading halts if portfolio value drops below this.',
    always_use_stop_loss: 'When enabled, every buy trade automatically sets a stop-loss order.',
    max_stop_loss_pct: 'Maximum allowed stop-loss distance as percentage from entry price.',
  },
  trading: {
    mode: 'Paper mode simulates trades without real money. Live mode executes real trades.',
    pair_discovery: '"all" scans the entire exchange for opportunities. "configured" uses only the pairs list.',
    pairs: 'Specific trading pairs to monitor (e.g. BTC-EUR, ETH-EUR).',
    quote_currency: 'Base currency used for trading (e.g. EUR, USD).',
    quote_currencies: 'All quote currencies accepted for pair matching.',
    interval: 'Seconds between each analysis cycle. Lower = more responsive but more API calls.',
    min_confidence: 'Minimum AI confidence score (0–1) required to trigger a trade signal.',
    max_open_positions: 'Maximum number of positions that can be open simultaneously.',
    reconcile_every_cycles: 'How often (in cycles) to reconcile dashboard state with exchange.',
    paper_slippage_pct: 'Simulated slippage for paper trades, making simulations more realistic.',
    live_holdings_sync: 'Sync holdings from the exchange at startup to reflect real positions.',
    holdings_refresh_seconds: 'How often to refresh holdings data from the exchange.',
    holdings_dust_threshold: 'Ignore holdings worth less than this in quote currency (dust filter).',
    invalidate_strategic_context: 'Force re-evaluation of strategic context on next cycle.',
    include_crypto_quotes: 'Include crypto-to-crypto pairs (e.g. ETH-BTC) in universe scanning.',
    pair_universe_refresh_seconds: 'How often to rescan the full pair universe for new opportunities.',
    max_active_pairs: 'Maximum number of pairs to actively monitor and trade at any time.',
    scan_volume_threshold: 'Minimum 24h volume required for a pair to be considered in scanning.',
    scan_movement_threshold_pct: 'Minimum price movement % to flag a pair as a mover during scans.',
    screener_interval_cycles: 'How often (in cycles) to run the pair screener/scanner.',
  },
  risk: {
    max_position_pct: 'Maximum percentage of portfolio that a single position can represent.',
    max_total_exposure_pct: 'Maximum total percentage of portfolio allocated to positions.',
    max_drawdown_pct: 'Maximum allowed drawdown from peak portfolio value before halting.',
    stop_loss_pct: 'Default stop-loss distance as percentage below entry price.',
    take_profit_pct: 'Default take-profit distance as percentage above entry price.',
    trailing_stop_pct: 'Trailing stop distance — adjusts upward as price increases.',
    max_trades_per_hour: 'Maximum trades per hour to prevent rapid-fire overtrading.',
    loss_cooldown_seconds: 'Cooldown period after a losing trade before allowing new trades.',
  },
  rotation: {
    enabled: 'Enable portfolio rotation to swap underperforming positions for better opportunities.',
    autonomous_allocation_pct: 'Percentage of portfolio the AI can autonomously reallocate.',
    min_score_delta: 'Minimum score difference between positions to trigger a rotation swap.',
    min_confidence: 'Minimum AI confidence to execute a rotation swap.',
    high_impact_confidence: 'Confidence threshold for high-impact rotation moves.',
    approval_threshold: 'Rotation swaps above this value require manual approval.',
    swap_cooldown_seconds: 'Minimum time between rotation swaps to prevent thrashing.',
    full_autonomy: 'Allow AI to perform rotations without manual approval for small amounts.',
    llm_validation: 'Use LLM to validate rotation decisions before execution.',
    llm_validation_temperature: 'LLM temperature for rotation validation (lower = more conservative).',
  },
  fees: {
    trade_fee_pct: 'Exchange taker fee percentage (e.g. 0.006 = 0.6%).',
    maker_fee_pct: 'Exchange maker fee percentage for limit orders.',
    safety_margin: 'Fee safety multiplier — accounts for fee variations and rounding.',
    min_gain_after_fees_pct: 'Minimum profit % after fees for a trade to be worthwhile.',
    min_trade_quote: 'Minimum trade size in quote currency (exchange minimum + buffer).',
    swap_cooldown_seconds: 'Cooldown between fee-related recalculations.',
  },
  high_stakes: {
    trade_size_multiplier: 'Multiply trade size by this factor during high-stakes mode.',
    swap_allocation_multiplier: 'Multiply rotation allocation by this during high-stakes mode.',
    min_confidence: 'Minimum confidence required to enter high-stakes mode.',
    min_swap_gain_pct: 'Minimum expected gain to trigger a high-stakes swap.',
    auto_approve_up_to: 'Auto-approve high-stakes trades up to this amount.',
  },
  telegram: {
    status_update_interval: 'Seconds between automatic status updates sent to Telegram.',
    notify_on_trade: 'Send a Telegram notification whenever a trade is executed.',
    notify_on_signal_confidence: 'Send notification when signal confidence exceeds this threshold.',
    daily_summary: 'Send a daily performance summary to Telegram.',
    daily_summary_hour: 'Hour of day (0–23) to send the daily summary.',
  },
  news: {
    fetch_interval: 'Seconds between news feed fetches.',
    reddit_subreddits: 'Subreddits to monitor for crypto news and sentiment.',
    rss_feeds: 'RSS feed URLs to scrape for news articles.',
    max_articles: 'Maximum articles to store in the news cache.',
    articles_for_analysis: 'Number of recent articles to include in AI analysis.',
  },
  fear_greed: {
    enabled: 'Include the Fear & Greed Index in market analysis.',
    cache_ttl: 'How long (seconds) to cache the Fear & Greed value before re-fetching.',
  },
  multi_timeframe: {
    enabled: 'Enable multi-timeframe analysis for more robust signals.',
    min_alignment: 'Minimum number of timeframes that must agree for a valid signal.',
  },
  journal: {
    enabled: 'Record decision journal entries for every trading cycle.',
    data_dir: 'Directory to store journal files.',
  },
  audit: {
    enabled: 'Enable audit trail logging for compliance and review.',
    data_dir: 'Directory to store audit files.',
  },
  llm: {
    model: 'Default LLM model name (fallback when no provider is available).',
    temperature: 'LLM temperature (0 = deterministic, 2 = creative). Lower is safer for trading.',
    max_tokens: 'Maximum tokens per LLM response.',
    max_retries: 'Number of retry attempts on LLM API failures.',
    timeout: 'Seconds to wait for LLM response before timing out.',
    persona: 'System prompt / personality definition for the trading AI assistant.',
  },
  logging: {
    level: 'Minimum log level. DEBUG = verbose, ERROR = critical only.',
    file_enabled: 'Write logs to files in addition to console.',
    directory: 'Directory for log files.',
    max_file_size: 'Maximum log file size in MB before rotation.',
    backup_count: 'Number of rotated log files to keep.',
  },
  health: {
    port: 'Port for the health-check HTTP endpoint.',
  },
  dashboard: {
    enabled: 'Enable the web dashboard.',
    port: 'Port for the dashboard web server.',
    langfuse_host: 'URL of the Langfuse tracing server.',
    langfuse_enabled: 'Enable Langfuse LLM tracing integration.',
  },
  'analysis.technical': {
    rsi_period: 'RSI calculation period (default 14). Lower = more sensitive.',
    rsi_overbought: 'RSI threshold above which an asset is considered overbought.',
    rsi_oversold: 'RSI threshold below which an asset is considered oversold.',
    macd_fast: 'MACD fast EMA period.',
    macd_slow: 'MACD slow EMA period.',
    macd_signal: 'MACD signal line smoothing period.',
    bb_period: 'Bollinger Bands calculation period.',
    bb_std: 'Bollinger Bands standard deviation multiplier.',
    ema_periods: 'EMA periods for moving average analysis (comma-separated).',
    candle_count: 'Number of candles to fetch for technical analysis.',
    candle_granularity: 'Candle timeframe (e.g. ONE_HOUR, FIFTEEN_MINUTE).',
  },
  'analysis.sentiment': {
    enabled: 'Include sentiment analysis in trading decisions.',
    sample_size: 'Number of data points to sample for sentiment scoring.',
  },
  routing: {
    bridge_currencies: 'Currencies used as bridges for indirect trading routes.',
    min_bridge_volume_24h: 'Minimum 24h volume for a bridge pair to be viable.',
    slippage_factor: 'Expected slippage per hop in a trade route.',
  },
}

/* ═══════════════════════════════════════════════════════════════════════════
   Section icons & categories
   ═══════════════════════════════════════════════════════════════════════════ */

const SECTION_ICONS: Record<string, ReactNode> = {
  absolute_rules: <Shield size={15} />,
  trading: <TrendingUp size={15} />,
  risk: <Gauge size={15} />,
  rotation: <RefreshCw size={15} />,
  fees: <DollarSign size={15} />,
  high_stakes: <Sparkles size={15} />,
  telegram: <MessageSquare size={15} />,
  news: <Newspaper size={15} />,
  fear_greed: <Heart size={15} />,
  multi_timeframe: <Layers size={15} />,
  llm: <Cpu size={15} />,
  analysis: <BarChart3 size={15} />,
  logging: <FileText size={15} />,
  journal: <BookOpen size={15} />,
  audit: <Activity size={15} />,
  health: <MonitorDot size={15} />,
  dashboard: <BarChart3 size={15} />,
  routing: <Wifi size={15} />,
}

type CategoryKey = 'trading' | 'intelligence' | 'infra' | 'appearance'

const SECTION_CATEGORIES: Record<CategoryKey, { label: string; icon: ReactNode; sections: string[] }> = {
  trading: {
    label: 'Trading & Safety',
    icon: <Shield size={15} />,
    sections: ['absolute_rules', 'trading', 'risk', 'rotation', 'fees', 'high_stakes', 'routing'],
  },
  intelligence: {
    label: 'AI & Analysis',
    icon: <Cpu size={15} />,
    sections: ['llm', 'analysis', 'news', 'fear_greed', 'multi_timeframe'],
  },
  infra: {
    label: 'Infrastructure',
    icon: <Server size={15} />,
    sections: ['telegram', 'logging', 'journal', 'audit', 'health', 'dashboard'],
  },
  appearance: {
    label: 'Appearance',
    icon: <Settings2 size={15} />,
    sections: [],
  },
}

/* ═══════════════════════════════════════════════════════════════════════════
   Preset config
   ═══════════════════════════════════════════════════════════════════════════ */

const PRESET_CONFIG: Record<string, { label: string; color: string; icon: ReactNode; desc: string }> = {
  disabled:      { label: 'Disabled',     color: '#6e7681', icon: <ShieldOff size={18} />,   desc: 'All trading stopped' },
  conservative:  { label: 'Conservative', color: '#3b82f6', icon: <Shield size={18} />,      desc: 'Low risk, small trades' },
  moderate:      { label: 'Moderate',     color: '#22c55e', icon: <Shield size={18} />,      desc: 'Balanced risk / reward' },
  aggressive:    { label: 'Aggressive',   color: '#f59e0b', icon: <ShieldAlert size={18} />, desc: 'Higher limits, more trades' },
}

const TIER_COLORS: Record<string, string> = { safe: '#22c55e', semi_safe: '#f59e0b', blocked: '#ef4444' }
const TIER_LABELS: Record<string, string> = { safe: 'Telegram Safe', semi_safe: 'Semi-Safe', blocked: 'Dashboard Only' }

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

/* ═══════════════════════════════════════════════════════════════════════════
   Helpers
   ═══════════════════════════════════════════════════════════════════════════ */

function formatKey(key: string): string {
  return key.replace(/_/g, ' ').replace(/\bpct\b/g, '%').replace(/\b\w/g, c => c.toUpperCase())
}

function formatFieldValue(key: string, val: unknown): string {
  if (val === null || val === undefined) return '—'
  if (key.endsWith('_pct') && typeof val === 'number') return `${(val * 100).toFixed(1)}%`
  if (typeof val === 'number') return val.toLocaleString()
  return String(val)
}

function renderValue(val: unknown): string {
  if (typeof val === 'boolean') return val ? '✓ Enabled' : '✗ Disabled'
  if (Array.isArray(val)) return val.length ? val.join(', ') : '(empty)'
  if (val === null || val === undefined) return '—'
  return String(val)
}

function getFieldDesc(section: string, field: string): string | undefined {
  return FIELD_DESCRIPTIONS[section]?.[field]
}

function detectActivePreset(
  settings: Record<string, unknown>,
  presets: Record<string, PresetInfo>,
): string | null {
  for (const [name, preset] of Object.entries(presets)) {
    let ok = true
    for (const [section, fields] of Object.entries(preset.values)) {
      const cur = settings[section] as Record<string, unknown> | undefined
      if (!cur) { ok = false; break }
      for (const [field, expected] of Object.entries(fields)) {
        if (JSON.stringify(cur[field]) !== JSON.stringify(expected)) { ok = false; break }
      }
      if (!ok) break
    }
    if (ok) return name
  }
  return null
}

interface DiffRow { key: string; section: string; label: string; current: unknown; target: unknown; changed: boolean }

function buildPresetDiff(settings: Record<string, unknown>, preset: PresetInfo): DiffRow[] {
  const rows: DiffRow[] = []
  for (const [section, fields] of Object.entries(preset.values)) {
    for (const [field, target] of Object.entries(fields)) {
      const current = (settings[section] as Record<string, unknown> | undefined)?.[field]
      rows.push({
        key: `${section}.${field}`, section, label: FIELD_LABELS[field] ?? formatKey(field),
        current, target, changed: JSON.stringify(current) !== JSON.stringify(target),
      })
    }
  }
  return rows
}

function btnStyle(bg: string): React.CSSProperties {
  return {
    display: 'inline-flex', alignItems: 'center', gap: 5,
    background: bg, color: '#e6edf3', border: '1px solid transparent',
    borderRadius: 6, padding: '6px 14px', fontSize: 12, fontWeight: 500,
    cursor: 'pointer', transition: 'all 0.15s',
    borderColor: bg === '#238636' ? '#238636' : '#30363d',
  }
}

const codeStyle: React.CSSProperties = {
  fontSize: 11, color: '#79c0ff', background: '#161b22', padding: '1px 5px',
  borderRadius: 3, fontFamily: 'var(--font-mono, monospace)',
}

/* ═══════════════════════════════════════════════════════════════════════════
   Toast notification
   ═══════════════════════════════════════════════════════════════════════════ */

function Toast({ message, type, onDismiss }: { message: string; type: 'success' | 'error'; onDismiss: () => void }) {
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

const inputBase: React.CSSProperties = {
  background: '#0d1117', color: '#e6edf3', border: '1px solid #30363d',
  borderRadius: 6, padding: '5px 10px', fontSize: 13, outline: 'none',
}

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

function SectionCard({ name, label, values, schema, telegramTier, onSave, searchQuery }: {
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
        <span style={{ fontWeight: 600, fontSize: 14, flex: 1, textAlign: 'left' }}>{label}</span>

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

  const providers = data?.providers ?? []
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
   Telegram Setup Guide
   ═══════════════════════════════════════════════════════════════════════════ */

function TelegramSetupGuide() {
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
                <li>Choose a display name (e.g. &quot;Auto Traitor Bot&quot;)</li>
                <li>Choose a username ending in &quot;bot&quot; (e.g. &quot;auto_traitor_bot&quot;)</li>
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

function DensityToggle() {
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
   Section ordering
   ═══════════════════════════════════════════════════════════════════════════ */

const SECTION_ORDER = [
  'absolute_rules', 'trading', 'risk', 'rotation', 'fees', 'high_stakes',
  'routing', 'telegram', 'news', 'fear_greed', 'multi_timeframe',
  'llm', 'analysis', 'logging', 'journal', 'audit', 'health', 'dashboard',
]

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
