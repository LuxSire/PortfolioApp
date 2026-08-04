import { useEffect, useState } from 'react'
import { Area, AreaChart, CartesianGrid, ReferenceDot, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

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
  // Running total of realized+unrealized through each day, keyed by date —
  // rows is already date-ascending, so a single pass gives each day's
  // cumulative P&L as of that day's close.
  const cumulativePnlByDate = {}
  if (rows) {
    let running = 0
    for (const r of rows) {
      if (r.realized !== null && r.unrealized !== null) running += r.realized + r.unrealized
      cumulativePnlByDate[r.date] = running
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

  return (
    <div className="positions-page">
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
              <span className="l">Deposits/Withdrawals</span>
            </div>
          </div>
        )}
      </header>

      {error && <p className="status-row">Couldn't load portfolio_performance.json — run: python ib_price_server.py performance</p>}
      {!error && !data && <p className="status-row">Loading…</p>}

      {rows && rows.length > 0 && <NavChart rows={rows} />}

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
                <th>Deposits/Withdrawals</th>
                <th>Commissions</th>
                <th>Dividends</th>
                <th>Interest</th>
                <th>Realized</th>
                <th>Unrealized</th>
                <th>Total P&amp;L</th>
                <th>Cumulative P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 && (
                <tr className="status-row">
                  <td colSpan={15}>No daily rows in the query response.</td>
                </tr>
              )}
              {[...rows].reverse().map((r) => {
                const totalPnl = r.realized !== null && r.unrealized !== null ? r.realized + r.unrealized : null
                const cumulativePnl = cumulativePnlByDate[r.date]
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
                    <td className={`num ${cumulativePnl >= 0 ? 'good' : 'bad'}`}>{fmtMoneyPlain(cumulativePnl)}</td>
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
