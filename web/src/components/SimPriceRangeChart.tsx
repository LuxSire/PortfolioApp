import { Bar, BarChart, CartesianGrid, ReferenceArea, ReferenceLine, ResponsiveContainer, XAxis, YAxis } from 'recharts'

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
  // p20/median/p80 are all the CONFIDENCE-WEIGHTED fair-value figures
  // (modules/simulations.py's forecastPriceP20/P80) -- the SAME transform
  // behind forecastPrice, applied to priceAtIndustryMultiple's own
  // p20/p80 instead of its median, NOT the raw (much wider, unadjusted)
  // priceAtIndustryMultiple percentiles. p20/p80 double as this card's
  // bear/bull case (the lognormal's fat p95 tail is deliberately not
  // reported).
  p20: number
  median: number
  p80: number
  probAboveCurrentPrice: number
  analystLow?: number | null
  analystMean?: number | null
  analystHigh?: number | null
}

// One-row floating bar spanning the confidence-weighted fair-value
// P20-P80 band (see modules/simulations.py's forecastPriceP20/P80 -- NOT
// the raw simulated distribution's own, much wider, unadjusted band), with
// vertical reference lines marking today's price and the model's
// forecastPrice -- the actual raw draws never leave the backend (only
// percentile summary stats do), so this deliberately shows the real
// percentile points rather than reconstructing an approximate density
// curve. The analyst low/mean/high target band (a separate, independent
// cross-check -- see modules/simulations.py's analystTargets) is shaded
// behind everything else.
export default function SimPriceRangeChart({
  title = 'Simulated Price Distribution',
  currentPrice,
  forecastPrice,
  forecastReturn,
  p20,
  median,
  p80,
  probAboveCurrentPrice,
  analystLow,
  analystMean,
  analystHigh,
}: SimPriceRangeChartProps) {
  const hasForecast = forecastPrice != null && forecastReturn != null
  const hasReturn = forecastReturn != null
  const data = [{ name: 'range', range: [p20, p80] as [number, number] }]
  const domainMax =
    Math.max(p80, currentPrice, hasForecast ? forecastPrice : 0, analystHigh ?? 0) * 1.08

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
        <div className="stat">
          <span className="n num">{fmtPrice(p20)}</span>
          <span className="l">Bear target (P20)</span>
        </div>
        <div className="stat">
          <span className="n num">{fmtPrice(p80)}</span>
          <span className="l">Bull target (P80)</span>
        </div>
      </div>
      <div className="chart-wrap chart-wrap-full-bleed">
        <ResponsiveContainer width="100%" height={130}>
          <BarChart
            data={data}
            layout="vertical"
            margin={{ top: 28, right: 16, bottom: 20, left: 16 }}
          >
            <CartesianGrid stroke="var(--line)" horizontal={false} />
            {analystLow != null && analystHigh != null && (
              <ReferenceArea x1={analystLow} x2={analystHigh} fill="var(--focus)" fillOpacity={0.12} stroke="none" />
            )}
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
            <ReferenceLine
              x={p20}
              stroke="var(--bad)"
              strokeDasharray="3 3"
              label={{ value: 'P20', position: 'insideBottom', fill: 'var(--bad)', fontSize: 10, fontFamily: CHART_FONT }}
            />
            <ReferenceLine x={median} stroke="var(--muted)" strokeDasharray="2 2" />
            <ReferenceLine
              x={p80}
              stroke="var(--good)"
              strokeDasharray="3 3"
              label={{ value: 'P80', position: 'insideBottom', fill: 'var(--good)', fontSize: 10, fontFamily: CHART_FONT }}
            />
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
            {analystMean != null && (
              <ReferenceLine
                x={analystMean}
                stroke="var(--focus)"
                strokeWidth={1.5}
                strokeDasharray="4 2"
                label={{ value: 'Analyst', position: 'insideTop', fill: 'var(--focus)', fontSize: 10, fontFamily: CHART_FONT }}
              />
            )}
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="sim-price-range-legend">
        <span>P20 {fmtPrice(p20)}</span>
        <span>Median {fmtPrice(median)}</span>
        <span>P80 {fmtPrice(p80)}</span>
        {analystLow != null && analystMean != null && analystHigh != null && (
          <span>
            Analyst {fmtPrice(analystLow)} / {fmtPrice(analystMean)} / {fmtPrice(analystHigh)}
          </span>
        )}
      </div>
    </div>
  )
}
