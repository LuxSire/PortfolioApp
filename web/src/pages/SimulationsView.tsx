import { useEffect, useMemo, useState } from 'react'
import { Search, X } from 'lucide-react'
import { getSectorIcon } from '../sectorIcons'
import { getSectorGroup } from '../sectorGroups'
import FilterDropdown from '../components/FilterDropdown'
import SectorFilter from '../components/SectorFilter'
import { fmtNum, fmtPrice } from '../screenerFactors'
import { IB_STREAM_URL } from '../ibStream'
import type { PositionsByTicker } from '../interfaces/IPositionsView'
import type { RawSimResult, SimRow } from '../interfaces/ISimulationsView'

const PAGE_SIZE = 100

// Columns shown, in order -- deliberately as many of simulations.json's
// own fields as make sense on one row (explicit instruction), spanning
// both scenarios (own multiple / industry-median multiple) plus the
// comparison and analyst-target cross-check. sortable defaults to true;
// only Name is excluded (free text, not a meaningful sort key here, same
// as ScreenerView's own Name column).
// Order: identity, then Price/Forecast Price side by side (explicit
// instruction), then the PE/EPS model inputs, then the headline diff/
// confidence-discount summary (the actual "is this attractive" numbers),
// then the full distributional detail for each scenario last, for anyone
// who wants to dig past the headline. No analyst-target columns (explicit
// instruction) -- analystTargets stays in simulations.json itself as a
// backend cross-check, just not surfaced on this page.
const COLUMNS: { key: keyof SimRow | 'position'; label: string; className?: string; sortable?: boolean }[] = [
  { key: 't', label: 'Ticker', className: 'col-left col-ticker' },
  { key: 'n', label: 'Name', className: 'col-left col-name', sortable: false },
  { key: 's', label: 'Industry', className: 'col-left' },
  { key: 'position', label: 'Position', sortable: false },
  { key: 'price', label: 'Price' },
  { key: 'forecastPrice', label: 'Forecast Price' },
  { key: 'forecastReturn', label: 'Forecast Return' },
  { key: 'curProbAbove', label: 'P(above)' },
  { key: 'ownPe', label: 'Own PE' },
  { key: 'industryPe', label: 'Industry Median PE' },

  { key: 'curMedian', label: 'Median @ Own PE' },
  { key: 'curReturn', label: 'Return @ Own PE' },
  { key: 'curProbAbove', label: 'P(Above) @ Own PE' },
  { key: 'indMedian', label: 'Median @ Blended PE' },
  { key: 'indReturn', label: 'Return @ Blended PE' },
  { key: 'indP5', label: 'P5 @ Blended PE' },
  { key: 'indP95', label: 'P95 @ Blended PE' },
  { key: 'indProbAbove', label: 'P(Above) @ Blended PE' },
]

