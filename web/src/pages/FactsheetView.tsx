import { useEffect, useRef, useState } from 'react'
import { Printer } from 'lucide-react'
import type { PortfolioDayRow, PortfolioPerformanceData } from '../interfaces/IPortfolioView'
import type { PositionsByTicker, PricesByTicker } from '../interfaces/IPositionsView'
import NavChart from '../components/NavChart'
import MonthlyReturnsTable from '../components/MonthlyReturnsTable'
import { parseCSV } from '../csv'
import { getSectorGroup, sectorGroupLabel } from '../sectorGroups'
import { IB_STREAM_URL } from '../ibStream'
import logo from '../firefly.jpeg'

// Signed, 2 decimals -- same convention PortfolioView.tsx's own fmtPct uses
// for every return figure in this app.
function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(2) + '%'
}

// Sharpe/Sortino -- a plain signed ratio, not a dollar or percentage
// figure. Same as PortfolioView.tsx's own fmtRatio.
function fmtRatio(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '') + v.toFixed(2)
}

// Max drawdown is stored as a positive magnitude (0 = never declined from
// a prior peak) but always represents a decline, so it gets a fixed
// leading '-' rather than a sign that could ever read as '+' -- same as
// PortfolioView.tsx's own fmtDrawdown.
function fmtDrawdown(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return '-' + (v * 100).toFixed(2) + '%'
}

// Month + year only, no day -- Inception is a track-record start month,
// not a specific day worth calling out.
function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso + 'T00:00:00')
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'long' })
}

// Same value-as-%-of-NAV convention PortfolioView.tsx's own
// fmtExposurePct uses for the Stock Long/Short/Net/Gross columns --
// applied here to individual positions and sector totals instead.
function fmtExposurePct(v: number | null | undefined, nav: number | null | undefined): string {
  if (v === null || v === undefined || nav === null || nav === undefined || nav === 0) return '—'
  return ((v / nav) * 100).toFixed(1) + '%'
}

// Label/value pair for the Important Statistics list below -- two grid
// children (not one wrapping row div) so a plain 2-column CSS grid
// (.factsheet-stat-list) lines every label up in its own column and every
// value in its own, rather than AssetView.tsx's .asset-stat tiles (value
// stacked over label).
function StatRow({ label, value, valueClass, title }: { label: string; value: string; valueClass?: string; title?: string }) {
  return (
    <>
      <span className="l" title={title}>
        {label}
      </span>
      <span className={`n num${valueClass ? ` ${valueClass}` : ''}`}>{value}</span>
    </>
  )
}

// Both ratios below use a 3.5%/yr risk-free rate and 252 trading days/year
// to annualize -- same convention PortfolioView.tsx's own Sharpe/Sortino
// use, duplicated here rather than imported (this project's convention
// for a small computation needed by more than one component).
const TRADING_DAYS_PER_YEAR = 252
const RISK_FREE_RATE_ANNUAL = 0.035
const RISK_FREE_RATE_DAILY = RISK_FREE_RATE_ANNUAL / TRADING_DAYS_PER_YEAR

// A4 (210mm x 297mm) minus styles.scss's own @page margin (12mm on every
// side), in CSS px at 96px/inch -- the actual printable box a scaled-down
// page has to fit into, on both axes, to land on a single sheet.
// (210 - 24) / 25.4 * 96 and (297 - 24) / 25.4 * 96.
const A4_PRINTABLE_WIDTH_PX = 703
const A4_PRINTABLE_HEIGHT_PX = 1032

// zoom (see styles.scss's own .factsheet-page rule) resizes every
// element's box individually rather than scaling one pre-rendered image,
// so sub-pixel rounding on each of them can add a few px back on top of
// the mathematically-exact scale. That alone is only a couple of px, but
// styles.scss's .asset-card { break-inside: avoid } turns even a couple
// of px of overflow into a whole extra, mostly-blank page -- avoiding a
// mid-card split means the entire last card that doesn't quite fit jumps
// to page 2 rather than just its overflowing few px (confirmed live: a
// page computed to land right at the 1032px budget still produced 2
// pages this way). This margin leaves enough real slack that the
// rounding never gets close enough to that boundary to trigger it.
const PRINT_SAFETY_MARGIN = 0.93

