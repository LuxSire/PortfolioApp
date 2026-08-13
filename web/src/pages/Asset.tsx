import { useEffect, useState, type ReactNode } from 'react'
import { belowOneClass, inversePctThresholdClass, rangeClass, targetClass } from '../colorRules'
import { earningsUrgencyClass, fmtEarningsDate, useNowTick } from '../earnings'
import { IB_NEWS_ARTICLE_URL, IB_NEWS_URL } from '../ibStream'
import type { AssetInfo, CandlePoint, Holder, NewsArticle, PricePoint } from '../interfaces/IAsset'
import { SENTIMENT_LABEL, fmtNewsTime, sentimentClass } from '../news'
import CandlestickChart from '../components/CandlestickChart'
import PriceChart from '../components/PriceChart'

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
  'sharesOutstanding',
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

function fmtValue(v: unknown): string {
  if (v === null || v === undefined || v === '') return '—'
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  if (typeof v === 'number') return Number.isInteger(v) ? v.toLocaleString() : v.toFixed(4)
  return String(v)
}

function fmtPrice(v: unknown): string {
  if (typeof v !== 'number') return '—'
  return '$' + v.toFixed(2)
}

function fmtPct(v: unknown): string {
  if (typeof v !== 'number') return '—'
  return (v * 100).toFixed(1) + '%'
}

function fmtNum(v: unknown): string {
  if (typeof v !== 'number') return '—'
  return v.toFixed(2)
}

// debtToEquity/dividendYield come from yfinance already in percentage units
// (e.g. 29.9 means 29.9%), unlike ebitdaMargins/revenueGrowth/etc which are
// fractions — no *100 here.
function fmtPctRaw(v: unknown): string {
  if (typeof v !== 'number') return '—'
  return v.toFixed(2) + '%'
}

