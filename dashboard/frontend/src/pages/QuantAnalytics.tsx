import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  fetchQuantFactorLoadings,
  fetchQuantHarRv,
  fetchQuantGranger,
  fetchQuantSlippageModel,
  fetchQuantCorrelationRegime,
  type QuantFactorLoadingRow,
  type QuantHarRvRow,
  type QuantGrangerRow,
  type QuantCorrelationRegimeEvent,
} from '../api'
import { useLiveStore } from '../store'

type Tab = 'factors' | 'harrv' | 'granger' | 'slippage' | 'regime'

const TABS: { key: Tab; label: string }[] = [
  { key: 'factors', label: 'Factor Loadings' },
  { key: 'harrv', label: 'HAR-RV Forecasts' },
  { key: 'granger', label: 'Granger Causality' },
  { key: 'slippage', label: 'Slippage Model' },
  { key: 'regime', label: 'Correlation Regime' },
]

const fmt = (v: number | null | undefined, digits = 4) =>
  v == null || !Number.isFinite(v) ? '—' : v.toFixed(digits)

const fmtTs = (s: string | null | undefined) => (s ? new Date(s).toLocaleString() : '—')

export default function QuantAnalytics() {
  const profile = useLiveStore((s) => s.profile)
  const [tab, setTab] = useState<Tab>('factors')

  return (
    <div style={{ padding: 16 }}>
      <h1 style={{ marginTop: 0 }}>Quantitative Analytics</h1>
      <p style={{ color: 'var(--muted, #888)', marginTop: 4 }}>
        Multi-factor regressions, HAR-RV vol forecasts, Granger lead-lag,
        slippage impact model, and universe correlation regime — all
        scoped to the active profile.
      </p>

      <div style={{ display: 'flex', gap: 8, margin: '16px 0', flexWrap: 'wrap' }}>
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            style={{
              padding: '6px 12px',
              borderRadius: 6,
              border: '1px solid var(--border, #444)',
              background: tab === t.key ? 'var(--accent, #2962ff)' : 'transparent',
              color: tab === t.key ? '#fff' : 'inherit',
              cursor: 'pointer',
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'factors' && <FactorLoadingsTab profile={profile} />}
      {tab === 'harrv' && <HarRvTab profile={profile} />}
      {tab === 'granger' && <GrangerTab profile={profile} />}
      {tab === 'slippage' && <SlippageTab profile={profile} />}
      {tab === 'regime' && <RegimeTab profile={profile} />}
    </div>
  )
}

// ─── Factor loadings ────────────────────────────────────────────────────

function FactorLoadingsTab({ profile }: { profile: string }) {
  const [symbol, setSymbol] = useState('')
  const [factor, setFactor] = useState('')
  const [minAbsT, setMinAbsT] = useState(0)

  const { data, isLoading, error } = useQuery({
    queryKey: ['quant', 'factor-loadings', profile, symbol, factor, minAbsT],
    queryFn: () =>
      fetchQuantFactorLoadings({
        symbol: symbol || undefined,
        factor: factor || undefined,
        minAbsTStat: minAbsT || undefined,
        limit: 500,
      }),
    enabled: !!profile,
  })

  return (
    <Section title="Per-symbol multi-factor regression loadings">
      <Filters>
        <input placeholder="symbol" value={symbol} onChange={(e) => setSymbol(e.target.value)} />
        <input placeholder="factor (e.g. ^GSPC)" value={factor} onChange={(e) => setFactor(e.target.value)} />
        <input
          type="number"
          step="0.1"
          min={0}
          placeholder="min |t-stat|"
          value={minAbsT}
          onChange={(e) => setMinAbsT(Number(e.target.value) || 0)}
          style={{ width: 100 }}
        />
      </Filters>
      <StateBanner loading={isLoading} error={error} empty={!data?.rows?.length} />
      {data?.rows?.length ? (
        <Table
          columns={['Symbol', 'Factor', 'β', 't-stat', 'R²', 'α (annual)', 'Idio σ', 'N', 'Computed']}
          rows={data.rows.map((r: QuantFactorLoadingRow) => [
            r.symbol,
            r.factor,
            fmt(r.beta, 4),
            fmt(r.t_stat, 2),
            fmt(r.r_squared, 3),
            fmt(r.alpha_annualised, 4),
            fmt(r.idio_vol, 4),
            String(r.sample_count),
            fmtTs(r.computed_at),
          ])}
        />
      ) : null}
    </Section>
  )
}

// ─── HAR-RV ─────────────────────────────────────────────────────────────

function HarRvTab({ profile }: { profile: string }) {
  const [symbol, setSymbol] = useState('')
  const [horizon, setHorizon] = useState(1)

  const { data, isLoading, error } = useQuery({
    queryKey: ['quant', 'har-rv', profile, symbol, horizon],
    queryFn: () =>
      fetchQuantHarRv({
        symbol: symbol || undefined,
        horizonDays: horizon,
        limit: 500,
      }),
    enabled: !!profile,
  })

  return (
    <Section title="HAR-RV one-step-ahead realised-volatility forecasts">
      <Filters>
        <input placeholder="symbol" value={symbol} onChange={(e) => setSymbol(e.target.value)} />
        <select value={horizon} onChange={(e) => setHorizon(Number(e.target.value))}>
          {[1, 5, 22].map((h) => (
            <option key={h} value={h}>{h}d horizon</option>
          ))}
        </select>
      </Filters>
      <StateBanner loading={isLoading} error={error} empty={!data?.rows?.length} />
      {data?.rows?.length ? (
        <Table
          columns={['Symbol', 'Forecast σ', 'RV daily', 'RV weekly', 'RV monthly', 'βd', 'βw', 'βm', 'R²', 'N', 'Computed']}
          rows={data.rows.map((r: QuantHarRvRow) => [
            r.symbol,
            fmt(r.forecast_vol, 5),
            fmt(r.realized_vol_daily, 5),
            fmt(r.realized_vol_weekly, 5),
            fmt(r.realized_vol_monthly, 5),
            fmt(r.beta_daily, 3),
            fmt(r.beta_weekly, 3),
            fmt(r.beta_monthly, 3),
            fmt(r.model_r_squared, 3),
            String(r.sample_count),
            fmtTs(r.computed_at),
          ])}
        />
      ) : null}
    </Section>
  )
}

// ─── Granger ────────────────────────────────────────────────────────────

function GrangerTab({ profile }: { profile: string }) {
  const [leader, setLeader] = useState('')
  const [follower, setFollower] = useState('')
  const [maxP, setMaxP] = useState(0.05)

  const { data, isLoading, error } = useQuery({
    queryKey: ['quant', 'granger', profile, leader, follower, maxP],
    queryFn: () =>
      fetchQuantGranger({
        leader: leader || undefined,
        follower: follower || undefined,
        maxPValue: maxP,
        limit: 500,
      }),
    enabled: !!profile,
  })

  return (
    <Section title="Granger-significant lead-lag edges">
      <Filters>
        <input placeholder="leader symbol" value={leader} onChange={(e) => setLeader(e.target.value)} />
        <input placeholder="follower symbol" value={follower} onChange={(e) => setFollower(e.target.value)} />
        <input
          type="number"
          step="0.01"
          min={0}
          max={1}
          value={maxP}
          onChange={(e) => setMaxP(Number(e.target.value) || 0.05)}
          style={{ width: 80 }}
        />
        <span style={{ color: 'var(--muted, #888)', fontSize: 12 }}>max p-value</span>
      </Filters>
      <StateBanner loading={isLoading} error={error} empty={!data?.rows?.length} />
      {data?.rows?.length ? (
        <Table
          columns={['Leader', 'Follower', 'Lag (h)', 'F-stat', 'p-value', 'N', 'Computed']}
          rows={data.rows.map((r: QuantGrangerRow) => [
            r.leader,
            r.follower,
            String(r.lag_hours),
            fmt(r.f_stat, 3),
            fmt(r.p_value, 4),
            String(r.sample_count),
            fmtTs(r.computed_at),
          ])}
        />
      ) : null}
    </Section>
  )
}

// ─── Slippage ───────────────────────────────────────────────────────────

function SlippageTab({ profile }: { profile: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['quant', 'slippage-model', profile],
    queryFn: () => fetchQuantSlippageModel(),
    enabled: !!profile,
  })

  const m = data?.model
  return (
    <Section title="Slippage impact regression — slippage_bps = α + β_size·(notional/ADV) + β_vol·σ">
      <StateBanner loading={isLoading} error={error} empty={!m} />
      {m ? (
        <div style={{ display: 'grid', gap: 8, gridTemplateColumns: 'repeat(2, minmax(160px, 1fr))', maxWidth: 600 }}>
          <Stat label="α (intercept)" value={fmt(m.alpha, 3)} />
          <Stat label="β_size" value={fmt(m.beta_size, 3)} />
          <Stat label="β_vol" value={fmt(m.beta_vol, 3)} />
          <Stat label="R²" value={fmt(m.r_squared, 3)} />
          <Stat label="Sample N" value={String(m.sample_count)} />
          <Stat label="Computed" value={fmtTs(m.computed_at)} />
        </div>
      ) : null}
    </Section>
  )
}

