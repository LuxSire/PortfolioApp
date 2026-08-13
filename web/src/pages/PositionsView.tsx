import { useEffect, useMemo, useState } from 'react'
import { parseCSV } from '../csv'
import { earningsUrgencyClass } from '../earnings'
import { getSectorGroup, sectorGroupLabel } from '../sectorGroups'
import { getSectorIcon } from '../sectorIcons'
import { IB_STREAM_URL } from '../ibStream'
import { avgInsiderScore, avgNewsSentiment, rankTo100, ratingClass, toNum } from '../screenerFactors'
import { FACTOR_COLUMNS, computeFactorAverages } from '../components/factorTable'
import FactorCells from '../components/FactorCells'
import type {
  Account,
  FactorsByTicker,
  HistoryByTicker,
  PortfolioBetaResult,
  PortfolioVolResult,
  PositionRow,
  PositionsByTicker,
  PricesByTicker,
  SideGroup,
  TickerInfoByTicker,
  TradesByTicker,
  WeightedSideFactor,
} from '../interfaces/IPositionsView'

// Same curated tags + order as IBApp.ACCOUNT_STATUS_TAGS; PnL fields get
// the good/bad sign coloring the rest of the app uses.
const ACCOUNT_FIELDS: { tag: string; label: string; signed?: boolean }[] = [
  { tag: 'NetLiquidation', label: 'Net Liquidation' },
  { tag: 'TotalCashValue', label: 'Total Cash' },
  { tag: 'AvailableFunds', label: 'Available Funds' },
  { tag: 'ExcessLiquidity', label: 'Excess Liquidity' },
  { tag: 'BuyingPower', label: 'Buying Power' },
  { tag: 'UnrealizedPnL', label: 'Unrealized P&L', signed: true },
  { tag: 'RealizedPnL', label: 'Realized P&L', signed: true },
  { tag: 'DailyPnL', label: 'Daily P&L', signed: true },
]

// This table's own leading columns, followed by the shared factor-columns
// tail (see FactorCells.jsx's FACTOR_COLUMNS — Avg PE through Score,
// identical set/order/formatting the Sectors tab's table uses too). Sum
// of % of NAV is this table's own addition on top of that shared tail:
// the sum of each held position's own % of NAV (see the positions table's
// own % of NAV column) on this side — i.e. the side's gross exposure as a
// share of the account, shown right next to Pos Value so it's clear that
// isn't the same number (Pos Value nets long vs. short within the row and
// is a raw dollar figure, not a % of NAV).
// Beta is this table's own trailing addition on top of the shared
// factor-columns tail (see FactorCells.jsx's FACTOR_COLUMNS) — not part
// of that shared module since it's Positions-specific (the Screener/
// Sectors tabs have no live position value to weight it by that would
// mean anything), so it's appended here rather than folded into
// FACTOR_COLUMNS/FactorCells where the Sectors tab would inherit it too.
const WEIGHTED_TABLE_COLUMNS: { key: string; label: string; className?: string }[] = [
  { key: 't', label: 'Ticker', className: 'col-left col-ticker' },
  { key: 'n', label: 'Name', className: 'col-left col-name' },
  { key: 'posval', label: 'Pos Value' },
  { key: 'weightSum', label: '% of NAV' },
  { key: 'dayPnl', label: 'Daily P&L' },
  ...FACTOR_COLUMNS,
  { key: 'beta', label: 'Beta' },
  { key: 'dollarPer1PctMove', label: '$/1% Mkt' },
]

function fmtPrice(v: number | null): string {
  if (v === null) return '—'
  return '$' + v.toFixed(2)
}

// The last close strictly before today from a {date, close} bar series
// (price_history_daily_3mo.json / price_history.json) — never today's own
// entry, which both sources can carry as a still-forming bar (close =
// latest price so far, not a settled close) when fetched intraday.
// Comparing a live price against that same-day bar instead of a real
// prior close silently understates or misreports the day's actual move.
function previousClose(series: { date: string; close: number }[] | undefined): number | null {
  if (!series || series.length === 0) return null
  const today = new Date().toISOString().slice(0, 10)
  for (let i = series.length - 1; i >= 0; i--) {
    if (series[i].date.slice(0, 10) < today) return series[i].close
  }
  return null
}

// Briefly highlights a value in green/red when it changes from the last
// render — up or down determined by comparing to the previous value, not
// by sign, since e.g. a bid ticking from $10.00 to $10.05 should flash
// green regardless of whether $10.05 itself is "good." Own component (not
// inline in the row) so each cell keeps its own previous-value ref/timer,
// keyed by React to the row + column it's rendered in.
function FlashCell({ value, format }: { value: number | null; format: (v: number | null) => string }) {
  // Deriving `flash` from a value change is React's documented "adjusting
  // state during rendering" pattern — setting state directly in the render
  // body here is safe (React re-renders with the new state before
  // committing), no ref involved. Auto-clearing it after a delay is a
  // separate concern — reacting to time passing, not to a prop — so that
  // part lives in its own effect keyed on `flash`, which owns the timer
  // and cleans it up if a new flash (or unmount) preempts it.
  const [prevValue, setPrevValue] = useState(value)
  const [flash, setFlash] = useState('')

  if (value !== prevValue) {
    if (prevValue !== null && value !== null) {
      setFlash(value > prevValue ? 'flash-up' : 'flash-down')
    }
    setPrevValue(value)
  }

  useEffect(() => {
    if (!flash) return
    const id = setTimeout(() => setFlash(''), 800)
    return () => clearTimeout(id)
  }, [flash])

  return <span className={`flash-cell ${flash}`}>{format(value)}</span>
}

// Whole numbers for the vast majority of IBKR positions; only show
// decimals for the rare fractional-share holding.
function fmtShares(v: number | null): string {
  if (v === null) return '—'
  return v.toLocaleString(undefined, { maximumFractionDigits: Number.isInteger(v) ? 0 : 4 })
}

