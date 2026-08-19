// Types for ScreenerView.tsx (the Screener tab, formerly PeTable.jsx).
import type { PricePoint } from './IAssetView'

export type HistoryByTicker = Record<string, PricePoint[]>

// One sorted_screen.csv row as first parsed (before sector-avg-PE and the
// per-metric subrank maps are joined in -- see ScreenerRow below for the
// fully-joined shape actually rendered).
export interface RawScreenerRow {
  rank: number
  t: string
  n: string
  s: string
  sent: number | null
  sentBullish: number | null
  sentBearish: number | null
  sentTotal: number | null
  newsSent: number | null
  newsSentCount: number | null
  instChange: number | null
  instChangeRaw: number | null
  insiders: number | null
  insiderBuys: number | null
  insiderSells: number | null
  beta: number | null
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
  epsVol: number | null
  de: number | null
  liq: number | null
  shortInt: number | null
  shortRatio: number | null
  p: number | null
  tgt: number | null
  tgtHigh: number | null
  tgtLow: number | null
  numAnalysts: number | null
  upside: number | null
  rec: string | null
  rating: string | null
  mom: number | null
  mr: number | null
  sc: number | null
  ern: number | null
  upd: string | null
}

// A RawScreenerRow plus its sector's avg forward PE and every per-metric
// subrank (see PeTable's fpeRank/pegRank/.../insidersRank memos) -- the
// shape the table actually renders, one row per ticker.
export interface ScreenerRow extends RawScreenerRow {
  savgpe: number | null
  fpeRank: number | null
  pegRank: number | null
  tpsRank: number | null
  pfcfRank: number | null
  evEbitdaRank: number | null
  epsVolRank: number | null
  deRank: number | null
  momRank: number | null
  mrRank: number | null
  epsTrendRank: number | null
  upsideRank: number | null
  revgRank: number | null
  diffRank: number | null
  liqRank: number | null
  shortIntRank: number | null
  sentRank: number | null
  newsSentRank: number | null
  instChangeRank: number | null
  insidersRank: number | null
}

// The live EventSource positions/prices payload.
export interface Position {
  shares?: number
}
export type PositionsByTicker = Record<string, Position>

export interface LiveTick {
  last?: number
  timestamp?: string
}
export type LivePricesByTicker = Record<string, LiveTick>

// useStickyHeaderClone's return shape -- see that hook's own comment.
export interface StickyHeaderState {
  stuck: boolean
  left: number
  width: number
  widths: number[]
}
