import { FormEvent, useState } from 'react'
import { api, ApiError } from '../api'
import { Logo } from '../components/Logo'

export function AuthGate({
  initialized,
  onSignedIn,
}: {
  initialized: boolean
  onSignedIn: (username: string) => void
}) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const creating = !initialized

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    if (creating && password !== confirm) {
      setError('Passwords do not match.')
      return
    }
    if (creating && password.length < 8) {
      setError('Password must be at least 8 characters.')
      return
    }
    setBusy(true)
    try {
      const path = creating ? '/api/auth/setup' : '/api/auth/login'
      const res = await api<{ username: string; signedIn: boolean }>('POST', path, {
        username,
        password,
      })
      onSignedIn(res.username)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="center-stage">
      <Logo size={72} />
      <div className="dim">
        {creating ? 'Create your profile — this password encrypts your broker keys.' : 'Welcome back.'}
      </div>
      <form className="center-stage" style={{ height: 'auto', gap: 12 }} onSubmit={submit}>
        <input
          className="field"
          placeholder="Username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoFocus
          spellCheck={false}
        />
        <input
          className="field"
          placeholder="Password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {creating ? (
          <input
            className="field"
            placeholder="Confirm password"
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        ) : null}
        {error ? <div className="error-text">{error}</div> : null}
        <button className="btn primary" disabled={busy || !username || !password} type="submit">
          {busy ? 'Working…' : creating ? 'Create profile' : 'Unlock'}
        </button>
        {creating ? (
          <div className="subtle" style={{ maxWidth: 340, textAlign: 'center' }}>
            There is no password recovery by design — a reset deletes stored broker keys and you
            re-enter them.
          </div>
        ) : null}
      </form>
    </div>
  )
}
