import { Badge, Banner, Button, Dialog, Surface, Text } from '@cloudflare/kumo'
import { CheckCircle, Info, WarningCircle, X } from '@phosphor-icons/react'
import type { ReactNode } from 'react'

export type StatusTone = 'neutral' | 'positive' | 'warning' | 'danger' | 'accent'

export function StatusBadge({ tone = 'neutral', children }: { tone?: StatusTone; children: ReactNode }) {
  const variant = {
    neutral: 'neutral',
    positive: 'success',
    warning: 'warning',
    danger: 'error',
    accent: 'purple',
  }[tone] as 'neutral' | 'success' | 'warning' | 'error' | 'purple'
  return <Badge variant={variant}>{children}</Badge>
}

export function Notice({ tone = 'info', children }: { tone?: 'info' | 'error' | 'success'; children: ReactNode }) {
  return (
    <Banner
      variant={tone === 'error' ? 'error' : tone === 'success' ? 'secondary' : 'default'}
      icon={tone === 'error' ? <WarningCircle weight="fill" /> : tone === 'success' ? <CheckCircle weight="fill" /> : <Info weight="fill" />}
      description={children}
    />
  )
}

export function Modal({ open, title, children, onClose }: { open: boolean; title: string; children: ReactNode; onClose: () => void }) {
  return (
    <Dialog.Root open={open} onOpenChange={(nextOpen) => !nextOpen && onClose()}>
      <Dialog size="lg" className="p-6">
        <div className="mb-6 flex items-start justify-between gap-4">
          <Dialog.Title>{title}</Dialog.Title>
          <Dialog.Close render={(props) => <Button {...props} variant="secondary" shape="square" icon={<X />} aria-label="Close dialog" />} />
        </div>
        {children}
      </Dialog>
    </Dialog.Root>
  )
}

export function PageHeader({ title, description, action }: { title: string; description?: string; action?: ReactNode }) {
  return (
    <header className="page-header">
      <div className="page-header__copy">
        <Text variant="heading1" as="h1">{title}</Text>
        {description && <Text variant="secondary">{description}</Text>}
      </div>
      {action}
    </header>
  )
}

export function Stat({ label, value, detail }: { label: string; value: ReactNode; detail?: string }) {
  return (
    <Surface className="rounded-lg border border-kumo-hairline p-5">
      <Text variant="secondary" size="sm">{label}</Text>
      <Text variant="heading2" as="p">{value}</Text>
      {detail && <Text variant="secondary" size="xs">{detail}</Text>}
    </Surface>
  )
}
