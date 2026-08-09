import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import Asset from './components/Asset.jsx'
import NewsPopup from './components/NewsPopup.jsx'

function parseRoute(hash) {
  const assetMatch = hash.match(/^#\/asset\/([^/]+)$/)
  if (assetMatch) return { page: 'asset', ticker: decodeURIComponent(assetMatch[1]) }
  const newsMatch = hash.match(/^#\/news\/([^/]+)$/)
  if (newsMatch) return { page: 'news', ticker: decodeURIComponent(newsMatch[1]) }
  return { page: 'app' }
}

function Router() {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash))

  useEffect(() => {
    const onHashChange = () => setRoute(parseRoute(window.location.hash))
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  if (route.page === 'asset') return <Asset ticker={route.ticker} />
  if (route.page === 'news') return <NewsPopup ticker={route.ticker} />
  return <App />
}

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <Router />
  </StrictMode>,
)
