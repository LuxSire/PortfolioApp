import { useEffect, useState } from 'react'
import { Area, AreaChart, CartesianGrid, ReferenceDot, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import MonthlyReturnsTable from './MonthlyReturnsTable'

function fmtMoney(v) {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+$' : '-$') + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })
}

// Same as fmtMoney but without the '$' — Realized/Unrealized/Total P&L
// columns already carry their currency in the header.
function fmtMoneyPlain(v) {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '-') + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })
}

// Cash/NAV are magnitudes, not a day's change — no +/- sign clutter.
function fmtLevel(v) {
  if (v === null || v === undefined) return '—'
  return '$' + v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

// Same as fmtLevel but without the '$' — the Stock Short column already
// carries its currency in the header.
function fmtLevelPlain(v) {
  if (v === null || v === undefined) return '—'
  return v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

// Daily return (Total P&L / prior-day NAV) — signed, 2 decimals.
function fmtPct(v) {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(2) + '%'
}

// Sharpe ratio — a plain signed ratio, not a dollar or percentage figure.
function fmtRatio(v) {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '') + v.toFixed(2)
}

// Volatility has no sign — it's a magnitude, not a direction — so it skips
// fmtPct's +/- prefix, same convention PositionsView.jsx's fmtVol uses.
function fmtVol(v) {
  if (v === null || v === undefined) return '—'
  return (v * 100).toFixed(2) + '%'
}

function fmtDate(iso) {
  if (!iso) return '—'
  const d = new Date(iso + 'T00:00:00')
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

function fmtAxisDate(iso) {
  const d = new Date(iso + 'T00:00:00')
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

const CHART_FONT = 'ui-monospace, "SF Mono", "Cascadia Code", "Roboto Mono", Menlo, monospace'
const CHART_TICK_STYLE = { fill: 'var(--muted)', fontSize: 10, fontFamily: CHART_FONT }

function NavTooltip({ active, payload }) {
  if (!active || !payload || !payload.length) return null
  const point = payload[0].payload
  return (
    <div className="chart-tooltip">
      <span className="chart-tooltip-value">{fmtLevel(point.nav)}</span>
      <span className="chart-tooltip-date">{fmtAxisDate(point.date)}</span>
    </div>
  )
}

// NAV over every day the Flex Query covers — same chart anatomy as
// Asset.jsx's PriceChart (dataviz skill: one hue, thin line, light fill,
// a handful of deduped x-ticks) so it reads consistently with the rest of
// the app, just plotting account NAV instead of a single stock's price.
function NavChart({ rows }) {
  const navs = rows.map((r) => r.nav).filter((v) => v !== null)
  const lo = Math.min(...navs)
  const hi = Math.max(...navs)
  const domainPad = (hi - lo || 1) * 0.12
  const yMin = lo - domainPad
  const yMax = hi + domainPad

  const first = rows[0]
  const last = rows[rows.length - 1]
  const changePct = first.nav ? last.nav / first.nav - 1 : null

  const xTickCount = Math.min(5, rows.length)
  const xTicks = [
    ...new Set(
      Array.from({ length: xTickCount }, (_, i) =>
        rows[Math.round((i * (rows.length - 1)) / (xTickCount - 1 || 1))].date
      )
    ),
  ]

  return (
    <div className="asset-card">
      <h2>
        Net Asset Value
        <span className="chart-last-price">{fmtLevel(last.nav)}</span>
        {changePct !== null && (
          <span className={`chart-change ${changePct >= 0 ? 'good' : 'bad'}`}>
            {(changePct >= 0 ? '+' : '') + (changePct * 100).toFixed(1)}%
          </span>
        )}
      </h2>
      <div className="chart-wrap">
        <ResponsiveContainer width="100%" height={260}>
          <AreaChart data={rows} margin={{ top: 12, right: 4, bottom: 4, left: 4 }}>
            <CartesianGrid stroke="var(--line)" vertical={false} />
            <XAxis
              dataKey="date"
              type="category"
              ticks={xTicks}
              tickFormatter={fmtAxisDate}
              tick={CHART_TICK_STYLE}
              axisLine={false}
              tickLine={false}
            />
            <YAxis
              domain={[yMin, yMax]}
              ticks={[lo, hi]}
              tickFormatter={fmtLevel}
              orientation="right"
              width={64}
              axisLine={false}
              tickLine={false}
              tick={CHART_TICK_STYLE}
            />
            <Tooltip content={<NavTooltip />} cursor={{ stroke: 'var(--muted)', strokeOpacity: 0.5 }} />
            <Area
              type="monotone"
              dataKey="nav"
              stroke="var(--accent)"
              strokeWidth={2}
              fill="var(--accent)"
              fillOpacity={0.1}
              dot={false}
              activeDot={{ r: 4, fill: 'var(--accent)', stroke: 'var(--surface)', strokeWidth: 2 }}
              isAnimationActive={false}
              connectNulls
            />
            <ReferenceDot x={last.date} y={last.nav} r={4} fill="var(--accent)" stroke="var(--surface)" strokeWidth={2} />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}

// The extracts of the IBKR Flex Query configured in ib_price_server.py's
// fetch_account_performance — real, IB-computed daily cash/NAV/realized/
// unrealized (see Results.csv for the exported reference shape), not
// derived from the screener's own price data. IBKR concatenates one full
// copy of its configured report sections per calendar day for a multi-day
// query, joined here by date into a single row per day.
export default function PortfolioView() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    fetch('/portfolio_performance.json')
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then(setData)
      .catch(() => setError(true))
  }, [])

  const rows = data?.kind === 'daily' ? data.rows : null
  // Running total of realized+unrealized through each day, keyed by date,
  // plus each day's own return (that day's Total P&L over the PRIOR day's
  // NAV — the base capital that P&L was actually earned on) — rows is
  // already date-ascending, so a single pass gives both. prevNav only
  // advances on a day with a real NAV (same rule the backend's own
  // _apply_unrealized_from_nav uses), so a gap in the data doesn't
  // corrupt the next real day's return.
  const cumulativePnlByDate = {}
  const dailyReturnByDate = {}
  // Geometric compounding of every day's return through that day (the
  // product of (1 + dailyReturn), minus 1) — the track record's total
  // return to date, not a simple running sum of daily returns, which is
  // not how percentage returns actually combine over time. Same
  // compounding MonthlyReturnsTable uses, just never reset (one running
  // total across the whole window instead of restarting each month).
  const cumulativeReturnByDate = {}
  const dailyReturns = []
  if (rows) {
    let running = 0
    let compounded = 1
    let prevNav = null
    for (const r of rows) {
      const dayTotalPnl = r.realized !== null && r.unrealized !== null ? r.realized + r.unrealized : null
      if (dayTotalPnl !== null) running += dayTotalPnl
      cumulativePnlByDate[r.date] = running

      const dailyReturn = dayTotalPnl !== null && prevNav ? dayTotalPnl / prevNav : null
      dailyReturnByDate[r.date] = dailyReturn
      if (dailyReturn !== null) {
        dailyReturns.push(dailyReturn)
        compounded *= 1 + dailyReturn
      }
      cumulativeReturnByDate[r.date] = compounded - 1

      if (r.nav !== null) prevNav = r.nav
    }
  }
  // Cumulative across every day shown, not a single day's figure — "how
  // much have I actually locked in / paid / moved over this whole window."
  const sumField = (field) => (rows ? rows.reduce((s, r) => s + (r[field] ?? 0), 0) : null)
  const totalRealized = sumField('realized')
  const totalUnrealized = sumField('unrealized')
  const totalPnl = rows ? totalRealized + totalUnrealized : null
  const totalCommissions = sumField('commissions')
  const totalDividends = sumField('dividends')
  const totalInterest = sumField('interest')
  const totalDepositsWithdrawals = sumField('depositsWithdrawals')

  // Both ratios use the same daily-return series as the new % column
  // above, and the same 3.5%/yr risk-free rate as the "excess return" in
  // their numerator — Sharpe divides that by total volatility, Sortino
  // by downside volatility only (upside swings aren't "risk"). 252
  // trading days/year for annualizing, same convention IBApp's own
  // momentum score uses.
  const TRADING_DAYS_PER_YEAR = 252
  const RISK_FREE_RATE_ANNUAL = 0.035
  const RISK_FREE_RATE_DAILY = RISK_FREE_RATE_ANNUAL / TRADING_DAYS_PER_YEAR
  let sharpe = null
  let sortino = null
  // Annualized volatility (std dev of daily returns × √252) — the same
  // denominator (before dividing into meanExcess) that produces Sharpe
  // below, surfaced on its own since "how volatile" and "risk-adjusted
  // return" are two different questions even though one feeds the other.
  // Subtracting the risk-free rate from every day doesn't change the std
  // dev (variance is shift-invariant), so this is identical whether
  // computed from excess or raw daily returns.
  let annualizedVol = null
  if (dailyReturns.length > 1) {
    const n = dailyReturns.length
    const excess = dailyReturns.map((r) => r - RISK_FREE_RATE_DAILY)
    const meanExcess = excess.reduce((a, b) => a + b, 0) / n

    // Sample variance (n-1), same convention PositionsView.jsx's
    // portfolioDailyVolatility uses.
    const variance = excess.reduce((a, b) => a + (b - meanExcess) ** 2, 0) / (n - 1)
    const stdDev = Math.sqrt(variance)
    annualizedVol = stdDev * Math.sqrt(TRADING_DAYS_PER_YEAR)
    sharpe = stdDev ? (meanExcess / stdDev) * Math.sqrt(TRADING_DAYS_PER_YEAR) : null

    // Classic Sortino & van der Meer definition: downside deviation
    // divides by the FULL sample size N (treating every upside day as a
    // zero deviation), not just the count of downside days — otherwise
    // an upside-heavy return series would get an artificially punishing
    // denominator from having "too few" bad observations to average over.
    const downsideSqSum = excess.reduce((a, e) => a + Math.min(e, 0) ** 2, 0)
    const downsideDev = Math.sqrt(downsideSqSum / n)
    sortino = downsideDev ? (meanExcess / downsideDev) * Math.sqrt(TRADING_DAYS_PER_YEAR) : null
  }

  return (
    <div className="positions-page portfolio-page">
      <header className="masthead">
        <div className="title-block">
          <h1>Portfolio</h1>
        </div>
        {rows && rows.length > 0 && (
          <div className="stat-row">
            <div className="stat">
              <span className={`n num${totalPnl >= 0 ? ' good' : ' bad'}`}>{fmtMoney(totalPnl)}</span>
              <span className="l">Total P&amp;L</span>
            </div>
            <div className="stat">
              <span className={`n num${totalRealized >= 0 ? ' good' : ' bad'}`}>{fmtMoney(totalRealized)}</span>
              <span className="l">Realized</span>
            </div>
            <div className="stat">
              <span className={`n num${totalCommissions >= 0 ? ' good' : ' bad'}`}>{fmtMoney(totalCommissions)}</span>
              <span className="l">Commissions</span>
            </div>
            <div className="stat">
              <span className={`n num${totalDividends >= 0 ? ' good' : ' bad'}`}>{fmtMoney(totalDividends)}</span>
              <span className="l">Dividends</span>
            </div>
            <div className="stat">
              <span className={`n num${totalInterest >= 0 ? ' good' : ' bad'}`}>{fmtMoney(totalInterest)}</span>
              <span className="l">Interest</span>
            </div>
            <div className="stat">
              <span className={`n num${totalDepositsWithdrawals >= 0 ? ' good' : ' bad'}`}>
                {fmtMoney(totalDepositsWithdrawals)}
              </span>
              <span className="l">Flows</span>
            </div>
            <div
              className="stat"
              title="Annualized volatility: std dev of daily (Total P&L / prior-day NAV) returns — × √252 trading days/year. The same denominator Sharpe (next) divides its excess return by."
            >
              <span className="n num">{fmtVol(annualizedVol)}</span>
              <span className="l">Volatility</span>
            </div>
            <div
              className="stat"
              title="Annualized Sharpe ratio: mean daily (Total P&L / prior-day NAV) return, minus a 3.5%/yr risk-free rate, over its own volatility (std dev) — × √252 trading days/year."
            >
              <span className={`n num${sharpe === null ? '' : sharpe >= 0 ? ' good' : ' bad'}`}>
                {fmtRatio(sharpe)}
              </span>
              <span className="l">Sharpe</span>
            </div>
            <div
              className="stat"
              title="Annualized Sortino ratio: same excess return as Sharpe, but over downside volatility only (upside swings aren't risk) — 3.5%/yr risk-free rate, × √252 trading days/year."
            >
              <span className={`n num${sortino === null ? '' : sortino >= 0 ? ' good' : ' bad'}`}>
                {fmtRatio(sortino)}
              </span>
              <span className="l">Sortino</span>
            </div>
          </div>
        )}
      </header>

      {error && <p className="status-row">Couldn't load portfolio_performance.json — run: python ib_price_server.py performance</p>}
      {!error && !data && <p className="status-row">Loading…</p>}

      {rows && rows.length > 0 && <NavChart rows={rows} />}
      {rows && rows.length > 0 && <MonthlyReturnsTable rows={rows} />}

      {rows && (
        <div className="table-wrap positions-table-wrap">
          <table>
            <thead>
              <tr>
                <th className="col-left">Date</th>
                <th>Cash</th>
                <th>NAV</th>
                <th>Stock Long</th>
                <th>Stock Short</th>
                <th>Net</th>
                <th>Gross</th>
                <th>Flows</th>
                <th>Commissions</th>
                <th>Dividends</th>
                <th>Interest</th>
                <th>Realized</th>
                <th>Unrealized</th>
                <th>Total P&amp;L</th>
                <th title="Total P&L / prior-day NAV">Total P&amp;L %</th>
                <th>Cumulative P&amp;L</th>
                <th title="Compounded Total P&L % return since the start of this track record">Cumulative %</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 && (
                <tr className="status-row">
                  <td colSpan={17}>No daily rows in the query response.</td>
                </tr>
              )}
              {[...rows].reverse().map((r) => {
                const totalPnl = r.realized !== null && r.unrealized !== null ? r.realized + r.unrealized : null
                const cumulativePnl = cumulativePnlByDate[r.date]
                const dailyReturn = dailyReturnByDate[r.date]
                const cumulativeReturn = cumulativeReturnByDate[r.date]
                return (
                  <tr key={r.date}>
                    <td className="col-left">{fmtDate(r.date)}</td>
                    <td className="num">{fmtLevel(r.cash)}</td>
                    <td className="num">{fmtLevel(r.nav)}</td>
                    <td className="num">{fmtLevel(r.stockLong)}</td>
                    <td className="num">{fmtLevelPlain(r.stockShort)}</td>
                    <td className="num">{fmtLevel(r.stockNet)}</td>
                    <td className="num">{fmtLevel(r.stockGross)}</td>
                    <td className="num">{fmtMoneyPlain(r.depositsWithdrawals)}</td>
                    <td className="num">{fmtMoneyPlain(r.commissions)}</td>
                    <td className="num">{fmtMoneyPlain(r.dividends)}</td>
                    <td className="num">{fmtMoneyPlain(r.interest)}</td>
                    <td className={`num ${r.realized === null ? '' : r.realized >= 0 ? 'good' : 'bad'}`}>
                      {fmtMoneyPlain(r.realized)}
                    </td>
                    <td className={`num ${r.unrealized === null ? '' : r.unrealized >= 0 ? 'good' : 'bad'}`}>
                      {fmtMoneyPlain(r.unrealized)}
                    </td>
                    <td className={`num ${totalPnl === null ? '' : totalPnl >= 0 ? 'good' : 'bad'}`}>
                      {fmtMoneyPlain(totalPnl)}
                    </td>
                    <td className={`num ${dailyReturn === null || dailyReturn === undefined ? '' : dailyReturn >= 0 ? 'good' : 'bad'}`}>
                      {fmtPct(dailyReturn)}
                    </td>
                    <td className={`num ${cumulativePnl >= 0 ? 'good' : 'bad'}`}>{fmtMoneyPlain(cumulativePnl)}</td>
                    <td className={`num ${cumulativeReturn === null || cumulativeReturn === undefined ? '' : cumulativeReturn >= 0 ? 'good' : 'bad'}`}>
                      {fmtPct(cumulativeReturn)}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
