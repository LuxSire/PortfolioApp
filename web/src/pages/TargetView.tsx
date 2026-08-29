import { useEffect, useState } from 'react'
import { getSectorIcon } from '../sectorIcons'
import { momentumClass, meanReversionClass } from '../colorRules'
import { fmtIndex100, fmtNum } from '../screenerFactors'
import { IB_STREAM_URL } from '../ibStream'
import type { HistoryByTicker, LivePricesByTicker } from '../interfaces/IScreenerView'

// ─── constants ────────────────────────────────────────────────────────────────
const MARKET_VOL = 0.20          // same value used in portfolio_optimizer.py
const RISK_FREE_ANNUAL = 0.035   // same 3.5 %/yr used in portfolio_optimizer.py

// ─── helpers ──────────────────────────────────────────────────────────────────
// The last close strictly before today, comparing BOTH bar series -- same
// helper, same two files, same fallback order as ScreenerView.tsx's own
// copy (see there for the fuller history of why this matters and isn't
// just a plain ?? fallback chain).
function previousClose(
  dailyHistory3mo: { date: string; close: number }[] | undefined,
  monthlyHistory: { date: string; close: number }[] | undefined
): number | null {
  const lastBarBeforeToday = (series: { date: string; close: number }[] | undefined) => {
    if (!series || series.length === 0) return null
    const today = new Date().toISOString().slice(0, 10)
    for (let i = series.length - 1; i >= 0; i--) {
      const date = series[i].date.slice(0, 10)
      if (date < today) return { date, close: series[i].close }
    }
    return null
  }
  const fromDaily = lastBarBeforeToday(dailyHistory3mo)
  if (fromDaily) return fromDaily.close
  return lastBarBeforeToday(monthlyHistory)?.close ?? null
}

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
// High short interest is favorable for a long (squeeze/contrarian upside
// against an already-crowded short) and unfavorable for a short (piling
// onto a crowded trade is squeeze risk) -- same direction
// portfolio_optimizer.py's own composite-score signal uses, just as a
// simple display threshold rather than a rank-percentile.
function shortInterestClass(si: number | null | undefined, side: 'Long' | 'Short'): string {
  if (si == null) return ''
  if (side === 'Long') return si >= 0.1 ? 'good' : ''
  return si >= 0.1 ? 'bad' : si <= 0.03 ? 'good' : ''
}

// A candidate that was evaluated at the same greedy-selection step as this
// row's own ticker but scored lower -- see portfolio_optimizer.py's own
// _greedy_max_sharpe docstring: this is what the optimizer would have
// picked into THIS slot instead, had the row's own ticker not been
// available, not just "the next-best composite score somewhere in the
// pool" (which ignores how a candidate would actually interact with the
// portfolio already built at that point).
interface Alternate {
  ticker: string
  name: string | null
  rating: string | null
  forecastReturn: number | null
  compositeScore: number
  poolRank: number
}

