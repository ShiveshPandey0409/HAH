import { KeyRound, UserRound } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { useAuth } from '../auth/AuthContext'
import { Button, Field, Input, Notice, PageHeader } from '../components/UI'
import { api } from '../lib/api'

export function SettingsPage() {
  const { user } = useAuth()
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  if (!user) return null

  const changePassword = async (event: FormEvent) => {
    event.preventDefault(); setLoading(true); setError(''); setMessage('')
    try { await api.changePassword(currentPassword, newPassword); setMessage('Password changed.'); setCurrentPassword(''); setNewPassword('') }
    catch (nextError) { setError(nextError instanceof Error ? nextError.message : 'Could not change password') }
    finally { setLoading(false) }
  }

  return (
    <div className="page settings-page">
      <PageHeader title="Account settings" description="Your account details and password." />
      <section className="settings-section"><div className="settings-section__heading"><span><UserRound size={20} /></span><div><h2>Profile</h2><p>Account identity returned by the API.</p></div></div><dl className="profile-facts"><div><dt>Display name</dt><dd>{user.display_name}</dd></div><div><dt>Email</dt><dd>{user.email}</dd></div><div><dt>Role</dt><dd>{user.can_create_tasks && user.can_work_tasks ? 'Creator and human' : user.can_create_tasks ? 'Creator' : 'Human'}</dd></div><div><dt>Bio</dt><dd>{user.bio || 'Not provided'}</dd></div></dl></section>
      <section className="settings-section" id="password"><div className="settings-section__heading"><span><KeyRound size={20} /></span><div><h2>Change password</h2><p>Use at least 8 characters.</p></div></div>{error && <Notice tone="error">{error}</Notice>}{message && <Notice tone="success">{message}</Notice>}<form onSubmit={changePassword} className="password-form"><Field label="Current password"><Input type="password" value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} required /></Field><Field label="New password"><Input type="password" minLength={8} value={newPassword} onChange={(e) => setNewPassword(e.target.value)} required /></Field><Button type="submit" loading={loading}>Change password</Button></form></section>
    </div>
  )
}
