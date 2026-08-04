import { useEffect, useMemo, useRef, useState } from 'react'
import { Search, ChevronDown, X } from 'lucide-react'
import { inverseRangeClass, inversePctThresholdClass, rangeClass } from './colorRules'
import { parseCSV } from './csv'
import { earningsUrgencyClass, fmtEarningsDate } from './earnings'
import { getSectorIcon } from './sectorIcons'
import { getSectorGroup } from './sectorGroups'
import { IB_STREAM_URL } from './ibStream'

const LIVE_PRICE_TOP_N = 99
const PAGE_SIZE = 100

const COLUMNS = [
  { key: 't', label: 'Ticker', className: 'col-left col-ticker' },
  { key: 'n', label: 'Name', className: 'col-left col-name', sortable: false },
  { key: 's', label: 'Industry', className: 'col-left' },
  { key: 'possize', label: 'Position', fmt: 'num0', sortable: false },
  { key: 'posval', label: 'Pos Value', fmt: 'money', sortable: false },
  { key: 'savgpe', label: 'Avg PE', fmt: 'num2' },
  { key: 'fpe', label: 'Fwd PE', fmt: 'num2' },
  { key: 'feps', label: 'Fwd EPS', fmt: 'price' },
  { key: 'tpe', label: 'Trail PE', fmt: 'num2' },
  { key: 'peg', label: 'PEG', fmt: 'num2' },
  { key: 'revg', label: 'Rev Growth', fmt: 'pct' },
  { key: 'pfcf', label: 'P/FCF', fmt: 'num2' },
  { key: 'de', label: 'D/E', fmt: 'num2' },
  { key: 'liq', label: 'Liq Ratio', fmt: 'num2' },
  { key: 'p', label: 'Price', fmt: 'price' },
  { key: 'tgt', label: 'Target', fmt: 'price' },
  { key: 'upside', label: 'Upside', fmt: 'pct' },
  { key: 'rec', label: 'Rec', className: 'col-rec' },
  { key: 'mom', label: 'Momentum', fmt: 'signed' },
  { key: 'sent', label: 'Sentiment', fmt: 'num2' },
  { key: 'sc', label: 'Score', fmt: 'score' },
  { key: 'rank', label: '#' },
  { key: 'upd', label: 'Updated', className: 'col-left' },
]

function toNum(v) {
  if (v === undefined || v === null || v === '') return null
  const n = Number(v)
  return Number.isNaN(n) ? null : n
}

function fmtNum(v) {
  if (v === null) return '—'
  if (!Number.isFinite(v)) return v > 0 ? '∞' : '−∞'
  return v.toFixed(1)
}

function fmtPct(v) {
  if (v === null) return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%'
}

// Momentum is a Sharpe-style risk-adjusted ratio (trend slope divided by
// its own volatility — see IBApp._regression_momentum), not a return, so
// it doesn't get fmtPct's *100/'%' treatment; a signed 2-decimal number
// reads as "units of risk-adjusted trend strength" instead of implying a
// percentage return that isn't what this value means.
function fmtSigned(v) {
  if (v === null) return '—'
  if (!Number.isFinite(v)) return v > 0 ? '∞' : '−∞'
  return (v >= 0 ? '+' : '') + v.toFixed(2)
}

// debtToEquity comes from yfinance already in percentage units (150.5 means
// 150.5%), unlike revg/upside/perf which are fractions — no *100 here.
function fmtDebtToEquity(v) {
  if (v === null) return '—'
  return v.toFixed(1) + '%'
}

function fmtPrice(v) {
  if (v === null) return '—'
  return '$' + v.toFixed(1)
}

// Share counts are whole numbers for the vast majority of IBKR positions;
// only show decimals for the rare fractional-share holding.
function fmtShares(v) {
  if (v === null) return '—'
  return v.toLocaleString(undefined, { maximumFractionDigits: Number.isInteger(v) ? 0 : 2 })
}

