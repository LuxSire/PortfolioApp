import { useEffect, useMemo, useState } from 'react'
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
// Backtest tabs (explicit instruction): the backtest's three Strong Buy + Strong Sell
// portfolios -- the 10% rating cut and the nested 5% / 2.5% cuts.
const BACKTEST_TABS = [
  { key: 'portfolio', label: 'Backtest · Portfolio 10%', name: 'Portfolio (Strong Buy + Strong Sell)' },
  { key: 'portfolioRestricted4', label: 'Backtest · Portfolio 5%', name: 'Portfolio (Strong Buy + Strong Sell) (5%)' },
  { key: 'portfolioRestricted2', label: 'Backtest · Portfolio 2.5%', name: 'Portfolio (Strong Buy + Strong Sell) (2.5%)' },
] as const
type BacktestSeriesKey = (typeof BACKTEST_TABS)[number]['key']
type BacktestJson = { weeks?: { week: string; currentModel?: { daily?: { dates: string[]; series: Record<string, (number | null)[]> } } }[] }
const BACKTEST_NOTIONAL_NAV = 100 // = the chart's 100 base, so the backtest NAV column IS the index
const PORTFOLIO_START_DATE = '2026-09-24' // explicit instruction: the whole page starts Thursday 24 Sep
// Explicit instruction: the NAV chart includes Friday 25 Sep 2026's own return: it is
// anchored at 100 on the close before it (Thursday 24 Sep) and compounds the daily
// returns (Total P&L / prior-day NAV) from Friday onward -- a time-weighted index,
// never IB's NAV ratio.

