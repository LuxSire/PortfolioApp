import { useState } from 'react'
import PeTable from './PeTable'
import PositionsView from './PositionsView'
import PortfolioView from './PortfolioView'

const TABS = [
  { key: 'screener', label: 'Screener' },
  { key: 'positions', label: 'Positions' },
  { key: 'portfolio', label: 'Portfolio' },
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
      {tab === 'portfolio' && <PortfolioView />}
    </div>
  )
}
