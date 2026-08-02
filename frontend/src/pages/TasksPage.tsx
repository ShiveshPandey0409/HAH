import { ArrowLeft, ArrowRight, CalendarDays, Copy, FilePlus2, MoreHorizontal, Pencil, Plus, Rocket, Trash2, UsersRound } from 'lucide-react'
import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { Badge, Button, EmptyState, Field, Input, Notice, PageHeader, Select, Skeleton, Textarea } from '../components/UI'
import { api } from '../lib/api'
import { date, money, titleCase, toIso, toLocalInput } from '../lib/utils'
import type { Bounty, BountyInput, Platform, ProofType, Task, TaskInput } from '../types'

const emptyBounty = (): BountyDraft => ({
  platform: 'reddit',
  action: 'post',
  title: '',
  instructions: '',
  reward: '25',
  slot_count: '1',
  influence_metric: 'karma',
  min_influence: '0',
  max_influence: '',
  proof_requirements: ['url'],
  deadline_at: '',
})

interface BountyDraft extends Omit<BountyInput, 'reward_minor' | 'slot_count' | 'min_influence' | 'max_influence' | 'deadline_at'> {
  reward: string
  slot_count: string
  min_influence: string
  max_influence: string
  deadline_at: string
}

function taskToDraft(task: Task) {
  return {
    title: task.title,
    description: task.description,
    budget: String(task.total_budget_minor / 100),
    currency: task.currency,
    deadline_at: toLocalInput(task.deadline_at),
    bounties: task.bounties.map((bounty): BountyDraft => ({
      platform: bounty.platform,
      action: bounty.action,
      title: bounty.title,
      instructions: bounty.instructions,
      reward: String(bounty.reward_minor / 100),
      slot_count: String(bounty.slot_count),
      influence_metric: bounty.influence_metric,
      min_influence: String(bounty.min_influence),
      max_influence: bounty.max_influence == null ? '' : String(bounty.max_influence),
      proof_requirements: bounty.proof_requirements,
      deadline_at: toLocalInput(bounty.deadline_at),
    })),
  }
}

export function TasksPage() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('all')

  useEffect(() => { api.listTasks().then(setTasks).finally(() => setLoading(false)) }, [])
  const visible = filter === 'all' ? tasks : tasks.filter((task) => task.status === filter)

  return (
    <div className="page">
      <PageHeader title="My tasks" description="Build, publish, and track human work." action={<Link to="/app/tasks/new"><Button><Plus size={17} /> New task</Button></Link>} />
      <div className="filter-bar">
        {['all', 'draft', 'open', 'completed'].map((item) => <button key={item} className={filter === item ? 'is-active' : ''} onClick={() => setFilter(item)}>{titleCase(item)} <span>{item === 'all' ? tasks.length : tasks.filter((task) => task.status === item).length}</span></button>)}
      </div>
      {loading ? <div className="task-list"><Skeleton className="skeleton--task" /><Skeleton className="skeleton--task" /></div> : visible.length ? (
        <div className="task-list">
          {visible.map((task) => <TaskRow task={task} key={task.id} />)}
        </div>
      ) : <EmptyState icon={<FilePlus2 size={23} />} title={tasks.length ? `No ${filter} tasks` : 'No tasks yet'} body={tasks.length ? 'Try another status.' : 'Create a campaign with one or more paid bounties.'} action={!tasks.length ? <Link to="/app/tasks/new"><Button>Create your first task</Button></Link> : undefined} />}
    </div>
  )
}

function TaskRow({ task }: { task: Task }) {
  const claims = task.bounties.reduce((sum, bounty) => sum + bounty.claim_count, 0)
  return (
    <Link to={`/app/tasks/${task.id}`} className="task-row">
      <div className="task-row__top"><Badge tone={task.status === 'open' ? 'positive' : task.status === 'draft' ? 'warning' : 'neutral'}>{titleCase(task.status)}</Badge><span>{task.created_via === 'mcp' ? 'Created by agent' : 'Created manually'}</span></div>
      <div className="task-row__main"><div><h2>{task.title}</h2><p>{task.description}</p></div><ArrowRight size={19} /></div>
      <div className="task-row__meta">
        <span><strong>{task.bounties.length}</strong> bounties</span>
        <span><strong>{claims}</strong> claims</span>
        <span><strong>{money(task.allocated_budget_minor, task.currency)}</strong> committed</span>
        <span><CalendarDays size={15} /> {date(task.deadline_at)}</span>
      </div>
    </Link>
  )
}

