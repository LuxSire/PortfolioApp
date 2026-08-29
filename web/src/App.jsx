import { useState } from 'react'
import ScreenerView from './pages/ScreenerView'
import PositionsView from './pages/PositionsView'
import TradesView from './pages/TradesView'
import PortfolioView from './pages/PortfolioView'
import FactsheetView from './pages/FactsheetView'
import NewsView from './pages/NewsView'
import ThemesView from './pages/ThemesView'
import SectorsView from './pages/SectorsView'
import HoldersView from './pages/HoldersView'
import RecommendationsView from './pages/RecommendationsView'
import SimulationsView from './pages/SimulationsView'
import TargetView from './pages/TargetView'
import DatasetView from './pages/DatasetView'
import ScoringView from './pages/ScoringView'
import BacktestingView from './pages/BacktestingView'

const TABS = [
  { key: 'positions', label: 'Positions' },
  { key: 'trades', label: 'Trades' },
  { key: 'portfolio', label: 'Portfolio' },
  { key: 'factsheet', label: 'Factsheet' },
  { key: 'news', label: 'News' },
  { key: 'themes', label: 'Themes' },
  { key: 'sectors', label: 'Sectors' },
  { key: 'holders', label: 'Holders' },
  { key: 'recommendations', label: 'Recommendations' },
  { key: 'simulations', label: 'Simulations' },
  { key: 'target', label: 'Target' },
  { key: 'dataset', label: 'Dataset' },
  { key: 'scoring', label: 'Scoring' },
  { key: 'screener', label: 'Screener' },
  { key: 'backtesting', label: 'Backtesting' },
]

export default function App() {
  const [tab, setTab] = useState('positions')

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

      {tab === 'screener' && <ScreenerView />}
      {tab === 'positions' && <PositionsView />}
      {tab === 'trades' && <TradesView />}
      {tab === 'portfolio' && <PortfolioView />}
      {tab === 'factsheet' && <FactsheetView />}
      {tab === 'news' && <NewsView />}
      {tab === 'themes' && <ThemesView />}
      {tab === 'sectors' && <SectorsView />}
      {tab === 'holders' && <HoldersView />}
      {tab === 'recommendations' && <RecommendationsView />}
      {tab === 'simulations' && <SimulationsView />}
      {tab === 'target' && <TargetView />}
      {tab === 'dataset' && <DatasetView />}
      {tab === 'scoring' && <ScoringView />}
      {tab === 'backtesting' && <BacktestingView />}
    </div>
  )
}
