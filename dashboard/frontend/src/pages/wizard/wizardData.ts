/**
 * wizardData.ts — Types, constants, validation, and generator logic for the Setup Wizard.
 * Extracted from SetupWizard.tsx to keep files under 1000 lines.
 */
import { type CSSProperties } from 'react'

/* ═══════════════════════════════════════════════════════════════════════════
   Types & Constants
   ═══════════════════════════════════════════════════════════════════════════ */

export const STORAGE_KEY = 'auto_traitor_setup_wizard'

export interface WizardState {
  exchanges: { coinbase: boolean; ibkr: boolean }
  tradingMode: 'paper' | 'live'
  liveConfirmed: boolean
  cryptoPairs: string[]
  customCryptoPair: string
  ibkrPairs: string[]
  customIbkrPair: string
  coinbaseApiKey: string
  coinbaseApiSecret: string
  ibkrHost: string
  ibkrPort: string
  ibkrClientId: string
  ibkrCurrency: string
  geminiEnabled: boolean
  geminiApiKey: string
  openrouterEnabled: boolean
  openrouterApiKey: string
  openaiEnabled: boolean
  openaiApiKey: string
  groqEnabled: boolean
  groqApiKey: string
  ollamaModel: string
  telegramEnabled: boolean
  telegramUserId: string
  telegramAdditionalUsers: string
  telegramCoinbaseBotToken: string
  telegramCoinbaseChatId: string
  telegramIbkrBotToken: string
  telegramIbkrChatId: string
  redditEnabled: boolean
  redditClientId: string
  redditClientSecret: string
  redditUserAgent: string
  /** Infrastructure secrets loaded from server — preserved on re-save */
  infraSecrets: Record<string, string>
}

export const INITIAL_STATE: WizardState = {
  exchanges: { coinbase: true, ibkr: false },
  tradingMode: 'paper',
  liveConfirmed: false,
  cryptoPairs: ['BTC-EUR', 'ETH-EUR', 'SOL-EUR', 'LINK-EUR', 'DOGE-EUR'],
  customCryptoPair: '',
  ibkrPairs: ['AAPL-USD', 'MSFT-USD', 'GOOGL-USD'],
  customIbkrPair: '',
  coinbaseApiKey: '',
  coinbaseApiSecret: '',
  ibkrHost: '127.0.0.1',
  ibkrPort: '4002',
  ibkrClientId: '1',
  ibkrCurrency: 'USD',
  geminiEnabled: false,
  geminiApiKey: '',
  openrouterEnabled: false,
  openrouterApiKey: '',
  openaiEnabled: false,
  openaiApiKey: '',
  groqEnabled: false,
  groqApiKey: '',
  ollamaModel: 'qwen2.5:14b',
  telegramEnabled: true,
  telegramUserId: '',
  telegramAdditionalUsers: '',
  telegramCoinbaseBotToken: '',
  telegramCoinbaseChatId: '',
  telegramIbkrBotToken: '',
  telegramIbkrChatId: '',
  redditEnabled: false,
  redditClientId: '',
  redditClientSecret: '',
  redditUserAgent: 'auto-traitor/1.0',
  infraSecrets: {},
}

/* ═══════════════════════════════════════════════════════════════════════════
   Data Catalogs
   ═══════════════════════════════════════════════════════════════════════════ */

export const POPULAR_CRYPTO = [
  { id: 'BTC-EUR', name: 'Bitcoin', symbol: 'BTC' },
  { id: 'ETH-EUR', name: 'Ethereum', symbol: 'ETH' },
  { id: 'SOL-EUR', name: 'Solana', symbol: 'SOL' },
  { id: 'ADA-EUR', name: 'Cardano', symbol: 'ADA' },
  { id: 'DOGE-EUR', name: 'Dogecoin', symbol: 'DOGE' },
  { id: 'AVAX-EUR', name: 'Avalanche', symbol: 'AVAX' },
  { id: 'LINK-EUR', name: 'Chainlink', symbol: 'LINK' },
  { id: 'DOT-EUR', name: 'Polkadot', symbol: 'DOT' },
  { id: 'ATOM-EUR', name: 'Cosmos', symbol: 'ATOM' },
  { id: 'MATIC-EUR', name: 'Polygon', symbol: 'MATIC' },
  { id: 'UNI-EUR', name: 'Uniswap', symbol: 'UNI' },
  { id: 'XRP-EUR', name: 'Ripple', symbol: 'XRP' },
  { id: 'LTC-EUR', name: 'Litecoin', symbol: 'LTC' },
  { id: 'NEAR-EUR', name: 'NEAR Protocol', symbol: 'NEAR' },
  { id: 'FIL-EUR', name: 'Filecoin', symbol: 'FIL' },
]


