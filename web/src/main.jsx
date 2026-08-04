import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import Asset from './Asset.jsx'

function parseRoute(hash) {
  const m = hash.match(/^#\/asset\/([^/]+)$/)
  return m ? { page: 'asset', ticker: decodeURIComponent(m[1]) } : { page: 'app' }
}

function Router() {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash))

  useEffect(() => {
    const onHashChange = () => setRoute(parseRoute(window.location.hash))
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  return route.page === 'asset' ? <Asset ticker={route.ticker} /> : <App />
}

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <Router />
  </StrictMode>,
)
