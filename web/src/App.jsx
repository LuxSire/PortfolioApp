import { useState } from 'react'
import PeTable from './components/PeTable'
import PositionsView from './components/PositionsView'
import TradesView from './components/TradesView'
import PortfolioView from './components/PortfolioView'
import NewsView from './components/NewsView'
import SectorsView from './components/SectorsView'

const TABS = [
  { key: 'screener', label: 'Screener' },
  { key: 'positions', label: 'Positions' },
  { key: 'trades', label: 'Trades' },
  { key: 'portfolio', label: 'Portfolio' },
  { key: 'news', label: 'News' },
  { key: 'sectors', label: 'Sectors' },
]

export default function App() {
  const [tab, setTab] = useState('screener')

  return (
    <div className="app">
      <div className="tab-bar">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            className={`tab-btn${tab === t.key ? ' active' : ''}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'screener' && <PeTable />}
      {tab === 'positions' && <PositionsView />}
      {tab === 'trades' && <TradesView />}
      {tab === 'portfolio' && <PortfolioView />}
      {tab === 'news' && <NewsView />}
      {tab === 'sectors' && <SectorsView />}
    </div>
  )
}