export const POPULAR_IBKR_STOCKS = [
  { id: 'AAPL-USD', name: 'Apple', sector: 'Technology' },
  { id: 'MSFT-USD', name: 'Microsoft', sector: 'Technology' },
  { id: 'GOOGL-USD', name: 'Alphabet', sector: 'Technology' },
  { id: 'AMZN-USD', name: 'Amazon', sector: 'Consumer' },
  { id: 'NVDA-USD', name: 'NVIDIA', sector: 'Technology' },
  { id: 'META-USD', name: 'Meta Platforms', sector: 'Technology' },
  { id: 'TSLA-USD', name: 'Tesla', sector: 'Automotive' },
  { id: 'JPM-USD', name: 'JPMorgan Chase', sector: 'Finance' },
  { id: 'V-USD', name: 'Visa', sector: 'Finance' },
  { id: 'JNJ-USD', name: 'Johnson & Johnson', sector: 'Healthcare' },
  { id: 'WMT-USD', name: 'Walmart', sector: 'Retail' },
  { id: 'UNH-USD', name: 'UnitedHealth', sector: 'Healthcare' },
]

export const OLLAMA_MODELS = [
  { id: 'qwen2.5:7b', name: 'Qwen 2.5 7B', desc: 'Fast, lower quality', vram: '~4 GB' },
  { id: 'qwen2.5:14b', name: 'Qwen 2.5 14B', desc: 'Balanced (recommended)', vram: '~8 GB', recommended: true },
  { id: 'qwen2.5:32b', name: 'Qwen 2.5 32B', desc: 'Best quality, slow', vram: '~18 GB' },
  { id: 'llama3.1:8b', name: 'Llama 3.1 8B', desc: 'Good alternative', vram: '~5 GB' },
]

/* ═══════════════════════════════════════════════════════════════════════════
   Shared Styles & CSS
   ═══════════════════════════════════════════════════════════════════════════ */

export const WIZARD_CSS = `
@keyframes at-fade-in { from { opacity: 0; transform: translateY(12px); } to { opacity: 1; transform: translateY(0); } }
@keyframes at-fade-out { from { opacity: 1; transform: translateY(0); } to { opacity: 0; transform: translateY(-8px); } }
@keyframes at-pulse-ring { 0% { box-shadow: 0 0 0 0 rgba(34,197,94,0.4); } 70% { box-shadow: 0 0 0 12px rgba(34,197,94,0); } 100% { box-shadow: 0 0 0 0 rgba(34,197,94,0); } }
@keyframes at-spin { to { transform: rotate(360deg); } }
@keyframes at-check-pop { 0% { transform: scale(0.6); opacity: 0; } 50% { transform: scale(1.15); } 100% { transform: scale(1); opacity: 1; } }
@keyframes at-gradient-shift { 0% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } 100% { background-position: 0% 50%; } }
.at-step-enter { animation: at-fade-in 0.3s ease-out both; }
.at-card-hover:hover { border-color: #484f58 !important; background: #1c2129 !important; }
.at-card-hover { transition: all 0.2s ease !important; }
.at-input:focus { border-color: #22c55e !important; box-shadow: 0 0 0 2px rgba(34,197,94,0.15); }
.at-input { transition: border-color 0.15s, box-shadow 0.15s; }
.at-copy-btn:hover { background: #30363d !important; }
`

export const card: CSSProperties = { background: '#161b22', border: '1px solid #30363d', borderRadius: 12, padding: 24 }
export const inputBase: CSSProperties = {
  width: '100%', padding: '10px 14px', background: '#0d1117', border: '1px solid #30363d',
  borderRadius: 8, color: '#e6edf3', fontSize: 14, fontFamily: 'inherit', outline: 'none',
}
export const mono: CSSProperties = { ...inputBase, fontFamily: "'JetBrains Mono', monospace", fontSize: 13 }