function fmtShares(v: number | null): string {
  if (v === null || v === undefined) return '—'
  return v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

function fmtPct(v: number | null): string {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%'
}

// No sign -- a probability/vol magnitude, not a signed change (same
// reasoning screenerFactors.js's own fmtPct0 gives for epsVol there).
function fmtProb(v: number | null): string {
  if (v === null || v === undefined) return '—'
  return Math.round(v * 100) + '%'
}

// Green above 55%, red below 45%, neutral between -- a probability
// meaningfully off 50/50 either way, not just noise around the midpoint.
function probClass(v: number | null): string {
  if (v === null || v === undefined) return ''
  if (v >= 0.55) return 'perf-pos'
  if (v <= 0.45) return 'perf-neg'
  return ''
}

// Positive median-diff-% (industry multiple implies more upside than
// today's own multiple) is the page's core "attractive for a long"
// signal -- green/red on sign alone, same as ScreenerView's own
// epsTrend/sentiment/etc. perf-pos/perf-neg cells.
function signClass(v: number | null): string {
  if (v === null || v === undefined) return ''
  return v >= 0 ? 'perf-pos' : 'perf-neg'
}

function rankDescending(rows: SimRow[], key: keyof SimRow): Map<string, number> {
  const valid = rows.filter((r) => r[key] !== null && r[key] !== undefined)
  valid.sort((a, b) => (b[key] as number) - (a[key] as number))
  const map = new Map<string, number>()
  valid.forEach((r, i) => map.set(r.t, i + 1))
  return map
}

function Subrank({ rank }: { rank: number | null | undefined }) {
  if (rank === null || rank === undefined) return null
  return <span className="subrank">{rank}</span>
}

export default function SimulationsView() {
  const [raw, setRaw] = useState<RawSimResult[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [selectedIndustries, setSelectedIndustries] = useState<Set<string>>(new Set())
  const [selectedGroups, setSelectedGroups] = useState<Set<string>>(new Set())
  const [sortKey, setSortKey] = useState('forecastReturn')
  const [sortDir, setSortDir] = useState(-1)
  const [page, setPage] = useState(0)
  const [positions, setPositions] = useState<PositionsByTicker>({})
  const [nonZeroOnly, setNonZeroOnly] = useState(false)

  useEffect(() => {
    const source = new EventSource(IB_STREAM_URL)
    source.onmessage = (e) => {
      const { positions: pos } = JSON.parse(e.data)
      setPositions(pos)
    }
    source.onerror = () => {}
    return () => source.close()
  }, [])

  useEffect(() => {
    fetch('/simulations.json')
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
        return r.json()
      })
      .then(setRaw)
      .catch((e) => setError(e.message))
  }, [])

  // Error entries (missing forwardEps/price/forwardPE for that ticker --
  // see simulate_ticker) carry nothing to show, so they're dropped here
  // rather than rendered as an empty/dashed row.
  const rows: SimRow[] | null = useMemo(() => {
    if (!raw) return null
    const out: SimRow[] = []
    for (const r of raw) {
      if (r.error || !r.inputs || !r.priceAtCurrentMultiple) continue
      const cur = r.priceAtCurrentMultiple
      const ind = r.priceAtBlendedMultiple ?? null
      out.push({
        t: r.ticker,
        n: r.name || r.ticker,
        s: r.sector || 'Unclassified',
        price: r.currentPrice as number,
        forecastPrice: r.forecastPrice ?? null,
        forecastReturn: r.forecastReturn ?? null,
        muEps: r.inputs.muEps,
        sigmaEpsPct: r.inputs.muEps ? r.inputs.sigmaEps / Math.abs(r.inputs.muEps) : null,
        epsVolSource: r.inputs.epsVolatilitySource,
        ownPe: r.inputs.ownPe,
        industryPe: r.inputs.industryMedianPe,
        peerCount: r.inputs.peerCount,
        peLevel: r.inputs.peLevel,
        peRatio: r.comparison?.peMultipleRatio ?? null,
        epsTrend: r.inputs.epsTrend,
        revenueGrowth: r.inputs.revenueGrowth,
        confidence: r.inputs.confidence,
        curMedian: cur.median,
        curReturn: r.currentPrice ? cur.median / (r.currentPrice as number) - 1 : 0,
        curProbAbove: cur.probAboveCurrentPrice,
        indMedian: ind?.median ?? null,
        indReturn: (ind && r.currentPrice) ? ind.median / (r.currentPrice as number) - 1 : null,
        indP5: ind?.p5 ?? null,
        indP95: ind?.p95 ?? null,
        indProbAbove: ind?.probAboveCurrentPrice ?? null,
        medianDiff: r.comparison?.medianDiff ?? null,
        medianDiffPct: r.comparison?.medianDiffPct ?? null,
        discountedMedianDiff: r.comparison?.discountedMedianDiff ?? null,
        discountedMedianDiffPct: r.comparison?.discountedMedianDiffPct ?? null,
      })
    }
    return out
  }, [raw])

  // Fixed rank (best = 1) on the page's own "attractive for a long"
  // signal, independent of sort/filter -- same "doesn't move around"
  // convention ScreenerView's own subranks use. Tickers with no industry
  // comparison (forecastReturn null) never get a rank here. Explicit
  // instruction: ranked on forecastReturn (forecastPrice vs. current
  // price), not discountedMedianDiffPct -- that percentage is
  // mathematically invariant to mu_eps (see modules/simulations.py's own
  // docstring), so it never actually reflected the EPS projection.
  const diffPctRank = useMemo(
    () => (rows ? rankDescending(rows, 'forecastReturn') : new Map<string, number>()),
    [rows]
  )

  const industryCounts: [string, number][] = useMemo(() => {
    if (!rows) return []
    const counts = new Map<string, number>()
    for (const r of rows) counts.set(r.s, (counts.get(r.s) || 0) + 1)
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
  }, [rows])

  const filtered: SimRow[] = useMemo(() => {
    if (!rows) return []
    const q = search.trim().toLowerCase()
    return rows.filter((r) => {
      if (selectedIndustries.size && !selectedIndustries.has(r.s)) return false
      if (selectedGroups.size && !selectedGroups.has(getSectorGroup(r.s))) return false
      if (q && !r.t.toLowerCase().includes(q) && !r.n.toLowerCase().includes(q)) return false
      if (nonZeroOnly && !positions[r.t]?.shares) return false
      return true
    })
  }, [rows, search, selectedIndustries, selectedGroups, nonZeroOnly, positions])

  const sorted: SimRow[] = useMemo(() => {
    const copy = [...filtered]
    copy.sort((a, b) => {
      const av = a[sortKey as keyof SimRow]
      const bv = b[sortKey as keyof SimRow]
      if (typeof av === 'string') return av.localeCompare(bv as string) * sortDir
      const an = av === null || av === undefined
      const bn = bv === null || bv === undefined
      if (an && bn) return 0
      if (an) return 1
      if (bn) return -1
      return ((av as number) - (bv as number)) * sortDir
    })
    return copy
  }, [filtered, sortKey, sortDir])

  const filterKey = JSON.stringify([search, [...selectedIndustries], [...selectedGroups], nonZeroOnly, sortKey, sortDir])
  const [lastFilterKey, setLastFilterKey] = useState(filterKey)
  if (filterKey !== lastFilterKey) {
    setLastFilterKey(filterKey)
    setPage(0)
  }

  const pageCount = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE))
  const currentPage = Math.min(page, pageCount - 1)
  const paged = useMemo(() => sorted.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE), [sorted, currentPage])

  const withIndustryCount = useMemo(() => (rows ? rows.filter((r) => r.indMedian !== null).length : 0), [rows])

  function handleSort(key: string) {
    if (sortKey === key) {
      setSortDir((d) => -d)
    } else {
      setSortKey(key)
      setSortDir(-1)
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

  return (
    <>
      <header className="masthead">
        <div className="title-block">
          <h1>Simulations</h1>
        </div>
        <div className="stat-row">
          <div className="stat">
            <span className="n num">{rows ? sorted.length : '—'}</span>
            <span className="l">shown</span>
          </div>
          <div className="stat">
            <span className="n num">{rows ? rows.length : '—'}</span>
            <span className="l">simulated</span>
          </div>
          <div className="stat">
            <span className="n num">{withIndustryCount}</span>
            <span className="l">with industry comparison</span>
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

        {rows && <SectorFilter industries={rows.map((r) => r.s)} selected={selectedGroups} onChange={setSelectedGroups} />}

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
        </div>

        {rows && (
          <div className="result-count">
            {sorted.length} of {rows.length}
          </div>
        )}
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
                <td colSpan={COLUMNS.length}>Couldn't load simulations.json: {error}</td>
              </tr>
            )}
            {!error && !rows && (
              <tr className="status-row">
                <td colSpan={COLUMNS.length}>Loading…</td>
              </tr>
            )}
            {!error && rows && sorted.length === 0 && (
              <tr className="empty-row">
                <td colSpan={COLUMNS.length}>No simulated tickers match the current filters.</td>
              </tr>
            )}
            {!error &&
              rows &&
              paged.map((r) => {
                const Icon = getSectorIcon(r.s)
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
                    </td>
                    <td className="col-left col-name" title={r.n}>
                      {r.n}
                    </td>
                    <td className="col-left">
                      <span className="sector-cell" title={r.s}>
                        <Icon />
                        <span className="sector-cell-name">{r.s}</span>
                      </span>
                    </td>
                    <td className="num">{fmtShares(positions[r.t]?.shares ?? null)}</td>
                    <td className="num">{fmtPrice(r.price)}</td>
                    <td className={`num ${signClass(r.forecastPrice !== null ? r.forecastPrice - r.price : null)}`}>
                      {fmtPrice(r.forecastPrice)}
                    </td>
                    <td className={`num ${signClass(r.forecastReturn)}`}>
                      {fmtPct(r.forecastReturn)} <Subrank rank={diffPctRank.get(r.t)} />
                    </td>
                    {(() => {
                      const pa =
                        r.curProbAbove !== null && r.indProbAbove !== null
                          ? (r.curProbAbove + r.indProbAbove) / 2
                          : r.curProbAbove
                      return <td className={`num ${probClass(pa)}`}>{fmtProb(pa)}</td>
                    })()}
                    <td className="num">{fmtNum(r.ownPe)}</td>
                    <td
                      className="num"
                      title={
                        r.peLevel
                          ? `Median across ${r.peerCount} ${r.peLevel === 'industry' ? 'same-industry' : 'same-sector (industry had too few)'} peers`
                          : undefined
                      }
                    >
                      {fmtNum(r.industryPe)}
                    </td>
                    <td className="num">{fmtPrice(r.curMedian)}</td>
                    <td className={`num ${signClass(r.curReturn)}`}>{fmtPct(r.curReturn)}</td>
                    <td className={`num ${probClass(r.curProbAbove)}`}>{fmtProb(r.curProbAbove)}</td>
                    <td className="num">{fmtPrice(r.indMedian)}</td>
                    <td className={`num ${signClass(r.indReturn)}`}>{fmtPct(r.indReturn)}</td>
                    <td className="num">{fmtPrice(r.indP5)}</td>
                    <td className="num">{fmtPrice(r.indP95)}</td>
                    <td className={`num ${probClass(r.indProbAbove)}`}>{fmtProb(r.indProbAbove)}</td>
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
