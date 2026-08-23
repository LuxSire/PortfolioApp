import { useEffect, useState } from 'react'
import { getSectorIcon } from '../sectorIcons'
import { momentumClass, meanReversionClass } from '../colorRules'
import { fmtIndex100, fmtNum } from '../screenerFactors'
import { IB_STREAM_URL } from '../ibStream'

// ─── constants ────────────────────────────────────────────────────────────────
const MARKET_VOL = 0.20          // same value used in portfolio_optimizer.py
const RISK_FREE_ANNUAL = 0.035   // same 3.5 %/yr used in portfolio_optimizer.py

// ─── helpers ──────────────────────────────────────────────────────────────────
function fmtPct(v: number | null | undefined): string {
  if (v == null) return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%'
}
function fmtVol(v: number | null | undefined): string {
  if (v == null) return '—'
  return (v * 100).toFixed(1) + '%'
}
function fmtRatio(v: number | null | undefined): string {
  if (v == null) return '—'
  return (v >= 0 ? '+' : '') + v.toFixed(2)
}
function fmtPrice(v: number | null | undefined): string {
  if (v == null) return '—'
  return '$' + v.toLocaleString(undefined, { maximumFractionDigits: 2 })
}
function signClass(v: number | null | undefined): string {
  if (v == null) return ''
  return v > 0 ? 'good' : v < 0 ? 'bad' : ''
}

// ─── types ────────────────────────────────────────────────────────────────────
interface TargetRow {
  ticker: string
  name: string | null
  sector: string | null
  price: number | null
  side: 'Long' | 'Short'
  rating: string | null
  score: number | null
  scorePercentile: number | null
  forecastReturn: number    // 1-year return from simulation (simulations.py step 5)
  positionReturn: number    // +forecastReturn for longs, −forecastReturn for shorts
  vol: number               // β × MARKET_VOL (annualised price vol proxy)
  beta: number
  indivSharpe: number       // (positionReturn − rf) / vol
  analysts: number | null   // numberOfAnalystOpinions (conviction signal)
  targetUpside: number | null // analyst consensus price target upside
  probAbove: number | null  // P(price > current) at blended PE from simulation
  compositeScore: number    // average of 3 rank-percentile signals
  // screener signals
  mom: number | null        // MSI — momentum index [0, 100]
  mr: number | null         // ST-MSI — mean reversion index [0, 100]
  sent: number | null       // social sentiment rank-rescaled [-100, 100]
  newsSent: number | null   // news sentiment rank-rescaled [-100, 100]
  instChange: number | null // inst. holdings QoQ change rank-rescaled [-100, 100]
  insiders: number | null   // insider buy/sell ratio × 100
}

