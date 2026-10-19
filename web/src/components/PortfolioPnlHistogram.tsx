import { Bar, BarChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

// Same monospace tick font every other recharts chart in this app uses
// (see SectorPosValueChart/SimPriceRangeChart's own copy of this comment).
const CHART_FONT = 'ui-monospace, "SF Mono", "Cascadia Code", "Roboto Mono", Menlo, monospace'
const CHART_TICK_STYLE = { fill: 'var(--muted)', fontSize: 10, fontFamily: CHART_FONT }

function fmtMoneyShort(v: number): string {
  const sign = v < 0 ? '-' : ''
  return sign + '$' + Math.abs(Math.round(v)).toLocaleString()
}

function mean(values: number[]): number {
  return values.reduce((a, b) => a + b, 0) / values.length
}

function median(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2
}

export interface PortfolioPnlHistogramProps {
  // The portfolio's own simulated daily $ P&L series (today's exact
  // position sizes applied to each day's actual historical close) --
  // PositionsView.tsx's portfolioVolatilityDecomposition's dPortfolio,
  // the SAME series Portfolio Vol./VaR 95%/VaR 99% above are computed
  // from, so this histogram is literally what those three stats summarize.
  values: number[]
  // Positive dollar loss figures (portfolioVolatilityDecomposition's own
  // var95Dollar/var99Dollar) -- drawn as vertical reference lines at
  // -var95/-var99 on the $ P&L axis.
  var95: number | null
  var99: number | null
  bins?: number
}

// Histogram of the book's own historical daily $ P&L, with mean, median,
// VaR 95% and VaR 99% all marked directly on the distribution they're
// drawn from -- explicit instruction, 2026-10-19, at the bottom of the
// Positions page (mean/median added same day, same instruction).
// Deliberately the raw empirical distribution (equal-width $ buckets, day
// count per bucket), not a fitted normal curve -- same "no normal-
// distribution shortcut" principle the VaR stats above already use.
export default function PortfolioPnlHistogram({ values, var95, var99, bins = 24 }: PortfolioPnlHistogramProps) {
  if (values.length < 5) return null
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const width = span / bins
  const counts = new Array(bins).fill(0)
  for (const v of values) {
    let idx = Math.floor((v - min) / width)
    if (idx >= bins) idx = bins - 1
    if (idx < 0) idx = 0
    counts[idx]++
  }
  // Bin midpoint doubles as both the Bar's x position (numeric XAxis, same
  // $ scale the ReferenceLines below use) and the Tooltip's own label.
  const data = counts.map((count, i) => ({ mid: min + width * (i + 0.5), count }))
  const meanPnl = mean(values)
  const medianPnl = median(values)

  return (
    <div className="asset-card">
      <h2>Historical Daily P&amp;L Distribution</h2>
      <p className="status-row">
        {values.length} trading days of this book's CURRENT positions (today's exact sizes applied to each day's
        actual historical close — same series Portfolio Vol./VaR 95%/VaR 99% above summarize) — each bar is one $
        bucket's day count. Mean {fmtMoneyShort(meanPnl)}, median {fmtMoneyShort(medianPnl)}.
      </p>
      <div className="chart-wrap chart-wrap-full-bleed">
        <ResponsiveContainer width="100%" height={240}>
          <BarChart data={data} margin={{ top: 28, right: 20, bottom: 8, left: 8 }}>
            <CartesianGrid stroke="var(--line)" vertical={false} />
            <XAxis
              type="number"
              dataKey="mid"
              domain={[min, max]}
              tick={CHART_TICK_STYLE}
              tickFormatter={fmtMoneyShort}
              axisLine={false}
              tickLine={false}
            />
            <YAxis tick={CHART_TICK_STYLE} axisLine={false} tickLine={false} allowDecimals={false} width={28} />
            <Tooltip
              formatter={(v: number) => [v, 'Days']}
              labelFormatter={(v: number) => fmtMoneyShort(v)}
              contentStyle={{
                background: 'var(--surface-2)',
                border: '1px solid var(--line)',
                fontSize: 12,
                fontFamily: CHART_FONT,
              }}
            />
            <Bar dataKey="count" fill="var(--line)" isAnimationActive={false} />
            <ReferenceLine
              x={meanPnl}
              stroke="var(--focus)"
              strokeWidth={2}
              label={{ value: 'Mean', position: 'bottom', fill: 'var(--focus)', fontSize: 10, fontFamily: CHART_FONT }}
            />
            <ReferenceLine
              x={medianPnl}
              stroke="var(--ink)"
              strokeWidth={2}
              strokeDasharray="2 2"
              label={{ value: 'Median', position: 'bottom', fill: 'var(--ink)', fontSize: 10, fontFamily: CHART_FONT }}
            />
            {var95 !== null && (
              <ReferenceLine
                x={-var95}
                stroke="var(--warn)"
                strokeWidth={2}
                strokeDasharray="3 3"
                label={{ value: 'VaR 95%', position: 'top', fill: 'var(--warn)', fontSize: 10, fontFamily: CHART_FONT }}
              />
            )}
            {var99 !== null && (
              <ReferenceLine
                x={-var99}
                stroke="var(--bad)"
                strokeWidth={2}
                strokeDasharray="3 3"
                label={{ value: 'VaR 99%', position: 'top', fill: 'var(--bad)', fontSize: 10, fontFamily: CHART_FONT }}
              />
            )}
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}
