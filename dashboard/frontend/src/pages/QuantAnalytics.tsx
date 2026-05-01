/**
 * QuantAnalytics — investor-grade view of the five quant pillars.
 *
 * Tabs:
 *   Factor Loadings · HAR-RV Forecasts · Granger Causality ·
 *   Slippage Model · Correlation Regime
 *
 * Each tab opens with a one-line plain-English explanation and a
 * KPI summary; tables are mobile-card-friendly via ResponsiveTable.
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Activity, BarChart3, GitCompareArrows, Layers, Waves,
  Search, Filter, Info, AlertTriangle, CheckCircle2,
} from 'lucide-react'
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
import Card from '../components/Card'
import KpiHero from '../components/KpiHero'
import StatCard from '../components/StatCard'
import MotionFade from '../components/MotionFade'
import ResponsiveTable, { type Column } from '../components/ResponsiveTable'
import EmptyState from '../components/EmptyState'
import PageTransition from '../components/PageTransition'
import { SkeletonTable } from '../components/Skeleton'

type Tab = 'factors' | 'harrv' | 'granger' | 'slippage' | 'regime'

const TABS: { key: Tab; label: string; icon: React.ReactNode; blurb: string }[] = [
  { key: 'factors',  label: 'Factor Loadings',     icon: <Layers size={14} />,        blurb: 'How each symbol moves with the market and macro factors (β, t-stat, R², α).' },
  { key: 'harrv',    label: 'Volatility Forecast', icon: <Activity size={14} />,      blurb: 'HAR-RV one-step-ahead realised-volatility forecast (daily / weekly / monthly).' },
  { key: 'granger',  label: 'Lead / Lag',          icon: <GitCompareArrows size={14} />, blurb: 'Granger-significant lead-lag edges — which symbols predict moves in others.' },
  { key: 'slippage', label: 'Slippage Model',      icon: <BarChart3 size={14} />,     blurb: 'Regression of realised slippage on order size (% of ADV) and volatility.' },
  { key: 'regime',   label: 'Correlation Regime',  icon: <Waves size={14} />,         blurb: 'Whether the universe is in a normal, elevated, or breakdown correlation regime.' },
]

const fmt = (v: number | null | undefined, digits = 4) =>
  v == null || !Number.isFinite(v) ? '—' : v.toFixed(digits)

const fmtTs = (s: string | null | undefined) => {
  if (!s) return '—'
  try {
    const d = new Date(s)
    return d.toLocaleString(undefined, { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' })
  } catch { return s }
}

const fmtPct = (v: number | null | undefined, digits = 2) =>
  v == null || !Number.isFinite(v) ? '—' : `${(v * 100).toFixed(digits)}%`

/* ── Significance helpers ─────────────────────────────────────────────── */
function tStatTone(t: number | null | undefined): 'green' | 'red' | 'gray' {
  if (t == null || !Number.isFinite(t)) return 'gray'
  const a = Math.abs(t)
  if (a >= 2.0) return t >= 0 ? 'green' : 'red'
  return 'gray'
}
function pValueTone(p: number | null | undefined): 'green' | 'gray' {
  if (p == null || !Number.isFinite(p)) return 'gray'
  return p <= 0.05 ? 'green' : 'gray'
}

