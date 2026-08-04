import { useEffect, useMemo, useState } from 'react'
import { parseCSV } from './csv'
import { earningsUrgencyClass } from './earnings'
import { getSectorGroup } from './sectorGroups'
import { getSectorIcon } from './sectorIcons'
import { IB_STREAM_URL } from './ibStream'

// Same curated tags + order as IBApp.ACCOUNT_STATUS_TAGS; PnL fields get
// the good/bad sign coloring the rest of the app uses.
const ACCOUNT_FIELDS = [
  { tag: 'NetLiquidation', label: 'Net Liquidation' },
  { tag: 'TotalCashValue', label: 'Total Cash' },
  { tag: 'AvailableFunds', label: 'Available Funds' },
  { tag: 'ExcessLiquidity', label: 'Excess Liquidity' },
  { tag: 'BuyingPower', label: 'Buying Power' },
  { tag: 'UnrealizedPnL', label: 'Unrealized P&L', signed: true },
  { tag: 'RealizedPnL', label: 'Realized P&L', signed: true },
  { tag: 'DailyPnL', label: 'Daily P&L', signed: true },
]

function toNum(v) {
  if (v === undefined || v === null || v === '') return null
  const n = Number(v)
  return Number.isNaN(n) ? null : n
}

function fmtPrice(v) {
  if (v === null) return '—'
  return '$' + v.toFixed(2)
}

