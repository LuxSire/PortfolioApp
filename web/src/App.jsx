import { useRef, useState } from 'react'
import IbFreshnessBadge from './components/IbFreshnessBadge'
import AccountBar from './components/AccountBar'
import ExportTabButton from './components/ExportTabButton'
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
import MathsView from './pages/MathsView'

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
  { key: 'screener', label: 'Screener' },
  { key: 'backtesting', label: 'Backtesting' },
  { key: 'dataset', label: 'Dataset' },
  { key: 'scoring', label: 'Scoring' },
  { key: 'maths', label: 'Maths' },
]

export default function App() {
  const [tab, setTab] = useState('positions')
  const contentRef = useRef(null)
  const tabLabel = TABS.find((t) => t.key === tab)?.label ?? tab

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
        <IbFreshnessBadge />
        <ExportTabButton tabKey={tab} tabLabel={tabLabel} contentRef={contentRef} />
      </div>

      <AccountBar />

      <div className="tab-content" ref={contentRef}>
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
      {tab === 'maths' && <MathsView />}
      </div>
    </div>
  )
}