export default function PortfolioView() {
  const [data, setData] = useState<PortfolioPerformanceData | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    fetch('/portfolio_performance.json')
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then(setData)
      .catch(() => setError(true))
  }, [])

  // Explicit instruction: a second tab shows the SAME page for the backtest's
  // "Portfolio (Strong Buy + Strong Sell) (2.5%)" daily returns (backtest.json,
  // currentModel.daily.series.portfolioRestricted2), stitched across the weekly
  // windows (a later week wins on an overlapping date) and turned into a notional
  // account: NAV starts at BACKTEST_NOTIONAL_NAV and each day's P&L is NAV x return,
  // so every stat/chart/table below works unchanged (no cash, flows or fees).
  const [source, setSource] = useState<'live' | BacktestSeriesKey>('live')
  const isBacktest = source !== 'live'
  const [backtestJson, setBacktestJson] = useState<BacktestJson | null>(null)
  const [backtestError, setBacktestError] = useState(false)
  useEffect(() => {
    if (!isBacktest || backtestJson) return
    fetch('/backtest.json')
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then(setBacktestJson)
      .catch(() => setBacktestError(true))
  }, [isBacktest, backtestJson])
  const backtestRows: PortfolioDayRow[] | null = useMemo(() => {
    if (!isBacktest) return null
    if (backtestError) return []
    if (!backtestJson) return null
    const byDate: Record<string, number> = {}
    for (const w of [...(backtestJson.weeks ?? [])].sort((a, b) => a.week.localeCompare(b.week))) {
      const d = w.currentModel?.daily
      const series = d?.series?.[source]
      if (!d || !series) continue
      d.dates.forEach((date, i) => {
        const v = series[i]
        if (v !== null && v !== undefined) byDate[date] = v
      })
    }
    const dates = Object.keys(byDate).sort()
    const out: PortfolioDayRow[] = []
    if (dates.length) {
      const blank = { cash: null, stockLong: null, stockShort: null, stockNet: null, stockGross: null }
      const first = new Date(dates[0] + 'T12:00:00')
      first.setDate(first.getDate() - 1)
      let nav = BACKTEST_NOTIONAL_NAV
      out.push({ date: first.toISOString().slice(0, 10), ...blank, nav, depositsWithdrawals: 0, commissions: 0, dividends: 0, interest: 0, realized: null, unrealized: null })
      for (const date of dates) {
        const pnl = nav * byDate[date]
        nav += pnl
        out.push({ date, ...blank, nav, depositsWithdrawals: 0, commissions: 0, dividends: 0, interest: 0, realized: pnl, unrealized: 0 })
      }
    }
    return out
  }, [isBacktest, source, backtestJson, backtestError])
  const backtestTab = BACKTEST_TABS.find((t) => t.key === source)

  const liveRows: PortfolioDayRow[] | null = data?.kind === 'daily' ? (data.rows ?? null) : null
  const allRows: PortfolioDayRow[] | null = source === 'live' ? liveRows : backtestRows
  // Explicit instruction: EVERYTHING on this page (stats, charts, monthly
  // and daily tables) starts on a FIXED date -- Thursday 24 Sep 2026 (moved back
  // from Monday 28 Sep, the day the new capital came in, by explicit instruction) -- a
  // display-only trim applied client-side, not by rewriting
  // portfolio_performance.json, so the stored history is never at risk.
  // The last NAV before Monday is kept only as the base for Monday's own
  // return (P&L over the prior day's NAV, same rule as everywhere else),
  // so a deposit/withdrawal on Monday isn't counted as performance.
  // Backtest tab (explicit instruction): starts on the backtest's own first day
  // (backtestRows[0] is the synthetic 100 anchor the day before it).
  const periodStart = isBacktest ? (backtestRows?.[1]?.date ?? PORTFOLIO_START_DATE) : PORTFOLIO_START_DATE
  const rows: PortfolioDayRow[] | null = allRows ? allRows.filter((r) => r.date >= periodStart) : null
  const baselineNav: number | null =
    [...(allRows ?? [])].reverse().find((r) => r.date < periodStart && r.nav !== null)?.nav ?? null
  const chartRows = rows
  // NAV chart: the anchor day (100, its own P&L not counted -- that day is before the
  // track record) followed by every day of the track record.
  const preRows = (allRows ?? []).filter((r) => r.date < periodStart)
  const extraDays = preRows.filter((r) => r.date >= periodStart) // none: the chart starts with the page
  const anchorRow = [...preRows].reverse().find((r) => r.date < periodStart && r.nav !== null) // the close before the start: the 100 base
  const navChartRows: PortfolioDayRow[] | null = rows
    ? [...(anchorRow ? [{ ...anchorRow, realized: null, unrealized: null }] : []), ...extraDays, ...rows]
    : null
  // Index level of each pre-track-record chart day, and the factor carried into the track record.
  const preIndex: Record<string, number> = {}
  let preFactor = 1
  {
    let prev = anchorRow?.nav ?? null
    if (anchorRow) preIndex[anchorRow.date] = 100
    for (const r of extraDays) {
      const pnl = dayTotalPnl(r)
      if (pnl !== null && prev) preFactor *= 1 + pnl / prev
      preIndex[r.date] = preFactor * 100
      if (r.nav !== null) prev = r.nav
    }
  }
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
  // Explicit instruction: the LIVE portfolio's Sharpe/Sortino subtract a 3.5% annual
  // risk-free rate; the backtest tabs keep 0.
  const RISK_FREE_RATE_ANNUAL = isBacktest ? 0 : 0.035
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
      <div className="tab-bar">
        {(
          [
            { key: 'live', label: 'Live (IB)' },
            ...BACKTEST_TABS,
          ] as const
        ).map((t) => (
          <button key={t.key} type="button" className={`tab-btn${source === t.key ? ' active' : ''}`} onClick={() => setSource(t.key)}>
            {t.label}
          </button>
        ))}
      </div>
      {isBacktest && (
        <p className="status-row" title={`backtest.json -> currentModel.daily.series.${source}, stitched across the weekly windows`}>
          Backtest of {backtestTab?.name} -- daily returns, NAV rebased to 100 (the chart's index), from the backtest's first day; no cash, flows, fees or exposure data.
        </p>
      )}
      <header className="masthead">
        <div className="title-block">
          <h1>Portfolio</h1>
        </div>
        {rows && rows.length > 0 && (
          <div className="stat-row">
            {source === 'live' && (
              <div className="stat">
                <span className={`n num${(totalPnl ?? 0) >= 0 ? ' good' : ' bad'}`}>{fmtMoney(totalPnl)}</span>
                <span className="l">Total P&amp;L</span>
              </div>
            )}
            {source === 'live' && (
              <div className="stat">
                <span className={`n num${(totalRealized ?? 0) >= 0 ? ' good' : ' bad'}`}>{fmtMoney(totalRealized)}</span>
                <span className="l">Realized</span>
              </div>
            )}
            {source === 'live' && (
              <div className="stat">
                <span className={`n num${(totalCommissions ?? 0) >= 0 ? ' good' : ' bad'}`}>
                  {fmtMoney(totalCommissions)}
                </span>
                <span className="l">Commissions</span>
              </div>
            )}
            {source === 'live' && (
              <div className="stat">
                <span className={`n num${(totalDividends ?? 0) >= 0 ? ' good' : ' bad'}`}>{fmtMoney(totalDividends)}</span>
                <span className="l">Dividends</span>
              </div>
            )}
            {source === 'live' && (
              <div className="stat" title="Tax withheld on dividends and interest (negative). Shown only if the IBKR Flex query includes Withholding Tax in its Change in NAV section.">
                <span className={`n num${(totalWithholdingTax ?? 0) >= 0 ? '' : ' bad'}`}>{fmtMoney(totalWithholdingTax)}</span>
                <span className="l">WT</span>
              </div>
            )}
            {source === 'live' && (
              <div className="stat">
                <span className={`n num${(totalInterest ?? 0) >= 0 ? ' good' : ' bad'}`}>{fmtMoney(totalInterest)}</span>
                <span className="l">Interest</span>
              </div>
            )}
            {source === 'live' && (
              <div className="stat">
                <span className={`n num${(totalDepositsWithdrawals ?? 0) >= 0 ? ' good' : ' bad'}`}>
                  {fmtMoney(totalDepositsWithdrawals)}
                </span>
                <span className="l">Flows</span>
              </div>
            )}
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
              title={`Annualized Sharpe ratio: mean daily (Total P&L / prior-day NAV) return minus the risk-free rate (${isBacktest ? 'none on backtest tabs' : '3.5%/yr on the live portfolio'}), over its own volatility (std dev) — × √252 trading days/year.`}
            >
              <span className={`n num${sharpe === null ? '' : sharpe >= 0 ? ' good' : ' bad'}`}>
                {fmtRatio(sharpe)}
              </span>
              <span className="l">Sharpe</span>
            </div>
            <div
              className="stat"
              title={`Annualized Sortino ratio: same return as Sharpe, but over downside volatility only (upside swings aren't risk) — risk-free rate ${isBacktest ? '0 on backtest tabs' : '3.5%/yr on the live portfolio'}, × √252 trading days/year.`}
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

      {isBacktest && !backtestRows && <p className="status-row">Loading backtest…</p>}
      {isBacktest && backtestRows && backtestRows.length === 0 && <p className="status-row">No backtest daily data -- run: python main.py recalc</p>}
      {source === 'live' && error && <p className="status-row">Couldn't load portfolio_performance.json — run: python ib_server.py performance</p>}
      {source === 'live' && !error && !data && <p className="status-row">Loading…</p>}

      {navChartRows && navChartRows.length > 0 && <NavChart
          rows={navChartRows}
          indexByDate={Object.fromEntries(
            navChartRows.map((r) => [
              r.date,
              r.date < periodStart ? (preIndex[r.date] ?? 100) : preFactor * (1 + (cumulativeReturnByDate[r.date] ?? 0)) * 100,
            ]),
          )}
        />}
      {source === 'live' && chartRows && chartRows.length > 0 && <ExposureChart rows={chartRows.map((r) => ({ ...r, cashEquivalents: cashEqByDate[r.date] ?? 0 }))} />}
      {rows && rows.length > 0 && <MonthlyReturnsTable rows={rows} baselineNav={baselineNav} />}

      {rows && (
        <div className="table-wrap positions-table-wrap">
          <table>
            <thead>
              <tr>
                <th className="col-left">Date</th>
                {source === 'live' && <th>Cash</th>}
                {source === 'live' && <th title="Market value of the cash-equivalent holdings (IB01, SGOV, SHV, ...), rebuilt from the fills -- kept out of Long, Net and Gross">Cash equiv.</th>}
                <th title={isBacktest ? 'Compounded index, 100 = the day before the first backtest day (same as the chart)' : undefined}>NAV</th>
                {source === 'live' && <th title="(Stock Long − cash equivalents) / NAV">Stock Long %</th>}
                {source === 'live' && <th title="Stock Short / NAV">Stock Short %</th>}
                {source === 'live' && <th title="Long (ex cash equivalents) + Short">Net $</th>}
                {source === 'live' && <th title="Net / NAV">Net %</th>}
                {source === 'live' && <th title="Long (ex cash equivalents) + |Short|">Gross $</th>}
                {source === 'live' && <th title="Gross / NAV">Gross %</th>}
                {source === 'live' && <th>Flows</th>}
                {source === 'live' && <th>Commissions</th>}
                {source === 'live' && <th>Dividends</th>}
                {source === 'live' && <th title="Withholding tax">WT</th>}
                {source === 'live' && <th>Interest</th>}
                {source === 'live' && <th>Realized</th>}
                {source === 'live' && <th>Unrealized</th>}
                <th>Total P&amp;L</th>
                <th title="Total P&L / prior-day NAV">Total P&amp;L %</th>
                <th>Cumulative P&amp;L</th>
                <th title="Compounded Total P&L % return since the start of this track record">Cumulative %</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 && (
                <tr className="status-row">
                  <td colSpan={source === 'live' ? 21 : 6}>No daily rows in the query response.</td>
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
                    {source === 'live' && <td className="num">{fmtLevel(r.cash)}</td>}
                    {source === 'live' && <td className="num">{fmtLevel(cashEq)}</td>}
                    <td className="num">{isBacktest ? (r.nav?.toFixed(2) ?? '—') : fmtLevel(r.nav)}</td>
                    {source === 'live' && <td className="num">{fmtExposurePct(longAdj, r.nav)}</td>}
                    {source === 'live' && <td className="num">{fmtExposurePct(r.stockShort, r.nav)}</td>}
                    {source === 'live' && <td className="num">{fmtMoneyPlain(netAdj)}</td>}
                    {source === 'live' && <td className="num">{fmtExposurePct(netAdj, r.nav)}</td>}
                    {source === 'live' && <td className="num">{fmtMoneyPlain(grossAdj)}</td>}
                    {source === 'live' && <td className="num">{fmtExposurePct(grossAdj, r.nav)}</td>}
                    {source === 'live' && <td className="num">{fmtMoneyPlain(r.depositsWithdrawals)}</td>}
                    {source === 'live' && <td className="num">{fmtMoneyPlain(r.commissions)}</td>}
                    {source === 'live' && <td className="num">{fmtMoneyPlain(r.dividends)}</td>}
                    {source === 'live' && <td className="num">{fmtMoneyPlain(r.withholdingTax ?? null)}</td>}
                    {source === 'live' && <td className="num">{fmtMoneyPlain(r.interest)}</td>}
                    {source === 'live' && (
                      <td className={`num ${r.realized === null ? '' : r.realized >= 0 ? 'good' : 'bad'}`}>
                        {fmtMoneyPlain(r.realized)}
                      </td>
                    )}
                    {source === 'live' && (
                      <td className={`num ${r.unrealized === null ? '' : r.unrealized >= 0 ? 'good' : 'bad'}`}>
                        {fmtMoneyPlain(r.unrealized)}
                      </td>
                    )}
                    <td className={`num ${totalPnl === null ? '' : totalPnl >= 0 ? 'good' : 'bad'}`}>
                      {isBacktest ? (totalPnl?.toFixed(2) ?? '—') : fmtMoneyPlain(totalPnl)}
                    </td>
                    <td className={`num ${dailyReturn === null || dailyReturn === undefined ? '' : dailyReturn >= 0 ? 'good' : 'bad'}`}>
                      {fmtPct(dailyReturn)}
                    </td>
                    <td className={`num ${cumulativePnl >= 0 ? 'good' : 'bad'}`}>{isBacktest ? cumulativePnl?.toFixed(2) : fmtMoneyPlain(cumulativePnl)}</td>
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
