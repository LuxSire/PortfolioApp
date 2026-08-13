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
}

// One industry group (mid tier of the Sector > Industry > Asset tree) --
// factors is a plain (equal-weight) average across its tickers, see
// factorTable.jsx's computeFactorAverages, which this page's own
// untyped-JS import returns as a loose numeric map.
export interface IndustryGroup {
  industry: string
  count: number
  posValSum: number
  factors: Record<string, number | null>
  tickers: AssetRow[]
}

// One sector group (top tier of the tree).
export interface SectorGroup {
  sectorGroup: string | null
  count: number
  posValSum: number
  factors: Record<string, number | null>
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
