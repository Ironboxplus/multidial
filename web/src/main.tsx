import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from '@/App'
import '@/index.css'

// On the router the host div is rendered by the LuCI template; guard so the
// bundle is a no-op if it's ever loaded without its mount point.
const el = document.getElementById('multidial-root')
if (el) {
  el.classList.add('dark')
  createRoot(el).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
}
