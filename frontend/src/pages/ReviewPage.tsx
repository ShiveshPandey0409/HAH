import { Button, Field, Input, InputArea, Surface, Tabs } from '@cloudflare/kumo'
import { Check, ClipboardText, MagnifyingGlass, SealQuestion } from '@phosphor-icons/react'
import { useState, type FormEvent } from 'react'
import { Notice, PageHeader, StatusBadge } from '../components/UI'
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
        <Surface as="section" className="panel review-form-panel rounded-lg border border-kumo-hairline p-6">
          <div className="review-form-panel__heading"><span><MagnifyingGlass size={22} /></span><div><h2>Manual verification</h2><p>The API does not currently expose a submission inbox.</p></div></div>
          {error && <Notice tone="error">{error}</Notice>}
          <form onSubmit={submit}>
            <Field label="Submission ID" description="Find it in submission.created webhook data."><Input value={submissionId} onChange={(e) => setSubmissionId(e.target.value)} placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" required autoFocus /></Field>
            <Tabs value={result} onValueChange={(value) => setResult(value as typeof result)} tabs={[{ value: 'passed', label: 'Approve' }, { value: 'review_required', label: 'Needs review' }, { value: 'failed', label: 'Reject' }]} />
            {result === 'failed' && <Field label="Reason"><InputArea value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Tell the human what did not meet the brief." required rows={3} /></Field>}
            <Button type="submit" variant="primary" loading={loading} icon={<ClipboardText />}>Record decision</Button>
          </form>
        </Surface>
        <Surface as="aside" className="panel review-result rounded-lg border border-kumo-hairline p-6">
          {submission ? <>
            <span className={`status-orb status-orb--${submission.verification_status}`}>{submission.verification_status === 'passed' ? <Check size={24} /> : <SealQuestion size={24} />}</span>
            <StatusBadge tone={submission.verification_status === 'passed' ? 'positive' : submission.verification_status === 'failed' ? 'danger' : 'warning'}>{titleCase(submission.verification_status)}</StatusBadge>
            <h2>Decision recorded</h2><p>The submission and claim state were updated through the shared backend state machine.</p>
            <dl><div><dt>Submission</dt><dd>{submission.id}</dd></div><div><dt>Claim status</dt><dd>{titleCase(submission.claim_status)}</dd></div><div><dt>Verified</dt><dd>{date(submission.verified_at, 'Now')}</dd></div></dl>
          </> : <><span className="review-result__icon"><SealQuestion size={25} /></span><h2>Ready for a submission</h2><p>Paste an ID, choose a decision, and the result appears here.</p></>}
        </Surface>
      </div>
    </div>
  )
}
