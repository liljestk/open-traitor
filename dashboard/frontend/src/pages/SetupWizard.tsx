/**
 * SetupWizard.tsx — Main wizard component + StepReview.
 * Step components and UI primitives are in ./wizard/WizardSteps.tsx.
 * Types, constants, validation, and generators are in ./wizard/wizardData.ts.
 */
import { useState, useCallback, useMemo, useEffect, useRef, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  ArrowRight, ArrowLeft, Check, Download,
  Sparkles, Coins, BarChart3, Newspaper, Activity,
  Settings2, MessageSquare, TrendingUp, Rocket,
  ChevronDown, ChevronRight, RefreshCw, CircleAlert, CheckCircle2,
  Terminal, Globe, Server,
} from 'lucide-react'
import {
  type WizardState, type StepValidation,
  STORAGE_KEY, INITIAL_STATE, card,
  isValidTelegramToken, validateStep,
  generateEnvContent, generateRootEnvContent,
} from './wizard/wizardData'
import {
  useInjectCSS, CopyButton, Tip, Warning, SectionHeader,
} from './wizard/WizardPrimitives'
import {
  StepWelcome, StepExchange, StepTradingMode, StepAssets,
  StepCoinbaseApi, StepIbkrConnection, StepLLM, StepTelegram, StepNews,
} from './wizard/WizardSteps'

/* ═══════════════════════════════════════════════════════════════════════════
   Steps Definition
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

/* ═══════════════════════════════════════════════════════════════════════════
   Step: Review & Save
   ═══════════════════════════════════════════════════════════════════════════ */