// ─── Correlation regime ─────────────────────────────────────────────────

function RegimeTab({ profile }: { profile: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['quant', 'correlation-regime', profile],
    queryFn: () => fetchQuantCorrelationRegime(200),
    enabled: !!profile,
  })

  const latest = data?.latest
  const regimeColor = useMemo(() => {
    if (!latest) return 'inherit'
    if (latest.regime === 'breakdown') return '#e53935'
    if (latest.regime === 'elevated') return '#fb8c00'
    return '#43a047'
  }, [latest])

  return (
    <Section title="Universe-wide correlation regime">
      <StateBanner loading={isLoading} error={error} empty={!data?.events?.length} />
      {latest ? (
        <div
          style={{
            display: 'grid',
            gap: 8,
            gridTemplateColumns: 'repeat(4, minmax(140px, 1fr))',
            maxWidth: 760,
            marginBottom: 16,
          }}
        >
          <Stat label="Regime" value={latest.regime} valueColor={regimeColor} />
          <Stat label="Avg ρ" value={fmt(latest.avg_corr, 3)} />
          <Stat label="Z-score" value={fmt(latest.z_score, 2)} />
          <Stat label="# pairs" value={String(latest.n_pairs)} />
        </div>
      ) : null}
      {data?.events?.length ? (
        <Table
          columns={['Time', 'Regime', 'Avg ρ', 'Z', '# pairs', 'History N']}
          rows={data.events.map((e: QuantCorrelationRegimeEvent) => [
            fmtTs(e.computed_at),
            e.regime,
            fmt(e.avg_corr, 3),
            fmt(e.z_score, 2),
            String(e.n_pairs),
            String(e.history_n),
          ])}
        />
      ) : null}
    </Section>
  )
}

