import { Badge, Button, Link, LinkButton, Surface, Tabs, Text } from '@cloudflare/kumo'
import { ArrowLeft, ArrowRight, Check, CheckCircle, Copy, PlugsConnected, Terminal, WarningCircle } from '@phosphor-icons/react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useAuth } from '../auth/AuthContext'
import { Logo } from '../components/AppShell'

const mcpUrl = import.meta.env.VITE_MCP_URL || 'http://localhost:8000/mcp'
const apiBase = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000').replace(/\/$/, '')

type CopyTarget = 'setup' | 'install' | 'login' | 'url' | 'prompt' | null

export function McpSetupPage() {
  const { user } = useAuth()
  const [mode, setMode] = useState('cli')
  const [copied, setCopied] = useState<CopyTarget>(null)
  const [oauthReady, setOauthReady] = useState<'checking' | 'ready' | 'unavailable'>('checking')
  const resetTimer = useRef<number | null>(null)

  const commands = useMemo(() => {
    const install = `codex mcp add hah --url ${mcpUrl} --oauth-resource ${mcpUrl}`
    const login = 'codex mcp login hah'
    return { install, login, setup: `${install} && ${login}` }
  }, [])
  const testPrompt = 'Use the HAH MCP server to get my USD global payment allowance and my wallet balance. Do not make any changes.'

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
          {oauthReady === 'ready' ? <Badge variant="success"><CheckCircle /> OAuth server ready</Badge> : oauthReady === 'unavailable' ? <Badge variant="error"><WarningCircle /> Server unavailable</Badge> : <Badge variant="neutral">Checking OAuth…</Badge>}
          {user ? <LinkButton href="/app/integrations" variant="secondary">Back to integrations</LinkButton> : <LinkButton href="/login?next=/connect" variant="secondary">Sign in</LinkButton>}
        </div>
      </header>

      <section className="mcp-setup-hero">
        <div>
          <Badge variant="purple"><PlugsConnected /> OAuth 2.1 + MCP</Badge>
          <Text variant="heading1" as="h1">Connect HAH to Codex in two minutes.</Text>
          <Text variant="secondary" size="lg">Add one Streamable HTTP server, sign in with your HAH account, and let your agent create and review human work.</Text>
        </div>
        <Surface className="mcp-setup-summary rounded-lg border border-kumo-hairline p-6">
          <span>1</span><div><strong>Add HAH</strong><small>Copy one command or use Codex settings.</small></div>
          <span>2</span><div><strong>Approve OAuth</strong><small>Use your existing HAH email and password.</small></div>
          <span>3</span><div><strong>Test a tool</strong><small>Start with a safe, read-only prompt.</small></div>
        </Surface>
      </section>

      <section className="mcp-setup-content">
        <Surface as="section" className="mcp-account-card rounded-lg border border-kumo-hairline p-6">
          <div className="mcp-step-number">1</div>
          <div><h2>Use a creator account</h2><p>OAuth links MCP actions to the exact same HAH user you use in the dashboard.</p></div>
          {user ? <Badge variant="success">Signed in as {user.email}</Badge> : <div className="mcp-account-actions"><LinkButton href="/signup?role=brand&next=/connect" variant="primary">Create creator account</LinkButton><LinkButton href="/login?next=/connect" variant="secondary">I already have one</LinkButton></div>}
        </Surface>

        <Surface as="section" className="mcp-connect-card rounded-lg border border-kumo-hairline p-6">
          <div className="mcp-card-heading"><div className="mcp-step-number">2</div><div><h2>Add HAH and start OAuth</h2><p>Choose the Codex surface you use. No API key or pasted bearer token is needed.</p></div></div>
          <Tabs value={mode} onValueChange={setMode} tabs={[{ value: 'cli', label: 'Codex CLI' }, { value: 'app', label: 'Codex app / IDE' }]} />
          {mode === 'cli' ? <div className="mcp-cli-guide">
            <div className="mcp-command-block mcp-command-block--primary"><div><span>Recommended: run once</span><code>{commands.setup}</code></div><Button variant="primary" icon={copied === 'setup' ? <Check /> : <Copy />} onClick={() => copy('setup', commands.setup)}>{copied === 'setup' ? 'Copied' : 'Copy setup command'}</Button></div>
            <details><summary>Prefer separate commands?</summary><div className="mcp-command-list"><CopyCommand label="Add server" value={commands.install} copied={copied === 'install'} onCopy={() => copy('install', commands.install)} /><CopyCommand label="Start OAuth" value={commands.login} copied={copied === 'login'} onCopy={() => copy('login', commands.login)} /></div></details>
            <ol className="mcp-instructions"><li>Paste the command into your terminal.</li><li>Your browser opens the HAH consent page.</li><li>Sign in, review the permissions, and select <strong>Allow MCP access</strong>.</li><li>Return to Codex and run <code>/mcp</code> to see HAH.</li></ol>
          </div> : <div className="mcp-app-guide">
            <ol className="mcp-instructions"><li>Open <strong>Settings → MCP servers → Add server</strong>.</li><li>Choose <strong>Streamable HTTP</strong>, name it <code>hah</code>, and paste the URL below.</li><li>Save, restart Codex or the IDE extension, then select <strong>Authenticate</strong>.</li><li>Sign in with HAH and approve the requested permissions.</li></ol>
            <CopyCommand label="HAH MCP URL" value={mcpUrl} copied={copied === 'url'} onCopy={() => copy('url', mcpUrl)} />
          </div>}
        </Surface>

        <Surface as="section" className="mcp-test-card rounded-lg border border-kumo-hairline p-6">
          <div className="mcp-card-heading"><div className="mcp-step-number">3</div><div><h2>Confirm the connection safely</h2><p>Paste this read-only prompt into a new Codex task.</p></div></div>
          <div className="mcp-test-prompt"><Terminal size={22} /><code>{testPrompt}</code><Button variant="secondary" icon={copied === 'prompt' ? <Check /> : <Copy />} onClick={() => copy('prompt', testPrompt)}>{copied === 'prompt' ? 'Copied' : 'Copy test prompt'}</Button></div>
          <p className="mcp-success-note"><CheckCircle /> Success means Codex returns the HAH allowance and wallet response without asking for an API token.</p>
        </Surface>

        <Surface as="section" className="mcp-permissions-card rounded-lg border border-kumo-hairline p-6">
          <h2>What OAuth can allow</h2>
          <div className="mcp-scope-grid">
            <span><strong>Create</strong><small>Draft campaign tasks</small></span>
            <span><strong>Review</strong><small>Read proofs and verify submissions</small></span>
            <span><strong>Payments</strong><small>Read sandbox status and wallet credits</small></span>
          </div>
          <p>HAH login tokens and API keys are never accepted by MCP. Codex stores and refreshes the OAuth credentials for the MCP connection.</p>
        </Surface>
      </section>

      <footer className="mcp-setup-footer"><Link href="/" variant="plain"><ArrowLeft /> Back to HAH</Link><Link href={`${apiBase}/.well-known/oauth-authorization-server`} target="_blank" rel="noreferrer">View OAuth metadata <ArrowRight /></Link></footer>
    </main>
  )
}

function CopyCommand({ label, value, copied, onCopy }: { label: string; value: string; copied: boolean; onCopy: () => void }) {
  return <div className="mcp-command-block"><div><span>{label}</span><code>{value}</code></div><Button variant="secondary" shape="square" icon={copied ? <Check /> : <Copy />} onClick={onCopy} aria-label={`Copy ${label}`} /></div>
}
