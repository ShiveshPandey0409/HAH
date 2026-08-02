import {
  Blocks,
  BriefcaseBusiness,
  ChevronDown,
  CircleUserRound,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Menu,
  RadioTower,
  Search,
  Settings,
  UserRoundSearch,
  X,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'

interface NavItem { to: string; label: string; icon: typeof LayoutDashboard }

export function Logo({ inverse = false }: { inverse?: boolean }) {
  return (
    <div className={`logo ${inverse ? 'logo--inverse' : ''}`}>
      <span className="logo__mark">H</span>
      <span className="logo__word">Hire a Human</span>
    </div>
  )
}

export function AppShell() {
  const { user, signOut } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [mobileOpen, setMobileOpen] = useState(false)
  const [accountOpen, setAccountOpen] = useState(false)
  const accountRef = useRef<HTMLDivElement>(null)

  useEffect(() => setMobileOpen(false), [location.pathname])
  useEffect(() => {
    const close = (event: MouseEvent) => {
      if (!accountRef.current?.contains(event.target as Node)) setAccountOpen(false)
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [])

  if (!user) return null

  const creatorNav: NavItem[] = [
    { to: '/app', label: 'Overview', icon: LayoutDashboard },
    { to: '/app/tasks', label: 'My tasks', icon: BriefcaseBusiness },
    { to: '/app/review', label: 'Review work', icon: UserRoundSearch },
    { to: '/app/integrations', label: 'Integrations', icon: RadioTower },
  ]
  const humanNav: NavItem[] = [
    { to: '/app', label: 'Overview', icon: LayoutDashboard },
    { to: '/app/marketplace', label: 'Find work', icon: Search },
    { to: '/app/work', label: 'My work', icon: Blocks },
    { to: '/app/profiles', label: 'Social profiles', icon: CircleUserRound },
  ]
  const nav = user.can_create_tasks ? creatorNav : humanNav
  if (user.can_create_tasks && user.can_work_tasks) {
    nav.push({ to: '/app/marketplace', label: 'Find work', icon: Search })
    nav.push({ to: '/app/work', label: 'My work', icon: Blocks })
    nav.push({ to: '/app/profiles', label: 'Social profiles', icon: CircleUserRound })
  }

  const handleLogout = async () => {
    await signOut()
    navigate('/')
  }

  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileOpen ? 'is-open' : ''}`}>
        <div className="sidebar__top">
          <Logo />
          <button className="icon-button sidebar__close" onClick={() => setMobileOpen(false)} aria-label="Close navigation"><X size={20} /></button>
        </div>
        <nav className="sidebar__nav" aria-label="Main navigation">
          {nav.map(({ to, label, icon: Icon }, index) => (
            <NavLink key={`${to}-${index}`} to={to} end={to === '/app'} className={({ isActive }) => (isActive ? 'nav-item is-active' : 'nav-item')}>
              <Icon size={18} />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar__foot">
          <div className="sidebar__signal"><span /> Live API</div>
          <p>Humans do the work.<br />Agents run the loop.</p>
        </div>
      </aside>
      {mobileOpen && <button className="mobile-scrim" onClick={() => setMobileOpen(false)} aria-label="Close navigation" />}

      <div className="app-frame">
        <header className="topbar">
          <button className="icon-button mobile-menu" onClick={() => setMobileOpen(true)} aria-label="Open navigation"><Menu size={20} /></button>
          <div className="topbar__context">
            <span>{user.can_create_tasks && user.can_work_tasks ? 'Creator + Human' : user.can_create_tasks ? 'Creator workspace' : 'Human workspace'}</span>
          </div>
          <div className="account" ref={accountRef}>
            <button className="account__trigger" onClick={() => setAccountOpen((value) => !value)} aria-expanded={accountOpen}>
              <span className="avatar">{user.display_name.slice(0, 1).toUpperCase()}</span>
              <span className="account__copy"><strong>{user.display_name}</strong><small>{user.email}</small></span>
              <ChevronDown size={16} />
            </button>
            {accountOpen && (
              <div className="account__menu">
                <button onClick={() => { navigate('/app/settings'); setAccountOpen(false) }}><Settings size={16} />Account settings</button>
                <button onClick={() => { navigate('/app/settings#password'); setAccountOpen(false) }}><KeyRound size={16} />Change password</button>
                <button onClick={handleLogout}><LogOut size={16} />Log out</button>
              </div>
            )}
          </div>
        </header>
        <main className="app-main"><Outlet /></main>
      </div>
    </div>
  )
}
