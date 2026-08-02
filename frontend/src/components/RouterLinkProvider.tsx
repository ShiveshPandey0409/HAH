import { LinkProvider, type LinkComponentProps } from '@cloudflare/kumo'
import { forwardRef, type ReactNode } from 'react'
import { Link as RouterLink } from 'react-router-dom'

const AppLink = forwardRef<HTMLAnchorElement, LinkComponentProps>(({ href, ...props }, ref) => (
  <RouterLink ref={ref} to={href ?? ''} {...props} />
))
AppLink.displayName = 'AppLink'

export function RouterLinkProvider({ children }: { children: ReactNode }) {
  return <LinkProvider component={AppLink}>{children}</LinkProvider>
}
