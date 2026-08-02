import { Button, Empty, Field, Input, Link, LinkButton, Loader, Surface } from '@cloudflare/kumo'
import { ArrowRight, ArrowSquareOut, CheckCircle, ClipboardText, Clock, FileText, PaperPlaneTilt, UploadSimple } from '@phosphor-icons/react'
import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useAuth } from '../auth/AuthContext'
import { Modal, Notice, PageHeader, StatusBadge } from '../components/UI'
import { api } from '../lib/api'
import { date, money, titleCase } from '../lib/utils'
import type { Payment, SubmissionProofInput, Wallet, WorkClaim } from '../types'

type ImageProofType = 'screenshot' | 'image'

export function WorkPage() {
  const { user } = useAuth()
  const [claims, setClaims] = useState<WorkClaim[]>([])
  const [wallet, setWallet] = useState<Wallet | null>(null)
  const [payments, setPayments] = useState<Record<string, Payment>>({})
  const [selected, setSelected] = useState<WorkClaim | null>(null)
  const [urlProof, setUrlProof] = useState('')
  const [files, setFiles] = useState<Record<ImageProofType, File | null>>({ screenshot: null, image: null })
  const [proofImages, setProofImages] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    if (!user) return
    setLoading(true)
    setError('')
    Promise.all([api.listClaims(user.id), api.getWallet()])
      .then(async ([items, nextWallet]) => {
        setClaims(items)
        setWallet(nextWallet)
        const paymentPairs = await Promise.all(items.filter((item) => item.submission).map(async (item) => {
          try { return [item.submission!.id, await api.getSubmissionPayment(item.submission!.id)] as const }
          catch { return null }
        }))
        setPayments(Object.fromEntries(paymentPairs.filter((item): item is readonly [string, Payment] => item !== null)))
        setSelected((current) => current ? items.find((item) => item.id === current.id) ?? null : null)
      })
      .catch((nextError) => setError(nextError instanceof Error ? nextError.message : 'Could not load work'))
      .finally(() => setLoading(false))
  }, [user])

  useEffect(() => { load() }, [load])
  if (!user) return null

  const openProof = (claim: WorkClaim) => {
    setSelected(claim)
    setError('')
    setUrlProof('')
    setFiles({ screenshot: null, image: null })
  }

  const loadImage = async (proofId: string, contentUrl: string) => {
    try {
      const blob = await api.getProofContent(contentUrl)
      const objectUrl = URL.createObjectURL(blob)
      setProofImages((current) => {
        if (current[proofId]) URL.revokeObjectURL(current[proofId])
        return { ...current, [proofId]: objectUrl }
      })
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Could not load image proof')
    }
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!selected) return
    setSubmitting(true)
    setError('')
    try {
      const proofs: SubmissionProofInput[] = []
      for (const proofType of selected.proof_requirements) {
        if (proofType === 'url') {
          proofs.push({ proof_type: 'url', url: urlProof })
          continue
        }
        const file = files[proofType]
        if (!file) throw new Error(`Choose a ${proofType} image before submitting.`)
        const upload = await api.uploadProof(selected.id, proofType, file)
        proofs.push({ proof_type: proofType, upload_id: upload.upload_id })
      }
      const submission = await api.submitProof(selected.id, proofs)
      const updated = { ...selected, submission, status: submission.claim_status }
      setClaims((items) => items.map((item) => item.id === selected.id ? updated : item))
      setSelected(updated)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Could not submit proof')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="page">
      <PageHeader title="My work" description="Your claimed work and latest review status, synced from the API." action={<Button variant="secondary" onClick={load}>Refresh</Button>} />
      {error && !selected && <Notice tone="error">{error}</Notice>}
      {wallet && <Surface as="section" className="wallet-panel rounded-lg border border-kumo-hairline p-5"><div><h2>Sandbox wallet</h2><p>Prava-funded hackathon credits. These balances are not redeemable.</p></div><div>{wallet.balances.length ? wallet.balances.map((balance) => <strong key={balance.currency}>{money(balance.balance_minor, balance.currency)}</strong>) : <strong>{money(0)}</strong>}</div></Surface>}
      {loading ? <div className="loading-state"><Loader size="lg" /></div> : claims.length ? (
        <div className="work-list">
          {claims.map((claim) => (
            <Surface as="article" className="work-row rounded-lg border border-kumo-hairline p-5" key={claim.id}>
              <span className={`platform-icon platform-icon--${claim.platform}`}>{claim.platform === 'reddit' ? 'r/' : 'in'}</span>
              <div className="work-row__body"><div><StatusBadge tone={claim.submission ? (claim.submission.verification_status === 'passed' ? 'positive' : claim.submission.verification_status === 'failed' ? 'danger' : 'warning') : 'accent'}>{claim.submission ? titleCase(claim.submission.verification_status) : titleCase(claim.status)}</StatusBadge><small>Claimed {date(claim.claimed_at, '')}</small></div><h2>{claim.bounty_title}</h2><p>{claim.task_title}</p></div>
              <div className="work-row__reward"><strong>{money(claim.reward_minor, claim.currency)}</strong><span>{claim.submission && payments[claim.submission.id] ? `Payment ${titleCase(payments[claim.submission.id].status)}` : 'fixed reward'}</span></div>
              {claim.submission ? <Button variant="secondary" icon={<FileText />} onClick={() => setSelected(claim)}>View proof</Button> : <Button variant="primary" icon={<UploadSimple />} onClick={() => openProof(claim)}>Submit proof</Button>}
            </Surface>
          ))}
        </div>
      ) : <Empty icon={<ClipboardText size={40} />} title="No active work yet" description="Claim a bounty from Find work. It will stay synced here across browsers and devices." contents={<LinkButton href="/app/marketplace" variant="primary" icon={<ArrowRight />}>Find work</LinkButton>} />}
      <Modal open={Boolean(selected)} onClose={() => { setSelected(null); setError('') }} title={selected?.submission ? 'Submission details' : 'Submit your work'}>
        {selected && (selected.submission ? (
          <div className="submission-detail">
            {error && <Notice tone="error">{error}</Notice>}
            <div className="submission-detail__status"><span className={`status-orb status-orb--${selected.submission.verification_status}`}>{selected.submission.verification_status === 'passed' ? <CheckCircle size={24} /> : <Clock size={24} />}</span><div><h3>{titleCase(selected.submission.verification_status)}</h3><p>Revision {selected.submission.revision} · Submitted {date(selected.submission.submitted_at, '')}</p></div></div>
            {selected.submission.failure_reason && <Notice tone="error">{selected.submission.failure_reason}</Notice>}
            <div className="proof-list">{selected.submission.proofs.map((proof) => <div key={proof.id}><span>{titleCase(proof.proof_type)}</span>{proof.url ? <Link href={proof.url} target="_blank" rel="noreferrer">Open proof <ArrowSquareOut size={14} /></Link> : proof.content_url ? <><Button size="sm" variant="secondary" onClick={() => loadImage(proof.id, proof.content_url!)}>Load image</Button>{proofImages[proof.id] && <img className="proof-preview" src={proofImages[proof.id]} alt={`${titleCase(proof.proof_type)} proof`} />}</> : <code>{proof.storage_key}</code>}</div>)}</div>
            <div className="id-box"><span>Submission ID</span><code>{selected.submission.id}</code></div>
          </div>
        ) : (
          <form onSubmit={submit} className="modal-form">
            {error && <Notice tone="error">{error}</Notice>}
            <div className="proof-brief"><span className={`platform-icon platform-icon--${selected.platform}`}>{selected.platform === 'reddit' ? 'r/' : 'in'}</span><div><h3>{selected.bounty_title}</h3><p>{selected.instructions}</p></div></div>
            {selected.proof_requirements.map((proof) => proof === 'url' ? (
              <Field key={proof} label="Public post or comment URL"><Input type="url" value={urlProof} onChange={(event) => setUrlProof(event.target.value)} placeholder="https://…" required /></Field>
            ) : (
              <Field key={proof} label={`${titleCase(proof)} image`} description="PNG, JPEG, GIF, or WebP up to 5 MiB."><input className="file-input" type="file" accept="image/png,image/jpeg,image/gif,image/webp" onChange={(event) => setFiles((value) => ({ ...value, [proof]: event.target.files?.[0] ?? null }))} required /></Field>
            ))}
            <div className="modal-actions"><Button type="button" variant="ghost" onClick={() => setSelected(null)}>Cancel</Button><Button type="submit" variant="primary" loading={submitting} icon={<PaperPlaneTilt />}>Upload & submit</Button></div>
          </form>
        ))}
      </Modal>
    </div>
  )
}