/* ═══════════════════════════════════════════════════════════════════════════
   Validation
   ═══════════════════════════════════════════════════════════════════════════ */

export function isValidTelegramToken(token: string): boolean {
  return /^\d{8,12}:[A-Za-z0-9_-]{30,50}$/.test(token.trim())
}

export type StepValidation = { ok: boolean; issues: string[] }

export function validateStep(stepId: string, state: WizardState): StepValidation {
  const issues: string[] = []
  switch (stepId) {
    case 'exchange':
      if (!state.exchanges.coinbase && !state.exchanges.ibkr) issues.push('Select at least one exchange')
      break
    case 'mode':
      if (state.tradingMode === 'live' && !state.liveConfirmed) issues.push('Confirm live trading risks')
      break
    case 'assets':
      if (state.exchanges.coinbase && state.cryptoPairs.length === 0) issues.push('Select crypto pairs')
      if (state.exchanges.ibkr && state.ibkrPairs.length === 0) issues.push('Select IBKR stock pairs')
      break
    case 'coinbase':
      if (state.tradingMode === 'live' && (!state.coinbaseApiKey || !state.coinbaseApiSecret))
        issues.push('API credentials required for live trading')
      break
    case 'ibkr':
      if (!state.ibkrHost) issues.push('IB Gateway host required')
      if (!state.ibkrPort) issues.push('IB Gateway port required')
      break
    case 'llm':
      if (state.geminiEnabled && !state.geminiApiKey) issues.push('Gemini API key missing')
      if (state.openrouterEnabled && !state.openrouterApiKey) issues.push('OpenRouter API key missing')
      if (state.openaiEnabled && !state.openaiApiKey) issues.push('OpenAI API key missing')
      if (state.groqEnabled && !state.groqApiKey) issues.push('Groq API key missing')
      break
    case 'telegram':
      if (state.telegramEnabled) {
        if (!state.telegramUserId) issues.push('User ID required')
        if (state.exchanges.coinbase && !state.telegramCoinbaseBotToken) issues.push('Coinbase bot token required')
        if (state.exchanges.ibkr && !state.telegramIbkrBotToken) issues.push('IBKR bot token required')
        if (state.telegramCoinbaseBotToken && !isValidTelegramToken(state.telegramCoinbaseBotToken)) issues.push('Coinbase token format invalid')
        if (state.telegramIbkrBotToken && !isValidTelegramToken(state.telegramIbkrBotToken)) issues.push('IBKR token format invalid')
      }
      break
    case 'news':
      if (state.redditEnabled && !state.redditClientId) issues.push('Reddit Client ID missing')
      break
  }
  return { ok: issues.length === 0, issues }
}

/* ═══════════════════════════════════════════════════════════════════════════
   Env File Generators
   ═══════════════════════════════════════════════════════════════════════════ */