// ─── Tiny presentational helpers ────────────────────────────────────────

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginTop: 16 }}>
      <h2 style={{ fontSize: 16, marginBottom: 8 }}>{title}</h2>
      {children}
    </div>
  )
}

function Filters({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8, flexWrap: 'wrap' }}>
      {children}
    </div>
  )
}

function StateBanner({
  loading,
  error,
  empty,
}: {
  loading: boolean
  error: unknown
  empty: boolean
}) {
  if (loading) return <div style={{ color: 'var(--muted, #888)' }}>Loading…</div>
  if (error) return <div style={{ color: '#e53935' }}>Error: {String((error as Error).message ?? error)}</div>
  if (empty) return <div style={{ color: 'var(--muted, #888)' }}>No data yet.</div>
  return null
}

function Stat({ label, value, valueColor }: { label: string; value: string; valueColor?: string }) {
  return (
    <div
      style={{
        padding: 10,
        borderRadius: 6,
        border: '1px solid var(--border, #333)',
        background: 'var(--card, #1a1a1a)',
      }}
    >
      <div style={{ fontSize: 11, color: 'var(--muted, #888)', textTransform: 'uppercase' }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 600, color: valueColor }}>{value}</div>
    </div>
  )
}

function Table({ columns, rows }: { columns: string[]; rows: (string | number)[][] }) {
  return (
    <div style={{ overflowX: 'auto', border: '1px solid var(--border, #333)', borderRadius: 6 }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <thead>
          <tr style={{ background: 'var(--card, #1a1a1a)' }}>
            {columns.map((c) => (
              <th
                key={c}
                style={{ textAlign: 'left', padding: '6px 10px', borderBottom: '1px solid var(--border, #333)' }}
              >
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} style={{ borderBottom: '1px solid var(--border, #222)' }}>
              {row.map((cell, j) => (
                <td key={j} style={{ padding: '6px 10px', fontFamily: j > 0 ? 'monospace' : 'inherit' }}>
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