/* ── Page shell ───────────────────────────────────────────────────────── */
export default function QuantAnalytics() {
  const profile = useLiveStore((s) => s.profile)
  const [tab, setTab] = useState<Tab>('factors')
  const active = TABS.find((t) => t.key === tab)!

  return (
    <PageTransition>
      <div className="p-4 md:p-6 space-y-5">
        {/* Header */}
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-3 flex-wrap">
            <h1 className="t-h1">Quantitative Analytics</h1>
            <span className="ot-chip ot-chip-neutral text-[10px] uppercase tracking-wider">
              {profile === 'ibkr' ? 'IBKR · Equities' : 'Coinbase · Crypto'}
            </span>
          </div>
          <p className="text-sm text-gray-400 max-w-3xl leading-relaxed">
            Five live research pillars — factor exposure, vol forecasts, lead/lag,
            execution cost, and correlation regime — all scoped to the active profile.
          </p>
        </div>

        {/* Tab pills */}
        <div className="flex gap-2 overflow-x-auto -mx-4 md:mx-0 px-4 md:px-0 pb-1 snap-x">
          {TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`snap-start flex items-center gap-2 px-3 py-2 rounded-lg border text-sm font-medium whitespace-nowrap transition-colors
                ${tab === t.key
                  ? 'bg-brand-600/20 border-brand-500/50 text-brand-300'
                  : 'bg-gray-900/60 border-gray-800 text-gray-400 hover:text-gray-200 hover:border-gray-700'}`}
            >
              {t.icon}
              {t.label}
            </button>
          ))}
        </div>

        {/* Active tab blurb */}
        <div className="flex items-start gap-2 text-xs text-gray-500 bg-gray-900/40 border border-gray-800/60 rounded-lg px-3 py-2">
          <Info size={12} className="text-brand-400 mt-0.5 flex-shrink-0" />
          <span>{active.blurb}</span>
        </div>

        <MotionFade key={tab}>
          {tab === 'factors' && <FactorLoadingsTab profile={profile} />}
          {tab === 'harrv' && <HarRvTab profile={profile} />}
          {tab === 'granger' && <GrangerTab profile={profile} />}
          {tab === 'slippage' && <SlippageTab profile={profile} />}
          {tab === 'regime' && <RegimeTab profile={profile} />}
        </MotionFade>
      </div>
    </PageTransition>
  )
}

/* ─── Factor loadings ─────────────────────────────────────────────────── */
function FactorLoadingsTab({ profile }: { profile: string }) {
  const [symbol, setSymbol] = useState('')
  const [factor, setFactor] = useState('')
  const [significantOnly, setSignificantOnly] = useState(false)

  const { data, isLoading, error } = useQuery({
    queryKey: ['quant', 'factor-loadings', profile, symbol, factor, significantOnly],
    queryFn: () =>
      fetchQuantFactorLoadings({
        symbol: symbol || undefined,
        factor: factor || undefined,
        minAbsTStat: significantOnly ? 2.0 : undefined,
        limit: 500,
      }),
    enabled: !!profile,
  })

  const rows = data?.rows ?? []
  const significantCount = rows.filter((r) => Math.abs(r.t_stat ?? 0) >= 2).length
  const avgR2 = rows.length ? rows.reduce((a, r) => a + (r.r_squared ?? 0), 0) / rows.length : 0
  const uniqueSyms = new Set(rows.map((r) => r.symbol)).size
  const uniqueFactors = new Set(rows.map((r) => r.factor)).size

  const columns: Column<QuantFactorLoadingRow>[] = [
    { header: 'Symbol', className: 't-mono',
      render: (r) => <span className="font-semibold text-gray-200">{r.symbol}</span> },
    { header: 'Factor', render: (r) => <span className="text-gray-300">{r.factor}</span> },
    { header: 'β', align: 'right', className: 't-mono',
      render: (r) => <span className="text-gray-200">{fmt(r.beta, 3)}</span> },
    { header: 't-stat', align: 'right', className: 't-mono',
      render: (r) => {
        const tone = tStatTone(r.t_stat)
        const cls = tone === 'green' ? 'text-green-400' : tone === 'red' ? 'text-red-400' : 'text-gray-500'
        return <span className={cls}>{fmt(r.t_stat, 2)}</span>
      } },
    { header: 'R²', align: 'right', className: 't-mono',
      render: (r) => <R2Bar value={r.r_squared} /> },
    { header: 'α (annual)', align: 'right', className: 't-mono',
      render: (r) => <span className="text-gray-300">{fmtPct(r.alpha_annualised, 1)}</span> },
    { header: 'Idio σ', align: 'right', className: 't-mono',
      render: (r) => <span className="text-gray-400">{fmt(r.idio_vol, 4)}</span> },
    { header: 'N', align: 'right', className: 't-mono',
      render: (r) => <span className="text-gray-500">{r.sample_count}</span> },
    { header: 'Computed', align: 'right', className: 't-mono', desktopOnly: true,
      render: (r) => <span className="text-gray-500">{fmtTs(r.computed_at)}</span> },
  ]

  return (
    <div className="space-y-4">
      <KpiHero>
        <StatCard label="Symbols × Factors" value={`${uniqueSyms} × ${uniqueFactors}`} accent="blue" sub="Distinct pairs" />
        <StatCard label="Significant" value={significantCount} accent={significantCount > 0 ? 'green' : 'gray'} sub="|t| ≥ 2" />
        <StatCard label="Avg R²" value={fmt(avgR2, 3)} accent="blue" sub="Mean fit quality" />
        <StatCard label="Loadings Loaded" value={rows.length} accent="gray" sub="In current view" />
      </KpiHero>

      <Card title="Filters">
        <FiltersRow>
          <SearchInput placeholder="Symbol (e.g. BTC-EUR)" value={symbol} onChange={setSymbol} />
          <SearchInput placeholder="Factor (e.g. ^GSPC)" value={factor} onChange={setFactor} />
          <ToggleChip
            active={significantOnly}
            onClick={() => setSignificantOnly((v) => !v)}
            label="|t| ≥ 2 only"
          />
        </FiltersRow>
      </Card>

      <Card title="Per-symbol multi-factor regression loadings"
        actions={<TermLegend terms={[
          { sym: 'β', meaning: 'sensitivity to factor' },
          { sym: 't-stat', meaning: '≥ 2 statistically significant' },
          { sym: 'R²', meaning: '0–1 fit quality' },
          { sym: 'α', meaning: 'unexplained annual return' },
        ]} />}
      >
        <DataState loading={isLoading} error={error} empty={!rows.length} />
        {!isLoading && rows.length > 0 && (
          <ResponsiveTable
            columns={columns}
            rows={rows}
            getKey={(r) => `${r.symbol}-${r.factor}-${r.computed_at ?? ''}`}
          />
        )}
      </Card>
    </div>
  )
}