export function TaskDetailPage() {
  const { taskId = '' } = useParams()
  const navigate = useNavigate()
  const [task, setTask] = useState<Task | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  useEffect(() => { api.getTask(taskId).then(setTask).catch((e) => setError(e.message)).finally(() => setLoading(false)) }, [taskId])
  const open = async () => {
    if (!task) return
    setBusy('open'); setError('')
    try { setTask(await api.openTask(task.id)) } catch (e) { setError(e instanceof Error ? e.message : 'Could not publish task') } finally { setBusy('') }
  }
  const remove = async () => {
    if (!task || !confirm(`Delete “${task.title}”? This cannot be undone.`)) return
    setBusy('delete'); setError('')
    try { await api.deleteTask(task.id); navigate('/app/tasks') } catch (e) { setError(e instanceof Error ? e.message : 'Could not delete task'); setBusy('') }
  }

  if (loading) return <div className="page"><Skeleton className="skeleton--hero" /><Skeleton className="skeleton--task" /></div>
  if (!task) return <div className="page"><Notice tone="error">{error || 'Task not found'}</Notice></div>

  return (
    <div className="page task-detail">
      <Link to="/app/tasks" className="back-link"><ArrowLeft size={16} /> All tasks</Link>
      {error && <Notice tone="error">{error}</Notice>}
      <header className="task-detail__header">
        <div><div className="task-detail__badges"><Badge tone={task.status === 'open' ? 'positive' : 'warning'}>{titleCase(task.status)}</Badge><Badge>{task.created_via === 'mcp' ? 'Agent-created' : 'Manual'}</Badge></div><h1>{task.title}</h1><p>{task.description}</p></div>
        <div className="task-detail__actions">
          {task.status === 'draft' && <><Link to={`/app/tasks/${task.id}/edit`}><Button variant="secondary"><Pencil size={16} /> Edit</Button></Link><Button loading={busy === 'open'} onClick={open}><Rocket size={16} /> Publish task</Button><Button variant="ghost" loading={busy === 'delete'} onClick={remove} aria-label="Delete task"><Trash2 size={17} /></Button></>}
        </div>
      </header>
      <div className="task-summary">
        <div><span>Total budget</span><strong>{money(task.total_budget_minor, task.currency)}</strong></div>
        <div><span>Committed</span><strong>{money(task.allocated_budget_minor, task.currency)}</strong></div>
        <div><span>Unallocated</span><strong>{money(task.remaining_budget_minor, task.currency)}</strong></div>
        <div><span>Campaign deadline</span><strong>{date(task.deadline_at)}</strong></div>
      </div>
      <section className="detail-section">
        <div className="detail-section__heading"><div><h2>Bounties</h2><p>Each one is a separate piece of paid work.</p></div><span>{task.bounties.length} total</span></div>
        <div className="bounty-list">{task.bounties.map((bounty) => <BountyDetail key={bounty.id} bounty={bounty} currency={task.currency} />)}</div>
      </section>
    </div>
  )
}

function BountyDetail({ bounty, currency }: { bounty: Bounty; currency: string }) {
  return (
    <article className="bounty-detail">
      <div className="bounty-detail__rail"><span className={`platform-icon platform-icon--${bounty.platform}`}>{bounty.platform === 'reddit' ? 'r/' : 'in'}</span><span className="bounty-detail__connector" /></div>
      <div className="bounty-detail__body">
        <div className="bounty-detail__heading"><div><span>{titleCase(bounty.platform)} · {titleCase(bounty.action)}</span><h3>{bounty.title}</h3></div><strong>{money(bounty.reward_minor, currency)} <small>/ person</small></strong></div>
        <p>{bounty.instructions}</p>
        <div className="bounty-detail__facts">
          <span><UsersRound size={16} /> {bounty.claim_count} claimed · {bounty.remaining_slots} left</span>
          <span>{titleCase(bounty.influence_metric)}: {bounty.min_influence.toLocaleString()}–{bounty.max_influence?.toLocaleString() ?? 'any'}</span>
          <span>Proof: {bounty.proof_requirements.map(titleCase).join(', ')}</span>
          <span>{date(bounty.deadline_at, 'Uses campaign deadline')}</span>
        </div>
      </div>
    </article>
  )
}

