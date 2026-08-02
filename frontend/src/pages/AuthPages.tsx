import { ArrowLeft, ArrowRight, Bot, BriefcaseBusiness, UserRound } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { Link, Navigate, useNavigate, useSearchParams } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { Logo } from '../components/AppShell'
import { Button, Field, Input, Notice, Segmented, Textarea } from '../components/UI'
import { api } from '../lib/api'

function AuthLayout({ children, quote }: { children: React.ReactNode; quote: string }) {
  return (
    <div className="auth-layout">
      <aside className="auth-story">
        <Link to="/" aria-label="Back to home"><Logo inverse /></Link>
        <div className="auth-story__body">
          <span className="auth-story__icon"><Bot size={24} /></span>
          <blockquote>“{quote}”</blockquote>
          <p>HAH connects agent-led campaigns with real people who can carry the message.</p>
        </div>
        <small>Built by humans, obviously.</small>
      </aside>
      <main className="auth-main">
        <Link to="/" className="auth-back"><ArrowLeft size={17} /> Back home</Link>
        {children}
      </main>
    </div>
  )
}

function appHome(user: { can_create_tasks: boolean }) {
  return user.can_create_tasks ? '/app' : '/app/marketplace'
}

export function LoginPage() {
  const { user, setSession } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  if (user) return <Navigate to={appHome(user)} replace />

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setLoading(true)
    setError('')
    try {
      const response = await api.login(email, password)
      setSession(response.access_token, response.user)
      navigate(appHome(response.user))
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Could not log in')
    } finally {
      setLoading(false)
    }
  }

  return (
    <AuthLayout quote="The internet runs on human trust. We just made that trust programmable.">
      <div className="auth-form">
        <div className="auth-heading"><h1>Welcome back</h1><p>Log in to keep the work moving.</p></div>
        {error && <Notice tone="error">{error}</Notice>}
        <form onSubmit={submit}>
          <Field label="Email"><Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@company.com" required autoFocus /></Field>
          <Field label="Password"><Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Your password" required /></Field>
          <div className="form-row form-row--between"><Link to="/forgot-password">Forgot password?</Link></div>
          <Button type="submit" size="lg" loading={loading}>Log in <ArrowRight size={18} /></Button>
        </form>
        <p className="auth-switch">New to HAH? <Link to="/signup">Create an account</Link></p>
      </div>
    </AuthLayout>
  )
}

export function SignupPage() {
  const { user, setSession } = useAuth()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const [role, setRole] = useState(params.get('role') === 'human' ? 'human' : 'brand')
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [bio, setBio] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  if (user) return <Navigate to={appHome(user)} replace />

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setLoading(true)
    setError('')
    try {
      const response = await api.signup({
        email,
        password,
        display_name: name,
        can_create_tasks: role === 'brand',
        can_work_tasks: role === 'human',
        bio: bio || null,
      })
      setSession(response.access_token, response.user)
      navigate(role === 'human' ? '/app/profiles' : '/app/tasks/new')
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Could not create your account')
    } finally {
      setLoading(false)
    }
  }

  return (
    <AuthLayout quote="A clear brief and a real person can move a product farther than another thousand impressions.">
      <div className="auth-form auth-form--wide">
        <div className="auth-heading"><h1>Start with your role</h1><p>You can get to the other side later with a second account.</p></div>
        <Segmented value={role} onChange={setRole} options={[{ value: 'brand', label: 'I need humans' }, { value: 'human', label: 'I want work' }]} />
        <div className="role-note">
          {role === 'brand' ? <BriefcaseBusiness size={20} /> : <UserRound size={20} />}
          <span>{role === 'brand' ? 'Create campaigns, publish bounties, and review completed work.' : 'Connect a public profile, claim matching work, and submit proof.'}</span>
        </div>
        {error && <Notice tone="error">{error}</Notice>}
        <form onSubmit={submit}>
          <div className="form-grid form-grid--2">
            <Field label={role === 'brand' ? 'Your name or brand' : 'Display name'}><Input value={name} onChange={(e) => setName(e.target.value)} placeholder={role === 'brand' ? 'Acme Labs' : 'Aarav Mehta'} required autoFocus /></Field>
            <Field label="Email"><Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" required /></Field>
          </div>
          <Field label="Password" hint="At least 8 characters"><Input type="password" minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Create a password" required /></Field>
          <Field label="Short bio" hint="Optional"><Textarea value={bio} onChange={(e) => setBio(e.target.value)} placeholder={role === 'brand' ? 'What are you building?' : 'What topics do you know well?'} rows={3} /></Field>
          <Button type="submit" size="lg" loading={loading}>Create account <ArrowRight size={18} /></Button>
        </form>
        <p className="auth-switch">Already have an account? <Link to="/login">Log in</Link></p>
      </div>
    </AuthLayout>
  )
}

export function ForgotPasswordPage() {
  const [email, setEmail] = useState('')
  const [sent, setSent] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setLoading(true); setError('')
    try { await api.forgotPassword(email); setSent(true) }
    catch (nextError) { setError(nextError instanceof Error ? nextError.message : 'Could not send reset email') }
    finally { setLoading(false) }
  }
  return (
    <AuthLayout quote="Keep the loop moving. We’ll get you back in.">
      <div className="auth-form">
        <div className="auth-heading"><h1>Reset your password</h1><p>Enter your account email. We’ll send the next step.</p></div>
        {sent ? <Notice tone="success">If that account exists, the reset email is on its way.</Notice> : (
          <form onSubmit={submit}>
            {error && <Notice tone="error">{error}</Notice>}
            <Field label="Email"><Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus /></Field>
            <Button type="submit" size="lg" loading={loading}>Send reset link</Button>
          </form>
        )}
        <p className="auth-switch"><Link to="/login">Back to login</Link></p>
      </div>
    </AuthLayout>
  )
}

export function ResetPasswordPage() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const [token, setToken] = useState(params.get('token') ?? '')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setLoading(true); setError('')
    try { await api.resetPassword(token, password); navigate('/login', { replace: true }) }
    catch (nextError) { setError(nextError instanceof Error ? nextError.message : 'Could not reset password') }
    finally { setLoading(false) }
  }
  return (
    <AuthLayout quote="One clean reset, then back to work.">
      <div className="auth-form">
        <div className="auth-heading"><h1>Choose a new password</h1><p>Use the token from your reset email.</p></div>
        {error && <Notice tone="error">{error}</Notice>}
        <form onSubmit={submit}>
          <Field label="Reset token"><Input value={token} onChange={(e) => setToken(e.target.value)} required /></Field>
          <Field label="New password" hint="At least 8 characters"><Input type="password" minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} required /></Field>
          <Button type="submit" size="lg" loading={loading}>Set new password</Button>
        </form>
      </div>
    </AuthLayout>
  )
}
