import { useState, useCallback } from 'react'
import { Lock, Sparkles, Eye, EyeOff } from 'lucide-react'
import { login, setCsrfToken } from '../api'

interface LoginProps {
  onSuccess: () => void
}

export default function Login({ onSuccess }: LoginProps) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [showPassword, setShowPassword] = useState(false)

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
      } else {
        setError(result.error || 'Invalid password')
      }
    } catch {
      setError('Could not reach the server')
    } finally {
      setLoading(false)
    }
  }, [password, onSuccess])

  return (
    <div style={{
      height: '100vh', background: '#080c10', display: 'flex', flexDirection: 'column',
      alignItems: 'center', justifyContent: 'center',
      fontFamily: "'Inter', system-ui, sans-serif", color: '#e6edf3',
    }}>
      <div style={{ textAlign: 'center', marginBottom: 32 }}>
        <div style={{
          width: 56, height: 56, borderRadius: 14,
          background: 'linear-gradient(135deg, #22c55e, #16a34a)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          margin: '0 auto 16px',
        }}>
          <Sparkles size={28} color="#fff" />
        </div>
        <div style={{ fontSize: 26, fontWeight: 800, letterSpacing: 0.5, marginBottom: 4 }}>AUTO-TRAITOR</div>
        <div style={{ fontSize: 13, color: '#8b949e' }}>Sign in to your dashboard</div>
      </div>

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
    </div>
  )
}
