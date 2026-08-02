import { CalendarClock, Check, Filter, Search, SlidersHorizontal, Sparkles, UsersRound, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { Badge, Button, EmptyState, Modal, Notice, PageHeader, Select, Skeleton } from '../components/UI'
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
      <PageHeader title="Find work" description="Only bounties that match a verified profile appear here." action={<Button variant="secondary" onClick={load}><Search size={16} /> Refresh</Button>} />
      {error && !selected && <Notice tone="error">{error}</Notice>}
      <div className="market-filters">
        <div><Filter size={17} /><strong>Filter</strong></div>
        <div className="filter-pills">{(['all', 'reddit', 'linkedin'] as const).map((item) => <button key={item} className={platform === item ? 'is-active' : ''} onClick={() => setPlatform(item)}>{titleCase(item)}</button>)}</div>
        <Select value={action} onChange={(e) => setAction(e.target.value)}><option value="all">All actions</option><option value="post">Posts</option><option value="comment">Comments</option></Select>
        {(platform !== 'all' || action !== 'all') && <button className="clear-filter" onClick={() => { setPlatform('all'); setAction('all') }}><X size={14} /> Clear</button>}
      </div>
      {loading ? <div className="market-grid"><Skeleton className="skeleton--market" /><Skeleton className="skeleton--market" /><Skeleton className="skeleton--market" /></div> : visible.length ? (
        <div className="market-grid">
          {visible.map((bounty) => (
            <article className="market-item" key={bounty.bounty_id}>
              <div className="market-item__top"><span className={`platform-icon platform-icon--${bounty.platform}`}>{bounty.platform === 'reddit' ? 'r/' : 'in'}</span><Badge>{titleCase(bounty.action)}</Badge><span className="market-item__spots"><UsersRound size={14} /> {bounty.remaining_slots} left</span></div>
              <div className="market-item__body"><small>{bounty.task_title}</small><h2>{bounty.bounty_title}</h2><p>{bounty.instructions}</p></div>
              <div className="market-item__proof"><span>Proof required</span>{bounty.proof_requirements.map((proof) => <Badge key={proof}>{titleCase(proof)}</Badge>)}</div>
              <div className="market-item__foot"><div><strong>{money(bounty.reward_minor, bounty.currency)}</strong><small>fixed reward</small></div><Button onClick={() => { setSelected(bounty); setClaimed(false); setError('') }}>View & claim</Button></div>
              <div className="market-item__deadline"><CalendarClock size={14} /> {date(bounty.effective_deadline)}</div>
            </article>
          ))}
        </div>
      ) : <EmptyState icon={bounties.length ? <SlidersHorizontal size={23} /> : <Sparkles size={23} />} title={bounties.length ? 'No work under these filters' : 'No eligible bounties right now'} body={bounties.length ? 'Clear a filter to see more work.' : 'Make sure your public profile is verified. New matching work appears here automatically.'} action={<Link to="/app/profiles"><Button variant="secondary">Check profiles</Button></Link>} />}

      <Modal open={Boolean(selected)} onClose={() => setSelected(null)} title={claimed ? 'The work is yours' : selected?.bounty_title ?? 'Bounty'}>
        {selected && (claimed ? (
          <div className="claim-success"><span><Check size={24} /></span><h3>Claim confirmed</h3><p>Complete the task yourself, then submit the required proof from My work.</p><div className="modal-actions"><Button variant="ghost" onClick={() => setSelected(null)}>Keep browsing</Button><Link to="/app/work"><Button>Go to my work</Button></Link></div></div>
        ) : (
          <div className="claim-detail">
            {error && <Notice tone="error">{error}</Notice>}
            <div className="claim-detail__meta"><Badge>{titleCase(selected.platform)} · {titleCase(selected.action)}</Badge><strong>{money(selected.reward_minor, selected.currency)}</strong></div>
            <h3>{selected.task_title}</h3><p>{selected.task_description}</p>
            <div className="instruction-box"><span>What to do</span><p>{selected.instructions}</p></div>
            <dl><div><dt>Deadline</dt><dd>{date(selected.effective_deadline)}</dd></div><div><dt>Proof</dt><dd>{selected.proof_requirements.map(titleCase).join(', ')}</dd></div><div><dt>Slots left</dt><dd>{selected.remaining_slots}</dd></div></dl>
            <Notice>Claiming reserves one slot. Complete the work through your own account.</Notice>
            <div className="modal-actions"><Button variant="ghost" onClick={() => setSelected(null)}>Cancel</Button><Button onClick={claim} loading={claiming}>Claim this work</Button></div>
          </div>
        ))}
      </Modal>
    </div>
  )
}