function StepReview({ state, initialServerState, stepsWithValidation: _stepsWithValidation }: { state: WizardState; initialServerState: WizardState | null; stepsWithValidation: { id: string; title: string; validation: StepValidation }[] }) {
  const [expandedEnv, setExpandedEnv] = useState(false)
  const envPreview = useMemo(() => generateEnvContent(state), [state])

  const changes = useMemo(() => {
    if (!initialServerState) return null
    const diffs: { label: string; oldValue: string; newValue: string }[] = []

    // Exchanges
    const wasEx = [
      initialServerState.exchanges.coinbase ? 'Coinbase' : null,
      initialServerState.exchanges.ibkr ? 'IBKR' : null,
    ].filter(Boolean).join(' + ') || 'None'
    const nowEx = [
      state.exchanges.coinbase ? 'Coinbase' : null,
      state.exchanges.ibkr ? 'IBKR' : null,
    ].filter(Boolean).join(' + ') || 'None'
    if (wasEx !== nowEx) diffs.push({ label: 'Exchanges', oldValue: wasEx, newValue: nowEx })

    // Trading Mode
    if (initialServerState.tradingMode !== state.tradingMode) {
      diffs.push({ label: 'Trading Mode', oldValue: initialServerState.tradingMode, newValue: state.tradingMode })
    }

    // Crypto Pairs
    const wasCrypto = initialServerState.cryptoPairs.join(', ') || 'None'
    const nowCrypto = state.cryptoPairs.join(', ') || 'None'
    if (wasCrypto !== nowCrypto) diffs.push({ label: 'Crypto Pairs', oldValue: wasCrypto, newValue: nowCrypto })

    // IBKR Pairs
    const wasIbkr = initialServerState.ibkrPairs.join(', ') || 'None'
    const nowIbkr = state.ibkrPairs.join(', ') || 'None'
    if (wasIbkr !== nowIbkr) diffs.push({ label: 'IBKR Pairs', oldValue: wasIbkr, newValue: nowIbkr })

    // LLM Models
    if (initialServerState.ollamaModel !== state.ollamaModel) diffs.push({ label: 'Ollama Model', oldValue: initialServerState.ollamaModel || 'None', newValue: state.ollamaModel || 'None' })

    const wasLlm = [initialServerState.geminiEnabled && 'Gemini', initialServerState.openrouterEnabled && 'OpenRouter', initialServerState.openaiEnabled && 'OpenAI'].filter(Boolean).join(', ') || 'None'
    const nowLlm = [state.geminiEnabled && 'Gemini', state.openrouterEnabled && 'OpenRouter', state.openaiEnabled && 'OpenAI'].filter(Boolean).join(', ') || 'None'
    if (wasLlm !== nowLlm) diffs.push({ label: 'Cloud LLMs', oldValue: wasLlm, newValue: nowLlm })

    return diffs.length > 0 ? diffs : null
  }, [state, initialServerState])

  const sections = useMemo(() => {
    const s: { title: string; icon: ReactNode; items: { label: string; value: string; ok: boolean }[] }[] = []
    const ex: string[] = []
    if (state.exchanges.coinbase) ex.push('Coinbase (Crypto)')
    if (state.exchanges.ibkr) ex.push('Interactive Brokers (US Equities)')
    s.push({ title: 'Exchanges', icon: <BarChart3 size={14} />, items: [{ label: 'Active', value: ex.join(' + '), ok: ex.length > 0 }] })
    s.push({ title: 'Trading Mode', icon: <Settings2 size={14} />, items: [{ label: 'Mode', value: state.tradingMode === 'paper' ? 'Paper (simulated)' : 'LIVE (real money)', ok: state.tradingMode === 'paper' || state.liveConfirmed }] })

    const assets: { label: string; value: string; ok: boolean }[] = []
    if (state.exchanges.coinbase) assets.push({ label: 'Crypto', value: `${state.cryptoPairs.length} pairs`, ok: state.cryptoPairs.length > 0 })
    if (state.exchanges.ibkr) assets.push({ label: 'IBKR Stocks', value: `${state.ibkrPairs.length} pairs`, ok: state.ibkrPairs.length > 0 })
    s.push({ title: 'Assets', icon: <TrendingUp size={14} />, items: assets })

    if (state.exchanges.coinbase) {
      const hasKey = !!state.coinbaseApiKey && !!state.coinbaseApiSecret
      s.push({ title: 'Coinbase API', icon: <Coins size={14} />, items: [{ label: 'Credentials', value: hasKey ? 'Configured' : state.tradingMode === 'paper' ? 'Skipped (paper)' : 'MISSING', ok: hasKey || state.tradingMode === 'paper' }] })
    }

    if (state.exchanges.ibkr) {
      s.push({
        title: 'IBKR Connection', icon: <BarChart3 size={14} />, items: [
          { label: 'Gateway', value: `${state.ibkrHost}:${state.ibkrPort}`, ok: !!state.ibkrHost && !!state.ibkrPort },
          { label: 'Client ID', value: state.ibkrClientId || '1', ok: true },
          { label: 'Currency', value: state.ibkrCurrency, ok: true },
        ]
      })
    }

    const llm: { label: string; value: string; ok: boolean }[] = []
    if (state.geminiEnabled) llm.push({ label: 'Gemini', value: state.geminiApiKey ? 'Configured' : 'Key missing', ok: !!state.geminiApiKey })
    if (state.openrouterEnabled) llm.push({ label: 'OpenRouter', value: state.openrouterApiKey ? 'Configured' : 'Key missing', ok: !!state.openrouterApiKey })
    if (state.openaiEnabled) llm.push({ label: 'OpenAI', value: state.openaiApiKey ? 'Configured' : 'Key missing', ok: !!state.openaiApiKey })
    llm.push({ label: 'Ollama', value: state.ollamaModel, ok: true })
    s.push({ title: 'LLM Providers', icon: <Sparkles size={14} />, items: llm })

    if (state.telegramEnabled) {
      const tg: { label: string; value: string; ok: boolean }[] = []
      tg.push({ label: 'User ID', value: state.telegramUserId || 'MISSING', ok: !!state.telegramUserId })
      if (state.exchanges.coinbase) tg.push({ label: 'Crypto Bot', value: state.telegramCoinbaseBotToken ? (isValidTelegramToken(state.telegramCoinbaseBotToken) ? 'Valid' : 'Bad format') : 'MISSING', ok: isValidTelegramToken(state.telegramCoinbaseBotToken) })
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

      {changes && (
        <div style={{ ...card, padding: 16, marginBottom: 16, marginTop: allOk ? 0 : 16, borderColor: '#3b82f6', background: 'rgba(59,130,246,0.05)' }}>
          <h4 style={{ margin: '0 0 10px 0', fontSize: 13, fontWeight: 700, color: '#60a5fa', display: 'flex', alignItems: 'center', gap: 6 }}>
            <Activity size={14} /> Changes from current setup
          </h4>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {changes.map(diff => (
              <div key={diff.label} style={{ fontSize: 12 }}>
                <strong style={{ color: '#c9d1d9' }}>{diff.label}:&nbsp;</strong>
                <span style={{ color: '#ef4444', textDecoration: 'line-through' }}>{diff.oldValue}</span>
                <span style={{ color: '#8b949e', margin: '0 4px' }}>&rarr;</span>
                <span style={{ color: '#4ade80' }}>{diff.newValue}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: (allOk && !changes) ? 0 : 16 }}>
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
   Main Wizard Component
   ═══════════════════════════════════════════════════════════════════════════ */

export default function SetupWizard() {
  useInjectCSS()
  const navigate = useNavigate()
  const [state, setState] = useState<WizardState>(() => {
    try { const s = localStorage.getItem(STORAGE_KEY); return s ? { ...INITIAL_STATE, ...JSON.parse(s) } : INITIAL_STATE }
    catch { return INITIAL_STATE }
  })
  const [initialServerState, setInitialServerState] = useState<WizardState | null>(null)
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
      ; (async () => {
        try {
          const res = await fetch('/api/setup')
          if (!res.ok) throw new Error('fetch failed')
          const data = await res.json()
          if (!cancelled && data?.exists) {
            const loadedState = {
              ...INITIAL_STATE,
              ...data,
              exchanges: { ...INITIAL_STATE.exchanges, ...(data.exchanges || {}) },
              infraSecrets: data.infraSecrets || {},
            };
            setState(prev => ({ ...prev, ...loadedState }))
            setInitialServerState(loadedState)
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
    const { coinbaseApiKey, coinbaseApiSecret, geminiApiKey, openrouterApiKey, openaiApiKey,
      telegramCoinbaseBotToken, telegramIbkrBotToken, redditClientSecret, infraSecrets, ...safe } = state
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
          assets: { coinbase_pairs: state.exchanges.coinbase ? state.cryptoPairs : [], ibkr_pairs: state.exchanges.ibkr ? state.ibkrPairs : [] },
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
      case 'review': return <StepReview state={state} initialServerState={initialServerState} stepsWithValidation={stepsWithValidation} />
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
                    onClick={() => { setStep(i); setStepKey(k => k + 1); mainRef.current?.scrollTo({ top: 0, behavior: 'smooth' }) }}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 10,
                      width: '100%', padding: '9px 12px', borderRadius: 8,
                      background: isCurrent ? 'rgba(34,197,94,0.06)' : 'transparent',
                      border: isCurrent ? '1px solid rgba(34,197,94,0.15)' : '1px solid transparent',
                      color: isCurrent ? '#22c55e' : isDone ? '#4ade80' : '#6e7681',
                      fontSize: 13, fontWeight: isCurrent ? 600 : 400,
                      cursor: 'pointer',
                      textAlign: 'left', marginBottom: 2,
                      opacity: 1, transition: 'all 0.15s',
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
