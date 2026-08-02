import { Badge, Button, Checkbox, ClipboardText, Field, Input, Link, Surface } from '@cloudflare/kumo'
import { ArrowRight, ArrowSquareOut, ArrowsClockwise, Broadcast, PlugsConnected, ShieldCheck } from '@phosphor-icons/react'
import { useEffect, useState, type FormEvent } from 'react'
import { useAuth } from '../auth/AuthContext'
import { Notice, PageHeader } from '../components/UI'
import { api, ApiError } from '../lib/api'
import { titleCase } from '../lib/utils'
import type { WebhookEndpoint, WebhookEvent } from '../types'

const events: { value: WebhookEvent; label: string; body: string }[] = [
  { value: 'submission.created', label: 'Submission created', body: 'A human sends proof for claimed work.' },
  { value: 'verification.completed', label: 'Verification completed', body: 'A submission is approved, rejected, or held.' },
  { value: 'payment.succeeded', label: 'Payment succeeded', body: 'An approved reward reaches the human wallet.' },
  { value: 'payment.failed', label: 'Payment failed', body: 'An automatic reward needs attention or a retry.' },
  { value: 'mcp_request.completed', label: 'MCP request completed', body: 'An agent task or verification request finishes.' },
]
const apiDocsUrl = `${(import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000').replace(/\/$/, '')}/docs`

export function IntegrationsPage() {
  const { user } = useAuth()
  const [endpoint, setEndpoint] = useState<WebhookEndpoint | null>(null)
  const [url, setUrl] = useState('')
  const [subscriptions, setSubscriptions] = useState<WebhookEvent[]>(events.map((event) => event.value))
  const [secret, setSecret] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

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
  return (
    <div className="page integrations-page">
      <PageHeader title="Integrations" description="Send signed task events to your backend or agent." />
      <Surface as="section" className="mcp-integration-callout rounded-lg border border-kumo-hairline p-6">
        <span><PlugsConnected size={24} /></span><div><h2>Connect HAH to your AI app</h2><p>Run one command on each computer, then authenticate with the same HAH account.</p></div><Link href="/connect">Connect MCP <ArrowRight /></Link>
      </Surface>
      <div className="integration-layout">
        <Surface render={<form className="panel integration-form" onSubmit={save} />} className="rounded-lg border border-kumo-hairline p-6">
          <div className="integration-form__heading"><span><Broadcast size={22} /></span><div><h2>Webhook endpoint</h2><p>One HTTPS destination per creator account.</p></div>{endpoint && <Badge variant="success">{titleCase(endpoint.status)}</Badge>}</div>
          {error && <Notice tone="error">{error}</Notice>}
          <Field label="Destination URL"><Input type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://api.yourapp.com/hah/events" required /></Field>
          <Checkbox.Group legend="Events" value={subscriptions} allValues={events.map((event) => event.value)}>
            {events.map((event) => <Checkbox key={event.value} checked={subscriptions.includes(event.value)} onCheckedChange={() => toggle(event.value)} label={<span><strong>{event.label}</strong><br /><small>{event.body}</small></span>} />)}
          </Checkbox.Group>
          <Button type="submit" variant="primary" loading={loading} icon={endpoint ? <ArrowsClockwise /> : undefined}>{endpoint ? 'Rotate secret & save' : 'Create endpoint'}</Button>
        </Surface>
        <aside className="integration-side">
          {secret ? <Surface as="section" className="secret-panel rounded-lg border border-kumo-hairline p-5"><ShieldCheck size={24} /><h2>Save this signing secret now</h2><p>It is shown once. Rotating the endpoint invalidates the previous secret.</p><ClipboardText text={secret} /></Surface> : <Surface as="section" className="panel webhook-guide rounded-lg border border-kumo-hairline p-5"><ShieldCheck size={22} /><h3>Signed delivery</h3><p>HAH retries failed deliveries and signs canonical payload bytes.</p><Link href={apiDocsUrl} target="_blank" rel="noreferrer">Open API docs <ArrowSquareOut size={14} /></Link></Surface>}
          {endpoint && <Surface as="section" className="panel endpoint-facts rounded-lg border border-kumo-hairline p-5"><h3>Delivery policy</h3><dl><div><dt>Success</dt><dd>{endpoint.delivery.success_statuses}</dd></div><div><dt>Max attempts</dt><dd>{endpoint.delivery.max_attempts}</dd></div><div><dt>Timeout</dt><dd>{endpoint.delivery.timeout_seconds}s</dd></div></dl></Surface>}
        </aside>
      </div>
    </div>
  )
}
