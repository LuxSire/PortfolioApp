import { Fragment, useEffect, useMemo, useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { parseCSV } from '../csv'
import { IB_STREAM_URL } from '../ibStream'
import { toNum } from '../screenerFactors'

function fmtMoney(v) {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+$' : '-$') + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })
}

// No +/- sign, unlike fmtMoney above — for a figure that's always
// non-negative by construction (gross value: a sum of absolute values),
// same convention PositionsView.jsx's own Gross Value stat uses.
function fmtMoneyUnsigned(v) {
  if (v === null || v === undefined) return '—'
  return '$' + v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

function fmtPct(v) {
  if (v === null) return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%'
}

const UNCLASSIFIED_THEME = {
  key: '__unclassified__',
  label: 'Unclassified',
  description: 'A held ticker with no entry yet in data/ticker_themes.json — ask for it to be tagged.',
}

// A hand-curated (not fetched/generated) read of "what does this company
// actually do" per held position, tagged against a fixed theme taxonomy
// so exposure is comparable and summable across tickers — see
// data/theme_taxonomy.json (the theme list) and data/ticker_themes.json
// (ticker -> [theme keys], assigned by reading each holding's own
// longBusinessSummary, e.g. raw_data.json's SSRM entry describing gold/
// silver/copper mining in the US, Turkey, Canada, and Argentina). A
// ticker can carry more than one tag (e.g. GOOG: digital advertising AND
// semiconductors/AI infrastructure, both real, simultaneous exposures)
// -- unlike a sector/industry classification, this isn't a forced 100%
// split, so a theme's exposure is the FULL position value of every
// ticker tagged with it, not a fraction. Static and manually maintained:
// there's no fetch script that regenerates these two files, so a newly
// opened position won't have theme tags until someone (a person, or
// Claude in a future conversation) reads its business description and
// adds them -- untagged holdings still show up here, under
// "Unclassified", rather than silently vanishing from the exposure
// totals.
export default function ThemesView() {
  const [taxonomy, setTaxonomy] = useState(null)
  const [tickerThemes, setTickerThemes] = useState({})
  const [tickerInfo, setTickerInfo] = useState({})
  const [positions, setPositions] = useState({})
  const [livePrices, setLivePrices] = useState({})
  const [expandedThemes, setExpandedThemes] = useState(new Set())
  const [error, setError] = useState(null)

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
    Promise.all([
      fetch('/theme_taxonomy.json').then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
        return r.json()
      }),
      fetch('/ticker_themes.json').then((r) => (r.ok ? r.json() : {})),
      fetch('/sorted_screen.csv').then((r) => (r.ok ? r.text() : '')),
    ])
      .then(([themes, themesByTicker, csvText]) => {
        setTaxonomy(themes)
        setTickerThemes(themesByTicker)
        const info = {}
        for (const row of parseCSV(csvText)) {
          info[row.ticker] = { name: row.name, price: toNum(row.price) }
        }
        setTickerInfo(info)
      })
      .catch((e) => setError(e.message))
  }, [])

  // One row per held position, regardless of whether it's tagged yet —
  // see the component-level comment on why an untagged ticker still
  // needs to show up (as "Unclassified") rather than disappear.
  const rows = useMemo(() => {
    return Object.entries(positions)
      .map(([ticker, p]) => {
        const shares = p.shares
        const price = livePrices[ticker]?.last ?? tickerInfo[ticker]?.price ?? null
        const value = shares !== null && price !== null ? shares * price : null
        const themeKeys = tickerThemes[ticker]?.length ? tickerThemes[ticker] : [UNCLASSIFIED_THEME.key]
        return {
          ticker,
          name: tickerInfo[ticker]?.name ?? null,
          shares,
          price,
          value,
          themeKeys,
        }
      })
      .filter((r) => r.shares)
  }, [positions, livePrices, tickerInfo, tickerThemes])

  const grossPortfolioValue = useMemo(() => rows.reduce((s, r) => s + Math.abs(r.value ?? 0), 0), [rows])

  // Each theme's exposure is the sum of the FULL position value of every
  // ticker tagged with it (see component comment: tags aren't a 100%
  // split, so a dual-tagged ticker's value counts toward both of its
  // themes in full, not half each) — signed, so a short position
  // correctly subtracts from a theme's net exposure rather than adding
  // to it, same convention every other $ figure in this app uses.
  const themeRows = useMemo(() => {
    if (!taxonomy) return []
    const allThemes = [...taxonomy, UNCLASSIFIED_THEME]
    const byTheme = new Map(allThemes.map((t) => [t.key, { ...t, tickers: [], netValue: 0, grossValue: 0 }]))
    for (const r of rows) {
      // A ticker tagged with N themes has its value split evenly N ways,
      // not counted in full toward each -- unlike the earlier "gross
      // thematic exposure" version, this makes every theme bucket a true
      // partition of the portfolio: summing every theme's netValue
      // reconstructs the portfolio's own net value exactly (see the
      // Total row below), the same way a position's own % of NAV always
      // sums to the whole account.
      const allocatedValue = r.value === null ? null : r.value / r.themeKeys.length
      for (const key of r.themeKeys) {
        const bucket = byTheme.get(key)
        if (!bucket) continue // a ticker_themes.json entry referencing a theme key no longer in the taxonomy
        bucket.tickers.push({ ...r, allocatedValue })
        bucket.netValue += allocatedValue ?? 0
        bucket.grossValue += Math.abs(allocatedValue ?? 0)
      }
    }
    return [...byTheme.values()]
      .filter((t) => t.tickers.length > 0)
      .sort((a, b) => Math.abs(b.netValue) - Math.abs(a.netValue))
  }, [taxonomy, rows])

  const netPortfolioValue = useMemo(() => rows.reduce((s, r) => s + (r.value ?? 0), 0), [rows])
  // Reconstructed as the sum of every theme's own netValue, not
  // recomputed independently from `rows` -- this is exactly the
  // identity the Total row exists to demonstrate holds (see themeRows'
  // own comment: an even split per ticker makes every theme bucket a
  // true partition, so the two sums are mathematically guaranteed equal,
  // not just approximately so).
  const themesNetTotal = useMemo(() => themeRows.reduce((s, t) => s + t.netValue, 0), [themeRows])

  function toggleTheme(key) {
    setExpandedThemes((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <div className="positions-page positions-unbounded">
      <header className="masthead">
        <div className="title-block">
          <h1>Themes</h1>
        </div>
        <div className="stat-row">
          <div className="stat">
            <span className="n num">{rows.length}</span>
            <span className="l">positions</span>
          </div>
          <div className="stat">
            <span className="n num">{themeRows.length}</span>
            <span className="l">themes</span>
          </div>
          <div className="stat">
            <span className="n num">{fmtMoneyUnsigned(grossPortfolioValue || null)}</span>
            <span className="l">Gross Value</span>
          </div>
        </div>
      </header>

      {error && <div className="asset-card">Couldn't load theme data: {error}</div>}
      {!error && !taxonomy && <div className="asset-card">Loading…</div>}
      {!error && taxonomy && rows.length === 0 && (
        <div className="asset-card">No open positions — or ib_price_server.py isn't running / hasn't reported positions yet.</div>
      )}

      {!error && taxonomy && rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className="col-left col-name">Theme</th>
                <th># Holdings</th>
                <th>Net Exposure</th>
                <th>% of Net Portfolio</th>
              </tr>
            </thead>
            <tbody>
              {themeRows.map((theme) => {
                const open = expandedThemes.has(theme.key)
                return (
                  <Fragment key={theme.key}>
                    <tr className="factor-tree-row factor-tree-level-0" onClick={() => toggleTheme(theme.key)}>
                      <td className="col-left col-name" title={theme.description}>
                        <span className="factor-tree-toggle">
                          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                          {theme.label}
                        </span>
                      </td>
                      <td className="num">{theme.tickers.length}</td>
                      <td className={`num ${theme.netValue === 0 ? '' : theme.netValue >= 0 ? 'good' : 'bad'}`}>
                        {fmtMoney(theme.netValue)}
                      </td>
                      <td className="num">{fmtPct(netPortfolioValue ? theme.netValue / netPortfolioValue : null)}</td>
                    </tr>
                    {open &&
                      theme.tickers
                        .slice()
                        .sort((a, b) => Math.abs(b.allocatedValue ?? 0) - Math.abs(a.allocatedValue ?? 0))
                        .map((t) => (
                          <tr className="factor-tree-row factor-tree-level-1" key={t.ticker}>
                            <td className="col-left col-name">
                              <span className="factor-tree-leaf">
                                <a
                                  href={`#/asset/${encodeURIComponent(t.ticker)}`}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  className="ticker-link"
                                >
                                  {t.ticker}
                                </a>
                                <span className="factor-tree-leaf-name">{t.name}</span>
                              </span>
                            </td>
                            <td className="num">—</td>
                            <td
                              className={`num tooltip-cell ${(t.allocatedValue ?? 0) >= 0 ? 'good' : 'bad'}`}
                              data-tip={
                                t.themeKeys.length > 1
                                  ? `${fmtMoney(t.value)} total, split evenly across its ${t.themeKeys.length} themes`
                                  : undefined
                              }
                            >
                              {fmtMoney(t.allocatedValue)}
                            </td>
                            <td className="num">—</td>
                          </tr>
                        ))}
                  </Fragment>
                )
              })}
              <tr className="factor-tree-row factor-tree-level-0 themes-total-row">
                <td className="col-left col-name">Total</td>
                <td className="num">{rows.length}</td>
                <td className={`num ${themesNetTotal === 0 ? '' : themesNetTotal >= 0 ? 'good' : 'bad'}`}>
                  {fmtMoney(themesNetTotal)}
                </td>
                <td className="num">{fmtPct(netPortfolioValue ? themesNetTotal / netPortfolioValue : null)}</td>
              </tr>
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
