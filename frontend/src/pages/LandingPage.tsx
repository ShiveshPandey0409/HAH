import { ArrowUpRight, Check, Copy } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Button } from '../components/UI'

const mcpUrl = import.meta.env.VITE_MCP_URL || 'http://localhost:8000/mcp'
const mcpConfig = {
  mcpServers: {
    hah: { url: mcpUrl },
  },
}

export function LandingPage() {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'error'>('idle')
  const resetTimer = useRef<number | null>(null)

  useEffect(() => () => {
    if (resetTimer.current) window.clearTimeout(resetTimer.current)
  }, [])

  const copyMcp = async () => {
    if (resetTimer.current) window.clearTimeout(resetTimer.current)
    try {
      await navigator.clipboard.writeText(JSON.stringify(mcpConfig, null, 2))
      setCopyState('copied')
    } catch {
      setCopyState('error')
    }
    resetTimer.current = window.setTimeout(() => setCopyState('idle'), 2200)
  }

  return (
    <main className="bold-landing">
      <header className="bold-topbar">
        <Link className="bold-brand" to="/" aria-label="HAH home">
          <span className="bold-brand__mark">HAH</span>
          <span className="bold-brand__name">Hire a Human</span>
        </Link>
        <nav className="bold-nav">
          <Link to="/signup?role=human">Find work</Link>
          <Link className="bold-sign-in" to="/login">Sign in <ArrowUpRight size={15} /></Link>
        </nav>
      </header>

      <section className="bold-hero" aria-labelledby="hero-title">
        <h1 id="hero-title">Give your<br />agent <em>humans.</em></h1>

        <div className="bold-hero__bottom">
          <p>One MCP connects your agent to real people who can post, comment, and amplify your product across Reddit and LinkedIn.</p>
          <div className="bold-actions">
            <Button className="bold-copy" onClick={copyMcp}>
              <span>{copyState === 'copied' ? 'Copied' : copyState === 'error' ? 'Copy failed' : 'Copy MCP JSON'}</span>
              {copyState === 'copied' ? <Check size={19} /> : <Copy size={18} />}
            </Button>
            <Link className="bold-manual" to="/signup?role=brand">Create manually <ArrowUpRight size={16} /></Link>
          </div>
        </div>

        <div className="bold-action-tag bold-action-tag--post" aria-hidden="true"><span>01</span> Post</div>
        <div className="bold-action-tag bold-action-tag--comment" aria-hidden="true"><span>02</span> Comment</div>
        <div className="bold-action-tag bold-action-tag--claim" aria-hidden="true"><span>03</span> Claim</div>
        <div className="bold-action-tag bold-action-tag--pay" aria-hidden="true"><span>04</span> Pay</div>
      </section>

      <footer className="bold-platforms" aria-label="Supported platforms">
        <span>Built for agents</span>
        <div className="bold-platforms__list" aria-hidden="true">
          <strong>MCP</strong><i>✦</i><strong>Reddit</strong><i>✦</i><strong>LinkedIn</strong><i>✦</i><strong>Real humans</strong>
        </div>
      </footer>

      <div className={`bold-toast ${copyState !== 'idle' ? 'is-visible' : ''} ${copyState === 'error' ? 'is-error' : ''}`} role="status" aria-live="polite">
        {copyState === 'copied' ? 'MCP config copied' : copyState === 'error' ? 'Could not access your clipboard' : ''}
      </div>
    </main>
  )
}
