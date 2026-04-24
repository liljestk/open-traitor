import { useQuery } from '@tanstack/react-query'
import { useLiveStore } from '../store'
import { fetchMacroRegime } from '../api'

/**
 * Compact macro-regime badge for the global header.
 *
 * Pulls the cross-asset macro view (/api/quant/macro_regime) and surfaces:
 *  - the *active profile's* per-domain regime (TRENDING_UP, HIGH_VOL, …)
 *  - the consensus across both domains (RISK_ON / RISK_OFF / MIXED)
 *
 * The regime drives the strategy-overlay tilts in the trading pipeline,
 * so showing it prominently turns an opaque internal state into a visible,
 * actionable signal for the operator.
 */

const REGIME_COLORS: Record<string, { fg: string; bg: string }> = {
  RISK_ON:        { fg: '#22c55e', bg: 'rgba(34,197,94,0.12)' },
  RISK_OFF:       { fg: '#ef4444', bg: 'rgba(239,68,68,0.12)' },
  MIXED:          { fg: '#f59e0b', bg: 'rgba(245,158,11,0.12)' },
  UNKNOWN:        { fg: '#8b949e', bg: 'rgba(139,148,158,0.10)' },
  TRENDING_UP:    { fg: '#22c55e', bg: 'rgba(34,197,94,0.12)' },
  TRENDING_DOWN:  { fg: '#ef4444', bg: 'rgba(239,68,68,0.12)' },
  MEAN_REVERTING: { fg: '#3b82f6', bg: 'rgba(59,130,246,0.12)' },
  HIGH_VOL:       { fg: '#f59e0b', bg: 'rgba(245,158,11,0.12)' },
  RANGING:        { fg: '#8b949e', bg: 'rgba(139,148,158,0.10)' },
}

function regimeColors(regime: string | undefined) {
  return REGIME_COLORS[(regime || 'UNKNOWN').toUpperCase()] ?? REGIME_COLORS.UNKNOWN
}

function profileToExchange(profile: string): string {
  if (profile === 'crypto') return 'coinbase'
  if (profile === 'ibkr') return 'ibkr'
  return profile
}

export default function RegimeBadge({ compact = false }: { compact?: boolean }) {
  const profile = useLiveStore((s) => s.profile) || 'crypto'

  // queryKey includes profile (domain-separation rule).
  const { data } = useQuery({
    queryKey: ['macro-regime', profile],
    queryFn: fetchMacroRegime,
    refetchInterval: 60_000, // every 1 minute
    staleTime: 30_000,
  })

  if (!data?.available) {
    return null
  }

  const exch = profileToExchange(profile)
  const profileSnap = data.profiles?.[exch]
  const localRegime = profileSnap?.regime?.toUpperCase()
  const consensusRegime = data.consensus?.regime?.toUpperCase()

  const local = regimeColors(localRegime)
  const cons = regimeColors(consensusRegime)

  const conf = profileSnap?.confidence
  const confPct = typeof conf === 'number' ? Math.round(conf * 100) : null

  const tooltip = [
    consensusRegime ? `Macro consensus: ${consensusRegime}` : null,
    data.consensus?.rationale ? `(${data.consensus.rationale})` : null,
    localRegime ? `Local: ${localRegime}${confPct !== null ? ` @ ${confPct}%` : ''}` : null,
  ].filter(Boolean).join(' · ')

  if (compact) {
    return (
      <span
        title={tooltip}
        style={{
          fontSize: 9,
          fontWeight: 700,
          letterSpacing: '0.08em',
          color: local.fg,
          background: local.bg,
          padding: '2px 6px',
          borderRadius: 4,
          textTransform: 'uppercase',
        }}
      >
        {(localRegime || '—').replace('_', ' ')}
      </span>
    )
  }

  return (
    <div
      title={tooltip}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        padding: '4px 10px',
        borderRadius: 999,
        background: '#161b22',
        border: '1px solid #30363d',
        fontSize: 11,
        fontFamily: 'JetBrains Mono, monospace',
      }}
    >
      <span style={{
        width: 6, height: 6, borderRadius: '50%',
        background: local.fg, boxShadow: `0 0 6px ${local.fg}`,
      }} />
      <span style={{ color: '#8b949e', fontWeight: 600 }}>REGIME</span>
      <span style={{ color: local.fg, fontWeight: 700 }}>
        {(localRegime || 'UNKNOWN').replace('_', ' ')}
      </span>
      {confPct !== null && (
        <span style={{ color: '#6e7681' }}>{confPct}%</span>
      )}
      {consensusRegime && consensusRegime !== 'UNKNOWN' && (
        <>
          <span style={{ color: '#30363d' }}>·</span>
          <span style={{ color: cons.fg, fontWeight: 700 }}>
            {consensusRegime.replace('_', ' ')}
          </span>
        </>
      )}
    </div>
  )
}
