/**
 * Card — uniform section/panel surface used across pages.
 */
import type { CSSProperties, ReactNode } from 'react'

interface Props {
  title?: ReactNode
  /** Right-aligned content next to title (e.g. range selector, badges). */
  actions?: ReactNode
  /** Tone glow tint. */
  tone?: 'green' | 'red' | 'blue' | 'none'
  /** Hover lift effect. */
  hover?: boolean
  /** Custom padding (e.g. '0' to use full bleed). */
  padding?: number | string
  className?: string
  style?: CSSProperties
  children: ReactNode
}

const TONE_CLASS: Record<string, string> = {
  green: 'ot-card-glow-green',
  red:   'ot-card-glow-red',
  blue:  'ot-card-glow-blue',
  none:  '',
}

export default function Card({
  title,
  actions,
  tone = 'none',
  hover = false,
  padding,
  className = '',
  style,
  children,
}: Props) {
  const cls = [
    'ot-card',
    hover ? 'ot-card-hover' : '',
    TONE_CLASS[tone] ?? '',
    className,
  ].filter(Boolean).join(' ')
  const pad = padding != null ? { padding } : null
  return (
    <section className={cls} style={{ ...pad, ...style }}>
      {(title || actions) && (
        <header className="ot-section-header">
          {typeof title === 'string' ? <h3 className="t-h2">{title}</h3> : title}
          {actions}
        </header>
      )}
      {children}
    </section>
  )
}