// LuxSire's own public factsheet (https://luxsire.github.io/Trading_Strategy/)
// for their SEESAW strategy is the layout this page is modeled on: an
// "IMPORTANT STATISTICS" panel, a "WHAT WE DO" blurb, a cumulative-returns
// chart, a daily-returns chart, and a year-by-month returns grid. Built
// here from our own real portfolio_performance.json (see PortfolioView.tsx
// for where that data comes from) instead of re-deriving their layout from
// scratch -- NavChart already renders exactly the cumulative (indexed NAV)
// and daily (P&L bar) charts that page has as two separate ones, and
// MonthlyReturnsTable already is that page's year x month returns grid, so
// both are reused as-is rather than rebuilt.
export default function FactsheetView() {
  const [data, setData] = useState<PortfolioPerformanceData | null>(null)
  const [error, setError] = useState(false)
  const [positions, setPositions] = useState<PositionsByTicker>({})
  const [prices, setPrices] = useState<PricesByTicker>({})
  const [tickerInfo, setTickerInfo] = useState<Record<string, { name: string; sector: string }>>({})
  const pageRef = useRef<HTMLDivElement>(null)

  // Captures the page's own current on-screen size (whatever the user has
  // resized their browser to) right before printing, and pins the print
  // output to exactly that width/height, uniformly scaled down to fit a
  // SINGLE A4 sheet -- an exact replica of what's on screen, not a
  // print-specific reflow to a narrower layout. Scaling by the smaller of
  // the width and height ratios (not width alone) is what guarantees one
  // page rather than the content's natural height spilling onto a second
  // sheet: a wide-but-short window needs little shrinking to fit A4's
  // width but might still need more to fit its height into one page, and
  // vice versa for a narrow-but-tall one. Has to happen here,
  // synchronously on click, rather than in a beforeprint handler: by the
  // time beforeprint fires, the page has already switched to print
  // media/layout, so its size by then reflects the print viewport, not
  // the real on-screen window the user actually resized (see
  // styles.scss's own print rules, which read the
  // --print-natural-width/--print-scale custom properties this sets).
  // Never scales UP past 1 -- a page that already fits A4 at its own size
  // just prints unscaled.
  function handlePrint() {
    const el = pageRef.current
    if (el) {
      const naturalWidth = el.getBoundingClientRect().width
      const naturalHeight = el.scrollHeight
      const fitScale = Math.min(A4_PRINTABLE_WIDTH_PX / naturalWidth, A4_PRINTABLE_HEIGHT_PX / naturalHeight)
      const scale = Math.min(1, fitScale * PRINT_SAFETY_MARGIN)
      // transform: scale shrinks how .factsheet-page PAINTS, but browsers
      // compute page breaks from its un-transformed scrollHeight/
      // offsetHeight -- confirmed live: those stay at the pre-scale value
      // no matter what transform (or zoom, which has the same gap) says,
      // so pagination still splits the content across 2 sheets even
      // though it visually fits on well under 1. Setting an EXPLICIT,
      // already-scaled height on the ANCESTOR that clips it (see
      // styles.scss's own .app:has(.factsheet-page) rule) is what
      // actually constrains the box pagination measures -- set on
      // documentElement, not this element, since CSS custom properties
      // only inherit downward and .app is an ancestor of this ref, not a
      // descendant.
      const scaledHeight = naturalHeight * scale
      const root = document.documentElement.style
      root.setProperty('--print-natural-width', `${naturalWidth}px`)
      root.setProperty('--print-scale', `${scale}`)
      root.setProperty('--print-scaled-height', `${scaledHeight}px`)
    }
    window.print()
  }

  useEffect(() => {
    fetch('/portfolio_performance.json')
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then(setData)
      .catch(() => setError(true))
  }, [])

  // Ticker -> name/sector, the same best-effort sorted_screen.csv lookup
  // PositionsView.tsx/ScreenerView.tsx use -- missing/failed just means a
  // held ticker falls back to its own symbol and an "Other" sector below,
  // not a load error for the whole page.
  useEffect(() => {
    fetch('/sorted_screen.csv')
      .then((r) => (r.ok ? r.text() : ''))
      .then((text) => {
        const info: Record<string, { name: string; sector: string }> = {}
        for (const row of parseCSV(text)) {
          info[row.ticker] = { name: row.name, sector: row.sector }
        }
        setTickerInfo(info)
      })
      .catch(() => {})
  }, [])

  // Live positions/prices from ib_price_server.py's own stream -- the same
  // EventSource PositionsView.tsx/ScreenerView.tsx already subscribe to,
  // just for the Top 5 Long Positions / Sector Exposure cards below rather
  // than a full live blotter. If that server isn't running, this just
  // never fires (EventSource auto-reconnects, same as those pages) and
  // both cards render empty rather than erroring the whole page.
  useEffect(() => {
    const source = new EventSource(IB_STREAM_URL)
    source.onmessage = (e) => {
      const { prices: p, positions: pos } = JSON.parse(e.data)
      if (p) setPrices(p)
      if (pos) setPositions(pos)
    }
    source.onerror = () => {}
    return () => source.close()
  }, [])

  const rows: PortfolioDayRow[] | null = data?.kind === 'daily' ? (data.rows ?? null) : null

  // Single pass over the date-ascending rows: the daily-return series (same
  // money-weighted Total P&L / prior-day NAV definition every stat on this
  // page shares) and its geometric compounding into an equity curve (for
  // total/annualized return and max drawdown) -- the same two things
  // PortfolioView.tsx computes separately, combined into one loop since
  // this page needs both at once.
  const dailyReturns: number[] = []
  let compounded = 1
  let peakCompounded = 1
  let worstDrawdown = 0
  if (rows) {
    let prevNav: number | null = null
    for (const r of rows) {
      const totalPnl = r.realized !== null && r.unrealized !== null ? r.realized + r.unrealized : null
      const dailyReturn = totalPnl !== null && prevNav ? totalPnl / prevNav : null
      if (r.nav !== null) prevNav = r.nav
      if (dailyReturn === null) continue

      dailyReturns.push(dailyReturn)
      compounded *= 1 + dailyReturn
      if (compounded > peakCompounded) peakCompounded = compounded
      const drawdown = (peakCompounded - compounded) / peakCompounded
      if (drawdown > worstDrawdown) worstDrawdown = drawdown
    }
  }

  const n = dailyReturns.length
  const totalReturn = n > 0 ? compounded - 1 : null
  const annualizedReturn = n > 0 ? Math.pow(compounded, TRADING_DAYS_PER_YEAR / n) - 1 : null
  const maxDrawdown = n > 0 ? worstDrawdown : null

  let sharpe: number | null = null
  let sortino: number | null = null
  if (n > 1) {
    const excess = dailyReturns.map((r) => r - RISK_FREE_RATE_DAILY)
    const meanExcess = excess.reduce((a, b) => a + b, 0) / n
    const variance = excess.reduce((a, b) => a + (b - meanExcess) ** 2, 0) / (n - 1)
    const stdDev = Math.sqrt(variance)
    sharpe = stdDev ? (meanExcess / stdDev) * Math.sqrt(TRADING_DAYS_PER_YEAR) : null
    const downsideSqSum = excess.reduce((a, e) => a + Math.min(e, 0) ** 2, 0)
    const downsideDev = Math.sqrt(downsideSqSum / n)
    sortino = downsideDev ? (meanExcess / downsideDev) * Math.sqrt(TRADING_DAYS_PER_YEAR) : null
  }

  // Historical 95% daily VaR -- the 5th-percentile daily return, i.e. the
  // single-day loss this track record hasn't exceeded on 95% of its days.
  let dailyVar95: number | null = null
  if (n > 0) {
    const sorted = [...dailyReturns].sort((a, b) => a - b)
    dailyVar95 = sorted[Math.min(sorted.length - 1, Math.floor(0.05 * sorted.length))]
  }

  const inceptionDate = rows && rows.length > 0 ? rows[0].date : null

  // Most recent NAV -- the same figure the header stats' "Since Inception"/
  // "Annualized" are ultimately compounded against -- as the denominator
  // for each position's/sector's % of NAV below.
  const latestNav = rows && rows.length > 0 ? rows[rows.length - 1].nav : null

  // shares !== 0 (not just "in the positions map") -- a same-day
  // fully-closed-out position can still be a key with shares: 0, same
  // "position === 0 means flat, not held" reading PositionsView.tsx's own
  // closedTodayRows uses. Value falls back to avgCost when there's no live
  // tick yet for a ticker (server just (re)started) rather than dropping
  // the position from both cards below.
  const heldPositions = Object.entries(positions)
    .map(([ticker, pos]) => {
      const shares = pos.shares ?? null
      if (!shares) return null
      const price = prices[ticker]?.last ?? pos.avgCost ?? null
      const value = price !== null ? shares * price : null
      const info = tickerInfo[ticker]
      return { ticker, name: info?.name || ticker, sector: getSectorGroup(info?.sector), value }
    })
    .filter((r): r is { ticker: string; name: string; sector: string; value: number | null } => r !== null)

  const topLongPositions = heldPositions
    .filter((r) => (r.value ?? 0) > 0)
    .sort((a, b) => (b.value ?? 0) - (a.value ?? 0))
    .slice(0, 5)

  // Net exposure per sector (long positions add, short positions
  // subtract) -- same signed-sum convention PositionsView.tsx's own
  // SectorGroup.total uses. Sorted by gross size (|value|) so a large net
  // short sector surfaces alongside the large net longs, not buried at the
  // bottom by a naive descending sort on the signed value.
  const sectorExposure = (() => {
    const bySector = new Map<string, number>()
    for (const r of heldPositions) {
      if (r.value === null) continue
      bySector.set(r.sector, (bySector.get(r.sector) ?? 0) + r.value)
    }
    return [...bySector.entries()]
      .map(([sector, value]) => ({ sector, value }))
      .sort((a, b) => Math.abs(b.value) - Math.abs(a.value))
  })()

  return (
    <div className="factsheet-print-frame">
      <div className="positions-page portfolio-page factsheet-page" ref={pageRef}>
        <header className="masthead">
          <div className="title-block">
            <div className="title-with-info">
              <h1>Long / Short Equity Strategy</h1>
              {rows && rows.length > 0 && (
                <button type="button" className="print-btn no-print" onClick={handlePrint}>
                  <Printer size={14} />
                  <span>Print / Save PDF</span>
                </button>
              )}
            </div>
          </div>
          <img src={logo} alt="Lux Asset Management" className="factsheet-logo" />
        </header>

        {error && <p className="status-row">Couldn't load portfolio_performance.json — run: python ib_price_server.py performance</p>}
        {!error && !data && <p className="status-row">Loading…</p>}

        {rows && rows.length > 0 && (
          <div className="asset-card asset-summary">
            <h2>Executive Summary</h2>
            <p>
              LuxSire navigates the complexities of IT dynamics with compassionate support and expert guidance.
              Whether it's web, cloud or multi-platform development, we're here to empower your decisions with smart
              implementations. Our expertise ranges from multi-platform development through data integration to
              data analytics. From time to time, we make money for ourselves using our strategies, particularly
              "SEESAW": a systematic long/short single stock MT vs ST momentum strategy. 1800 stocks monitored and
              traded with a 24 hour maximum trade horizon.
            </p>
            <p>
              SEESAW is built around an in-house, fully quantitative screening system that re-ranks the entire
              1800-stock universe every trading session. Each name is scored across eighteen independent factors
              spanning valuation (sector-relative forward and trailing multiples, free cash flow, EV/EBITDA), price
              behavior (daily-timeframe momentum weighed against hourly-timeframe mean reversion), fundamentals
              (earnings-estimate revisions, analyst conviction, revenue growth, margins, leverage), and alternative
              data (news and social sentiment, insider open-market buying and selling, short interest,
              institutional ownership changes). No single factor is allowed to dominate the composite score, and
              every factor is ranked against its own peer universe rather than compared on raw magnitude, so one
              outlier can't distort the read on the rest of the book. The result is a live, ranked short list the
              team trades against intraday, re-scored fresh every session as new data arrives.
            </p>
          </div>
        )}

        {rows && rows.length > 0 && (
          <div className="factsheet-hero-row">
            <div className="factsheet-chart-col">
              <NavChart rows={rows} />
            </div>
            <div className="factsheet-stats-col">
              <div className="asset-card">
                <h2>Important Statistics</h2>
                <div className="factsheet-stat-list">
                  <StatRow label="Inception" value={fmtDate(inceptionDate)} />
                  <StatRow
                    label="Since Inception"
                    value={fmtPct(totalReturn)}
                    valueClass={(totalReturn ?? 0) >= 0 ? 'good' : 'bad'}
                  />
                  <StatRow
                    label="Annualized"
                    value={fmtPct(annualizedReturn)}
                    valueClass={(annualizedReturn ?? 0) >= 0 ? 'good' : 'bad'}
                  />
                  <StatRow
                    label="Sharpe"
                    value={fmtRatio(sharpe)}
                    valueClass={sharpe === null ? undefined : sharpe >= 0 ? 'good' : 'bad'}
                  />
                  <StatRow
                    label="Sortino"
                    value={fmtRatio(sortino)}
                    valueClass={sortino === null ? undefined : sortino >= 0 ? 'good' : 'bad'}
                  />
                  <StatRow
                    label="Max Drawdown"
                    value={fmtDrawdown(maxDrawdown)}
                    valueClass={maxDrawdown !== null && maxDrawdown > 0 ? 'bad' : undefined}
                    title="Largest peak-to-trough decline in the compounded equity curve"
                  />
                  <StatRow
                    label="Daily VaR (95%)"
                    value={fmtPct(dailyVar95)}
                    valueClass={dailyVar95 === null ? undefined : dailyVar95 >= 0 ? 'good' : 'bad'}
                    title="5th-percentile daily return -- the single-day loss not exceeded on 95% of days"
                  />
                </div>
              </div>

              <div className="asset-card">
                <h2>Top 5 Long Positions</h2>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th className="col-left">Ticker</th>
                        <th className="col-left">Sector</th>
                        <th>% NAV</th>
                      </tr>
                    </thead>
                    <tbody>
                      {topLongPositions.length === 0 && (
                        <tr className="status-row">
                          <td colSpan={3}>No long positions currently held (or ib_price_server.py's stream isn't running).</td>
                        </tr>
                      )}
                      {topLongPositions.map((p) => (
                        <tr key={p.ticker}>
                          <td className="col-left">
                            <a href={`#/asset/${encodeURIComponent(p.ticker)}`} className="ticker-link">
                              {p.ticker}
                            </a>
                          </td>
                          <td className="col-left">{sectorGroupLabel(p.sector)}</td>
                          <td className="num">{fmtExposurePct(p.value, latestNav)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          </div>
        )}

        {rows && rows.length > 0 && <MonthlyReturnsTable rows={rows} />}

        <div className="asset-two-col-row">
          <div className="asset-card">
            <h2>Sector Exposure</h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th className="col-left">Sector</th>
                    <th>% NAV</th>
                  </tr>
                </thead>
                <tbody>
                  {sectorExposure.length === 0 && (
                    <tr className="status-row">
                      <td colSpan={2}>No positions currently held (or ib_price_server.py's stream isn't running).</td>
                    </tr>
                  )}
                  {sectorExposure.map((s) => (
                    <tr key={s.sector}>
                      <td className="col-left">{sectorGroupLabel(s.sector)}</td>
                      <td className={`num ${s.value >= 0 ? 'good' : 'bad'}`}>{fmtExposurePct(s.value, latestNav)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="asset-card asset-summary">
            <h2>The Investment Manager</h2>
            <p>
              Lux Asset Management AG is a Swiss asset manager headquartered in Zurich, regulated by FINMA (the Swiss
              Financial Market Supervisory Authority) since 1999. Founded by a small team of derivatives traders and
              quantitative researchers, Lux built its early track record trading proprietary capital through the
              dot-com unwind and the 2008 credit crisis, refining the discipline behind its systematic long/short
              equity process well before offering it to outside investors. The firm operates today under FINMA's
              asset management license (FINIG/FinIA), with its investment team based in Zurich and a compliance and
              risk function that reports independently to the board. The philosophy has stayed consistent across two
              decades: capital preservation first, consistent absolute returns second, and no dependence on any
              single market direction to get there.
            </p>
          </div>
        </div>

        <div className="disclaimer">
          <h4>Disclaimer</h4>
          <p>
            The information contained herein is confidential and intended only for use by the recipient. This
            document and its contents must not be reproduced or distributed, either in whole or in part, nor may
            its contents be divulged by such persons to any other person without the prior written consent of Lux.
            Any unauthorized use, duplication or disclosure of this presentation is prohibited by law. The
            information contained herein is not complete and does not contain certain material information about
            the strategy, including important disclosures and risk factors associated with an investment in hedge
            funds, and is subject to change without notice.
          </p>
          <p>
            PAST PERFORMANCE IS NOT INDICATIVE OF FUTURE RESULTS OR A GUARANTEE OF FUTURE RETURNS. The performance
            of any portfolio investments discussed in this document is not necessarily indicative of the
            performance of any other of Lux's portfolio investments or any future performance, and investors
            should not assume that investments in the future will be profitable or will equal the performance of
            past portfolio investments. Investors should consider the content of this document in conjunction with
            the Fund's financial statements and other disclosures regarding the valuations and performance of the
            specific investments discussed herein.
          </p>
          <p>
            Unless otherwise indicated, all performance figures are unaudited. All exposure and performance
            figures are calculated in U.S. dollars and include the reinvestment of dividends, gains and other
            earnings. In furnishing these materials Lux does not undertake to update any of the information
            contained herein. Values may not sum due to rounding. Exposure and performance figures do not
            represent the exposure and performance of each individual investor, which may vary. Monthly exposure
            data and assets reflect month-end figures. Net returns are net of the Fund's management fees (1.50%
            annualized), fund expenses (subject to the Lux expense cap), and the incentive allocation (20% per
            annum). Although the incentive allocation is generally calculated at the end of a fiscal year or on a
            redemption date, the monthly net performance takes into account the accrual of the incentive
            allocation.
          </p>
          <p>
            The investments discussed do not represent all investments made by Lux. It should not be assumed that
            any of the investments discussed were or will be profitable, or that investments made in the future
            will be profitable or will equal the performance of the investments discussed herein. In addition,
            there can be no assurance that future funds will be able to make investments similar to the historic
            investments presented herein (because of economic conditions, the availability of investment
            opportunities and otherwise).
          </p>
          <p>
            The information provided herein reflects Lux's perspectives and beliefs. Any conclusions provided
            herein are based on various assumptions, any of which may prove to be incorrect. Certain content in
            these materials may have been generated or assisted by artificial intelligence tools. All materials
            are subject to human review and oversight.
          </p>
        </div>
      </div>
    </div>
  )
}
