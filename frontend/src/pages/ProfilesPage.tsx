import { Button, Empty, Field, Input, Link, Loader, Surface, Tabs } from '@cloudflare/kumo'
import { ArrowClockwise, ArrowRight, ArrowSquareOut, CheckCircle, LinkSimple, Plus } from '@phosphor-icons/react'
import { useEffect, useState, type FormEvent } from 'react'
import { useAuth } from '../auth/AuthContext'
import { Modal, Notice, PageHeader, StatusBadge } from '../components/UI'
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
      <PageHeader title="Social profiles" description="Add public URLs only. We never ask for a login or social token." action={<Button variant="primary" icon={<Plus />} onClick={() => startAdd(profiles.some((p) => p.platform === 'reddit') ? 'linkedin' : 'reddit')}>Add profile</Button>} />
      <Notice><strong>Your account stays yours.</strong> HAH reads public profile metrics for eligibility. It cannot post, comment, or access your account.</Notice>
      {loading ? <div className="loading-state"><Loader size="lg" /></div> : profiles.length ? (
        <div className="profile-list">
          {profiles.map((profile) => (
            <Surface as="article" className="profile-row rounded-lg border border-kumo-hairline p-5" key={profile.id}>
              <span className={`platform-icon platform-icon--${profile.platform}`}>{profile.platform === 'reddit' ? 'r/' : 'in'}</span>
              <div className="profile-row__main">
                <div><h2>{titleCase(profile.platform)}</h2><StatusBadge tone={profile.is_verified ? 'positive' : 'warning'}>{profile.is_verified ? 'Verified' : 'Needs verification'}</StatusBadge></div>
                <Link href={profile.profile_url} target="_blank" rel="noreferrer">{profile.profile_url} <ArrowSquareOut size={13} /></Link>
                <div className="profile-row__metrics">
                  <span><strong>{profile.follower_count?.toLocaleString() ?? '—'}</strong> followers</span>
                  {profile.platform === 'reddit' && <span><strong>{profile.karma?.toLocaleString() ?? '—'}</strong> karma</span>}
                  <span><strong>{profile.following_count?.toLocaleString() ?? '—'}</strong> following</span>
                </div>
              </div>
              <Button variant="secondary" icon={<ArrowClockwise />} onClick={() => startAdd(profile.platform)}>Refresh</Button>
            </Surface>
          ))}
          <div className="profile-next"><CheckCircle size={20} /><span>Profiles added. Browse work matched to your verified reach.</span><Link href="/app/marketplace">Find work <ArrowRight size={16} /></Link></div>
        </div>
      ) : <Empty icon={<LinkSimple size={40} />} title="Connect your first public profile" description="Paste a Reddit or LinkedIn profile URL. HAH uses public metrics to match you with eligible tasks." contents={<Button variant="primary" onClick={() => startAdd('reddit')}>Add a profile</Button>} />}
      <Modal open={modalOpen} onClose={() => setModalOpen(false)} title={`${profiles.some((profile) => profile.platform === platform) ? 'Update' : 'Add'} ${titleCase(platform)} profile`}>
        <form onSubmit={connect} className="modal-form">
          {error && <Notice tone="error">{error}</Notice>}
          <Tabs value={platform} onValueChange={(value) => { const next = value as Platform; setPlatform(next); setProfileUrl(profiles.find((p) => p.platform === next)?.profile_url ?? '') }} tabs={[{ value: 'reddit', label: 'Reddit' }, { value: 'linkedin', label: 'LinkedIn' }]} />
          <Field label="Public account URL" description={platform === 'reddit' ? 'Example: https://www.reddit.com/user/yourname' : 'Example: https://www.linkedin.com/in/yourname'}><Input type="url" value={profileUrl} onChange={(e) => setProfileUrl(e.target.value)} placeholder={platform === 'reddit' ? 'https://reddit.com/user/…' : 'https://linkedin.com/in/…'} required autoFocus /></Field>
          <Notice>Enrichment must be configured on the backend. If unavailable, HAH will preserve the URL and explain the issue.</Notice>
          <div className="modal-actions"><Button type="button" variant="ghost" onClick={() => setModalOpen(false)}>Cancel</Button><Button type="submit" variant="primary" loading={saving}>Save profile</Button></div>
        </form>
      </Modal>
    </div>
  )
}
