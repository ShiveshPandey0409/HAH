import { LoaderCircle } from 'lucide-react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { useAuth } from './auth/AuthContext'
import { AppShell } from './components/AppShell'
import { ForgotPasswordPage, LoginPage, ResetPasswordPage, SignupPage } from './pages/AuthPages'
import { DashboardPage } from './pages/DashboardPage'
import { IntegrationsPage } from './pages/IntegrationsPage'
import { LandingPage } from './pages/LandingPage'
import { MarketplacePage } from './pages/MarketplacePage'
import { ProfilesPage } from './pages/ProfilesPage'
import { ReviewPage } from './pages/ReviewPage'
import { SettingsPage } from './pages/SettingsPage'
import { TaskDetailPage, TaskEditorPage, TasksPage } from './pages/TasksPage'
import { WorkPage } from './pages/WorkPage'

function ProtectedRoute() {
  const { user, loading } = useAuth()
  if (loading) return <div className="app-loading"><LoaderCircle className="spin" /><span>Opening HAH</span></div>
  return user ? <AppShell /> : <Navigate to="/login" replace />
}

function CapabilityRoute({ capability, children }: { capability: 'creator' | 'human'; children: React.ReactNode }) {
  const { user } = useAuth()
  const allowed = capability === 'creator' ? user?.can_create_tasks : user?.can_work_tasks
  return allowed ? children : <Navigate to="/app" replace />
}

function NotFound() {
  return <div className="not-found"><strong>404</strong><h1>This page wandered off.</h1><a href="/">Back to HAH</a></div>
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<LandingPage />} />
      <Route path="/login" element={<LoginPage />} />
      <Route path="/signup" element={<SignupPage />} />
      <Route path="/forgot-password" element={<ForgotPasswordPage />} />
      <Route path="/reset-password" element={<ResetPasswordPage />} />
      <Route path="/app" element={<ProtectedRoute />}>
        <Route index element={<DashboardPage />} />
        <Route path="tasks" element={<CapabilityRoute capability="creator"><TasksPage /></CapabilityRoute>} />
        <Route path="tasks/new" element={<CapabilityRoute capability="creator"><TaskEditorPage /></CapabilityRoute>} />
        <Route path="tasks/:taskId" element={<CapabilityRoute capability="creator"><TaskDetailPage /></CapabilityRoute>} />
        <Route path="tasks/:taskId/edit" element={<CapabilityRoute capability="creator"><TaskEditorPage /></CapabilityRoute>} />
        <Route path="review" element={<CapabilityRoute capability="creator"><ReviewPage /></CapabilityRoute>} />
        <Route path="integrations" element={<CapabilityRoute capability="creator"><IntegrationsPage /></CapabilityRoute>} />
        <Route path="marketplace" element={<CapabilityRoute capability="human"><MarketplacePage /></CapabilityRoute>} />
        <Route path="work" element={<CapabilityRoute capability="human"><WorkPage /></CapabilityRoute>} />
        <Route path="profiles" element={<CapabilityRoute capability="human"><ProfilesPage /></CapabilityRoute>} />
        <Route path="settings" element={<SettingsPage />} />
      </Route>
      <Route path="*" element={<NotFound />} />
    </Routes>
  )
}
