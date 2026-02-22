import { useState, useCallback, useMemo, useEffect, useRef, type ReactNode, type CSSProperties } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  ArrowRight, ArrowLeft, Check, AlertTriangle, Eye, EyeOff, Plus, X, Download,
  Zap, Server, Cloud, Bot, Coins, BarChart3, Newspaper,
  Lock, Info, Sparkles, Settings2, Copy, ExternalLink,
  MonitorDot, MessageSquare, TrendingUp, Rocket, Shield,
  ChevronDown, ChevronRight, RefreshCw, CircleAlert, CheckCircle2,
  KeyRound, Terminal, Globe,
} from 'lucide-react'

/* ═══════════════════════════════════════════════════════════════════════════
   Constants & Data
   ═══════════════════════════════════════════════════════════════════════════ */

const STORAGE_KEY = 'auto_traitor_setup_wizard'

interface WizardState {
  exchanges: { coinbase: boolean; nordnet: boolean; ibkr: boolean }
  tradingMode: 'paper' | 'live'
  liveConfirmed: boolean
  cryptoPairs: string[]
  customCryptoPair: string
  stockPairs: string[]
  customStockPair: string
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
  ollamaModel: string
  telegramEnabled: boolean
  telegramUserId: string
  telegramAdditionalUsers: string
  telegramCoinbaseBotToken: string
  telegramCoinbaseChatId: string
  telegramNordnetBotToken: string
  telegramNordnetChatId: string
  telegramIbkrBotToken: string
  telegramIbkrChatId: string
  redditEnabled: boolean
  redditClientId: string
  redditClientSecret: string
  redditUserAgent: string
  /** Infrastructure secrets loaded from server — preserved on re-save */
  infraSecrets: Record<string, string>
}

const INITIAL_STATE: WizardState = {
  exchanges: { coinbase: true, nordnet: false, ibkr: false },
  tradingMode: 'paper',
  liveConfirmed: false,
  cryptoPairs: ['BTC-EUR', 'ETH-EUR'],
  customCryptoPair: '',
  stockPairs: ['VOLV-B.ST', 'ERIC-B.ST', 'ABB.ST'],
  customStockPair: '',
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
  ollamaModel: 'qwen2.5:14b',
  telegramEnabled: true,
  telegramUserId: '',
  telegramAdditionalUsers: '',
  telegramCoinbaseBotToken: '',
  telegramCoinbaseChatId: '',
  telegramNordnetBotToken: '',
  telegramNordnetChatId: '',
  telegramIbkrBotToken: '',
  telegramIbkrChatId: '',
  redditEnabled: false,
  redditClientId: '',
  redditClientSecret: '',
  redditUserAgent: 'auto-traitor/1.0',
  infraSecrets: {},
}