// ─── component ────────────────────────────────────────────────────────────────
export default function TargetView() {
  const [portfolio, setPortfolio] = useState<{
    longs: TargetRow[]
    shorts: TargetRow[]
    stats: { portfolioReturn: number | null; portfolioVol: number | null; sharpe: number | null; sortino: number | null }
    generatedAt: string
  } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [positions, setPositions] = useState<Record<string, { shares: number }>>({})

  useEffect(() => {
    const source = new EventSource(IB_STREAM_URL)
    source.onmessage = (e) => {
      const { positions: pos } = JSON.parse(e.data)
      if (pos) setPositions(pos)
    }
    source.onerror = () => {}
    return () => source.close()
  }, [])

  useEffect(() => {
    fetch('/target_portfolio.json')
      .then((r) => (r.ok ? r.json() : Promise.reject('target_portfolio.json')))
      .then((data) => setPortfolio(data))
      .catch((e) => setError(String(e)))
  }, [])

  const longs: TargetRow[] = portfolio?.longs ?? []
  const shorts: TargetRow[] = portfolio?.shorts ?? []
  const { portfolioReturn, portfolioVol, sharpe, sortino } = portfolio?.stats ?? {
    portfolioReturn: null, portfolioVol: null, sharpe: null, sortino: null,
  }

  const n = longs.length + shorts.length
  const weightPct = n > 0 ? (100 / n).toFixed(1) + '%' : '—'
  const loaded = portfolio !== null

  function PositionTable({ rows, side }: { rows: TargetRow[]; side: 'Long' | 'Short' }) {
    return (
      <section className="target-section">
        <h2 className={`section-heading ${side === 'Long' ? 'good' : 'bad'}`}>{side} Positions ({rows.length})</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className="col-left col-ticker">Ticker</th>
                <th className="col-left col-name">Name</th>
                <th className="col-left">Sector</th>
                <th>Rating</th>
                <th>Price</th>
                <th title="1-year forecast return from 5-year DCF simulation">Fcst (1y)</th>
                <th title={`Annualised vol proxy: β × ${(MARKET_VOL * 100).toFixed(0)}%`}>Vol</th>
                <th title="Average of own-PE and blended-PE simulation probabilities of price being above current">P(↑)</th>
                <th title="Screener percentile rank (0 = top of screener, 100 = bottom)">Screener %</th>
                <th title="Average of 3 rank-percentile signals: Sharpe · screener · rating strength">Composite</th>
                <th title="MSI: Money Flow / RSI momentum index [0=oversold, 100=overbought]">MSI</th>
                <th title="ST-MSI: short-term mean-reversion index [0=oversold, 100=overbought]">ST-MSI</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const Icon = getSectorIcon(r.sector ?? '')
                const isHeld = (positions[r.ticker]?.shares ?? 0) !== 0
                return (
                  <tr key={r.ticker} className={isHeld ? 'row-held' : undefined}>
                    <td className="col-left col-ticker num">
                      <a
                        href={`#/asset/${encodeURIComponent(r.ticker)}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="ticker-link"
                      >
                        {r.ticker}
                      </a>
                    </td>
                    <td className="col-left col-name" title={r.name ?? undefined}>
                      {r.name}
                    </td>
                    <td className="col-left">
                      <span className="sector-cell" title={r.sector ?? undefined}>
                        <Icon />
                        <span className="sector-cell-name">{r.sector}</span>
                      </span>
                    </td>
                    <td className="num">{r.rating}</td>
                    <td className="num">{fmtPrice(r.price)}</td>
                    <td className={`num ${signClass(r.forecastReturn)}`}>{fmtPct(r.forecastReturn)}</td>
                    <td className="num">{fmtVol(r.vol)}</td>
                    <td className={`num ${r.probAbove != null ? r.probAbove > 0.66 ? 'good' : r.probAbove < 0.33 ? 'bad' : '' : ''}`}>{r.probAbove != null ? (r.probAbove * 100).toFixed(0) + '%' : '—'}</td>
                    <td className="num">{r.scorePercentile != null ? r.scorePercentile.toFixed(1) + '%' : '—'}</td>
                    <td className={`num ${signClass(r.compositeScore - 0.5)}`}>{(r.compositeScore * 100).toFixed(0)}%</td>
                    <td className={`num ${momentumClass(r.mom)}`}>{fmtNum(r.mom)}</td>
                    <td className={`num ${meanReversionClass(r.mr)}`}>{fmtNum(r.mr)}</td>
                  </tr>
                )
              })}
              {rows.length === 0 && (
                <tr className="empty-row">
                  <td colSpan={11}>
                    No {side.toLowerCase()} candidates with{' '}
                    {side === 'Long' ? 'positive' : 'negative'} forecast return found.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    )
  }

  return (
    <div className="positions-page portfolio-page">
      <header className="masthead">
        <div className="title-block">
          <h1>Target Portfolio</h1>
        </div>
        {loaded && (
          <div className="stat-row">
            <div
              className="stat"
              title="Equal-weight portfolio expected annualised return: Σ(positionReturn)/N where longs contribute +annRet, shorts contribute −annRet"
            >
              <span className={`n num ${signClass(portfolioReturn)}`}>{fmtPct(portfolioReturn)}</span>
              <span className="l">Expected Return</span>
            </div>
            <div
              className="stat"
              title={`Annualised portfolio volatility from sector-aware covariance matrix (portfolio_optimizer.py); β anchored at ${(MARKET_VOL * 100).toFixed(0)}% market vol`}
            >
              <span className="n num">{fmtVol(portfolioVol)}</span>
              <span className="l">Volatility</span>
            </div>
            <div
              className="stat"
              title={`Annualised Sharpe: (portfolioReturn − ${(RISK_FREE_ANNUAL * 100).toFixed(1)}% risk-free) / portfolioVol`}
            >
              <span className={`n num ${signClass(sharpe)}`}>{fmtRatio(sharpe)}</span>
              <span className="l">Sharpe</span>
            </div>
            <div
              className="stat"
              title="Annualised Sortino: same excess return as Sharpe divided by downside vol — approximated as portfolioVol/√2 (half-normal, same as Sharpe×√2)"
            >
              <span className={`n num ${signClass(sortino)}`}>{fmtRatio(sortino)}</span>
              <span className="l">Sortino</span>
            </div>
            <div className="stat">
              <span className="n num">{longs.length}</span>
              <span className="l">Longs</span>
            </div>
            <div className="stat">
              <span className="n num">{shorts.length}</span>
              <span className="l">Shorts</span>
            </div>
            <div
              className="stat"
              title="Equal gross weight per position"
            >
              <span className="n num">{weightPct}</span>
              <span className="l">Weight each</span>
            </div>
          </div>
        )}
      </header>

      {error && (
        <div className="asset-card">Failed to load: {error}</div>
      )}
      {!error && !loaded && (
        <div className="asset-card">Loading…</div>
      )}

      {!error && loaded && (
        <>
          <PositionTable rows={longs} side="Long" />
          <PositionTable rows={shorts} side="Short" />
        </>
      )}
    </div>
  )
}
