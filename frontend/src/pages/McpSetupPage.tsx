import { Badge, Button, Link, LinkButton, Surface, Text } from '@cloudflare/kumo'
import { ArrowLeft, Check, CheckCircle, Copy, PlugsConnected, Terminal, WarningCircle } from '@phosphor-icons/react'
import { useEffect, useRef, useState } from 'react'
import { useAuth } from '../auth/AuthContext'
import { Logo } from '../components/AppShell'

const mcpUrl = import.meta.env.VITE_MCP_URL || 'http://localhost:8000/mcp'
const apiBase = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000').replace(/\/$/, '')
const installCommand = `npx add-mcp ${mcpUrl} -g -n hah`
const testPrompt = 'Use HAH to show my wallet balance. Do not make any changes.'

type CopyTarget = 'install' | 'url' | 'prompt' | null

export function McpSetupPage() {
  const { user } = useAuth()
  const [copied, setCopied] = useState<CopyTarget>(null)
  const [oauthReady, setOauthReady] = useState<'checking' | 'ready' | 'unavailable'>('checking')
  const resetTimer = useRef<number | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    fetch(`${apiBase}/.well-known/oauth-protected-resource/mcp`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error('OAuth metadata is unavailable')
        const metadata = await response.json() as { resource?: string; scopes_supported?: string[] }
        if (metadata.resource !== mcpUrl || !metadata.scopes_supported?.includes('mcp:access')) throw new Error('OAuth metadata is incomplete')
        setOauthReady('ready')
      })
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === 'AbortError')) setOauthReady('unavailable')
      })
    return () => controller.abort()
  }, [])

  useEffect(() => () => {
    if (resetTimer.current) window.clearTimeout(resetTimer.current)
  }, [])

  const copy = async (target: Exclude<CopyTarget, null>, value: string) => {
    if (resetTimer.current) window.clearTimeout(resetTimer.current)
    try {
      await navigator.clipboard.writeText(value)
      setCopied(target)
      resetTimer.current = window.setTimeout(() => setCopied(null), 2200)
    } catch {
      setCopied(null)
    }
  }

  return (
    <main className="mcp-setup-page">
      <header className="mcp-setup-nav">
        <Link href="/" variant="plain"><Logo /></Link>
        <div>
          {oauthReady === 'ready' ? <Badge variant="success"><CheckCircle /> Ready</Badge> : oauthReady === 'unavailable' ? <Badge variant="error"><WarningCircle /> Unavailable</Badge> : <Badge variant="neutral">Checking…</Badge>}
          {user ? <LinkButton href="/app/integrations" variant="secondary">Back to integrations</LinkButton> : <LinkButton href="/login?next=/connect" variant="secondary">Sign in</LinkButton>}
        </div>
      </header>

      <section className="mcp-simple-hero">
        <div className="mcp-simple-heading">
          <Badge variant="purple"><PlugsConnected /> MCP</Badge>
          <Text variant="heading1" as="h1">Connect HAH to your AI app.</Text>
          <Text variant="secondary" size="lg">Run it once on each computer. Use the same HAH account everywhere.</Text>
        </div>

        <Surface className="mcp-install-card rounded-lg border border-kumo-hairline p-6">
          <span>Run once</span>
          <code>{installCommand}</code>
          <Button variant="primary" icon={copied === 'install' ? <Check /> : <Copy />} onClick={() => copy('install', installCommand)}>{copied === 'install' ? 'Copied' : 'Copy'}</Button>
          <small>Installs globally as “hah” for Claude, Cursor, Codex, VS Code, Windsurf, and more.</small>
        </Surface>
      </section>

      <section className="mcp-simple-grid">
        <Surface as="section" className="mcp-next-card rounded-lg border border-kumo-hairline p-6">
          <h2>Then</h2>
          <ol className="mcp-next-list">
            <li><span>1</span><div><strong>Choose your app</strong><small>The installer detects supported MCP clients and adds HAH globally.</small></div></li>
            <li><span>2</span><div><strong>Restart your app</strong><small>Open its MCP list and choose Authenticate for “hah” if prompted.</small></div></li>
            <li><span>3</span><div><strong>Sign in on the same computer</strong><small>Use your existing HAH email. Keep the AI app open until the browser returns to its local callback.</small></div></li>
            <li><span>4</span><div><strong>Approve once when needed</strong><small>The first paid task may ask you to approve an allowance. Later task rewards are automatic until it runs out.</small></div></li>
          </ol>
          <p>Every computer gets its own secure connection. No API key or token to copy.</p>
        </Surface>

        <Surface as="section" className="mcp-test-card rounded-lg border border-kumo-hairline p-6">
          <div className="mcp-card-heading"><Terminal size={22} /><div><h2>Test it</h2><p>Ask your app:</p></div></div>
          <div className="mcp-test-prompt"><code>{testPrompt}</code><Button variant="secondary" icon={copied === 'prompt' ? <Check /> : <Copy />} onClick={() => copy('prompt', testPrompt)}>{copied === 'prompt' ? 'Copied' : 'Copy'}</Button></div>
        </Surface>
      </section>

      <Surface as="section" className="mcp-direct-card rounded-lg border border-kumo-hairline p-5">
        <div><h2>Adding it manually?</h2><p>Paste this as a Streamable HTTP MCP URL.</p></div>
        <CopyCommand value={mcpUrl} copied={copied === 'url'} onCopy={() => copy('url', mcpUrl)} />
      </Surface>

      <footer className="mcp-setup-footer"><Link href="/" variant="plain"><ArrowLeft /> Back to HAH</Link></footer>
    </main>
  )
}

function CopyCommand({ value, copied, onCopy }: { value: string; copied: boolean; onCopy: () => void }) {
  return <div className="mcp-url-block"><code>{value}</code><Button variant="secondary" shape="square" icon={copied ? <Check /> : <Copy />} onClick={onCopy} aria-label="Copy MCP URL" /></div>
}