function fmtMoney(v) {
  if (v === null) return '—'
  return '$' + v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

// lastDownload is stored as a full ISO timestamp (needed to check "within
// the last 12h" server-side); the table only needs the date portion.
function fmtDate(v) {
  if (!v) return '—'
  return v.slice(0, 10)
}

function fmtScore(v) {
  if (v === null) return '—'
  return v.toFixed(3)
}

const REC_LABELS = {
  strong_buy: 'Strong Buy',
  buy: 'Buy',
  hold: 'Hold',
  underperform: 'Underperform',
  sell: 'Sell',
  none: 'N/A',
}

// strong_buy green, buy light green, hold/none/unknown neutral white,
// underperform yellow, sell red.
const REC_CLASSES = {
  strong_buy: 'rec-strong-buy',
  buy: 'rec-buy',
  underperform: 'rec-underperform',
  sell: 'rec-sell',
}

function recLabel(key) {
  if (!key) return '—'
  return REC_LABELS[key] || key
}

function recClass(key) {
  return REC_CLASSES[key] || 'rec-neutral'
}

// Name cell's title is the tooltip that already shows the full company
// name when the column truncates it; fold the earnings date in as
// human-readable text rather than leaving readers to decode the color tier
// (or the raw epoch seconds backing it) on their own.
function nameTooltip(r) {
  if (r.ern === null) return r.n
  return `${r.n} — earnings ${fmtEarningsDate(r.ern)}`
}

function targetTooltip(r) {
  if (r.tgtLow === null && r.tgtHigh === null && r.numAnalysts === null) return undefined
  const parts = []
  if (r.tgtLow !== null || r.tgtHigh !== null) {
    parts.push(`Range ${fmtPrice(r.tgtLow)} – ${fmtPrice(r.tgtHigh)}`)
  }
  if (r.numAnalysts !== null) {
    parts.push(`${r.numAnalysts} analyst${r.numAnalysts === 1 ? '' : 's'}`)
  }
  return parts.join(' · ')
}

// StockTwits sentiment score: (bullish - bearish) / tagged messages, in
// [-1, 1]. Shown with an explicit sign since 0 is a meaningful midpoint
// (evenly split), not "no signal" — that's what '—' is for.
function fmtSentiment(v) {
  if (v === null) return '—'
  return (v >= 0 ? '+' : '') + v.toFixed(2)
}

function sentimentTooltip(r) {
  if (r.sentBullish === null && r.sentBearish === null) return undefined
  const parts = [`${r.sentBullish ?? 0} bullish · ${r.sentBearish ?? 0} bearish`]
  if (r.sentTotal !== null) parts.push(`${r.sentTotal} recent messages`)
  return parts.join(' · ')
}

function Subrank({ rank }) {
  if (rank === null || rank === undefined) return null
  return <span className="subrank">{rank}</span>
}

// 1-indexed rank per ticker (best = 1); rows failing filterFn or missing a
// value are left out of the map entirely (no subrank shown for them).
function rankAscending(rows, key, filterFn) {
  const valid = rows.filter((r) => r[key] !== null && (!filterFn || filterFn(r[key])))
  valid.sort((a, b) => a[key] - b[key])
  const map = new Map()
  valid.forEach((r, i) => map.set(r.t, i + 1))
  return map
}

function rankDescending(rows, key) {
  const valid = rows.filter((r) => r[key] !== null)
  valid.sort((a, b) => b[key] - a[key])
  const map = new Map()
  valid.forEach((r, i) => map.set(r.t, i + 1))
  return map
}

function useOutsideClick(ref, onOutside) {
  useEffect(() => {
    function handler(e) {
      if (ref.current && !ref.current.contains(e.target)) onOutside()
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [ref, onOutside])
}

function FilterDropdown({ noun, plural, items, selected, onToggle, onClear, getIcon }) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const wrapRef = useRef(null)
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

export default function PeTable() {
  const [rawRows, setRawRows] = useState(null)
  const [error, setError] = useState(null)
  const [search, setSearch] = useState('')
  const [selectedIndustries, setSelectedIndustries] = useState(new Set())
  const [selectedGroups, setSelectedGroups] = useState(new Set())
  const [sortKey, setSortKey] = useState('sc')
  const [sortDir, setSortDir] = useState(1)
  const [livePrices, setLivePrices] = useState({})
  const [positions, setPositions] = useState({})
  const [positiveOnly, setPositiveOnly] = useState(false)
  const [page, setPage] = useState(0)

  // ib_price_server.py — a separate always-running local process (not part
  // of this fetch-once pipeline) that pushes IB Gateway last-price ticks
  // (top LIVE_PRICE_TOP_N tickers of the ranking) and account positions
  // (any ticker) over Server-Sent Events as they change — no polling, no
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
    Promise.all([
      fetch('/sorted_screen.csv').then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
        return r.text()
      }),
      // Only covers the top SENTIMENT_TOP_N tickers of a past run (see
      // social_sentiment.py) and may not exist yet — missing/failed fetch
      // just means every row's sentiment is blank, not a load error.
      fetch('/social_sentiment.json')
        .then((r) => (r.ok ? r.json() : {}))
        .catch(() => ({})),
    ])
      .then(([text, sentiment]) => {
        // sorted_screen.csv is already ranked best-to-worst by score; capture
        // that as a fixed rank so it doesn't shift when the table is
        // re-sorted or filtered.
        const parsed = parseCSV(text).map((r, idx) => ({
          rank: idx + 1,
          t: r.ticker,
          n: r.name,
          s: r.sector || 'Unclassified',
          sent: toNum(sentiment[r.ticker]?.score),
          sentBullish: toNum(sentiment[r.ticker]?.bullish),
          sentBearish: toNum(sentiment[r.ticker]?.bearish),
          sentTotal: toNum(sentiment[r.ticker]?.total),
          fpe: toNum(r.forwardPE),
          feps: toNum(r.forwardEps),
          tpe: toNum(r.trailingPE),
          peg: toNum(r.pegRatio),
          revg: toNum(r.revenueGrowth),
          pfcf: toNum(r.priceToFCF),
          de: toNum(r.debtToEquity),
          liq: toNum(r.LiqRatio),
          p: toNum(r.price),
          tgt: toNum(r.targetMeanPrice),
          tgtHigh: toNum(r.targetHighPrice),
          tgtLow: toNum(r.targetLowPrice),
          numAnalysts: toNum(r.numberOfAnalystOpinions),
          upside: toNum(r.targetUpside),
          rec: r.recommendationKey || null,
          mom: toNum(r.momentum),
          sc: toNum(r.score),
          ern: toNum(r.earningsTimestampStart),
          upd: r.lastDownload || null,
        }))
        setRawRows(parsed)
      })
      .catch((e) => setError(e.message))
  }, [])

  // Sector's average forward P/E, computed once across the full (unfiltered)
  // list — the same figure main.py uses for the sector-relative score factor.
  const sectorAvgPE = useMemo(() => {
    if (!rawRows) return new Map()
    const sums = new Map()
    const counts = new Map()
    for (const r of rawRows) {
      if (r.s && r.fpe !== null) {
        sums.set(r.s, (sums.get(r.s) || 0) + r.fpe)
        counts.set(r.s, (counts.get(r.s) || 0) + 1)
      }
    }
    const avg = new Map()
    for (const [s, sum] of sums) avg.set(s, sum / counts.get(s))
    return avg
  }, [rawRows])

  // Per-metric subranks (best = 1), fixed across the full unfiltered list —
  // same "doesn't move when you filter" convention as the # column. Negative
  // priceToFCF/pegRatio/debtToEquity are excluded rather than ranked as
  // "cheap"/"low debt", matching how main.py's scoring treats them.
  const fpeRank = useMemo(() => (rawRows ? rankAscending(rawRows, 'fpe') : new Map()), [rawRows])
  const pegRank = useMemo(() => (rawRows ? rankAscending(rawRows, 'peg', (v) => v > 0) : new Map()), [rawRows])
  const pfcfRank = useMemo(() => (rawRows ? rankAscending(rawRows, 'pfcf', (v) => v > 0) : new Map()), [rawRows])
  const deRank = useMemo(() => (rawRows ? rankAscending(rawRows, 'de', (v) => v >= 0) : new Map()), [rawRows])
  const momRank = useMemo(() => (rawRows ? rankDescending(rawRows, 'mom') : new Map()), [rawRows])
  const upsideRank = useMemo(() => (rawRows ? rankDescending(rawRows, 'upside') : new Map()), [rawRows])
  const revgRank = useMemo(() => (rawRows ? rankDescending(rawRows, 'revg') : new Map()), [rawRows])
  const liqRank = useMemo(() => (rawRows ? rankDescending(rawRows, 'liq') : new Map()), [rawRows])
  const sentRank = useMemo(() => (rawRows ? rankDescending(rawRows, 'sent') : new Map()), [rawRows])

  // Rank on forwardPE - trailingPE (more negative = better), same factor
  // main.py's score weights at 10%. Infinite or negative trailingPE (no real
  // earnings signal) is treated as 200 for this calculation only — the table
  // still displays the real trailingPE value.
  const diffRank = useMemo(() => {
    if (!rawRows) return new Map()
    const valid = rawRows
      .map((r) => {
        if (r.fpe === null || r.tpe === null) return { t: r.t, diff: null }
        const effectiveTpe = !Number.isFinite(r.tpe) || r.tpe < 0 ? 200 : r.tpe
        return { t: r.t, diff: r.fpe - effectiveTpe }
      })
      .filter((r) => r.diff !== null)
    valid.sort((a, b) => a.diff - b.diff)
    const map = new Map()
    valid.forEach((r, i) => map.set(r.t, i + 1))
    return map
  }, [rawRows])

  const rows = useMemo(() => {
    if (!rawRows) return null
    return rawRows.map((r) => ({
      ...r,
      savgpe: sectorAvgPE.get(r.s) ?? null,
      fpeRank: fpeRank.get(r.t) ?? null,
      pegRank: pegRank.get(r.t) ?? null,
      pfcfRank: pfcfRank.get(r.t) ?? null,
      deRank: deRank.get(r.t) ?? null,
      momRank: momRank.get(r.t) ?? null,
      upsideRank: upsideRank.get(r.t) ?? null,
      revgRank: revgRank.get(r.t) ?? null,
      diffRank: diffRank.get(r.t) ?? null,
      liqRank: liqRank.get(r.t) ?? null,
      sentRank: sentRank.get(r.t) ?? null,
    }))
  }, [
    rawRows,
    sectorAvgPE,
    fpeRank,
    pegRank,
    pfcfRank,
    deRank,
    momRank,
    upsideRank,
    revgRank,
    diffRank,
    liqRank,
    sentRank,
  ])

  const industryCounts = useMemo(() => {
    if (!rows) return []
    const counts = new Map()
    for (const r of rows) counts.set(r.s, (counts.get(r.s) || 0) + 1)
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
  }, [rows])

  // Broad sector groupings (Technology, Healthcare, etc.) derived from the
  // granular industry — a coarser filter dimension layered on top; every
  // other computation (scoring, subranks, industry avg PE) stays industry-based.
  const groupCounts = useMemo(() => {
    if (!rows) return []
    const counts = new Map()
    for (const r of rows) {
      const g = getSectorGroup(r.s)
      counts.set(g, (counts.get(g) || 0) + 1)
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
  }, [rows])

  const scoreRange = useMemo(() => {
    if (!rows) return [0, 1]
    const scores = rows.map((r) => r.sc).filter((v) => v !== null)
    return [Math.min(...scores), Math.max(...scores)]
  }, [rows])

  const filtered = useMemo(() => {
    if (!rows) return []
    const q = search.trim().toLowerCase()
    return rows.filter((r) => {
      if (selectedIndustries.size && !selectedIndustries.has(r.s)) return false
      if (selectedGroups.size && !selectedGroups.has(getSectorGroup(r.s))) return false
      if (q && !r.t.toLowerCase().includes(q) && !r.n.toLowerCase().includes(q)) return false
      // A short position (negative shares) or no position at all (undefined)
      // both fail this — "positive" means a real long holding, not just
      // "any position record exists".
      if (positiveOnly && !(positions[r.t]?.shares > 0)) return false
      return true
    })
  }, [rows, search, selectedIndustries, selectedGroups, positiveOnly, positions])

  const sorted = useMemo(() => {
    const copy = [...filtered]
    copy.sort((a, b) => {
      const av = a[sortKey]
      const bv = b[sortKey]
      if (typeof av === 'string') return av.localeCompare(bv) * sortDir
      const an = av === null
      const bn = bv === null
      if (an && bn) return 0
      if (an) return 1
      if (bn) return -1
      return (av - bv) * sortDir
    })
    return copy
  }, [filtered, sortKey, sortDir])

  // Reset to the first page whenever the result set itself changes shape —
  // search/filter/sort — so page 3 of an old, larger result set doesn't
  // silently show page 3 of a much smaller new one (or nothing at all).
  useEffect(() => {
    setPage(0)
  }, [search, selectedIndustries, selectedGroups, positiveOnly, sortKey, sortDir])

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
    if (!sorted.length) return { avgScore: null, avgMom: null }
    const avgScore = sorted.reduce((s, r) => s + (r.sc ?? 0), 0) / sorted.length
    const momVals = sorted.filter((r) => r.mom !== null)
    const avgMom = momVals.length ? momVals.reduce((s, r) => s + r.mom, 0) / momVals.length : null
    return { avgScore, avgMom }
  }, [sorted])

  function handleSort(key) {
    if (sortKey === key) {
      setSortDir((d) => -d)
    } else {
      setSortKey(key)
      setSortDir(1)
    }
  }

  function toggleIndustry(name) {
    setSelectedIndustries((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  function toggleGroup(name) {
    setSelectedGroups((prev) => {
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
          <h1>Stock Screener</h1>
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
            <span className="n num">{stats.avgMom === null ? '—' : fmtSigned(stats.avgMom)}</span>
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

        <label className="position-filter">
          <input type="checkbox" checked={positiveOnly} onChange={(e) => setPositiveOnly(e.target.checked)} />
          Position &gt; 0
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
        </div>

        {rows && <div className="result-count">{sorted.length} of {rows.length}</div>}
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              {COLUMNS.map((col) => {
                const sortable = col.sortable !== false
                const active = sortKey === col.key
                return (
                  <th
                    key={col.key}
                    className={col.className || ''}
                    tabIndex={sortable ? 0 : -1}
                    onClick={sortable ? () => handleSort(col.key) : undefined}
                    onKeyDown={
                      sortable
                        ? (e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault()
                              handleSort(col.key)
                            }
                          }
                        : undefined
                    }
                  >
                    {col.label}
                    {sortable && active && <span className="arrow">{sortDir === 1 ? '▲' : '▼'}</span>}
                  </th>
                )
              })}
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
              const sentClass = r.sent === null ? '' : r.sent >= 0 ? 'perf-pos' : 'perf-neg'
              const upsideClass = r.upside === null ? '' : r.upside >= 0 ? 'perf-pos' : 'perf-neg'
              // Green only above 10% growth, red below 0%, neutral between.
              const revgClass = inversePctThresholdClass(r.revg, 0, 10)
              // Same thresholds as the asset page: PEG < 1 cheap relative to
              // growth, > 1 pricey; Liq Ratio > 3 comfortably liquid, < 1 weak;
              // P/FCF, Trailing PE, Fwd PE cheap under 10x, expensive above 50x.
              const pegClass = rangeClass(r.peg, 1, 1)
              const liqClass = inverseRangeClass(r.liq, 1, 3)
              const pfcfClass = rangeClass(r.pfcf, 10, 50)
              const tpeClass = rangeClass(r.tpe, 10, 50)
              const fpeClass = rangeClass(r.fpe, 10, 50)
              const earningsClass = earningsUrgencyClass(r.ern, r.upd)
              const live = r.rank <= LIVE_PRICE_TOP_N ? livePrices[r.t] : undefined
              const liveRatio = live?.last != null && r.p ? live.last / r.p - 1 : null
              const liveClass =
                liveRatio === null
                  ? ''
                  : Math.abs(liveRatio) <= 0.005
                    ? 'perf-neutral'
                    : liveRatio >= 0
                      ? 'perf-pos'
                      : 'perf-neg'
              // Positions come from ib_price_server.py and can exist for any
              // ticker (not just the top LIVE_PRICE_TOP_N), so they're read
              // independent of `live` — value uses the live price when this
              // row has one (same price shown in the Price column), else
              // falls back to the sorted_screen.csv price.
              const possize = positions[r.t]?.shares ?? null
              const posPrice = live?.last ?? r.p
              const posval = possize !== null && posPrice !== null ? possize * posPrice : null
              const scorePct = r.sc === null ? 0 : ((r.sc - scoreMin) / scoreSpan) * 100
              return (
                <tr key={r.t}>
                  <td className="col-left col-ticker num">
                    <a href={`#/asset/${encodeURIComponent(r.t)}`} className="ticker-link">{r.t}</a>
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
                  <td className="num">{fmtNum(r.savgpe)}</td>
                  <td className={`num ${fpeClass}`}>{fmtNum(r.fpe)} <Subrank rank={r.fpeRank} /></td>
                  <td className="num">{fmtPrice(r.feps)}</td>
                  <td className={`num ${tpeClass}`}>{fmtNum(r.tpe)} <Subrank rank={r.diffRank} /></td>
                  <td className={`num ${pegClass}`}>{fmtNum(r.peg)} <Subrank rank={r.pegRank} /></td>
                  <td className={`num ${revgClass}`}>{fmtPct(r.revg)} <Subrank rank={r.revgRank} /></td>
                  <td className={`num ${pfcfClass}`}>{fmtNum(r.pfcf)} <Subrank rank={r.pfcfRank} /></td>
                  <td className="num">{fmtDebtToEquity(r.de)} <Subrank rank={r.deRank} /></td>
                  <td className={`num ${liqClass}`}>{fmtNum(r.liq)} <Subrank rank={r.liqRank} /></td>
                  <td className="num price-cell">
                    <span className="price-value">{live?.last != null ? fmtPrice(live.last) : fmtPrice(r.p)}</span>
                    {liveRatio !== null && (
                      <span
                        className={`live-price ${liveClass}`}
                        title={`IB Gateway ${fmtPrice(live.last)} at ${live.timestamp} vs. yfinance ${fmtPrice(r.p)}`}
                      >
                        {fmtPct(liveRatio)}
                      </span>
                    )}
                  </td>
                  <td className="num tooltip-cell" data-tip={targetTooltip(r)}>{fmtPrice(r.tgt)}</td>
                  <td className={`num ${upsideClass}`}>{fmtPct(r.upside)} <Subrank rank={r.upsideRank} /></td>
                  <td className="col-rec">
                    <span className={`rec-badge ${recClass(r.rec)}`}>{recLabel(r.rec)}</span>
                  </td>
                  <td className={`num ${momClass}`}>{fmtSigned(r.mom)} <Subrank rank={r.momRank} /></td>
                  <td className={`num tooltip-cell ${sentClass}`} data-tip={sentimentTooltip(r)}>
                    {fmtSentiment(r.sent)} <Subrank rank={r.sentRank} />
                  </td>
                  <td className="num">
                    <div className="score-cell">
                      <span className="score-bar"><span style={{ width: `${(100 - scorePct).toFixed(1)}%` }} /></span>
                      {fmtScore(r.sc)}
                    </div>
                  </td>
                  <td className="num rank-cell">{r.rank}</td>
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