function fmtMoney(v: number | null): string {
  if (v === null) return '—'
  return '$' + v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

// Same as fmtMoney but without the '$' — the Daily $ column already carries
// its currency in the header, so a sign on every row is redundant clutter.
function fmtDollars(v: number | null): string {
  if (v === null) return '—'
  return v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

// Beta (both Portfolio Beta and the Portfolio Factors table's own Beta
// column) is dimensionless, not a dollar amount — fmtMoney's
// maximumFractionDigits: 0 would round a realistic beta (typically well
// under 1 in magnitude) straight down to "$0", silently destroying it.
// 2 decimals, signed, no currency symbol — same convention
// PortfolioView.jsx's fmtRatio uses for Sharpe/Sortino.
function fmtRatio(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '') + v.toFixed(2)
}

function fmtPct(v: number | null): string {
  if (v === null) return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(2) + '%'
}

// good/bad for a price ratio (daily change, or price vs. avgCost) in terms
// of real gain/loss, not the raw sign of the ratio — a short position
// (negative shares) profits when the price falls, so a positive ratio
// there is a loss and must color red, the mirror of a long position.
// neutralBand is a magnitude threshold on the raw ratio (e.g. IB-vs-
// yfinance noise), evaluated before the direction flip.
function perfClass(ratio: number | null, shares: number | null, neutralBand = 0): string {
  if (ratio === null || shares === null) return ''
  if (Math.abs(ratio) <= neutralBand) return ''
  const effective = shares >= 0 ? ratio : -ratio
  return effective >= 0 ? 'good' : 'bad'
}

// Volatility has no sign — it's a magnitude, not a direction — so it skips
// fmtPct's +/- prefix.
function fmtVol(v: number | null): string {
  if (v === null) return '—'
  return (v * 100).toFixed(2) + '%'
}

// price_history_daily_3mo.json (IB Gateway's own bars) dates every point
// "2026-05-08 00:00:00"; price_history.json (yfinance) dates every point
// "2026-07-06" — no time component. Both portfolioVolatilityDecomposition
// and portfolioArkkSensitivity below fall back per-ticker between the two
// sources (whichever has that ticker's history), then intersect every
// priced ticker's dates to find days every one of them has a close for.
// Without normalizing first, a single ticker falling back to the
// yfinance source (e.g. one IB Gateway never fetched candlestick history
// for) has date strings that never string-equal the other tickers'
// " 00:00:00"-suffixed ones — collapsing the ENTIRE intersection to
// zero dates, not just excluding that one ticker, silently zeroing out
// Portfolio Vol./ARKK Sensitivity for the whole book. Confirmed live:
// CPNG had no price_history_daily_3mo.json entry at all, fell back to
// price_history.json, and that alone took commonDates from 61 down to 0
// across all 25 held tickers.
function normalizedDateKey(dateStr: string): string {
  return typeof dateStr === 'string' ? dateStr.slice(0, 10) : dateStr
}

// Simulates "if I'd held today's exact position sizes for the last ~3
// months" by pricing today's share counts against each ticker's historical
// daily closes — not an average of each position's individual volatility,
// which would ignore that positions move together or offset each other. A
// date is only included if every priced ticker has a close for it, so the
// simulated portfolio's composition (and therefore its dollar value) is
// apples-to-apples from one day to the next; a ticker missing from both
// price_history_daily_3mo.json and price_history.json (no history at all)
// is left out of the simulation rather than treated as flat, so it
// doesn't silently mute the real number.
//
// Returns the portfolio's dollar volatility (volDollar) plus an EXACT
// per-position dollar decomposition that sums to it to the last cent —
// not a "vol% × current net value" approximation. volDollar's own %-of-
// account reading (shown as this stat's small subvalue) divides it by
// NetLiquidation at render time (see PositionsView's own JSX) rather than
// this function returning a second, independently-computed %-of-returns
// figure the way it briefly did -- that alternative (stdev of the
// simulated portfolio's own day-to-day % returns, denominator shifting
// with each day's historical value) doesn't answer "how big is this next
// to my actual account," which is what the subvalue is for; dividing the
// exact dollar figure by today's real NetLiquidation does. The trick for
// volDollar/cvolByTicker themselves: work in dollar P&L space, not %
// returns. Each day's
// portfolio dollar change is, by construction (shares_i fixed = today's
// share count, applied to historical prices), exactly the sum of each
// position's own dollar change that day: dPortfolio(t) = Σ_i dAsset_i(t).
// Variance is linear under sums of a variable with itself:
// Var(dPortfolio) = Var(Σ_i dAsset_i) = Σ_i Cov(dAsset_i, dPortfolio) —
// so Σ_i [Cov(dAsset_i, dPortfolio) / volDollar] = volDollar EXACTLY, a
// provable identity, not an approximation. That per-term quotient is each
// position's component volatility (cvolByTicker), i.e. "how many of the
// portfolio's total dollars of daily volatility this position accounts
// for" — unlike that position's OWN standalone volatility, this can be
// negative for a position that tends to move opposite the rest of the
// book (a genuine diversifier, reducing total portfolio risk).
function portfolioVolatilityDecomposition(
  rows: PositionRow[],
  dailyHistory3mo: HistoryByTicker,
  monthlyHistory: HistoryByTicker
): PortfolioVolResult {
  const total = rows.filter((r) => r.shares).length
  const empty: PortfolioVolResult = { volDollar: null, cvolByTicker: new Map(), covered: 0, total }

  const priced: { ticker: string; shares: number; byDate: Map<string, number> }[] = []
  for (const r of rows) {
    if (!r.shares) continue
    const series = dailyHistory3mo[r.ticker] || monthlyHistory[r.ticker]
    if (!series || series.length < 6) continue
    priced.push({
      ticker: r.ticker,
      shares: r.shares,
      byDate: new Map(series.map((p) => [normalizedDateKey(p.date), p.close])),
    })
  }
  if (priced.length === 0) return empty

  let commonDates = new Set(priced[0].byDate.keys())
  for (const p of priced.slice(1)) {
    commonDates = new Set([...commonDates].filter((d) => p.byDate.has(d)))
  }
  const sortedDates = [...commonDates].sort()
  const covered = priced.length
  if (sortedDates.length < 6) return { ...empty, covered }

  const portfolioValues = sortedDates.map((date) =>
    priced.reduce((sum, p) => sum + p.shares * (p.byDate.get(date) as number), 0)
  )
  if (portfolioValues.length < 6) return { ...empty, covered }

  const dPortfolio: number[] = []
  for (let i = 1; i < portfolioValues.length; i++) {
    dPortfolio.push(portfolioValues[i] - portfolioValues[i - 1])
  }
  if (dPortfolio.length < 5) return { ...empty, covered }

  const meanDPortfolio = dPortfolio.reduce((a, b) => a + b, 0) / dPortfolio.length
  const varDPortfolio = dPortfolio.reduce((a, b) => a + (b - meanDPortfolio) ** 2, 0) / (dPortfolio.length - 1)
  const volDollar = Math.sqrt(varDPortfolio)

  const cvolByTicker = new Map<string, number>()
  if (volDollar) {
    for (const p of priced) {
      const dAsset: number[] = []
      for (let i = 1; i < sortedDates.length; i++) {
        dAsset.push(p.shares * ((p.byDate.get(sortedDates[i]) as number) - (p.byDate.get(sortedDates[i - 1]) as number)))
      }
      const meanAsset = dAsset.reduce((a, b) => a + b, 0) / dAsset.length
      let cov = 0
      for (let i = 0; i < dAsset.length; i++) {
        cov += (dAsset[i] - meanAsset) * (dPortfolio[i] - meanDPortfolio)
      }
      cov /= dAsset.length - 1
      cvolByTicker.set(p.ticker, cov / volDollar)
    }
  }

  return { volDollar, cvolByTicker, covered, total }
}

// Portfolio beta exposure: each held position's value * beta, summed,
// divided by GROSS value (Math.abs(value) — same weighting convention
// every other averaged factor in the Portfolio Factors table below
// uses), NOT signed value. The numerator has to stay signed (a short
// position's contribution must subtract from net market exposure, the
// opposite of holding it long — shorting a positive-beta stock is a
// genuinely negative beta exposure), but the DENOMINATOR must not be
// signed too: for an all-short subset, signed value sums negative, and
// dividing two negatives silently cancels the sign back to positive.
// Confirmed live: the Portfolio Factors table's Short row was showing a
// positive Beta before this fix, when it should be negative. Gross
// weighting avoids that (and also can't blow up toward a near-zero
// denominator the way net value can for a close-to-hedged book).
// Missing beta (yfinance has no value at all for a handful of tickers,
// e.g. a very recent IPO/spinoff without enough price history yet — see
// SNDK) defaults to 1 (average market sensitivity) rather than being
// excluded, so a large position with no beta doesn't just vanish from
// the calculation; `covered` still tracks how many had a REAL beta, for
// the UI to disclose how much of the figure is assumed vs. actual.
// weightedSum (Σ value_i × beta_i) is also, on its own, exactly the
// first-order dollar P&L this set of rows would take from a 1% market
// move — beta_i is "expected % move for a 1% market move," so
// value_i × beta_i × 1% is that position's expected dollar move, and the
// sum is the book's. Dividing by grossWeightTotal to get the *average*
// beta above loses that dollar figure (grossWeightTotal is gross, not
// net, exposure, so beta * netValue would NOT recover it correctly for
// a one-sided book -- verified by hand for an all-short subset: beta_avg
// there is negative and netValue is also negative, so beta_avg *
// netValue comes out positive, the wrong sign, whereas weightedSum
// itself already has the right sign built in). So this returns both,
// rather than making callers try to reconstruct one from the other.
function portfolioBetaExposure(rows: PositionRow[]): PortfolioBetaResult {
  let weightedSum = 0
  let grossWeightTotal = 0
  let covered = 0
  const total = rows.filter((r) => r.shares).length
  for (const r of rows) {
    if (!r.shares || r.value === null) continue
    const hasBeta = r.beta !== null && r.beta !== undefined && Number.isFinite(r.beta)
    if (hasBeta) covered += 1
    weightedSum += r.value * (hasBeta ? (r.beta as number) : 1)
    grossWeightTotal += Math.abs(r.value)
  }
  if (!grossWeightTotal) return { beta: null, dollarPer1PctMove: null, covered, total }
  return { beta: weightedSum / grossWeightTotal, dollarPer1PctMove: weightedSum * 0.01, covered, total }
}

// One row of the value-weighted portfolio-factors table (see
// weightedSideFactors and WEIGHTED_TABLE_COLUMNS) — every cell is either a
// value-weighted average (numeric factors), a sum (Pos Value = the side's
// net dollar exposure; Sum of % of NAV = the sum of each position's own %
// of NAV on this side), or descriptive text (Ticker/Name).
function WeightedFactorRow({ side, count, netValue, sumWeightPct, dayPnl, factors, beta, dollarPer1PctMove }: WeightedSideFactor) {
  return (
    <tr>
      <td className={`col-left col-ticker side-group-cell${side === 'Short' ? ' side-group-cell-short' : ''}`}>
        <span className="side-group-label">{side}</span>
      </td>
      <td className="col-left col-name">
        {count} position{count === 1 ? '' : 's'}
      </td>
      <td className="num">{fmtMoney(netValue || null)}</td>
      <td className="num">{fmtPct(sumWeightPct)}</td>
      <td className={`num ${dayPnl === 0 ? '' : dayPnl >= 0 ? 'good' : 'bad'}`}>{fmtMoney(dayPnl || null)}</td>
      <FactorCells factors={factors} />
      <td className="num">{fmtRatio(beta)}</td>
      <td
        className={`num ${dollarPer1PctMove === null ? '' : dollarPer1PctMove >= 0 ? 'good' : 'bad'}`}
        title="Σ(value × beta) × 1% — this side's expected dollar P&L from a 1% move in the broad market."
      >
        {fmtMoney(dollarPer1PctMove)}
      </td>
    </tr>
  )
}

// Stocks only — see ib_price_server.py's docstring on why an option and its
// underlying can't share this ticker-symbol-keyed price stream.
export default function PositionsView() {
  // {ticker: {name, sector, price, ern, upd}}, best-effort labeling + the
  // yfinance price sorted_screen.csv already has, as a fallback for
  // tickers ib_price_server.py hasn't (or can't) get a live quote for.
  // ern/upd feed earningsUrgencyClass, same as PeTable.jsx's Name cell.
  const [tickerInfo, setTickerInfo] = useState<TickerInfoByTicker>({})
  const [prices, setPrices] = useState<PricesByTicker>({})
  const [positions, setPositions] = useState<PositionsByTicker>({})
  const [account, setAccount] = useState<Account>({})
  // {ticker: {qty, value}} — today's fills only (see ib_price_server.py's
  // refresh_trades). Only present for a symbol actually traded today;
  // used below to mark those shares at their own fill price instead of
  // assuming the whole position was held since yesterday's close.
  const [trades, setTrades] = useState<TradesByTicker>({})
  // Daily-close series for volatility. price_history_daily_3mo.json is IB
  // Gateway's own history and always covers every held ticker (see
  // ib_price_server.py's fetch_candlestick_history — held positions are
  // unioned in regardless of the ranked-tickers budget), so it's the
  // primary source; price_history.json (yfinance, 1mo, broader but not
  // guaranteed to include every held ticker) is the fallback for a
  // position IB Gateway hasn't fetched history for yet.
  const [dailyHistory3mo, setDailyHistory3mo] = useState<HistoryByTicker>({})
  const [monthlyHistory, setMonthlyHistory] = useState<HistoryByTicker>({})
  // Every screener factor column (see screenerFactors.js's COLUMNS), keyed
  // by ticker — feeds the value-weighted Long/Short portfolio-factors table
  // below. Kept separate from tickerInfo (name/sector/price/ern/upd) rather
  // than folded in, since tickerInfo already has its own established shape
  // used throughout `rows` below.
  const [factorsByTicker, setFactorsByTicker] = useState<FactorsByTicker>({})
  // Sector's average forward P/E across the FULL screener universe (not
  // just held tickers) — same figure PeTable.jsx's Avg PE column shows,
  // computed the same way (see that file's sectorAvgPE) since a sector
  // average is only meaningful over the whole universe, not the handful of
  // names actually held in it.
  const [sectorAvgPE, setSectorAvgPE] = useState<Map<string, number>>(new Map())

  useEffect(() => {
    Promise.all([
      fetch('/sorted_screen.csv').then((r) => (r.ok ? r.text() : '')),
      // Same best-effort contract as PeTable.jsx's own fetch of these —
      // missing/failed just means every ticker's sent/newsSent/instChange/
      // insiders is blank, not a load error.
      fetch('/social_sentiment.json')
        .then((r) => (r.ok ? r.json() : {}))
        .catch(() => ({})) as Promise<Record<string, any>>,
      fetch('/news_sentiment.json')
        .then((r) => (r.ok ? r.json() : {}))
        .catch(() => ({})) as Promise<Record<string, any>>,
      fetch('/sec/form4/insider_transactions.json')
        .then((r) => (r.ok ? r.json() : {}))
        .catch(() => ({})) as Promise<Record<string, any>>,
      fetch('/sec/13f/institutional_holdings.json')
        .then((r) => (r.ok ? r.json() : {}))
        .catch(() => ({})) as Promise<Record<string, any>>,
    ] as const)
      .then(([text, sentiment, newsSentiment, insiderTransactions, institutionalHoldings]) => {
        const info: TickerInfoByTicker = {}
        const factors: FactorsByTicker = {}
        const peSums = new Map<string, number>()
        const peCounts = new Map<string, number>()
        for (const row of parseCSV(text)) {
          info[row.ticker] = {
            name: row.name,
            sector: row.sector,
            price: toNum(row.price),
            ern: toNum(row.earningsTimestampStart),
            upd: row.lastDownload || null,
            rating: row.rating || null,
          }
          const fpe = toNum(row.forwardPE)
          if (row.sector && fpe !== null && fpe > 0) {
            peSums.set(row.sector, (peSums.get(row.sector) || 0) + fpe)
            peCounts.set(row.sector, (peCounts.get(row.sector) || 0) + 1)
          }
          const newsSent = avgNewsSentiment(newsSentiment[row.ticker])
          const insiders = avgInsiderScore(insiderTransactions[row.ticker])
          // Simple average of whichever of the two periods is present —
          // same treatment as PeTable.jsx's own epsTrend (both periods
          // are already the same %-change-ratio scale, see
          // IBApp._eps_revision, unlike e.g. shortRatio/shortPercentOfFloat
          // which need rank-averaging instead of raw-value averaging).
          const epsTrendParts = [toNum(row.epsRevision0y), toNum(row.epsRevision1y)].filter((v) => v !== null) as number[]
          const epsTrend = epsTrendParts.length
            ? epsTrendParts.reduce((a, b) => a + b, 0) / epsTrendParts.length
            : null
          factors[row.ticker] = {
            fpe,
            feps: toNum(row.forwardEps),
            epsTrend,
            tpe: toNum(row.trailingPE),
            tps: toNum(row.trailingPS),
            peg: toNum(row.pegRatio),
            revg: toNum(row.revenueGrowth),
            pfcf: toNum(row.priceToFCF),
            evEbitda: toNum(row.enterpriseToEbitda),
            beta: toNum(row.beta),
            opMargin: toNum(row.operatingMargins),
            de: toNum(row.debtToEquity),
            liq: toNum(row.LiqRatio),
            shortInt: toNum(row.shortPercentOfFloat),
            tgt: toNum(row.targetMeanPrice),
            upside: toNum(row.targetUpside),
            mom: toNum(row.momentum),
            mr: toNum(row.meanReversion),
            sc: toNum(row.score),
            sent: toNum(sentiment[row.ticker]?.score),
            newsSent: newsSent.avg !== null ? newsSent.avg - 3 : null,
            instChange: toNum(institutionalHoldings[row.ticker]?.pctShareChangeQoQ),
            // ×100, NOT rank-rescaled like the loop below — see
            // ScreenerView.tsx's identical assignment for why: insiders.avg
            // is already a bounded, comparable ratio, and a percentile
            // rank over an insider-buy universe this dominated by
            // zero-buy ties lets a single stray buy vault a ticker
            // dramatically up the scale (confirmed live: LQDA, 1 buy vs.
            // 79 sells, ranked strongly positive under the old treatment).
            insiders: insiders.avg !== null ? insiders.avg * 100 : null,
          }
        }
        // Same rank-to-[-100, 100] rescale as ScreenerView.tsx's Screener
        // (see rankTo100's own comment for why rank-based, not min-max),
        // and over the same full sorted_screen.csv universe (this parses
        // that same file before subsetting to held positions below), so
        // Momentum/MeanRev/Sentiment/News read on one identical scale
        // across every tab, not a Positions-only ranking. Insiders
        // deliberately excluded -- see its own ×100 comment above.
        const factorRows = Object.values(factors)
        for (const key of ['mom', 'mr', 'sent', 'newsSent', 'instChange'] as const) {
          const ranked = rankTo100(factorRows.map((f) => f[key]))
          factorRows.forEach((f, i) => {
            f[key] = ranked[i]
          })
        }
        const avgPE = new Map<string, number>()
        for (const [sector, sum] of peSums) avgPE.set(sector, sum / (peCounts.get(sector) as number))
        setTickerInfo(info)
        setFactorsByTicker(factors)
        setSectorAvgPE(avgPE)
      })
      .catch(() => {})
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
    const source = new EventSource(IB_STREAM_URL)
    source.onmessage = (e) => {
      const { prices: p, positions: pos, account: acc, trades: tr } = JSON.parse(e.data)
      setPrices(p)
      setAccount(acc || {})
      setPositions(pos)
      setTrades(tr || {})
    }
    source.onerror = () => {} // EventSource auto-reconnects; nothing to do here.
    return () => source.close()
  }, [])

  const rows: PositionRow[] = useMemo(() => {
    return Object.entries(positions).map(([ticker, pos]) => {
      const info = tickerInfo[ticker]
      // Guard every input with Number.isFinite, not just a null check —
      // `undefined * price` and `NaN`-anything both silently produce NaN
      // rather than throwing, and NaN isn't caught by `!== null` or `??`
      // (NaN ?? 0 is still NaN). A malformed or missing value anywhere in
      // this chain should fall back to "unknown" (null, rendered as "—"),
      // never a NaN that then poisons every sum built on top of it.
      const shares = Number.isFinite(pos?.shares) ? (pos.shares as number) : null
      const avgCost = Number.isFinite(pos?.avgCost) ? (pos.avgCost as number) : null
      // IB Gateway's live tick first; sorted_screen.csv's yfinance price
      // (same one the screener falls back to) when IB has no quote for it —
      // e.g. a held ticker outside the screener's usual universe, or the
      // price server isn't running at all.
      const ibPrice = Number.isFinite(prices[ticker]?.last) ? (prices[ticker].last as number) : null
      const bid = Number.isFinite(prices[ticker]?.bid) ? (prices[ticker].bid as number) : null
      const ask = Number.isFinite(prices[ticker]?.ask) ? (prices[ticker].ask as number) : null
      const yfPrice = Number.isFinite(info?.price) ? (info?.price as number) : null
      // IB Gateway's own daily bars (price_history_daily_3mo.json), not
      // sorted_screen.csv's yfinance price — that's whatever main.py's
      // yfinance fetch last happened to see (a live quote at fetch time,
      // not necessarily yesterday's close), which made the daily-change
      // math below compare against an arbitrary moment instead of a real
      // prior close. previousClose also strips today's own bar — IB's
      // history is fetched intraday, so its last entry is often today's
      // still-forming bar (close = latest price, not a settled close);
      // comparing the live price against that understates or misreports
      // the real daily move (this is what made ARKK's P&L wrong). Falls
      // back to price_history.json (yfinance, broader coverage but not
      // guaranteed for every ticker) then sorted_screen.csv's price only
      // if IB has no history for this ticker at all.
      const referencePrice =
        previousClose(dailyHistory3mo[ticker]) ?? previousClose(monthlyHistory[ticker]) ?? yfPrice
      const price = ibPrice ?? referencePrice
      const value = shares !== null && price !== null ? shares * price : null
      const pnlPct = price !== null && avgCost ? price / avgCost - 1 : null
      // Same "daily performance" proxy PeTable.jsx's Price column uses:
      // IB Gateway's live price vs. the prior reference price above —
      // requires both, never falls back, unlike `price` above. Stays a
      // pure price move regardless of trades (it doesn't factor shares
      // at all); dayPnl below is the dollar figure that does need
      // correcting for same-day trades.
      const dayPct = ibPrice !== null && referencePrice ? ibPrice / referencePrice - 1 : null
      // Marks today's position in two pieces instead of assuming the
      // whole thing was held since yesterday's close: shares still held
      // from before today (positionAtStartOfDay) at referencePrice, plus
      // whatever was bought/sold today (trade.qty/trade.value, signed) at
      // each fill's own price — both then marked to today's live price.
      // Without this split, a same-day trade (e.g. buying AU today) would
      // misprice the whole position as if it were held at yesterday's
      // close, badly overstating or understating today's P&L. See
      // ib_price_server.py's refresh_trades for where trade.qty/value
      // come from.
      const trade = trades[ticker]
      const todayQty = trade?.qty ?? 0
      const todayValue = trade?.value ?? 0
      const dayPnl =
        shares !== null && ibPrice !== null && referencePrice !== null
          ? ibPrice * shares - referencePrice * (shares - todayQty) - todayValue
          : null
      return {
        ticker,
        name: info?.name || ticker,
        // Sector, not the finer-grained industry sorted_screen.csv carries
        // (main.py stores yfinance's "industry" in that column — see
        // IBApp.get_forward_pe) — same industry->sector mapping the
        // screener's own sector filter uses.
        sector: getSectorGroup(info?.sector),
        shares,
        avgCost,
        price,
        referencePrice,
        bid,
        ask,
        value,
        pnlPct,
        dayPct,
        dayPnl,
        ern: info?.ern ?? null,
        upd: info?.upd ?? null,
        rating: info?.rating ?? null,
        // Screener factor columns (see screenerFactors.js) — feeds the
        // value-weighted Long/Short portfolio-factors table below only;
        // nothing else in this component reads these.
        savgpe: sectorAvgPE.get(info?.sector as string) ?? null,
        ...factorsByTicker[ticker],
      }
    })
  }, [positions, prices, tickerInfo, dailyHistory3mo, monthlyHistory, trades, factorsByTicker, sectorAvgPE])

  // Long/short split first — the book's two fundamentally different
  // exposures, rendered as their own leftmost rowSpan column — sector
  // subgrouping second, nested underneath exactly like before. A short
  // position's shares (and so its value) are negative; shares === 0
  // shouldn't occur among open positions at all, but falls into Long
  // rather than being dropped if it somehow does.
  const sideGroups: SideGroup[] = useMemo(() => {
    const sides = (
      [
        { side: 'Long', sideRows: rows.filter((r) => (r.shares ?? 0) >= 0) },
        { side: 'Short', sideRows: rows.filter((r) => (r.shares ?? 0) < 0) },
      ] as { side: 'Long' | 'Short'; sideRows: PositionRow[] }[]
    ).filter((s) => s.sideRows.length > 0)

    return sides.map(({ side, sideRows }) => {
      const bySector = new Map<string | null, PositionRow[]>()
      for (const r of sideRows) {
        if (!bySector.has(r.sector)) bySector.set(r.sector, [])
        ;(bySector.get(r.sector) as PositionRow[]).push(r)
      }
      const sectorGroups = [...bySector.entries()].map(([sector, sectorRows]) => {
        sectorRows.sort((a, b) => (b.value ?? -1) - (a.value ?? -1))
        const total = sectorRows.reduce((s, r) => s + (r.value ?? 0), 0)
        const dayPnl = sectorRows.reduce((s, r) => s + (r.dayPnl ?? 0), 0)
        return { sector, rows: sectorRows, total, dayPnl }
      })
      // Long: biggest positive total first. Short: totals are negative,
      // so ascending puts the biggest short (most negative) first —
      // "biggest exposure first" either way.
      sectorGroups.sort((a, b) => (side === 'Long' ? b.total - a.total : a.total - b.total))
      const total = sideRows.reduce((s, r) => s + (r.value ?? 0), 0)
      const dayPnl = sideRows.reduce((s, r) => s + (r.dayPnl ?? 0), 0)
      return { side, sectorGroups, total, dayPnl, rowCount: sideRows.length }
    })
  }, [rows])

  // Value-weighted average of every screener factor (see FactorCells.jsx's
  // FACTOR_KEYS/computeFactorAverages), one row per side (Long/Short)
  // rather than one blended figure — a portfolio's long and short books
  // are different bets with opposite exposure, so averaging them together
  // would net out (and hide) real positioning rather than describe either
  // side. Weight is each position's gross dollar exposure
  // (Math.abs(value)), not signed value — signed weighting on the Short
  // side (all-negative values) would make every factor's weighted average
  // work out to the position-count-weighted average with its sign
  // flipped, not a real exposure weighting.
  const weightedSideFactors: WeightedSideFactor[] = useMemo(() => {
    const netLiq = account.NetLiquidation
    return sideGroups.map((g) => {
      const sideRows = rows.filter((r) => (g.side === 'Long' ? (r.shares ?? 0) >= 0 : (r.shares ?? 0) < 0))
      const { factors, count, sumWeight } = computeFactorAverages(sideRows, (r: PositionRow) => Math.abs(r.value ?? 0))
      // Sum of each position's own % of NAV (see the positions table's own
      // % of NAV column) — the side's gross exposure (sumWeight) expressed
      // as a share of the account, rather than a raw dollar figure.
      const sumWeightPct = netLiq ? sumWeight / netLiq : null
      // portfolioBetaExposure is already gross-weighted (see its own
      // comment), same convention every other column in this table uses
      // — reused as-is per side, not just for the top Portfolio Beta stat.
      const { beta, dollarPer1PctMove } = portfolioBetaExposure(sideRows)
      // g.dayPnl (from sideGroups above) is the same per-position dayPnl
      // sum the main table's side-group header and the masthead's Daily
      // P&L stat already use — reused as-is, not recomputed.
      return { side: g.side, count, netValue: g.total, sumWeightPct, factors, beta, dollarPer1PctMove, dayPnl: g.dayPnl }
    })
  }, [sideGroups, rows, account])

  // Net: long and short values offset (a short's value is negative, since
  // its shares are negative) — the portfolio's actual directional exposure.
  // Gross: every position's magnitude summed regardless of side — total
  // capital at work either way.
  const netValue = rows.reduce((s, r) => s + (r.value ?? 0), 0)
  const grossValue = rows.reduce((s, r) => s + Math.abs(r.value ?? 0), 0)
  // Sum of each position's dollar dayPnl (see rows above) — same IB-vs-
  // yfinance daily proxy as the Daily % column, just summed in dollars.
  // This is the unrealized side only (today's mark-to-market on
  // currently-open positions); closedPositionsRealizedPnl below is the
  // realized side, and Daily P&L is their sum.
  const positionsDayPnl = rows.reduce((s, r) => s + (r.dayPnl ?? 0), 0)
  // Today's REALIZED P&L — the mark-to-market gain/loss attributable to a
  // symbol traded today that's now fully closed out (shares back to 0). A
  // symbol only PARTIALLY closed today doesn't need separate handling:
  // it's still in `positions`, so it's already in `rows` above, and that
  // existing dayPnl formula already nets in today's realized contribution
  // alongside the unrealized mark on whatever's left open — verified by
  // hand: holding 100 @ prevClose, selling 30 @ execPrice, marking the
  // remaining 70 at the current price, dayPnl's result equals
  // (execPrice-prevClose)*30 + (currentPrice-prevClose)*70 exactly, the
  // correct realized+unrealized split.
  //
  // A FULLY closed symbol splits into two genuinely different cases,
  // since trade.qty (today's NET signed quantity across every fill) is
  // exactly 0 precisely when there was no prior holding at all (shares
  // held at the start of today = sharesNow - todayQty = 0 - 0 = 0) —
  // that's not a coincidence, it's the same arithmetic as the general
  // formula (dailyPnL = currentPrice*sharesNow - prevClose*sharesAtStart
  // - todayValue, with sharesNow=0) collapsing two different ways:
  //   - trade.qty === 0: entered AND fully closed within today. There's
  //     no prior holding to mark against yesterday's close at all (the
  //     prevClose*sharesAtStart term is prevClose*0, zero regardless of
  //     what prevClose even is) -- today's own trade prices alone fully
  //     determine the result, same principle as the unrealized dayPnl
  //     formula using each trade's own execution price for shares bought
  //     today rather than yesterday's close. Previously this case
  //     incorrectly required a prevClose to exist at all (skipping to 0
  //     if the ticker had no price history), even though prevClose was
  //     never actually needed here.
  //   - trade.qty !== 0: there WAS a real prior holding (sharesAtStart =
  //     -trade.qty) that's now fully closed -- those pre-existing shares
  //     still need marking from yesterday's close, same "vs. previous
  //     close" convention every other daily figure on this page uses.
  const closedPositionsRealizedPnl = Object.entries(trades).reduce((sum, [ticker, trade]) => {
    if (positions[ticker]?.shares) return sum // still open — already covered by rows/dayPnl above
    if (Math.abs(trade.qty) < 1e-6) return sum - trade.value
    const prevClose = previousClose(dailyHistory3mo[ticker]) ?? previousClose(monthlyHistory[ticker])
    if (prevClose === null) return sum
    return sum + (prevClose * trade.qty - trade.value)
  }, 0)
  // Unrealized (open positions' mark-to-market) + realized (today's fully
  // closed positions) — the complete picture, confirmed not to double
  // count (see closedPositionsRealizedPnl's own comment on why a
  // partially-closed position's realized slice already lives in
  // positionsDayPnl instead).
  const totalDayPnl = positionsDayPnl + closedPositionsRealizedPnl
  // Net/gross as a share of the account's actual liquidation value —
  // e.g. gross > 100% means leverage (more capital at work than the
  // account is worth).
  const netLiq = account.NetLiquidation
  const netValuePct = netLiq ? netValue / netLiq : null
  const grossValuePct = netLiq ? grossValue / netLiq : null

  const portfolioVol = useMemo(
    () => portfolioVolatilityDecomposition(rows, dailyHistory3mo, monthlyHistory),
    [rows, dailyHistory3mo, monthlyHistory]
  )
  // Portfolio Vol.'s small-font subvalue: the dollar figure as a % of the
  // account's actual NetLiquidation, same "how big is this next to my
  // whole account" framing netValuePct/grossValuePct above already use --
  // not a %-of-returns figure computed from the historical simulation
  // (that used to live here as portfolioVol.volPct; see
  // portfolioVolatilityDecomposition's own comment on why that answers a
  // different question).
  const volOfNetLiqPct = netLiq && portfolioVol.volDollar !== null ? portfolioVol.volDollar / netLiq : null

  const portfolioBeta = useMemo(() => portfolioBetaExposure(rows), [rows])

  return (
    <div className="positions-page positions-view">
      {Object.keys(account).length > 0 && (
        <div className="asset-card">
          <h2>Account</h2>
          <div className="asset-stat-grid">
            {ACCOUNT_FIELDS.filter((f) => account[f.tag] !== undefined).map((f) => {
              const v = account[f.tag]
              const valueClass = f.signed ? (v >= 0 ? 'good' : 'bad') : undefined
              return (
                <div className="asset-stat" key={f.tag}>
                  <span className={`n num${valueClass ? ` ${valueClass}` : ''}`}>{fmtMoney(v)}</span>
                  <span className="l">{f.label}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}

      <header className="masthead">
        <div className="title-block">
          <h1>Positions</h1>
        </div>
        <div className="stat-row">
          <div className="stat">
            <span className="n num">
              {fmtMoney(netValue || null)}
              {netValuePct !== null && <span className="stat-subvalue">{fmtPct(netValuePct)}</span>}
            </span>
            <span className="l">Net Value</span>
          </div>
          <div className="stat">
            <span className="n num">
              {fmtMoney(grossValue || null)}
              {grossValuePct !== null && <span className="stat-subvalue">{fmtPct(grossValuePct)}</span>}
            </span>
            <span className="l">Gross Value</span>
          </div>
          <div className="stat">
            <span className={`n num${totalDayPnl === 0 ? '' : totalDayPnl >= 0 ? ' good' : ' bad'}`}>
              {fmtMoney(totalDayPnl || null)}
            </span>
            <span className="l">Daily P&amp;L</span>
          </div>
          <div
            className="stat"
            title="Today's realized P&L — mark-to-market vs. yesterday's close, for a symbol traded today that's now fully closed out. Positions still open today already have their own realized contribution folded into Daily P&L."
          >
            <span className={`n num${closedPositionsRealizedPnl === 0 ? '' : closedPositionsRealizedPnl >= 0 ? ' good' : ' bad'}`}>
              {fmtMoney(closedPositionsRealizedPnl || null)}
            </span>
            <span className="l">Realized P&amp;L</span>
          </div>
          <div className="stat">
            <span className="n num">{rows.length}</span>
            <span className="l">Positions</span>
          </div>
          <div
            className="stat"
            title={
              portfolioVol.covered < portfolioVol.total
                ? `Priced from ${portfolioVol.covered} of ${portfolioVol.total} positions — the rest have no historical price series available`
                : `Priced from all ${portfolioVol.covered} positions`
            }
          >
            <span className="n num">
              {fmtMoney(portfolioVol.volDollar)}
              {volOfNetLiqPct !== null && <span className="stat-subvalue">{fmtVol(volOfNetLiqPct)}</span>}
            </span>
            <span className="l">Portfolio Vol.</span>
          </div>
          <div
            className="stat"
            title={
              portfolioBeta.beta !== null
                ? `Σ(value × beta) / Σ|value| across all ${portfolioBeta.total} priced positions — a short position's beta subtracts from net market exposure rather than adding to it. ${portfolioBeta.covered} of ${portfolioBeta.total} have an actual beta from yfinance; the rest (e.g. a very recent IPO/spinoff) assume 1.`
                : 'None of the currently held positions are priced to compute this from'
            }
          >
            <span className="n num">{fmtRatio(portfolioBeta.beta)}</span>
            <span className="l">Portfolio Beta</span>
          </div>
          <div
            className="stat"
            title="Σ(value × beta) × 1% — the whole book's expected dollar P&L from a 1% move in the broad market, combining every position's own beta-implied co-movement (long positions add to it, short positions subtract, same sign convention as Portfolio Beta itself)."
          >
            <span className={`n num${portfolioBeta.dollarPer1PctMove === null ? '' : portfolioBeta.dollarPer1PctMove >= 0 ? ' good' : ' bad'}`}>
              {fmtMoney(portfolioBeta.dollarPer1PctMove)}
            </span>
            <span className="l">$ / 1% Market Move</span>
          </div>
        </div>
      </header>

      {weightedSideFactors.length > 0 && (
        <div className="asset-card asset-card-table-overflow-visible">
          <h2>Portfolio Factors (Value-Weighted)</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  {WEIGHTED_TABLE_COLUMNS.map((col) => (
                    <th key={col.key} className={col.className || ''}>
                      {col.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {weightedSideFactors.map((w) => (
                  <WeightedFactorRow
                    key={w.side}
                    side={w.side}
                    count={w.count}
                    netValue={w.netValue}
                    sumWeightPct={w.sumWeightPct}
                    dayPnl={w.dayPnl}
                    factors={w.factors}
                    beta={w.beta}
                    dollarPer1PctMove={w.dollarPer1PctMove}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="table-wrap positions-table-wrap">
        <table>
          <thead>
            <tr>
              <th className="col-left">Position</th>
              <th className="col-left">Sector</th>
              <th className="col-left col-name">Asset / Security</th>
              <th className="col-rec" title="This screener's own forced-distribution rating, from its score percentile — not an analyst consensus.">
                Rating
              </th>
              <th>Shares</th>
              <th>Value</th>
              <th title="Value divided by the account's Net Liquidation — this position's share of the whole account.">
                % of NAV
              </th>
              <th>Avg Price</th>
              <th>Bid</th>
              <th>Ask</th>
              <th>Yesterday</th>
              <th>Price</th>
              <th>Daily %</th>
              <th>Daily $</th>
              <th title="P&amp;L since acquisition">P&amp;L SI</th>
              <th title="Component volatility — this position's exact share of the portfolio's total dollar volatility (see Portfolio Vol.); every position's CVol sums to it precisely. Can be negative for a position that tends to move opposite the rest of the book, genuinely reducing total risk.">
                CVol
              </th>
              <th title="This position's own beta (yfinance) — how much it tends to move per 1 unit of market move, independent of position size. See Portfolio Beta above for the whole book's value-weighted figure.">
                Beta
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr className="status-row">
                <td colSpan={17}>
                  No open positions — or ib_price_server.py isn't running / hasn't reported positions yet.
                </td>
              </tr>
            )}
            {sideGroups.map((sideGroup) =>
              sideGroup.sectorGroups.map((group, groupIndex) => {
                const GroupIcon = getSectorIcon(group.sector)
                return group.rows.map((r, i) => {
                  const pnlClass = perfClass(r.pnlPct, r.shares)
                  // Same +/-0.5% neutral band as PeTable.jsx's Price column —
                  // within that range, IB vs. yfinance is basically noise.
                  const dayClass = perfClass(r.dayPct, r.shares, 0.005)
                  // Same earnings-date-proximity background as PeTable.jsx's
                  // Name cell (see earnings.js) — blank for a ticker outside
                  // the screener universe, like ARKK, since it has no ern/upd.
                  const earningsClass = earningsUrgencyClass(r.ern, r.upd)
                  return (
                    <tr key={r.ticker}>
                      {groupIndex === 0 && i === 0 && (
                        <td
                          className={`col-left side-group-cell${sideGroup.side === 'Short' ? ' side-group-cell-short' : ''}`}
                          rowSpan={sideGroup.rowCount}
                        >
                          <span className="side-group-label">{sideGroup.side}</span>
                          <span className="side-group-total num">{fmtMoney(sideGroup.total)}</span>
                          <span className={`side-group-pnl num ${sideGroup.dayPnl >= 0 ? 'good' : 'bad'}`}>
                            {fmtMoney(sideGroup.dayPnl)}
                          </span>
                        </td>
                      )}
                      {i === 0 && (
                        <td className="col-left sector-group-cell" rowSpan={group.rows.length}>
                          <span className="sector-group-label">
                            <GroupIcon />
                            {sectorGroupLabel(group.sector)}
                          </span>
                          <span className="sector-group-total num">{fmtMoney(group.total)}</span>
                          <span className={`sector-group-pnl num ${group.dayPnl >= 0 ? 'good' : 'bad'}`}>
                            {fmtMoney(group.dayPnl)}
                          </span>
                        </td>
                      )}
                      <td className={`col-left col-name pos-name-cell ${earningsClass}`}>
                        <span className="pos-asset">
                          <a
                            href={`#/asset/${encodeURIComponent(r.ticker)}`}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="ticker-link pos-ticker"
                          >
                            {r.ticker}
                          </a>
                          <span className="pos-name">{r.name}</span>
                        </span>
                      </td>
                      <td className="col-rec">
                        <span className={`rec-badge ${ratingClass(r.rating)}`}>{r.rating || '—'}</span>
                      </td>
                      <td className="num">{fmtShares(r.shares)}</td>
                      <td className="num">{fmtMoney(r.value)}</td>
                      <td className="num">
                        {fmtPct(netLiq && r.value !== null ? r.value / netLiq : null)}
                      </td>
                      <td className="num">{fmtPrice(r.avgCost)}</td>
                      <td className="num"><FlashCell value={r.bid} format={fmtPrice} /></td>
                      <td className="num"><FlashCell value={r.ask} format={fmtPrice} /></td>
                      <td className="num">{fmtPrice(r.referencePrice)}</td>
                      <td className="num"><FlashCell value={r.price} format={fmtPrice} /></td>
                      <td className={`num ${dayClass}`}>{fmtPct(r.dayPct)}</td>
                      <td className={`num ${dayClass}`}>{fmtDollars(r.dayPnl)}</td>
                      <td className={`num ${pnlClass}`}>{fmtPct(r.pnlPct)}</td>
                      <td className="num">{fmtDollars(portfolioVol.cvolByTicker.get(r.ticker) ?? null)}</td>
                      <td className="num">{fmtRatio(r.beta ?? null)}</td>
                    </tr>
                  )
                })
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