// The last close strictly before today from a {date, close} bar series
// (price_history_daily_3mo.json / price_history.json) — never today's own
// entry, which both sources can carry as a still-forming bar (close =
// latest price so far, not a settled close) when fetched intraday.
// Comparing a live price against that same-day bar instead of a real
// prior close silently understates or misreports the day's actual move.
function previousClose(series) {
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
function FlashCell({ value, format }) {
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
function fmtShares(v) {
  if (v === null) return '—'
  return v.toLocaleString(undefined, { maximumFractionDigits: Number.isInteger(v) ? 0 : 4 })
}

function fmtMoney(v) {
  if (v === null) return '—'
  return '$' + v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

// Same as fmtMoney but without the '$' — the Daily $ column already carries
// its currency in the header, so a sign on every row is redundant clutter.
function fmtDollars(v) {
  if (v === null) return '—'
  return v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}


function fmtPct(v) {
  if (v === null) return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(2) + '%'
}

// good/bad for a price ratio (daily change, or price vs. avgCost) in terms
// of real gain/loss, not the raw sign of the ratio — a short position
// (negative shares) profits when the price falls, so a positive ratio
// there is a loss and must color red, the mirror of a long position.
// neutralBand is a magnitude threshold on the raw ratio (e.g. IB-vs-
// yfinance noise), evaluated before the direction flip.
function perfClass(ratio, shares, neutralBand = 0) {
  if (ratio === null || shares === null) return ''
  if (Math.abs(ratio) <= neutralBand) return ''
  const effective = shares >= 0 ? ratio : -ratio
  return effective >= 0 ? 'good' : 'bad'
}

// Volatility has no sign — it's a magnitude, not a direction — so it skips
// fmtPct's +/- prefix.
function fmtVol(v) {
  if (v === null) return '—'
  return (v * 100).toFixed(2) + '%'
}

// Simulates "if I'd held today's exact position sizes for the last ~3
// months" by pricing today's share counts against each ticker's historical
// daily closes, then takes the standard deviation of that simulated
// portfolio's own daily returns — not an average of each position's
// individual volatility, which would ignore that positions move together
// or offset each other. A date is only included if every priced ticker has
// a close for it, so the simulated portfolio's composition (and therefore
// its dollar value) is apples-to-apples from one day to the next; a ticker
// missing from both price_history_daily_3mo.json and price_history.json
// (no history at all) is left out of the simulation rather than treated as
// flat, so it doesn't silently mute the real number.
function portfolioDailyVolatility(rows, dailyHistory3mo, monthlyHistory) {
  const priced = []
  for (const r of rows) {
    if (!r.shares) continue
    const series = dailyHistory3mo[r.ticker] || monthlyHistory[r.ticker]
    if (!series || series.length < 6) continue
    priced.push({ ticker: r.ticker, shares: r.shares, byDate: new Map(series.map((p) => [p.date, p.close])) })
  }
  if (priced.length === 0) return { vol: null, covered: 0, total: rows.filter((r) => r.shares).length }

  let commonDates = new Set(priced[0].byDate.keys())
  for (const p of priced.slice(1)) {
    commonDates = new Set([...commonDates].filter((d) => p.byDate.has(d)))
  }
  const sortedDates = [...commonDates].sort()

  const portfolioValues = sortedDates.map((date) =>
    priced.reduce((sum, p) => sum + p.shares * p.byDate.get(date), 0)
  )
  if (portfolioValues.length < 6) return { vol: null, covered: priced.length, total: rows.filter((r) => r.shares).length }

  const returns = []
  for (let i = 1; i < portfolioValues.length; i++) {
    const prev = portfolioValues[i - 1]
    if (prev) returns.push(portfolioValues[i] / prev - 1)
  }
  if (returns.length < 5) return { vol: null, covered: priced.length, total: rows.filter((r) => r.shares).length }
  const mean = returns.reduce((a, b) => a + b, 0) / returns.length
  const variance = returns.reduce((a, b) => a + (b - mean) ** 2, 0) / (returns.length - 1)
  return { vol: Math.sqrt(variance), covered: priced.length, total: rows.filter((r) => r.shares).length }
}

// Stocks only — see ib_price_server.py's docstring on why an option and its
// underlying can't share this ticker-symbol-keyed price stream.
export default function PositionsView() {
  // {ticker: {name, sector, price, ern, upd}}, best-effort labeling + the
  // yfinance price sorted_screen.csv already has, as a fallback for
  // tickers ib_price_server.py hasn't (or can't) get a live quote for.
  // ern/upd feed earningsUrgencyClass, same as PeTable.jsx's Name cell.
  const [tickerInfo, setTickerInfo] = useState({})
  const [prices, setPrices] = useState({})
  const [positions, setPositions] = useState({})
  const [account, setAccount] = useState({})
  // Daily-close series for volatility. price_history_daily_3mo.json is IB
  // Gateway's own history and always covers every held ticker (see
  // ib_price_server.py's fetch_candlestick_history — held positions are
  // unioned in regardless of the ranked-tickers budget), so it's the
  // primary source; price_history.json (yfinance, 1mo, broader but not
  // guaranteed to include every held ticker) is the fallback for a
  // position IB Gateway hasn't fetched history for yet.
  const [dailyHistory3mo, setDailyHistory3mo] = useState({})
  const [monthlyHistory, setMonthlyHistory] = useState({})

  useEffect(() => {
    fetch('/sorted_screen.csv')
      .then((r) => (r.ok ? r.text() : ''))
      .then((text) => {
        const info = {}
        for (const row of parseCSV(text)) {
          info[row.ticker] = {
            name: row.name,
            sector: row.sector,
            price: toNum(row.price),
            ern: toNum(row.earningsTimestampStart),
            upd: row.lastDownload || null,
          }
        }
        setTickerInfo(info)
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
      const { prices: p, positions: pos, account: acc } = JSON.parse(e.data)
      setPrices(p)
      setAccount(acc || {})
      setPositions(pos)
    }
    source.onerror = () => {} // EventSource auto-reconnects; nothing to do here.
    return () => source.close()
  }, [])

  const rows = useMemo(() => {
    return Object.entries(positions).map(([ticker, pos]) => {
      const info = tickerInfo[ticker]
      // Guard every input with Number.isFinite, not just a null check —
      // `undefined * price` and `NaN`-anything both silently produce NaN
      // rather than throwing, and NaN isn't caught by `!== null` or `??`
      // (NaN ?? 0 is still NaN). A malformed or missing value anywhere in
      // this chain should fall back to "unknown" (null, rendered as "—"),
      // never a NaN that then poisons every sum built on top of it.
      const shares = Number.isFinite(pos?.shares) ? pos.shares : null
      const avgCost = Number.isFinite(pos?.avgCost) ? pos.avgCost : null
      // IB Gateway's live tick first; sorted_screen.csv's yfinance price
      // (same one the screener falls back to) when IB has no quote for it —
      // e.g. a held ticker outside the screener's usual universe, or the
      // price server isn't running at all.
      const ibPrice = Number.isFinite(prices[ticker]?.last) ? prices[ticker].last : null
      const bid = Number.isFinite(prices[ticker]?.bid) ? prices[ticker].bid : null
      const ask = Number.isFinite(prices[ticker]?.ask) ? prices[ticker].ask : null
      const yfPrice = Number.isFinite(info?.price) ? info.price : null
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
      // requires both, never falls back, unlike `price` above. dayPnl is
      // its dollar form, for the header's Daily P&L sum.
      const dayPct = ibPrice !== null && referencePrice ? ibPrice / referencePrice - 1 : null
      const dayPnl = shares !== null && ibPrice !== null && referencePrice !== null ? shares * (ibPrice - referencePrice) : null
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
        bid,
        ask,
        value,
        pnlPct,
        dayPct,
        dayPnl,
        ern: info?.ern ?? null,
        upd: info?.upd ?? null,
      }
    })
  }, [positions, prices, tickerInfo, dailyHistory3mo, monthlyHistory])

  const groups = useMemo(() => {
    const bySector = new Map()
    for (const r of rows) {
      if (!bySector.has(r.sector)) bySector.set(r.sector, [])
      bySector.get(r.sector).push(r)
    }
    const list = [...bySector.entries()].map(([sector, sectorRows]) => {
      sectorRows.sort((a, b) => (b.value ?? -1) - (a.value ?? -1))
      const total = sectorRows.reduce((s, r) => s + (r.value ?? 0), 0)
      const dayPnl = sectorRows.reduce((s, r) => s + (r.dayPnl ?? 0), 0)
      return { sector, rows: sectorRows, total, dayPnl }
    })
    list.sort((a, b) => b.total - a.total)
    return list
  }, [rows])

  // Net: long and short values offset (a short's value is negative, since
  // its shares are negative) — the portfolio's actual directional exposure.
  // Gross: every position's magnitude summed regardless of side — total
  // capital at work either way.
  const netValue = rows.reduce((s, r) => s + (r.value ?? 0), 0)
  const grossValue = rows.reduce((s, r) => s + Math.abs(r.value ?? 0), 0)
  // Sum of each position's dollar dayPnl (see rows above) — same IB-vs-
  // yfinance daily proxy as the Daily % column, just summed in dollars.
  // On its own this only ever covers positions still open right now, so
  // a trade closed out earlier today would vanish from it the moment
  // it's flat, even though it really made or lost money today — adding
  // IB's own account-wide RealizedPnL (today's closed-trade P&L, from
  // the account summary — see IBApp.ACCOUNT_STATUS_TAGS) covers that gap.
  const positionsDayPnl = rows.reduce((s, r) => s + (r.dayPnl ?? 0), 0)
  const realizedPnl = Number.isFinite(account.RealizedPnL) ? account.RealizedPnL : 0
  const totalDayPnl = positionsDayPnl + realizedPnl
  // Net/gross as a share of the account's actual liquidation value —
  // e.g. gross > 100% means leverage (more capital at work than the
  // account is worth).
  const netLiq = account.NetLiquidation
  const netValuePct = netLiq ? netValue / netLiq : null
  const grossValuePct = netLiq ? grossValue / netLiq : null

  const portfolioVol = useMemo(
    () => portfolioDailyVolatility(rows, dailyHistory3mo, monthlyHistory),
    [rows, dailyHistory3mo, monthlyHistory]
  )
  // Dollar terms against today's live net value (not the simulation's own
  // last historical close), so it reads as "today's typical daily swing in
  // dollars" — Math.abs since volatility is a magnitude, even for a net
  // short book where netValue itself is negative.
  const portfolioVolDollar = portfolioVol.vol !== null ? portfolioVol.vol * Math.abs(netValue) : null

  return (
    <div className="positions-page">
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
              {fmtMoney(portfolioVolDollar)}
              {portfolioVol.vol !== null && <span className="stat-subvalue">{fmtVol(portfolioVol.vol)}</span>}
            </span>
            <span className="l">Portfolio Vol.</span>
          </div>
        </div>
      </header>

      <div className="table-wrap positions-table-wrap">
        <table>
          <thead>
            <tr>
              <th className="col-left">Sector</th>
              <th className="col-left col-name">Asset / Security</th>
              <th>Shares</th>
              <th>Value</th>
              <th>Bid</th>
              <th>Ask</th>
              <th>Price</th>
              <th>Daily %</th>
              <th>Daily $</th>
              <th>P&amp;L since acquisition</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr className="status-row">
                <td colSpan={10}>
                  No open positions — or ib_price_server.py isn't running / hasn't reported positions yet.
                </td>
              </tr>
            )}
            {groups.map((group) => {
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
                    {i === 0 && (
                      <td className="col-left sector-group-cell" rowSpan={group.rows.length}>
                        <span className="sector-group-label">
                          <GroupIcon />
                          {group.sector}
                        </span>
                        <span className="sector-group-total num">{fmtMoney(group.total)}</span>
                        <span className={`sector-group-pnl num ${group.dayPnl >= 0 ? 'good' : 'bad'}`}>
                          {fmtMoney(group.dayPnl)}
                        </span>
                      </td>
                    )}
                    <td className={`col-left col-name pos-name-cell ${earningsClass}`}>
                      <span className="pos-asset">
                        <a href={`#/asset/${encodeURIComponent(r.ticker)}`} className="ticker-link pos-ticker">
                          {r.ticker}
                        </a>
                        <span className="pos-name">{r.name}</span>
                      </span>
                    </td>
                    <td className="num">{fmtShares(r.shares)}</td>
                    <td className="num">{fmtMoney(r.value)}</td>
                    <td className="num"><FlashCell value={r.bid} format={fmtPrice} /></td>
                    <td className="num"><FlashCell value={r.ask} format={fmtPrice} /></td>
                    <td className="num"><FlashCell value={r.price} format={fmtPrice} /></td>
                    <td className={`num ${dayClass}`}>{fmtPct(r.dayPct)}</td>
                    <td className={`num ${dayClass}`}>{fmtDollars(r.dayPnl)}</td>
                    <td className={`num ${pnlClass}`}>{fmtPct(r.pnlPct)}</td>
                  </tr>
                )
              })
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
