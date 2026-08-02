import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './styles.css'

// Console errors are otherwise invisible to the e2e harness (and to us):
// keep a small ring buffer the diagnostic can assert against.
declare global {
  interface Window {
    __e2e_errs?: string[]
  }
}
window.__e2e_errs = []
const origError = console.error
console.error = (...args: unknown[]) => {
  window.__e2e_errs!.push(args.map(String).join(' ').slice(0, 300))
  if (window.__e2e_errs!.length > 50) window.__e2e_errs!.shift()
  origError(...args)
}
window.addEventListener('error', (e) => {
  window.__e2e_errs!.push(`uncaught: ${e.message}`)
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
