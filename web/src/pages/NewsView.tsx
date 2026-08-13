import { useEffect, useMemo, useState } from 'react'
import { Search } from 'lucide-react'
import { IB_NEWS_ARTICLE_URL, IB_NEWS_URL, IB_STREAM_URL } from '../ibStream'
import { SENTIMENT_LABEL, fmtNewsTime, sentimentClass } from '../news'
import type { Article, ArticlesByTicker, FlatArticle, PositionsByTicker } from '../interfaces/INewsView'

const PAGE_SIZE = 100

// Flattens {ticker: [article, ...]} (GET /api/news, newest first per
// ticker -- see ib_price_server.py's _news_snapshot) into one flat
// [{ticker, articleId, time, provider, headline, sentiment}, ...] list
// across every ticker, re-sorted newest first globally -- per-ticker
// order alone doesn't give one combined chronological order across
// tickers.
function flattenNews(byTicker: ArticlesByTicker): FlatArticle[] {
  const rows: FlatArticle[] = []
  for (const [ticker, articles] of Object.entries(byTicker)) {
    for (const a of articles) rows.push({ ticker, ...a })
  }
  rows.sort((a, b) => (a.time < b.time ? 1 : a.time > b.time ? -1 : 0))
  return rows
}

interface ArticleBody {
  open: boolean
  text: string | null
  error: string | null
  loading: boolean
}