// One "Nth Choice" cell -- ticker (linked) + name, with forecastReturn as a
// signed subvalue so a runner-up's own directional thesis is visible at a
// glance without opening its own asset page. '—' when this slot ran out of
// candidates (a thin pool, e.g. a niche sector with few Strong Buy/Sell names).
function AlternateCell({ alt }: { alt: Alternate | undefined }) {
  if (!alt) return <td className="col-left col-name">—</td>
  return (
    <td className="col-left col-name" title={alt.name ?? undefined}>
      <a href={`#/asset/${encodeURIComponent(alt.ticker)}`} target="_blank" rel="noopener noreferrer" className="ticker-link">
        {alt.ticker}
      </a>
      {alt.forecastReturn != null && (
        <span className={`live-price ${signClass(alt.forecastReturn)}`}>{fmtPct(alt.forecastReturn)}</span>
      )}
    </td>
  )
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
  forecastReturn: number    // confidence-weighted fair-value-vs-current return from simulation (simulations.py step 5)
  positionReturn: number    // +forecastReturn for longs, −forecastReturn for shorts
  vol: number               // β × MARKET_VOL (annualised price vol proxy)
  beta: number
  indivSharpe: number       // (positionReturn − rf) / vol
  analysts: number | null   // numberOfAnalystOpinions (conviction signal)
  targetUpside: number | null // analyst consensus price target upside
  probAbove: number | null  // P(price > current) at industry-median PE from simulation
  shortInterest: number | null // FINRA pctOfFloat (fresher), else yfinance shortPercentOfFloat
  compositeScore: number    // average of 4 rank-percentile signals (see portfolio_optimizer.py's _composite_score)
  alternates: Alternate[]   // 2nd/3rd choice for this slot -- see Alternate's own comment
  // screener signals
  mom: number | null        // MSI — momentum index [0, 100]
  mr: number | null         // ST-MSI — mean reversion index [0, 100]
  sent: number | null       // social sentiment rank-rescaled [-100, 100]
  newsSent: number | null   // news sentiment rank-rescaled [-100, 100]
  instChange: number | null // inst. holdings QoQ change rank-rescaled [-100, 100]
  insiders: number | null   // insider buy/sell ratio × 100
}

type LegStats = { return: number | null; vol: number | null; sharpe: number | null }
type Portfolio = {
  longs: TargetRow[]
  shorts: TargetRow[]
  stats: {
    portfolioReturn: number | null
    portfolioVol: number | null
    sharpe: number | null
    sortino: number | null
    long?: LegStats
    short?: LegStats
  }
  generatedAt: string
}

