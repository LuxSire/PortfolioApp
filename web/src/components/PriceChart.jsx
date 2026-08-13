import { Area, AreaChart, CartesianGrid, ReferenceDot, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

function fmtPrice(v) {
  if (typeof v !== 'number') return '—'
  return '$' + v.toFixed(2)
}

function fmtAxisDate(iso) {
  const d = new Date(iso + 'T00:00:00')
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

const CHART_FONT = 'ui-monospace, "SF Mono", "Cascadia Code", "Roboto Mono", Menlo, monospace'
const CHART_TICK_STYLE = { fill: 'var(--muted)', fontSize: 10, fontFamily: CHART_FONT }

function ChartTooltip({ active, payload }) {
  if (!active || !payload || !payload.length) return null
  const point = payload[0].payload
  return (
    <div className="chart-tooltip">
      <span className="chart-tooltip-value">{fmtPrice(point.close)}</span>
      <span className="chart-tooltip-date">{fmtAxisDate(point.date)}</span>
    </div>
  )
}

// price_history.json (see main.py's add_momentum_and_persist_history) is
// the trailing ~1 month of daily closes captured from the same yfinance
// fetch that already computes the screener's momentum score — no separate
// API, so this only ever plots what that fetch last saw.
export default function PriceChart({ data }) {
  const closes = data.map((d) => d.close)
  const lo = Math.min(...closes)
  const hi = Math.max(...closes)
  const domainPad = (hi - lo || 1) * 0.12
  const yMin = lo - domainPad
  const yMax = hi + domainPad

  const first = data[0]
  const last = data[data.length - 1]
  const changePct = first.close ? last.close / first.close - 1 : null

  // A handful of evenly spaced date labels, not just the endpoints — up to
  // 5, deduped (fewer than 5 data points would otherwise repeat a date).
  const xTickCount = Math.min(5, data.length)
  const xTicks = [
    ...new Set(
      Array.from({ length: xTickCount }, (_, i) =>
        data[Math.round((i * (data.length - 1)) / (xTickCount - 1 || 1))].date
      )
    ),
  ]

  return (
    <div className="asset-card">
      <h2>
        Price History
        <span className="chart-last-price">{fmtPrice(last.close)}</span>
        {changePct !== null && (
          <span className={`chart-change ${changePct >= 0 ? 'good' : 'bad'}`}>
            {(changePct >= 0 ? '+' : '') + (changePct * 100).toFixed(1)}%
          </span>
        )}
      </h2>
      <div className="chart-wrap">
        <ResponsiveContainer width="100%" height={260}>
          <AreaChart data={data} margin={{ top: 12, right: 4, bottom: 4, left: 4 }}>
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
              tickFormatter={fmtPrice}
              orientation="right"
              width={56}
              axisLine={false}
              tickLine={false}
              tick={CHART_TICK_STYLE}
            />
            <Tooltip content={<ChartTooltip />} cursor={{ stroke: 'var(--muted)', strokeOpacity: 0.5 }} />
            <Area
              type="monotone"
              dataKey="close"
              stroke="var(--accent)"
              strokeWidth={2}
              fill="var(--accent)"
              fillOpacity={0.1}
              dot={false}
              activeDot={{ r: 4, fill: 'var(--accent)', stroke: 'var(--surface)', strokeWidth: 2 }}
              isAnimationActive={false}
            />
            <ReferenceDot
              x={last.date}
              y={last.close}
              r={4}
              fill="var(--accent)"
              stroke="var(--surface)"
              strokeWidth={2}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}
