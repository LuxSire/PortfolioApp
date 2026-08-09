import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

// This project's first TypeScript component (everything else is .jsx) —
// the surrounding data layer (screenerFactors.js, factorTable.jsx) has no
// type definitions of its own, so this component's props are the only
// thing actually type-checked; callers still just pass a plain object
// shaped like SectorPosValuePoint.
export interface SectorPosValuePoint {
  sector: string
  label: string
  posValue: number
}

interface SectorPosValueChartProps {
  data: SectorPosValuePoint[]
}

// Same monospace tick font / muted color every other recharts chart in
// this app uses (see Asset.jsx's PriceChart/VolumeChart) — kept as its
// own copy rather than a shared import since neither file exports these
// constants today.
const CHART_FONT = 'ui-monospace, "SF Mono", "Cascadia Code", "Roboto Mono", Menlo, monospace'
const CHART_TICK_STYLE = { fill: 'var(--muted)', fontSize: 10, fontFamily: CHART_FONT }

function fmtMoney(v: number): string {
  return '$' + v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

interface ChartTooltipProps {
  active?: boolean
  payload?: Array<{ payload: SectorPosValuePoint }>
}

function ChartTooltip({ active, payload }: ChartTooltipProps) {
  if (!active || !payload || !payload.length) return null
  const point = payload[0].payload
  return (
    <div className="chart-tooltip">
      <span className={`chart-tooltip-value ${point.posValue >= 0 ? 'good' : 'bad'}`}>
        {fmtMoney(point.posValue)}
      </span>
      <span className="chart-tooltip-date">{point.label}</span>
    </div>
  )
}

// Position Value per sector (see SectorsView.jsx's own posValSum, summed
// at the sector level only, not per industry/asset) as a bar chart —
// negative bars (a sector that's net short) color red, same good/bad
// sign convention every dollar figure elsewhere in this app already
// uses, via a per-bar <Cell> rather than a single fixed Bar fill.
export default function SectorPosValueChart({ data }: SectorPosValueChartProps) {
  return (
    <div className="asset-card">
      <h2>Position Value by Sector</h2>
      {/* Bleeds through .asset-card's own horizontal padding (see
          .chart-wrap-full-bleed) so ResponsiveContainer's 100% width
          measures out to the same width as the Sectors table's
          .table-wrap (which has no horizontal padding of its own),
          instead of rendering ~48px narrower than it. */}
      <div className="chart-wrap chart-wrap-full-bleed">
        <ResponsiveContainer width="100%" height={280}>
          <BarChart data={data} margin={{ top: 12, right: 8, bottom: 4, left: 8 }}>
            <CartesianGrid stroke="var(--line)" vertical={false} />
            <XAxis
              dataKey="label"
              type="category"
              interval={0}
              angle={-30}
              textAnchor="end"
              height={70}
              tick={CHART_TICK_STYLE}
              axisLine={false}
              tickLine={false}
            />
            <YAxis hide domain={['auto', 'auto']} />
            <Tooltip content={<ChartTooltip />} cursor={{ fill: 'var(--line)', fillOpacity: 0.3 }} />
            <Bar dataKey="posValue" isAnimationActive={false} radius={[3, 3, 0, 0]}>
              {data.map((d) => (
                <Cell key={d.sector} fill={d.posValue >= 0 ? 'var(--good)' : 'var(--bad)'} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}
