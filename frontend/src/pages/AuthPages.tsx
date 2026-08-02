import { Button, Field, Input, InputArea, Link, Surface, Tabs, Text } from '@cloudflare/kumo'
import { ArrowLeft, ArrowRight, Briefcase, Robot, User } from '@phosphor-icons/react'
import { useState, type FormEvent } from 'react'
import { Navigate, useNavigate, useSearchParams } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { Logo } from '../components/AppShell'
import { Notice } from '../components/UI'
import { api } from '../lib/api'

function AuthLayout({ children, quote }: { children: React.ReactNode; quote: string }) {
  return (
    <div className="auth-layout">
      <Surface as="aside" className="auth-story">
        <Link href="/" variant="plain" aria-label="Back to home"><Logo /></Link>
        <div className="auth-story__body">
          <Robot size={32} />
          <Text variant="heading2" as="p">“{quote}”</Text>
          <Text variant="secondary">HAH connects agent-led campaigns with real people who can carry the message.</Text>
        </div>
        <Text variant="secondary" size="xs">Built by humans, obviously.</Text>
      </Surface>
      <main className="auth-main">
        <Link href="/" variant="plain"><ArrowLeft /> Back home</Link>
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
      <Surface className="auth-form rounded-lg border border-kumo-hairline p-8">
        <div className="auth-heading"><Text variant="heading1" as="h1">Welcome back</Text><Text variant="secondary">Log in to keep the work moving.</Text></div>
        {error && <Notice tone="error">{error}</Notice>}
        <form onSubmit={submit}>
          <Field label="Email"><Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@company.com" required autoFocus /></Field>
          <Field label="Password"><Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Your password" required /></Field>
          <div className="form-row form-row--between"><Link href="/forgot-password">Forgot password?</Link></div>
          <Button type="submit" variant="primary" size="lg" loading={loading} icon={<ArrowRight />}>Log in</Button>
        </form>
        <div className="auth-switch"><Text>New to HAH? <Link href="/signup">Create an account</Link></Text></div>
      </Surface>
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
      <Surface className="auth-form auth-form--wide rounded-lg border border-kumo-hairline p-8">
        <div className="auth-heading"><Text variant="heading1" as="h1">Start with your role</Text><Text variant="secondary">You can get to the other side later with a second account.</Text></div>
        <Tabs value={role} onValueChange={(value) => setRole(value as 'brand' | 'human')} tabs={[{ value: 'brand', label: 'I need humans' }, { value: 'human', label: 'I want work' }]} />
        <Surface className="role-note rounded-lg border border-kumo-hairline p-4">
          {role === 'brand' ? <Briefcase size={20} /> : <User size={20} />}
          <Text size="sm">{role === 'brand' ? 'Create campaigns, publish bounties, and review completed work.' : 'Connect a public profile, claim matching work, and submit proof.'}</Text>
        </Surface>
        {error && <Notice tone="error">{error}</Notice>}
        <form onSubmit={submit}>
          <div className="form-grid form-grid--2">
            <Field label={role === 'brand' ? 'Your name or brand' : 'Display name'}><Input value={name} onChange={(e) => setName(e.target.value)} placeholder={role === 'brand' ? 'Acme Labs' : 'Aarav Mehta'} required autoFocus /></Field>
            <Field label="Email"><Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" required /></Field>
          </div>
          <Field label="Password" description="At least 8 characters"><Input type="password" minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Create a password" required /></Field>
          <Field label="Short bio" required={false}><InputArea value={bio} onChange={(e) => setBio(e.target.value)} placeholder={role === 'brand' ? 'What are you building?' : 'What topics do you know well?'} rows={3} /></Field>
          <Button type="submit" variant="primary" size="lg" loading={loading} icon={<ArrowRight />}>Create account</Button>
        </form>
        <div className="auth-switch"><Text>Already have an account? <Link href="/login">Log in</Link></Text></div>
      </Surface>
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
      <Surface className="auth-form rounded-lg border border-kumo-hairline p-8">
        <div className="auth-heading"><Text variant="heading1" as="h1">Reset your password</Text><Text variant="secondary">Enter your account email. We’ll send the next step.</Text></div>
        {sent ? <Notice tone="success">If that account exists, the reset email is on its way.</Notice> : (
          <form onSubmit={submit}>
            {error && <Notice tone="error">{error}</Notice>}
            <Field label="Email"><Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus /></Field>
            <Button type="submit" variant="primary" size="lg" loading={loading}>Send reset link</Button>
          </form>
        )}
        <div className="auth-switch"><Text><Link href="/login">Back to login</Link></Text></div>
      </Surface>
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
      <Surface className="auth-form rounded-lg border border-kumo-hairline p-8">
        <div className="auth-heading"><Text variant="heading1" as="h1">Choose a new password</Text><Text variant="secondary">Use the token from your reset email.</Text></div>
        {error && <Notice tone="error">{error}</Notice>}
        <form onSubmit={submit}>
          <Field label="Reset token"><Input value={token} onChange={(e) => setToken(e.target.value)} required /></Field>
          <Field label="New password" description="At least 8 characters"><Input type="password" minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} required /></Field>
          <Button type="submit" variant="primary" size="lg" loading={loading}>Set new password</Button>
        </form>
      </Surface>
    </AuthLayout>
  )
}
