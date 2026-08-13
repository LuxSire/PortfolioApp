import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

// Cash/NAV are magnitudes, not a day's change — no +/- sign clutter. Same
// convention PortfolioView.jsx's own fmtLevel uses for the daily table --
// duplicated here rather than imported, this project's convention for a
// small formatter needed by more than one component (see
// RecommendationsView.jsx's previousClose for the fuller precedent).
function fmtLevel(v) {
  if (v === null || v === undefined) return '—'
  return '$' + v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

function fmtAxisDate(iso) {
  const d = new Date(iso + 'T00:00:00')
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

const CHART_FONT = 'ui-monospace, "SF Mono", "Cascadia Code", "Roboto Mono", Menlo, monospace'
const CHART_TICK_STYLE = { fill: 'var(--muted)', fontSize: 10, fontFamily: CHART_FONT }

// Net/Long/Short/Gross legend -- "legend always present for two or more
// series" (dataviz skill's marks-and-anatomy.md). Gross has no swatch of
// its own: it's not a fourth color, just the Long+Short stack's total
// (see ExposureTooltip, which shows its value without a color key for the
// same reason).
function ExposureLegend() {
  return (
    <span className="chart-legend">
      <span className="chart-legend-item">
        <span className="chart-legend-swatch" style={{ '--swatch-color': 'var(--viz-net)' }} />
        Net
      </span>
      <span className="chart-legend-item">
        <span className="chart-legend-swatch" style={{ '--swatch-color': 'var(--viz-long)' }} />
        Long
      </span>
      <span className="chart-legend-item">
        <span className="chart-legend-swatch" style={{ '--swatch-color': 'var(--viz-short)' }} />
        Short
      </span>
    </span>
  )
}

function ExposureTooltip({ active, payload }) {
  if (!active || !payload || !payload.length) return null
  const point = payload[0]?.payload
  if (!point) return null
  return (
    <div className="chart-tooltip chart-tooltip-follow-cursor">
      <span className="chart-tooltip-date">{fmtAxisDate(point.date)}</span>
      <span className="chart-tooltip-rows">
        <span className="chart-tooltip-row">
          <span className="chart-tooltip-key" style={{ '--key-color': 'var(--viz-net)' }} />
          <span className="chart-tooltip-row-label">Net</span>
          <span className="chart-tooltip-row-value">{fmtLevel(point.stockNet)}</span>
        </span>
        <span className="chart-tooltip-row">
          <span className="chart-tooltip-key" style={{ '--key-color': 'var(--viz-long)' }} />
          <span className="chart-tooltip-row-label">Long</span>
          <span className="chart-tooltip-row-value">{fmtLevel(point.stockLong)}</span>
        </span>
        <span className="chart-tooltip-row">
          <span className="chart-tooltip-key" style={{ '--key-color': 'var(--viz-short)' }} />
          <span className="chart-tooltip-row-label">Short</span>
          <span className="chart-tooltip-row-value">{fmtLevel(point.stockShort)}</span>
        </span>
        <span className="chart-tooltip-row">
          <span className="chart-tooltip-key" style={{ background: 'transparent' }} />
          <span className="chart-tooltip-row-label">Gross</span>
          <span className="chart-tooltip-row-value">{fmtLevel(point.stockGross)}</span>
        </span>
      </span>
    </div>
  )
}

// Daily net/gross market exposure -- same rows/date axis as NavChart, so
// the two read as one timeline when stacked in PortfolioView.jsx. Net is
// its own bar (stockNet, signed -- long-biased books run positive, but
// the axis/domain below still makes room for a net-short day). Gross is a
// second, stacked bar: stockLong (already ≥ 0) at the base, |stockShort|
// on top (stockShort itself comes back negative from the Flex Query --
// see ib_price_server.py's fetch_account_performance), summing to
// stockGross exactly -- explicit instruction: "the gross exposure bar
// must show long and short absolute value sum," not just the combined
// total as a single flat color. Net and Gross group side by side per day
// (Recharts groups distinct stackIds automatically) rather than stacking
// all three together, since Net isn't a component OF Gross -- it's Long
// minus |Short|, a different figure entirely.
// rows: PortfolioView.jsx's own portfolio_performance.json rows
// ({date, stockNet, stockLong, stockShort, stockGross, ...}, ascending by
// date).
export default function ExposureChart({ rows }) {
  const data = rows.map((r) => ({
    ...r,
    stockShortAbs: r.stockShort === null || r.stockShort === undefined ? null : Math.abs(r.stockShort),
  }))

  const values = data.flatMap((r) => [r.stockNet, r.stockGross]).filter((v) => v !== null && v !== undefined)
  const maxVal = Math.max(0, ...values)
  const minVal = Math.min(0, ...values)
  const domainPad = (maxVal - minVal || 1) * 0.1
  const yMax = maxVal + domainPad
  const yMin = minVal === 0 ? 0 : minVal - domainPad

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
        Exposure
        <ExposureLegend />
      </h2>
      <div className="chart-wrap">
        <ResponsiveContainer width="100%" height={260}>
          <BarChart data={data} margin={{ top: 12, right: 4, bottom: 4, left: 4 }}>
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
              ticks={[yMin, yMax]}
              tickFormatter={fmtLevel}
              orientation="right"
              width={64}
              axisLine={false}
              tickLine={false}
              tick={CHART_TICK_STYLE}
            />
            <Tooltip
              content={<ExposureTooltip />}
              cursor={{ fill: 'var(--surface-2)' }}
              wrapperStyle={{ zIndex: 10 }}
            />
            <Bar dataKey="stockNet" name="Net" fill="var(--viz-net)" radius={[4, 4, 0, 0]} maxBarSize={24} isAnimationActive={false} />
            <Bar
              dataKey="stockLong"
              name="Long"
              stackId="gross"
              fill="var(--viz-long)"
              radius={[0, 0, 0, 0]}
              maxBarSize={24}
              isAnimationActive={false}
            />
            <Bar
              dataKey="stockShortAbs"
              name="Short"
              stackId="gross"
              fill="var(--viz-short)"
              radius={[4, 4, 0, 0]}
              maxBarSize={24}
              isAnimationActive={false}
            />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}
