import { Bar, CartesianGrid, ComposedChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

function fmtPrice(v) {
  if (typeof v !== 'number') return '—'
  return '$' + v.toFixed(2)
}

function fmtVolume(v) {
  if (typeof v !== 'number') return '—'
  if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M'
  if (v >= 1e3) return (v / 1e3).toFixed(0) + 'K'
  return String(v)
}

const CHART_FONT = 'ui-monospace, "SF Mono", "Cascadia Code", "Roboto Mono", Menlo, monospace'
const CHART_TICK_STYLE = { fill: 'var(--muted)', fontSize: 10, fontFamily: CHART_FONT }

// Recharts has no built-in candlestick mark: this is the standard
// workaround — a Bar whose dataKey resolves to [low, high] (so Recharts'
// y-scale positions y/height to exactly span that range), with a custom
// shape that reinterprets that same y/height as a price scale to place the
// open/close body and the high/low wick.
function Candle({ x, y, width, height, payload }) {
  const { open, close, high, low } = payload
  const isUp = close >= open
  const color = isUp ? 'var(--good)' : 'var(--bad)'
  const scale = height / (high - low || 1)
  const yForPrice = (price) => y + (high - price) * scale
  const bodyTop = yForPrice(Math.max(open, close))
  const bodyBottom = yForPrice(Math.min(open, close))
  const bodyHeight = Math.max(1, bodyBottom - bodyTop)
  const cx = x + width / 2
  return (
    <g>
      <line x1={cx} x2={cx} y1={y} y2={y + height} stroke={color} strokeWidth={1} />
      <rect x={x} y={bodyTop} width={Math.max(1, width)} height={bodyHeight} fill={color} />
    </g>
  )
}

function CandleTooltip({ active, payload, dateFormatter }) {
  if (!active || !payload || !payload.length) return null
  const p = payload[0].payload
  const isUp = p.close >= p.open
  return (
    <div className="chart-tooltip">
      <span className={`chart-tooltip-value ${isUp ? 'good' : 'bad'}`}>{fmtPrice(p.close)}</span>
      <span className="chart-tooltip-ohlc">
        O {fmtPrice(p.open)} · H {fmtPrice(p.high)} · L {fmtPrice(p.low)}
      </span>
      <span className="chart-tooltip-date">{dateFormatter(p.date)}</span>
    </div>
  )
}

function VolumeTooltip({ active, payload, dateFormatter }) {
  if (!active || !payload || !payload.length) return null
  const p = payload[0].payload
  return (
    <div className="chart-tooltip">
      <span className="chart-tooltip-value">{fmtVolume(p.volume)}</span>
      <span className="chart-tooltip-date">{dateFormatter(p.date)}</span>
    </div>
  )
}

// A separate small chart stacked under the candlesticks, not a second
// y-axis on the same plot (see the dataviz "one axis" rule) — same data,
// margin, and ticks/xTicks as the price chart above it so bars line up.
function VolumeChart({ data, dateFormatter, barSize, xTicks }) {
  return (
    <div className="chart-wrap chart-wrap-volume">
      <ResponsiveContainer width="100%" height={60}>
        <ComposedChart data={data} margin={{ top: 0, right: 4, bottom: 4, left: 4 }}>
          <XAxis dataKey="date" type="category" ticks={xTicks} tick={false} axisLine={false} tickLine={false} />
          <YAxis
            domain={[0, 'auto']}
            orientation="right"
            width={56}
            tick={false}
            axisLine={false}
            tickLine={false}
          />
          <Tooltip
            content={<VolumeTooltip dateFormatter={dateFormatter} />}
            cursor={{ fill: 'var(--surface-2)' }}
          />
          <Bar dataKey="volume" fill="var(--muted)" isAnimationActive={false} barSize={barSize} radius={[1, 1, 0, 0]} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}

// data: [{date, open, high, low, close, volume}, ...] from
// price_history_hourly.json or price_history_daily_3mo.json (see
// ib_server.py's fetch_candlestick_history) — IB Gateway's own
// historical bars for every ticker that process streams a price for, not
// just the screener universe PriceChart is limited to.
export default function CandlestickChart({ data, title, dateFormatter, barSize }) {
  const highs = data.map((d) => d.high)
  const lows = data.map((d) => d.low)
  const lo = Math.min(...lows)
  const hi = Math.max(...highs)
  const domainPad = (hi - lo || 1) * 0.08
  const yMin = lo - domainPad
  const yMax = hi + domainPad

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
      <h2>{title}</h2>
      <div className="chart-wrap">
        <ResponsiveContainer width="100%" height={290}>
          <ComposedChart data={data} margin={{ top: 12, right: 4, bottom: 4, left: 4 }}>
            <CartesianGrid stroke="var(--line)" vertical={false} />
            <XAxis
              dataKey="date"
              type="category"
              ticks={xTicks}
              tickFormatter={dateFormatter}
              tick={CHART_TICK_STYLE}
              axisLine={false}
              tickLine={false}
            />
            <YAxis
              domain={[yMin, yMax]}
              tickFormatter={fmtPrice}
              orientation="right"
              width={56}
              axisLine={false}
              tickLine={false}
              tick={CHART_TICK_STYLE}
            />
            <Tooltip
              content={<CandleTooltip dateFormatter={dateFormatter} />}
              cursor={{ stroke: 'var(--muted)', strokeOpacity: 0.5 }}
            />
            <Bar dataKey={(d) => [d.low, d.high]} shape={<Candle />} isAnimationActive={false} barSize={barSize} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <VolumeChart data={data} dateFormatter={dateFormatter} barSize={barSize} xTicks={xTicks} />
    </div>
  )
}
