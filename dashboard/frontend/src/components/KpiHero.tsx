/**
 * KpiHero — horizontal-snap grid of StatCards on mobile,
 * auto-fit grid on desktop. Uses .ot-kpi-grid utility.
 */
import type { ReactNode } from 'react'

interface Props {
  /** Optional eyebrow label above the grid. */
  label?: ReactNode
  children: ReactNode
}

export default function KpiHero({ label, children }: Props) {
  return (
    <div>
      {label && <p className="t-label" style={{ marginBottom: 8 }}>{label}</p>}
      <div className="ot-kpi-grid">{children}</div>
    </div>
  )
}