const POPULAR_CRYPTO = [
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

const POPULAR_STOCKS = [
  { id: 'VOLV-B.ST', name: 'Volvo B', sector: 'Industrials' },
  { id: 'ERIC-B.ST', name: 'Ericsson B', sector: 'Telecom' },
  { id: 'ABB.ST', name: 'ABB Ltd', sector: 'Industrials' },
  { id: 'HM-B.ST', name: 'H&M B', sector: 'Retail' },
  { id: 'SEB-A.ST', name: 'SEB A', sector: 'Finance' },
  { id: 'SWED-A.ST', name: 'Swedbank A', sector: 'Finance' },
  { id: 'ATCO-A.ST', name: 'Atlas Copco A', sector: 'Industrials' },
  { id: 'SAND.ST', name: 'Sandvik', sector: 'Industrials' },
  { id: 'HEXA-B.ST', name: 'Hexagon B', sector: 'Technology' },
  { id: 'INVE-B.ST', name: 'Investor B', sector: 'Finance' },
  { id: 'ASSA-B.ST', name: 'ASSA ABLOY B', sector: 'Industrials' },
  { id: 'TELIA.ST', name: 'Telia Company', sector: 'Telecom' },
]

const POPULAR_IBKR_STOCKS = [
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

const OLLAMA_MODELS = [
  { id: 'qwen2.5:7b', name: 'Qwen 2.5 7B', desc: 'Fast, lower quality', vram: '~4 GB' },
  { id: 'qwen2.5:14b', name: 'Qwen 2.5 14B', desc: 'Balanced (recommended)', vram: '~8 GB', recommended: true },
  { id: 'qwen2.5:32b', name: 'Qwen 2.5 32B', desc: 'Best quality, slow', vram: '~18 GB' },
  { id: 'llama3.1:8b', name: 'Llama 3.1 8B', desc: 'Good alternative', vram: '~5 GB' },
]

/* ═══════════════════════════════════════════════════════════════════════════
   Shared Styles & Keyframe CSS (injected once)
   ═══════════════════════════════════════════════════════════════════════════ */

const WIZARD_CSS = `
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

function useInjectCSS() {
  useEffect(() => {
    const id = 'at-setup-wizard-css'
    if (document.getElementById(id)) return
    const style = document.createElement('style')
    style.id = id
    style.textContent = WIZARD_CSS
    document.head.appendChild(style)
    return () => { style.remove() }
  }, [])
}

/* ═══════════════════════════════════════════════════════════════════════════
   Shared UI Components
   ═══════════════════════════════════════════════════════════════════════════ */

const card: CSSProperties = { background: '#161b22', border: '1px solid #30363d', borderRadius: 12, padding: 24 }
const inputBase: CSSProperties = {
  width: '100%', padding: '10px 14px', background: '#0d1117', border: '1px solid #30363d',
  borderRadius: 8, color: '#e6edf3', fontSize: 14, fontFamily: 'inherit', outline: 'none',
}
const mono: CSSProperties = { ...inputBase, fontFamily: "'JetBrains Mono', monospace", fontSize: 13 }

function Tip({ children }: { children: ReactNode }) {
  return (
    <div style={{
      display: 'flex', gap: 10, alignItems: 'flex-start', padding: '12px 16px',
      background: 'rgba(34,197,94,0.06)', border: '1px solid rgba(34,197,94,0.15)',
      borderRadius: 10, fontSize: 13, color: '#8b949e', lineHeight: 1.6,
    }}>
      <Info size={16} style={{ color: '#22c55e', flexShrink: 0, marginTop: 2 }} />
      <div>{children}</div>
    </div>
  )
}

function Warning({ children }: { children: ReactNode }) {
  return (
    <div style={{
      display: 'flex', gap: 10, alignItems: 'flex-start', padding: '12px 16px',
      background: 'rgba(234,179,8,0.06)', border: '1px solid rgba(234,179,8,0.15)',
      borderRadius: 10, fontSize: 13, color: '#eab308', lineHeight: 1.6,
    }}>
      <AlertTriangle size={16} style={{ flexShrink: 0, marginTop: 2 }} />
      <div>{children}</div>
    </div>
  )
}

function SecurityBox({ children }: { children: ReactNode }) {
  return (
    <div style={{
      display: 'flex', gap: 12, alignItems: 'flex-start', padding: '16px 18px',
      background: 'rgba(239,68,68,0.05)', border: '1px solid rgba(239,68,68,0.2)',
      borderRadius: 12, fontSize: 13, color: '#fca5a5', lineHeight: 1.6,
    }}>
      <Lock size={18} style={{ color: '#ef4444', flexShrink: 0, marginTop: 2 }} />
      <div style={{ flex: 1 }}>{children}</div>
    </div>
  )
}

function HowTo({ title, steps, link }: { title: string; steps: string[]; link?: { url: string; label: string } }) {
  const [open, setOpen] = useState(false)
  return (
    <div style={{ ...card, padding: 0, overflow: 'hidden' }}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        style={{
          width: '100%', padding: '14px 18px', background: 'transparent', border: 'none',
          color: '#c9d1d9', fontSize: 14, fontWeight: 600, cursor: 'pointer',
          display: 'flex', alignItems: 'center', gap: 10, textAlign: 'left',
        }}
      >
        {open ? <ChevronDown size={16} color="#22c55e" /> : <ChevronRight size={16} color="#6e7681" />}
        {title}
        {link && (
          <a
            href={link.url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={e => e.stopPropagation()}
            style={{
              marginLeft: 'auto', fontSize: 12, color: '#58a6ff',
              display: 'flex', alignItems: 'center', gap: 4, textDecoration: 'none',
            }}
          >
            {link.label} <ExternalLink size={12} />
          </a>
        )}
      </button>
      {open && (
        <div style={{ padding: '0 18px 16px 18px' }}>
          <ol style={{ margin: 0, paddingLeft: 20, display: 'flex', flexDirection: 'column', gap: 6 }}>
            {steps.map((s, i) => (
              <li key={i} style={{ fontSize: 13, color: '#8b949e', lineHeight: 1.5 }}>{s}</li>
            ))}
          </ol>
        </div>
      )}
    </div>
  )
}

function PasswordInput({ value, onChange, placeholder, useMono, className }: {
  value: string; onChange: (v: string) => void; placeholder?: string; useMono?: boolean; className?: string
}) {
  const [visible, setVisible] = useState(false)
  return (
    <div style={{ position: 'relative' }}>
      <input
        type={visible ? 'text' : 'password'}
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        style={useMono ? mono : inputBase}
        className={`at-input ${className || ''}`}
      />
      <button
        type="button"
        onClick={() => setVisible(!visible)}
        style={{
          position: 'absolute', right: 10, top: '50%', transform: 'translateY(-50%)',
          background: 'none', border: 'none', color: '#6e7681', cursor: 'pointer', padding: 4,
        }}
      >
        {visible ? <EyeOff size={16} /> : <Eye size={16} />}
      </button>
    </div>
  )
}

function ToggleChip({ selected, onClick, children, color = '#22c55e' }: {
  selected: boolean; onClick: () => void; children: ReactNode; color?: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        padding: '7px 14px', borderRadius: 20,
        border: `1.5px solid ${selected ? color : '#30363d'}`,
        background: selected ? `${color}15` : 'transparent',
        color: selected ? color : '#8b949e',
        fontSize: 13, fontWeight: selected ? 600 : 400,
        cursor: 'pointer', transition: 'all 0.15s',
        display: 'flex', alignItems: 'center', gap: 6,
      }}
    >
      {selected && <Check size={13} />}
      {children}
    </button>
  )
}

function SectionHeader({ icon, title, subtitle }: { icon: ReactNode; title: string; subtitle: string }) {
  return (
    <div style={{ marginBottom: 28 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 8 }}>
        <div style={{
          width: 44, height: 44, borderRadius: 11,
          background: 'rgba(34,197,94,0.08)', border: '1px solid rgba(34,197,94,0.15)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#22c55e',
        }}>{icon}</div>
        <h2 style={{ margin: 0, fontSize: 24, fontWeight: 800, color: '#e6edf3', letterSpacing: -0.3 }}>{title}</h2>
      </div>
      <p style={{ margin: 0, fontSize: 14, color: '#8b949e', lineHeight: 1.6, paddingLeft: 58 }}>{subtitle}</p>
    </div>
  )
}

function FormField({ label, help, required, children }: {
  label: string; help?: string; required?: boolean; children: ReactNode
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <label style={{ fontSize: 13, fontWeight: 600, color: '#c9d1d9' }}>
        {label}
        {required && <span style={{ color: '#ef4444', marginLeft: 4 }}>*</span>}
      </label>
      {help && <span style={{ fontSize: 12, color: '#6e7681', lineHeight: 1.4 }}>{help}</span>}
      {children}
    </div>
  )
}

function ValidationBadge({ valid, label }: { valid: boolean; label: string }) {
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 4,
      padding: '2px 8px', borderRadius: 6, fontSize: 11, fontWeight: 600,
      background: valid ? 'rgba(34,197,94,0.1)' : 'rgba(234,179,8,0.1)',
      color: valid ? '#4ade80' : '#eab308',
    }}>
      {valid ? <CheckCircle2 size={11} /> : <CircleAlert size={11} />}
      {label}
    </span>
  )
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      type="button"
      className="at-copy-btn"
      onClick={() => {
        navigator.clipboard.writeText(text)
        setCopied(true)
        setTimeout(() => setCopied(false), 1500)
      }}
      style={{
        background: '#21262d', border: 'none', borderRadius: 6,
        color: copied ? '#4ade80' : '#8b949e', cursor: 'pointer',
        padding: '4px 8px', display: 'flex', alignItems: 'center', gap: 4,
        fontSize: 11, fontWeight: 600, transition: 'all 0.15s',
      }}
    >
      {copied ? <><Check size={12} /> Copied</> : <><Copy size={12} /> Copy</>}
    </button>
  )
}

function SkipLink({ onClick, label = 'Skip this step' }: { onClick: () => void; label?: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        background: 'none', border: 'none', color: '#6e7681', cursor: 'pointer',
        fontSize: 13, padding: '6px 0', display: 'flex', alignItems: 'center', gap: 6,
        textDecoration: 'underline', textDecorationColor: '#30363d', textUnderlineOffset: 3,
      }}
    >
      <ArrowRight size={14} /> {label}
    </button>
  )
}

/* ═══════════════════════════════════════════════════════════════════════════
   Validation helpers
   ═══════════════════════════════════════════════════════════════════════════ */

function isValidTelegramToken(token: string): boolean {
  return /^\d{8,12}:[A-Za-z0-9_-]{30,50}$/.test(token.trim())
}

type StepValidation = { ok: boolean; issues: string[] }

function validateStep(stepId: string, state: WizardState): StepValidation {
  const issues: string[] = []
  switch (stepId) {
    case 'exchange':
      if (!state.exchanges.coinbase && !state.exchanges.nordnet && !state.exchanges.ibkr) issues.push('Select at least one exchange')
      if (state.exchanges.nordnet && state.exchanges.ibkr) issues.push('Pick one shares broker — NordNet or IBKR, not both')
      break
    case 'mode':
      if (state.tradingMode === 'live' && !state.liveConfirmed) issues.push('Confirm live trading risks')
      break
    case 'assets':
      if (state.exchanges.coinbase && state.cryptoPairs.length === 0) issues.push('Select crypto pairs')
      if (state.exchanges.nordnet && state.stockPairs.length === 0) issues.push('Select stock pairs')
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
      break
    case 'telegram':
      if (state.telegramEnabled) {
        if (!state.telegramUserId) issues.push('User ID required')
        if (state.exchanges.coinbase && !state.telegramCoinbaseBotToken) issues.push('Coinbase bot token required')
        if (state.exchanges.nordnet && !state.telegramNordnetBotToken) issues.push('Nordnet bot token required')
        if (state.exchanges.ibkr && !state.telegramIbkrBotToken) issues.push('IBKR bot token required')
        if (state.telegramCoinbaseBotToken && !isValidTelegramToken(state.telegramCoinbaseBotToken)) issues.push('Coinbase token format invalid')
        if (state.telegramNordnetBotToken && !isValidTelegramToken(state.telegramNordnetBotToken)) issues.push('Nordnet token format invalid')
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
   Step: Welcome
   ═══════════════════════════════════════════════════════════════════════════ */

function StepWelcome({ onStart }: { onStart: () => void }) {
  return (
    <div style={{ textAlign: 'center', maxWidth: 600, margin: '0 auto', paddingTop: 20 }}>
      <div style={{
        width: 88, height: 88, borderRadius: 22, margin: '0 auto 28px',
        background: 'linear-gradient(135deg, #22c55e, #16a34a, #15803d)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        animation: 'at-pulse-ring 2s ease-out infinite',
        boxShadow: '0 8px 32px rgba(34,197,94,0.25)',
      }}>
        <Sparkles size={40} color="#fff" />
      </div>

      <h1 style={{ margin: '0 0 8px 0', fontSize: 32, fontWeight: 800, color: '#e6edf3', letterSpacing: -0.5 }}>
        Welcome to Auto-Traitor
      </h1>
      <p style={{ margin: '0 0 36px 0', fontSize: 16, color: '#8b949e', lineHeight: 1.7 }}>
        Autonomous LLM-powered multi-asset trading agent.<br />
        This wizard will guide you through the complete setup in a few minutes.
      </p>

      {/* What you'll need */}
      <div style={{ ...card, textAlign: 'left', marginBottom: 28 }}>
        <h3 style={{ margin: '0 0 16px 0', fontSize: 15, fontWeight: 700, color: '#c9d1d9', display: 'flex', alignItems: 'center', gap: 8 }}>
          <KeyRound size={16} color="#22c55e" /> What you'll need
        </h3>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
          {[
            { icon: <BarChart3 size={14} />, text: 'Exchange account (Coinbase + optionally NordNet or IBKR)', required: true },
            { icon: <Sparkles size={14} />, text: 'LLM API key (or use local Ollama)', required: false },
            { icon: <MessageSquare size={14} />, text: 'Telegram account (for alerts)', required: false },
            { icon: <Shield size={14} />, text: 'Docker Desktop installed', required: true },
          ].map((item, i) => (
            <div key={i} style={{
              display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px',
              borderRadius: 8, background: '#0d1117', border: '1px solid #21262d',
            }}>
              <div style={{ color: item.required ? '#22c55e' : '#6e7681' }}>{item.icon}</div>
              <span style={{ fontSize: 13, color: '#c9d1d9' }}>{item.text}</span>
              {!item.required && (
                <span style={{ marginLeft: 'auto', fontSize: 10, color: '#6e7681', fontWeight: 600 }}>OPTIONAL</span>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Architecture overview */}
      <div style={{ ...card, textAlign: 'left', marginBottom: 32 }}>
        <h3 style={{ margin: '0 0 14px 0', fontSize: 15, fontWeight: 700, color: '#c9d1d9', display: 'flex', alignItems: 'center', gap: 8 }}>
          <Globe size={16} color="#22c55e" /> What gets configured
        </h3>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {[
            ['Exchange connection', 'Coinbase crypto + NordNet or IBKR equities'],
            ['Trading mode', 'Paper (simulated) or Live (real money)'],
            ['Asset universe', 'Which crypto & stocks the agent monitors'],
            ['AI brain', 'Multi-provider LLM chain (Gemini / OpenRouter / Ollama)'],
            ['Notifications', 'Telegram bot for alerts, approvals & commands'],
            ['News feeds', 'Reddit + RSS for market sentiment analysis'],
            ['Infrastructure', 'Redis, Langfuse, Temporal (auto-generated secrets)'],
          ].map(([title, desc]) => (
            <div key={title} style={{ display: 'flex', gap: 12, padding: '6px 0' }}>
              <Check size={14} style={{ color: '#22c55e', flexShrink: 0, marginTop: 3 }} />
              <div>
                <span style={{ fontSize: 13, fontWeight: 600, color: '#c9d1d9' }}>{title}</span>
                <span style={{ fontSize: 12, color: '#6e7681' }}> &mdash; {desc}</span>
              </div>
            </div>
          ))}
        </div>
      </div>

      <button
        type="button"
        onClick={onStart}
        style={{
          padding: '14px 40px', borderRadius: 12,
          background: 'linear-gradient(135deg, #22c55e, #16a34a)',
          color: '#fff', border: 'none', fontSize: 16, fontWeight: 700,
          cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 10,
          boxShadow: '0 4px 20px rgba(34,197,94,0.35)',
          transition: 'transform 0.15s, box-shadow 0.15s',
        }}
        onMouseEnter={e => { e.currentTarget.style.transform = 'translateY(-1px)'; e.currentTarget.style.boxShadow = '0 6px 24px rgba(34,197,94,0.45)' }}
        onMouseLeave={e => { e.currentTarget.style.transform = ''; e.currentTarget.style.boxShadow = '0 4px 20px rgba(34,197,94,0.35)' }}
      >
        <Rocket size={18} /> Begin Setup
      </button>
      <p style={{ marginTop: 12, fontSize: 12, color: '#484f58' }}>
        Takes about 5 minutes. Your progress is auto-saved.
      </p>
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════════════════
   Step Components
   ═══════════════════════════════════════════════════════════════════════════ */

function StepExchange({ state, update }: { state: WizardState; update: (p: Partial<WizardState>) => void }) {
  const { exchanges } = state
  const cards: { key: 'coinbase' | 'nordnet' | 'ibkr'; icon: typeof Coins; color: string; title: string; sub: string; desc: string; tags: string[] }[] = [
    {
      key: 'coinbase', icon: Coins, color: '#3b82f6',
      title: 'Coinbase', sub: 'Cryptocurrency', desc: 'Trade crypto assets like BTC, ETH, SOL on Coinbase Advanced Trade. Supports paper and live trading with real-time WebSocket price feeds.',
      tags: ['BTC', 'ETH', 'SOL', 'ADA', 'DOGE'],
    },
    {
      key: 'nordnet', icon: TrendingUp, color: '#22c55e',
      title: 'Nordnet', sub: 'OMX Stockholm Equities', desc: 'Trade Swedish equities on OMX Stockholm. Currently paper-mode only. SEK-denominated with Courtage Mini fee model.',
      tags: ['VOLV-B', 'ERIC-B', 'ABB', 'H&M', 'SEB'],
    },
    {
      key: 'ibkr', icon: BarChart3, color: '#e11d48',
      title: 'Interactive Brokers', sub: 'US Equities (IBKR)', desc: 'Trade US equities via IB Gateway / TWS. Supports paper and live trading with IBKR tiered commission model. USD-denominated.',
      tags: ['AAPL', 'MSFT', 'GOOGL', 'NVDA', 'AMZN'],
    },
  ]

  const selectExchange = (key: 'coinbase' | 'nordnet' | 'ibkr') => {
    // Coinbase toggles independently; NordNet & IBKR are mutually exclusive (pick one shares broker)
    if (key === 'coinbase') {
      update({ exchanges: { ...exchanges, coinbase: !exchanges.coinbase } })
    } else {
      // Shares broker: toggle selected, deselect the other
      const alreadyActive = exchanges[key]
      update({ exchanges: { ...exchanges, nordnet: key === 'nordnet' ? !alreadyActive : false, ibkr: key === 'ibkr' ? !alreadyActive : false } })
    }
  }

  return (
    <>
      <SectionHeader
        icon={<BarChart3 size={22} />}
        title="Choose Your Exchanges"
        subtitle="Pick Coinbase for crypto, plus optionally one shares broker (NordNet or IBKR — not both)."
      />
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 16 }}>
        {cards.map(c => {
          const Icon = c.icon
          const active = exchanges[c.key]
          return (
            <button
              key={c.key}
              type="button"
              className="at-card-hover"
              onClick={() => selectExchange(c.key)}
              style={{
                ...card, cursor: 'pointer', textAlign: 'left',
                border: active ? `2px solid ${c.color}` : '1px solid #30363d',
                background: active ? `${c.color}08` : '#161b22',
                padding: active ? 23 : 24,
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 14 }}>
                <div style={{
                  width: 44, height: 44, borderRadius: 10,
                  background: active ? c.color : '#21262d',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  transition: 'background 0.2s',
                }}>
                  <Icon size={22} color="#fff" />
                </div>
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 17, fontWeight: 700, color: '#e6edf3' }}>{c.title}</div>
                  <div style={{ fontSize: 12, color: '#6e7681' }}>{c.sub}</div>
                </div>
                <div style={{
                  width: 24, height: 24, borderRadius: c.key === 'coinbase' ? 6 : '50%',
                  border: `2px solid ${active ? c.color : '#30363d'}`,
                  background: active ? c.color : 'transparent',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  transition: 'all 0.15s',
                }}>
                  {active && (c.key === 'coinbase'
                    ? <Check size={14} color="#fff" strokeWidth={3} />
                    : <div style={{ width: 10, height: 10, borderRadius: '50%', background: '#fff' }} />
                  )}
                </div>
              </div>
              <p style={{ margin: '0 0 14px 0', fontSize: 13, color: '#8b949e', lineHeight: 1.6 }}>{c.desc}</p>
              <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap' }}>
                {c.tags.map(t => (
                  <span key={t} style={{
                    padding: '2px 8px', borderRadius: 4, fontSize: 11, fontWeight: 600,
                    background: `${c.color}18`, color: `${c.color}cc`,
                  }}>{t}</span>
                ))}
              </div>
            </button>
          )
        })}
      </div>
      {!exchanges.coinbase && !exchanges.nordnet && !exchanges.ibkr && (
        <div style={{ marginTop: 16 }}><Warning>Please select at least one exchange to continue.</Warning></div>
      )}
      {exchanges.nordnet && exchanges.ibkr && (
        <div style={{ marginTop: 16 }}><Warning>Pick one shares broker — NordNet or IBKR, not both.</Warning></div>
      )}
    </>
  )
}

function StepTradingMode({ state, update }: { state: WizardState; update: (p: Partial<WizardState>) => void }) {
  return (
    <>
      <SectionHeader icon={<Settings2 size={22} />} title="Trading Mode" subtitle="Choose how the agent trades. You can always switch modes later from the Settings page." />
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        {([
          { mode: 'paper' as const, icon: MonitorDot, title: 'Paper Trading', color: '#22c55e', tag: 'RECOMMENDED',
            desc: 'Simulated trading with no real money. Perfect for testing strategies and building confidence. All trades tracked like real ones.' },
          { mode: 'live' as const, icon: Zap, title: 'Live Trading', color: '#ef4444', tag: '',
            desc: 'Real money trading on the exchange. The agent executes actual buy/sell orders. Make sure you understand the risks.' },
        ]).map(m => {
          const Icon = m.icon
          const active = state.tradingMode === m.mode
          return (
            <button
              key={m.mode}
              type="button"
              className="at-card-hover"
              onClick={() => update({ tradingMode: m.mode, liveConfirmed: m.mode === 'paper' ? false : state.liveConfirmed })}
              style={{
                ...card, cursor: 'pointer', textAlign: 'left',
                border: active ? `2px solid ${m.color}` : '1px solid #30363d',
                background: active ? `${m.color}08` : '#161b22',
                padding: active ? 23 : 24,
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
                <Icon size={22} color={active ? m.color : '#6e7681'} />
                <span style={{ fontSize: 18, fontWeight: 700, color: '#e6edf3' }}>{m.title}</span>
                {m.tag && <span style={{
                  marginLeft: 'auto', padding: '2px 10px', borderRadius: 12,
                  background: `${m.color}18`, color: m.color, fontSize: 10, fontWeight: 700,
                }}>{m.tag}</span>}
              </div>
              <p style={{ margin: 0, fontSize: 13, color: '#8b949e', lineHeight: 1.6 }}>{m.desc}</p>
            </button>
          )
        })}
      </div>
      {state.tradingMode === 'live' && (
        <div style={{ marginTop: 20 }}>
          <SecurityBox>
            <div style={{ fontWeight: 700, marginBottom: 8, color: '#fca5a5', fontSize: 14 }}>Live Trading Warning</div>
            <p style={{ margin: '0 0 12px 0' }}>
              You are enabling <strong>live trading with real money</strong>. Please ensure you have:
            </p>
            <ul style={{ margin: '0 0 14px 0', paddingLeft: 18, display: 'flex', flexDirection: 'column', gap: 4 }}>
              <li>Set appropriate trade limits and risk parameters</li>
              <li>Tested your strategy in paper mode first</li>
              <li>Only deposited funds you can afford to lose</li>
            </ul>
            <label style={{
              display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer',
              color: '#e6edf3', fontWeight: 600, fontSize: 14,
              padding: '10px 14px', borderRadius: 8, background: 'rgba(239,68,68,0.08)',
              border: '1px solid rgba(239,68,68,0.15)',
            }}>
              <input
                type="checkbox" checked={state.liveConfirmed}
                onChange={e => update({ liveConfirmed: e.target.checked })}
                style={{ width: 18, height: 18, accentColor: '#ef4444' }}
              />
              I understand the risks of live trading
            </label>
          </SecurityBox>
        </div>
      )}
    </>
  )
}

function StepAssets({ state, update, onSkip: _onSkip }: { state: WizardState; update: (p: Partial<WizardState>) => void; onSkip: () => void }) {
  const toggle = (list: string[], id: string) =>
    list.includes(id) ? list.filter(p => p !== id) : [...list, id]
  const addCustom = (field: 'cryptoPairs' | 'stockPairs' | 'ibkrPairs', inputField: 'customCryptoPair' | 'customStockPair' | 'customIbkrPair') => {
    const v = (state[inputField] as string).trim().toUpperCase()
    if (v && !(state[field] as string[]).includes(v)) {
      update({ [field]: [...(state[field] as string[]), v], [inputField]: '' })
    }
  }

  const renderPairSection = (opts: {
    title: string; icon: ReactNode; color: string;
    items: { id: string; name: string; symbol?: string; sector?: string }[];
    pairs: string[]; pairsKey: 'cryptoPairs' | 'stockPairs' | 'ibkrPairs';
    custom: string; customKey: 'customCryptoPair' | 'customStockPair' | 'customIbkrPair';
    placeholder: string; subtitle: string;
  }) => (
    <div style={{ ...card, marginBottom: 16 }}>
      <h3 style={{ margin: '0 0 6px 0', fontSize: 16, fontWeight: 700, color: opts.color, display: 'flex', alignItems: 'center', gap: 8 }}>
        {opts.icon} {opts.title}
        <span style={{ marginLeft: 'auto' }}>
          <ValidationBadge valid={opts.pairs.length > 0} label={`${opts.pairs.length} selected`} />
        </span>
      </h3>
      <p style={{ margin: '0 0 16px 0', fontSize: 13, color: '#8b949e' }}>{opts.subtitle}</p>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 16 }}>
        {opts.items.map(item => (
          <ToggleChip key={item.id} selected={opts.pairs.includes(item.id)} onClick={() => update({ [opts.pairsKey]: toggle(opts.pairs, item.id) })} color={opts.color}>
            <span style={{ fontWeight: 700 }}>{item.symbol || item.id.replace('.ST', '')}</span>
            <span style={{ fontSize: 11, opacity: 0.7 }}>{item.name || item.sector}</span>
          </ToggleChip>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        <input
          value={opts.custom}
          onChange={e => update({ [opts.customKey]: e.target.value })}
          onKeyDown={e => e.key === 'Enter' && addCustom(opts.pairsKey, opts.customKey)}
          placeholder={opts.placeholder}
          style={{ ...inputBase, flex: 1 }}
          className="at-input"
        />
        <button type="button" onClick={() => addCustom(opts.pairsKey, opts.customKey)} style={{
          padding: '10px 16px', borderRadius: 8, border: `1px solid ${opts.color}`,
          background: `${opts.color}12`, color: opts.color,
          cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 6, fontWeight: 600, fontSize: 13,
        }}>
          <Plus size={15} /> Add
        </button>
      </div>
      {opts.pairs.length > 0 && (
        <div style={{ marginTop: 12, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {opts.pairs.map(p => (
            <span key={p} style={{
              display: 'inline-flex', alignItems: 'center', gap: 4,
              padding: '4px 10px', borderRadius: 6, fontSize: 12, fontWeight: 600,
              background: `${opts.color}14`, color: opts.color,
            }}>
              {p}
              <button type="button" onClick={() => update({ [opts.pairsKey]: opts.pairs.filter(x => x !== p) })}
                style={{ background: 'none', border: 'none', color: opts.color, cursor: 'pointer', padding: 0, display: 'flex' }}>
                <X size={12} />
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  )

  return (
    <>
      <SectionHeader icon={<TrendingUp size={22} />} title="Assets to Follow" subtitle="Select which assets the agent will monitor. The agent also auto-discovers opportunities beyond this list." />
      <Tip>These are your <strong>starting pairs</strong>. The agent's pair discovery engine will automatically scan for additional opportunities based on volume and momentum.</Tip>
      <div style={{ marginTop: 16 }}>
        {state.exchanges.coinbase && renderPairSection({
          title: 'Cryptocurrency Pairs', icon: <Coins size={18} />, color: '#3b82f6',
          items: POPULAR_CRYPTO, pairs: state.cryptoPairs, pairsKey: 'cryptoPairs',
          custom: state.customCryptoPair, customKey: 'customCryptoPair',
          placeholder: 'Add custom pair (e.g. PEPE-EUR)',
          subtitle: 'Select which crypto assets to monitor on Coinbase.',
        })}
        {state.exchanges.nordnet && renderPairSection({
          title: 'OMX Stockholm Stocks', icon: <TrendingUp size={18} />, color: '#22c55e',
          items: POPULAR_STOCKS, pairs: state.stockPairs, pairsKey: 'stockPairs',
          custom: state.customStockPair, customKey: 'customStockPair',
          placeholder: 'Add custom ticker (e.g. SSAB-A.ST)',
          subtitle: 'Select which Swedish equities to monitor. All SEK-denominated.',
        })}
        {state.exchanges.ibkr && renderPairSection({
          title: 'US Equities (IBKR)', icon: <BarChart3 size={18} />, color: '#e11d48',
          items: POPULAR_IBKR_STOCKS, pairs: state.ibkrPairs, pairsKey: 'ibkrPairs',
          custom: state.customIbkrPair, customKey: 'customIbkrPair',
          placeholder: 'Add custom ticker (e.g. TSLA-USD)',
          subtitle: 'Select which US equities to monitor. USD-denominated via Interactive Brokers.',
        })}
      </div>
    </>
  )
}

function StepCoinbaseApi({ state, update, onSkip }: { state: WizardState; update: (p: Partial<WizardState>) => void; onSkip: () => void }) {
  const isPaper = state.tradingMode === 'paper'
  return (
    <>
      <SectionHeader icon={<Coins size={22} />} title="Coinbase API Credentials" subtitle="Connect your Coinbase Advanced Trade account for market data and trading." />
      {isPaper && (
        <div style={{ marginBottom: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
          <Tip>You're in <strong>Paper Trading</strong> mode. API keys are optional &mdash; the agent simulates trades without connecting to Coinbase. Add keys for real-time prices.</Tip>
          <SkipLink onClick={onSkip} label="Skip &mdash; I'll add keys later" />
        </div>
      )}
      <HowTo
        title="How to get your Coinbase API keys"
        link={{ url: 'https://www.coinbase.com/settings/api', label: 'Open Coinbase' }}
        steps={[
          'Go to coinbase.com/settings/api (or Coinbase Developer Platform)',
          'Click "New API Key"',
          'Select permissions: View ✓, Trade ✓, Transfer ✗',
          'Coinbase shows two values: API Key Name & Private Key',
          'API Key Name looks like: organizations/xxxx/apiKeys/xxxx',
          'Private Key is a multi-line EC PEM key (shown only once!)',
          'Copy both values before closing the dialog',
        ]}
      />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16, marginTop: 20 }}>
        <FormField label="API Key Name" help='Looks like "organizations/xxxx-xxxx/apiKeys/xxxx-xxxx"' required={!isPaper}>
          <PasswordInput value={state.coinbaseApiKey} onChange={v => update({ coinbaseApiKey: v })}
            placeholder="organizations/xxxxxxxx-xxxx/apiKeys/xxxxxxxx-xxxx" useMono />
        </FormField>
        <FormField label="Private Key (PEM)" help="Starts with -----BEGIN EC PRIVATE KEY-----" required={!isPaper}>
          <PasswordInput value={state.coinbaseApiSecret} onChange={v => update({ coinbaseApiSecret: v })}
            placeholder="-----BEGIN EC PRIVATE KEY-----\n..." useMono />
          <Tip>Paste as a single line with <code style={{ color: '#22c55e' }}>\n</code> replacing newlines, or paste the full multi-line key.</Tip>
        </FormField>
      </div>
    </>
  )
}

function StepIbkrConnection({ state, update }: { state: WizardState; update: (p: Partial<WizardState>) => void }) {
  const currencies = ['USD', 'EUR', 'GBP', 'CHF']
  return (
    <>
      <SectionHeader icon={<BarChart3 size={22} />} title="IBKR Connection Settings" subtitle="Configure how the agent connects to IB Gateway or TWS (Trader Workstation)." />
      <Tip>
        <strong>Paper trading:</strong> IB Gateway paper port is typically <strong>4002</strong>.
        Live port is <strong>4001</strong>. Make sure IB Gateway is running and API connections are enabled.
      </Tip>
      <HowTo
        title="How to set up IB Gateway"
        steps={[
          'Download IB Gateway from interactivebrokers.com',
          'Log in with your IBKR credentials (paper or live account)',
          'Go to Configure → Settings → API → Settings',
          'Enable "Enable ActiveX and Socket Clients"',
          'Set "Socket port" to 4002 (paper) or 4001 (live)',
          'Uncheck "Read-Only API" if you want the agent to place orders',
          'Note the Client ID you want to use (default: 1)',
        ]}
      />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16, marginTop: 20 }}>
        <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 16 }}>
          <FormField label="IB Gateway Host" help="Usually 127.0.0.1 for local, or host.docker.internal from Docker" required>
            <input value={state.ibkrHost} onChange={e => update({ ibkrHost: e.target.value })}
              placeholder="127.0.0.1" style={inputBase} className="at-input" />
          </FormField>
          <FormField label="Port" help="4002 = paper, 4001 = live" required>
            <input value={state.ibkrPort} onChange={e => update({ ibkrPort: e.target.value.replace(/[^\d]/g, '') })}
              placeholder="4002" style={inputBase} className="at-input" />
          </FormField>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          <FormField label="Client ID" help="Must be unique per connection. Default: 1">
            <input value={state.ibkrClientId} onChange={e => update({ ibkrClientId: e.target.value.replace(/[^\d]/g, '') })}
              placeholder="1" style={inputBase} className="at-input" />
          </FormField>
          <FormField label="Base Currency" help="Currency for your IBKR account">
            <div style={{ display: 'flex', gap: 8 }}>
              {currencies.map(c => (
                <button key={c} type="button" onClick={() => update({ ibkrCurrency: c })} style={{
                  flex: 1, padding: '10px 0', borderRadius: 8, fontSize: 13, fontWeight: 600,
                  border: state.ibkrCurrency === c ? '2px solid #e11d48' : '1px solid #30363d',
                  background: state.ibkrCurrency === c ? '#e11d4810' : '#161b22',
                  color: state.ibkrCurrency === c ? '#fb7185' : '#8b949e',
                  cursor: 'pointer', transition: 'all 0.15s',
                }}>{c}</button>
              ))}
            </div>
          </FormField>
        </div>
      </div>
    </>
  )
}

function StepLLM({ state, update }: { state: WizardState; update: (p: Partial<WizardState>) => void }) {
  return (
    <>
      <SectionHeader icon={<Sparkles size={22} />} title="AI / LLM Configuration" subtitle="Configure the AI brain. Requests try providers in order: Gemini → OpenRouter → OpenAI → Ollama (local fallback)." />

      <h3 style={{ margin: '0 0 12px 0', fontSize: 15, fontWeight: 700, color: '#e6edf3', display: 'flex', alignItems: 'center', gap: 8 }}>
        <Cloud size={16} color="#6e7681" /> Cloud Providers
        <span style={{ fontSize: 12, fontWeight: 400, color: '#484f58' }}>Optional &mdash; faster than local</span>
      </h3>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 28 }}>
        {([
          { key: 'gemini' as const, enabled: state.geminiEnabled, apiKey: state.geminiApiKey,
            enabledKey: 'geminiEnabled', apiKeyKey: 'geminiApiKey',
            icon: Zap, color: '#3b82f6', title: 'Google Gemini', model: 'gemini-2.0-flash', rate: '14 RPM free',
            placeholder: 'AIza...', steps: ['Go to aistudio.google.com/app/apikey', 'Click "Create API key"', 'Copy the key'],
            link: { url: 'https://aistudio.google.com/app/apikey', label: 'Get key' },
          },
          { key: 'openrouter' as const, enabled: state.openrouterEnabled, apiKey: state.openrouterApiKey,
            enabledKey: 'openrouterEnabled', apiKeyKey: 'openrouterApiKey',
            icon: Cloud, color: '#f59e0b', title: 'OpenRouter', model: '200+ models (free tier)', rate: 'Free models available',
            placeholder: 'sk-or-...', steps: ['Go to openrouter.ai/keys', 'Sign in and click "Create Key"', 'Copy the key (starts with sk-or-)'],
            link: { url: 'https://openrouter.ai/keys', label: 'Get key' },
          },
          { key: 'openai' as const, enabled: state.openaiEnabled, apiKey: state.openaiApiKey,
            enabledKey: 'openaiEnabled', apiKeyKey: 'openaiApiKey',
            icon: Cloud, color: '#10b981', title: 'OpenAI', model: 'gpt-4o-mini', rate: '450 RPM',
            placeholder: 'sk-...', steps: ['Go to platform.openai.com/api-keys', 'Click "Create new secret key"', 'Copy the key (starts with sk-)'],
            link: { url: 'https://platform.openai.com/api-keys', label: 'Get key' },
          },
        ]).map(p => {
          const Icon = p.icon
          return (
            <div key={p.key} style={{ ...card, border: p.enabled ? `1.5px solid ${p.color}` : '1px solid #30363d' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <Icon size={18} color={p.color} />
                  <div>
                    <div style={{ fontSize: 14, fontWeight: 700, color: '#e6edf3' }}>{p.title}</div>
                    <div style={{ fontSize: 11, color: '#6e7681' }}>{p.model} &middot; {p.rate}</div>
                  </div>
                </div>
                <label style={{ cursor: 'pointer' }}>
                  <input type="checkbox" checked={p.enabled} onChange={e => update({ [p.enabledKey]: e.target.checked })}
                    style={{ width: 18, height: 18, accentColor: p.color }} />
                </label>
              </div>
              {p.enabled && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  <HowTo title="How to get your key" link={p.link} steps={p.steps} />
                  <PasswordInput value={p.apiKey} onChange={v => update({ [p.apiKeyKey]: v })} placeholder={p.placeholder} useMono />
                  {p.apiKey && <ValidationBadge valid={p.apiKey.length > 10} label={p.apiKey.length > 10 ? 'Key provided' : 'Key looks short'} />}
                </div>
              )}
            </div>
          )
        })}
      </div>

      <h3 style={{ margin: '0 0 12px 0', fontSize: 15, fontWeight: 700, color: '#e6edf3', display: 'flex', alignItems: 'center', gap: 8 }}>
        <Server size={16} color="#a855f7" /> Ollama Local Model
        <span style={{ fontSize: 12, fontWeight: 400, color: '#484f58' }}>Always available &middot; Runs on your GPU</span>
      </h3>
      <Tip>Ollama runs locally on your GPU as the final fallback. The model downloads automatically when you start Docker.</Tip>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 16 }}>
        {OLLAMA_MODELS.map(m => (
          <button key={m.id} type="button" className="at-card-hover" onClick={() => update({ ollamaModel: m.id })} style={{
            ...card, padding: 16, cursor: 'pointer', textAlign: 'left',
            border: state.ollamaModel === m.id ? '2px solid #a855f7' : '1px solid #30363d',
            background: state.ollamaModel === m.id ? 'rgba(168,85,247,0.06)' : '#161b22',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <div style={{ flex: 1 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ fontSize: 14, fontWeight: 700, color: '#e6edf3' }}>{m.name}</span>
                  {m.recommended && <span style={{ padding: '1px 8px', borderRadius: 10, fontSize: 10, fontWeight: 700, background: 'rgba(168,85,247,0.2)', color: '#c084fc' }}>BEST</span>}
                </div>
                <div style={{ fontSize: 12, color: '#8b949e', marginTop: 3 }}>{m.desc}</div>
              </div>
              <span style={{
                padding: '4px 10px', borderRadius: 6, fontSize: 11, fontFamily: "'JetBrains Mono', monospace",
                background: '#21262d', color: '#8b949e',
              }}>{m.vram}</span>
            </div>
          </button>
        ))}
      </div>
    </>
  )
}

function StepTelegram({ state, update, onSkip }: { state: WizardState; update: (p: Partial<WizardState>) => void; onSkip: () => void }) {
  const tokenValid = (t: string) => !t || isValidTelegramToken(t)
  return (
    <>
      <SectionHeader icon={<MessageSquare size={22} />} title="Telegram Bot Setup" subtitle="Receive trade alerts, approve high-value trades, and control the agent via Telegram." />
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 20 }}>
        <label style={{ fontSize: 15, fontWeight: 600, color: '#e6edf3', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 10 }}>
          <input type="checkbox" checked={state.telegramEnabled} onChange={e => update({ telegramEnabled: e.target.checked })}
            style={{ width: 18, height: 18, accentColor: '#22c55e' }} />
          Enable Telegram integration
        </label>
        {!state.telegramEnabled && <SkipLink onClick={onSkip} />}
      </div>

      {!state.telegramEnabled ? (
        <Warning>Without Telegram, you won't receive trade notifications or be able to approve/reject trades. The agent runs fully autonomously.</Warning>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }} className="at-step-enter">
          {/* User ID */}
          <SecurityBox>
            <div style={{ fontWeight: 700, marginBottom: 8, fontSize: 14 }}>Your Telegram User ID</div>
            <p style={{ margin: '0 0 10px 0' }}>
              Your numeric User ID is how the bot verifies your identity. <strong>Only authorized IDs can control the bot.</strong>
            </p>
            <HowTo title="How to find your User ID" steps={[
              'Open Telegram and search for @userinfobot',
              'Send it any message',
              'It replies with your numeric ID (e.g. 123456789)',
              'This is NOT the same as your chat ID!',
            ]} />
            <div style={{ marginTop: 12 }}>
              <input value={state.telegramUserId} onChange={e => update({ telegramUserId: e.target.value.replace(/[^\d]/g, '') })}
                placeholder="Your numeric User ID (e.g. 123456789)" style={mono} className="at-input" />
            </div>
            {state.telegramUserId && <div style={{ marginTop: 6 }}><ValidationBadge valid={/^\d{5,}$/.test(state.telegramUserId)} label={/^\d{5,}$/.test(state.telegramUserId) ? 'Valid numeric ID' : 'Must be numeric'} /></div>}
            <div style={{ marginTop: 10, fontSize: 12, color: '#6e7681' }}>
              Additional authorized users (optional):
              <input value={state.telegramAdditionalUsers} onChange={e => update({ telegramAdditionalUsers: e.target.value })}
                placeholder="Comma-separated user IDs" style={{ ...mono, marginTop: 6, fontSize: 12, padding: '6px 10px' }} className="at-input" />
            </div>
          </SecurityBox>

          {/* Per-exchange bots */}
          {state.exchanges.coinbase && (
            <div style={card}>
              <h3 style={{ margin: '0 0 6px 0', fontSize: 15, fontWeight: 700, color: '#60a5fa', display: 'flex', alignItems: 'center', gap: 8 }}>
                <Bot size={16} /> Coinbase Bot
                {state.telegramCoinbaseBotToken && (
                  <span style={{ marginLeft: 'auto' }}>
                    <ValidationBadge valid={tokenValid(state.telegramCoinbaseBotToken)} label={isValidTelegramToken(state.telegramCoinbaseBotToken) ? 'Valid format' : 'Invalid format'} />
                  </span>
                )}
              </h3>
              <p style={{ margin: '0 0 14px 0', fontSize: 13, color: '#8b949e' }}>A dedicated Telegram bot for crypto trading notifications.</p>
              <HowTo title="How to create a bot via @BotFather" steps={[
                'Open Telegram and search for @BotFather',
                'Send /newbot',
                'Choose a name (e.g. "Auto-Traitor Crypto")',
                'Choose a username (e.g. "my_at_crypto_bot")',
                'BotFather gives you a token like: 1234567890:ABCdefGHIjklMNO...',
              ]} />
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12, marginTop: 14 }}>
                <FormField label="Bot Token" required>
                  <PasswordInput value={state.telegramCoinbaseBotToken} onChange={v => update({ telegramCoinbaseBotToken: v })}
                    placeholder="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz" useMono />
                </FormField>
                <FormField label="Chat ID" help={`Defaults to your User ID (${state.telegramUserId || '...'})`}>
                  <input value={state.telegramCoinbaseChatId} onChange={e => update({ telegramCoinbaseChatId: e.target.value })}
                    placeholder={state.telegramUserId || 'Same as User ID'} style={mono} className="at-input" />
                </FormField>
              </div>
            </div>
          )}

          {state.exchanges.nordnet && (
            <div style={card}>
              <h3 style={{ margin: '0 0 6px 0', fontSize: 15, fontWeight: 700, color: '#4ade80', display: 'flex', alignItems: 'center', gap: 8 }}>
                <Bot size={16} /> Equities Bot (Nordnet)
                {state.telegramNordnetBotToken && (
                  <span style={{ marginLeft: 'auto' }}>
                    <ValidationBadge valid={tokenValid(state.telegramNordnetBotToken)} label={isValidTelegramToken(state.telegramNordnetBotToken) ? 'Valid format' : 'Invalid format'} />
                  </span>
                )}
              </h3>
              <p style={{ margin: '0 0 14px 0', fontSize: 13, color: '#8b949e' }}>Create a Telegram bot via @BotFather.</p>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                <FormField label="Bot Token" required>
                  <PasswordInput value={state.telegramNordnetBotToken} onChange={v => update({ telegramNordnetBotToken: v })}
                    placeholder="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz" useMono />
                </FormField>
                <FormField label="Chat ID" help={`Defaults to your User ID (${state.telegramUserId || '...'})`}>
                  <input value={state.telegramNordnetChatId} onChange={e => update({ telegramNordnetChatId: e.target.value })}
                    placeholder={state.telegramUserId || 'Same as User ID'} style={mono} className="at-input" />
                </FormField>
              </div>
            </div>
          )}

          {state.exchanges.ibkr && (
            <div style={card}>
              <h3 style={{ margin: '0 0 6px 0', fontSize: 15, fontWeight: 700, color: '#fb7185', display: 'flex', alignItems: 'center', gap: 8 }}>
                <Bot size={16} /> IBKR Bot
                {state.telegramIbkrBotToken && (
                  <span style={{ marginLeft: 'auto' }}>
                    <ValidationBadge valid={tokenValid(state.telegramIbkrBotToken)} label={isValidTelegramToken(state.telegramIbkrBotToken) ? 'Valid format' : 'Invalid format'} />
                  </span>
                )}
              </h3>
              <p style={{ margin: '0 0 14px 0', fontSize: 13, color: '#8b949e' }}>Create a Telegram bot via @BotFather for IBKR trading notifications.</p>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                <FormField label="Bot Token" required>
                  <PasswordInput value={state.telegramIbkrBotToken} onChange={v => update({ telegramIbkrBotToken: v })}
                    placeholder="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz" useMono />
                </FormField>
                <FormField label="Chat ID" help={`Defaults to your User ID (${state.telegramUserId || '...'})`}>
                  <input value={state.telegramIbkrChatId} onChange={e => update({ telegramIbkrChatId: e.target.value })}
                    placeholder={state.telegramUserId || 'Same as User ID'} style={mono} className="at-input" />
                </FormField>
              </div>
            </div>
          )}

          <Tip>
            <strong>Security tip:</strong> After creating bots, go to @BotFather &rarr; /mybots &rarr; Bot Settings &rarr; Allow Groups &rarr; Turn OFF.
          </Tip>
        </div>
      )}
    </>
  )
}

function StepNews({ state, update, onSkip }: { state: WizardState; update: (p: Partial<WizardState>) => void; onSkip: () => void }) {
  return (
    <>
      <SectionHeader icon={<Newspaper size={22} />} title="News & Sentiment Sources" subtitle="The agent monitors news for market sentiment. RSS feeds are built-in and require no setup." />
      <Tip>Built-in RSS feeds: CoinTelegraph, CoinDesk, Decrypt, DI.se &mdash; all active automatically.</Tip>
      <div style={{ ...card, marginTop: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <Newspaper size={18} color="#f97316" />
            <div>
              <div style={{ fontSize: 14, fontWeight: 700, color: '#e6edf3' }}>Reddit API</div>
              <div style={{ fontSize: 11, color: '#6e7681' }}>r/cryptocurrency, r/bitcoin, r/CryptoMarkets &amp; more</div>
            </div>
          </div>
          <label style={{ cursor: 'pointer' }}>
            <input type="checkbox" checked={state.redditEnabled} onChange={e => update({ redditEnabled: e.target.checked })}
              style={{ width: 18, height: 18, accentColor: '#f97316' }} />
          </label>
        </div>
        {state.redditEnabled && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14, marginTop: 8 }} className="at-step-enter">
            <HowTo title="How to get Reddit API credentials" link={{ url: 'https://www.reddit.com/prefs/apps', label: 'reddit.com/prefs/apps' }} steps={[
              'Go to reddit.com/prefs/apps', 'Click "create another app..."', 'Select "script" type',
              'Use any redirect URI (e.g. http://localhost)', 'Copy Client ID (under app name) and Client Secret',
            ]} />
            <FormField label="Client ID" required>
              <PasswordInput value={state.redditClientId} onChange={v => update({ redditClientId: v })} placeholder="Your Client ID" useMono />
            </FormField>
            <FormField label="Client Secret" required>
              <PasswordInput value={state.redditClientSecret} onChange={v => update({ redditClientSecret: v })} placeholder="Your Client Secret" useMono />
            </FormField>
            <FormField label="User Agent" help="Identifies your app to Reddit.">
              <input value={state.redditUserAgent} onChange={e => update({ redditUserAgent: e.target.value })} style={inputBase} className="at-input" />
            </FormField>
          </div>
        )}
      </div>
      {!state.redditEnabled && <div style={{ marginTop: 12 }}><SkipLink onClick={onSkip} label="Continue without Reddit" /></div>}
    </>
  )
}

function StepReview({ state, stepsWithValidation: _stepsWithValidation }: { state: WizardState; stepsWithValidation: { id: string; title: string; validation: StepValidation }[] }) {
  const [expandedEnv, setExpandedEnv] = useState(false)
  const envPreview = useMemo(() => generateEnvContent(state), [state])

  const sections = useMemo(() => {
    const s: { title: string; icon: ReactNode; items: { label: string; value: string; ok: boolean }[] }[] = []
    const ex: string[] = []
    if (state.exchanges.coinbase) ex.push('Coinbase (Crypto)')
    if (state.exchanges.nordnet) ex.push('Nordnet (Equities)')
    if (state.exchanges.ibkr) ex.push('Interactive Brokers (US Equities)')
    s.push({ title: 'Exchanges', icon: <BarChart3 size={14} />, items: [{ label: 'Active', value: ex.join(' + '), ok: ex.length > 0 }] })
    s.push({ title: 'Trading Mode', icon: <Settings2 size={14} />, items: [{ label: 'Mode', value: state.tradingMode === 'paper' ? 'Paper (simulated)' : 'LIVE (real money)', ok: state.tradingMode === 'paper' || state.liveConfirmed }] })

    const assets: { label: string; value: string; ok: boolean }[] = []
    if (state.exchanges.coinbase) assets.push({ label: 'Crypto', value: `${state.cryptoPairs.length} pairs`, ok: state.cryptoPairs.length > 0 })
    if (state.exchanges.nordnet) assets.push({ label: 'Stocks', value: `${state.stockPairs.length} pairs`, ok: state.stockPairs.length > 0 })
    if (state.exchanges.ibkr) assets.push({ label: 'IBKR Stocks', value: `${state.ibkrPairs.length} pairs`, ok: state.ibkrPairs.length > 0 })
    s.push({ title: 'Assets', icon: <TrendingUp size={14} />, items: assets })

    if (state.exchanges.coinbase) {
      const hasKey = !!state.coinbaseApiKey && !!state.coinbaseApiSecret
      s.push({ title: 'Coinbase API', icon: <Coins size={14} />, items: [{ label: 'Credentials', value: hasKey ? 'Configured' : state.tradingMode === 'paper' ? 'Skipped (paper)' : 'MISSING', ok: hasKey || state.tradingMode === 'paper' }] })
    }

    if (state.exchanges.ibkr) {
      s.push({ title: 'IBKR Connection', icon: <BarChart3 size={14} />, items: [
        { label: 'Gateway', value: `${state.ibkrHost}:${state.ibkrPort}`, ok: !!state.ibkrHost && !!state.ibkrPort },
        { label: 'Client ID', value: state.ibkrClientId || '1', ok: true },
        { label: 'Currency', value: state.ibkrCurrency, ok: true },
      ] })
    }

    const llm: { label: string; value: string; ok: boolean }[] = []
    if (state.geminiEnabled) llm.push({ label: 'Gemini', value: state.geminiApiKey ? 'Configured' : 'Key missing', ok: !!state.geminiApiKey })
    if (state.openaiEnabled) llm.push({ label: 'OpenAI', value: state.openaiApiKey ? 'Configured' : 'Key missing', ok: !!state.openaiApiKey })
    llm.push({ label: 'Ollama', value: state.ollamaModel, ok: true })
    s.push({ title: 'LLM Providers', icon: <Sparkles size={14} />, items: llm })

    if (state.telegramEnabled) {
      const tg: { label: string; value: string; ok: boolean }[] = []
      tg.push({ label: 'User ID', value: state.telegramUserId || 'MISSING', ok: !!state.telegramUserId })
      if (state.exchanges.coinbase) tg.push({ label: 'Crypto Bot', value: state.telegramCoinbaseBotToken ? (isValidTelegramToken(state.telegramCoinbaseBotToken) ? 'Valid' : 'Bad format') : 'MISSING', ok: isValidTelegramToken(state.telegramCoinbaseBotToken) })
      if (state.exchanges.nordnet) tg.push({ label: 'Stock Bot', value: state.telegramNordnetBotToken ? (isValidTelegramToken(state.telegramNordnetBotToken) ? 'Valid' : 'Bad format') : 'MISSING', ok: isValidTelegramToken(state.telegramNordnetBotToken) })
      if (state.exchanges.ibkr) tg.push({ label: 'IBKR Bot', value: state.telegramIbkrBotToken ? (isValidTelegramToken(state.telegramIbkrBotToken) ? 'Valid' : 'Bad format') : 'MISSING', ok: isValidTelegramToken(state.telegramIbkrBotToken) })
      s.push({ title: 'Telegram', icon: <MessageSquare size={14} />, items: tg })
    } else {
      s.push({ title: 'Telegram', icon: <MessageSquare size={14} />, items: [{ label: 'Status', value: 'Disabled', ok: true }] })
    }

    s.push({
      title: 'News', icon: <Newspaper size={14} />, items: [
        { label: 'RSS', value: 'Built-in', ok: true },
        { label: 'Reddit', value: state.redditEnabled ? (state.redditClientId ? 'Configured' : 'Key missing') : 'Skipped', ok: !state.redditEnabled || !!state.redditClientId },
      ],
    })

    s.push({
      title: 'Infrastructure', icon: <Server size={14} />, items: [
        { label: 'Redis', value: 'Auto-generated', ok: true },
        { label: 'Langfuse', value: 'Auto-generated', ok: true },
        { label: 'Temporal', value: 'Auto-generated', ok: true },
      ],
    })

    return s
  }, [state])

  const allOk = sections.every(s => s.items.every(i => i.ok))

  return (
    <>
      <SectionHeader icon={<Check size={22} />} title="Review & Save" subtitle="Review your configuration. Go back to any step to make changes." />

      {!allOk && <Warning>Some items need attention (marked in yellow). You can still save and fix them later.</Warning>}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: allOk ? 0 : 16 }}>
        {sections.map(sec => (
          <div key={sec.title} style={{ ...card, padding: 16 }}>
            <h4 style={{ margin: '0 0 10px 0', fontSize: 13, fontWeight: 700, color: '#c9d1d9', display: 'flex', alignItems: 'center', gap: 6 }}>
              {sec.icon} {sec.title}
            </h4>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
              {sec.items.map(item => (
                <div key={item.label} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <span style={{ fontSize: 12, color: '#6e7681' }}>{item.label}</span>
                  <span style={{ fontSize: 12, fontWeight: 600, color: item.ok ? '#4ade80' : '#eab308', display: 'flex', alignItems: 'center', gap: 4 }}>
                    {item.ok ? <CheckCircle2 size={12} /> : <CircleAlert size={12} />}
                    {item.value}
                  </span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>

      {/* Env preview */}
      <div style={{ marginTop: 20 }}>
        <button type="button" onClick={() => setExpandedEnv(!expandedEnv)} style={{
          width: '100%', padding: '12px 16px', background: '#161b22', border: '1px solid #30363d',
          borderRadius: expandedEnv ? '10px 10px 0 0' : 10, color: '#c9d1d9', fontSize: 13, fontWeight: 600,
          cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 8, textAlign: 'left',
        }}>
          <Terminal size={14} color="#22c55e" />
          Preview generated config/.env
          {expandedEnv ? <ChevronDown size={14} style={{ marginLeft: 'auto' }} /> : <ChevronRight size={14} style={{ marginLeft: 'auto' }} />}
        </button>
        {expandedEnv && (
          <div style={{
            padding: 16, background: '#0d1117', border: '1px solid #30363d', borderTop: 'none',
            borderRadius: '0 0 10px 10px', maxHeight: 300, overflowY: 'auto',
          }}>
            <pre style={{ margin: 0, fontSize: 12, color: '#8b949e', fontFamily: "'JetBrains Mono', monospace", whiteSpace: 'pre-wrap', lineHeight: 1.6 }}>
              {envPreview}
            </pre>
          </div>
        )}
      </div>

      <div style={{ marginTop: 16 }}>
        <Tip>
          <strong>Save</strong> writes <code style={{ color: '#22c55e' }}>config/.env</code> + root <code style={{ color: '#22c55e' }}>.env</code> and
          updates YAML configs with your selected trading pairs. Infrastructure secrets are auto-generated securely.
        </Tip>
      </div>
    </>
  )
}

/* ═══════════════════════════════════════════════════════════════════════════
   Env file generator (unchanged logic, cleaner structure)
   ═══════════════════════════════════════════════════════════════════════════ */

function generateEnvContent(state: WizardState): string {
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
    if (state.exchanges.nordnet && state.telegramNordnetBotToken) {
      const cid = state.telegramNordnetChatId || state.telegramUserId
      add('TELEGRAM_BOT_TOKEN_NORDNET', state.telegramNordnetBotToken, 'Telegram Bot — Nordnet agent')
      add('TELEGRAM_CHAT_ID_NORDNET', cid)
      if (!state.exchanges.coinbase) { add('TELEGRAM_BOT_TOKEN', state.telegramNordnetBotToken, 'Generic fallback'); add('TELEGRAM_CHAT_ID', cid) }
      blank()
    }
    if (state.exchanges.ibkr && state.telegramIbkrBotToken) {
      const cid = state.telegramIbkrChatId || state.telegramUserId
      add('TELEGRAM_BOT_TOKEN_IBKR', state.telegramIbkrBotToken, 'Telegram Bot — IBKR agent')
      add('TELEGRAM_CHAT_ID_IBKR', cid)
      if (!state.exchanges.coinbase && !state.exchanges.nordnet) { add('TELEGRAM_BOT_TOKEN', state.telegramIbkrBotToken, 'Generic fallback'); add('TELEGRAM_CHAT_ID', cid) }
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

function generateRootEnvContent(state: WizardState, configEnv: string): string {
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

/* ═══════════════════════════════════════════════════════════════════════════
   Main Wizard Component
   ═══════════════════════════════════════════════════════════════════════════ */

const STEPS = [
  { id: 'welcome', title: 'Welcome', icon: Rocket },
  { id: 'exchange', title: 'Exchange', icon: BarChart3 },
  { id: 'mode', title: 'Trading Mode', icon: Settings2 },
  { id: 'assets', title: 'Assets', icon: TrendingUp },
  { id: 'coinbase', title: 'Coinbase API', icon: Coins },
  { id: 'ibkr', title: 'IBKR Connection', icon: BarChart3 },
  { id: 'llm', title: 'AI / LLM', icon: Sparkles },
  { id: 'telegram', title: 'Telegram', icon: MessageSquare },
  { id: 'news', title: 'News', icon: Newspaper },
  { id: 'review', title: 'Review & Save', icon: Check },
]

export default function SetupWizard() {
  useInjectCSS()
  const navigate = useNavigate()
  const [state, setState] = useState<WizardState>(() => {
    try { const s = localStorage.getItem(STORAGE_KEY); return s ? { ...INITIAL_STATE, ...JSON.parse(s) } : INITIAL_STATE }
    catch { return INITIAL_STATE }
  })
  const [step, setStep] = useState(0)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')
  const [stepKey, setStepKey] = useState(0) // for re-triggering enter animation
  const [loading, setLoading] = useState(true)
  const mainRef = useRef<HTMLDivElement>(null)

  // Load live config from server on mount
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch('/api/setup')
        if (!res.ok) throw new Error('fetch failed')
        const data = await res.json()
        if (!cancelled && data?.exists) {
          setState(prev => ({
            ...prev,
            ...data,
            // Ensure nested objects merge properly
            exchanges: { ...prev.exchanges, ...(data.exchanges || {}) },
            infraSecrets: data.infraSecrets || {},
          }))
          // Skip welcome step — go directly to Exchange
          setStep(1)
        }
      } catch {
        // Server unreachable or no config — stay on welcome with defaults
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => { cancelled = true }
  }, [])

  // Auto-save to localStorage (skip secrets and infraSecrets)
  useEffect(() => {
    const { coinbaseApiKey, coinbaseApiSecret, geminiApiKey, openaiApiKey,
      telegramCoinbaseBotToken, telegramNordnetBotToken, telegramIbkrBotToken, redditClientSecret, infraSecrets, ...safe } = state
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(safe)) } catch { /* ignore */ }
  }, [state])

  const update = useCallback((partial: Partial<WizardState>) => {
    setState(prev => ({ ...prev, ...partial }))
  }, [])

  const activeSteps = useMemo(() =>
    STEPS.filter(s => {
      if (s.id === 'coinbase') return state.exchanges.coinbase
      if (s.id === 'ibkr') return state.exchanges.ibkr
      return true
    }),
    [state.exchanges],
  )

  const currentStep = activeSteps[step]
  const isFirst = step === 0
  const isLast = step === activeSteps.length - 1
  const isWelcome = currentStep?.id === 'welcome'

  const stepsWithValidation = useMemo(() =>
    activeSteps.map(s => ({ ...s, validation: validateStep(s.id, state) })),
    [activeSteps, state],
  )

  const canProceed = useMemo(() => {
    if (!currentStep) return false
    if (currentStep.id === 'welcome') return true
    return validateStep(currentStep.id, state).ok
  }, [currentStep, state])

  const goNext = useCallback(() => {
    if (step < activeSteps.length - 1) {
      setStep(step + 1)
      setStepKey(k => k + 1)
      mainRef.current?.scrollTo({ top: 0, behavior: 'smooth' })
    }
  }, [step, activeSteps.length])

  const goBack = useCallback(() => {
    if (step > 0) {
      setStep(step - 1)
      setStepKey(k => k + 1)
      mainRef.current?.scrollTo({ top: 0, behavior: 'smooth' })
    }
  }, [step])

  // Keyboard shortcuts
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return
      if (e.key === 'Enter' && canProceed && !isLast) goNext()
      if (e.key === 'Escape' && !isFirst) goBack()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [canProceed, isLast, isFirst, goNext, goBack])

  const handleSave = useCallback(async () => {
    setSaving(true); setError('')
    try {
      const envContent = generateEnvContent(state)
      const rootEnvContent = generateRootEnvContent(state, envContent)
      const parse = (content: string) => {
        const vars: Record<string, string> = {}
        for (const line of content.split('\n')) {
          const t = line.trim()
          if (t && !t.startsWith('#') && t.includes('=')) { const i = t.indexOf('='); vars[t.slice(0, i)] = t.slice(i + 1) }
        }
        return vars
      }
      const res = await fetch('/api/setup', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          config_env: parse(envContent), root_env: parse(rootEnvContent),
          assets: { coinbase_pairs: state.exchanges.coinbase ? state.cryptoPairs : [], nordnet_pairs: state.exchanges.nordnet ? state.stockPairs : [], ibkr_pairs: state.exchanges.ibkr ? state.ibkrPairs : [] },
        }),
      })
      if (!res.ok) throw new Error(`Server error: ${await res.text()}`)
      localStorage.removeItem(STORAGE_KEY)
      setSaved(true)
    } catch (err: any) { setError(err.message || 'Failed to save') } finally { setSaving(false) }
  }, [state])

  const handleDownload = useCallback(() => {
    const blob = new Blob([generateEnvContent(state)], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a'); a.href = url; a.download = '.env'; a.click()
    URL.revokeObjectURL(url)
  }, [state])

  // ── Loading screen ──
  if (loading) {
    return (
      <div style={{
        height: '100vh', background: '#080c10', display: 'flex',
        alignItems: 'center', justifyContent: 'center',
        fontFamily: "'Inter', system-ui, sans-serif", color: '#8b949e',
      }}>
        <div style={{ textAlign: 'center' }}>
          <RefreshCw size={28} color="#22c55e" style={{ animation: 'at-spin 1s linear infinite', marginBottom: 16 }} />
          <div style={{ fontSize: 14 }}>Loading configuration…</div>
        </div>
      </div>
    )
  }

  // ── Success screen ──
  if (saved) {
    const envContent = generateEnvContent(state)
    const langfusePassword = envContent.match(/^LANGFUSE_ADMIN_PASSWORD=(.*)$/m)?.[1] || ''
    return (
      <div style={{
        minHeight: '100vh', height: '100vh', background: '#080c10', overflow: 'auto',
        fontFamily: "'Inter', system-ui, sans-serif",
      }}>
        <div style={{ maxWidth: 640, margin: '0 auto', padding: '60px 32px 80px' }}>
          <div style={{ textAlign: 'center', marginBottom: 40 }}>
            <div style={{
              width: 88, height: 88, borderRadius: '50%', margin: '0 auto 24px',
              background: 'rgba(34,197,94,0.12)', border: '2px solid #22c55e',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              animation: 'at-check-pop 0.5s ease-out both',
            }}>
              <Check size={44} color="#22c55e" />
            </div>
            <h1 style={{ margin: '0 0 8px 0', fontSize: 30, fontWeight: 800, color: '#e6edf3' }}>Setup Complete!</h1>
            <p style={{ margin: 0, fontSize: 15, color: '#8b949e', lineHeight: 1.6 }}>
              Configuration saved to <code style={{ color: '#22c55e' }}>config/.env</code>. You're ready to launch.
            </p>
          </div>

          {/* Quick commands */}
          <div style={{ ...card, marginBottom: 20 }}>
            <h3 style={{ margin: '0 0 14px 0', fontSize: 14, fontWeight: 700, color: '#c9d1d9', display: 'flex', alignItems: 'center', gap: 8 }}>
              <Terminal size={15} color="#22c55e" /> Quick Start Commands
            </h3>
            {[
              { cmd: 'docker compose up -d', desc: 'Start the full stack' },
              { cmd: 'docker compose logs -f', desc: 'Watch the logs' },
              { cmd: 'docker compose ps', desc: 'Check service status' },
              { cmd: 'docker compose down', desc: 'Stop everything' },
            ].map(({ cmd, desc }) => (
              <div key={cmd} style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                <code style={{
                  flex: 1, padding: '8px 12px', borderRadius: 6, background: '#0d1117',
                  color: '#22c55e', fontSize: 13, fontFamily: "'JetBrains Mono', monospace",
                }}>{cmd}</code>
                <CopyButton text={cmd} />
                <span style={{ fontSize: 12, color: '#484f58', minWidth: 130 }}>{desc}</span>
              </div>
            ))}
          </div>

          {/* Web UIs */}
          <div style={{ ...card, marginBottom: 20 }}>
            <h3 style={{ margin: '0 0 14px 0', fontSize: 14, fontWeight: 700, color: '#c9d1d9', display: 'flex', alignItems: 'center', gap: 8 }}>
              <Globe size={15} color="#22c55e" /> Web Interfaces
            </h3>
            {[
              { name: 'Dashboard', url: 'http://localhost:8090', info: '' },
              { name: 'Langfuse', url: 'http://localhost:3000', info: langfusePassword ? `admin@auto-traitor.local / ${langfusePassword}` : '' },
              { name: 'Temporal UI', url: 'http://localhost:8233', info: '' },
            ].map(u => (
              <div key={u.name} style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8, padding: '6px 0' }}>
                <span style={{ fontSize: 13, fontWeight: 600, color: '#c9d1d9', minWidth: 90 }}>{u.name}</span>
                <code style={{ fontSize: 12, color: '#58a6ff', fontFamily: "'JetBrains Mono', monospace" }}>{u.url}</code>
                {u.info && <span style={{ fontSize: 11, color: '#484f58', marginLeft: 'auto' }}>{u.info}</span>}
              </div>
            ))}
          </div>

          {/* Telegram commands */}
          {state.telegramEnabled && (
            <div style={{ ...card, marginBottom: 20 }}>
              <h3 style={{ margin: '0 0 14px 0', fontSize: 14, fontWeight: 700, color: '#c9d1d9', display: 'flex', alignItems: 'center', gap: 8 }}>
                <MessageSquare size={15} color="#22c55e" /> Telegram Commands
              </h3>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px 24px' }}>
                {[
                  ['/status', 'Portfolio overview'], ['/positions', 'Open positions'],
                  ['/trades', 'Recent trades'], ['/rotate', 'Force rotation check'],
                  ['/swaps', 'View pending swaps'], ['/fees', 'Fee configuration'],
                  ['/highstakes 4h', 'Enable high-stakes'], ['/pause', 'Pause trading'],
                ].map(([cmd, desc]) => (
                  <div key={cmd} style={{ display: 'flex', gap: 8, padding: '4px 0' }}>
                    <code style={{ fontSize: 12, color: '#c084fc', fontFamily: "'JetBrains Mono', monospace", minWidth: 110 }}>{cmd}</code>
                    <span style={{ fontSize: 12, color: '#6e7681' }}>{desc}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div style={{ display: 'flex', gap: 12, justifyContent: 'center', marginTop: 32 }}>
            <button type="button" onClick={() => navigate('/')} style={{
              padding: '12px 32px', borderRadius: 10, background: '#22c55e', color: '#000',
              border: 'none', fontSize: 14, fontWeight: 700, cursor: 'pointer',
            }}>Go to Dashboard</button>
            <button type="button" onClick={handleDownload} style={{
              padding: '12px 24px', borderRadius: 10, background: 'transparent', color: '#8b949e',
              border: '1px solid #30363d', fontSize: 14, fontWeight: 600, cursor: 'pointer',
              display: 'flex', alignItems: 'center', gap: 8,
            }}><Download size={16} /> Download .env</button>
          </div>
        </div>
      </div>
    )
  }

  // ── Main wizard layout ──
  const renderStep = () => {
    if (!currentStep) return null
    const props = { state, update }
    switch (currentStep.id) {
      case 'welcome': return <StepWelcome onStart={goNext} />
      case 'exchange': return <StepExchange {...props} />
      case 'mode': return <StepTradingMode {...props} />
      case 'assets': return <StepAssets {...props} onSkip={goNext} />
      case 'coinbase': return <StepCoinbaseApi {...props} onSkip={goNext} />
      case 'ibkr': return <StepIbkrConnection {...props} />
      case 'llm': return <StepLLM {...props} />
      case 'telegram': return <StepTelegram {...props} onSkip={goNext} />
      case 'news': return <StepNews {...props} onSkip={goNext} />
      case 'review': return <StepReview state={state} stepsWithValidation={stepsWithValidation} />
      default: return null
    }
  }

  return (
    <div style={{
      height: '100vh', background: '#080c10',
      fontFamily: "'Inter', system-ui, sans-serif", color: '#e6edf3',
      display: 'flex', flexDirection: 'column',
    }}>
      {/* Header */}
      <header style={{
        padding: '12px 28px', borderBottom: '1px solid #21262d', background: '#0d1117',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexShrink: 0,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div style={{
            width: 34, height: 34, borderRadius: 8,
            background: 'linear-gradient(135deg, #22c55e, #16a34a)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}>
            <Sparkles size={18} color="#fff" />
          </div>
          <div>
            <div style={{ fontSize: 15, fontWeight: 800, letterSpacing: 0.5 }}>AUTO-TRAITOR</div>
            <div style={{ fontSize: 10, color: '#484f58', textTransform: 'uppercase', letterSpacing: 1 }}>Setup Wizard</div>
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: 11, color: '#484f58' }}>
            <kbd style={{ padding: '1px 5px', borderRadius: 4, background: '#161b22', border: '1px solid #30363d', fontSize: 10 }}>Enter</kbd> next
            &nbsp;&nbsp;
            <kbd style={{ padding: '1px 5px', borderRadius: 4, background: '#161b22', border: '1px solid #30363d', fontSize: 10 }}>Esc</kbd> back
          </span>
          <button type="button" onClick={handleDownload} style={{
            padding: '6px 14px', borderRadius: 8, background: 'transparent', color: '#6e7681',
            border: '1px solid #30363d', fontSize: 12, cursor: 'pointer',
            display: 'flex', alignItems: 'center', gap: 6,
          }}><Download size={13} /> .env</button>
        </div>
      </header>

      <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
        {/* Sidebar */}
        {!isWelcome && (
          <nav style={{
            width: 250, padding: '20px 12px', borderRight: '1px solid #21262d',
            background: '#0d1117', overflowY: 'auto', flexShrink: 0,
          }}>
            <div style={{ fontSize: 10, fontWeight: 700, color: '#484f58', textTransform: 'uppercase', letterSpacing: 1.2, marginBottom: 14, paddingLeft: 12 }}>
              Setup Steps
            </div>
            {activeSteps.filter(s => s.id !== 'welcome').map((s, rawI) => {
              const i = rawI + 1 // offset for welcome
              const Icon = s.icon
              const isCurrent = i === step
              const isDone = i < step
              const v = stepsWithValidation.find(sv => sv.id === s.id)?.validation
              const hasIssues = isDone && v && !v.ok

              return (
                <div key={s.id} style={{ position: 'relative' }}>
                  {/* Connector line */}
                  {rawI < activeSteps.length - 2 && (
                    <div style={{
                      position: 'absolute', left: 25, top: 40, width: 2, height: 12,
                      background: isDone ? 'rgba(34,197,94,0.3)' : '#21262d',
                    }} />
                  )}
                  <button
                    type="button"
                    onClick={() => { if (i <= step) { setStep(i); setStepKey(k => k + 1); mainRef.current?.scrollTo({ top: 0, behavior: 'smooth' }) } }}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 10,
                      width: '100%', padding: '9px 12px', borderRadius: 8,
                      background: isCurrent ? 'rgba(34,197,94,0.06)' : 'transparent',
                      border: isCurrent ? '1px solid rgba(34,197,94,0.15)' : '1px solid transparent',
                      color: isCurrent ? '#22c55e' : isDone ? '#4ade80' : '#6e7681',
                      fontSize: 13, fontWeight: isCurrent ? 600 : 400,
                      cursor: i <= step ? 'pointer' : 'default',
                      textAlign: 'left', marginBottom: 2,
                      opacity: i > step ? 0.4 : 1, transition: 'all 0.15s',
                    }}
                  >
                    <div style={{
                      width: 26, height: 26, borderRadius: 7,
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      background: isDone ? (hasIssues ? 'rgba(234,179,8,0.15)' : 'rgba(34,197,94,0.12)') : isCurrent ? 'rgba(34,197,94,0.08)' : '#161b22',
                      border: `1px solid ${isDone ? (hasIssues ? 'rgba(234,179,8,0.3)' : 'rgba(34,197,94,0.25)') : isCurrent ? 'rgba(34,197,94,0.15)' : '#30363d'}`,
                      flexShrink: 0,
                    }}>
                      {isDone ? (hasIssues ? <CircleAlert size={12} color="#eab308" /> : <Check size={12} />) : <Icon size={12} />}
                    </div>
                    <span style={{ flex: 1 }}>{s.title}</span>
                    <span style={{ fontSize: 10, fontWeight: 700, color: '#484f58' }}>{rawI + 1}</span>
                  </button>
                </div>
              )
            })}
          </nav>
        )}

        {/* Main content */}
        <main ref={mainRef} style={{ flex: 1, overflowY: 'auto', padding: isWelcome ? '40px 48px 100px' : '28px 48px 120px' }}>
          <div style={{ maxWidth: 740 }}>
            {/* Progress bar (hidden on welcome) */}
            {!isWelcome && (
              <div style={{ marginBottom: 28 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
                  <span style={{ fontSize: 11, color: '#484f58' }}>Step {step} of {activeSteps.length - 1}</span>
                  <span style={{ fontSize: 11, color: '#484f58' }}>{Math.round((step / (activeSteps.length - 1)) * 100)}%</span>
                </div>
                <div style={{ height: 3, background: '#21262d', borderRadius: 2, overflow: 'hidden' }}>
                  <div style={{
                    height: '100%', borderRadius: 2, transition: 'width 0.4s ease',
                    background: 'linear-gradient(90deg, #22c55e, #16a34a)',
                    width: `${(step / (activeSteps.length - 1)) * 100}%`,
                  }} />
                </div>
              </div>
            )}

            {/* Step content with animation */}
            <div key={stepKey} className="at-step-enter">
              {renderStep()}
            </div>

            {/* Error */}
            {error && (
              <div style={{
                marginTop: 16, padding: '12px 16px', borderRadius: 10,
                background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.2)',
                color: '#fca5a5', fontSize: 13, display: 'flex', alignItems: 'center', gap: 10,
              }}>
                <CircleAlert size={16} />
                <span style={{ flex: 1 }}>{error}</span>
                <button type="button" onClick={handleSave} style={{
                  background: 'rgba(239,68,68,0.15)', border: '1px solid rgba(239,68,68,0.25)',
                  color: '#fca5a5', borderRadius: 6, padding: '4px 12px', cursor: 'pointer',
                  fontSize: 12, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 4,
                }}>
                  <RefreshCw size={12} /> Retry
                </button>
              </div>
            )}
          </div>
        </main>
      </div>

      {/* Footer nav (hidden on welcome) */}
      {!isWelcome && (
        <footer style={{
          padding: '14px 28px', borderTop: '1px solid #21262d', background: '#0d1117',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexShrink: 0,
        }}>
          <button type="button" onClick={goBack} disabled={step <= 1} style={{
            padding: '9px 22px', borderRadius: 8,
            background: 'transparent', color: step <= 1 ? '#21262d' : '#8b949e',
            border: `1px solid ${step <= 1 ? '#161b22' : '#30363d'}`,
            fontSize: 14, fontWeight: 600, cursor: step <= 1 ? 'default' : 'pointer',
            display: 'flex', alignItems: 'center', gap: 8,
          }}>
            <ArrowLeft size={16} /> Back
          </button>

          <div style={{ display: 'flex', gap: 10 }}>
            {isLast ? (
              <>
                <button type="button" onClick={handleDownload} style={{
                  padding: '9px 22px', borderRadius: 8, background: 'transparent', color: '#8b949e',
                  border: '1px solid #30363d', fontSize: 14, fontWeight: 600, cursor: 'pointer',
                  display: 'flex', alignItems: 'center', gap: 8,
                }}>
                  <Download size={15} /> Download Only
                </button>
                <button type="button" onClick={handleSave} disabled={saving} style={{
                  padding: '9px 28px', borderRadius: 8,
                  background: saving ? '#15803d' : 'linear-gradient(135deg, #22c55e, #16a34a)',
                  color: '#fff', border: 'none', fontSize: 14, fontWeight: 700,
                  cursor: saving ? 'wait' : 'pointer',
                  display: 'flex', alignItems: 'center', gap: 8,
                  boxShadow: '0 2px 16px rgba(34,197,94,0.3)',
                }}>
                  {saving ? (
                    <><RefreshCw size={15} style={{ animation: 'at-spin 1s linear infinite' }} /> Saving...</>
                  ) : (
                    <><Check size={15} /> Save Configuration</>
                  )}
                </button>
              </>
            ) : (
              <button type="button" onClick={goNext} disabled={!canProceed} style={{
                padding: '9px 28px', borderRadius: 8,
                background: canProceed ? 'linear-gradient(135deg, #22c55e, #16a34a)' : '#21262d',
                color: canProceed ? '#fff' : '#484f58', border: 'none',
                fontSize: 14, fontWeight: 700, cursor: canProceed ? 'pointer' : 'default',
                display: 'flex', alignItems: 'center', gap: 8,
                boxShadow: canProceed ? '0 2px 16px rgba(34,197,94,0.3)' : 'none',
              }}>
                Continue <ArrowRight size={15} />
              </button>
            )}
          </div>
        </footer>
      )}
    </div>
  )
}