export function generateEnvContent(state: WizardState): string {
  const lines: string[] = []
  const add = (key: string, value: string, comment?: string) => { if (comment) lines.push(`# ${comment}`); lines.push(`${key}=${value}`) }
  const blank = () => lines.push('')

  lines.push('# ===========================================', '# Auto-Traitor Environment Configuration',
    `# Generated: ${new Date().toISOString().replace('T', ' ').slice(0, 19)}`, '# Generated by: Setup Wizard (web)',
    '# ===========================================')
  blank()
  add('TRADING_MODE', state.tradingMode, 'Trading mode: paper or live')
  if (state.tradingMode === 'live' && state.liveConfirmed) add('LIVE_TRADING_CONFIRMED', 'I UNDERSTAND THE RISKS', 'Headless live mode confirmation')
  blank()
  if (state.exchanges.coinbase) {
    add('COINBASE_API_KEY', state.coinbaseApiKey, 'Coinbase Advanced Trade — API Key Name')
    add('COINBASE_API_SECRET', state.coinbaseApiSecret, 'Coinbase Advanced Trade — EC Private Key (PEM)')
    blank()
  }
  if (state.exchanges.ibkr) {
    add('IBKR_HOST', state.ibkrHost, 'Interactive Brokers — IB Gateway / TWS connection')
    add('IBKR_PORT', state.ibkrPort)
    add('IBKR_CLIENT_ID', state.ibkrClientId)
    add('IBKR_CURRENCY', state.ibkrCurrency)
    blank()
  }
  if (state.geminiEnabled && state.geminiApiKey) add('GEMINI_API_KEY', state.geminiApiKey, 'Google Gemini API (provider 1 — fastest)')
  else lines.push('# GEMINI_API_KEY= (not configured)')
  blank()
  if (state.openrouterEnabled && state.openrouterApiKey) add('OPENROUTER_API_KEY', state.openrouterApiKey, 'OpenRouter API (provider 2 — 200+ models, free tier)')
  else lines.push('# OPENROUTER_API_KEY= (not configured)')
  blank()
  if (state.openaiEnabled && state.openaiApiKey) add('OPENAI_API_KEY', state.openaiApiKey, 'OpenAI API (provider 3 — fallback)')
  else lines.push('# OPENAI_API_KEY= (not configured)')
  blank()
  if (state.groqEnabled && state.groqApiKey) add('GROQ_API_KEY', state.groqApiKey, 'Groq API (free tier — llama-3.3-70b + llama-4-maverick)')
  else lines.push('# GROQ_API_KEY= (not configured)')
  blank()
  add('OLLAMA_MODEL', state.ollamaModel, 'Ollama LLM model')
  add('OLLAMA_BASE_URL', 'http://ollama:11434', 'Ollama URL (Docker internal)')
  blank()
  if (state.telegramEnabled && state.telegramUserId) {
    const all = [state.telegramUserId, ...state.telegramAdditionalUsers.split(',').map(s => s.trim()).filter(Boolean)].join(',')
    add('TELEGRAM_AUTHORIZED_USERS', all, 'SECURITY: Only these user IDs can control the bot')
    blank()
    if (state.exchanges.coinbase && state.telegramCoinbaseBotToken) {
      const cid = state.telegramCoinbaseChatId || state.telegramUserId
      add('TELEGRAM_BOT_TOKEN_COINBASE', state.telegramCoinbaseBotToken, 'Telegram Bot — Coinbase agent')
      add('TELEGRAM_CHAT_ID_COINBASE', cid)
      add('TELEGRAM_BOT_TOKEN', state.telegramCoinbaseBotToken, 'Generic fallback')
      add('TELEGRAM_CHAT_ID', cid)
      blank()
    }
    if (state.exchanges.ibkr && state.telegramIbkrBotToken) {
      const cid = state.telegramIbkrChatId || state.telegramUserId
      add('TELEGRAM_BOT_TOKEN_IBKR', state.telegramIbkrBotToken, 'Telegram Bot — IBKR agent')
      add('TELEGRAM_CHAT_ID_IBKR', cid)
      if (!state.exchanges.coinbase) { add('TELEGRAM_BOT_TOKEN', state.telegramIbkrBotToken, 'Generic fallback'); add('TELEGRAM_CHAT_ID', cid) }
      blank()
    }
  } else { lines.push('# TELEGRAM_BOT_TOKEN= (not configured)', '# TELEGRAM_CHAT_ID=', '# TELEGRAM_AUTHORIZED_USERS='); blank() }
  if (state.redditEnabled && state.redditClientId) { add('REDDIT_CLIENT_ID', state.redditClientId, 'Reddit API'); add('REDDIT_CLIENT_SECRET', state.redditClientSecret); add('REDDIT_USER_AGENT', state.redditUserAgent) }
  else lines.push('# REDDIT_CLIENT_ID= (not configured)', '# REDDIT_CLIENT_SECRET=')
  blank()
  const rs = (n: number) => { const c = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'; const a = new Uint8Array(n); crypto.getRandomValues(a); return Array.from(a).map(b => c[b % c.length]).join('') }
  const rh = (n: number) => { const a = new Uint8Array(n); crypto.getRandomValues(a); return Array.from(a).map(b => b.toString(16).padStart(2, '0')).join('') }
  // Reuse existing infrastructure secrets if loaded from server, otherwise generate new ones
  const s = state.infraSecrets || {}
  const rp = s.REDIS_PASSWORD || rs(32)
  add('REDIS_PASSWORD', rp, 'Redis (auto-generated)'); add('REDIS_URL', s.REDIS_URL || `redis://default:${rp}@redis:6379/0`); blank()
  add('TEMPORAL_DB_USER', s.TEMPORAL_DB_USER || 'temporal', 'Temporal (auto-generated)')
  add('TEMPORAL_DB_PASSWORD', s.TEMPORAL_DB_PASSWORD || rs(32))
  add('TEMPORAL_DB_NAME', s.TEMPORAL_DB_NAME || 'temporal'); blank()
  add('LANGFUSE_DB_PASSWORD', s.LANGFUSE_DB_PASSWORD || rs(32), 'Langfuse (auto-generated)')
  add('LANGFUSE_NEXTAUTH_SECRET', s.LANGFUSE_NEXTAUTH_SECRET || rs(48))
  add('LANGFUSE_SALT', s.LANGFUSE_SALT || rs(48))
  add('LANGFUSE_ADMIN_PASSWORD', s.LANGFUSE_ADMIN_PASSWORD || rs(20))
  add('LANGFUSE_PUBLIC_KEY', s.LANGFUSE_PUBLIC_KEY || 'at-public-key', 'Langfuse project init keys')
  add('LANGFUSE_SECRET_KEY', s.LANGFUSE_SECRET_KEY || 'at-secret-key'); blank()
  add('CLICKHOUSE_PASSWORD', s.CLICKHOUSE_PASSWORD || rs(32), 'Langfuse v3 — ClickHouse + MinIO (auto-generated)')
  add('MINIO_ROOT_USER', s.MINIO_ROOT_USER || 'minio')
  add('MINIO_ROOT_PASSWORD', s.MINIO_ROOT_PASSWORD || rs(32))
  add('LANGFUSE_ENCRYPTION_KEY', s.LANGFUSE_ENCRYPTION_KEY || rh(32)); blank()
  return lines.join('\n')
}

export function generateRootEnvContent(state: WizardState, configEnv: string): string {
  const p = (key: string) => configEnv.match(new RegExp(`^${key}=(.*)$`, 'm'))?.[1] || ''
  return [
    '# Docker Compose variable substitution — generated by setup wizard', '',
    `OLLAMA_MODEL=${state.ollamaModel}`, '',
    `LANGFUSE_NEXTAUTH_SECRET=${p('LANGFUSE_NEXTAUTH_SECRET')}`, `LANGFUSE_SALT=${p('LANGFUSE_SALT')}`,
    `LANGFUSE_ADMIN_PASSWORD=${p('LANGFUSE_ADMIN_PASSWORD')}`, 'LANGFUSE_ADMIN_EMAIL=admin@auto-traitor.local',
    'LANGFUSE_ADMIN_NAME=admin', `LANGFUSE_DB_PASSWORD=${p('LANGFUSE_DB_PASSWORD')}`,
    `LANGFUSE_PUBLIC_KEY=${p('LANGFUSE_PUBLIC_KEY')}`, `LANGFUSE_SECRET_KEY=${p('LANGFUSE_SECRET_KEY')}`, '',
    `REDIS_PASSWORD=${p('REDIS_PASSWORD')}`, '',
    `TEMPORAL_DB_USER=${p('TEMPORAL_DB_USER')}`, `TEMPORAL_DB_PASSWORD=${p('TEMPORAL_DB_PASSWORD')}`,
    `TEMPORAL_DB_NAME=${p('TEMPORAL_DB_NAME')}`, '',
    `CLICKHOUSE_PASSWORD=${p('CLICKHOUSE_PASSWORD')}`, `MINIO_ROOT_USER=${p('MINIO_ROOT_USER')}`,
    `MINIO_ROOT_PASSWORD=${p('MINIO_ROOT_PASSWORD')}`, `LANGFUSE_ENCRYPTION_KEY=${p('LANGFUSE_ENCRYPTION_KEY')}`,
  ].join('\n')
}
