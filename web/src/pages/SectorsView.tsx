import { Fragment, useEffect, useMemo, useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { parseCSV } from '../csv'
import { getSectorGroup, sectorGroupLabel } from '../sectorGroups'
import { getSectorIcon } from '../sectorIcons'
import { avgInsiderScore, avgNewsSentiment, rankTo100, toNum } from '../screenerFactors'
import { FACTOR_COLUMNS, computeFactorAverages } from '../components/factorTable'
import { IB_STREAM_URL } from '../ibStream'
import FactorCells from '../components/FactorCells'
import SectorPosValueChart from '../components/SectorPosValueChart'
import type {
  AssetRow,
  IndustryGroup,
  LivePricesByTicker,
  PositionsByTicker,
  SectorGroup,
} from '../interfaces/ISectorsView'

function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return '$' + v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

// Signed, 1 decimal -- same convention every other %-return figure in
// this app uses (e.g. PortfolioView.tsx's fmtPct).
function fmtRet(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%'
}

// Daily/weekly trailing price return from price_history.json's daily
// closes (already date-ascending, see PortfolioView.jsx's own NavChart
// for the same assumption on that file's shape elsewhere in this app).
// Daily = last close vs. the one right before it. Weekly = last close
// vs. ~5 trading sessions back (index length-6) -- if fewer than 6
// closes are on file, falls back to the EARLIEST one available instead
// of returning null outright, so a recently-listed or newly-tracked
// ticker still gets a (shorter-window) weekly figure rather than a blank
// -- only a ticker with a single close on file (nothing to diff against)
// gets null for both.
function computeReturns(history: { date: string; close: number }[] | undefined): { dailyPct: number | null; weeklyPct: number | null } {
  if (!history || history.length < 2) return { dailyPct: null, weeklyPct: null }
  const last = history[history.length - 1].close
  const prev = history[history.length - 2].close
  const dailyPct = prev ? (last - prev) / prev : null
  const weekIdx = Math.max(0, history.length - 6)
  const weekAgo = history[weekIdx].close
  const weeklyPct = weekIdx < history.length - 1 && weekAgo ? (last - weekAgo) / weekAgo : null
  return { dailyPct, weeklyPct }
}

// Equal-weight mean of `key` across every row that has it -- the "Sector"
// Daily/Weekly columns' weighting (see this file's own header comment on
// why group rows are plain averages here, unlike PositionsView.tsx's
// value-weighted ones).
function meanOf(rows: AssetRow[], key: 'dailyPct' | 'weeklyPct'): number | null {
  let sum = 0
  let n = 0
  for (const r of rows) {
    const v = r[key]
    if (v === null || v === undefined || !Number.isFinite(v)) continue
    sum += v
    n += 1
  }
  return n ? sum / n : null
}

// Value-weighted mean of `key`, weight = Math.abs(posval) -- the
// "Positions" Daily/Weekly columns' weighting, restricted to rows
// actually held (posval truthy) by construction (an unheld row has
// weight 0 and drops out). Same gross-value-weighting convention
// PositionsView.tsx's own factor table uses (see that file's own
// computeFactorAverages call) -- a short position's raw price return is
// weighted by its absolute exposure, not sign-inverted into a P&L
// figure, so this reads as "how the underlying assets moved," the same
// metric the equal-weighted Sector columns show, just reweighted.
function valueWeightedMeanOf(rows: AssetRow[], key: 'dailyPct' | 'weeklyPct'): number | null {
  let sum = 0
  let weight = 0
  for (const r of rows) {
    const v = r[key]
    const w = Math.abs(r.posval ?? 0)
    if (v === null || v === undefined || !Number.isFinite(v) || !w) continue
    sum += v * w
    weight += w
  }
  return weight ? sum / weight : null
}

// Sectors tab: the full screener universe (sorted_screen.csv — every
// scored ticker, not just held positions; same fetch shape as
// PositionsView.jsx's own portfolio-factors table, see that file for the
// "why" behind each field) grouped into a three-level, expandable
// Sector > Industry > Asset tree. Sector/Industry rows show a PLAIN
// average (equal weight per ticker — there's no dollar exposure to
// weight by outside a real position, unlike Positions' value-weighted
// factors table) of every screener factor across their tickers;
// Asset/leaf rows show that ticker's own real values, rendered through
// the exact same FactorCells component used for the averaged rows (see
// factorTable.js's computeFactorAverages: an "average" of one row is
// just that row, so no separate leaf-rendering path is needed).
//
// Sector 1D/1W vs. Pos 1D/1W (next to Pos Value, ahead of the FactorCells
// tail) are a deliberately DIFFERENT weighting pair, not part of
// FACTOR_KEYS/computeFactorAverages above: Sector 1D/1W is the equal-
// weight daily/weekly price return across EVERY ticker in the group
// (held or not, matching this page's own equal-weight convention for
// every other group-level column) -- Pos 1D/1W is the SAME two returns
// but value-weighted by Math.abs(posval) across only the tickers
// actually HELD in that group (matching PositionsView.tsx's own gross-
// value-weighting convention), null when nothing in the group is held.
// See meanOf/valueWeightedMeanOf and computeReturns below.
export default function SectorsView() {
  const [rawRows, setRawRows] = useState<AssetRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [expandedSectors, setExpandedSectors] = useState<Set<string | null>>(new Set())
  const [expandedIndustries, setExpandedIndustries] = useState<Set<string>>(new Set())
  // Same "Positions only" filter NewsView.tsx already offers, just
  // placed next to the title instead of in a controls row -- restricts
  // the whole Sector > Industry > Asset tree (and the pos-value chart
  // below it, since that's derived from `tree`) to currently held
  // tickers only.
  const [positionsOnly, setPositionsOnly] = useState(false)
  // Live IB Gateway prices + account positions — same SSE stream
  // PeTable.jsx/PositionsView.jsx already subscribe to — used only to
  // compute each ticker's Pos Value (possize * live-or-CSV price), same
  // formula PeTable.jsx's own Pos Value column uses. Best-effort: no
  // server running just means every Pos Value shows as held-nothing.
  const [livePrices, setLivePrices] = useState<LivePricesByTicker>({})
  const [positions, setPositions] = useState<PositionsByTicker>({})

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
      // Same best-effort contract as PeTable.jsx/PositionsView.jsx's own
      // fetch of these — missing/failed just means every ticker's
      // sent/newsSent/instChange/insiders is blank, not a load error.
      fetch('/social_sentiment.json')
        .then((r) => (r.ok ? r.json() : {}))
        .catch(() => ({})) as Promise<Record<string, any>>,
      fetch('/news_sentiment.json')
        .then((r) => (r.ok ? r.json() : {}))
        .catch(() => ({})) as Promise<Record<string, any>>,
      fetch('/sec/form4/insider_transactions.json')
        .then((r) => (r.ok ? r.json() : {}))
        .catch(() => ({})) as Promise<Record<string, any>>,
      fetch('/sec/13f/institutional_holdings.json')
        .then((r) => (r.ok ? r.json() : {}))
        .catch(() => ({})) as Promise<Record<string, any>>,
      // Daily closes per ticker (yfinance, see PortfolioView.jsx's own
      // NavChart for the same file elsewhere in this app) -- the source
      // for the Daily/Weekly return columns below (see computeReturns).
      // Same best-effort contract as the other optional fetches here.
      fetch('/price_history.json')
        .then((r) => (r.ok ? r.json() : {}))
        .catch(() => ({})) as Promise<Record<string, { date: string; close: number }[]>>,
    ] as const)
      .then(([text, sentiment, newsSentiment, insiderTransactions, institutionalHoldings, priceHistory]) => {
        // industry's own average forwardPE across the FULL universe (see
        // PeTable.jsx's sectorAvgPE) — computed per raw/granular industry
        // (sorted_screen.csv's "sector" column — see IBApp.get_forward_pe,
        // it's actually yfinance's "industry" field), not the broad
        // sectorGroup, matching what main.py's own sector-relative-PE
        // factor compares against.
        const peSums = new Map<string, number>()
        const peCounts = new Map<string, number>()
        const parsed: AssetRow[] = parseCSV(text).map((r: any) => {
          const fpe = toNum(r.forwardPE)
          const industry = r.sector || 'Unclassified'
          if (fpe !== null && fpe > 0) {
            peSums.set(industry, (peSums.get(industry) || 0) + fpe)
            peCounts.set(industry, (peCounts.get(industry) || 0) + 1)
          }
          const newsSent = avgNewsSentiment(newsSentiment[r.ticker])
          const insiders = avgInsiderScore(insiderTransactions[r.ticker])
          // Simple average of whichever of the two periods is present —
          // same treatment as PeTable.jsx/PositionsView.jsx's own epsTrend.
          const epsTrendParts = [toNum(r.epsRevision0y), toNum(r.epsRevision1y)].filter((v) => v !== null) as number[]
          const epsTrend = epsTrendParts.length
            ? epsTrendParts.reduce((a, b) => a + b, 0) / epsTrendParts.length
            : null
          return {
            t: r.ticker,
            n: r.name,
            industry,
            sectorGroup: getSectorGroup(industry),
            p: toNum(r.price),
            fpe,
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
            shortInt: toNum(r.shortPercentOfFloat),
            upside: toNum(r.targetUpside),
            mom: toNum(r.momentum),
            mr: toNum(r.meanReversion),
            sc: toNum(r.score),
            sent: toNum(sentiment[r.ticker]?.score),
            newsSent: newsSent.avg !== null ? newsSent.avg - 3 : null,
            instChange: toNum(institutionalHoldings[r.ticker]?.pctShareChangeQoQ),
            // ×100, NOT rank-rescaled like the loop below — see
            // ScreenerView.tsx's identical assignment for why: a
            // percentile rank over an insider-buy universe this dominated
            // by zero-buy ties lets a single stray buy vault a ticker
            // dramatically up the scale.
            insiders: insiders.avg !== null ? insiders.avg * 100 : null,
            ...computeReturns(priceHistory[r.ticker]),
          }
        })
        // Same rank-to-[-100, 100] rescale as ScreenerView.tsx/PositionsView.tsx
        // (see rankTo100's own comment for why rank-based, not min-max),
        // over this same full sorted_screen.csv universe (before grouping
        // into sector/industry rows below), so the scale matches across
        // every tab rather than being Sectors-only. Insiders deliberately
        // excluded — see its own ×100 comment above.
        for (const key of ['mom', 'mr', 'sent', 'newsSent', 'instChange'] as const) {
          const ranked = rankTo100(parsed.map((r) => r[key]))
          parsed.forEach((r, i) => {
            r[key] = ranked[i]
          })
        }
        const avgPE = new Map<string, number>()
        for (const [industry, sum] of peSums) avgPE.set(industry, sum / (peCounts.get(industry) as number))
        setRawRows(parsed.map((r) => ({ ...r, savgpe: avgPE.get(r.industry) ?? null })))
      })
      .catch((e) => setError(e.message))
  }, [])

  // Each ticker's Pos Value (shares held × live-or-CSV price) — same
  // formula as PeTable.jsx's own Pos Value column. null (not held, or
  // held but unpriced) for the vast majority of the screener universe;
  // only actually held tickers contribute to a group's Pos Value sum.
  const rows: AssetRow[] | null = useMemo(() => {
    if (!rawRows) return null
    return rawRows.map((r) => {
      const possize = positions[r.t]?.shares ?? null
      const posPrice = livePrices[r.t]?.last ?? r.p
      const posval = possize !== null && posPrice !== null ? possize * posPrice : null
      return { ...r, posval }
    })
  }, [rawRows, positions, livePrices])

  // Sector > Industry > Asset tree. Every group row (sector or industry)
  // averages its tickers' factors with equal weight — see
  // computeFactorAverages — then every level is sorted best-Score-first
  // (nulls last), same "lower is better" direction the Screener itself
  // sorts by default, so this reads as "which sector/industry looks best
  // right now" at a glance rather than needing to be re-sorted by hand.
  const tree: SectorGroup[] = useMemo(() => {
    if (!rows) return []
    const visibleRows = positionsOnly ? rows.filter((r) => (positions[r.t]?.shares ?? 0) !== 0) : rows
    const bySector = new Map<string | null, Map<string, AssetRow[]>>()
    for (const r of visibleRows) {
      if (!bySector.has(r.sectorGroup)) bySector.set(r.sectorGroup, new Map())
      const byIndustry = bySector.get(r.sectorGroup) as Map<string, AssetRow[]>
      if (!byIndustry.has(r.industry)) byIndustry.set(r.industry, [])
      ;(byIndustry.get(r.industry) as AssetRow[]).push(r)
    }
    const byScore = (
      a: { factors?: Record<string, number | null>; sc?: number | null },
      b: { factors?: Record<string, number | null>; sc?: number | null }
    ) => {
      const av = a.factors ? a.factors.sc : a.sc
      const bv = b.factors ? b.factors.sc : b.sc
      if (av === null || av === undefined) return bv === null || bv === undefined ? 0 : 1
      if (bv === null || bv === undefined) return -1
      return av - bv
    }
    const posValSum = (groupRows: AssetRow[]) => groupRows.reduce((s, r) => s + (r.posval ?? 0), 0)
    const sectors: SectorGroup[] = [...bySector.entries()].map(([sectorGroup, byIndustry]) => {
      const sectorRows = [...byIndustry.values()].flat()
      const { factors, count } = computeFactorAverages(sectorRows, () => 1) as unknown as {
        factors: Record<string, number | null>
        count: number
      }
      const industries: IndustryGroup[] = [...byIndustry.entries()].map(([industry, industryRows]) => {
        const { factors: industryFactors, count: industryCount } = computeFactorAverages(industryRows, () => 1) as unknown as {
          factors: Record<string, number | null>
          count: number
        }
        return {
          industry,
          count: industryCount,
          posValSum: posValSum(industryRows),
          factors: industryFactors,
          sectorDailyPct: meanOf(industryRows, 'dailyPct'),
          sectorWeeklyPct: meanOf(industryRows, 'weeklyPct'),
          posDailyPct: valueWeightedMeanOf(industryRows, 'dailyPct'),
          tickers: [...industryRows].sort(byScore),
        }
      })
      industries.sort(byScore)
      return {
        sectorGroup,
        count,
        posValSum: posValSum(sectorRows),
        factors,
        sectorDailyPct: meanOf(sectorRows, 'dailyPct'),
        sectorWeeklyPct: meanOf(sectorRows, 'weeklyPct'),
        posDailyPct: valueWeightedMeanOf(sectorRows, 'dailyPct'),
        industries,
      }
    })
    sectors.sort(byScore)
    return sectors
  }, [rows, positions, positionsOnly])

  // One bar per sector (see SectorPosValueChart.tsx) — same posValSum
  // already shown in the table's Pos Value column at the sector level,
  // just derived from `tree` here rather than duplicating the sum.
  const chartData = useMemo(
    () =>
      tree.map((sector) => ({
        sector: sector.sectorGroup ?? '',
        label: sectorGroupLabel(sector.sectorGroup),
        posValue: sector.posValSum,
      })),
    [tree]
  )

  function toggleSector(sectorGroup: string | null) {
    setExpandedSectors((prev) => {
      const next = new Set(prev)
      if (next.has(sectorGroup)) next.delete(sectorGroup)
      else next.add(sectorGroup)
      return next
    })
  }

  function toggleIndustry(key: string) {
    setExpandedIndustries((prev) => {
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
          <div className="title-with-info">
            <h1>Sectors</h1>
            <label className="position-filter">
              <input type="checkbox" checked={positionsOnly} onChange={(e) => setPositionsOnly(e.target.checked)} />
              Positions only{Object.keys(positions).length > 0 ? ` (${Object.keys(positions).length})` : ''}
            </label>
          </div>
        </div>
      </header>

      {error && <div className="asset-card">Couldn't load sorted_screen.csv: {error}</div>}
      {!error && rawRows === null && <div className="asset-card">Loading…</div>}

      {!error && rawRows && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className="col-left col-name">Group</th>
                <th># Assets</th>
                <th>Pos Value</th>
                <th title="Every ticker in this group, equal-weighted">Sector 1D</th>
                <th title="Every ticker in this group, equal-weighted">Sector 1W</th>
                <th title="Held tickers in this group only, weighted by position value">Pos 1D</th>
                {FACTOR_COLUMNS.map((col: { key: string; label: string }) => (
                  <th key={col.key}>{col.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {tree.map((sector) => {
                const SectorIcon = getSectorIcon(sector.sectorGroup)
                const sectorOpen = expandedSectors.has(sector.sectorGroup)
                return (
                  <Fragment key={sector.sectorGroup}>
                    <tr
                      className="factor-tree-row factor-tree-level-0"
                      onClick={() => toggleSector(sector.sectorGroup)}
                    >
                      <td className="col-left col-name">
                        <span className="factor-tree-toggle">
                          {sectorOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                          <SectorIcon size={14} />
                          {sectorGroupLabel(sector.sectorGroup)}
                        </span>
                      </td>
                      <td className="num">{sector.count}</td>
                      <td className="num">{fmtMoney(sector.posValSum || null)}</td>
                      <td className={`num ${sector.sectorDailyPct === null ? '' : sector.sectorDailyPct >= 0 ? 'good' : 'bad'}`}>
                        {fmtRet(sector.sectorDailyPct)}
                      </td>
                      <td className={`num ${sector.sectorWeeklyPct === null ? '' : sector.sectorWeeklyPct >= 0 ? 'good' : 'bad'}`}>
                        {fmtRet(sector.sectorWeeklyPct)}
                      </td>
                      <td className={`num ${sector.posDailyPct === null ? '' : sector.posDailyPct >= 0 ? 'good' : 'bad'}`}>
                        {fmtRet(sector.posDailyPct)}
                      </td>
                      <FactorCells factors={sector.factors} />
                    </tr>
                    {sectorOpen &&
                      sector.industries.map((industry) => {
                        const industryKey = `${sector.sectorGroup}::${industry.industry}`
                        const industryOpen = expandedIndustries.has(industryKey)
                        return (
                          <Fragment key={industryKey}>
                            <tr
                              className="factor-tree-row factor-tree-level-1"
                              onClick={() => toggleIndustry(industryKey)}
                            >
                              <td className="col-left col-name">
                                <span className="factor-tree-toggle">
                                  {industryOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                                  {industry.industry}
                                </span>
                              </td>
                              <td className="num">{industry.count}</td>
                              <td className="num">{fmtMoney(industry.posValSum || null)}</td>
                              <td className={`num ${industry.sectorDailyPct === null ? '' : industry.sectorDailyPct >= 0 ? 'good' : 'bad'}`}>
                                {fmtRet(industry.sectorDailyPct)}
                              </td>
                              <td className={`num ${industry.sectorWeeklyPct === null ? '' : industry.sectorWeeklyPct >= 0 ? 'good' : 'bad'}`}>
                                {fmtRet(industry.sectorWeeklyPct)}
                              </td>
                              <td className={`num ${industry.posDailyPct === null ? '' : industry.posDailyPct >= 0 ? 'good' : 'bad'}`}>
                                {fmtRet(industry.posDailyPct)}
                              </td>
                              <FactorCells factors={industry.factors} />
                            </tr>
                            {industryOpen &&
                              industry.tickers.map((t) => {
                                // A leaf's "Sector" columns are just its own
                                // return (equal-weight-of-one); "Pos" columns
                                // only show that same return if this ticker
                                // is actually held (t.posval not null) --
                                // otherwise there's no position to weight.
                                const held = t.posval !== null && t.posval !== undefined
                                return (
                                  <tr className="factor-tree-row factor-tree-level-2" key={t.t}>
                                    <td className="col-left col-name">
                                      <span className="factor-tree-leaf">
                                        <a
                                          href={`#/asset/${encodeURIComponent(t.t)}`}
                                          target="_blank"
                                          rel="noopener noreferrer"
                                          className="ticker-link"
                                        >
                                          {t.t}
                                        </a>
                                        <span className="factor-tree-leaf-name">{t.n}</span>
                                      </span>
                                    </td>
                                    <td className="num">—</td>
                                    <td className="num">{fmtMoney(t.posval)}</td>
                                    <td className={`num ${t.dailyPct === null || t.dailyPct === undefined ? '' : t.dailyPct >= 0 ? 'good' : 'bad'}`}>
                                      {fmtRet(t.dailyPct)}
                                    </td>
                                    <td className={`num ${t.weeklyPct === null || t.weeklyPct === undefined ? '' : t.weeklyPct >= 0 ? 'good' : 'bad'}`}>
                                      {fmtRet(t.weeklyPct)}
                                    </td>
                                    <td className={`num ${!held || t.dailyPct === null || t.dailyPct === undefined ? '' : t.dailyPct >= 0 ? 'good' : 'bad'}`}>
                                      {held ? fmtRet(t.dailyPct) : '—'}
                                    </td>
                                    <FactorCells factors={t} />
                                  </tr>
                                )
                              })}
                          </Fragment>
                        )
                      })}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {chartData.length > 0 && <SectorPosValueChart data={chartData} />}
    </div>
  )
}
