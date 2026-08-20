import { Fragment, useEffect, useMemo, useState } from 'react'
import { parseCSV } from '../csv'
import { getSectorGroup, sectorGroupLabel } from '../sectorGroups'
import type { SectorRatingRow } from '../interfaces/IScoringView'

function fmtPct1(v: number): string {
  return `${v.toFixed(1)}%`
}

function blankRow(sector: string): SectorRatingRow {
  return { sector, strongBuy: 0, buy: 0, hold: 0, sell: 0, strongSell: 0, na: 0, total: 0, scored: 0, buyPct: 0, sellPct: 0 }
}

function tally(r: SectorRatingRow, rating: string) {
  r.total += 1
  switch (rating) {
    case 'Strong Buy':
      r.strongBuy += 1
      break
    case 'Buy':
      r.buy += 1
      break
    case 'Hold':
      r.hold += 1
      break
    case 'Sell':
      r.sell += 1
      break
    case 'Strong Sell':
      r.strongSell += 1
      break
    default:
      r.na += 1
  }
}

function withPct(r: SectorRatingRow): SectorRatingRow {
  r.scored = r.total - r.na
  r.buyPct = r.scored ? ((r.strongBuy + r.buy) / r.scored) * 100 : 0
  r.sellPct = r.scored ? ((r.sell + r.strongSell) / r.scored) * 100 : 0
  return r
}

// sorted_screen.csv's rating column (see main.py's rating_for_percentile),
// counted per ticker's sector/industry and rolled up into the 11 broad
// Yahoo sectors (see sectorGroups.js's getSectorGroup -- the same grouping
// the Positions/Sectors tabs already use). Top-level rows are the broad
// sectors, click one to expand it into its granular industries -- built to
// spot exactly the kind of sector-level scoring bias this app's Financials/
// Utilities/Real-Estate weight overrides (see ScoringFormulaTable) exist to
// correct for in the first place.
//
// Self-contained (own fetch, no props) -- extracted out of ScoringView.tsx
// (the Scoring tab) so it could be dropped into another page too, not just
// this one.
export default function RatingBreakdownTable() {
  const [screenerRows, setScreenerRows] = useState<Array<Record<string, string>> | null>(null)

  // sorted_screen.csv -- same file the Screener itself renders, read here
  // only for its sector/rating columns. Fetched once on mount, not polled
  // -- this file only changes when a Dataset-tab job or CLI rescore runs.
  useEffect(() => {
    fetch('/sorted_screen.csv')
      .then((r) => (r.ok ? r.text() : ''))
      .then((text) => setScreenerRows(parseCSV(text)))
      .catch(() => setScreenerRows([]))
  }, [])

  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set())

  function toggleGroup(group: string) {
    setExpandedGroups((prev) => {
      const next = new Set(prev)
      if (next.has(group)) next.delete(group)
      else next.add(group)
      return next
    })
  }

  // Two levels: sectorGroupRows (the 11 broad sectors) as the top-level,
  // expandable rows; industryRowsByGroup underneath each, shown only once
  // its parent group is toggled open.
  const { sectorGroupRows, industryRowsByGroup } = useMemo(() => {
    if (!screenerRows) return { sectorGroupRows: [] as SectorRatingRow[], industryRowsByGroup: new Map<string, SectorRatingRow[]>() }

    const byIndustry = new Map<string, SectorRatingRow>()
    const byGroup = new Map<string, SectorRatingRow>()
    const groupOfIndustry = new Map<string, string>()

    for (const row of screenerRows) {
      const industry = row.sector || '(none)'
      const group = getSectorGroup(row.sector)
      groupOfIndustry.set(industry, group)

      let ir = byIndustry.get(industry)
      if (!ir) {
        ir = blankRow(industry)
        byIndustry.set(industry, ir)
      }
      tally(ir, row.rating)

      let gr = byGroup.get(group)
      if (!gr) {
        gr = blankRow(group)
        byGroup.set(group, gr)
      }
      tally(gr, row.rating)
    }

    const industryRowsByGroup = new Map<string, SectorRatingRow[]>()
    for (const [industry, row] of byIndustry) {
      withPct(row)
      const group = groupOfIndustry.get(industry) as string
      if (!industryRowsByGroup.has(group)) industryRowsByGroup.set(group, [])
      industryRowsByGroup.get(group)!.push(row)
    }
    for (const rows of industryRowsByGroup.values()) {
      rows.sort((a, b) => b.buyPct - a.buyPct)
    }

    const sectorGroupRows = [...byGroup.values()].map(withPct).sort((a, b) => b.buyPct - a.buyPct)

    return { sectorGroupRows, industryRowsByGroup }
  }, [screenerRows])

  return (
    <>
      <header className="masthead">
        <div className="title-block">
          <h2>Rating Breakdown by Sector</h2>
        </div>
      </header>

      {!screenerRows && <div className="asset-card">Loading…</div>}
      {screenerRows && screenerRows.length === 0 && (
        <div className="asset-card">Couldn't reach /sorted_screen.csv — is ib_server.py running?</div>
      )}
      {sectorGroupRows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className="col-left">Sector</th>
                <th>Strong Buy</th>
                <th>Buy</th>
                <th>Hold</th>
                <th>Sell</th>
                <th>Strong Sell</th>
                <th>NA</th>
                <th>Total</th>
                <th title="Strong Buy + Buy, as a % of scored (non-NA) tickers">Buy %</th>
                <th title="Sell + Strong Sell, as a % of scored (non-NA) tickers">Sell %</th>
              </tr>
            </thead>
            <tbody>
              {sectorGroupRows.map((g) => {
                const expanded = expandedGroups.has(g.sector)
                const industries = industryRowsByGroup.get(g.sector) ?? []
                return (
                  <Fragment key={g.sector}>
                    <tr className="scoring-sector-row" onClick={() => toggleGroup(g.sector)}>
                      <td className="col-left">
                        <span className={`scoring-expand-caret${expanded ? ' expanded' : ''}`}>▸</span>
                        {sectorGroupLabel(g.sector)}
                      </td>
                      <td className="num">{g.strongBuy}</td>
                      <td className="num">{g.buy}</td>
                      <td className="num">{g.hold}</td>
                      <td className="num">{g.sell}</td>
                      <td className="num">{g.strongSell}</td>
                      <td className="num">{g.na}</td>
                      <td className="num">{g.total}</td>
                      <td className="num">{fmtPct1(g.buyPct)}</td>
                      <td className="num">{fmtPct1(g.sellPct)}</td>
                    </tr>
                    {expanded &&
                      industries.map((r) => (
                        <tr key={`${g.sector}-${r.sector}`} className="scoring-industry-row">
                          <td className="col-left">{r.sector}</td>
                          <td className="num">{r.strongBuy}</td>
                          <td className="num">{r.buy}</td>
                          <td className="num">{r.hold}</td>
                          <td className="num">{r.sell}</td>
                          <td className="num">{r.strongSell}</td>
                          <td className="num">{r.na}</td>
                          <td className="num">{r.total}</td>
                          <td className="num">{fmtPct1(r.buyPct)}</td>
                          <td className="num">{fmtPct1(r.sellPct)}</td>
                        </tr>
                      ))}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}
