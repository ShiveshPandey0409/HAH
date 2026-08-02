import { Badge, Banner, Button, Grid, GridItem, Link, LinkButton, Surface, Text } from '@cloudflare/kumo'
import { ArrowRight, Check, Copy, Info, LinkedinLogo, RedditLogo, UsersThree, WarningCircle } from '@phosphor-icons/react'
import { useEffect, useRef, useState } from 'react'
import { Logo } from '../components/AppShell'

const mcpUrl = import.meta.env.VITE_MCP_URL || 'http://localhost:8000/mcp'
const mcpConfig = { mcpServers: { hah: { url: mcpUrl } } }

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
    <main className="landing-page">
      <header className="landing-nav">
        <Link href="/" variant="plain"><Logo /></Link>
        <nav aria-label="Primary navigation">
          <Link href="/signup?role=human" variant="plain">Find work</Link>
          <LinkButton href="/login" variant="secondary">Sign in</LinkButton>
        </nav>
      </header>

      <section className="landing-hero" aria-labelledby="hero-title">
        <div className="landing-hero__copy">
          <Text id="hero-title" variant="heading1" as="h1">Give your agent humans.</Text>
          <Text variant="secondary" size="lg">One MCP connects your agent to real people who can post, comment, and amplify your product across Reddit and LinkedIn.</Text>
          <div className="landing-actions">
            <Button variant="primary" size="lg" icon={copyState === 'copied' ? <Check /> : <Copy />} onClick={copyMcp}>
              {copyState === 'copied' ? 'Copied MCP JSON' : 'Copy MCP JSON'}
            </Button>
            <LinkButton href="/signup?role=brand" variant="secondary" size="lg" icon={<ArrowRight />}>Create manually</LinkButton>
          </div>
          {copyState !== 'idle' && (
            <Banner
              size="sm"
              variant={copyState === 'error' ? 'error' : 'secondary'}
              icon={copyState === 'error' ? <WarningCircle weight="fill" /> : <Info weight="fill" />}
              description={copyState === 'error' ? 'Could not access your clipboard.' : 'MCP config copied.'}
            />
          )}
        </div>

        <Surface className="landing-hero__demo rounded-lg border border-kumo-hairline p-6">
          <Text variant="secondary" size="sm">Agent request</Text>
          <Text variant="heading3" as="h2">“Find three Reddit users to share our launch.”</Text>
          <div className="landing-demo__flow">
            <Badge variant="success">Task created</Badge>
            <Badge variant="info">Humans matched</Badge>
            <Badge variant="purple">Proof submitted</Badge>
          </div>
          <Text variant="mono-secondary">{mcpUrl}</Text>
        </Surface>
      </section>

      <section className="landing-section" aria-labelledby="how-title">
        <Text id="how-title" variant="heading2" as="h2">A short loop, completed by real people</Text>
        <Grid variant="4up" gap="base">
          {[
            ['01', 'Create', 'Your agent creates a clear paid task.'],
            ['02', 'Match', 'Eligible humans find work through public profiles.'],
            ['03', 'Verify', 'Humans submit the proof the task requires.'],
            ['04', 'Pay', 'Approved work receives the fixed reward.'],
          ].map(([number, title, description]) => (
            <GridItem key={number}>
              <Surface className="landing-step rounded-lg border border-kumo-hairline p-5">
                <Badge variant="neutral">{number}</Badge>
                <Text variant="heading3" as="h3">{title}</Text>
                <Text variant="secondary" size="sm">{description}</Text>
              </Surface>
            </GridItem>
          ))}
        </Grid>
      </section>

      <footer className="landing-footer">
        <div><RedditLogo size={24} /><LinkedinLogo size={24} /><UsersThree size={24} /></div>
        <Text variant="secondary" size="sm">Reddit, LinkedIn, and real humans.</Text>
      </footer>
    </main>
  )
}
