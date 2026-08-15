import { useEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { Search, ChevronDown, ExternalLink, Info, Newspaper, X } from 'lucide-react'
import { inverseRangeClass, inversePctThresholdClass, rangeClass } from '../colorRules'
import { parseCSV } from '../csv'
import { earningsUrgencyClass, fmtEarningsDate, useNowTick } from '../earnings'
import { getSectorIcon } from '../sectorIcons'
import { getSectorGroup } from '../sectorGroups'
import { IB_STREAM_URL } from '../ibStream'
import {
  COLUMNS,
  avgInsiderScore,
  avgNewsSentiment,
  fmtDebtToEquity,
  fmtIndex100,
  fmtNum,
  fmtPct,
  fmtPrice,
  fmtScore,
  rankTo100,
  recClass,
  recLabel,
  ratingClass,
  toNum,
} from '../screenerFactors'
import type {
  HistoryByTicker,
  LivePricesByTicker,
  PositionsByTicker,
  RawScreenerRow,
  ScreenerRow,
  StickyHeaderState,
} from '../interfaces/IScreenerView'

const PAGE_SIZE = 100

// The last close strictly before today, comparing BOTH bar series --
// price_history_daily_3mo.json (IB Gateway's own history, fetched once
// at ib_price_server.py STARTUP only, so it silently goes stale the
// longer the server runs without a restart) and price_history.json
// (yfinance, refreshed daily via main.py) — never today's own entry,
// which both sources can carry as a still-forming bar (close = latest
// price so far, not a settled close) when fetched intraday. Comparing a
// live price against that same-day bar instead of a real prior close
// silently understates or misreports the day's actual move. Same
// helper as PositionsView.jsx's — see there for the fuller history of
// why this matters (it's what made ARKK's P&L wrong before) and for
// the "pick whichever source is actually fresher" bug fix (confirmed
// live on TSLA: price_history_daily_3mo.json sitting 2 trading days
// stale still returned a valid, just outdated, close, so a plain ??
// fallback chain never reached the fresher price_history.json one).
function previousClose(
  dailyHistory3mo: { date: string; close: number }[] | undefined,
  monthlyHistory: { date: string; close: number }[] | undefined
): number | null {
  const lastBarBeforeToday = (series: { date: string; close: number }[] | undefined) => {
    if (!series || series.length === 0) return null
    const today = new Date().toISOString().slice(0, 10)
    for (let i = series.length - 1; i >= 0; i--) {
      const date = series[i].date.slice(0, 10)
      if (date < today) return { date, close: series[i].close }
    }
    return null
  }
  const fromDaily = lastBarBeforeToday(dailyHistory3mo)
  const fromMonthly = lastBarBeforeToday(monthlyHistory)
  if (fromDaily && fromMonthly) return fromDaily.date >= fromMonthly.date ? fromDaily.close : fromMonthly.close
  return (fromDaily ?? fromMonthly)?.close ?? null
}

// Share counts are whole numbers for the vast majority of IBKR positions;
// only show decimals for the rare fractional-share holding.
function fmtShares(v: number | null): string {
  if (v === null) return '—'
  return v.toLocaleString(undefined, { maximumFractionDigits: Number.isInteger(v) ? 0 : 2 })
}

function fmtMoney(v: number | null): string {
  if (v === null) return '—'
  return '$' + v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

// lastDownload is stored as a full ISO timestamp (needed to check "within
// the last 12h" server-side); the table only needs the date portion --
// except for today, where the date alone doesn't distinguish "updated 5
// minutes ago" from "updated this morning", so show the time instead.
function fmtDate(v: string | null): string {
  if (!v) return '—'
  const d = new Date(v)
  const now = new Date()
  const isToday =
    d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate()
  if (isToday) return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  return v.slice(0, 10)
}

// The # column shows each ticker's rank as a percentile of the ranked
// universe (rank / rankedTotal * 100) rather than a raw position — 0% is
// the single best-ranked ticker, 100% the worst-ranked one, same "lower
// is better" direction as the raw rank it replaces (and the composite
// score itself). null for an unranked row (see rankedTotal's own
// comment) rather than a nonsensical >100% figure.
function fmtPercentileRank(rank: number | null, total: number): string {
  if (rank === null || rank === undefined || !total) return '—'
  return ((rank / total) * 100).toFixed(2) + '%'
}

function percentileRankTooltip(rank: number | null, total: number): string | undefined {
  if (rank === null || rank === undefined || !total) return undefined
  return `#${rank} of ${total} ranked`
}

// Name cell's title is the tooltip that already shows the full company
// name when the column truncates it; fold the earnings date in as
// human-readable text rather than leaving readers to decode the color tier
// (or the raw epoch seconds backing it) on their own.
function nameTooltip(r: ScreenerRow): string {
  if (r.ern === null) return r.n
  return `${r.n} — earnings ${fmtEarningsDate(r.ern)}`
}

function targetTooltip(r: ScreenerRow): string | undefined {
  if (r.tgtLow === null && r.tgtHigh === null && r.numAnalysts === null) return undefined
  const parts: string[] = []
  if (r.tgtLow !== null || r.tgtHigh !== null) {
    parts.push(`Range ${fmtPrice(r.tgtLow)} – ${fmtPrice(r.tgtHigh)}`)
  }
  if (r.numAnalysts !== null) {
    parts.push(`${r.numAnalysts} analyst${r.numAnalysts === 1 ? '' : 's'}`)
  }
  return parts.join(' · ')
}

function sentimentTooltip(r: ScreenerRow): string | undefined {
  if (r.sentBullish === null && r.sentBearish === null) return undefined
  const parts = [`${r.sentBullish ?? 0} bullish · ${r.sentBearish ?? 0} bearish`]
  if (r.sentTotal !== null) parts.push(`${r.sentTotal} recent messages`)
  return parts.join(' · ')
}

function newsSentimentTooltip(r: ScreenerRow): string | undefined {
  if (!r.newsSentCount) return undefined
  return `${r.newsSentCount} headline${r.newsSentCount === 1 ? '' : 's'} scored (FinBERT, last month)`
}

// The "90 days" here matches sec_edgar.py's FORM4_LOOKBACK_DAYS -- kept
// in sync by hand, since there's no shared constant across the Python/JS
// boundary.
function insidersTooltip(r: ScreenerRow): string | undefined {
  if (!r.insiderBuys && !r.insiderSells) return undefined
  return `${r.insiderBuys} open-market buy${r.insiderBuys === 1 ? '' : 's'} · ${r.insiderSells} sale${r.insiderSells === 1 ? '' : 's'} (SEC Form 4, last 90 days)`
}

// r.instChange itself gets overwritten by the rank-to-[-100,100] pass
// (see the useEffect below) same as mom/mr/sent/newsSent/insiders, so the
// actual raw percent is kept separately in instChangeRaw just for this.
function instChangeTooltip(r: ScreenerRow): string | undefined {
  if (r.instChangeRaw === null) return undefined
  return `${(r.instChangeRaw * 100).toFixed(1)}% change in institutional shares held vs. last quarter (SEC 13F)`
}

function zacksUrl(ticker: string): string {
  const t = encodeURIComponent(ticker)
  return `https://www.zacks.com/stock/quote/${t}`
}

// A real popup window (fixed size, no browser chrome), not just a new
// background tab — the point is a quick side-by-side look at Zacks'
// quote page without losing your place in the screener.
function openZacksPopup(e: React.MouseEvent, ticker: string) {
  e.preventDefault()
  window.open(zacksUrl(ticker), `zacks_${ticker}`, 'width=1000,height=800,noopener,noreferrer')
}

// #/news/TICKER (see main.jsx's Router) renders NewsPopup.jsx standalone,
// no tab bar — a relative hash href, resolved against this page's own
// origin/path by the browser, same pattern as the ticker-link column
// linking to #/asset/TICKER just without a page navigation.
function newsPopupHref(ticker: string): string {
  return `#/news/${encodeURIComponent(ticker)}`
}

// Same real-popup-window treatment as openZacksPopup, just narrower and
// taller — a table of headlines reads better tall than wide.
function openNewsPopup(e: React.MouseEvent, ticker: string) {
  e.preventDefault()
  window.open(newsPopupHref(ticker), `news_${ticker}`, 'width=640,height=780,noopener,noreferrer')
}

function Subrank({ rank }: { rank: number | null | undefined }) {
  if (rank === null || rank === undefined) return null
  return <span className="subrank">{rank}</span>
}

// 1-indexed rank per ticker (best = 1); rows failing filterFn or missing a
// value are left out of the map entirely (no subrank shown for them).
function rankAscending(
  rows: ScreenerRow[] | RawScreenerRow[],
  key: keyof RawScreenerRow,
  filterFn?: (v: number) => boolean
): Map<string, number> {
  const valid = rows.filter((r) => r[key] !== null && (!filterFn || filterFn(r[key] as number)))
  valid.sort((a, b) => (a[key] as number) - (b[key] as number))
  const map = new Map<string, number>()
  valid.forEach((r, i) => map.set(r.t, i + 1))
  return map
}

function rankDescending(rows: ScreenerRow[] | RawScreenerRow[], key: keyof RawScreenerRow): Map<string, number> {
  const valid = rows.filter((r) => r[key] !== null)
  valid.sort((a, b) => (b[key] as number) - (a[key] as number))
  const map = new Map<string, number>()
  valid.forEach((r, i) => map.set(r.t, i + 1))
  return map
}

function useOutsideClick(ref: RefObject<HTMLElement | null>, onOutside: () => void) {
  useEffect(() => {
    function handler(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) onOutside()
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [ref, onOutside])
}

function FilterDropdown({
  noun,
  plural,
  items,
  selected,
  onToggle,
  onClear,
  getIcon,
}: {
  noun: string
  plural?: string
  items: [string, number][]
  selected: Set<string>
  onToggle: (name: string) => void
  onClear: () => void
  getIcon?: (name: string) => React.ComponentType<{ size?: number }>
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const wrapRef = useRef<HTMLDivElement>(null)
  useOutsideClick(wrapRef, () => setOpen(false))
  const pluralNoun = plural || `${noun}s`

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return items.filter(([name]) => !q || name.toLowerCase().includes(q))
  }, [items, query])

  return (
    <div className="sector-control" ref={wrapRef}>
      <button
        type="button"
        className="sector-toggle"
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span>{selected.size ? `${selected.size} ${selected.size > 1 ? pluralNoun : noun}` : `All ${pluralNoun}`}</span>
        <ChevronDown size={12} />
      </button>
      {open && (
        <div className="sector-panel open">
          <div className="sp-search">
            <input
              type="text"
              placeholder={`Filter ${pluralNoun}…`}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              autoFocus
            />
          </div>
          <div className="sp-list">
            {filtered.map(([name, count]) => {
              const Icon = getIcon ? getIcon(name) : null
              return (
                <label className="sp-item" key={name}>
                  <input
                    type="checkbox"
                    checked={selected.has(name)}
                    onChange={() => onToggle(name)}
                  />
                  {Icon && <Icon size={14} />}
                  <span>{name}</span>
                  <span className="cnt">{count}</span>
                </label>
              )
            })}
          </div>
          <div className="sp-actions">
            <button type="button" onClick={onClear}>Clear all</button>
            <button type="button" onClick={() => setOpen(false)}>Done</button>
          </div>
        </div>
      )}
    </div>
  )
}

// Mirrors main.py's score_rows exactly — weights and directions here must
// be kept in sync by hand whenever that function's composite score
// changes, since there's no live endpoint serving the formula itself
// (sorted_screen.csv only carries the resulting number, not how it was
// built).
const SCORE_FACTORS = [
  { label: 'Forward PE', weight: 5, note: 'low is better' },
  { label: 'Price / FCF', weight: 5, note: 'low is better; negative or missing FCF ranked worst' },
  { label: 'EV / EBITDA', weight: 5, note: 'low is better; negative EBITDA ranked worst' },
  { label: 'Forward PE vs. sector average', weight: 10, note: 'low relative to sector is better' },
  {
    label: 'Momentum',
    weight: 5,
    note: 'daily-timeframe regression-slope trend, divided by its own volatility; high is better',
  },
  {
    label: 'Short-term mean reversion',
    weight: 5,
    note: 'same regression-slope trend formula as Momentum, just on the hourly timeframe; low is better (a stock already trending up hard on the hour is one being chased, not caught early)',
  },
  {
    label: 'EPS trend',
    weight: 5,
    note: 'avg of high current-fiscal-year + high next-fiscal-year 30-day consensus EPS estimate revision ranks; high (analysts raising estimates) is better',
  },
  {
    label: 'Analyst conviction',
    weight: 7.5,
    note: 'avg of high target upside + low (strong-buy) recommendation mean + low target-price dispersion ranks',
  },
  { label: 'Forward PE vs. trailing PE', weight: 5, note: 'more negative is better; unprofitable ranked worst' },
  { label: 'PEG ratio', weight: 5, note: 'low is better; negative ranked worst, not best' },
  {
    label: 'Trailing P/S',
    weight: 2.5,
    note: 'low is better; a separate valuation lens from P/E/P-FCF/EV-EBITDA, stays meaningful when those break down',
  },
  { label: 'Revenue growth', weight: 7.5, note: 'high is better; negative ranked worst, not just low' },
  { label: 'Debt / Equity vs. sector average', weight: 5, note: 'low relative to sector is better' },
  { label: 'Liquidity', weight: 2.5, note: 'avg of high quick ratio + high current ratio ranks' },
  { label: 'Return on equity', weight: 5, note: 'high is better; negative ranked worst, not just low' },
  {
    label: 'Short interest',
    weight: 5,
    note: 'avg of high short ratio + high short % of float ranks — contrarian: more shorted scores better',
  },
  {
    label: 'News + social + institutional sentiment',
    weight: 5,
    note: 'avg of StockTwits social sentiment + FinBERT-scored news sentiment (neutral news excluded) + institutional QoQ share-change (SEC 13F, clipped to ±50%); missing ranked worst',
  },
  {
    label: 'Insiders',
    weight: 5,
    note: 'open-market buys minus sells, as a share of both (SEC Form 4); missing ranked worst',
  },
  {
    label: 'Margins',
    weight: 5,
    note: 'avg of high profit margin + high operating margin ranks; negative ranked worst, not just low',
  },
]

// Shared between the real <thead> and its fixed-position clone below (see
// useStickyHeaderClone) so both render identically — same labels, same
// sort-arrow, same click/keyboard handlers. widths, if given, is applied
// as an explicit inline width per <th>, in COLUMNS order — used only by
// the clone, to hold each column's rendered width steady once measured
// (the real header keeps its normal table-layout: auto sizing).
function HeaderCells({
  sortKey,
  sortDir,
  onSort,
  widths,
}: {
  sortKey: string
  sortDir: number
  onSort: (key: string) => void
  widths?: number[]
}) {
  return COLUMNS.map((col: { key: string; label: string; className?: string; sortable?: boolean }, i: number) => {
    const sortable = col.sortable !== false
    const active = sortKey === col.key
    return (
      <th
        key={col.key}
        className={col.className || ''}
        tabIndex={sortable ? 0 : -1}
        onClick={sortable ? () => onSort(col.key) : undefined}
        onKeyDown={
          sortable
            ? (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  onSort(col.key)
                }
              }
            : undefined
        }
        style={widths ? { width: widths[i], minWidth: widths[i], maxWidth: widths[i] } : undefined}
      >
        {col.label}
        {sortable && active && <span className="arrow">{sortDir === 1 ? '▲' : '▼'}</span>}
      </th>
    )
  })
}

// CSS position: sticky on thead th should already keep the header pinned
// on its own (see styles.scss) — this is a belt-and-suspenders JS fallback
// that doesn't depend on that mechanism at all, for cases where it isn't
// taking effect. Measures the real table's header via tableRef and, once
// its natural position has scrolled above the viewport top, reports
// enough (left offset, each column's current rendered width) to render a
// position: fixed clone of the header row that visually replaces it.
// Window-scroll-driven (the table itself has no scroll container of its
// own — see .table-wrap's own comment on why), not IntersectionObserver,
// since this also needs the live column widths on every check, not just
// a boolean.
function useStickyHeaderClone(tableRef: RefObject<HTMLTableElement | null>): StickyHeaderState {
  const [state, setState] = useState<StickyHeaderState>({ stuck: false, left: 0, width: 0, widths: [] })

  useEffect(() => {
    let ticking = false
    function measure() {
      ticking = false
      const table = tableRef.current
      const thead = table?.querySelector('thead')
      if (!thead || !table) return
      const headRect = thead.getBoundingClientRect()
      const tableRect = table.getBoundingClientRect()
      const widths = [...thead.querySelectorAll('th')].map((th) => th.getBoundingClientRect().width)
      setState({ stuck: headRect.top <= 0, left: tableRect.left, width: tableRect.width, widths })
    }
    function onScrollOrResize() {
      if (ticking) return
      ticking = true
      requestAnimationFrame(measure)
    }
    measure()
    window.addEventListener('scroll', onScrollOrResize, { passive: true })
    window.addEventListener('resize', onScrollOrResize)
    return () => {
      window.removeEventListener('scroll', onScrollOrResize)
      window.removeEventListener('resize', onScrollOrResize)
    }
  }, [tableRef])

  return state
}

function ScoreFormula() {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  useOutsideClick(wrapRef, () => setOpen(false))

  return (
    <div className="score-formula" ref={wrapRef}>
      <button
        type="button"
        className="score-formula-toggle"
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <Info size={14} />
        <span>Score formula</span>
      </button>
      {open && (
        <div className="score-formula-panel">
          <div className="score-formula-header">Composite score — lower is better</div>
          <ul className="score-formula-list">
            {SCORE_FACTORS.map((f) => (
              <li key={f.label}>
                <span className="score-formula-weight">{f.weight}%</span>
                <span className="score-formula-body">
                  <span className="score-formula-label">{f.label}</span>
                  <span className="score-formula-note">{f.note}</span>
                </span>
              </li>
            ))}
          </ul>
          <div className="score-formula-footer">
            The <strong>Rating</strong> column buckets this score's percentile into a
            forced Strong Buy/Buy/Hold/Sell/Strong Sell distribution (top/bottom 5% =
            Strong Buy/Strong Sell, next 15% each = Buy/Sell, middle 60% = Hold) — same
            shape as Zacks Rank, independent of the Rec column's analyst consensus.
          </div>
        </div>
      )}
    </div>
  )
}

export default function ScreenerView() {
  const [rawRows, setRawRows] = useState<RawScreenerRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [selectedIndustries, setSelectedIndustries] = useState<Set<string>>(new Set())
  const [selectedGroups, setSelectedGroups] = useState<Set<string>>(new Set())
  const [selectedRatings, setSelectedRatings] = useState<Set<string>>(new Set())
  const [sortKey, setSortKey] = useState('sc')
  const [sortDir, setSortDir] = useState(1)
  const [livePrices, setLivePrices] = useState<LivePricesByTicker>({})
  const [positions, setPositions] = useState<PositionsByTicker>({})
  const [nonZeroOnly, setNonZeroOnly] = useState(false)
  const [page, setPage] = useState(0)
  const tableRef = useRef<HTMLTableElement>(null)
  const stickyHeader = useStickyHeaderClone(tableRef)
  // Live current instant — see useNowTick — so the Name cell's earnings-
  // urgency color (earningsUrgencyClass) stays accurate as real time
  // passes, not just when sorted_screen.csv happens to be refetched.
  const now = useNowTick()
  // Daily-close series for the Price column's daily-% badge — see
  // previousClose. price_history_daily_3mo.json (IB Gateway's own daily
  // bars, written by ib_price_server.py's fetch_candlestick_history) is
  // the primary source; price_history.json (yfinance, 1mo, written by
  // main.py) is the fallback for a ticker IB Gateway hasn't fetched
  // history for. Same two files, same fallback order, as
  // PositionsView.jsx.
  const [dailyHistory3mo, setDailyHistory3mo] = useState<HistoryByTicker>({})
  const [monthlyHistory, setMonthlyHistory] = useState<HistoryByTicker>({})

  // ib_price_server.py — a separate always-running local process (not part
  // of this fetch-once pipeline) that pushes IB Gateway last-price ticks
  // (every screener ticker: a live reqMktData subscription for the top
  // MAX_STREAMED_SYMBOLS, a periodic snapshot for everything else — see
  // ib_price_server.py's snapshot_loop) and account positions (any
  // ticker) over Server-Sent Events as they change — no polling, no
  // re-request on our end. Silently a no-op if the server isn't running;
  // EventSource retries the connection on its own, and this is a
  // best-effort live overlay, not a load-bearing data source.
  useEffect(() => {
    const source = new EventSource(IB_STREAM_URL)
    source.onmessage = (e) => {
      const { prices, positions: pos } = JSON.parse(e.data)
      setLivePrices(prices)
      setPositions(pos)
    }
    source.onerror = () => {} // EventSource auto-reconnects; nothing to do here.
    return () => source.close()
  }, [])

  useEffect(() => {
    fetch('/price_history_daily_3mo.json')
      .then((r) => (r.ok ? r.json() : {}))
      .then(setDailyHistory3mo)
      .catch(() => {})
    fetch('/price_history.json')
      .then((r) => (r.ok ? r.json() : {}))
      .then(setMonthlyHistory)
      .catch(() => {})
  }, [])

  useEffect(() => {
    Promise.all([
      fetch('/sorted_screen.csv').then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
        return r.text()
      }),
      // Only covers RATED_FOR_EXTRAS tickers (Strong Buy/Buy/Sell/Strong
      // Sell) of a past run (see social_sentiment.py) and may not exist
      // yet — missing/failed fetch just means every row's sentiment is
      // blank, not a load error.
      fetch('/social_sentiment.json')
        .then((r) => (r.ok ? r.json() : {}))
        .catch(() => ({})) as Promise<Record<string, any>>,
      // Same best-effort contract as social_sentiment.json above, but from
      // ib_price_server.py's news_loop instead of a fetch-once script —
      // may not exist yet (server never run) or only cover tickers that
      // actually had news in the last NEWS_WINDOW_DAYS.
      fetch('/news_sentiment.json')
        .then((r) => (r.ok ? r.json() : {}))
        .catch(() => ({})) as Promise<Record<string, any>>,
      // Same RATED_FOR_EXTRAS scope as social_sentiment.json above, from
      // sec_edgar.py's fetch_form4 (`python main.py form4`) instead of
      // the main pipeline — may not exist yet if that's never been run.
      fetch('/sec/form4/insider_transactions.json')
        .then((r) => (r.ok ? r.json() : {}))
        .catch(() => ({})) as Promise<Record<string, any>>,
      // Same RATED_FOR_EXTRAS scope, from sec_edgar.py's fetch_13f_holdings
      // (`python main.py 13f`) — pctShareChangeQoQ per ticker, already a
      // single number (no per-article/per-filing averaging needed the way
      // newsSentiment/insiderTransactions above do).
      fetch('/sec/13f/institutional_holdings.json')
        .then((r) => (r.ok ? r.json() : {}))
        .catch(() => ({})) as Promise<Record<string, any>>,
    ] as const)
      .then(([text, sentiment, newsSentiment, insiderTransactions, institutionalHoldings]) => {
        // sorted_screen.csv is already ranked best-to-worst by score; capture
        // that as a fixed rank so it doesn't shift when the table is
        // re-sorted or filtered.
        const parsed: RawScreenerRow[] = parseCSV(text).map((r: any, idx: number) => {
          const newsSent = avgNewsSentiment(newsSentiment[r.ticker])
          const insiders = avgInsiderScore(insiderTransactions[r.ticker])
          // Simple average of whichever of the two periods is present —
          // both are already the same %-change-ratio scale (see
          // IBApp._eps_revision), unlike e.g. shortRatio/shortPercentOfFloat,
          // which need rank-averaging instead of raw-value averaging.
          const epsTrendParts = [toNum(r.epsRevision0y), toNum(r.epsRevision1y)].filter((v) => v !== null) as number[]
          const epsTrend = epsTrendParts.length
            ? epsTrendParts.reduce((a, b) => a + b, 0) / epsTrendParts.length
            : null
          return {
            rank: idx + 1,
            t: r.ticker,
            n: r.name,
            s: r.sector || 'Unclassified',
            sent: toNum(sentiment[r.ticker]?.score),
            sentBullish: toNum(sentiment[r.ticker]?.bullish),
            sentBearish: toNum(sentiment[r.ticker]?.bearish),
            sentTotal: toNum(sentiment[r.ticker]?.total),
            newsSent: newsSent.avg !== null ? newsSent.avg - 3 : null,
            newsSentCount: newsSent.count || null,
            instChange: toNum(institutionalHoldings[r.ticker]?.pctShareChangeQoQ),
            instChangeRaw: toNum(institutionalHoldings[r.ticker]?.pctShareChangeQoQ),
            // ×100 to sit on the same visual scale as Momentum/MeanRev/etc
            // (their post-rankTo100 range), but NOT rank-rescaled the way
            // those are (see the loop below, which deliberately excludes
            // 'insiders') -- insiders.avg is already a bounded, comparable
            // ratio (buys-sells)/(buys+sells) in [-1, 1], and this
            // universe's insider-BUY activity is dominated by a huge tied
            // block of tickers with literally zero buys (confirmed: ~78%
            // of tickers with any Form4 P/S activity have a raw ratio of
            // exactly -1.0) -- a percentile rank ties that whole block to
            // the worst rank and then lets a single stray buy against
            // dozens of sells vault a ticker dramatically up the scale
            // just for not being tied to that block (confirmed live:
            // LQDA, 1 buy vs. 79 sells, ranked at the 78th percentile and
            // displayed as strongly positive despite being 98.8% sells).
            // The raw ratio itself has no such artifact, so it's used
            // directly instead.
            insiders: insiders.avg !== null ? insiders.avg * 100 : null,
            insiderBuys: insiders.buys,
            insiderSells: insiders.sells,
            beta: toNum(r.beta),
            fpe: toNum(r.forwardPE),
            feps: toNum(r.forwardEps),
            epsTrend,
            tpe: toNum(r.trailingPE),
            tps: toNum(r.trailingPS),
            peg: toNum(r.pegRatio),
            revg: toNum(r.revenueGrowth),
            pfcf: toNum(r.priceToFCF),
            evEbitda: toNum(r.enterpriseToEbitda),
            opMargin: toNum(r.operatingMargins),
            de: toNum(r.debtToEquity),
            liq: toNum(r.LiqRatio),
            // shortInt is the sorted/displayed value (shortPercentOfFloat —
            // the standard, cross-company-comparable short-interest metric);
            // shortRatio (days-to-cover) isn't separately displayed, only
            // used alongside it for the blended subrank below, same
            // "average of two ranks on incompatible scales" pattern as
            // main.py's own short_interest_ranks.
            shortInt: toNum(r.shortPercentOfFloat),
            shortRatio: toNum(r.shortRatio),
            p: toNum(r.price),
            tgt: toNum(r.targetMeanPrice),
            tgtHigh: toNum(r.targetHighPrice),
            tgtLow: toNum(r.targetLowPrice),
            numAnalysts: toNum(r.numberOfAnalystOpinions),
            upside: toNum(r.targetUpside),
            rec: r.recommendationKey || null,
            rating: r.rating || null,
            mom: toNum(r.momentum),
            mr: toNum(r.meanReversion),
            sc: toNum(r.score),
            ern: toNum(r.earningsTimestampStart),
            upd: r.lastDownload || null,
          }
        })
        // Momentum/MeanRev/Sentiment/News share one common display scale:
        // rank-rescaled to [-100, 100] against whatever this fetch
        // actually observed (see rankTo100), so the single worst/best-
        // ranked reading in each is always -100/+100 — immune to one
        // outlier crushing everything else toward one end, unlike a
        // min-max rescale (see rankTo100's own comment for why that was
        // tried first and replaced). Insiders deliberately excluded from
        // this loop — see its own ×100 comment above for why a
        // percentile rank is the wrong treatment for that one factor.
        // Ranking (rankDescending below) is unaffected either way — a
        // monotonic rescale never changes relative order.
        for (const key of ['mom', 'mr', 'sent', 'newsSent', 'instChange'] as const) {
          const ranked = rankTo100(parsed.map((r) => r[key]))
          parsed.forEach((r, i) => {
            r[key] = ranked[i]
          })
        }
        setRawRows(parsed)
      })
      .catch((e) => setError(e.message))
  }, [])

  // Sector's average forward P/E, computed once across the full (unfiltered)
  // list — the same figure main.py uses for the sector-relative score factor.
  // Excludes a non-positive forwardPE (main.py's write_sorted_screen_csv now
  // appends those unranked, for visibility only — see load_top_tickers) the
  // same way sector_avg_forward_pe does server-side: a negative estimate
  // isn't a "low" multiple, it'd just drag the sector average down into
  // meaninglessness.
  const sectorAvgPE = useMemo(() => {
    if (!rawRows) return new Map<string, number>()
    const sums = new Map<string, number>()
    const counts = new Map<string, number>()
    for (const r of rawRows) {
      if (r.s && r.fpe !== null && r.fpe > 0) {
        sums.set(r.s, (sums.get(r.s) || 0) + r.fpe)
        counts.set(r.s, (counts.get(r.s) || 0) + 1)
      }
    }
    const avg = new Map<string, number>()
    for (const [s, sum] of sums) avg.set(s, sum / (counts.get(s) as number))
    return avg
  }, [rawRows])

  // Per-metric subranks (best = 1), fixed across the full unfiltered list —
  // same "doesn't move when you filter" convention as the # column. Negative
  // forwardPE/priceToFCF/pegRatio/debtToEquity are excluded rather than
  // ranked as "cheap"/"low debt" (most-negative sorting as "best" under a
  // naive ascending rank, the opposite of what it means), matching how
  // main.py's scoring treats them.
  const fpeRank = useMemo(() => (rawRows ? rankAscending(rawRows, 'fpe', (v) => v > 0) : new Map<string, number>()), [rawRows])
  const pegRank = useMemo(() => (rawRows ? rankAscending(rawRows, 'peg', (v) => v > 0) : new Map<string, number>()), [rawRows])
  const tpsRank = useMemo(() => (rawRows ? rankAscending(rawRows, 'tps', (v) => v > 0) : new Map<string, number>()), [rawRows])
  const pfcfRank = useMemo(() => (rawRows ? rankAscending(rawRows, 'pfcf', (v) => v > 0) : new Map<string, number>()), [rawRows])
  const evEbitdaRank = useMemo(
    () => (rawRows ? rankAscending(rawRows, 'evEbitda', (v) => v > 0) : new Map<string, number>()),
    [rawRows]
  )
  const deRank = useMemo(() => (rawRows ? rankAscending(rawRows, 'de', (v) => v >= 0) : new Map<string, number>()), [rawRows])
  const momRank = useMemo(() => (rawRows ? rankDescending(rawRows, 'mom') : new Map<string, number>()), [rawRows])
  const mrRank = useMemo(() => (rawRows ? rankDescending(rawRows, 'mr') : new Map<string, number>()), [rawRows])
  const epsTrendRank = useMemo(() => (rawRows ? rankDescending(rawRows, 'epsTrend') : new Map<string, number>()), [rawRows])
  const upsideRank = useMemo(() => (rawRows ? rankDescending(rawRows, 'upside') : new Map<string, number>()), [rawRows])
  // Mirrors scoring.growth_rank's own GROWTH_CAP: a near-zero-revenue
  // base-effect artifact (a raw revg in the thousands of percent)
  // shouldn't claim the single best subrank just for being the most
  // extreme value. Capped for this rank computation only -- the r.revg
  // cell rendered below (fmtPct(r.revg)) stays the actual, uncapped number.
  const revgRank = useMemo(() => {
    if (!rawRows) return new Map<string, number>()
    const GROWTH_RANK_CAP = 3.0 // +300%, mirrors scoring.growth_rank's GROWTH_CAP
    const capped = rawRows.map((r) => (r.revg !== null && r.revg > GROWTH_RANK_CAP ? { ...r, revg: GROWTH_RANK_CAP } : r))
    return rankDescending(capped, 'revg')
  }, [rawRows])
  const liqRank = useMemo(() => (rawRows ? rankDescending(rawRows, 'liq') : new Map<string, number>()), [rawRows])
  const sentRank = useMemo(() => (rawRows ? rankDescending(rawRows, 'sent') : new Map<string, number>()), [rawRows])
  const newsSentRank = useMemo(() => (rawRows ? rankDescending(rawRows, 'newsSent') : new Map<string, number>()), [rawRows])
  const instChangeRank = useMemo(() => (rawRows ? rankDescending(rawRows, 'instChange') : new Map<string, number>()), [rawRows])
  const insidersRank = useMemo(() => (rawRows ? rankDescending(rawRows, 'insiders') : new Map<string, number>()), [rawRows])

  // Short Interest's subrank blends both ranks the same way main.py's
  // short_interest_ranks does — shortInt (shortPercentOfFloat) and
  // shortRatio (days-to-cover) are on incompatible scales, so the ranks
  // are averaged, not the raw values. A ticker missing just one of the
  // two still gets a subrank from whichever it has; only missing both
  // yields no subrank at all.
  const shortPctRank = useMemo(() => (rawRows ? rankDescending(rawRows, 'shortInt') : new Map<string, number>()), [rawRows])
  const shortRatioRank = useMemo(
    () => (rawRows ? rankDescending(rawRows, 'shortRatio') : new Map<string, number>()),
    [rawRows]
  )
  const shortIntRank = useMemo(() => {
    const map = new Map<string, number>()
    for (const r of rawRows || []) {
      const a = shortPctRank.get(r.t) ?? null
      const b = shortRatioRank.get(r.t) ?? null
      if (a === null && b === null) continue
      map.set(r.t, a === null ? (b as number) : b === null ? a : (a + b) / 2)
    }
    return map
  }, [rawRows, shortPctRank, shortRatioRank])

  // Rank on forwardPE - trailingPE (more negative = better), same factor
  // main.py's score weights at 10%. Infinite or negative trailingPE (no real
  // earnings signal) is treated as 200 for this calculation only — the table
  // still displays the real trailingPE value.
  const diffRank = useMemo(() => {
    if (!rawRows) return new Map<string, number>()
    const valid = rawRows
      .map((r) => {
        // A non-positive forwardPE is itself a broken input to this
        // factor (see fpeRank above) — excluded here too rather than
        // producing an artificially very-negative diff that would sort
        // as "best".
        if (r.fpe === null || r.fpe <= 0 || r.tpe === null) return { t: r.t, diff: null as number | null }
        const effectiveTpe = !Number.isFinite(r.tpe) || r.tpe < 0 ? 200 : r.tpe
        return { t: r.t, diff: r.fpe - effectiveTpe }
      })
      .filter((r): r is { t: string; diff: number } => r.diff !== null)
    valid.sort((a, b) => a.diff - b.diff)
    const map = new Map<string, number>()
    valid.forEach((r, i) => map.set(r.t, i + 1))
    return map
  }, [rawRows])

  const rows: ScreenerRow[] | null = useMemo(() => {
    if (!rawRows) return null
    return rawRows.map((r) => ({
      ...r,
      savgpe: sectorAvgPE.get(r.s) ?? null,
      fpeRank: fpeRank.get(r.t) ?? null,
      pegRank: pegRank.get(r.t) ?? null,
      tpsRank: tpsRank.get(r.t) ?? null,
      pfcfRank: pfcfRank.get(r.t) ?? null,
      evEbitdaRank: evEbitdaRank.get(r.t) ?? null,
      deRank: deRank.get(r.t) ?? null,
      momRank: momRank.get(r.t) ?? null,
      mrRank: mrRank.get(r.t) ?? null,
      epsTrendRank: epsTrendRank.get(r.t) ?? null,
      upsideRank: upsideRank.get(r.t) ?? null,
      revgRank: revgRank.get(r.t) ?? null,
      diffRank: diffRank.get(r.t) ?? null,
      liqRank: liqRank.get(r.t) ?? null,
      shortIntRank: shortIntRank.get(r.t) ?? null,
      sentRank: sentRank.get(r.t) ?? null,
      newsSentRank: newsSentRank.get(r.t) ?? null,
      instChangeRank: instChangeRank.get(r.t) ?? null,
      insidersRank: insidersRank.get(r.t) ?? null,
    }))
  }, [
    rawRows,
    sectorAvgPE,
    fpeRank,
    pegRank,
    tpsRank,
    pfcfRank,
    evEbitdaRank,
    deRank,
    momRank,
    mrRank,
    epsTrendRank,
    upsideRank,
    revgRank,
    diffRank,
    liqRank,
    shortIntRank,
    sentRank,
    newsSentRank,
    instChangeRank,
    insidersRank,
  ])

  const industryCounts: [string, number][] = useMemo(() => {
    if (!rows) return []
    const counts = new Map<string, number>()
    for (const r of rows) counts.set(r.s, (counts.get(r.s) || 0) + 1)
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
  }, [rows])

  // Broad sector groupings (Technology, Healthcare, etc.) derived from the
  // granular industry — a coarser filter dimension layered on top; every
  // other computation (scoring, subranks, industry avg PE) stays industry-based.
  const groupCounts: [string, number][] = useMemo(() => {
    if (!rows) return []
    const counts = new Map<string, number>()
    for (const r of rows) {
      const g = getSectorGroup(r.s)
      counts.set(g, (counts.get(g) || 0) + 1)
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
  }, [rows])

  // Fixed Strong Buy → Strong Sell → NA order (not sorted by count, unlike
  // industryCounts/groupCounts above) — a rating's natural rank order is
  // more useful to scan here than "biggest bucket first". NA is a real
  // main.py rating value (see RATING_NA), not "no data" — every
  // non-positive-forwardPE ticker gets it explicitly rather than a blank.
  const ratingCounts: [string, number][] = useMemo(() => {
    if (!rows) return []
    const order = ['Strong Buy', 'Buy', 'Hold', 'Sell', 'Strong Sell', 'NA']
    const counts = new Map<string, number>()
    for (const r of rows) {
      const key = r.rating || 'NA'
      counts.set(key, (counts.get(key) || 0) + 1)
    }
    return order.filter((name) => counts.has(name)).map((name) => [name, counts.get(name) as number] as [string, number])
  }, [rows])

  const scoreRange: [number, number] = useMemo(() => {
    if (!rows) return [0, 1]
    const scores = rows.map((r) => r.sc).filter((v): v is number => v !== null)
    return [Math.min(...scores), Math.max(...scores)]
  }, [rows])

  // Count of actually-ranked rows (sc !== null) — the denominator for the
  // # column's percentile display below. Deliberately excludes the
  // unranked, non-positive-forwardPE tail main.py appends after every
  // real rank (see write_sorted_screen_csv): those rows' `rank` (their
  // raw idx+1 position in the CSV) is a position in that alphabetical
  // tail, not a real score-based rank, so rank/rankedTotal for one of
  // them would come out over 100% instead of being suppressed.
  const rankedTotal = useMemo(() => {
    if (!rows) return 0
    return rows.filter((r) => r.sc !== null).length
  }, [rows])

  const filtered: ScreenerRow[] = useMemo(() => {
    if (!rows) return []
    const q = search.trim().toLowerCase()
    return rows.filter((r) => {
      if (selectedIndustries.size && !selectedIndustries.has(r.s)) return false
      if (selectedGroups.size && !selectedGroups.has(getSectorGroup(r.s))) return false
      if (selectedRatings.size && !selectedRatings.has(r.rating || 'NA')) return false
      if (q && !r.t.toLowerCase().includes(q) && !r.n.toLowerCase().includes(q)) return false
      // No position at all (undefined, or a real 0-share record) fails
      // this — long AND short holdings both pass, since either one means
      // "any actual position exists", not just a long one.
      if (nonZeroOnly && !positions[r.t]?.shares) return false
      return true
    })
  }, [rows, search, selectedIndustries, selectedGroups, selectedRatings, nonZeroOnly, positions])

  const sorted: ScreenerRow[] = useMemo(() => {
    const copy = [...filtered]
    copy.sort((a, b) => {
      const av = a[sortKey as keyof ScreenerRow]
      const bv = b[sortKey as keyof ScreenerRow]
      if (typeof av === 'string') return av.localeCompare(bv as string) * sortDir
      const an = av === null
      const bn = bv === null
      if (an && bn) return 0
      if (an) return 1
      if (bn) return -1
      return ((av as number) - (bv as number)) * sortDir
    })
    return copy
  }, [filtered, sortKey, sortDir])

  // Reset to the first page whenever the result set itself changes shape —
  // search/filter/sort — so page 3 of an old, larger result set doesn't
  // silently show page 3 of a much smaller new one (or nothing at all).
  // React's documented "adjusting state during render" pattern (same as
  // FlashCell's flash-on-change below) rather than a useEffect: comparing
  // against the last-seen filter key and calling setPage directly here is
  // safe since React re-renders with the new state before committing, and
  // avoids the extra render+effect pass a useEffect would cost.
  const filterKey = JSON.stringify([
    search,
    [...selectedIndustries],
    [...selectedGroups],
    [...selectedRatings],
    nonZeroOnly,
    sortKey,
    sortDir,
  ])
  const [lastFilterKey, setLastFilterKey] = useState(filterKey)
  if (filterKey !== lastFilterKey) {
    setLastFilterKey(filterKey)
    setPage(0)
  }

  const pageCount = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE))
  // Clamped separately from the reset above — covers the live data itself
  // changing shape (e.g. a background refresh) without touching page on
  // every unrelated re-render.
  const currentPage = Math.min(page, pageCount - 1)
  const paged = useMemo(
    () => sorted.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE),
    [sorted, currentPage]
  )

  const stats = useMemo(() => {
    if (!sorted.length) return { avgScore: null as number | null, avgMom: null as number | null }
    // Excludes unranked (negative-forwardPE) rows the same way avgMom
    // already excluded a missing momentum — dividing by the full row
    // count while treating a null score as 0 would silently dilute this
    // toward 0 as more unranked rows show up, rather than reflecting the
    // real ranked set's average.
    const scoreVals = sorted.filter((r) => r.sc !== null)
    const avgScore = scoreVals.length ? scoreVals.reduce((s, r) => s + (r.sc as number), 0) / scoreVals.length : null
    const momVals = sorted.filter((r) => r.mom !== null)
    const avgMom = momVals.length ? momVals.reduce((s, r) => s + (r.mom as number), 0) / momVals.length : null
    return { avgScore, avgMom }
  }, [sorted])

  function handleSort(key: string) {
    if (sortKey === key) {
      setSortDir((d) => -d)
    } else {
      setSortKey(key)
      setSortDir(1)
    }
  }

  function toggleIndustry(name: string) {
    setSelectedIndustries((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  function toggleGroup(name: string) {
    setSelectedGroups((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  function toggleRating(name: string) {
    setSelectedRatings((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  const [scoreMin, scoreMax] = scoreRange
  const scoreSpan = scoreMax - scoreMin || 1

  return (
    <>
      <header className="masthead">
        <div className="title-block">
          <div className="title-with-info">
            <h1>Stock Screener</h1>
            <ScoreFormula />
          </div>
        </div>
        <div className="stat-row">
          <div className="stat">
            <span className="n num">{rows ? sorted.length : '—'}</span>
            <span className="l">shown</span>
          </div>
          <div className="stat">
            <span className="n num">{stats.avgScore === null ? '—' : stats.avgScore.toFixed(3)}</span>
            <span className="l">avg score</span>
          </div>
          <div className="stat">
            <span className="n num">{stats.avgMom === null ? '—' : fmtIndex100(stats.avgMom)}</span>
            <span className="l">avg momentum</span>
          </div>
        </div>
      </header>

      <div className="controls">
        <div className="search-box">
          <Search />
          <input
            type="text"
            placeholder="Search ticker or name…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        {rows && (
          <FilterDropdown
            noun="industry"
            plural="industries"
            items={industryCounts}
            selected={selectedIndustries}
            onToggle={toggleIndustry}
            onClear={() => setSelectedIndustries(new Set())}
            getIcon={getSectorIcon}
          />
        )}

        {rows && (
          <FilterDropdown
            noun="sector"
            items={groupCounts}
            selected={selectedGroups}
            onToggle={toggleGroup}
            onClear={() => setSelectedGroups(new Set())}
          />
        )}

        {rows && (
          <FilterDropdown
            noun="rating"
            plural="ratings"
            items={ratingCounts}
            selected={selectedRatings}
            onToggle={toggleRating}
            onClear={() => setSelectedRatings(new Set())}
          />
        )}

        <label className="position-filter">
          <input type="checkbox" checked={nonZeroOnly} onChange={(e) => setNonZeroOnly(e.target.checked)} />
          Position &lt;&gt; 0
        </label>

        <div className="active-chips">
          {[...selectedIndustries].map((s) => (
            <span className="chip" key={`ind-${s}`}>
              {s}
              <button type="button" aria-label={`Remove ${s} filter`} onClick={() => toggleIndustry(s)}>
                <X />
              </button>
            </span>
          ))}
          {[...selectedGroups].map((g) => (
            <span className="chip" key={`grp-${g}`}>
              {g}
              <button type="button" aria-label={`Remove ${g} filter`} onClick={() => toggleGroup(g)}>
                <X />
              </button>
            </span>
          ))}
          {[...selectedRatings].map((r) => (
            <span className="chip" key={`rat-${r}`}>
              {r}
              <button type="button" aria-label={`Remove ${r} filter`} onClick={() => toggleRating(r)}>
                <X />
              </button>
            </span>
          ))}
        </div>

        {rows && <div className="result-count">{sorted.length} of {rows.length}</div>}
      </div>

      {stickyHeader.stuck && (
        <div
          className="sticky-header-clone"
          style={{ position: 'fixed', top: 0, left: stickyHeader.left, width: stickyHeader.width }}
        >
          <table>
            <thead>
              <tr>
                <HeaderCells sortKey={sortKey} sortDir={sortDir} onSort={handleSort} widths={stickyHeader.widths} />
              </tr>
            </thead>
          </table>
        </div>
      )}

      <div className="table-wrap">
        <table ref={tableRef}>
          <thead>
            <tr>
              <HeaderCells sortKey={sortKey} sortDir={sortDir} onSort={handleSort} />
            </tr>
          </thead>
          <tbody>
            {error && (
              <tr className="status-row">
                <td colSpan={COLUMNS.length}>Couldn't load sorted_screen.csv: {error}</td>
              </tr>
            )}
            {!error && !rows && (
              <tr className="status-row">
                <td colSpan={COLUMNS.length}>Loading…</td>
              </tr>
            )}
            {!error && rows && sorted.length === 0 && (
              <tr className="empty-row">
                <td colSpan={COLUMNS.length}>No stocks match the current filters.</td>
              </tr>
            )}
            {!error && rows && paged.map((r) => {
              const Icon = getSectorIcon(r.s)
              const momClass = r.mom === null ? '' : r.mom >= 0 ? 'perf-pos' : 'perf-neg'
              const mrClass = r.mr === null ? '' : r.mr >= 0 ? 'perf-pos' : 'perf-neg'
              const epsTrendClass = r.epsTrend === null ? '' : r.epsTrend >= 0 ? 'perf-pos' : 'perf-neg'
              const sentClass = r.sent === null ? '' : r.sent >= 0 ? 'perf-pos' : 'perf-neg'
              const newsSentClass = r.newsSent === null ? '' : r.newsSent >= 0 ? 'perf-pos' : 'perf-neg'
              const instChangeClass = r.instChange === null ? '' : r.instChange >= 0 ? 'perf-pos' : 'perf-neg'
              const insidersClass = r.insiders === null ? '' : r.insiders >= 0 ? 'perf-pos' : 'perf-neg'
              const upsideClass = r.upside === null ? '' : r.upside >= 0 ? 'perf-pos' : 'perf-neg'
              // Green only above 10% growth, red below 0%, neutral between.
              const revgClass = inversePctThresholdClass(r.revg, 0, 10)
              // Same thresholds as the asset page: PEG < 1 cheap relative to
              // growth, > 1 pricey; Liq Ratio > 3 comfortably liquid, < 1 weak;
              // P/FCF, Trailing PE, Fwd PE cheap under 10x, expensive above 50x.
              const pegClass = rangeClass(r.peg, 1, 1)
              const liqClass = inverseRangeClass(r.liq, 1, 3)
              // Contrarian direction (high is good) — >10% of float short
              // is genuinely notable short-interest territory; <2% is
              // unremarkable. Same asymmetric-threshold helper revgClass
              // uses, just green/red swapped in spirit since this factor
              // scores the opposite way of everything else on the page.
              const shortIntClass = inversePctThresholdClass(r.shortInt, 2, 10)
              const pfcfClass = rangeClass(r.pfcf, 10, 50)
              // EV/EBITDA multiples run lower than P/E or P/FCF in
              // practice, so a tighter cheap/expensive band than those
              // (10, 50) — cheap under 10x, expensive above 20x.
              const evEbitdaClass = rangeClass(r.evEbitda, 10, 20)
              // High is good, same pattern as revgClass — red below 0%
              // (operating losses), green above 15% (a healthy operating
              // margin), neutral between.
              const opMarginClass = inversePctThresholdClass(r.opMargin, 0, 15)
              const tpeClass = rangeClass(r.tpe, 10, 50)
              // P/S runs on a much smaller scale than P/E/P-FCF — cheap
              // under 2x revenue, expensive above 10x (vs. those factors'
              // 10/50 band).
              const tpsClass = rangeClass(r.tps, 2, 10)
              const fpeClass = rangeClass(r.fpe, 10, 50)
              // r.de is already in percentage-point units (150.5 means
              // 150.5%), same as the threshold values below — under 50%
              // green, over 200% red (rangeClass also reds out a negative
              // debtToEquity, which comes from negative shareholder
              // equity/financial distress, not "low debt"). Colors
              // themselves stay in styles.scss's .good/.bad rules; this
              // only picks which class name applies.
              const deClass = rangeClass(r.de, 50, 200)
              const earningsClass = earningsUrgencyClass(r.ern, now)
              // livePrices now covers every screener ticker, not just a
              // top-ranked slice — ib_price_server.py streams the top
              // MAX_STREAMED_SYMBOLS live and snapshot-polls everything
              // else (see snapshot_loop), so any row can have an entry.
              const live = livePrices[r.t]
              // Genuine prior close (whichever of IB's own daily bars or
              // yfinance is actually fresher — see previousClose's own
              // comment), not r.p — sorted_screen.csv's price is
              // whatever main.py's yfinance fetch last happened to see,
              // a live quote at fetch time rather than a settled close,
              // which made this badge compare "now" against an arbitrary
              // moment instead of a real daily change.
              const referencePrice = previousClose(dailyHistory3mo[r.t], monthlyHistory[r.t]) ?? r.p
              const liveRatio = live?.last != null && referencePrice ? live.last / referencePrice - 1 : null
              const liveClass =
                liveRatio === null
                  ? ''
                  : Math.abs(liveRatio) <= 0.005
                    ? 'perf-neutral'
                    : liveRatio >= 0
                      ? 'perf-pos'
                      : 'perf-neg'
              // Positions come from ib_price_server.py and can exist for any
              // ticker — read independent of `live` — value uses the live
              // price when this row has one (same price shown in the
              // Price column), else falls back to the sorted_screen.csv
              // price.
              const possize = positions[r.t]?.shares ?? null
              const posPrice = live?.last ?? r.p
              const posval = possize !== null && posPrice !== null ? possize * posPrice : null
              const scorePct = r.sc === null ? 0 : ((r.sc - scoreMin) / scoreSpan) * 100
              return (
                <tr key={r.t}>
                  <td className="col-left col-ticker num">
                    <a
                      href={`#/asset/${encodeURIComponent(r.t)}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="ticker-link"
                    >
                      {r.t}
                    </a>
                    <a
                      href={newsPopupHref(r.t)}
                      onClick={(e) => openNewsPopup(e, r.t)}
                      className="news-popup-link"
                      title={`Open ${r.t} news`}
                      aria-label={`Open ${r.t} news`}
                    >
                      <Newspaper size={11} />
                    </a>
                    <a
                      href={zacksUrl(r.t)}
                      onClick={(e) => openZacksPopup(e, r.t)}
                      className="zacks-link"
                      title={`Open ${r.t} on Zacks`}
                      aria-label={`Open ${r.t} on Zacks`}
                    >
                      <ExternalLink size={11} />
                    </a>
                  </td>
                  <td className={`col-left col-name ${earningsClass}`} title={nameTooltip(r)}>{r.n}</td>
                  <td className="col-left">
                    <span className="sector-cell" title={r.s}>
                      <Icon />
                      <span className="sector-cell-name">{r.s}</span>
                    </span>
                  </td>
                  <td className="num">{fmtShares(possize)}</td>
                  <td className="num">{fmtMoney(posval)}</td>
                  <td className="col-rec" title="This screener's own forced-distribution rating, from its score percentile — not an analyst consensus.">
                    <span className={`rec-badge ${ratingClass(r.rating)}`}>{r.rating || '—'}</span>
                  </td>
                  <td className="num price-cell">
                    <span className="price-value">{live?.last != null ? fmtPrice(live.last) : fmtPrice(r.p)}</span>
                    {liveRatio !== null && live && (
                      <span
                        className={`live-price ${liveClass}`}
                        title={`IB Gateway ${fmtPrice(live.last ?? null)} at ${live.timestamp} vs. yesterday's close ${fmtPrice(referencePrice)}`}
                      >
                        {fmtPct(liveRatio)}
                      </span>
                    )}
                  </td>
                  <td className="num">{fmtNum(r.beta)}</td>
                  <td className="num">{fmtNum(r.savgpe)}</td>
                  <td className={`num ${fpeClass}`}>{fmtNum(r.fpe)} <Subrank rank={r.fpeRank} /></td>
                  <td className="num">{fmtPrice(r.feps)}</td>
                  <td className={`num ${epsTrendClass}`}>
                    {fmtPct(r.epsTrend)} <Subrank rank={r.epsTrendRank} />
                  </td>
                  <td className={`num ${tpeClass}`}>{fmtNum(r.tpe)} <Subrank rank={r.diffRank} /></td>
                  <td className={`num ${tpsClass}`}>{fmtNum(r.tps)} <Subrank rank={r.tpsRank} /></td>
                  <td className={`num ${pegClass}`}>{fmtNum(r.peg)} <Subrank rank={r.pegRank} /></td>
                  <td className={`num ${revgClass}`}>{fmtPct(r.revg)} <Subrank rank={r.revgRank} /></td>
                  <td className={`num ${pfcfClass}`}>{fmtNum(r.pfcf)} <Subrank rank={r.pfcfRank} /></td>
                  <td className={`num ${evEbitdaClass}`}>{fmtNum(r.evEbitda)} <Subrank rank={r.evEbitdaRank} /></td>
                  <td className={`num ${opMarginClass}`}>{fmtPct(r.opMargin)}</td>
                  <td className={`num ${deClass}`}>{fmtDebtToEquity(r.de)} <Subrank rank={r.deRank} /></td>
                  <td className={`num ${liqClass}`}>{fmtNum(r.liq)} <Subrank rank={r.liqRank} /></td>
                  <td
                    className={`num ${shortIntClass}`}
                    title={r.shortRatio !== null ? `${fmtNum(r.shortRatio)} days to cover (short ratio)` : undefined}
                  >
                    {fmtPct(r.shortInt)} <Subrank rank={r.shortIntRank} />
                  </td>
                  <td className="num tooltip-cell" data-tip={targetTooltip(r)}>{fmtPrice(r.tgt)}</td>
                  <td className={`num ${upsideClass}`}>{fmtPct(r.upside)} <Subrank rank={r.upsideRank} /></td>
                  <td className="col-rec">
                    <span className={`rec-badge ${recClass(r.rec)}`}>{recLabel(r.rec)}</span>
                  </td>
                  <td className={`num ${momClass}`}>{fmtIndex100(r.mom)} <Subrank rank={r.momRank} /></td>
                  <td className={`num ${mrClass}`}>{fmtIndex100(r.mr)} <Subrank rank={r.mrRank} /></td>
                  <td className={`num tooltip-cell ${sentClass}`} data-tip={sentimentTooltip(r)}>
                    {fmtIndex100(r.sent)} <Subrank rank={r.sentRank} />
                  </td>
                  <td className={`num tooltip-cell ${newsSentClass}`} data-tip={newsSentimentTooltip(r)}>
                    {fmtIndex100(r.newsSent)} <Subrank rank={r.newsSentRank} />
                  </td>
                  <td className={`num tooltip-cell ${instChangeClass}`} data-tip={instChangeTooltip(r)}>
                    {fmtIndex100(r.instChange)} <Subrank rank={r.instChangeRank} />
                  </td>
                  <td className={`num tooltip-cell ${insidersClass}`} data-tip={insidersTooltip(r)}>
                    {fmtIndex100(r.insiders)} <Subrank rank={r.insidersRank} />
                  </td>
                  <td className="num">
                    <div className="score-cell">
                      <span className="score-bar"><span style={{ width: `${(100 - scorePct).toFixed(1)}%` }} /></span>
                      {fmtScore(r.sc)}
                    </div>
                  </td>
                  <td
                    className="num rank-cell tooltip-cell"
                    data-tip={percentileRankTooltip(r.sc !== null ? r.rank : null, rankedTotal)}
                  >
                    {fmtPercentileRank(r.sc !== null ? r.rank : null, rankedTotal)}
                  </td>
                  <td className="col-left">{fmtDate(r.upd)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {rows && sorted.length > PAGE_SIZE && (
        <div className="pagination">
          <button type="button" onClick={() => setPage(currentPage - 1)} disabled={currentPage === 0}>
            Prev
          </button>
          <span className="pagination-info">
            Page {currentPage + 1} of {pageCount} · {sorted.length} results
          </span>
          <button type="button" onClick={() => setPage(currentPage + 1)} disabled={currentPage >= pageCount - 1}>
            Next
          </button>
        </div>
      )}
    </>
  )
}
