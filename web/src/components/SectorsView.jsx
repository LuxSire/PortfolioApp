import { Fragment, useEffect, useMemo, useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { parseCSV } from '../csv'
import { getSectorGroup, sectorGroupLabel } from '../sectorGroups'
import { getSectorIcon } from '../sectorIcons'
import { avgNewsSentiment, toNum } from '../screenerFactors'
import { FACTOR_COLUMNS, computeFactorAverages } from './factorTable'
import { IB_STREAM_URL } from '../ibStream'
import FactorCells from './FactorCells'
import SectorPosValueChart from './SectorPosValueChart'

function fmtMoney(v) {
  if (v === null || v === undefined) return '—'
  return '$' + v.toLocaleString(undefined, { maximumFractionDigits: 0 })
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
export default function SectorsView() {
  const [rawRows, setRawRows] = useState(null)
  const [error, setError] = useState(null)
  const [expandedSectors, setExpandedSectors] = useState(new Set())
  const [expandedIndustries, setExpandedIndustries] = useState(new Set())
  // Live IB Gateway prices + account positions — same SSE stream
  // PeTable.jsx/PositionsView.jsx already subscribe to — used only to
  // compute each ticker's Pos Value (possize * live-or-CSV price), same
  // formula PeTable.jsx's own Pos Value column uses. Best-effort: no
  // server running just means every Pos Value shows as held-nothing.
  const [livePrices, setLivePrices] = useState({})
  const [positions, setPositions] = useState({})

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
      // sent/newsSent is blank, not a load error.
      fetch('/social_sentiment.json')
        .then((r) => (r.ok ? r.json() : {}))
        .catch(() => ({})),
      fetch('/news_sentiment.json')
        .then((r) => (r.ok ? r.json() : {}))
        .catch(() => ({})),
    ])
      .then(([text, sentiment, newsSentiment]) => {
        // industry's own average forwardPE across the FULL universe (see
        // PeTable.jsx's sectorAvgPE) — computed per raw/granular industry
        // (sorted_screen.csv's "sector" column — see IBApp.get_forward_pe,
        // it's actually yfinance's "industry" field), not the broad
        // sectorGroup, matching what main.py's own sector-relative-PE
        // factor compares against.
        const peSums = new Map()
        const peCounts = new Map()
        const parsed = parseCSV(text).map((r) => {
          const fpe = toNum(r.forwardPE)
          const industry = r.sector || 'Unclassified'
          if (fpe !== null && fpe > 0) {
            peSums.set(industry, (peSums.get(industry) || 0) + fpe)
            peCounts.set(industry, (peCounts.get(industry) || 0) + 1)
          }
          const newsSent = avgNewsSentiment(newsSentiment[r.ticker])
          // Simple average of whichever of the two periods is present —
          // same treatment as PeTable.jsx/PositionsView.jsx's own epsTrend.
          const epsTrendParts = [toNum(r.epsRevision0y), toNum(r.epsRevision1y)].filter((v) => v !== null)
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
          }
        })
        const avgPE = new Map()
        for (const [industry, sum] of peSums) avgPE.set(industry, sum / peCounts.get(industry))
        setRawRows(parsed.map((r) => ({ ...r, savgpe: avgPE.get(r.industry) ?? null })))
      })
      .catch((e) => setError(e.message))
  }, [])

  // Each ticker's Pos Value (shares held × live-or-CSV price) — same
  // formula as PeTable.jsx's own Pos Value column. null (not held, or
  // held but unpriced) for the vast majority of the screener universe;
  // only actually held tickers contribute to a group's Pos Value sum.
  const rows = useMemo(() => {
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
  const tree = useMemo(() => {
    if (!rows) return []
    const bySector = new Map()
    for (const r of rows) {
      if (!bySector.has(r.sectorGroup)) bySector.set(r.sectorGroup, new Map())
      const byIndustry = bySector.get(r.sectorGroup)
      if (!byIndustry.has(r.industry)) byIndustry.set(r.industry, [])
      byIndustry.get(r.industry).push(r)
    }
    const byScore = (a, b) => {
      const av = a.factors ? a.factors.sc : a.sc
      const bv = b.factors ? b.factors.sc : b.sc
      if (av === null && bv === null) return 0
      if (av === null) return 1
      if (bv === null) return -1
      return av - bv
    }
    const posValSum = (groupRows) => groupRows.reduce((s, r) => s + (r.posval ?? 0), 0)
    const sectors = [...bySector.entries()].map(([sectorGroup, byIndustry]) => {
      const sectorRows = [...byIndustry.values()].flat()
      const { factors, count } = computeFactorAverages(sectorRows, () => 1)
      const industries = [...byIndustry.entries()].map(([industry, industryRows]) => {
        const { factors: industryFactors, count: industryCount } = computeFactorAverages(industryRows, () => 1)
        return {
          industry,
          count: industryCount,
          posValSum: posValSum(industryRows),
          factors: industryFactors,
          tickers: [...industryRows].sort(byScore),
        }
      })
      industries.sort(byScore)
      return { sectorGroup, count, posValSum: posValSum(sectorRows), factors, industries }
    })
    sectors.sort(byScore)
    return sectors
  }, [rows])

  // One bar per sector (see SectorPosValueChart.tsx) — same posValSum
  // already shown in the table's Pos Value column at the sector level,
  // just derived from `tree` here rather than duplicating the sum.
  const chartData = useMemo(
    () => tree.map((sector) => ({ sector: sector.sectorGroup, label: sectorGroupLabel(sector.sectorGroup), posValue: sector.posValSum })),
    [tree]
  )

  function toggleSector(sectorGroup) {
    setExpandedSectors((prev) => {
      const next = new Set(prev)
      if (next.has(sectorGroup)) next.delete(sectorGroup)
      else next.add(sectorGroup)
      return next
    })
  }

  function toggleIndustry(key) {
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
          <h1>Sectors</h1>
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
                {FACTOR_COLUMNS.map((col) => (
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
                              <FactorCells factors={industry.factors} />
                            </tr>
                            {industryOpen &&
                              industry.tickers.map((t) => (
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
                                  <FactorCells factors={t} />
                                </tr>
                              ))}
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
