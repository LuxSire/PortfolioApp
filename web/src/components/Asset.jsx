import { useEffect, useState } from 'react'
import {
  AreaChart,
  Area,
  Bar,
  CartesianGrid,
  ComposedChart,
  ReferenceDot,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { belowOneClass, inversePctThresholdClass, rangeClass, targetClass } from '../colorRules'
import { earningsUrgencyClass, fmtEarningsDate, useNowTick } from '../earnings'
import { IB_NEWS_ARTICLE_URL, IB_NEWS_URL } from '../ibStream'
import { SENTIMENT_LABEL, fmtNewsTime, sentimentClass } from '../news'

// Fields already surfaced in one of the curated cards above — kept out of
// the raw field dump at the bottom so nothing shows twice.
const USED_FIELDS = new Set([
  'ask',
  'averageAnalystRating',
  'bid',
  'currentPrice',
  'currentRatio',
  'debtToEquity',
  'dividendYield',
  'earningsTimestampStart',
  'earningsTimestampEnd',
  'isEarningsDateEstimate',
  'ebitdaMargins',
  'enterpriseToEbitda',
  'enterpriseToRevenue',
  'epsCurrentYear',
  'forwardEps',
  'forwardPE',
  'longBusinessSummary',
  'numberOfAnalystOpinions',
  'operatingMargins',
  'pegRatio',
  'priceEpsCurrentYear',
  'priceToBook',
  'profitMargins',
  'quickRatio',
  'regularMarketPrice',
  'returnOnAssets',
  'returnOnEquity',
  'revenueGrowth',
  'revenuePerShare',
  'shortPercentOfFloat',
  'shortRatio',
  'targetHighPrice',
  'targetLowPrice',
  'targetMeanPrice',
  'targetMedianPrice',
  'trailingEps',
  'trailingPE',
  'trailingPegRatio',
])

function fmtValue(v) {
  if (v === null || v === undefined || v === '') return '—'
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  if (typeof v === 'number') return Number.isInteger(v) ? v.toLocaleString() : v.toFixed(4)
  return String(v)
}

function fmtPrice(v) {
  if (typeof v !== 'number') return '—'
  return '$' + v.toFixed(2)
}

function fmtPct(v) {
  if (typeof v !== 'number') return '—'
  return (v * 100).toFixed(1) + '%'
}

function fmtNum(v) {
  if (typeof v !== 'number') return '—'
  return v.toFixed(2)
}

// debtToEquity/dividendYield come from yfinance already in percentage units
// (e.g. 29.9 means 29.9%), unlike ebitdaMargins/revenueGrowth/etc which are
// fractions — no *100 here.
function fmtPctRaw(v) {
  if (typeof v !== 'number') return '—'
  return v.toFixed(2) + '%'
}

function Stat({ label, value, valueClass }) {
  return (
    <div className="asset-stat">
      <span className={`n num${valueClass ? ` ${valueClass}` : ''}`}>{value}</span>
      <span className="l">{label}</span>
    </div>
  )
}

function Section({ title, children }) {
  return (
    <div className="asset-card">
      <h2>{title}</h2>
      <div className="asset-stat-grid">{children}</div>
    </div>
  )
}

// Fetches url (a {ticker: series} JSON file) and returns this ticker's
// series, or null while loading/missing/not covering this ticker. A
// missing file (e.g. ib_price_server.py hasn't finished its candlestick
// fetch yet, or was never run) is treated the same as "no data for this
// ticker" — best-effort, never blocks or errors out the rest of the page.
function useTickerSeries(url, ticker) {
  const [state, setState] = useState({ ticker: null, series: null })
  useEffect(() => {
    let cancelled = false
    fetch(url)
      .then((r) => {
        const isJson = (r.headers.get('content-type') || '').includes('json')
        if (!r.ok || !isJson) throw new Error('missing')
        return r.json()
      })
      .then((all) => {
        if (!cancelled) setState({ ticker, series: all[ticker] || null })
      })
      .catch(() => {
        if (!cancelled) setState({ ticker, series: null })
      })
    return () => {
      cancelled = true
    }
  }, [url, ticker])
  return state.ticker === ticker ? state.series : null
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
function PriceChart({ data }) {
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

// IBApp.get_ib_historical_bars formats every bar's date as
// "%Y-%m-%d %H:%M:%S" — daily bars just carry 00:00:00 (a bare
// datetime.date has no time component, see IBApp.py), so both series use
// the same parseable string shape and only the display formatting differs.
function fmtHourlyAxisDate(raw) {
  const d = new Date(raw.replace(' ', 'T'))
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric' })
}

function fmtDailyAxisDate(raw) {
  const d = new Date(raw.replace(' ', 'T'))
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

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

// data: [{date, open, high, low, close}, ...] from price_history_hourly.json
// or price_history_daily_3mo.json (see ib_price_server.py's
// fetch_candlestick_history) — IB Gateway's own historical bars for every
// ticker that process streams a price for, not just the screener universe
// PriceChart above is limited to.
function fmtVolume(v) {
  if (typeof v !== 'number') return '—'
  if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M'
  if (v >= 1e3) return (v / 1e3).toFixed(0) + 'K'
  return String(v)
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

function CandlestickChart({ data, title, dateFormatter, barSize }) {
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

const NEWS_PAGE_SIZE = 10

// One headline, lazily expandable to its full article body. Body text
// isn't fetched up front for every headline (IB's reqNewsArticle is a
// per-article request, subject to the same undocumented pacing budget as
// the headline fetch itself — see IBApp.get_news_article_async) — only
// when a user actually clicks a headline does GET /api/news/article go
// fetch and cache it, in state keyed by articleId so re-collapsing and
// re-expanding the same headline doesn't refetch.
function NewsItem({ ticker, article }) {
  const [body, setBody] = useState({ open: false, text: null, error: null, loading: false })

  function toggle() {
    if (!body.open) {
      if (body.text === null && !body.loading && !body.error) {
        setBody({ open: true, text: null, error: null, loading: true })
        fetch(`${IB_NEWS_ARTICLE_URL}?ticker=${encodeURIComponent(ticker)}&articleId=${encodeURIComponent(article.articleId)}`)
          .then((r) => {
            if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
            return r.json()
          })
          .then((data) => {
            if (data.error) throw new Error(data.error)
            setBody({ open: true, text: data.text || 'No article body available.', error: null, loading: false })
          })
          .catch((e) => setBody({ open: true, text: null, error: e.message, loading: false }))
      } else {
        setBody((b) => ({ ...b, open: true }))
      }
    } else {
      setBody((b) => ({ ...b, open: false }))
    }
  }

  return (
    <li className="news-item">
      <span className={`news-sentiment ${sentimentClass(article.sentiment)}`}>
        {SENTIMENT_LABEL[article.sentiment] || '—'}
      </span>
      <span className="news-headline news-headline-expandable" onClick={toggle}>
        {article.headline}
      </span>
      <span className="news-meta">
        {article.provider} · {fmtNewsTime(article.time)}
      </span>
      {body.open && (
        <div className="news-body">
          {body.loading && 'Loading article…'}
          {body.error && `Couldn't load article — ${body.error}`}
          {body.text}
        </div>
      )}
    </li>
  )
}

// articles: [{articleId, time, provider, headline, sentiment}, ...] from
// GET /api/news (ib_price_server.py) — best-effort live overlay, same
// contract as useTickerSeries' other callers (missing server/no news for
// this ticker just means this card doesn't render, not a load error).
function NewsPanel({ ticker, articles }) {
  const [page, setPage] = useState(0)
  const pageCount = Math.max(1, Math.ceil(articles.length / NEWS_PAGE_SIZE))
  // Clamped rather than reset-on-change: a ticker switch remounts this
  // component fresh (Asset's key is the ticker), so there's no stale-page
  // case to guard against here — just a background refresh shrinking the
  // article count out from under the current page.
  const currentPage = Math.min(page, pageCount - 1)
  const paged = articles.slice(currentPage * NEWS_PAGE_SIZE, (currentPage + 1) * NEWS_PAGE_SIZE)

  return (
    <div className="asset-card">
      <h2>News</h2>
      <ul className="news-list">
        {paged.map((a) => (
          <NewsItem key={a.articleId} ticker={ticker} article={a} />
        ))}
      </ul>
      {articles.length > NEWS_PAGE_SIZE && (
        <div className="pagination">
          <button type="button" onClick={() => setPage(currentPage - 1)} disabled={currentPage === 0}>
            Prev
          </button>
          <span className="pagination-info">
            Page {currentPage + 1} of {pageCount}
          </span>
          <button type="button" onClick={() => setPage(currentPage + 1)} disabled={currentPage >= pageCount - 1}>
            Next
          </button>
        </div>
      )}
    </div>
  )
}

export default function Asset({ ticker }) {
  // result.ticker tracks which ticker result.info/error belong to, so a
  // ticker change is treated as loading (rather than flashing the previous
  // ticker's data) without resetting state synchronously inside the effect.
  const [result, setResult] = useState({ ticker: null, info: null, error: null })
  // Live current instant — see useNowTick — so the earnings stat's
  // urgency color (earningsUrgencyClass) stays accurate as real time
  // passes, not just when raw_data.json happens to be refetched.
  const now = useNowTick()

  useEffect(() => {
    let cancelled = false
    fetch('/raw_data.json')
      .then((r) => {
        // A missing raw_data.json falls back to index.html (200, text/html)
        // under Vite's dev/preview servers rather than a real 404, so check
        // content-type instead of just r.ok.
        const isJson = (r.headers.get('content-type') || '').includes('json')
        if (!r.ok || !isJson) throw new Error('missing')
        return r.json()
      })
      .then((all) => {
        if (!cancelled) setResult({ ticker, info: all[ticker] || 'notfound', error: null })
      })
      .catch((e) => {
        if (cancelled) return
        setResult(
          e.message === 'missing'
            ? { ticker, info: 'notfound', error: null }
            : { ticker, info: null, error: e.message }
        )
      })
    return () => {
      cancelled = true
    }
  }, [ticker])

  // Three distinct, best-effort series — each may not exist yet (or not
  // cover this ticker) without blocking or erroring out the rest of the page.
  const priceHistory = useTickerSeries('/price_history.json', ticker)
  const hourlyHistory = useTickerSeries('/price_history_hourly.json', ticker)
  const dailyHistory3mo = useTickerSeries('/price_history_daily_3mo.json', ticker)
  // Live from ib_price_server.py (GET /api/news), not a static build
  // artifact — same best-effort contract as the three series above, just a
  // different (absolute, cross-origin) URL.
  const news = useTickerSeries(IB_NEWS_URL, ticker)

  const { info, error } = result.ticker === ticker ? result : { info: null, error: null }
  const loaded = !error && info && info !== 'notfound'
  const fields = loaded
    ? Object.entries(info)
        .filter(([k]) => !USED_FIELDS.has(k))
        .sort((a, b) => a[0].localeCompare(b[0]))
    : null
  const lastPrice = loaded ? info.currentPrice ?? info.regularMarketPrice : null

  return (
    <div className="app">
      <header className="masthead">
        <div className="title-block">
          <a href="#/" className="back-link">← Back to screen</a>
          <h1>{ticker}</h1>
        </div>
        {loaded && (
          <div className="stat-row">
            <div className="stat">
              <span className="n num">{fmtPrice(info.bid)}</span>
              <span className="l">Bid</span>
            </div>
            <div className="stat">
              <span className="n num">{fmtPrice(info.ask)}</span>
              <span className="l">Ask</span>
            </div>
            <div className="stat">
              <span className="n num">{fmtPrice(lastPrice)}</span>
              <span className="l">Last Price</span>
            </div>
            <div
              className={`stat earnings-stat ${earningsUrgencyClass(info.earningsTimestampStart, now)}`}
            >
              <span className="n">
                {fmtEarningsDate(info.earningsTimestampStart)}
                {info.isEarningsDateEstimate ? ' (est.)' : ''}
              </span>
              <span className="l">Next Earnings</span>
            </div>
          </div>
        )}
      </header>

      {error && <div className="asset-card">Couldn't load raw_data.json: {error}</div>}
      {!error && info === null && <div className="asset-card">Loading…</div>}
      {!error && info === 'notfound' && (
        <div className="asset-card">
          No raw data for {ticker}. Run <code>python main.py all</code> to (re)generate raw_data.json.
        </div>
      )}

      {loaded && (
        <>
          {(info.longBusinessSummary || (news && news.length > 0)) && (
            <div className="asset-summary-news-row">
              {info.longBusinessSummary && (
                <div className="asset-card asset-summary">
                  <h2>Business Summary</h2>
                  <p>{info.longBusinessSummary}</p>
                </div>
              )}

              {news && news.length > 0 && <NewsPanel ticker={ticker} articles={news} />}
            </div>
          )}

          {priceHistory && priceHistory.length > 1 && <PriceChart data={priceHistory} />}

          {hourlyHistory && hourlyHistory.length > 1 && (
            <CandlestickChart
              data={hourlyHistory}
              title="Hourly (1 Month)"
              dateFormatter={fmtHourlyAxisDate}
              barSize={3}
            />
          )}

          {dailyHistory3mo && dailyHistory3mo.length > 1 && (
            <CandlestickChart
              data={dailyHistory3mo}
              title="Daily (3 Months)"
              dateFormatter={fmtDailyAxisDate}
              barSize={6}
            />
          )}

          <Section title="Per Share Values">
            <Stat label="Last Price" value={fmtPrice(lastPrice)} />
            <Stat label="Fwd EPS" value={fmtPrice(info.forwardEps)} />
            <Stat label="Current Year EPS" value={fmtPrice(info.epsCurrentYear)} />
            <Stat label="Trailing EPS" value={fmtPrice(info.trailingEps)} />
            <Stat label="Revenue/Share" value={fmtPrice(info.revenuePerShare)} />
          </Section>

          <Section title="PE">
            <Stat label="Fwd PE" value={fmtNum(info.forwardPE)} valueClass={rangeClass(info.forwardPE, 10, 30)} />
            <Stat
              label="Trailing PE"
              value={fmtNum(info.trailingPE)}
              valueClass={rangeClass(info.trailingPE, 10, 30)}
            />
            <Stat
              label="Current Year PE"
              value={fmtNum(info.priceEpsCurrentYear)}
              valueClass={rangeClass(info.priceEpsCurrentYear, 10, 30)}
            />
          </Section>

          <Section title="Yield">
            <Stat
              label="Return on Assets"
              value={fmtPct(info.returnOnAssets)}
              valueClass={inversePctThresholdClass(info.returnOnAssets, 5, 10)}
            />
            <Stat
              label="Return on Equity"
              value={fmtPct(info.returnOnEquity)}
              valueClass={inversePctThresholdClass(info.returnOnEquity, 10, 20)}
            />
            <Stat label="Dividend Yield" value={fmtPctRaw(info.dividendYield)} />
          </Section>

          <Section title="Margins">
            <Stat label="EBITDA Margin" value={fmtPct(info.ebitdaMargins)} />
            <Stat label="Operating Margin" value={fmtPct(info.operatingMargins)} />
            <Stat label="Profit Margin" value={fmtPct(info.profitMargins)} />
          </Section>

          <Section title="Growth">
            <Stat label="PEG Ratio" value={fmtNum(info.pegRatio)} valueClass={rangeClass(info.pegRatio, 1, 1)} />
            <Stat
              label="Revenue Growth"
              value={fmtPct(info.revenueGrowth)}
              valueClass={inversePctThresholdClass(info.revenueGrowth, 0, 10)}
            />
            <Stat
              label="Trailing PEG Ratio"
              value={fmtNum(info.trailingPegRatio)}
              valueClass={rangeClass(info.trailingPegRatio, 1, 1)}
            />
          </Section>

          <Section title="Balance Sheet">
            <Stat label="Debt/Equity" value={fmtPctRaw(info.debtToEquity)} />
            <Stat
              label="Quick Ratio"
              value={fmtNum(info.quickRatio)}
              valueClass={belowOneClass(info.quickRatio)}
            />
            <Stat
              label="Current Ratio"
              value={fmtNum(info.currentRatio)}
              valueClass={belowOneClass(info.currentRatio)}
            />
            <Stat
              label="Price/Book"
              value={fmtNum(info.priceToBook)}
              valueClass={rangeClass(info.priceToBook, 1, 3)}
            />
            <Stat
              label="EV/EBITDA"
              value={fmtNum(info.enterpriseToEbitda)}
              valueClass={rangeClass(info.enterpriseToEbitda, 10, 15)}
            />
            <Stat
              label="EV/Revenue"
              value={fmtNum(info.enterpriseToRevenue)}
              valueClass={rangeClass(info.enterpriseToRevenue, 1, 10)}
            />
          </Section>

          <Section title="Short Interest">
            <Stat
              label="Short Ratio"
              value={fmtNum(info.shortRatio)}
              valueClass={rangeClass(info.shortRatio, 2, 10)}
            />
            <Stat label="Short % of Float" value={fmtPct(info.shortPercentOfFloat)} />
          </Section>

          <Section title="Price Targets">
            <Stat
              label="Target Mean"
              value={fmtPrice(info.targetMeanPrice)}
              valueClass={targetClass(info.targetMeanPrice, lastPrice)}
            />
            <Stat
              label="Target Median"
              value={fmtPrice(info.targetMedianPrice)}
              valueClass={targetClass(info.targetMedianPrice, lastPrice)}
            />
            <Stat
              label="Target Low"
              value={fmtPrice(info.targetLowPrice)}
              valueClass={targetClass(info.targetLowPrice, lastPrice)}
            />
            <Stat
              label="Target High"
              value={fmtPrice(info.targetHighPrice)}
              valueClass={targetClass(info.targetHighPrice, lastPrice)}
            />
            <Stat label="Analyst Opinions" value={fmtValue(info.numberOfAnalystOpinions)} />
            <Stat label="Avg Analyst Rating" value={info.averageAnalystRating || '—'} />
          </Section>
        </>
      )}

      {fields && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className="col-left">Field</th>
                <th className="col-left">Value</th>
              </tr>
            </thead>
            <tbody>
              {fields.map(([k, v]) => (
                <tr key={k}>
                  <td className="col-left">{k}</td>
                  <td className="col-left">{fmtValue(v)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <footer className="note">Built from raw_data.json</footer>
    </div>
  )
}