/* ─── HAR-RV ──────────────────────────────────────────────────────────── */
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

  const rows = data?.rows ?? []
  const avgFcst = rows.length ? rows.reduce((a, r) => a + (r.forecast_vol ?? 0), 0) / rows.length : 0
  const avgR2 = rows.length ? rows.reduce((a, r) => a + (r.model_r_squared ?? 0), 0) / rows.length : 0
  const elevated = rows.filter((r) => (r.forecast_vol ?? 0) > (r.realized_vol_monthly ?? 0)).length

  const columns: Column<QuantHarRvRow>[] = [
    { header: 'Symbol', className: 't-mono',
      render: (r) => <span className="font-semibold text-gray-200">{r.symbol}</span> },
    { header: 'Forecast σ', align: 'right', className: 't-mono',
      render: (r) => <span className="text-brand-300 font-medium">{fmt(r.forecast_vol, 4)}</span> },
    { header: 'Realised d', align: 'right', className: 't-mono', desktopOnly: true,
      render: (r) => <span className="text-gray-400">{fmt(r.realized_vol_daily, 4)}</span> },
    { header: 'Realised w', align: 'right', className: 't-mono', desktopOnly: true,
      render: (r) => <span className="text-gray-400">{fmt(r.realized_vol_weekly, 4)}</span> },
    { header: 'Realised m', align: 'right', className: 't-mono',
      render: (r) => <span className="text-gray-400">{fmt(r.realized_vol_monthly, 4)}</span> },
    { header: 'βd', align: 'right', className: 't-mono', desktopOnly: true,
      render: (r) => <span className="text-gray-300">{fmt(r.beta_daily, 2)}</span> },
    { header: 'βw', align: 'right', className: 't-mono', desktopOnly: true,
      render: (r) => <span className="text-gray-300">{fmt(r.beta_weekly, 2)}</span> },
    { header: 'βm', align: 'right', className: 't-mono', desktopOnly: true,
      render: (r) => <span className="text-gray-300">{fmt(r.beta_monthly, 2)}</span> },
    { header: 'R²', align: 'right', className: 't-mono',
      render: (r) => <R2Bar value={r.model_r_squared} /> },
    { header: 'N', align: 'right', className: 't-mono',
      render: (r) => <span className="text-gray-500">{r.sample_count}</span> },
    { header: 'Computed', align: 'right', className: 't-mono', desktopOnly: true,
      render: (r) => <span className="text-gray-500">{fmtTs(r.computed_at)}</span> },
  ]

  return (
    <div className="space-y-4">
      <KpiHero>
        <StatCard label={`Avg Forecast σ (${horizon}d)`} value={fmt(avgFcst, 4)} accent="blue" sub="Mean across symbols" />
        <StatCard label="Avg model R²" value={fmt(avgR2, 3)} accent="blue" sub="HAR-RV fit quality" />
        <StatCard label="Vol Rising" value={elevated} accent={elevated > 0 ? 'red' : 'green'} sub="Forecast > monthly realised" />
        <StatCard label="Symbols" value={rows.length} accent="gray" sub="In current view" />
      </KpiHero>

      <Card title="Filters">
        <FiltersRow>
          <SearchInput placeholder="Symbol (e.g. ETH-EUR)" value={symbol} onChange={setSymbol} />
          <div className="flex gap-1.5">
            {[1, 5, 22].map((h) => (
              <button key={h}
                onClick={() => setHorizon(h)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors
                  ${horizon === h
                    ? 'bg-brand-600/25 border-brand-500/50 text-brand-300'
                    : 'bg-gray-900/60 border-gray-800 text-gray-400 hover:text-gray-200'}`}
              >
                {h}-day
              </button>
            ))}
          </div>
        </FiltersRow>
      </Card>

      <Card title="HAR-RV one-step-ahead realised-volatility forecasts"
        actions={<TermLegend terms={[
          { sym: 'σ', meaning: 'annualised volatility' },
          { sym: 'βd/βw/βm', meaning: 'daily/weekly/monthly weight' },
        ]} />}
      >
        <DataState loading={isLoading} error={error} empty={!rows.length} />
        {!isLoading && rows.length > 0 && (
          <ResponsiveTable
            columns={columns}
            rows={rows}
            getKey={(r) => `${r.symbol}-${r.computed_at ?? ''}`}
          />
        )}
      </Card>
    </div>
  )
}

/* ─── Granger ─────────────────────────────────────────────────────────── */
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

  const rows = data?.rows ?? []
  const strongest = rows.reduce<QuantGrangerRow | null>(
    (best, r) => (best == null || (r.f_stat ?? 0) > (best.f_stat ?? 0) ? r : best),
    null,
  )
  const uniqueLeaders = new Set(rows.map((r) => r.leader)).size
  const uniqueFollowers = new Set(rows.map((r) => r.follower)).size

  const columns: Column<QuantGrangerRow>[] = [
    { header: 'Leader', className: 't-mono',
      render: (r) => <span className="font-semibold text-brand-300">{r.leader}</span> },
    { header: '', align: 'center', desktopOnly: true,
      render: () => <span className="text-gray-600">→</span> },
    { header: 'Follower', className: 't-mono',
      render: (r) => <span className="font-semibold text-gray-200">{r.follower}</span> },
    { header: 'Lag (h)', align: 'right', className: 't-mono',
      render: (r) => <span className="text-gray-300">{r.lag_hours}</span> },
    { header: 'F-stat', align: 'right', className: 't-mono',
      render: (r) => <span className="text-gray-300">{fmt(r.f_stat, 2)}</span> },
    { header: 'p-value', align: 'right', className: 't-mono',
      render: (r) => {
        const tone = pValueTone(r.p_value)
        return <span className={tone === 'green' ? 'text-green-400' : 'text-gray-500'}>{fmt(r.p_value, 4)}</span>
      } },
    { header: 'N', align: 'right', className: 't-mono',
      render: (r) => <span className="text-gray-500">{r.sample_count}</span> },
    { header: 'Computed', align: 'right', className: 't-mono', desktopOnly: true,
      render: (r) => <span className="text-gray-500">{fmtTs(r.computed_at)}</span> },
  ]

  return (
    <div className="space-y-4">
      <KpiHero>
        <StatCard label="Edges Found" value={rows.length} accent={rows.length > 0 ? 'green' : 'gray'} sub={`p ≤ ${maxP}`} />
        <StatCard label="Unique Leaders" value={uniqueLeaders} accent="blue" />
        <StatCard label="Unique Followers" value={uniqueFollowers} accent="blue" />
        <StatCard
          label="Strongest Edge"
          value={strongest ? `${strongest.leader} → ${strongest.follower}` : '—'}
          accent="green"
          sub={strongest ? `F=${fmt(strongest.f_stat, 2)} · ${strongest.lag_hours}h lag` : 'No edges'}
        />
      </KpiHero>

      <Card title="Filters">
        <FiltersRow>
          <SearchInput placeholder="Leader symbol" value={leader} onChange={setLeader} />
          <SearchInput placeholder="Follower symbol" value={follower} onChange={setFollower} />
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-gray-500 whitespace-nowrap">Max p</span>
            {[0.01, 0.05, 0.1].map((p) => (
              <button key={p}
                onClick={() => setMaxP(p)}
                className={`px-2.5 py-1.5 rounded-lg text-xs font-mono border transition-colors
                  ${Math.abs(maxP - p) < 1e-9
                    ? 'bg-brand-600/25 border-brand-500/50 text-brand-300'
                    : 'bg-gray-900/60 border-gray-800 text-gray-400 hover:text-gray-200'}`}
              >
                ≤ {p}
              </button>
            ))}
          </div>
        </FiltersRow>
      </Card>

      <Card title="Granger-significant lead-lag edges">
        <DataState loading={isLoading} error={error} empty={!rows.length} />
        {!isLoading && rows.length > 0 && (
          <ResponsiveTable
            columns={columns}
            rows={rows}
            getKey={(r) => `${r.leader}-${r.follower}-${r.lag_hours}`}
          />
        )}
      </Card>
    </div>
  )
}

/* ─── Slippage ────────────────────────────────────────────────────────── */
function SlippageTab({ profile }: { profile: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['quant', 'slippage-model', profile],
    queryFn: () => fetchQuantSlippageModel(),
    enabled: !!profile,
  })

  const m = data?.model

  // Build sample impact projections for visualization
  const projections = useMemo(() => {
    if (!m) return []
    const sizes = [0.001, 0.005, 0.01, 0.02, 0.05]  // % of ADV
    const sigma = 0.02  // assumed daily vol
    return sizes.map((s) => ({
      pctAdv: s,
      bps: (m.alpha ?? 0) + (m.beta_size ?? 0) * s + (m.beta_vol ?? 0) * sigma,
    }))
  }, [m])

  const r2Tone = (m?.r_squared ?? 0) >= 0.3 ? 'green' : (m?.r_squared ?? 0) >= 0.1 ? 'blue' : 'gray'

  return (
    <div className="space-y-4">
      <KpiHero>
        <StatCard label="α (intercept)" value={fmt(m?.alpha, 2)} accent="blue" sub="bps base cost" />
        <StatCard label="β_size" value={fmt(m?.beta_size, 2)} accent="blue" sub="per unit notional/ADV" />
        <StatCard label="β_vol" value={fmt(m?.beta_vol, 2)} accent="blue" sub="per unit daily σ" />
        <StatCard label="R²" value={fmt(m?.r_squared, 3)} accent={r2Tone} sub={`N = ${m?.sample_count ?? 0}`} />
      </KpiHero>

      <Card title="Slippage impact regression"
        actions={<span className="t-mono text-[11px] text-gray-500">slippage_bps = α + β_size·(notional/ADV) + β_vol·σ</span>}
      >
        <DataState loading={isLoading} error={error} empty={!m} />
        {m && (
          <div className="space-y-4">
            <div className="text-xs text-gray-400 leading-relaxed">
              At 2% daily volatility (σ = 0.02), expected slippage for typical order sizes:
            </div>
            <div className="overflow-x-auto">
              <table className="ot-table">
                <thead>
                  <tr>
                    <th>Order size (% ADV)</th>
                    <th className="text-right">Predicted slippage</th>
                    <th className="text-right t-mono">on €10 000 trade</th>
                  </tr>
                </thead>
                <tbody>
                  {projections.map((p) => (
                    <tr key={p.pctAdv}>
                      <td className="t-mono">{(p.pctAdv * 100).toFixed(1)}%</td>
                      <td className="t-mono text-right text-gray-200">{p.bps.toFixed(2)} bps</td>
                      <td className="t-mono text-right text-gray-400">€{(p.bps * 10000 / 10000).toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="text-[11px] text-gray-500 flex items-center gap-1.5">
              <Info size={11} className="text-brand-400" /> Updated {fmtTs(m.computed_at)}
            </div>
          </div>
        )}
      </Card>
    </div>
  )
}

/* ─── Correlation regime ─────────────────────────────────────────────── */
function RegimeTab({ profile }: { profile: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['quant', 'correlation-regime', profile],
    queryFn: () => fetchQuantCorrelationRegime(200),
    enabled: !!profile,
  })

  const events = data?.events ?? []
  const latest = data?.latest

  const regimeMeta = useMemo(() => {
    const r = latest?.regime
    if (r === 'breakdown') return { tone: 'red' as const, label: 'BREAKDOWN', icon: <AlertTriangle size={14} />, blurb: 'Correlations dispersed; diversification working but unusual.' }
    if (r === 'elevated') return { tone: 'red' as const, label: 'ELEVATED', icon: <AlertTriangle size={14} />, blurb: 'Risk-on/off pressure; assets moving together.' }
    return { tone: 'green' as const, label: 'NORMAL', icon: <CheckCircle2 size={14} />, blurb: 'Cross-asset correlations within typical range.' }
  }, [latest])

  // Z-score history sparkline
  const zHistory = events.slice(0, 60).reverse().map((e) => e.z_score ?? 0)

  const columns: Column<QuantCorrelationRegimeEvent>[] = [
    { header: 'Time', className: 't-mono',
      render: (r) => <span className="text-gray-400">{fmtTs(r.computed_at)}</span> },
    { header: 'Regime',
      render: (r) => <RegimePill regime={r.regime} /> },
    { header: 'Avg ρ', align: 'right', className: 't-mono',
      render: (r) => <span className="text-gray-300">{fmt(r.avg_corr, 3)}</span> },
    { header: 'Z-score', align: 'right', className: 't-mono',
      render: (r) => {
        const z = r.z_score ?? 0
        const cls = Math.abs(z) >= 2 ? 'text-red-400' : Math.abs(z) >= 1 ? 'text-amber-400' : 'text-gray-300'
        return <span className={cls}>{fmt(z, 2)}</span>
      } },
    { header: '# Pairs', align: 'right', className: 't-mono',
      render: (r) => <span className="text-gray-500">{r.n_pairs}</span> },
    { header: 'History N', align: 'right', className: 't-mono', desktopOnly: true,
      render: (r) => <span className="text-gray-500">{r.history_n}</span> },
  ]

  return (
    <div className="space-y-4">
      {latest && (
        <Card tone={regimeMeta.tone === 'red' ? 'red' : 'green'}>
          <div className="flex items-start justify-between gap-4 flex-wrap">
            <div>
              <div className="flex items-center gap-2 mb-1">
                <span className={regimeMeta.tone === 'red' ? 'text-red-400' : 'text-green-400'}>
                  {regimeMeta.icon}
                </span>
                <span className="t-label text-gray-500">Current Regime</span>
              </div>
              <div className={`text-2xl font-bold tracking-wide ${regimeMeta.tone === 'red' ? 'text-red-400' : 'text-green-400'}`}>
                {regimeMeta.label}
              </div>
              <div className="text-xs text-gray-400 mt-1">{regimeMeta.blurb}</div>
            </div>
            <div className="text-xs text-gray-500 t-mono whitespace-nowrap">
              Updated {fmtTs(latest.computed_at)}
            </div>
          </div>
        </Card>
      )}

      <KpiHero>
        <StatCard label="Avg pairwise ρ" value={fmt(latest?.avg_corr, 3)} accent="blue" sub="Universe mean" />
        <StatCard label="Z-score" value={fmt(latest?.z_score, 2)}
          accent={Math.abs(latest?.z_score ?? 0) >= 2 ? 'red' : 'blue'}
          sub="Std-deviations from norm"
          sparkline={zHistory.length ? zHistory : undefined}
        />
        <StatCard label="# Pairs" value={latest?.n_pairs ?? 0} accent="gray" sub="In universe" />
        <StatCard label="History Window" value={events.length} accent="gray" sub="Snapshots loaded" />
      </KpiHero>

      <Card title="Regime history">
        <DataState loading={isLoading} error={error} empty={!events.length} />
        {!isLoading && events.length > 0 && (
          <ResponsiveTable
            columns={columns}
            rows={events}
            getKey={(r) => r.computed_at ?? `${r.regime}-${r.z_score}`}
          />
        )}
      </Card>
    </div>
  )
}

/* ─── Shared sub-components ──────────────────────────────────────────── */

function FiltersRow({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <Filter size={13} className="text-gray-600" />
      {children}
    </div>
  )
}

function SearchInput({ placeholder, value, onChange }: { placeholder: string; value: string; onChange: (v: string) => void }) {
  return (
    <div className="relative">
      <Search size={12} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-600 pointer-events-none" />
      <input
        type="text"
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-gray-950 border border-gray-800 focus:border-brand-600/50 focus:outline-none rounded-lg pl-7 pr-3 py-1.5 text-sm text-gray-200 placeholder-gray-600 t-mono w-44"
      />
    </div>
  )
}

function ToggleChip({ active, onClick, label }: { active: boolean; onClick: () => void; label: string }) {
  return (
    <button onClick={onClick}
      className={`px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors
        ${active
          ? 'bg-brand-600/25 border-brand-500/50 text-brand-300'
          : 'bg-gray-900/60 border-gray-800 text-gray-400 hover:text-gray-200'}`}
    >
      {label}
    </button>
  )
}

function R2Bar({ value }: { value: number | null | undefined }) {
  if (value == null || !Number.isFinite(value)) return <span className="text-gray-500">—</span>
  const pct = Math.max(0, Math.min(1, value))
  const color = pct >= 0.5 ? 'bg-green-500' : pct >= 0.2 ? 'bg-brand-500' : 'bg-gray-600'
  return (
    <div className="flex items-center gap-2 justify-end">
      <span className="t-mono text-xs text-gray-400 w-10 text-right">{fmt(value, 2)}</span>
      <div className="w-16 h-1.5 bg-gray-800 rounded-full overflow-hidden hidden md:block">
        <div className={`h-full ${color}`} style={{ width: `${pct * 100}%` }} />
      </div>
    </div>
  )
}

function RegimePill({ regime }: { regime: string }) {
  const cls =
    regime === 'breakdown' ? 'ot-chip ot-chip-negative' :
    regime === 'elevated'  ? 'ot-chip ot-chip-negative' :
    'ot-chip ot-chip-positive'
  return <span className={`${cls} text-[10px] uppercase tracking-wider`}>{regime}</span>
}

function TermLegend({ terms }: { terms: { sym: string; meaning: string }[] }) {
  return (
    <div className="hidden md:flex items-center gap-3 text-[11px] text-gray-500">
      {terms.map((t) => (
        <span key={t.sym} className="flex items-center gap-1">
          <span className="t-mono text-gray-400">{t.sym}</span>
          <span>= {t.meaning}</span>
        </span>
      ))}
    </div>
  )
}

function DataState({ loading, error, empty }: { loading: boolean; error: unknown; empty: boolean }) {
  if (loading) return <SkeletonTable rows={6} cols={5} />
  if (error) {
    return (
      <div className="flex items-start gap-2 px-3 py-2 bg-red-900/20 border border-red-800/40 rounded-lg text-sm text-red-300">
        <AlertTriangle size={14} className="mt-0.5 flex-shrink-0" />
        <span>{String((error as Error).message ?? error)}</span>
      </div>
    )
  }
  if (empty) {
    return (
      <EmptyState
        icon="search"
        title="No data yet"
        description="Quant pillars compute on a schedule. Try widening filters or wait for the next run."
      />
    )
  }
  return null
}
