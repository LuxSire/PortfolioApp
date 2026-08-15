import { useEffect, useMemo, useState } from 'react'
import { Search, X } from 'lucide-react'
import type { Holding, HoldersByTicker, Institution, RawDataByTicker } from '../interfaces/IHoldersView'

const HOLDINGS_PREVIEW_COUNT = 10
const HOLDINGS_PER_MODAL_PAGE = 25
const INSTITUTIONS_PER_PAGE = 24

function fmtShares(v: number): string {
  return v.toLocaleString()
}

function fmtPct(v: number | null): string {
  if (v === null) return '—'
  return (v * 100).toFixed(2) + '%'
}

// Shared by the card preview and the full-list modal -- same three
// columns in both (Ticker/Shares/% Owned; no dollar Value column,
// explicit instruction).
function HoldingsTable({ holdings }: { holdings: Holding[] }) {
  return (
    <table>
      <thead>
        <tr>
          <th className="col-left">Ticker</th>
          <th>Shares</th>
          <th>% Owned</th>
        </tr>
      </thead>
      <tbody>
        {holdings.map((h) => (
          <tr key={h.ticker}>
            <td className="col-left">
              <a
                href={`#/asset/${encodeURIComponent(h.ticker)}`}
                target="_blank"
                rel="noopener noreferrer"
                className="ticker-link"
                title={h.name || undefined}
                onClick={(e) => e.stopPropagation()}
              >
                {h.ticker}
              </a>
            </td>
            <td className="num">{fmtShares(h.shares)}</td>
            <td className="num">{fmtPct(h.pctOwned)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// Full holdings list for one institution -- explicit instruction: clicking
// a card opens this, with pagination (a top-conviction institution can
// hold 400+ of the tracked tickers -- see GEODE CAPITAL/BlackRock in
// practice -- far more than fits one screen).
function HolderModal({ institution, onClose }: { institution: Institution; onClose: () => void }) {
  const [page, setPage] = useState(0)
  const pageCount = Math.max(1, Math.ceil(institution.holdings.length / HOLDINGS_PER_MODAL_PAGE))
  const currentPage = Math.min(page, pageCount - 1)
  const paged = institution.holdings.slice(
    currentPage * HOLDINGS_PER_MODAL_PAGE,
    (currentPage + 1) * HOLDINGS_PER_MODAL_PAGE
  )

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-panel" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>{institution.name}</h2>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </div>
        <div className="modal-body">
          <HoldingsTable holdings={paged} />
        </div>
        {pageCount > 1 && (
          <div className="pagination">
            <button type="button" onClick={() => setPage(currentPage - 1)} disabled={currentPage === 0}>
              Prev
            </button>
            <span className="pagination-info">
              Page {currentPage + 1} of {pageCount} ({institution.holdings.length} holdings)
            </span>
            <button type="button" onClick={() => setPage(currentPage + 1)} disabled={currentPage >= pageCount - 1}>
              Next
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

// One institution's top HOLDINGS_PREVIEW_COUNT positions, already sorted
// by pctOwned descending -- "largest positions" means highest ownership
// concentration, not highest dollar value (an 8%-owned small-cap is a
// bigger "position" in that sense than a mega-cap stake that's 0.01% of
// the company) -- see buildInstitutionCards below for the ranking itself.
// Click anywhere on the card to open HolderModal with the complete list.
function HolderCard({ institution, onOpen }: { institution: Institution; onOpen: (institution: Institution) => void }) {
  const preview = institution.holdings.slice(0, HOLDINGS_PREVIEW_COUNT)
  return (
    <div className="asset-card recommendation-card holder-card" onClick={() => onOpen(institution)}>
      <div className="recommendation-card-header">
        <div>
          <span className="recommendation-ticker">{institution.name}</span>
        </div>
        <div className="recommendation-badges">
          <span className="rec-badge rec-neutral">
            {institution.holdings.length} holding{institution.holdings.length === 1 ? '' : 's'}
          </span>
        </div>
      </div>
      <HoldingsTable holdings={preview} />
      {institution.holdings.length > preview.length && (
        <div className="dataset-notes">+ {institution.holdings.length - preview.length} more — click to view all</div>
      )}
    </div>
  )
}

// Inverts data/sec/13f/institutional_holders.json (per-ticker: [{name,
// valueUsd, shares}], see sec_edgar.py's fetch_13f_holdings) into
// per-institution: every tracked ticker that institution holds, each
// position's pctOwned computed against that ticker's own sharesOutstanding
// (raw_data.json, yfinance) -- same ownership-estimate caveat AssetView.tsx's
// own HoldersPanel carries: 13F filings are as of the prior quarter's
// report date, not today's actual share count. Returns each institution's
// FULL holdings list (not capped) -- HolderCard/HolderModal each decide
// their own slice (preview vs. paginated-complete) from this same array,
// so there's one source of truth for "everything this institution holds
// among tracked tickers," not two different queries.
//
// Important ceiling this whole page inherits from institutional_holders.json:
// that file only keeps the top MAX_HOLDERS_PER_TICKER (15) holders BY
// VALUE per ticker, not each institution's complete position list -- an
// institution's card here can only ever show tickers where it ranked in
// that ticker's own top 15, so a fund with many small, spread-out
// positions will show up thin here even if it's large in reality.
function buildInstitutionCards(holdersByTicker: HoldersByTicker, rawDataByTicker: RawDataByTicker): Institution[] {
  // institution -> ticker -> holding: an inner map, not an array, because
  // the SAME institution name can appear more than once for the SAME
  // ticker in the source data -- distinct 13F accessions (e.g. separate
  // sub-funds) that happen to file under an identical FILINGMANAGER_NAME
  // (confirmed live: "DANSKE BANK A/S" filed 3 separate accessions for
  // EXLS). Merging them here (sum shares/valueUsd) is what keeps a card's
  // per-ticker rows unique -- pushing them as separate array entries
  // produced two "EXLS" rows in the same card, a real duplicate-React-key
  // bug caught while testing this page.
  const byInstitution = new Map<string, Map<string, Holding>>()
  for (const [ticker, holders] of Object.entries(holdersByTicker)) {
    const info = rawDataByTicker[ticker]
    const sharesOutstanding = typeof info?.sharesOutstanding === 'number' ? info.sharesOutstanding : null
    const name = info?.shortName || info?.longName || null
    for (const h of holders) {
      if (!byInstitution.has(h.name)) byInstitution.set(h.name, new Map())
      const byTicker = byInstitution.get(h.name)!
      const existing = byTicker.get(ticker)
      if (existing) {
        existing.shares += h.shares
        existing.valueUsd += h.valueUsd
        existing.pctOwned = sharesOutstanding ? existing.shares / sharesOutstanding : null
      } else {
        byTicker.set(ticker, {
          ticker,
          name,
          shares: h.shares,
          valueUsd: h.valueUsd,
          pctOwned: sharesOutstanding ? h.shares / sharesOutstanding : null,
        })
      }
    }
  }

  const cards: Institution[] = []
  for (const [institution, byTicker] of byInstitution.entries()) {
    const holdings = [...byTicker.values()]
    // Unknown pctOwned (no sharesOutstanding) sorts last, not first --
    // treated as "smallest", not "biggest unknown".
    holdings.sort((a, b) => (b.pctOwned ?? -1) - (a.pctOwned ?? -1))
    cards.push({ name: institution, holdings })
  }
  // Most tracked-universe coverage first by default -- the search box
  // below is how you jump straight to a specific institution regardless
  // of this ordering.
  cards.sort((a, b) => b.holdings.length - a.holdings.length || a.name.localeCompare(b.name))
  return cards
}

// Institutional 13F holders, one card per institution, cross-referencing
// data/sec/13f/institutional_holders.json (which tickers, per-ticker top
// holders) against raw_data.json (each ticker's sharesOutstanding, to turn
// a raw share count into an ownership %) -- see buildInstitutionCards.
export default function HoldersView() {
  const [holdersByTicker, setHoldersByTicker] = useState<HoldersByTicker | null>(null)
  const [rawDataByTicker, setRawDataByTicker] = useState<RawDataByTicker | null>(null)
  const [error, setError] = useState(false)
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(0)
  const [openInstitution, setOpenInstitution] = useState<Institution | null>(null)

  useEffect(() => {
    fetch('/sec/13f/institutional_holders.json')
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then(setHoldersByTicker)
      .catch(() => setError(true))
    fetch('/raw_data.json')
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then(setRawDataByTicker)
      .catch(() => setError(true))
  }, [])

  const cards = useMemo(() => {
    if (!holdersByTicker || !rawDataByTicker) return null
    return buildInstitutionCards(holdersByTicker, rawDataByTicker)
  }, [holdersByTicker, rawDataByTicker])

  const filtered = useMemo(() => {
    if (!cards) return []
    const q = search.trim().toUpperCase()
    return q ? cards.filter((c) => c.name.toUpperCase().includes(q)) : cards
  }, [cards, search])

  const pageCount = Math.max(1, Math.ceil(filtered.length / INSTITUTIONS_PER_PAGE))
  const currentPage = Math.min(page, pageCount - 1)
  const paged = filtered.slice(currentPage * INSTITUTIONS_PER_PAGE, (currentPage + 1) * INSTITUTIONS_PER_PAGE)

  return (
    <div className="positions-page positions-unbounded">
      <header className="masthead">
        <div className="title-block">
          <h1>Holders</h1>
        </div>
        <div className="stat-row">
          <div className="stat">
            <span className="n num">{cards ? cards.length : '—'}</span>
            <span className="l">institutions</span>
          </div>
          {search && (
            <div className="stat">
              <span className="n num">{filtered.length}</span>
              <span className="l">matching</span>
            </div>
          )}
        </div>
      </header>

      <div className="controls">
        <div className="search-box">
          <Search />
          <input
            type="text"
            placeholder="Filter by institution…"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value)
              setPage(0)
            }}
          />
        </div>
      </div>

      {error && (
        <div className="asset-card">
          Couldn't load institutional_holders.json / raw_data.json — run <code>python main.py 13f</code> and{' '}
          <code>python main.py all</code>.
        </div>
      )}
      {!error && !cards && <div className="asset-card">Loading…</div>}
      {!error && cards && filtered.length === 0 && <div className="asset-card">No institution matches “{search}”.</div>}

      {!error && paged.length > 0 && (
        <>
          <div className="recommendation-grid">
            {paged.map((c) => (
              <HolderCard key={c.name} institution={c} onOpen={setOpenInstitution} />
            ))}
          </div>
          {pageCount > 1 && (
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
        </>
      )}

      {openInstitution && <HolderModal institution={openInstitution} onClose={() => setOpenInstitution(null)} />}
    </div>
  )
}