// ─── component ────────────────────────────────────────────────────────────────
export default function TargetView() {
  const [portfolioAll, setPortfolioAll] = useState<Portfolio | null>(null)
  const [portfolioEx, setPortfolioEx] = useState<Portfolio | null>(null)
  // Which target portfolio to show: the full universe, or the variant
  // with Financial Services + Healthcare + Real Estate excluded
  // (target_portfolio_ex.json).
  const [variant, setVariant] = useState<'all' | 'ex'>('all')
  const portfolio = variant === 'ex' ? portfolioEx : portfolioAll
  const [error, setError] = useState<string | null>(null)
  const [positions, setPositions] = useState<Record<string, { shares: number }>>({})
  const [livePrices, setLivePrices] = useState<LivePricesByTicker>({})
  // Daily-close series for the Price column's daily-% badge -- see
  // previousClose. Same two files, same fallback order, as
  // ScreenerView.tsx/PositionsView.jsx.
  const [dailyHistory3mo, setDailyHistory3mo] = useState<HistoryByTicker>({})
  const [monthlyHistory, setMonthlyHistory] = useState<HistoryByTicker>({})
  // Long / Short shown on separate tabs -- same tab-bar convention as
  // PositionsView.tsx / AssetView.tsx.
  const [tab, setTab] = useState<'long' | 'short'>('long')

  useEffect(() => {
    const source = new EventSource(IB_STREAM_URL)
    source.onmessage = (e) => {
      const { prices, positions: pos } = JSON.parse(e.data)
      if (prices) setLivePrices(prices)
      if (pos) setPositions(pos)
    }
    source.onerror = () => {}
    return () => source.close()
  }, [])

  useEffect(() => {
    fetch('/price_history_daily_3mo.json')
      .then((r) => (r.ok ? r.json() : {}))
      .then(setDailyHistory3mo)
      .catch(() => {})
    fetch('/price_history.json')
      .then((r) => (r.ok ? r.json() : {}))
      .then(setMonthlyHistory)
      .catch(() => {})
  }, [])

  useEffect(() => {
    fetch('/target_portfolio.json')
      .then((r) => (r.ok ? r.json() : Promise.reject('target_portfolio.json')))
      .then((data) => setPortfolioAll(data))
      .catch((e) => setError(String(e)))
    // Optional -- if it isn't on disk yet the "all" portfolio still renders
    // and the Ex-Fin/Health tab just shows a loading state.
    fetch('/target_portfolio_ex.json')
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((data) => setPortfolioEx(data))
      .catch(() => {})
  }, [])

  const longs: TargetRow[] = portfolio?.longs ?? []
  const shorts: TargetRow[] = portfolio?.shorts ?? []
  const {
    portfolioReturn,
    portfolioVol,
    sharpe,
    sortino,
    long: longLeg,
    short: shortLeg,
  } = portfolio?.stats ?? {
    portfolioReturn: null, portfolioVol: null, sharpe: null, sortino: null, long: undefined, short: undefined,
  }

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
                <th title="Confidence-weighted fair value vs. current price, from 5-year DCF simulation">Forecast</th>
                <th title={`Annualised vol proxy: β × ${(MARKET_VOL * 100).toFixed(0)}%`}>Vol</th>
                <th title="Simulation probability of price being above current, at the industry-median PE">P(↑)</th>
                <th title="FINRA pctOfFloat (fresher), else yfinance shortPercentOfFloat -- high short interest favors a long (squeeze/contrarian upside) and penalizes a short (crowded-trade squeeze risk)">Short Int.</th>
                <th title="Screener percentile rank (0 = top of screener, 100 = bottom)">Screener %</th>
                <th title="Average of 4 rank-percentile signals: Sharpe · screener · rating strength · short interest">Composite</th>
                <th title="MSI: Money Flow / RSI momentum index [0=oversold, 100=overbought]">MSI</th>
                <th title="ST-MSI: short-term mean-reversion index [0=oversold, 100=overbought]">ST-MSI</th>
                <th
                  className="col-left"
                  title="The candidate the optimizer would have picked into this slot instead, had this ticker not been available -- evaluated against the SAME already-selected portfolio at that step, not just the next-best composite score in the pool"
                >
                  2nd Choice
                </th>
                <th
                  className="col-left"
                  title="The next candidate after 2nd Choice, same slot, same step"
                >
                  3rd Choice
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const Icon = getSectorIcon(r.sector ?? '')
                const isHeld = (positions[r.ticker]?.shares ?? 0) !== 0
                // Live IB Gateway price vs. yesterday's close -- same
                // price-cell badge as ScreenerView.tsx's own copy.
                const live = livePrices[r.ticker]
                const referencePrice = previousClose(dailyHistory3mo[r.ticker], monthlyHistory[r.ticker]) ?? r.price
                const liveRatio = live?.last != null && referencePrice ? live.last / referencePrice - 1 : null
                const liveClass =
                  liveRatio === null
                    ? ''
                    : Math.abs(liveRatio) <= 0.005
                      ? 'perf-neutral'
                      : liveRatio >= 0
                        ? 'perf-pos'
                        : 'perf-neg'
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
                    <td className="num price-cell">
                      <span className="price-value">{live?.last != null ? fmtPrice(live.last) : fmtPrice(r.price)}</span>
                      {liveRatio !== null && live && (
                        <span
                          className={`live-price ${liveClass}`}
                          title={`IB Gateway ${fmtPrice(live.last ?? null)} at ${live.timestamp} vs. yesterday's close ${fmtPrice(referencePrice)}`}
                        >
                          {fmtPct(liveRatio)}
                        </span>
                      )}
                    </td>
                    <td className={`num ${signClass(r.forecastReturn)}`}>{fmtPct(r.forecastReturn)}</td>
                    <td className="num">{fmtVol(r.vol)}</td>
                    <td className={`num ${r.probAbove != null ? r.probAbove > 0.66 ? 'good' : r.probAbove < 0.33 ? 'bad' : '' : ''}`}>{r.probAbove != null ? (r.probAbove * 100).toFixed(0) + '%' : '—'}</td>
                    <td className={`num ${shortInterestClass(r.shortInterest, r.side)}`}>
                      {r.shortInterest != null ? (r.shortInterest * 100).toFixed(1) + '%' : '—'}
                    </td>
                    <td className="num">{r.scorePercentile != null ? r.scorePercentile.toFixed(1) + '%' : '—'}</td>
                    <td className={`num ${signClass(r.compositeScore - 0.5)}`}>{(r.compositeScore * 100).toFixed(0)}%</td>
                    <td className={`num ${momentumClass(r.mom)}`}>{fmtNum(r.mom)}</td>
                    <td className={`num ${meanReversionClass(r.mr)}`}>{fmtNum(r.mr)}</td>
                    <AlternateCell alt={r.alternates?.[0]} />
                    <AlternateCell alt={r.alternates?.[1]} />
                  </tr>
                )
              })}
              {rows.length === 0 && (
                <tr className="empty-row">
                  <td colSpan={14}>
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
          <div className="tab-bar">
            {(
              [
                { key: 'all', label: 'All sectors' },
                { key: 'ex', label: 'Ex-Fin, Health & Real Estate' },
              ] as const
            ).map((v) => (
              <button
                key={v.key}
                type="button"
                className={`tab-btn${variant === v.key ? ' active' : ''}`}
                onClick={() => setVariant(v.key)}
              >
                {v.label}
              </button>
            ))}
          </div>
        </div>
        {loaded && (
          <div className="stat-row">
            <div
              className="stat"
              title="Expected annualised return, 1/50 per position (each leg 100% gross, dollar-neutral): mean(long forecastReturn) − mean(short forecastReturn)"
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
            <div
              className="stat"
              title={`Long leg on its own (${longs.length} names, 1/${longs.length} each, 100% gross): mean long forecastReturn`}
            >
              <span className={`n num ${signClass(longLeg?.return ?? null)}`}>{fmtPct(longLeg?.return ?? null)}</span>
              <span className="l">Long Return</span>
            </div>
            <div
              className="stat"
              title={`Long leg annualised Sharpe: (return − ${(RISK_FREE_ANNUAL * 100).toFixed(1)}% risk-free) / vol ${fmtVol(longLeg?.vol ?? null)}`}
            >
              <span className={`n num ${signClass(longLeg?.sharpe ?? null)}`}>{fmtRatio(longLeg?.sharpe ?? null)}</span>
              <span className="l">Long Sharpe</span>
            </div>
            <div
              className="stat"
              title={`Short leg on its own (${shorts.length} names, 1/${shorts.length} each, 100% gross): profit from prices falling, −mean short forecastReturn`}
            >
              <span className={`n num ${signClass(shortLeg?.return ?? null)}`}>{fmtPct(shortLeg?.return ?? null)}</span>
              <span className="l">Short Return</span>
            </div>
            <div
              className="stat"
              title={`Short leg annualised Sharpe: (return − ${(RISK_FREE_ANNUAL * 100).toFixed(1)}% risk-free) / vol ${fmtVol(shortLeg?.vol ?? null)}`}
            >
              <span className={`n num ${signClass(shortLeg?.sharpe ?? null)}`}>{fmtRatio(shortLeg?.sharpe ?? null)}</span>
              <span className="l">Short Sharpe</span>
            </div>
          </div>
        )}
      </header>

      {error && (
        <div className="asset-card">Failed to load: {error}</div>
      )}
      {!error && !loaded && variant === 'ex' && portfolioAll !== null && (
        <div className="asset-card">
          Ex-Fin, Health &amp; Real Estate portfolio not generated yet — run <code>python main.py target</code>.
        </div>
      )}
      {!error && !loaded && !(variant === 'ex' && portfolioAll !== null) && (
        <div className="asset-card">Loading…</div>
      )}

      {!error && loaded && (
        <>
          <div className="tab-bar">
            {(
              [
                { key: 'long', label: `Long (${longs.length})` },
                { key: 'short', label: `Short (${shorts.length})` },
              ] as const
            ).map((t) => (
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

          {tab === 'long' && <PositionTable rows={longs} side="Long" />}
          {tab === 'short' && <PositionTable rows={shorts} side="Short" />}
        </>
      )}
    </div>
  )
}
