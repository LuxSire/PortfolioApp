// Types for SectorsView.tsx (the Sectors tab).

// One sorted_screen.csv row's screener factors, parsed the same way
// PositionsView.tsx's own TickerFactors is (see that file) plus this
// page's own leading fields (t/n/industry/sectorGroup/p) and savgpe
// (industry's own average forward P/E, attached after the initial parse)
// -- posval (live position value) is layered on afterward too, by the
// `rows` useMemo, so it's optional here rather than required on the raw
// parsed row.
export interface AssetRow {
  t: string
  n: string
  industry: string
  sectorGroup: string | null
  p: number | null
  fpe: number | null
  feps: number | null
  epsTrend: number | null
  tpe: number | null
  tps: number | null
  peg: number | null
  revg: number | null
  pfcf: number | null
  evEbitda: number | null
  opMargin: number | null
  de: number | null
  liq: number | null
  shortInt: number | null
  upside: number | null
  mom: number | null
  mr: number | null
  sc: number | null
  sent: number | null
  newsSent: number | null
  instChange: number | null
  insiders: number | null
  savgpe?: number | null
  posval?: number | null
  // Trailing daily/weekly price return (fraction, e.g. 0.023 = +2.3%),
  // derived from price_history.json -- see SectorsView.tsx's own
  // computeReturns. Optional/nullable: a ticker with fewer than 2 daily
  // closes on file (new listing, or price_history.json hasn't covered it
  // yet) has neither.
  dailyPct?: number | null
  weeklyPct?: number | null
}

// One industry group (mid tier of the Sector > Industry > Asset tree) --
// factors is a plain (equal-weight) average across its tickers, see
// factorTable.jsx's computeFactorAverages, which this page's own
// untyped-JS import returns as a loose numeric map. sectorDailyPct/
// sectorWeeklyPct are that SAME equal-weight treatment applied to
// dailyPct/weeklyPct across every ticker in the group (held or not) --
// posDailyPct is the opposite weighting, value-weighted by
// Math.abs(posval) across only the tickers actually held in this group
// (same gross-value-weighting convention PositionsView.tsx's own
// factor table uses), null when nothing in the group is held. See
// SectorsView.tsx's own meanOf/valueWeightedMeanOf. There's no
// posWeeklyPct (value-weighted weekly) -- explicit instruction, dropped
// as wrong: a week-over-week return computed from the STOCK's own price
// history has no idea when a position was actually opened, so a
// recently-opened position's "weekly performance" would mostly reflect
// days before it was even held -- misleading in a way posDailyPct isn't
// (a day-old position and today's daily return are close enough to
// still be meaningful). sectorWeeklyPct doesn't have this problem since
// it was never claiming to represent a position's own performance.
export interface IndustryGroup {
  industry: string
  count: number
  posValSum: number
  factors: Record<string, number | null>
  sectorDailyPct: number | null
  sectorWeeklyPct: number | null
  posDailyPct: number | null
  tickers: AssetRow[]
}

// One sector group (top tier of the tree) -- same fields as IndustryGroup,
// just aggregated one level up.
export interface SectorGroup {
  sectorGroup: string | null
  count: number
  posValSum: number
  factors: Record<string, number | null>
  sectorDailyPct: number | null
  sectorWeeklyPct: number | null
  posDailyPct: number | null
  industries: IndustryGroup[]
}

// The live EventSource positions/prices payload.
export interface Position {
  shares?: number
}
export type PositionsByTicker = Record<string, Position>

export interface LiveTick {
  last?: number
}
export type LivePricesByTicker = Record<string, LiveTick>
