import { ArrowRight, CheckCircle2, ClipboardCheck, Clock3, ExternalLink, FileCheck2, Send, UploadCloud } from 'lucide-react'
import { useEffect, useState, type FormEvent } from 'react'
import { useAuth } from '../auth/AuthContext'
import { Badge, Button, EmptyState, Field, Input, Modal, Notice, PageHeader } from '../components/UI'
import { api } from '../lib/api'
import { date, localClaims, money, titleCase } from '../lib/utils'
import type { LocalClaim, ProofType, SubmissionProof } from '../types'

export function WorkPage() {
  const { user } = useAuth()
  const [claims, setClaims] = useState<LocalClaim[]>([])
  const [selected, setSelected] = useState<LocalClaim | null>(null)
  const [proofs, setProofs] = useState<Record<ProofType, string>>({ url: '', screenshot: '', image: '' })
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => { if (user) setClaims(localClaims.list(user.id)) }, [user])
  if (!user) return null

  const openProof = (claim: LocalClaim) => {
    setSelected(claim); setError(''); setProofs({ url: '', screenshot: '', image: '' })
  }
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!selected) return
    setLoading(true); setError('')
    try {
      const required = selected.bounty?.proof_requirements ?? ['url']
      const payload: SubmissionProof[] = required.map((proof) => proof === 'url'
        ? { proof_type: proof, url: proofs[proof] }
        : { proof_type: proof, storage_key: proofs[proof] })
      const submission = await api.submitProof(selected.id, payload)
      localClaims.update(user.id, selected.id, { submission, status: submission.claim_status })
      setClaims(localClaims.list(user.id)); setSelected({ ...selected, submission, status: submission.claim_status })
    } catch (nextError) { setError(nextError instanceof Error ? nextError.message : 'Could not submit proof') }
    finally { setLoading(false) }
  }

  return (
    <div className="page">
      <PageHeader title="My work" description="Claims made in this browser appear here until the API exposes work history." />
      {claims.length ? (
        <div className="work-list">
          {claims.map((claim) => (
            <article className="work-row" key={claim.id}>
              <span className={`platform-icon platform-icon--${claim.platform}`}>{claim.platform === 'reddit' ? 'r/' : 'in'}</span>
              <div className="work-row__body"><div><Badge tone={claim.submission ? (claim.submission.verification_status === 'passed' ? 'positive' : claim.submission.verification_status === 'failed' ? 'danger' : 'warning') : 'accent'}>{claim.submission ? titleCase(claim.submission.verification_status) : titleCase(claim.status)}</Badge><small>Claimed {date(claim.claimed_at, '')}</small></div><h2>{claim.bounty?.bounty_title ?? `Bounty ${claim.bounty_id.slice(0, 8)}`}</h2><p>{claim.bounty?.task_title ?? 'Claimed work'}</p></div>
              <div className="work-row__reward"><strong>{money(claim.reward_minor, claim.currency)}</strong><span>fixed reward</span></div>
              {claim.submission ? <Button variant="secondary" onClick={() => setSelected(claim)}><FileCheck2 size={16} /> View proof</Button> : <Button onClick={() => openProof(claim)}><UploadCloud size={16} /> Submit proof</Button>}
            </article>
          ))}
        </div>
      ) : <EmptyState icon={<ClipboardCheck size={23} />} title="No active work yet" body="Claim a bounty from Find work. It will appear here with the exact proof requirements." action={<a href="/app/marketplace"><Button>Find work <ArrowRight size={16} /></Button></a>} />}
      <Modal open={Boolean(selected)} onClose={() => setSelected(null)} title={selected?.submission ? 'Submission details' : 'Submit your work'}>
        {selected && (selected.submission ? (
          <div className="submission-detail">
            <div className="submission-detail__status"><span className={`status-orb status-orb--${selected.submission.verification_status}`}>{selected.submission.verification_status === 'passed' ? <CheckCircle2 size={24} /> : <Clock3 size={24} />}</span><div><h3>{titleCase(selected.submission.verification_status)}</h3><p>Revision {selected.submission.revision} · Submitted {date(selected.submission.submitted_at, '')}</p></div></div>
            {selected.submission.failure_reason && <Notice tone="error">{selected.submission.failure_reason}</Notice>}
            <div className="proof-list">{selected.submission.proofs.map((proof) => <div key={proof.id ?? proof.proof_type}><span>{titleCase(proof.proof_type)}</span>{proof.url ? <a href={proof.url} target="_blank" rel="noreferrer">Open proof <ExternalLink size={14} /></a> : <code>{proof.storage_key}</code>}</div>)}</div>
            <div className="id-box"><span>Submission ID</span><code>{selected.submission.id}</code><small>Share this with the creator if they review manually.</small></div>
          </div>
        ) : (
          <form onSubmit={submit} className="modal-form">
            {error && <Notice tone="error">{error}</Notice>}
            <div className="proof-brief"><span className={`platform-icon platform-icon--${selected.platform}`}>{selected.platform === 'reddit' ? 'r/' : 'in'}</span><div><h3>{selected.bounty?.bounty_title ?? 'Claimed bounty'}</h3><p>{selected.bounty?.instructions}</p></div></div>
            {(selected.bounty?.proof_requirements ?? ['url']).map((proof) => proof === 'url' ? (
              <Field key={proof} label="Public post or comment URL"><Input type="url" value={proofs.url} onChange={(e) => setProofs((value) => ({ ...value, url: e.target.value }))} placeholder="https://…" required /></Field>
            ) : (
              <Field key={proof} label={`${titleCase(proof)} storage key`} hint="Use the key returned by your configured file storage uploader."><Input value={proofs[proof]} onChange={(e) => setProofs((value) => ({ ...value, [proof]: e.target.value }))} placeholder={`proof/${selected.id}/${proof}.png`} required /></Field>
            ))}
            <Notice>File upload is not exposed by the current API. Image proofs require an existing object-storage key.</Notice>
            <div className="modal-actions"><Button type="button" variant="ghost" onClick={() => setSelected(null)}>Cancel</Button><Button type="submit" loading={loading}><Send size={16} /> Submit proof</Button></div>
          </form>
        ))}
      </Modal>
    </div>
  )
}