function fmtMoney(v: number): string {
  return '$' + v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

function fmtShares(v: number): string {
  return v.toLocaleString()
}

// 13F holdings are as of the prior quarter's report date, not today's
// actual share count -- a real, if slightly lagged, ownership estimate,
// not a live figure. sharesOutstanding comes from the same raw_data.json
// (yfinance) this whole page already loads, not a second fetch.
function fmtPctOwned(shares: number, sharesOutstanding: unknown): string {
  if (typeof sharesOutstanding !== 'number' || !sharesOutstanding) return '—'
  return ((shares / sharesOutstanding) * 100).toFixed(2) + '%'
}

function Stat({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="asset-stat">
      <span className={`n num${valueClass ? ` ${valueClass}` : ''}`}>{value}</span>
      <span className="l">{label}</span>
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="asset-card">
      <h2>{title}</h2>
      <div className="asset-stat-grid">{children}</div>
    </div>
  )
}

// Fetches url (a {ticker: T} JSON file) and returns this ticker's entry,
// or null while loading/missing/not covering this ticker. A missing file
// (e.g. ib_price_server.py hasn't finished its candlestick fetch yet, or
// the 13F holders fetch hasn't been run) is treated the same as "no data
// for this ticker" — best-effort, never blocks or errors out the rest of
// the page.
function useTickerSeries<T>(url: string, ticker: string): T | null {
  const [state, setState] = useState<{ ticker: string | null; series: T | null }>({ ticker: null, series: null })
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

// holders: data/sec/13f/institutional_holders.json's per-ticker array (see
// sec_edgar.py's fetch_13f_holdings) -- top MAX_HOLDERS_PER_TICKER
// institutions by position value, current 13F quarter only, sorted
// descending by valueUsd already.
function HoldersPanel({ holders, sharesOutstanding }: { holders: Holder[]; sharesOutstanding: unknown }) {
  return (
    <div className="asset-card">
      <h2>Institutional Holders</h2>
      <table>
        <thead>
          <tr>
            <th className="col-left">Institution</th>
            <th>Value</th>
            <th>Shares</th>
            <th>% Owned</th>
          </tr>
        </thead>
        <tbody>
          {holders.map((h) => (
            <tr key={h.name}>
              <td className="col-left col-name">{h.name}</td>
              <td className="num">{fmtMoney(h.valueUsd)}</td>
              <td className="num">{fmtShares(h.shares)}</td>
              <td className="num">{fmtPctOwned(h.shares, sharesOutstanding)}</td>
            </tr>
          ))}
        </tbody>
      </table>
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
function NewsItem({ ticker, article }: { ticker: string; article: NewsArticle }) {
  const [body, setBody] = useState<{ open: boolean; text: string | null; error: string | null; loading: boolean }>({
    open: false,
    text: null,
    error: null,
    loading: false,
  })

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
        {(article.sentiment !== null && (SENTIMENT_LABEL as Record<number, string>)[article.sentiment]) || '—'}
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
function NewsPanel({ ticker, articles }: { ticker: string; articles: NewsArticle[] }) {
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

// fmtHourlyAxisDate/fmtDailyAxisDate: IBApp.get_ib_historical_bars formats
// every bar's date as "%Y-%m-%d %H:%M:%S" — daily bars just carry
// 00:00:00 (a bare datetime.date has no time component, see IBApp.py), so
// both series use the same parseable string shape and only the display
// formatting differs.
function fmtHourlyAxisDate(raw: string): string {
  const d = new Date(raw.replace(' ', 'T'))
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric' })
}

function fmtDailyAxisDate(raw: string): string {
  const d = new Date(raw.replace(' ', 'T'))
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export default function Asset({ ticker }: { ticker: string }) {
  // result.ticker tracks which ticker result.info/error belong to, so a
  // ticker change is treated as loading (rather than flashing the previous
  // ticker's data) without resetting state synchronously inside the effect.
  const [result, setResult] = useState<{ ticker: string | null; info: AssetInfo | 'notfound' | null; error: string | null }>({
    ticker: null,
    info: null,
    error: null,
  })
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

  // Four distinct, best-effort series — each may not exist yet (or not
  // cover this ticker) without blocking or erroring out the rest of the page.
  const priceHistory = useTickerSeries<PricePoint[]>('/price_history.json', ticker)
  const hourlyHistory = useTickerSeries<CandlePoint[]>('/price_history_hourly.json', ticker)
  const dailyHistory3mo = useTickerSeries<CandlePoint[]>('/price_history_daily_3mo.json', ticker)
  const holders = useTickerSeries<Holder[]>('/sec/13f/institutional_holders.json', ticker)
  // Live from ib_price_server.py (GET /api/news), not a static build
  // artifact — same best-effort contract as the series above, just a
  // different (absolute, cross-origin) URL.
  const news = useTickerSeries<NewsArticle[]>(IB_NEWS_URL, ticker)

  const { info, error } = result.ticker === ticker ? result : { info: null, error: null }
  const loaded = !error && info && info !== 'notfound'
  const fields = loaded
    ? Object.entries(info)
        .filter(([k]) => !USED_FIELDS.has(k))
        .sort((a, b) => a[0].localeCompare(b[0]))
    : null
  const lastPrice = loaded ? (info.currentPrice ?? info.regularMarketPrice) : null

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
              className={`stat earnings-stat ${earningsUrgencyClass(info.earningsTimestampStart as number, now)}`}
            >
              <span className="n">
                {fmtEarningsDate(info.earningsTimestampStart as number)}
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
          {(Boolean(info.longBusinessSummary) || (priceHistory && priceHistory.length > 1)) && (
            <div className="asset-two-col-row">
              {Boolean(info.longBusinessSummary) && (
                <div className="asset-card asset-summary">
                  <h2>Business Summary</h2>
                  <p>{info.longBusinessSummary as string}</p>
                </div>
              )}

              {priceHistory && priceHistory.length > 1 && <PriceChart data={priceHistory} />}
            </div>
          )}

          {((holders && holders.length > 0) || (news && news.length > 0)) && (
            <div className="asset-two-col-row">
              {holders && holders.length > 0 && (
                <HoldersPanel holders={holders} sharesOutstanding={info.sharesOutstanding} />
              )}

              {news && news.length > 0 && <NewsPanel ticker={ticker} articles={news} />}
            </div>
          )}

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
            <Stat label="Fwd PE" value={fmtNum(info.forwardPE)} valueClass={rangeClass(info.forwardPE as number, 10, 30)} />
            <Stat
              label="Trailing PE"
              value={fmtNum(info.trailingPE)}
              valueClass={rangeClass(info.trailingPE as number, 10, 30)}
            />
            <Stat
              label="Current Year PE"
              value={fmtNum(info.priceEpsCurrentYear)}
              valueClass={rangeClass(info.priceEpsCurrentYear as number, 10, 30)}
            />
          </Section>

          <Section title="Yield">
            <Stat
              label="Return on Assets"
              value={fmtPct(info.returnOnAssets)}
              valueClass={inversePctThresholdClass(info.returnOnAssets as number, 5, 10)}
            />
            <Stat
              label="Return on Equity"
              value={fmtPct(info.returnOnEquity)}
              valueClass={inversePctThresholdClass(info.returnOnEquity as number, 10, 20)}
            />
            <Stat label="Dividend Yield" value={fmtPctRaw(info.dividendYield)} />
          </Section>

          <Section title="Margins">
            <Stat label="EBITDA Margin" value={fmtPct(info.ebitdaMargins)} />
            <Stat label="Operating Margin" value={fmtPct(info.operatingMargins)} />
            <Stat label="Profit Margin" value={fmtPct(info.profitMargins)} />
          </Section>

          <Section title="Growth">
            <Stat label="PEG Ratio" value={fmtNum(info.pegRatio)} valueClass={rangeClass(info.pegRatio as number, 1, 1)} />
            <Stat
              label="Revenue Growth"
              value={fmtPct(info.revenueGrowth)}
              valueClass={inversePctThresholdClass(info.revenueGrowth as number, 0, 10)}
            />
            <Stat
              label="Trailing PEG Ratio"
              value={fmtNum(info.trailingPegRatio)}
              valueClass={rangeClass(info.trailingPegRatio as number, 1, 1)}
            />
          </Section>

          <Section title="Balance Sheet">
            <Stat label="Debt/Equity" value={fmtPctRaw(info.debtToEquity)} />
            <Stat
              label="Quick Ratio"
              value={fmtNum(info.quickRatio)}
              valueClass={belowOneClass(info.quickRatio as number)}
            />
            <Stat
              label="Current Ratio"
              value={fmtNum(info.currentRatio)}
              valueClass={belowOneClass(info.currentRatio as number)}
            />
            <Stat
              label="Price/Book"
              value={fmtNum(info.priceToBook)}
              valueClass={rangeClass(info.priceToBook as number, 1, 3)}
            />
            <Stat
              label="EV/EBITDA"
              value={fmtNum(info.enterpriseToEbitda)}
              valueClass={rangeClass(info.enterpriseToEbitda as number, 10, 15)}
            />
            <Stat
              label="EV/Revenue"
              value={fmtNum(info.enterpriseToRevenue)}
              valueClass={rangeClass(info.enterpriseToRevenue as number, 1, 10)}
            />
          </Section>

          <Section title="Short Interest">
            <Stat
              label="Short Ratio"
              value={fmtNum(info.shortRatio)}
              valueClass={rangeClass(info.shortRatio as number, 2, 10)}
            />
            <Stat label="Short % of Float" value={fmtPct(info.shortPercentOfFloat)} />
          </Section>

          <Section title="Price Targets">
            <Stat
              label="Target Mean"
              value={fmtPrice(info.targetMeanPrice)}
              valueClass={targetClass(info.targetMeanPrice as number, lastPrice as number)}
            />
            <Stat
              label="Target Median"
              value={fmtPrice(info.targetMedianPrice)}
              valueClass={targetClass(info.targetMedianPrice as number, lastPrice as number)}
            />
            <Stat
              label="Target Low"
              value={fmtPrice(info.targetLowPrice)}
              valueClass={targetClass(info.targetLowPrice as number, lastPrice as number)}
            />
            <Stat
              label="Target High"
              value={fmtPrice(info.targetHighPrice)}
              valueClass={targetClass(info.targetHighPrice as number, lastPrice as number)}
            />
            <Stat label="Analyst Opinions" value={fmtValue(info.numberOfAnalystOpinions)} />
            <Stat label="Avg Analyst Rating" value={(info.averageAnalystRating as string) || '—'} />
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
