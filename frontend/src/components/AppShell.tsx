import { Button, DropdownMenu, Sidebar, Text } from '@cloudflare/kumo'
import {
  Broadcast,
  Briefcase,
  CaretDown,
  CirclesFour,
  Gear,
  Key,
  MagnifyingGlass,
  SignOut,
  SquaresFour,
  User,
  UserFocus,
} from '@phosphor-icons/react'
import type { Icon } from '@phosphor-icons/react'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'

interface NavItem { to: string; label: string; icon: Icon }

export function Logo() {
  return (
    <div className="logo" aria-label="Hire a Human">
      <span className="logo__mark">H</span>
      <Text as="span" bold>Hire a Human</Text>
    </div>
  )
}

export function AppShell() {
  const { user, signOut } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  if (!user) return null

  const creatorNav: NavItem[] = [
    { to: '/app', label: 'Overview', icon: SquaresFour },
    { to: '/app/tasks', label: 'My tasks', icon: Briefcase },
    { to: '/app/review', label: 'Review work', icon: UserFocus },
    { to: '/app/integrations', label: 'Integrations', icon: Broadcast },
  ]
  const humanNav: NavItem[] = [
    { to: '/app', label: 'Overview', icon: SquaresFour },
    { to: '/app/marketplace', label: 'Find work', icon: MagnifyingGlass },
    { to: '/app/work', label: 'My work', icon: CirclesFour },
    { to: '/app/profiles', label: 'Social profiles', icon: User },
  ]
  const nav = user.can_create_tasks ? [...creatorNav] : [...humanNav]
  if (user.can_create_tasks && user.can_work_tasks) nav.push(...humanNav.slice(1))

  const handleLogout = async () => {
    await signOut()
    navigate('/')
  }

  return (
    <Sidebar.Provider defaultOpen collapsible="offcanvas" mobileBreakpoint={768} className="app-shell">
      <Sidebar>
        <Sidebar.Header><Logo /></Sidebar.Header>
        <Sidebar.Content>
          <Sidebar.Group>
            <Sidebar.GroupLabel>Workspace</Sidebar.GroupLabel>
            <Sidebar.Menu>
              {nav.map(({ to, label, icon }) => (
                <Sidebar.MenuButton key={to} href={to} icon={icon} active={location.pathname === to || (to !== '/app' && location.pathname.startsWith(`${to}/`))}>
                  {label}
                </Sidebar.MenuButton>
              ))}
            </Sidebar.Menu>
          </Sidebar.Group>
        </Sidebar.Content>
        <Sidebar.Footer>
          <Text variant="secondary" size="xs">Humans do the work. Agents run the loop.</Text>
          <Sidebar.Trigger />
        </Sidebar.Footer>
      </Sidebar>

      <div className="app-frame">
        <header className="topbar">
          <Sidebar.Trigger />
          <Text variant="secondary" size="sm">
            {user.can_create_tasks && user.can_work_tasks ? 'Creator + Human' : user.can_create_tasks ? 'Creator workspace' : 'Human workspace'}
          </Text>
          <DropdownMenu>
            <DropdownMenu.Trigger render={<Button variant="secondary" icon={<User />} /> }>
              {user.display_name}
              <CaretDown />
            </DropdownMenu.Trigger>
            <DropdownMenu.Content>
              <DropdownMenu.Group>
                <DropdownMenu.Label>{user.email}</DropdownMenu.Label>
                <DropdownMenu.LinkItem href="/app/settings" icon={Gear}>Account settings</DropdownMenu.LinkItem>
                <DropdownMenu.LinkItem href="/app/settings#password" icon={Key}>Change password</DropdownMenu.LinkItem>
              </DropdownMenu.Group>
              <DropdownMenu.Separator />
              <DropdownMenu.Item icon={SignOut} variant="danger" onClick={handleLogout}>Log out</DropdownMenu.Item>
            </DropdownMenu.Content>
          </DropdownMenu>
        </header>
        <main className="app-main"><Outlet /></main>
      </div>
    </Sidebar.Provider>
  )
}
