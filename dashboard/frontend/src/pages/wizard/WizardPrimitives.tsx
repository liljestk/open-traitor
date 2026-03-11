/**
 * WizardPrimitives.tsx — Shared UI primitives for the Setup Wizard.
 * Consumed by WizardSteps.tsx and SetupWizard.tsx.
 */
import { useState, useEffect, type ReactNode } from 'react'
import {
  ArrowRight, Check, AlertTriangle, Eye, EyeOff,
  Lock, Info, Copy, ExternalLink,
  ChevronDown, ChevronRight, CircleAlert, CheckCircle2,
} from 'lucide-react'
import { WIZARD_CSS, card, inputBase, mono } from './wizardData'

/* ═══════════════════════════════════════════════════════════════════════════
   CSS Injection Hook
   ═══════════════════════════════════════════════════════════════════════════ */

export function useInjectCSS() {
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
   Shared UI Primitives
   ═══════════════════════════════════════════════════════════════════════════ */

export function Tip({ children }: { children: ReactNode }) {
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

export function Warning({ children }: { children: ReactNode }) {
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

export function SecurityBox({ children }: { children: ReactNode }) {
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

export function HowTo({ title, steps, link }: { title: string; steps: string[]; link?: { url: string; label: string } }) {
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

export function PasswordInput({ value, onChange, placeholder, useMono, className }: {
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

export function ToggleChip({ selected, onClick, children, color = '#22c55e' }: {
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

export function SectionHeader({ icon, title, subtitle }: { icon: ReactNode; title: string; subtitle: string }) {
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

export function FormField({ label, help, required, children }: {
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

export function ValidationBadge({ valid, label }: { valid: boolean; label: string }) {
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

export function CopyButton({ text }: { text: string }) {
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

export function SkipLink({ onClick, label = 'Skip this step' }: { onClick: () => void; label?: string }) {
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
