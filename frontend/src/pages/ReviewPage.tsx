import { Check, CircleX, ClipboardPaste, SearchCheck, ShieldQuestion } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { Badge, Button, Field, Input, Notice, PageHeader, Textarea } from '../components/UI'
import { api } from '../lib/api'
import { date, titleCase } from '../lib/utils'
import type { Submission } from '../types'

export function ReviewPage() {
  const [submissionId, setSubmissionId] = useState('')
  const [result, setResult] = useState<'passed' | 'failed' | 'review_required'>('passed')
  const [reason, setReason] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [submission, setSubmission] = useState<Submission | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault(); setLoading(true); setError(''); setSubmission(null)
    try { setSubmission(await api.verifySubmission(submissionId.trim(), result, result === 'failed' ? reason : undefined)) }
    catch (nextError) { setError(nextError instanceof Error ? nextError.message : 'Could not verify submission') }
    finally { setLoading(false) }
  }

  return (
    <div className="page review-page">
      <PageHeader title="Review work" description="Verify a submission using the ID delivered by your webhook or shared by the human." />
      <div className="review-layout">
        <section className="panel review-form-panel">
          <div className="review-form-panel__heading"><span><SearchCheck size={22} /></span><div><h2>Manual verification</h2><p>The API does not currently expose a submission inbox.</p></div></div>
          {error && <Notice tone="error">{error}</Notice>}
          <form onSubmit={submit}>
            <Field label="Submission ID" hint="Find it in submission.created webhook data."><div className="input-with-icon"><ClipboardPaste size={17} /><Input value={submissionId} onChange={(e) => setSubmissionId(e.target.value)} placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" required autoFocus /></div></Field>
            <div className="decision-group"><span className="field__label">Decision</span><div>
              <button type="button" className={result === 'passed' ? 'is-active is-positive' : ''} onClick={() => setResult('passed')}><Check size={18} /><strong>Approve</strong><small>Proof meets the brief</small></button>
              <button type="button" className={result === 'review_required' ? 'is-active is-warning' : ''} onClick={() => setResult('review_required')}><ShieldQuestion size={18} /><strong>Needs review</strong><small>Hold for another check</small></button>
              <button type="button" className={result === 'failed' ? 'is-active is-danger' : ''} onClick={() => setResult('failed')}><CircleX size={18} /><strong>Reject</strong><small>Proof does not qualify</small></button>
            </div></div>
            {result === 'failed' && <Field label="Reason"><Textarea value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Tell the human what did not meet the brief." required rows={3} /></Field>}
            <Button type="submit" loading={loading}>Record decision</Button>
          </form>
        </section>
        <aside className="panel review-result">
          {submission ? <>
            <span className={`status-orb status-orb--${submission.verification_status}`}>{submission.verification_status === 'passed' ? <Check size={24} /> : <ShieldQuestion size={24} />}</span>
            <Badge tone={submission.verification_status === 'passed' ? 'positive' : submission.verification_status === 'failed' ? 'danger' : 'warning'}>{titleCase(submission.verification_status)}</Badge>
            <h2>Decision recorded</h2><p>The submission and claim state were updated through the shared backend state machine.</p>
            <dl><div><dt>Submission</dt><dd>{submission.id}</dd></div><div><dt>Claim status</dt><dd>{titleCase(submission.claim_status)}</dd></div><div><dt>Verified</dt><dd>{date(submission.verified_at, 'Now')}</dd></div></dl>
          </> : <><span className="review-result__icon"><ShieldQuestion size={25} /></span><h2>Ready for a submission</h2><p>Paste an ID, choose a decision, and the result appears here.</p></>}
        </aside>
      </div>
    </div>
  )
}
