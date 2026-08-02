import { Check, Copy, ExternalLink, RadioTower, RotateCw, ShieldCheck } from 'lucide-react'
import { useEffect, useState, type FormEvent } from 'react'
import { useAuth } from '../auth/AuthContext'
import { Badge, Button, Field, Input, Notice, PageHeader } from '../components/UI'
import { api, ApiError } from '../lib/api'
import { titleCase } from '../lib/utils'
import type { WebhookEndpoint, WebhookEvent } from '../types'

const events: { value: WebhookEvent; label: string; body: string }[] = [
  { value: 'submission.created', label: 'Submission created', body: 'A human sends proof for claimed work.' },
  { value: 'verification.completed', label: 'Verification completed', body: 'A submission is approved, rejected, or held.' },
  { value: 'mcp_request.completed', label: 'MCP request completed', body: 'An agent task or verification request finishes.' },
]

export function IntegrationsPage() {
  const { user } = useAuth()
  const [endpoint, setEndpoint] = useState<WebhookEndpoint | null>(null)
  const [url, setUrl] = useState('')
  const [subscriptions, setSubscriptions] = useState<WebhookEvent[]>(['submission.created', 'verification.completed'])
  const [secret, setSecret] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!user) return
    api.getWebhook(user.id).then((value) => { setEndpoint(value); setUrl(value.url); setSubscriptions(value.subscribed_events) }).catch((nextError) => { if (!(nextError instanceof ApiError && nextError.status === 404)) setError(nextError.message) })
  }, [user])
  if (!user) return null

  const toggle = (event: WebhookEvent) => setSubscriptions((items) => items.includes(event) ? items.filter((item) => item !== event) : [...items, event])
  const save = async (event: FormEvent) => {
    event.preventDefault(); setLoading(true); setError(''); setSecret('')
    try { const value = await api.putWebhook(user.id, url, subscriptions); setEndpoint(value); setSecret(value.signing_secret ?? '') }
    catch (nextError) { setError(nextError instanceof Error ? nextError.message : 'Could not save webhook') }
    finally { setLoading(false) }
  }
  const copySecret = async () => { await navigator.clipboard.writeText(secret); setCopied(true); window.setTimeout(() => setCopied(false), 1500) }

  return (
    <div className="page integrations-page">
      <PageHeader title="Integrations" description="Send signed task events to your backend or agent." />
      <div className="integration-layout">
        <form className="panel integration-form" onSubmit={save}>
          <div className="integration-form__heading"><span><RadioTower size={22} /></span><div><h2>Webhook endpoint</h2><p>One HTTPS destination per creator account.</p></div>{endpoint && <Badge tone="positive">{titleCase(endpoint.status)}</Badge>}</div>
          {error && <Notice tone="error">{error}</Notice>}
          <Field label="Destination URL"><Input type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://api.yourapp.com/hah/events" required /></Field>
          <div className="field"><span className="field__label">Events</span><div className="event-list">{events.map((event) => <label key={event.value} className={subscriptions.includes(event.value) ? 'event-option is-selected' : 'event-option'}><input type="checkbox" checked={subscriptions.includes(event.value)} onChange={() => toggle(event.value)} /><span className="event-option__check">{subscriptions.includes(event.value) && <Check size={14} />}</span><span><strong>{event.label}</strong><small>{event.body}</small></span></label>)}</div></div>
          <Button type="submit" loading={loading}>{endpoint ? <><RotateCw size={16} /> Rotate secret & save</> : 'Create endpoint'}</Button>
        </form>
        <aside className="integration-side">
          {secret ? <section className="secret-panel"><ShieldCheck size={24} /><h2>Save this signing secret now</h2><p>It is shown once. Rotating the endpoint invalidates the previous secret.</p><div><code>{secret}</code><button onClick={copySecret} aria-label="Copy signing secret">{copied ? <Check size={17} /> : <Copy size={17} />}</button></div></section> : <section className="panel webhook-guide"><ShieldCheck size={22} /><h3>Signed delivery</h3><p>HAH retries failed deliveries and signs canonical payload bytes.</p><a href="http://localhost:8000/docs" target="_blank" rel="noreferrer">Open API docs <ExternalLink size={14} /></a></section>}
          {endpoint && <section className="panel endpoint-facts"><h3>Delivery policy</h3><dl><div><dt>Success</dt><dd>{endpoint.delivery.success_statuses}</dd></div><div><dt>Max attempts</dt><dd>{endpoint.delivery.max_attempts}</dd></div><div><dt>Timeout</dt><dd>{endpoint.delivery.timeout_seconds}s</dd></div></dl></section>}
        </aside>
      </div>
    </div>
  )
}
