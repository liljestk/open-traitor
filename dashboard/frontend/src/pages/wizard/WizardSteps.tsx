/**
 * WizardSteps.tsx — Step components for the Setup Wizard.
 * UI primitives are in ./WizardPrimitives.tsx.
 */
import { type ReactNode } from 'react'
import {
  Check, Plus, X, Zap, Server, Cloud, Bot,
  Coins, BarChart3, Newspaper, Activity, Sparkles, Settings2,
  MonitorDot, MessageSquare, TrendingUp, Rocket, Shield,
  KeyRound, Globe,
} from 'lucide-react'
import {
  type WizardState,
  POPULAR_CRYPTO, POPULAR_IBKR_STOCKS, OLLAMA_MODELS,
  card, inputBase, mono, isValidTelegramToken,
} from './wizardData'
import {
  Tip, Warning, SecurityBox, HowTo, PasswordInput, ToggleChip,
  SectionHeader, FormField, ValidationBadge, SkipLink,
} from './WizardPrimitives'

/* ═══════════════════════════════════════════════════════════════════════════
   Step: Welcome
   ═══════════════════════════════════════════════════════════════════════════ */

export function StepWelcome({ onStart }: { onStart: () => void }) {
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
            { icon: <BarChart3 size={14} />, text: 'Exchange account (Coinbase + optionally IBKR)', required: true },
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
            ['Exchange connection', 'Coinbase crypto + IBKR equities'],
            ['Trading mode', 'Paper (simulated) or Live (real money)'],
            ['Asset universe', 'Which crypto & stocks the agent monitors'],
            ['AI brain', 'Multi-provider LLM chain (Groq / Gemini / OpenRouter / Ollama)'],
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
   Step: Exchange
   ═══════════════════════════════════════════════════════════════════════════ */

export function StepExchange({ state, update }: { state: WizardState; update: (p: Partial<WizardState>) => void }) {
  const { exchanges } = state
  const cards: { key: 'coinbase' | 'ibkr'; icon: typeof Coins; color: string; title: string; sub: string; desc: string; tags: string[] }[] = [
    {
      key: 'coinbase', icon: Coins, color: '#3b82f6',
      title: 'Coinbase', sub: 'Cryptocurrency', desc: 'Trade crypto assets like BTC, ETH, SOL on Coinbase Advanced Trade. Supports paper and live trading with real-time WebSocket price feeds.',
      tags: ['BTC', 'ETH', 'SOL', 'ADA', 'DOGE'],
    },
    {
      key: 'ibkr', icon: BarChart3, color: '#e11d48',
      title: 'Interactive Brokers', sub: 'US Equities (IBKR)', desc: 'Trade US equities via IB Gateway / TWS. Supports paper and live trading with IBKR tiered commission model. USD-denominated.',
      tags: ['AAPL', 'MSFT', 'GOOGL', 'NVDA', 'AMZN'],
    },
  ]

  const selectExchange = (key: 'coinbase' | 'ibkr') => {
    // Both toggle independently
    update({ exchanges: { ...exchanges, [key]: !exchanges[key] } })
  }

  return (
    <>
      <SectionHeader
        icon={<BarChart3 size={22} />}
        title="Choose Your Exchanges"
        subtitle="Pick Coinbase for crypto, plus optionally IBKR for equities."
      />
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
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
      {!exchanges.coinbase && !exchanges.ibkr && (
        <div style={{ marginTop: 16 }}><Warning>Please select at least one exchange to continue.</Warning></div>
      )}
    </>
  )
}

/* ═══════════════════════════════════════════════════════════════════════════
   Step: Trading Mode
   ═══════════════════════════════════════════════════════════════════════════ */

export function StepTradingMode({ state, update }: { state: WizardState; update: (p: Partial<WizardState>) => void }) {
  return (
    <>
      <SectionHeader icon={<Settings2 size={22} />} title="Trading Mode" subtitle="Choose how the agent trades. You can always switch modes later from the Settings page." />
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        {([
          {
            mode: 'paper' as const, icon: MonitorDot, title: 'Paper Trading', color: '#22c55e', tag: 'RECOMMENDED',
            desc: 'Simulated trading with no real money. Perfect for testing strategies and building confidence. All trades tracked like real ones.'
          },
          {
            mode: 'live' as const, icon: Zap, title: 'Live Trading', color: '#ef4444', tag: '',
            desc: 'Real money trading on the exchange. The agent executes actual buy/sell orders. Make sure you understand the risks.'
          },
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

/* ═══════════════════════════════════════════════════════════════════════════
   Step: Assets
   ═══════════════════════════════════════════════════════════════════════════ */

export function StepAssets({ state, update, onSkip: _onSkip }: { state: WizardState; update: (p: Partial<WizardState>) => void; onSkip: () => void }) {
  const toggle = (list: string[], id: string) =>
    list.includes(id) ? list.filter(p => p !== id) : [...list, id]
  const addCustom = (field: 'cryptoPairs' | 'ibkrPairs', inputField: 'customCryptoPair' | 'customIbkrPair') => {
    const v = (state[inputField] as string).trim().toUpperCase()
    if (v && !(state[field] as string[]).includes(v)) {
      update({ [field]: [...(state[field] as string[]), v], [inputField]: '' })
    }
  }

  const renderPairSection = (opts: {
    title: string; icon: ReactNode; color: string;
    items: { id: string; name: string; symbol?: string; sector?: string }[];
    pairs: string[]; pairsKey: 'cryptoPairs' | 'ibkrPairs';
    custom: string; customKey: 'customCryptoPair' | 'customIbkrPair';
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
      <div style={{
        padding: '10px 14px', background: '#3b82f608', border: '1px solid #3b82f622',
        borderRadius: 8, marginTop: 12, fontSize: 12, color: '#8b949e', display: 'flex', alignItems: 'center', gap: 8,
      }}>
        <Activity size={14} style={{ color: '#60a5fa', flexShrink: 0 }} />
        <span>
          The number of pairs you can actively track depends on your LLM provider's RPM limit.
          Free-tier providers (Groq 30 RPM, OpenRouter 20 RPM, Gemini 14 RPM) support 5–10 pairs; paid providers support up to 30.
          Don't worry — you can select more here, the system auto-adjusts at runtime.
        </span>
      </div>
      <div style={{ marginTop: 16 }}>
        {state.exchanges.coinbase && renderPairSection({
          title: 'Cryptocurrency Pairs', icon: <Coins size={18} />, color: '#3b82f6',
          items: POPULAR_CRYPTO, pairs: state.cryptoPairs, pairsKey: 'cryptoPairs',
          custom: state.customCryptoPair, customKey: 'customCryptoPair',
          placeholder: 'Add custom pair (e.g. PEPE-EUR)',
          subtitle: 'Select which crypto assets to monitor on Coinbase.',
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

/* ═══════════════════════════════════════════════════════════════════════════
   Step: Coinbase API
   ═══════════════════════════════════════════════════════════════════════════ */

export function StepCoinbaseApi({ state, update, onSkip }: { state: WizardState; update: (p: Partial<WizardState>) => void; onSkip: () => void }) {
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

/* ═══════════════════════════════════════════════════════════════════════════
   Step: IBKR Connection
   ═══════════════════════════════════════════════════════════════════════════ */

export function StepIbkrConnection({ state, update }: { state: WizardState; update: (p: Partial<WizardState>) => void }) {
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

/* ═══════════════════════════════════════════════════════════════════════════
   Step: LLM
   ═══════════════════════════════════════════════════════════════════════════ */

export function StepLLM({ state, update }: { state: WizardState; update: (p: Partial<WizardState>) => void }) {
  return (
    <>
      <SectionHeader icon={<Sparkles size={22} />} title="AI / LLM Configuration" subtitle="Configure the AI brain. Requests try providers in order: Groq → OpenRouter → Gemini → OpenAI → Ollama (local fallback)." />

      <h3 style={{ margin: '0 0 12px 0', fontSize: 15, fontWeight: 700, color: '#e6edf3', display: 'flex', alignItems: 'center', gap: 8 }}>
        <Cloud size={16} color="#6e7681" /> Cloud Providers
        <span style={{ fontSize: 12, fontWeight: 400, color: '#484f58' }}>Optional &mdash; faster than local</span>
      </h3>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 16, marginBottom: 28 }}>
        {([
          {
            key: 'groq' as const, enabled: state.groqEnabled, apiKey: state.groqApiKey,
            enabledKey: 'groqEnabled', apiKeyKey: 'groqApiKey',
            icon: Zap, color: '#f97316', title: 'Groq', model: 'llama-3.3-70b-versatile', rate: '30 RPM · 1K req/day free',
            placeholder: 'gsk_...', steps: ['Go to console.groq.com/keys', 'Click "Create API Key"', 'Copy the key (starts with gsk_)'],
            link: { url: 'https://console.groq.com/keys', label: 'Get key' },
          },
          {
            key: 'gemini' as const, enabled: state.geminiEnabled, apiKey: state.geminiApiKey,
            enabledKey: 'geminiEnabled', apiKeyKey: 'geminiApiKey',
            icon: Zap, color: '#3b82f6', title: 'Google Gemini', model: 'gemini-2.0-flash', rate: '14 RPM free',
            placeholder: 'AIza...', steps: ['Go to aistudio.google.com/app/apikey', 'Click "Create API key"', 'Copy the key'],
            link: { url: 'https://aistudio.google.com/app/apikey', label: 'Get key' },
          },
          {
            key: 'openrouter' as const, enabled: state.openrouterEnabled, apiKey: state.openrouterApiKey,
            enabledKey: 'openrouterEnabled', apiKeyKey: 'openrouterApiKey',
            icon: Cloud, color: '#f59e0b', title: 'OpenRouter', model: '200+ models (free tier)', rate: 'Free models available',
            placeholder: 'sk-or-...', steps: ['Go to openrouter.ai/keys', 'Sign in and click "Create Key"', 'Copy the key (starts with sk-or-)'],
            link: { url: 'https://openrouter.ai/keys', label: 'Get key' },
          },
          {
            key: 'openai' as const, enabled: state.openaiEnabled, apiKey: state.openaiApiKey,
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

      {/* RPM Budget Explanation */}
      <div style={{
        padding: '14px 18px', background: 'linear-gradient(135deg, #3b82f608, #3b82f615)',
        border: '1px solid #3b82f633', borderRadius: 10, marginBottom: 24,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          <Activity size={16} style={{ color: '#60a5fa' }} />
          <span style={{ fontWeight: 700, fontSize: 13, color: '#e6edf3' }}>How RPM Limits Affect Trading Capacity</span>
        </div>
        <p style={{ margin: 0, fontSize: 12, color: '#8b949e', lineHeight: 1.6 }}>
          Each trading cycle uses ~2 LLM calls per tracked asset. Your provider's <strong style={{ color: '#c9d1d9' }}>requests-per-minute (RPM)</strong> limit
          determines how many assets the agent can monitor simultaneously.
        </p>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginTop: 12 }}>
          {([
            { label: 'Groq Free', rpm: 30, pairs: 10, color: '#f97316' },
            { label: 'Gemini Free', rpm: 14, pairs: 5, color: '#3b82f6' },
            { label: 'OpenRouter Free', rpm: 20, pairs: 8, color: '#f59e0b' },
            { label: 'OpenAI Paid', rpm: 450, pairs: 30, color: '#10b981' },
          ]).map(p => (
            <div key={p.label} style={{
              padding: '10px 14px', background: '#0d111788', borderRadius: 8,
              border: `1px solid ${p.color}22`,
            }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: p.color }}>{p.label}</div>
              <div style={{ fontSize: 11, color: '#6e7681', marginTop: 2 }}>{p.rpm} RPM → up to <strong style={{ color: '#e6edf3' }}>{p.pairs} pairs</strong></div>
            </div>
          ))}
        </div>
        <p style={{ margin: '10px 0 0', fontSize: 11, color: '#6e7681' }}>
          You can adjust <strong style={{ color: '#8b949e' }}>max_active_pairs</strong> in Settings after setup. The system auto-clamps to a safe value based on your provider's RPM.
        </p>
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

/* ═══════════════════════════════════════════════════════════════════════════
   Step: Telegram
   ═══════════════════════════════════════════════════════════════════════════ */

export function StepTelegram({ state, update, onSkip }: { state: WizardState; update: (p: Partial<WizardState>) => void; onSkip: () => void }) {
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

/* ═══════════════════════════════════════════════════════════════════════════
   Step: News
   ═══════════════════════════════════════════════════════════════════════════ */

export function StepNews({ state, update, onSkip }: { state: WizardState; update: (p: Partial<WizardState>) => void; onSkip: () => void }) {
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
