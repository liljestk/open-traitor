import type { ReactNode } from 'react'
import type { PresetInfo } from '../../api'
import {
  Shield, ShieldAlert, ShieldOff,
  TrendingUp, Gauge, RefreshCw, DollarSign, Sparkles,
  MessageSquare, Newspaper, Heart, Layers,
  Cpu, BarChart3, FileText, BookOpen, Activity, MonitorDot, Wifi,
  Server, Settings2,
} from 'lucide-react'
import { createElement as h } from 'react'

/* ═══════════════════════════════════════════════════════════════════════════
   Field descriptions — human-readable explanations for every setting
   ═══════════════════════════════════════════════════════════════════════════ */

export const FIELD_DESCRIPTIONS: Record<string, Record<string, string>> = {
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
    max_active_pairs: 'Maximum pairs to actively monitor. Capped by your LLM provider\'s RPM limit — the system automatically calculates the safe maximum based on your provider and cycle interval.',
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
    min_trade_quote: 'Absolute floor for minimum trade size in quote currency (e.g. 1.0 EUR).',
    min_trade_pct: 'Minimum trade size as % of portfolio (0.01 = 1%). Dynamic — scales with account.',
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
    // Trade & Signal Alerts
    notify_on_trade: 'Send a Telegram message whenever a trade is executed.',
    notify_on_signal: 'Send a Telegram message when a trading signal is detected.',
    notify_on_signal_confidence: 'Minimum AI confidence (0–1) a signal must reach before a notification is sent. 0.65 = 65%.',
    // Win / Loss Highlights
    notify_on_big_win: 'Celebrate trades with a profit above the win threshold.',
    big_win_threshold: 'Profit in USD that qualifies a trade as a "big win" (sends a celebratory message).',
    notify_on_big_loss: 'Alert when a trade loss exceeds the loss threshold.',
    big_loss_threshold: 'Loss in USD (absolute value) that triggers a "big loss" alert.',
    // Price Movement Alerts
    notify_on_price_move: 'Alert when a held pair moves significantly in price.',
    price_move_threshold_pct: 'Price change % required to send an alert (e.g. 5 = 5%). Applied to open positions only.',
    price_move_cooldown_minutes: 'Minutes to wait before sending another price alert for the same pair.',
    // Scheduled Messages
    notify_morning_plan: 'Send a morning briefing with overnight recap and day plan (06:00–09:00 UTC).',
    notify_evening_summary: 'Send an evening wrap-up with the day\'s performance (20:00–22:00 UTC).',
    notify_periodic_update: 'Send periodic LLM-generated check-in messages.',
    status_update_interval: 'Seconds between periodic check-in messages (0 = disabled).',
    daily_summary: 'Send a daily performance summary to Telegram.',
    daily_summary_hour: 'Hour of day (0–23 UTC) to send the daily summary.',
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

export const SECTION_ICONS: Record<string, ReactNode> = {
  absolute_rules: h(Shield, { size: 15 }),
  trading: h(TrendingUp, { size: 15 }),
  risk: h(Gauge, { size: 15 }),
  rotation: h(RefreshCw, { size: 15 }),
  fees: h(DollarSign, { size: 15 }),
  high_stakes: h(Sparkles, { size: 15 }),
  telegram: h(MessageSquare, { size: 15 }),
  news: h(Newspaper, { size: 15 }),
  fear_greed: h(Heart, { size: 15 }),
  multi_timeframe: h(Layers, { size: 15 }),
  llm: h(Cpu, { size: 15 }),
  analysis: h(BarChart3, { size: 15 }),
  logging: h(FileText, { size: 15 }),
  journal: h(BookOpen, { size: 15 }),
  audit: h(Activity, { size: 15 }),
  health: h(MonitorDot, { size: 15 }),
  dashboard: h(BarChart3, { size: 15 }),
  routing: h(Wifi, { size: 15 }),
}

export type CategoryKey = 'trading' | 'intelligence' | 'infra' | 'appearance'

export const SECTION_CATEGORIES: Record<CategoryKey, { label: string; icon: ReactNode; sections: string[] }> = {
  trading: {
    label: 'Trading & Safety',
    icon: h(Shield, { size: 15 }),
    sections: ['absolute_rules', 'trading', 'risk', 'rotation', 'fees', 'high_stakes', 'routing'],
  },
  intelligence: {
    label: 'AI & Analysis',
    icon: h(Cpu, { size: 15 }),
    sections: ['llm', 'analysis', 'news', 'fear_greed', 'multi_timeframe'],
  },
  infra: {
    label: 'Infrastructure',
    icon: h(Server, { size: 15 }),
    sections: ['telegram', 'logging', 'journal', 'audit', 'health', 'dashboard'],
  },
  appearance: {
    label: 'Appearance',
    icon: h(Settings2, { size: 15 }),
    sections: [],
  },
}

/* ═══════════════════════════════════════════════════════════════════════════
   Preset config
   ═══════════════════════════════════════════════════════════════════════════ */

export const PRESET_CONFIG: Record<string, { label: string; color: string; icon: ReactNode; desc: string }> = {
  disabled:      { label: 'Disabled',     color: '#6e7681', icon: h(ShieldOff, { size: 18 }),   desc: 'All trading stopped' },
  conservative:  { label: 'Conservative', color: '#3b82f6', icon: h(Shield, { size: 18 }),      desc: 'Low risk, small trades' },
  moderate:      { label: 'Moderate',     color: '#22c55e', icon: h(Shield, { size: 18 }),      desc: 'Balanced risk / reward' },
  aggressive:    { label: 'Aggressive',   color: '#f59e0b', icon: h(ShieldAlert, { size: 18 }), desc: 'Higher limits, more trades' },
}

export const TIER_COLORS: Record<string, string> = { safe: '#22c55e', semi_safe: '#f59e0b', blocked: '#ef4444' }
export const TIER_LABELS: Record<string, string> = { safe: 'Telegram Safe', semi_safe: 'Semi-Safe', blocked: 'Dashboard Only' }

export const FIELD_LABELS: Record<string, string> = {
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

export function formatKey(key: string): string {
  return key.replace(/_/g, ' ').replace(/\bpct\b/g, '%').replace(/\b\w/g, c => c.toUpperCase())
}

export function formatFieldValue(key: string, val: unknown): string {
  if (val === null || val === undefined) return '—'
  if (key.endsWith('_pct') && typeof val === 'number') return `${(val * 100).toFixed(1)}%`
  if (typeof val === 'number') return val.toLocaleString()
  return String(val)
}

export function renderValue(val: unknown): string {
  if (typeof val === 'boolean') return val ? '✓ Enabled' : '✗ Disabled'
  if (Array.isArray(val)) return val.length ? val.join(', ') : '(empty)'
  if (val === null || val === undefined) return '—'
  return String(val)
}

export function getFieldDesc(section: string, field: string): string | undefined {
  return FIELD_DESCRIPTIONS[section]?.[field]
}

export function detectActivePreset(
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

export interface DiffRow { key: string; section: string; label: string; current: unknown; target: unknown; changed: boolean }

export function buildPresetDiff(settings: Record<string, unknown>, preset: PresetInfo): DiffRow[] {
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

/* ═══════════════════════════════════════════════════════════════════════════
   Shared styles
   ═══════════════════════════════════════════════════════════════════════════ */

export function btnStyle(bg: string): React.CSSProperties {
  return {
    display: 'inline-flex', alignItems: 'center', gap: 5,
    background: bg, color: '#e6edf3', border: '1px solid transparent',
    borderRadius: 6, padding: '6px 14px', fontSize: 12, fontWeight: 500,
    cursor: 'pointer', transition: 'all 0.15s',
    borderColor: bg === '#238636' ? '#238636' : '#30363d',
  }
}

export const codeStyle: React.CSSProperties = {
  fontSize: 11, color: '#79c0ff', background: '#161b22', padding: '1px 5px',
  borderRadius: 3, fontFamily: 'var(--font-mono, monospace)',
}

export const inputBase: React.CSSProperties = {
  background: '#0d1117', color: '#e6edf3', border: '1px solid #30363d',
  borderRadius: 6, padding: '5px 10px', fontSize: 13, outline: 'none',
}

/* ═══════════════════════════════════════════════════════════════════════════
   Section ordering
   ═══════════════════════════════════════════════════════════════════════════ */

export const SECTION_ORDER = [
  'absolute_rules', 'trading', 'risk', 'rotation', 'fees', 'high_stakes',
  'routing', 'telegram', 'news', 'fear_greed', 'multi_timeframe',
  'llm', 'analysis', 'logging', 'journal', 'audit', 'health', 'dashboard',
]

/* ═══════════════════════════════════════════════════════════════════════════
   Section summary chips — key fields shown in collapsed headers
   ═══════════════════════════════════════════════════════════════════════════ */

export const SECTION_SUMMARY: Record<string, Array<{ key: string; label: string }>> = {
  absolute_rules: [
    { key: 'max_single_trade', label: 'Max Trade' },
    { key: 'max_daily_loss', label: 'Max Loss/Day' },
    { key: 'require_approval_above', label: 'Approval >' },
  ],
  trading: [
    { key: 'mode', label: 'Mode' },
    { key: 'interval', label: 'Interval' },
    { key: 'max_open_positions', label: 'Open Pos' },
    { key: 'max_active_pairs', label: 'Active Pairs' },
  ],
  risk: [
    { key: 'stop_loss_pct', label: 'Stop Loss' },
    { key: 'take_profit_pct', label: 'Take Profit' },
    { key: 'max_drawdown_pct', label: 'Drawdown' },
  ],
  rotation: [
    { key: 'enabled', label: 'Enabled' },
    { key: 'autonomous_allocation_pct', label: 'Auto Alloc' },
  ],
  fees: [
    { key: 'trade_fee_pct', label: 'Taker Fee' },
    { key: 'min_gain_after_fees_pct', label: 'Min Gain' },
  ],
  high_stakes: [
    { key: 'min_confidence', label: 'Min Conf' },
    { key: 'trade_size_multiplier', label: 'Size ×' },
  ],
  telegram: [
    { key: 'notify_on_trade', label: 'Trades' },
    { key: 'notify_on_signal', label: 'Signals' },
    { key: 'notify_on_price_move', label: 'Price Moves' },
    { key: 'notify_morning_plan', label: 'Morning' },
  ],
  llm: [
    { key: 'temperature', label: 'Temp' },
    { key: 'max_tokens', label: 'Max Tokens' },
  ],
  news: [
    { key: 'fetch_interval', label: 'Fetch Every' },
    { key: 'articles_for_analysis', label: 'Articles' },
  ],
  multi_timeframe: [
    { key: 'enabled', label: 'Enabled' },
    { key: 'min_alignment', label: 'Min Align' },
  ],
  fear_greed: [{ key: 'enabled', label: 'Enabled' }],
  logging: [{ key: 'level', label: 'Level' }, { key: 'file_enabled', label: 'File Logs' }],
  health: [{ key: 'port', label: 'Port' }],
  dashboard: [{ key: 'enabled', label: 'Enabled' }, { key: 'port', label: 'Port' }],
}

export function formatSummaryValue(key: string, val: unknown): string {
  if (val === null || val === undefined) return '—'
  if (typeof val === 'boolean') return val ? 'On' : 'Off'
  if (key.endsWith('_pct') && typeof val === 'number') return `${(val * 100).toFixed(1)}%`
  if ((key === 'interval' || key === 'fetch_interval') && typeof val === 'number') return `${val}s`
  if (key === 'status_update_interval' && typeof val === 'number') return `${Math.round(val / 60)}m`
  if (typeof val === 'number') {
    if (['max_single_trade', 'max_daily_spend', 'max_daily_loss', 'require_approval_above',
         'approval_threshold', 'auto_approve_up_to'].includes(key)) return `€${val}`
    return String(val)
  }
  return String(val)
}