// One headline row, lazily expandable to its full article body (same
// GET /api/news/article, on-demand-only pattern as Asset.jsx's NewsItem —
// see IBApp.get_news_article_async for why this is never bulk-fetched).
// Rendered as two <tr>s so the expanded body row can span the whole table
// width instead of squeezing into the Headline column.
function NewsRow({ article }: { article: FlatArticle }) {
  const [body, setBody] = useState<ArticleBody>({ open: false, text: null, error: null, loading: false })

  function toggle() {
    if (!body.open) {
      if (body.text === null && !body.loading && !body.error) {
        setBody({ open: true, text: null, error: null, loading: true })
        fetch(
          `${IB_NEWS_ARTICLE_URL}?ticker=${encodeURIComponent(article.ticker)}&articleId=${encodeURIComponent(article.articleId)}`
        )
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
    <>
      <tr>
        <td className="col-left">{fmtNewsTime(article.time)}</td>
        <td className="col-left">
          <a
            href={`#/asset/${encodeURIComponent(article.ticker)}`}
            target="_blank"
            rel="noopener noreferrer"
            className="ticker-link"
          >
            {article.ticker}
          </a>
        </td>
        <td className={`col-left news-sentiment ${sentimentClass(article.sentiment)}`}>
          {(article.sentiment !== null && (SENTIMENT_LABEL as Record<number, string>)[article.sentiment]) || '—'}
        </td>
        <td className="col-left">{article.provider}</td>
        <td className="col-left news-headline-expandable" onClick={toggle}>
          {article.headline}
        </td>
      </tr>
      {body.open && (
        <tr>
          <td className="news-body" colSpan={5}>
            {body.loading && 'Loading article…'}
            {body.error && `Couldn't load article — ${body.error}`}
            {body.text}
          </td>
        </tr>
      )}
    </>
  )
}

// Every scored headline currently cached by ib_price_server.py's news_loop
// (same rolling NEWS_WINDOW_DAYS window as news.json/news_sentiment.json,
// now 1 month — see _prune_and_write_news), across the whole screener
// universe, newest first, paginated the same way PeTable.jsx paginates the
// screener itself. Best-effort live overlay like the rest of this app's
// news UI (Asset.jsx's NewsPanel, NewsPopup.jsx): a missing/unreachable
// server just means an empty state, not a page error.
export default function NewsView() {
  const [rows, setRows] = useState<FlatArticle[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [page, setPage] = useState(0)
  const [tickerFilter, setTickerFilter] = useState('')
  // Neutral (score 3) headlines are mostly routine filings/dividend
  // notices — real noise in a feed meant for scanning, same reasoning
  // PeTable.jsx's News Sentiment column already excludes them for (see
  // avgNewsSentiment). Hidden by default; this is just a view-level
  // filter, not a re-score, so toggling it never touches the data itself.
  const [showNeutral, setShowNeutral] = useState(false)
  // {ticker: {shares, avgCost}} — same live SSE stream every other
  // positions-aware tab (Positions/Sectors/Themes) already subscribes
  // to, used only to know which tickers are currently held. Best-effort:
  // no server running just means the filter checkbox below has nothing
  // to show as "held", same "empty state, not an error" treatment the
  // rest of this page's news fetch already uses.
  const [positions, setPositions] = useState<PositionsByTicker>({})
  const [heldOnly, setHeldOnly] = useState(false)

  useEffect(() => {
    const source = new EventSource(IB_STREAM_URL)
    source.onmessage = (e) => {
      const { positions: pos } = JSON.parse(e.data)
      setPositions(pos || {})
    }
    source.onerror = () => {} // EventSource auto-reconnects; nothing to do here.
    return () => source.close()
  }, [])

  useEffect(() => {
    let cancelled = false
    fetch(IB_NEWS_URL)
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
        return r.json()
      })
      .then((all: ArticlesByTicker) => {
        if (!cancelled) setRows(flattenNews(all))
      })
      .catch((e) => {
        if (!cancelled) setError(e.message)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const tickerQuery = tickerFilter.trim().toUpperCase()
  const byTicker = useMemo(() => {
    if (!rows) return null
    let result = rows
    if (tickerQuery) result = result.filter((a) => a.ticker.toUpperCase().includes(tickerQuery))
    if (heldOnly) result = result.filter((a) => positions[a.ticker]?.shares)
    return result
  }, [rows, tickerQuery, heldOnly, positions])
  const neutralCount = useMemo(() => (byTicker ? byTicker.filter((a) => a.sentiment === 3).length : 0), [byTicker])
  const filtered = useMemo(
    () => (byTicker ? (showNeutral ? byTicker : byTicker.filter((a) => a.sentiment !== 3)) : null),
    [byTicker, showNeutral]
  )

  // Resets to page 0 whenever the ticker filter or showNeutral changes —
  // the result set just changed size out from under whatever page was
  // showing, same "detect the filter changed mid-render" convention as
  // PeTable.jsx's lastFilterKey.
  const filterKey = JSON.stringify([tickerQuery, showNeutral, heldOnly])
  const [lastFilterKey, setLastFilterKey] = useState(filterKey)
  if (filterKey !== lastFilterKey) {
    setLastFilterKey(filterKey)
    setPage(0)
  }

  const pageCount = Math.max(1, Math.ceil((filtered?.length ?? 0) / PAGE_SIZE))
  // Clamped separately from page state — covers a background refresh
  // shrinking the result set out from under the current page, same
  // convention as PeTable.jsx's currentPage.
  const currentPage = Math.min(page, pageCount - 1)
  const paged = useMemo(
    () => (filtered ? filtered.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE) : []),
    [filtered, currentPage]
  )

  return (
    <div className="positions-page news-page">
      <header className="masthead">
        <div className="title-block">
          <h1>News</h1>
        </div>
      </header>

      {rows && rows.length > 0 && (
        <div className="controls">
          <div className="search-box">
            <Search />
            <input
              type="text"
              placeholder="Filter by ticker…"
              value={tickerFilter}
              onChange={(e) => setTickerFilter(e.target.value)}
            />
          </div>

          <label className="position-filter">
            <input type="checkbox" checked={showNeutral} onChange={(e) => setShowNeutral(e.target.checked)} />
            Show neutral news{neutralCount > 0 ? ` (${neutralCount} hidden)` : ''}
          </label>

          <label className="position-filter">
            <input type="checkbox" checked={heldOnly} onChange={(e) => setHeldOnly(e.target.checked)} />
            Held positions only{Object.keys(positions).length > 0 ? ` (${Object.keys(positions).length})` : ''}
          </label>
        </div>
      )}

      {error && (
        <div className="asset-card">Couldn't load news — is ib_price_server.py running? ({error})</div>
      )}
      {!error && rows === null && <div className="asset-card">Loading…</div>}
      {!error && rows && rows.length === 0 && <div className="asset-card">No news available.</div>}
      {!error && rows && rows.length > 0 && byTicker && byTicker.length === 0 && (
        <div className="asset-card">
          No news for{tickerQuery ? ` "${tickerFilter.trim()}"` : ''}
          {heldOnly ? (tickerQuery ? ' among held positions' : ' any held position') : ''}.
        </div>
      )}
      {!error && byTicker && byTicker.length > 0 && filtered && filtered.length === 0 && (
        <div className="asset-card">
          All {byTicker.length} matching headline{byTicker.length === 1 ? ' is' : 's are'} neutral — check "Show
          neutral news" above to see them.
        </div>
      )}

      {!error && filtered && filtered.length > 0 && (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th className="col-left">Time</th>
                  <th className="col-left">Ticker</th>
                  <th className="col-left">Score</th>
                  <th className="col-left">Provider</th>
                  <th className="col-left">Headline</th>
                </tr>
              </thead>
              <tbody>
                {paged.map((a) => (
                  <NewsRow key={`${a.ticker}-${a.articleId}`} article={a} />
                ))}
              </tbody>
            </table>
          </div>

          {filtered.length > PAGE_SIZE && (
            <div className="pagination">
              <button type="button" onClick={() => setPage(currentPage - 1)} disabled={currentPage === 0}>
                Prev
              </button>
              <span className="pagination-info">
                Page {currentPage + 1} of {pageCount} · {filtered.length} results
              </span>
              <button type="button" onClick={() => setPage(currentPage + 1)} disabled={currentPage >= pageCount - 1}>
                Next
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
