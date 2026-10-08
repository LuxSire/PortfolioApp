import { useEffect, useState } from 'react'
import type { PortfolioDayRow, PortfolioPerformanceData } from '../interfaces/IPortfolioView'
import ExposureChart from '../components/ExposureChart'
import { useCashEquivalentValues } from '../cashEquivalentHistory'
import MonthlyReturnsTable from '../components/MonthlyReturnsTable'
import NavChart from '../components/NavChart'
import { dayTotalPnl } from '../portfolioPnl'

function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+$' : '-$') + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })
}

// Same as fmtMoney but without the '$' — Realized/Unrealized/Total P&L
// columns already carry their currency in the header.
function fmtMoneyPlain(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '-') + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })
}

// Cash/NAV are magnitudes, not a day's change — no +/- sign clutter.
function fmtLevel(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return '$' + v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

// Stock Long/Short/Net as a % of that day's NAV rather than a raw dollar
// figure — exposure is more legible normalized against book size than as
// an absolute amount. Same normalization ExposureChart.jsx now uses for
// its bars.
function fmtExposurePct(v: number | null | undefined, nav: number | null | undefined): string {
  if (v === null || v === undefined || nav === null || nav === undefined || nav === 0) return '—'
  return ((v / nav) * 100).toFixed(1) + '%'
}

// Daily return (Total P&L / prior-day NAV) — signed, 2 decimals.
function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(2) + '%'
}

// Sharpe ratio — a plain signed ratio, not a dollar or percentage figure.
function fmtRatio(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '') + v.toFixed(2)
}

// Volatility has no sign — it's a magnitude, not a direction — so it skips
// fmtPct's +/- prefix, same convention PositionsView.tsx's fmtVol uses.
function fmtVol(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return (v * 100).toFixed(2) + '%'
}

// Max drawdown is stored as a positive magnitude (0 = never declined from
// a prior peak) but always represents a decline, so — unlike fmtVol — it
// gets a fixed leading '-' rather than a sign that could ever read as '+'.
function fmtDrawdown(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return '-' + (v * 100).toFixed(2) + '%'
}

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso + 'T00:00:00')
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

// The extracts of the IBKR Flex Query configured in ib_server.py's
// fetch_account_performance — real, IB-computed daily cash/NAV/realized/
// unrealized (see Results.csv for the exported reference shape), not
// derived from the screener's own price data. IBKR concatenates one full
// copy of its configured report sections per calendar day for a multi-day
// query, joined here by date into a single row per day.
const PORTFOLIO_START_DATE = '2026-09-28'

