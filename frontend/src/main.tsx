import '@cloudflare/kumo/styles/standalone'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { AuthProvider } from './auth/AuthContext'
import { RouterLinkProvider } from './components/RouterLinkProvider'
import './styles.css'

document.documentElement.dataset.mode = 'light'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <RouterLinkProvider>
        <AuthProvider><App /></AuthProvider>
      </RouterLinkProvider>
    </BrowserRouter>
  </StrictMode>,
)
