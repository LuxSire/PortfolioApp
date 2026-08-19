import { useEffect, useState } from 'react'
import { IB_NEWS_URL } from '../ibStream'
import { SENTIMENT_LABEL, fmtNewsTime, sentimentClass } from '../news'

// Standalone page (see main.jsx's #/news/TICKER route), meant to be opened
// as a real popup window (see PeTable.jsx's openNewsPopup) rather than
// navigated to in-tab — deliberately no tab bar or back link, just this
// one ticker's news. Same live GET /api/news source as Asset.jsx's
// NewsPanel, but as a table instead of a list: a popup this narrow is
// meant for scanning many headlines against a Score column at a glance,
// not reading prose.
export default function NewsPopup({ ticker }) {
  // state.ticker tracks which ticker state.articles/error belong to, same
  // "avoid flashing stale data on a ticker change" convention as Asset.jsx.
  const [state, setState] = useState({ ticker: null, articles: null, error: null })

  useEffect(() => {
    let cancelled = false
    fetch(IB_NEWS_URL)
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
        return r.json()
      })
      .then((all) => {
        if (!cancelled) setState({ ticker, articles: all[ticker] || [], error: null })
      })
      .catch((e) => {
        if (!cancelled) setState({ ticker, articles: null, error: e.message })
      })
    return () => {
      cancelled = true
    }
  }, [ticker])

  const { articles, error } = state.ticker === ticker ? state : { articles: null, error: null }

  return (
    <div className="app news-popup">
      <header className="masthead">
        <h1>{ticker} News</h1>
      </header>

      {error && (
        <div className="asset-card">Couldn't load news — is ib_server.py running? ({error})</div>
      )}
      {!error && articles === null && <div className="asset-card">Loading…</div>}
      {!error && articles && articles.length === 0 && (
        <div className="asset-card">No news for {ticker} in the last few days.</div>
      )}

      {!error && articles && articles.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className="col-left">Time</th>
                <th className="col-left">Score</th>
                <th className="col-left">Provider</th>
                <th className="col-left">Headline</th>
              </tr>
            </thead>
            <tbody>
              {articles.map((a) => (
                <tr key={a.articleId}>
                  <td className="col-left">{fmtNewsTime(a.time)}</td>
                  <td className={`col-left news-sentiment ${sentimentClass(a.sentiment)}`}>
                    {SENTIMENT_LABEL[a.sentiment] || '—'}
                  </td>
                  <td className="col-left">{a.provider}</td>
                  <td className="col-left">{a.headline}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