export default function PortfolioView() {
  const [data, setData] = useState<PortfolioPerformanceData | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    fetch('/portfolio_performance.json')
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then(setData)
      .catch(() => setError(true))
  }, [])

  const allRows: PortfolioDayRow[] | null = data?.kind === 'daily' ? (data.rows ?? null) : null
  // Explicit instruction: EVERYTHING on this page (stats, charts, monthly
  // and daily tables) starts on a FIXED date -- Monday 28 Sep 2026, the day
  // the new capital came in (same start as the Factsheet) -- a
  // display-only trim applied client-side, not by rewriting
  // portfolio_performance.json, so the stored history is never at risk.
  // The last NAV before Monday is kept only as the base for Monday's own
  // return (P&L over the prior day's NAV, same rule as everywhere else),
  // so a deposit/withdrawal on Monday isn't counted as performance.
  const periodStart = PORTFOLIO_START_DATE
  const rows: PortfolioDayRow[] | null = allRows ? allRows.filter((r) => r.date >= periodStart) : null
  const baselineNav: number | null =
    [...(allRows ?? [])].reverse().find((r) => r.date < periodStart && r.nav !== null)?.nav ?? null
  const chartRows = rows
  // cash-equivalent holdings per day, taken out of Long and shown with cash in the Exposure chart
  const cashEqByDate = useCashEquivalentValues((allRows ?? []).map((r) => r.date))
  // Running total of realized+unrealized through each day, keyed by date,
  // plus each day's own return (that day's Total P&L over the PRIOR day's
  // NAV — the base capital that P&L was actually earned on) — rows is
  // already date-ascending, so a single pass gives both. prevNav only
  // advances on a day with a real NAV (same rule the backend's own
  // _apply_unrealized_from_nav uses), so a gap in the data doesn't
  // corrupt the next real day's return.
  const cumulativePnlByDate: Record<string, number> = {}
  const dailyReturnByDate: Record<string, number | null> = {}
  // Geometric compounding of every day's return through that day (the
  // product of (1 + dailyReturn), minus 1) — the track record's total
  // return to date, not a simple running sum of daily returns, which is
  // not how percentage returns actually combine over time. Same
  // compounding MonthlyReturnsTable uses, just never reset (one running
  // total across the whole window instead of restarting each month).
  const cumulativeReturnByDate: Record<string, number> = {}
  const dailyReturns: number[] = []
  // Peak-to-trough decline of the same compounded equity curve as
  // cumulativeReturnByDate above (running product of (1 + dailyReturn)),
  // not of raw NAV — NAV moves on deposits/withdrawals too, which would
  // read as a "drawdown" they aren't. Tracked as a positive magnitude (the
  // largest fractional drop from any prior peak to a later trough), null
  // until there's at least one real daily return to measure.
  let maxDrawdown: number | null = null
  if (rows) {
    let running = 0
    let compounded = 1
    let peakCompounded = 1
    let worstDrawdown = 0
    let prevNav: number | null = baselineNav
    for (const r of rows) {
      const dayPnl = dayTotalPnl(r)
      if (dayPnl !== null) running += dayPnl
      cumulativePnlByDate[r.date] = running

      const dailyReturn = dayPnl !== null && prevNav ? dayPnl / prevNav : null
      dailyReturnByDate[r.date] = dailyReturn
      if (dailyReturn !== null) {
        dailyReturns.push(dailyReturn)
        compounded *= 1 + dailyReturn
        if (compounded > peakCompounded) peakCompounded = compounded
        const drawdown = (peakCompounded - compounded) / peakCompounded
        if (drawdown > worstDrawdown) worstDrawdown = drawdown
      }
      cumulativeReturnByDate[r.date] = compounded - 1

      if (r.nav !== null) prevNav = r.nav
    }
    if (dailyReturns.length > 0) maxDrawdown = worstDrawdown
  }
  // Cumulative across every day shown, not a single day's figure — "how
  // much have I actually locked in / paid / moved over this whole window."
  const sumField = (field: keyof PortfolioDayRow): number | null =>
    rows ? rows.reduce((s, r) => s + ((r[field] as number | null) ?? 0), 0) : null
  const totalRealized = sumField('realized')
  const totalUnrealized = sumField('unrealized')
  const totalCommissions = sumField('commissions')
  const totalDividends = sumField('dividends')
  const totalWithholdingTax = sumField('withholdingTax')
  const totalInterest = sumField('interest')
  // realized + unrealized + commissions + dividends + interest + withholding tax
  const totalPnl =
    rows && totalRealized !== null && totalUnrealized !== null
      ? totalRealized + totalUnrealized + (totalCommissions ?? 0) + (totalDividends ?? 0) + (totalInterest ?? 0) + (totalWithholdingTax ?? 0)
      : null
  const totalDepositsWithdrawals = sumField('depositsWithdrawals')

  // Both ratios use the same daily-return series as the new % column
  // above, and NO risk-free rate (explicit instruction: the book holds a
  // treasury / cash-equivalent allocation) in
  // their numerator — Sharpe divides that by total volatility, Sortino
  // by downside volatility only (upside swings aren't "risk"). 252
  // trading days/year for annualizing, same convention IBApp's own
  // momentum score uses.
  const TRADING_DAYS_PER_YEAR = 252
  const RISK_FREE_RATE_ANNUAL = 0
  const RISK_FREE_RATE_DAILY = RISK_FREE_RATE_ANNUAL / TRADING_DAYS_PER_YEAR
  let sharpe: number | null = null
  let sortino: number | null = null
  // Annualized volatility (std dev of daily returns × √252) — the same
  // denominator (before dividing into meanExcess) that produces Sharpe
  // below, surfaced on its own since "how volatile" and "risk-adjusted
  // return" are two different questions even though one feeds the other.
  // Subtracting the risk-free rate from every day doesn't change the std
  // dev (variance is shift-invariant), so this is identical whether
  // computed from excess or raw daily returns.
  let annualizedVol: number | null = null
  // Annualized performance: mean RAW daily return (not excess-of-risk-free)
  // × √252's own counterpart for a mean, × TRADING_DAYS_PER_YEAR -- same
  // arithmetic-mean annualization convention Sharpe/Sortino's own
  // meanExcess numerator already uses below, just without subtracting the
  // risk-free rate, since this stat answers "how did the account actually
  // perform," not "how did it perform net of a risk-free alternative."
  let annualizedReturn: number | null = null
  if (dailyReturns.length > 1) {
    const n = dailyReturns.length
    const meanReturn = dailyReturns.reduce((a, b) => a + b, 0) / n
    annualizedReturn = meanReturn * TRADING_DAYS_PER_YEAR
    const excess = dailyReturns.map((r) => r - RISK_FREE_RATE_DAILY)
    const meanExcess = excess.reduce((a, b) => a + b, 0) / n

    // Sample variance (n-1), same convention PositionsView.tsx's
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
              <span className={`n num${(totalPnl ?? 0) >= 0 ? ' good' : ' bad'}`}>{fmtMoney(totalPnl)}</span>
              <span className="l">Total P&amp;L</span>
            </div>
            <div className="stat">
              <span className={`n num${(totalRealized ?? 0) >= 0 ? ' good' : ' bad'}`}>{fmtMoney(totalRealized)}</span>
              <span className="l">Realized</span>
            </div>
            <div className="stat">
              <span className={`n num${(totalCommissions ?? 0) >= 0 ? ' good' : ' bad'}`}>
                {fmtMoney(totalCommissions)}
              </span>
              <span className="l">Commissions</span>
            </div>
            <div className="stat">
              <span className={`n num${(totalDividends ?? 0) >= 0 ? ' good' : ' bad'}`}>{fmtMoney(totalDividends)}</span>
              <span className="l">Dividends</span>
            </div>
            <div className="stat" title="Tax withheld on dividends and interest (negative). Shown only if the IBKR Flex query includes Withholding Tax in its Change in NAV section.">
              <span className={`n num${(totalWithholdingTax ?? 0) >= 0 ? '' : ' bad'}`}>{fmtMoney(totalWithholdingTax)}</span>
              <span className="l">WT</span>
            </div>
            <div className="stat">
              <span className={`n num${(totalInterest ?? 0) >= 0 ? ' good' : ' bad'}`}>{fmtMoney(totalInterest)}</span>
              <span className="l">Interest</span>
            </div>
            <div className="stat">
              <span className={`n num${(totalDepositsWithdrawals ?? 0) >= 0 ? ' good' : ' bad'}`}>
                {fmtMoney(totalDepositsWithdrawals)}
              </span>
              <span className="l">Flows</span>
            </div>
            <div
              className="stat"
              title="Annualized performance: mean daily (Total P&L / prior-day NAV) return × 252 trading days/year — the raw return this track record annualizes to, before any risk adjustment."
            >
              <span className={`n num${annualizedReturn === null ? '' : annualizedReturn >= 0 ? ' good' : ' bad'}`}>
                {fmtPct(annualizedReturn)}
              </span>
              <span className="l">Ann. Perf.</span>
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
              title="Annualized Sharpe ratio: mean daily (Total P&L / prior-day NAV) return, with no risk-free rate subtracted (the book holds a treasury allocation), over its own volatility (std dev) — × √252 trading days/year."
            >
              <span className={`n num${sharpe === null ? '' : sharpe >= 0 ? ' good' : ' bad'}`}>
                {fmtRatio(sharpe)}
              </span>
              <span className="l">Sharpe</span>
            </div>
            <div
              className="stat"
              title="Annualized Sortino ratio: same return as Sharpe, but over downside volatility only (upside swings aren't risk) — no risk-free rate, × √252 trading days/year."
            >
              <span className={`n num${sortino === null ? '' : sortino >= 0 ? ' good' : ' bad'}`}>
                {fmtRatio(sortino)}
              </span>
              <span className="l">Sortino</span>
            </div>
            <div
              className="stat"
              title="Largest peak-to-trough decline in the compounded (Total P&L / prior-day NAV) equity curve — the worst drawdown this track record has experienced, not a single day's loss."
            >
              <span className={`n num${maxDrawdown === null ? '' : maxDrawdown > 0 ? ' bad' : ''}`}>
                {fmtDrawdown(maxDrawdown)}
              </span>
              <span className="l">Max Drawdown</span>
            </div>
          </div>
        )}
      </header>

      {error && <p className="status-row">Couldn't load portfolio_performance.json — run: python ib_server.py performance</p>}
      {!error && !data && <p className="status-row">Loading…</p>}

      {chartRows && chartRows.length > 0 && <NavChart
          rows={chartRows}
          indexByDate={Object.fromEntries(chartRows.map((r) => [r.date, (1 + (cumulativeReturnByDate[r.date] ?? 0)) * 100]))}
        />}
      {chartRows && chartRows.length > 0 && <ExposureChart rows={chartRows.map((r) => ({ ...r, cashEquivalents: cashEqByDate[r.date] ?? 0 }))} />}
      {rows && rows.length > 0 && <MonthlyReturnsTable rows={rows} baselineNav={baselineNav} />}

      {rows && (
        <div className="table-wrap positions-table-wrap">
          <table>
            <thead>
              <tr>
                <th className="col-left">Date</th>
                <th>Cash</th>
                <th title="Market value of the cash-equivalent holdings (IB01, SGOV, SHV, ...), rebuilt from the fills -- kept out of Long, Net and Gross">Cash equiv.</th>
                <th>NAV</th>
                <th title="(Stock Long − cash equivalents) / NAV">Stock Long %</th>
                <th title="Stock Short / NAV">Stock Short %</th>
                <th title="Long (ex cash equivalents) + Short">Net $</th>
                <th title="Net / NAV">Net %</th>
                <th title="Long (ex cash equivalents) + |Short|">Gross $</th>
                <th title="Gross / NAV">Gross %</th>
                <th>Flows</th>
                <th>Commissions</th>
                <th>Dividends</th>
                <th title="Withholding tax">WT</th>
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
                  <td colSpan={21}>No daily rows in the query response.</td>
                </tr>
              )}
              {[...rows].reverse().map((r) => {
                const totalPnl = dayTotalPnl(r)
                const cumulativePnl = cumulativePnlByDate[r.date]
                const dailyReturn = dailyReturnByDate[r.date]
                const cumulativeReturn = cumulativeReturnByDate[r.date]
                // Cash-equivalent holdings are not exposure -- explicit instruction: taken out of Long
                // (the Flex Query's stockLong includes them), hence also out of Net and Gross.
                const cashEq = cashEqByDate[r.date] ?? 0
                const longAdj = r.stockLong === null ? null : Math.max(0, r.stockLong - cashEq)
                const netAdj = longAdj !== null && r.stockShort !== null ? longAdj + r.stockShort : r.stockNet
                const grossAdj = longAdj !== null && r.stockShort !== null ? longAdj + Math.abs(r.stockShort) : r.stockGross
                return (
                  <tr key={r.date}>
                    <td className="col-left">{fmtDate(r.date)}</td>
                    <td className="num">{fmtLevel(r.cash)}</td>
                    <td className="num">{fmtLevel(cashEq)}</td>
                    <td className="num">{fmtLevel(r.nav)}</td>
                    <td className="num">{fmtExposurePct(longAdj, r.nav)}</td>
                    <td className="num">{fmtExposurePct(r.stockShort, r.nav)}</td>
                    <td className="num">{fmtMoneyPlain(netAdj)}</td>
                    <td className="num">{fmtExposurePct(netAdj, r.nav)}</td>
                    <td className="num">{fmtMoneyPlain(grossAdj)}</td>
                    <td className="num">{fmtExposurePct(grossAdj, r.nav)}</td>
                    <td className="num">{fmtMoneyPlain(r.depositsWithdrawals)}</td>
                    <td className="num">{fmtMoneyPlain(r.commissions)}</td>
                    <td className="num">{fmtMoneyPlain(r.dividends)}</td>
                    <td className="num">{fmtMoneyPlain(r.withholdingTax ?? null)}</td>
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