export function TaskEditorPage() {
  const { taskId } = useParams()
  const navigate = useNavigate()
  const editing = Boolean(taskId)
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [budget, setBudget] = useState('100')
  const [currency, setCurrency] = useState('USD')
  const [deadline, setDeadline] = useState('')
  const [bounties, setBounties] = useState<BountyDraft[]>([emptyBounty()])
  const [loading, setLoading] = useState(editing)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!taskId) return
    api.getTask(taskId).then((task) => {
      const draft = taskToDraft(task)
      setTitle(draft.title); setDescription(draft.description); setBudget(draft.budget); setCurrency(draft.currency); setDeadline(draft.deadline_at); setBounties(draft.bounties)
    }).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }, [taskId])

  const updateBounty = <K extends keyof BountyDraft>(index: number, key: K, value: BountyDraft[K]) => {
    setBounties((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, [key]: value } : item))
  }
  const allocated = useMemo(() => bounties.reduce((sum, bounty) => sum + (Number(bounty.reward) || 0) * (Number(bounty.slot_count) || 0), 0), [bounties])
  const toggleProof = (index: number, proof: ProofType) => {
    const current = bounties[index].proof_requirements
    updateBounty(index, 'proof_requirements', current.includes(proof) ? current.filter((item) => item !== proof) : [...current, proof])
  }
  const duplicate = (index: number) => setBounties((items) => [...items.slice(0, index + 1), { ...items[index], title: `${items[index].title} copy` }, ...items.slice(index + 1)])
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setSaving(true); setError('')
    try {
      const payload: TaskInput = {
        title, description, currency: currency.toUpperCase(), total_budget_minor: Math.round(Number(budget) * 100), deadline_at: toIso(deadline),
        bounties: bounties.map((bounty) => ({
          platform: bounty.platform, action: bounty.action, title: bounty.title, instructions: bounty.instructions,
          reward_minor: Math.round(Number(bounty.reward) * 100), slot_count: Number(bounty.slot_count), influence_metric: bounty.platform === 'linkedin' ? 'followers' : bounty.influence_metric,
          min_influence: Number(bounty.min_influence), max_influence: bounty.max_influence ? Number(bounty.max_influence) : null,
          proof_requirements: bounty.proof_requirements, deadline_at: toIso(bounty.deadline_at),
        })),
      }
      const task = taskId ? await api.replaceTask(taskId, payload) : await api.createTask(payload)
      navigate(`/app/tasks/${task.id}`)
    } catch (nextError) { setError(nextError instanceof Error ? nextError.message : 'Could not save task') }
    finally { setSaving(false) }
  }

  if (loading) return <div className="page"><Skeleton className="skeleton--hero" /><Skeleton className="skeleton--task" /></div>
  return (
    <div className="page editor-page">
      <Link to={taskId ? `/app/tasks/${taskId}` : '/app/tasks'} className="back-link"><ArrowLeft size={16} /> {taskId ? 'Back to task' : 'Cancel'}</Link>
      <PageHeader title={editing ? 'Edit task' : 'Create a task'} description="One campaign can contain multiple paid bounties." />
      {error && <Notice tone="error">{error}</Notice>}
      <form onSubmit={submit} className="editor-layout">
        <div className="editor-main">
          <section className="form-section">
            <div className="form-section__heading"><span>1</span><div><h2>Campaign brief</h2><p>What are humans helping you promote?</p></div></div>
            <Field label="Task title"><Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Launch HAH to indie builders" required autoFocus /></Field>
            <Field label="Description"><Textarea value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Give people the context they need to understand the campaign." rows={4} required /></Field>
            <div className="form-grid form-grid--3">
              <Field label="Total budget"><Input type="number" min="0.01" step="0.01" value={budget} onChange={(e) => setBudget(e.target.value)} required /></Field>
              <Field label="Currency"><Input value={currency} maxLength={3} onChange={(e) => setCurrency(e.target.value.toUpperCase())} required /></Field>
              <Field label="Campaign deadline" hint="Optional"><Input type="datetime-local" value={deadline} onChange={(e) => setDeadline(e.target.value)} /></Field>
            </div>
          </section>
          <section className="form-section">
            <div className="form-section__heading"><span>2</span><div><h2>Paid bounties</h2><p>Define each post or comment separately.</p></div></div>
            <div className="bounty-editor-list">
              {bounties.map((bounty, index) => (
                <BountyEditor key={index} bounty={bounty} index={index} update={updateBounty} toggleProof={toggleProof} duplicate={duplicate} remove={() => setBounties((items) => items.filter((_, itemIndex) => itemIndex !== index))} canRemove={bounties.length > 1} />
              ))}
            </div>
            <Button type="button" variant="secondary" onClick={() => setBounties((items) => [...items, emptyBounty()])}><Plus size={17} /> Add another bounty</Button>
          </section>
        </div>
        <aside className="editor-summary">
          <h2>Budget check</h2>
          <div><span>Total budget</span><strong>{money(Math.round(Number(budget || 0) * 100), currency)}</strong></div>
          <div><span>Bounties</span><strong>{money(Math.round(allocated * 100), currency)}</strong></div>
          <div className={allocated > Number(budget) ? 'is-over' : ''}><span>Unallocated</span><strong>{money(Math.round((Number(budget || 0) - allocated) * 100), currency)}</strong></div>
          <div className="budget-meter"><span style={{ width: `${Math.min(100, budget ? (allocated / Number(budget)) * 100 : 0)}%` }} /></div>
          {allocated > Number(budget) && <small className="editor-summary__error">Bounties exceed the task budget.</small>}
          <Button type="submit" loading={saving} disabled={allocated > Number(budget) || bounties.some((bounty) => !bounty.proof_requirements.length)}>Save draft <ArrowRight size={17} /></Button>
          <p>Drafts are private until you publish them.</p>
        </aside>
      </form>
    </div>
  )
}

