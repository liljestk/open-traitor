import { useState, useCallback, useRef, useEffect } from 'react'
import { Lock, Eye, EyeOff, ShieldCheck, ArrowLeft } from 'lucide-react'
import { login, verify2FA, setCsrfToken } from '../api'

interface LoginProps {
  onSuccess: () => void
}

export default function Login({ onSuccess }: LoginProps) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [showPassword, setShowPassword] = useState(false)

  // 2FA state
  const [pendingToken, setPendingToken] = useState<string | null>(null)
  const [totpCode, setTotpCode] = useState('')
  const [useBackup, setUseBackup] = useState(false)
  const totpRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (pendingToken && totpRef.current) totpRef.current.focus()
  }, [pendingToken])

  const handleSubmit = useCallback(async (e?: React.FormEvent) => {
    e?.preventDefault()
    if (!password.trim()) return
    setLoading(true)
    setError('')
    try {
      const result = await login(password)
      if (result.status === 'ok') {
        if (result.csrf_token) setCsrfToken(result.csrf_token)
        onSuccess()
      } else if (result.status === 'requires_2fa' && result.pending_token) {
        setPendingToken(result.pending_token)
      } else {
        setError(result.error || 'Invalid password')
      }
    } catch {
      setError('Could not reach the server')
    } finally {
      setLoading(false)
    }
  }, [password, onSuccess])

  const handle2FASubmit = useCallback(async (e?: React.FormEvent) => {
    e?.preventDefault()
    if (!totpCode.trim() || !pendingToken) return
    setLoading(true)
    setError('')
    try {
      const result = await verify2FA(totpCode.trim(), pendingToken, useBackup)
      if (result.status === 'ok') {
        onSuccess()
      } else {
        setError(result.error || 'Invalid code')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Verification failed')
    } finally {
      setLoading(false)
    }
  }, [totpCode, pendingToken, useBackup, onSuccess])

  const resetTo2FA = () => {
    setPendingToken(null)
    setTotpCode('')
    setUseBackup(false)
    setError('')
  }

  return (
    <div style={{
      height: '100vh', background: '#080c10', display: 'flex', flexDirection: 'column',
      alignItems: 'center', justifyContent: 'center',
      fontFamily: "'Inter', system-ui, sans-serif", color: '#e6edf3',
    }}>
      <div style={{ textAlign: 'center', marginBottom: 32 }}>
        <img src="/logo.png" alt="OpenTraitor" style={{
          width: 64, height: 64, borderRadius: 14,
          margin: '0 auto 16px', display: 'block',
        }} />
        <div style={{ fontSize: 26, fontWeight: 800, letterSpacing: 0.5, marginBottom: 4 }}>OPENTRAITOR</div>
        <div style={{ fontSize: 13, color: '#8b949e' }}>
          {pendingToken ? 'Two-factor authentication' : 'Sign in to your dashboard'}
        </div>
      </div>

      {/* ── Password form ── */}
      {!pendingToken && (
        <form onSubmit={handleSubmit} style={{
          width: '100%', maxWidth: 400, padding: '28px 32px',
          background: '#0d1117', border: '1px solid #21262d', borderRadius: 14,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 20 }}>
            <Lock size={16} style={{ color: '#22c55e' }} />
            <span style={{ fontSize: 15, fontWeight: 700, color: '#e6edf3' }}>Dashboard Login</span>
          </div>

          <label htmlFor="login-password" style={{ fontSize: 12, color: '#8b949e', display: 'block', marginBottom: 6 }}>
            Password
          </label>
          <div style={{ position: 'relative', marginBottom: 16 }}>
            <input
              id="login-password"
              type={showPassword ? 'text' : 'password'}
              value={password}
              onChange={e => setPassword(e.target.value)}
              autoFocus
              style={{
                width: '100%', padding: '10px 40px 10px 12px', fontSize: 14,
                background: '#161b22', border: '1px solid #30363d', borderRadius: 8,
                color: '#e6edf3', outline: 'none', boxSizing: 'border-box',
              }}
            />
            <button
              type="button"
              onClick={() => setShowPassword(v => !v)}
              style={{
                position: 'absolute', right: 8, top: '50%', transform: 'translateY(-50%)',
                background: 'none', border: 'none', cursor: 'pointer', color: '#8b949e', padding: 4,
              }}
            >
              {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </div>

          {error && (
            <div style={{
              padding: '8px 12px', background: 'rgba(248,81,73,0.1)', border: '1px solid rgba(248,81,73,0.3)',
              borderRadius: 8, fontSize: 12, color: '#f85149', marginBottom: 16,
            }}>
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading || !password.trim()}
            style={{
              width: '100%', padding: '10px 16px', fontSize: 14, fontWeight: 600,
              background: loading || !password.trim() ? '#21262d' : '#22c55e',
              color: loading || !password.trim() ? '#484f58' : '#fff',
              border: 'none', borderRadius: 8, cursor: loading ? 'wait' : 'pointer',
            }}
          >
            {loading ? 'Signing in…' : 'Sign In'}
          </button>
        </form>
      )}

      {/* ── 2FA verification form ── */}
      {pendingToken && (
        <form onSubmit={handle2FASubmit} style={{
          width: '100%', maxWidth: 400, padding: '28px 32px',
          background: '#0d1117', border: '1px solid #21262d', borderRadius: 14,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 20 }}>
            <ShieldCheck size={16} style={{ color: '#22c55e' }} />
            <span style={{ fontSize: 15, fontWeight: 700, color: '#e6edf3' }}>
              {useBackup ? 'Backup Code' : 'Authenticator Code'}
            </span>
          </div>

          <label htmlFor="totp-code" style={{ fontSize: 12, color: '#8b949e', display: 'block', marginBottom: 6 }}>
            {useBackup ? 'Enter a backup code' : 'Enter the 6-digit code from your authenticator app'}
          </label>
          <input
            ref={totpRef}
            id="totp-code"
            type="text"
            inputMode={useBackup ? 'text' : 'numeric'}
            autoComplete="one-time-code"
            maxLength={useBackup ? 20 : 6}
            value={totpCode}
            onChange={e => setTotpCode(useBackup ? e.target.value : e.target.value.replace(/\D/g, ''))}
            placeholder={useBackup ? 'xxxxxxxx' : '000000'}
            style={{
              width: '100%', padding: '10px 12px', fontSize: useBackup ? 14 : 24,
              fontFamily: 'monospace', letterSpacing: useBackup ? 1 : 8, textAlign: 'center',
              background: '#161b22', border: '1px solid #30363d', borderRadius: 8,
              color: '#e6edf3', outline: 'none', boxSizing: 'border-box', marginBottom: 16,
            }}
          />

          {error && (
            <div style={{
              padding: '8px 12px', background: 'rgba(248,81,73,0.1)', border: '1px solid rgba(248,81,73,0.3)',
              borderRadius: 8, fontSize: 12, color: '#f85149', marginBottom: 16,
            }}>
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading || !totpCode.trim()}
            style={{
              width: '100%', padding: '10px 16px', fontSize: 14, fontWeight: 600,
              background: loading || !totpCode.trim() ? '#21262d' : '#22c55e',
              color: loading || !totpCode.trim() ? '#484f58' : '#fff',
              border: 'none', borderRadius: 8, cursor: loading ? 'wait' : 'pointer',
              marginBottom: 12,
            }}
          >
            {loading ? 'Verifying…' : 'Verify'}
          </button>

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <button type="button" onClick={resetTo2FA} style={{
              background: 'none', border: 'none', cursor: 'pointer',
              color: '#8b949e', fontSize: 12, display: 'flex', alignItems: 'center', gap: 4,
              padding: 0,
            }}>
              <ArrowLeft size={12} /> Back to login
            </button>
            <button type="button" onClick={() => { setUseBackup(v => !v); setTotpCode(''); setError('') }} style={{
              background: 'none', border: 'none', cursor: 'pointer',
              color: '#58a6ff', fontSize: 12, padding: 0,
            }}>
              {useBackup ? 'Use authenticator app' : 'Use a backup code'}
            </button>
          </div>
        </form>
      )}
    </div>
  )
}
