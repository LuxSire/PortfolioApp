import { Bar, BarChart, CartesianGrid, ReferenceLine, ResponsiveContainer, XAxis, YAxis } from 'recharts'

// Same monospace tick font every other recharts chart in this app uses
// (see SectorPosValueChart's own copy of this comment/constant).
const CHART_FONT = 'ui-monospace, "SF Mono", "Cascadia Code", "Roboto Mono", Menlo, monospace'
const CHART_TICK_STYLE = { fill: 'var(--muted)', fontSize: 10, fontFamily: CHART_FONT }

function fmtPrice(v: number): string {
  return '$' + v.toFixed(2)
}

function fmtPct(v: number): string {
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%'
}

function fmtProb(v: number): string {
  return Math.round(v * 100) + '%'
}

export interface SimPriceRangeChartProps {
  title?: string
  currentPrice: number
  forecastPrice?: number | null
  forecastReturn?: number | null
  p5: number
  p25: number
  median: number
  p75: number
  p95: number
  probAboveCurrentPrice: number
  priceFloor?: number | null
  priceCap?: number | null
}

// One-row floating bar spanning the simulated P5-P95 range (see
// modules/simulations.py's priceAtBlendedMultiple), with vertical
// reference lines marking the P25/median/P75 percentiles plus today's
// price and the model's forecastPrice -- the actual raw draws never leave
// the backend (only percentile summary stats do), so this deliberately
// shows the real percentile points rather than reconstructing an
// approximate density curve that would misrepresent the EPS-floored-at-0
// tail this distribution can have.
export default function SimPriceRangeChart({
  title = 'Simulated Price Distribution',
  currentPrice,
  forecastPrice,
  forecastReturn,
  p5,
  p25,
  median,
  p75,
  p95,
  probAboveCurrentPrice,
  priceFloor,
  priceCap,
}: SimPriceRangeChartProps) {
  const hasForecast = forecastPrice != null && forecastReturn != null
  const hasReturn = forecastReturn != null
  const data = [{ name: 'range', range: [p5, p95] as [number, number] }]
  const domainMax = Math.max(p95, currentPrice, hasForecast ? forecastPrice : 0, priceCap ?? 0) * 1.08

  return (
    <div className="asset-card">
      <h2>{title}</h2>
      <div className="stat-row">
        {hasForecast ? (
          <>
            <div className="stat">
              <span className="n num">{fmtPrice(forecastPrice)}</span>
              <span className="l">Forecast price</span>
            </div>
            <div className="stat">
              <span className={`n num ${forecastReturn >= 0 ? 'good' : 'bad'}`}>{fmtPct(forecastReturn)}</span>
              <span className="l">Forecast return</span>
            </div>
          </>
        ) : hasReturn ? (
          <div className="stat">
            <span className={`n num ${forecastReturn >= 0 ? 'good' : 'bad'}`}>{fmtPct(forecastReturn)}</span>
            <span className="l">Median return</span>
          </div>
        ) : (
          <div className="stat">
            <span className="n num">{fmtPrice(median)}</span>
            <span className="l">Median</span>
          </div>
        )}
        <div className="stat">
          <span className="n num">{fmtProb(probAboveCurrentPrice)}</span>
          <span className="l">P(above current)</span>
        </div>
        {priceFloor != null && (
          <div className="stat">
            <span className="n num">{fmtPrice(priceFloor)}</span>
            <span className="l">Bear target (floor)</span>
          </div>
        )}
        {priceCap != null && (
          <div className="stat">
            <span className="n num">{fmtPrice(priceCap)}</span>
            <span className="l">Bull target (cap)</span>
          </div>
        )}
      </div>
      <div className="chart-wrap chart-wrap-full-bleed">
        <ResponsiveContainer width="100%" height={130}>
          <BarChart
            data={data}
            layout="vertical"
            margin={{ top: 28, right: 16, bottom: 20, left: 16 }}
          >
            <CartesianGrid stroke="var(--line)" horizontal={false} />
            <XAxis
              type="number"
              domain={[0, domainMax]}
              tick={CHART_TICK_STYLE}
              tickFormatter={(v: number) => fmtPrice(v)}
              axisLine={false}
              tickLine={false}
            />
            <YAxis type="category" dataKey="name" hide />
            <Bar dataKey="range" fill="var(--line)" barSize={22} radius={[4, 4, 4, 4]} isAnimationActive={false} />
            <ReferenceLine x={p25} stroke="var(--muted)" strokeDasharray="2 2" />
            <ReferenceLine x={p75} stroke="var(--muted)" strokeDasharray="2 2" />
            <ReferenceLine
              x={currentPrice}
              stroke="var(--ink)"
              strokeWidth={2}
              label={{ value: 'Current', position: 'insideBottom', fill: 'var(--ink)', fontSize: 10, fontFamily: CHART_FONT }}
            />
            {hasForecast && (
              <ReferenceLine
                x={forecastPrice}
                stroke={forecastReturn >= 0 ? 'var(--good)' : 'var(--bad)'}
                strokeWidth={2}
                label={{
                  value: 'Forecast',
                  position: 'top',
                  fill: forecastReturn >= 0 ? 'var(--good)' : 'var(--bad)',
                  fontSize: 10,
                  fontFamily: CHART_FONT,
                }}
              />
            )}
            {priceFloor != null && (
              <ReferenceLine
                x={priceFloor}
                stroke="var(--bad)"
                strokeDasharray="3 3"
                label={{ value: 'Bear', position: 'insideBottom', fill: 'var(--bad)', fontSize: 10, fontFamily: CHART_FONT }}
              />
            )}
            {priceCap != null && (
              <ReferenceLine
                x={priceCap}
                stroke="var(--good)"
                strokeDasharray="3 3"
                label={{ value: 'Bull', position: 'insideBottom', fill: 'var(--good)', fontSize: 10, fontFamily: CHART_FONT }}
              />
            )}
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="sim-price-range-legend">
        <span>P5 {fmtPrice(p5)}</span>
        <span>P25 {fmtPrice(p25)}</span>
        <span>Median {fmtPrice(median)}</span>
        <span>P75 {fmtPrice(p75)}</span>
        <span>P95 {fmtPrice(p95)}</span>
      </div>
    </div>
  )
}
