import { Button, Empty, Field, Input, InputArea, Link, Loader, Surface, Tabs } from '@cloudflare/kumo'
import { ArrowSquareOut, Check, ClipboardText, MagnifyingGlass, SealQuestion } from '@phosphor-icons/react'
import { useEffect, useState, type FormEvent } from 'react'
import { Notice, PageHeader, StatusBadge } from '../components/UI'
import { api } from '../lib/api'
import { date, titleCase } from '../lib/utils'
import type { Payment, Submission, Task } from '../types'

interface ReviewItem {
  submission: Submission
  taskTitle: string
}

export function ReviewPage() {
  const [items, setItems] = useState<ReviewItem[]>([])
  const [selected, setSelected] = useState<ReviewItem | null>(null)
  const [submissionId, setSubmissionId] = useState('')
  const [result, setResult] = useState<'passed' | 'failed' | 'review_required'>('passed')
  const [reason, setReason] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [proofImages, setProofImages] = useState<Record<string, string>>({})
  const [payment, setPayment] = useState<Payment | null>(null)

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const tasks = await api.listTasks()
      const submissions = await Promise.all(tasks.map(async (task: Task) => ({
        task,
        submissions: await api.listTaskSubmissions(task.id),
      })))
      const nextItems = submissions.flatMap(({ task, submissions: taskSubmissions }) =>
        taskSubmissions.map((submission) => ({ submission, taskTitle: task.title })),
      )
      nextItems.sort((a, b) => b.submission.submitted_at.localeCompare(a.submission.submitted_at))
      setItems(nextItems)
      setSelected((current) => current ? nextItems.find((item) => item.submission.id === current.submission.id) ?? current : null)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Could not load submissions')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  const lookup = async (event: FormEvent) => {
    event.preventDefault()
    setSaving(true)
    setError('')
    setPayment(null)
    try {
      const submission = await api.getSubmission(submissionId.trim())
      setSelected({ submission, taskTitle: 'Direct lookup' })
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Could not find submission')
    } finally {
      setSaving(false)
    }
  }

  const verify = async (event: FormEvent) => {
    event.preventDefault()
    if (!selected) return
    setSaving(true)
    setError('')
    try {
      const submission = await api.verifySubmission(
        selected.submission.id,
        result,
        result === 'failed' ? reason : undefined,
      )
      const updated = { ...selected, submission }
      setSelected(updated)
      setItems((current) => current.map((item) => item.submission.id === submission.id ? updated : item))
      if (result === 'passed') setPayment(await api.getSubmissionPayment(submission.id))
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Could not verify submission')
    } finally {
      setSaving(false)
    }
  }

  const loadImage = async (proofId: string, contentUrl: string) => {
    try {
      const blob = await api.getProofContent(contentUrl)
      setProofImages((current) => ({ ...current, [proofId]: URL.createObjectURL(blob) }))
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Could not load proof image')
    }
  }

  return (
    <div className="page review-page">
      <PageHeader title="Review work" description="Submissions from all your tasks, synced directly from the API." action={<Button variant="secondary" onClick={() => void load()}>Refresh</Button>} />
      {error && <Notice tone="error">{error}</Notice>}
      <Surface as="section" className="panel rounded-lg border border-kumo-hairline p-5">
        <form onSubmit={lookup} className="review-lookup">
          <Field label="Find a submission by ID"><Input value={submissionId} onChange={(event) => setSubmissionId(event.target.value)} placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" required /></Field>
          <Button type="submit" variant="secondary" loading={saving} icon={<MagnifyingGlass />}>Find</Button>
        </form>
      </Surface>
      <div className="review-layout">
        <Surface as="section" className="panel review-inbox rounded-lg border border-kumo-hairline p-6">
          <div className="review-form-panel__heading"><span><ClipboardText size={22} /></span><div><h2>Submission inbox</h2><p>Open a submission to inspect proof and record a decision.</p></div></div>
          {loading ? <div className="loading-state"><Loader /></div> : items.length ? <div className="review-inbox__list">
            {items.map((item) => <button type="button" className={`review-inbox__item${selected?.submission.id === item.submission.id ? ' is-selected' : ''}`} key={item.submission.id} onClick={() => { setSelected(item); setError(''); setPayment(null) }}>
              <span><strong>{item.taskTitle}</strong><small>{date(item.submission.submitted_at, '')}</small></span>
              <StatusBadge tone={item.submission.verification_status === 'passed' ? 'positive' : item.submission.verification_status === 'failed' ? 'danger' : 'warning'}>{titleCase(item.submission.verification_status)}</StatusBadge>
            </button>)}
          </div> : <Empty icon={<SealQuestion size={36} />} title="No submissions yet" description="New human proof will appear here automatically." />}
        </Surface>
        <Surface as="section" className="panel review-form-panel rounded-lg border border-kumo-hairline p-6">
          {selected ? <>
            <div className="review-form-panel__heading"><span><SealQuestion size={22} /></span><div><h2>{selected.taskTitle}</h2><p>Revision {selected.submission.revision} · {selected.submission.id}</p></div></div>
            <div className="proof-list">{selected.submission.proofs.map((proof) => <div key={proof.id}><span>{titleCase(proof.proof_type)}</span>{proof.url ? <Link href={proof.url} target="_blank" rel="noreferrer">Open proof <ArrowSquareOut size={14} /></Link> : proof.content_url ? <><Button size="sm" variant="secondary" onClick={() => loadImage(proof.id, proof.content_url!)}>Load image</Button>{proofImages[proof.id] && <img className="proof-preview" src={proofImages[proof.id]} alt={`${titleCase(proof.proof_type)} proof`} />}</> : <code>{proof.storage_key}</code>}</div>)}</div>
            {selected.submission.failure_reason && <Notice tone="error">{selected.submission.failure_reason}</Notice>}
            {payment && <Notice tone={payment.status === 'failed' ? 'error' : 'success'}>Payment {titleCase(payment.status)} · {payment.amount_minor / 100} {payment.currency}</Notice>}
            {selected.submission.verification_status === 'passed' || selected.submission.verification_status === 'failed' ? <Notice tone="success">This submission has a final decision.</Notice> : <form onSubmit={verify} className="review-decision-form">
              <Tabs value={result} onValueChange={(value) => setResult(value as typeof result)} tabs={[{ value: 'passed', label: 'Approve' }, { value: 'review_required', label: 'Needs review' }, { value: 'failed', label: 'Reject' }]} />
              {result === 'failed' && <Field label="Reason"><InputArea value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Tell the human what did not meet the brief." required rows={3} /></Field>}
              <Button type="submit" variant="primary" loading={saving} icon={<Check />}>Record decision</Button>
            </form>}
          </> : <div className="review-result"><span className="review-result__icon"><SealQuestion size={25} /></span><h2>Select a submission</h2><p>Choose an inbox item to inspect its proof and review it.</p></div>}
        </Surface>
      </div>
    </div>
  )
}
