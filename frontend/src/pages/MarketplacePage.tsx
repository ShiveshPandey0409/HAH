import { Badge, Button, Empty, LinkButton, Loader, Select, Surface, Tabs } from '@cloudflare/kumo'
import { Calendar, Check, Funnel, MagnifyingGlass, SlidersHorizontal, Sparkle, Users, X } from '@phosphor-icons/react'
import { useEffect, useMemo, useState } from 'react'
import { useAuth } from '../auth/AuthContext'
import { Modal, Notice, PageHeader } from '../components/UI'
import { api } from '../lib/api'
import { date, localClaims, money, titleCase } from '../lib/utils'
import type { EligibleBounty, Platform } from '../types'

export function MarketplacePage() {
  const { user } = useAuth()
  const [bounties, setBounties] = useState<EligibleBounty[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [platform, setPlatform] = useState<'all' | Platform>('all')
  const [action, setAction] = useState('all')
  const [selected, setSelected] = useState<EligibleBounty | null>(null)
  const [claiming, setClaiming] = useState(false)
  const [claimed, setClaimed] = useState(false)

  const load = () => {
    if (!user) return
    setLoading(true); setError('')
    api.listBounties(user.id).then(setBounties).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }
  useEffect(load, [user])
  const visible = useMemo(() => bounties.filter((bounty) => (platform === 'all' || bounty.platform === platform) && (action === 'all' || bounty.action === action)), [action, bounties, platform])
  if (!user) return null

  const claim = async () => {
    if (!selected) return
    setClaiming(true); setError('')
    try {
      const response = await api.claimBounty(selected.bounty_id, selected.social_account_id)
      localClaims.add(user.id, { ...response, bounty: selected })
      setBounties((items) => items.filter((item) => item.bounty_id !== selected.bounty_id)); setClaimed(true)
    } catch (nextError) { setError(nextError instanceof Error ? nextError.message : 'Could not claim bounty') }
    finally { setClaiming(false) }
  }

  return (
    <div className="page marketplace">
      <PageHeader title="Find work" description="Only bounties that match a verified profile appear here." action={<Button variant="secondary" icon={<MagnifyingGlass />} onClick={load}>Refresh</Button>} />
      {error && !selected && <Notice tone="error">{error}</Notice>}
      <div className="market-filters">
        <div><Funnel size={17} /><strong>Filter</strong></div>
        <Tabs size="sm" value={platform} onValueChange={(value) => setPlatform(value as 'all' | Platform)} tabs={(['all', 'reddit', 'linkedin'] as const).map((item) => ({ value: item, label: titleCase(item) }))} />
        <Select aria-label="Action" value={action} onValueChange={(value) => setAction(value ?? 'all')} items={{ all: 'All actions', post: 'Posts', comment: 'Comments' }} />
        {(platform !== 'all' || action !== 'all') && <Button variant="ghost" size="sm" icon={<X />} onClick={() => { setPlatform('all'); setAction('all') }}>Clear</Button>}
      </div>
      {loading ? <div className="loading-state"><Loader size="lg" /></div> : visible.length ? (
        <div className="market-grid">
          {visible.map((bounty) => (
            <Surface as="article" className="market-item rounded-lg border border-kumo-hairline p-5" key={bounty.bounty_id}>
              <div className="market-item__top"><span className={`platform-icon platform-icon--${bounty.platform}`}>{bounty.platform === 'reddit' ? 'r/' : 'in'}</span><Badge variant="secondary">{titleCase(bounty.action)}</Badge><span className="market-item__spots"><Users size={14} /> {bounty.remaining_slots} left</span></div>
              <div className="market-item__body"><small>{bounty.task_title}</small><h2>{bounty.bounty_title}</h2><p>{bounty.instructions}</p></div>
              <div className="market-item__proof"><span>Proof required</span>{bounty.proof_requirements.map((proof) => <Badge variant="neutral" key={proof}>{titleCase(proof)}</Badge>)}</div>
              <div className="market-item__foot"><div><strong>{money(bounty.reward_minor, bounty.currency)}</strong><small>fixed reward</small></div><Button variant="primary" onClick={() => { setSelected(bounty); setClaimed(false); setError('') }}>View & claim</Button></div>
              <div className="market-item__deadline"><Calendar size={14} /> {date(bounty.effective_deadline)}</div>
            </Surface>
          ))}
        </div>
      ) : <Empty icon={bounties.length ? <SlidersHorizontal size={40} /> : <Sparkle size={40} />} title={bounties.length ? 'No work under these filters' : 'No eligible bounties right now'} description={bounties.length ? 'Clear a filter to see more work.' : 'Make sure your public profile is verified. New matching work appears here automatically.'} contents={<LinkButton href="/app/profiles" variant="secondary">Check profiles</LinkButton>} />}

      <Modal open={Boolean(selected)} onClose={() => setSelected(null)} title={claimed ? 'The work is yours' : selected?.bounty_title ?? 'Bounty'}>
        {selected && (claimed ? (
          <div className="claim-success"><span><Check size={24} /></span><h3>Claim confirmed</h3><p>Complete the task yourself, then submit the required proof from My work.</p><div className="modal-actions"><Button variant="ghost" onClick={() => setSelected(null)}>Keep browsing</Button><LinkButton href="/app/work" variant="primary">Go to my work</LinkButton></div></div>
        ) : (
          <div className="claim-detail">
            {error && <Notice tone="error">{error}</Notice>}
            <div className="claim-detail__meta"><Badge variant="secondary">{titleCase(selected.platform)} · {titleCase(selected.action)}</Badge><strong>{money(selected.reward_minor, selected.currency)}</strong></div>
            <h3>{selected.task_title}</h3><p>{selected.task_description}</p>
            <div className="instruction-box"><span>What to do</span><p>{selected.instructions}</p></div>
            <dl><div><dt>Deadline</dt><dd>{date(selected.effective_deadline)}</dd></div><div><dt>Proof</dt><dd>{selected.proof_requirements.map(titleCase).join(', ')}</dd></div><div><dt>Slots left</dt><dd>{selected.remaining_slots}</dd></div></dl>
            <Notice>Claiming reserves one slot. Complete the work through your own account.</Notice>
            <div className="modal-actions"><Button variant="ghost" onClick={() => setSelected(null)}>Cancel</Button><Button variant="primary" onClick={claim} loading={claiming}>Claim this work</Button></div>
          </div>
        ))}
      </Modal>
    </div>
  )
}
