import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/v1': process.env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000',
      '/health': process.env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000',
      '/ready': process.env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000',
    },
  },
})
