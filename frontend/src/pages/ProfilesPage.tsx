import { ArrowRight, CheckCircle2, ExternalLink, Link2, Plus, RefreshCw, ShieldCheck } from 'lucide-react'
import { useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { Badge, Button, EmptyState, Field, Input, Modal, Notice, PageHeader, Skeleton } from '../components/UI'
import { api } from '../lib/api'
import { titleCase } from '../lib/utils'
import type { Platform, SocialProfile } from '../types'

export function ProfilesPage() {
  const { user } = useAuth()
  const [profiles, setProfiles] = useState<SocialProfile[]>([])
  const [loading, setLoading] = useState(true)
  const [modalOpen, setModalOpen] = useState(false)
  const [platform, setPlatform] = useState<Platform>('reddit')
  const [profileUrl, setProfileUrl] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const load = () => {
    if (!user) return
    setLoading(true)
    api.listProfiles(user.id).then(setProfiles).finally(() => setLoading(false))
  }
  useEffect(load, [user])
  if (!user) return null

  const connect = async (event: FormEvent) => {
    event.preventDefault(); setSaving(true); setError('')
    try {
      const profile = await api.putProfile(user.id, platform, profileUrl)
      setProfiles((items) => [profile, ...items.filter((item) => item.platform !== profile.platform)])
      setModalOpen(false); setProfileUrl('')
    } catch (nextError) { setError(nextError instanceof Error ? nextError.message : 'Could not add profile') }
    finally { setSaving(false) }
  }

  const startAdd = (nextPlatform: Platform) => {
    const current = profiles.find((profile) => profile.platform === nextPlatform)
    setPlatform(nextPlatform); setProfileUrl(current?.profile_url ?? ''); setError(''); setModalOpen(true)
  }

  return (
    <div className="page">
      <PageHeader title="Social profiles" description="Add public URLs only. We never ask for a login or social token." action={<Button onClick={() => startAdd(profiles.some((p) => p.platform === 'reddit') ? 'linkedin' : 'reddit')}><Plus size={17} /> Add profile</Button>} />
      <div className="safety-note"><ShieldCheck size={22} /><div><strong>Your account stays yours.</strong><p>HAH reads public profile metrics for eligibility. It cannot post, comment, or access your account.</p></div></div>
      {loading ? <div className="profile-list"><Skeleton className="skeleton--task" /><Skeleton className="skeleton--task" /></div> : profiles.length ? (
        <div className="profile-list">
          {profiles.map((profile) => (
            <article className="profile-row" key={profile.id}>
              <span className={`platform-icon platform-icon--${profile.platform}`}>{profile.platform === 'reddit' ? 'r/' : 'in'}</span>
              <div className="profile-row__main">
                <div><h2>{titleCase(profile.platform)}</h2><Badge tone={profile.is_verified ? 'positive' : 'warning'}>{profile.is_verified ? 'Verified' : 'Needs verification'}</Badge></div>
                <a href={profile.profile_url} target="_blank" rel="noreferrer">{profile.profile_url} <ExternalLink size={13} /></a>
                <div className="profile-row__metrics">
                  <span><strong>{profile.follower_count?.toLocaleString() ?? '—'}</strong> followers</span>
                  {profile.platform === 'reddit' && <span><strong>{profile.karma?.toLocaleString() ?? '—'}</strong> karma</span>}
                  <span><strong>{profile.following_count?.toLocaleString() ?? '—'}</strong> following</span>
                </div>
              </div>
              <Button variant="secondary" onClick={() => startAdd(profile.platform)}><RefreshCw size={15} /> Refresh</Button>
            </article>
          ))}
          <div className="profile-next"><CheckCircle2 size={20} /><span>Profiles added. Browse work matched to your verified reach.</span><Link to="/app/marketplace">Find work <ArrowRight size={16} /></Link></div>
        </div>
      ) : <EmptyState icon={<Link2 size={23} />} title="Connect your first public profile" body="Paste a Reddit or LinkedIn profile URL. HAH uses public metrics to match you with eligible tasks." action={<Button onClick={() => startAdd('reddit')}>Add a profile</Button>} />}
      <Modal open={modalOpen} onClose={() => setModalOpen(false)} title={`${profiles.some((profile) => profile.platform === platform) ? 'Update' : 'Add'} ${titleCase(platform)} profile`}>
        <form onSubmit={connect} className="modal-form">
          {error && <Notice tone="error">{error}</Notice>}
          <div className="platform-picker"><button type="button" className={platform === 'reddit' ? 'is-active' : ''} onClick={() => { setPlatform('reddit'); setProfileUrl(profiles.find((p) => p.platform === 'reddit')?.profile_url ?? '') }}><span className="platform-icon platform-icon--reddit">r/</span> Reddit</button><button type="button" className={platform === 'linkedin' ? 'is-active' : ''} onClick={() => { setPlatform('linkedin'); setProfileUrl(profiles.find((p) => p.platform === 'linkedin')?.profile_url ?? '') }}><span className="platform-icon platform-icon--linkedin">in</span> LinkedIn</button></div>
          <Field label="Public account URL" hint={platform === 'reddit' ? 'Example: https://www.reddit.com/user/yourname' : 'Example: https://www.linkedin.com/in/yourname'}><Input type="url" value={profileUrl} onChange={(e) => setProfileUrl(e.target.value)} placeholder={platform === 'reddit' ? 'https://reddit.com/user/…' : 'https://linkedin.com/in/…'} required autoFocus /></Field>
          <Notice>Enrichment must be configured on the backend. If unavailable, HAH will preserve the URL and explain the issue.</Notice>
          <div className="modal-actions"><Button type="button" variant="ghost" onClick={() => setModalOpen(false)}>Cancel</Button><Button type="submit" loading={saving}>Save profile</Button></div>
        </form>
      </Modal>
    </div>
  )
}
