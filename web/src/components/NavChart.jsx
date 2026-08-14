import { Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, ReferenceDot, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

// Cash/NAV are magnitudes, not a day's change — no +/- sign clutter. Same
// convention PortfolioView.jsx's own fmtLevel uses for the daily table —
// duplicated here rather than imported, this project's convention for a
// small formatter needed by more than one component (see
// RecommendationsView.jsx's previousClose for the fuller precedent).
function fmtLevel(v) {
  if (v === null || v === undefined) return '—'
  return '$' + v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

// Signed, unlike fmtLevel above — a day's P&L is a change, not a level, so
// it gets the same +/-$ sign convention PortfolioView.jsx's own fmtMoney
// uses for its Realized/Unrealized/Total P&L columns.
function fmtPnl(v) {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+$' : '-$') + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })
}

function fmtAxisDate(iso) {
  const d = new Date(iso + 'T00:00:00')
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

// The chart itself plots NAV rebased to 100 on the first day (an index,
// not a dollar figure), so it reads as "growth from a common start point"
// the way a benchmark-comparison chart would — 2 decimals since index
// points move in much finer increments than whole dollars do.
function fmtIndex(v) {
  if (v === null || v === undefined) return '—'
  return v.toFixed(2)
}

const CHART_FONT = 'ui-monospace, "SF Mono", "Cascadia Code", "Roboto Mono", Menlo, monospace'
const CHART_TICK_STYLE = { fill: 'var(--muted)', fontSize: 10, fontFamily: CHART_FONT }

function NavTooltip({ active, payload }) {
  if (!active || !payload || !payload.length) return null
  const point = payload[0].payload
  return (
    <div className="chart-tooltip">
      <span className="chart-tooltip-value">{fmtIndex(point.navIndex)}</span>
      <span className="chart-tooltip-date">
        {fmtLevel(point.nav)} · {fmtAxisDate(point.date)}
      </span>
    </div>
  )
}

// Same good/bad sign convention every P&L figure elsewhere in this app
// already uses (see PortfolioView.tsx's own Total P&L column, which this
// mirrors exactly — dayTotalPnl below is that same realized+unrealized
// sum, just computed here rather than imported).
function PnlTooltip({ active, payload }) {
  if (!active || !payload || !payload.length) return null
  const point = payload[0].payload
  if (point.dayTotalPnl === null || point.dayTotalPnl === undefined) return null
  return (
    <div className="chart-tooltip">
      <span className={`chart-tooltip-value ${point.dayTotalPnl >= 0 ? 'good' : 'bad'}`}>
        {fmtPnl(point.dayTotalPnl)}
      </span>
      <span className="chart-tooltip-date">{fmtAxisDate(point.date)}</span>
    </div>
  )
}

// Shared by both charts below — same rows, same date axis, so a tick at
// (say) every ~1/5th of the window lines the NAV curve and the P&L bars
// underneath it up into one readable timeline when stacked, rather than
// each picking its own independent tick set.
function dateTicks(rows) {
  const xTickCount = Math.min(5, rows.length)
  return [
    ...new Set(
      Array.from({ length: xTickCount }, (_, i) =>
        rows[Math.round((i * (rows.length - 1)) / (xTickCount - 1 || 1))].date
      )
    ),
  ]
}

// NAV over every day the Flex Query covers — same chart anatomy as
// Asset.jsx's PriceChart (dataviz skill: one hue, thin line, light fill,
// a handful of deduped x-ticks) so it reads consistently with the rest of
// the app, just plotting account NAV instead of a single stock's price.
// rows: PortfolioView.jsx's own portfolio_performance.json rows
// ({date, nav, ...}, ascending by date).
export default function NavChart({ rows }) {
  const first = rows[0]
  const last = rows[rows.length - 1]
  const changePct = first.nav ? last.nav / first.nav - 1 : null

  // Rebased to 100 on the first day -- an index, not a dollar level, so
  // the plotted line/axis read as "growth from a common start point"
  // rather than the account's actual dollar scale (still shown in the
  // header stat above and the tooltip's secondary line).
  const indexedRows = rows.map((r) => ({
    ...r,
    navIndex: r.nav !== null && first.nav ? (r.nav / first.nav) * 100 : null,
  }))
  const indexVals = indexedRows.map((r) => r.navIndex).filter((v) => v !== null)
  const lo = Math.min(...indexVals)
  const hi = Math.max(...indexVals)
  const domainPad = (hi - lo || 1) * 0.12
  const yMin = lo - domainPad
  const yMax = hi + domainPad

  const lastIndex = indexedRows[indexedRows.length - 1].navIndex

  const xTicks = dateTicks(rows)

  // Total P&L per day (realized + unrealized) — same computation
  // PortfolioView.tsx's own table does per row, duplicated here rather
  // than passed in as a prop since NavChart already receives the full
  // PortfolioDayRow set it needs. null (not 0) when either side is
  // missing, same "unknown, not flat" treatment the table gives it —
  // Bar/Cell below just skip a null point rather than drawing a bar at 0.
  const pnlData = rows.map((r) => ({
    date: r.date,
    dayTotalPnl: r.realized !== null && r.unrealized !== null ? r.realized + r.unrealized : null,
  }))
  const pnlValues = pnlData.map((r) => r.dayTotalPnl).filter((v) => v !== null)
  const totalPnl = pnlValues.length ? pnlValues.reduce((a, b) => a + b, 0) : null
  // Same "pad around the real min/max, but never hide 0" domain rule
  // ExposureChart's Net/Gross axis uses -- a P&L series that's all one
  // sign still gets 0 in its domain so the ReferenceLine's baseline is
  // never clipped off the visible axis.
  const pnlMax = Math.max(0, ...pnlValues)
  const pnlMin = Math.min(0, ...pnlValues)
  const pnlDomainPad = (pnlMax - pnlMin || 1) * 0.1
  const pnlYMax = pnlMax + pnlDomainPad
  const pnlYMin = pnlMin === 0 ? 0 : pnlMin - pnlDomainPad

  return (
    <>
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
            <AreaChart data={indexedRows} margin={{ top: 12, right: 4, bottom: 4, left: 4 }}>
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
                tickFormatter={fmtIndex}
                orientation="right"
                width={64}
                axisLine={false}
                tickLine={false}
                tick={CHART_TICK_STYLE}
              />
              <Tooltip content={<NavTooltip />} cursor={{ stroke: 'var(--muted)', strokeOpacity: 0.5 }} />
              <Area
                type="monotone"
                dataKey="navIndex"
                stroke="var(--accent)"
                strokeWidth={2}
                fill="var(--accent)"
                fillOpacity={0.1}
                dot={false}
                activeDot={{ r: 4, fill: 'var(--accent)', stroke: 'var(--surface)', strokeWidth: 2 }}
                isAnimationActive={false}
                connectNulls
              />
              <ReferenceDot x={last.date} y={lastIndex} r={4} fill="var(--accent)" stroke="var(--surface)" strokeWidth={2} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="asset-card">
        <h2>
          Daily P&amp;L
          {totalPnl !== null && (
            <span className={`chart-change ${totalPnl >= 0 ? 'good' : 'bad'}`}>{fmtPnl(totalPnl)}</span>
          )}
        </h2>
        <div className="chart-wrap">
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={pnlData} margin={{ top: 12, right: 4, bottom: 4, left: 4 }}>
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
                domain={[pnlYMin, pnlYMax]}
                ticks={[pnlYMin, 0, pnlYMax]}
                tickFormatter={fmtPnl}
                orientation="right"
                width={64}
                axisLine={false}
                tickLine={false}
                tick={CHART_TICK_STYLE}
              />
              <ReferenceLine y={0} stroke="var(--line)" />
              <Tooltip content={<PnlTooltip />} cursor={{ fill: 'var(--surface-2)' }} />
              <Bar dataKey="dayTotalPnl" isAnimationActive={false} radius={[2, 2, 2, 2]} maxBarSize={16}>
                {pnlData.map((d) => (
                  <Cell key={d.date} fill={d.dayTotalPnl === null ? 'transparent' : d.dayTotalPnl >= 0 ? 'var(--good)' : 'var(--bad)'} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </>
  )
}
