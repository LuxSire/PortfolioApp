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
  const navs = rows.map((r) => r.nav).filter((v) => v !== null)
  const lo = Math.min(...navs)
  const hi = Math.max(...navs)
  const domainPad = (hi - lo || 1) * 0.12
  const yMin = lo - domainPad
  const yMax = hi + domainPad

  const first = rows[0]
  const last = rows[rows.length - 1]
  const changePct = first.nav ? last.nav / first.nav - 1 : null

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
              <YAxis hide domain={['auto', 'auto']} />
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
