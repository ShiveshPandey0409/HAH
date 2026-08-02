import { ArrowRight, BriefcaseBusiness, CircleCheckBig, CircleDollarSign, Link2, Search, Sparkles, UserRoundCheck } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { Badge, Button, EmptyState, PageHeader, Skeleton, Stat } from '../components/UI'
import { api } from '../lib/api'
import { localClaims, money, relativeDate, titleCase } from '../lib/utils'
import type { EligibleBounty, SocialProfile, Task } from '../types'

export function DashboardPage() {
  const { user } = useAuth()
  const [tasks, setTasks] = useState<Task[]>([])
  const [bounties, setBounties] = useState<EligibleBounty[]>([])
  const [profiles, setProfiles] = useState<SocialProfile[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!user) return
    const calls: Promise<void>[] = []
    if (user.can_create_tasks) calls.push(api.listTasks().then(setTasks))
    if (user.can_work_tasks) {
      calls.push(api.listProfiles(user.id).then(setProfiles))
      calls.push(api.listBounties(user.id).then(setBounties).catch(() => setBounties([])))
    }
    Promise.all(calls).finally(() => setLoading(false))
  }, [user])

  if (!user) return null
  const creator = user.can_create_tasks
  const claims = user.can_work_tasks ? localClaims.list(user.id) : []
  const allocated = tasks.reduce((sum, task) => sum + task.allocated_budget_minor, 0)
  const openTasks = tasks.filter((task) => task.status === 'open')

  return (
    <div className="page page--dashboard">
      <PageHeader
        title={`Hey, ${user.display_name.split(' ')[0]}`}
        description={creator ? 'Your campaigns and human work, in one place.' : 'Fresh work that fits your public profiles.'}
        action={<Link to={creator ? '/app/tasks/new' : '/app/marketplace'}><Button>{creator ? 'Create a task' : 'Find work'} <ArrowRight size={17} /></Button></Link>}
      />

      {loading ? (
        <div className="stats-row"><Skeleton /><Skeleton /><Skeleton /></div>
      ) : creator ? (
        <div className="stats-row">
          <Stat label="Open campaigns" value={openTasks.length} detail={`${tasks.length} total`} />
          <Stat label="Committed budget" value={money(allocated, tasks[0]?.currency ?? 'USD')} detail="Across every bounty slot" />
          <Stat label="Claims in progress" value={tasks.reduce((sum, task) => sum + task.bounties.reduce((n, bounty) => n + bounty.claim_count, 0), 0)} detail="Waiting on human work" />
        </div>
      ) : (
        <div className="stats-row">
          <Stat label="Matched bounties" value={bounties.length} detail="Based on your profiles" />
          <Stat label="Active work" value={claims.filter((claim) => !claim.submission).length} detail="Claimed and not submitted" />
          <Stat label="Submitted" value={claims.filter((claim) => claim.submission).length} detail="Proof sent for review" />
        </div>
      )}

      <div className="dashboard-grid">
        <section className="panel panel--main">
          <div className="panel__header">
            <div><h2>{creator ? 'Recent tasks' : 'Work picked for you'}</h2><p>{creator ? 'Your latest campaign activity.' : 'Eligible now, while slots last.'}</p></div>
            <Link to={creator ? '/app/tasks' : '/app/marketplace'}>View all <ArrowRight size={15} /></Link>
          </div>
          {loading ? <><Skeleton className="skeleton--row" /><Skeleton className="skeleton--row" /><Skeleton className="skeleton--row" /></> : creator ? (
            tasks.length ? <div className="activity-list">
              {tasks.slice(0, 4).map((task) => (
                <Link to={`/app/tasks/${task.id}`} className="activity-row" key={task.id}>
                  <span className="activity-row__icon"><BriefcaseBusiness size={18} /></span>
                  <span className="activity-row__copy"><strong>{task.title}</strong><small>{task.bounties.length} bounties · {money(task.allocated_budget_minor, task.currency)}</small></span>
                  <Badge tone={task.status === 'open' ? 'positive' : task.status === 'draft' ? 'warning' : 'neutral'}>{titleCase(task.status)}</Badge>
                  <small>{relativeDate(task.updated_at)}</small>
                </Link>
              ))}
            </div> : <EmptyState icon={<Sparkles size={22} />} title="Your first campaign starts here" body="Create a task, add at least one bounty, then publish it for humans to claim." action={<Link to="/app/tasks/new"><Button>Create a task</Button></Link>} />
          ) : bounties.length ? <div className="activity-list">
            {bounties.slice(0, 4).map((bounty) => (
              <Link to="/app/marketplace" className="activity-row" key={bounty.bounty_id}>
                <span className={`platform-icon platform-icon--${bounty.platform}`}>{bounty.platform === 'reddit' ? 'r/' : 'in'}</span>
                <span className="activity-row__copy"><strong>{bounty.bounty_title}</strong><small>{bounty.task_title} · {titleCase(bounty.action)}</small></span>
                <strong className="activity-row__money">{money(bounty.reward_minor, bounty.currency)}</strong>
                <small>{bounty.remaining_slots} left</small>
              </Link>
            ))}
          </div> : <EmptyState icon={<Search size={22} />} title="No matching work yet" body={profiles.length ? 'New bounties will appear as creators publish work that fits your profiles.' : 'Add a public social profile so we can match you with eligible work.'} action={<Link to={profiles.length ? '/app/marketplace' : '/app/profiles'}><Button>{profiles.length ? 'Refresh work' : 'Add profile'}</Button></Link>} />}
        </section>

        <aside className="dashboard-side">
          <section className="panel next-step">
            <div className="next-step__mark">NEXT</div>
            {creator ? (
              <>
                <span className="next-step__icon"><UserRoundCheck size={22} /></span>
                <h2>{openTasks.length ? 'Review incoming work' : 'Publish a draft task'}</h2>
                <p>{openTasks.length ? 'Use a submission ID from your webhook to approve, reject, or request review.' : 'Drafts stay private until you open them.'}</p>
                <Link to={openTasks.length ? '/app/review' : tasks[0] ? `/app/tasks/${tasks[0].id}` : '/app/tasks/new'}>Go there <ArrowRight size={16} /></Link>
              </>
            ) : (
              <>
                <span className="next-step__icon"><Link2 size={22} /></span>
                <h2>{profiles.length ? 'Claim your first bounty' : 'Connect a public profile'}</h2>
                <p>{profiles.length ? 'Pick work that fits your audience, then submit proof when it is live.' : 'Paste a Reddit or LinkedIn profile URL. Nothing else.'}</p>
                <Link to={profiles.length ? '/app/marketplace' : '/app/profiles'}>Go there <ArrowRight size={16} /></Link>
              </>
            )}
          </section>
          <section className="panel principle">
            <CircleDollarSign size={22} />
            <h3>Fixed work. Clear reward.</h3>
            <p>Every claim keeps the reward shown when it was accepted.</p>
            <div className="principle__line"><CircleCheckBig size={16} /> No bidding</div>
            <div className="principle__line"><CircleCheckBig size={16} /> No follower access</div>
          </section>
        </aside>
      </div>
    </div>
  )
}