function BountyEditor({ bounty, index, update, toggleProof, duplicate, remove, canRemove }: {
  bounty: BountyDraft
  index: number
  update: <K extends keyof BountyDraft>(index: number, key: K, value: BountyDraft[K]) => void
  toggleProof: (index: number, proof: ProofType) => void
  duplicate: (index: number) => void
  remove: () => void
  canRemove: boolean
}) {
  return (
    <fieldset className="bounty-editor">
      <legend>Bounty {index + 1}</legend>
      <div className="bounty-editor__tools"><button type="button" onClick={() => duplicate(index)}><Copy size={15} /> Duplicate</button>{canRemove && <button type="button" onClick={remove}><Trash2 size={15} /> Remove</button>}<MoreHorizontal size={17} /></div>
      <div className="form-grid form-grid--2">
        <Field label="Platform"><Select value={bounty.platform} onChange={(e) => { const platform = e.target.value as Platform; update(index, 'platform', platform); if (platform === 'linkedin') update(index, 'influence_metric', 'followers') }}><option value="reddit">Reddit</option><option value="linkedin">LinkedIn</option></Select></Field>
        <Field label="Action"><Select value={bounty.action} onChange={(e) => update(index, 'action', e.target.value as 'post' | 'comment')}><option value="post">Create a post</option><option value="comment">Add a comment</option></Select></Field>
      </div>
      <Field label="Bounty title"><Input value={bounty.title} onChange={(e) => update(index, 'title', e.target.value)} placeholder="Share our launch story" required /></Field>
      <Field label="Instructions"><Textarea value={bounty.instructions} onChange={(e) => update(index, 'instructions', e.target.value)} placeholder="Say what good work looks like. Keep it specific." rows={4} required /></Field>
      <div className="form-grid form-grid--3">
        <Field label="Reward per person"><Input type="number" min="0.01" step="0.01" value={bounty.reward} onChange={(e) => update(index, 'reward', e.target.value)} required /></Field>
        <Field label="Available spots"><Input type="number" min="1" value={bounty.slot_count} onChange={(e) => update(index, 'slot_count', e.target.value)} required /></Field>
        <Field label="Bounty deadline" hint="Optional"><Input type="datetime-local" value={bounty.deadline_at} onChange={(e) => update(index, 'deadline_at', e.target.value)} /></Field>
      </div>
      <div className="form-grid form-grid--3">
        <Field label="Audience metric"><Select value={bounty.platform === 'linkedin' ? 'followers' : bounty.influence_metric} disabled={bounty.platform === 'linkedin'} onChange={(e) => update(index, 'influence_metric', e.target.value as 'followers' | 'karma')}><option value="followers">Followers</option>{bounty.platform === 'reddit' && <option value="karma">Reddit karma</option>}</Select></Field>
        <Field label="Minimum"><Input type="number" min="0" value={bounty.min_influence} onChange={(e) => update(index, 'min_influence', e.target.value)} required /></Field>
        <Field label="Maximum" hint="Optional"><Input type="number" min="0" value={bounty.max_influence} onChange={(e) => update(index, 'max_influence', e.target.value)} /></Field>
      </div>
      <div className="field"><span className="field__label">Required proof</span><div className="check-row">{(['url', 'screenshot', 'image'] as ProofType[]).map((proof) => <label key={proof} className={bounty.proof_requirements.includes(proof) ? 'check-chip is-checked' : 'check-chip'}><input type="checkbox" checked={bounty.proof_requirements.includes(proof)} onChange={() => toggleProof(index, proof)} /><span>{titleCase(proof)}</span></label>)}</div>{!bounty.proof_requirements.length && <span className="field__error">Select at least one proof type.</span>}</div>
    </fieldset>
  )
}
