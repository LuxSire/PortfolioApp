// Types for PositionsView.tsx (the Positions tab).
import type { PricePoint } from './IAssetView'

// Re-exported for callers that fetch price_history_daily_3mo.json /
// price_history.json the same way AssetView.tsx does -- one {date, close} bar
// series per ticker, keyed by ticker.
export type HistoryByTicker = Record<string, PricePoint[]>

// Best-effort label/quote info parsed from sorted_screen.csv, keyed by
// ticker -- see the CSV-parsing effect below. Only used as a fallback for
// a ticker ib_price_server.py's live stream hasn't (or can't) quote.
export interface TickerInfo {
  name: string
  sector: string
  price: number | null
  ern: number | null
  upd: string | null
  rating: string | null
}

export type TickerInfoByTicker = Record<string, TickerInfo>

// Every screener factor column (see screenerFactors.js's COLUMNS /
// factorTable.jsx's FACTOR_KEYS) computed per ticker from
// sorted_screen.csv + sentiment/insider/13F side data -- feeds the
// value-weighted Long/Short portfolio-factors table. All rank-rescaled
// (mom/mr/sent/newsSent/instChange/insiders) or raw depending on the
// field, same as PeTable.jsx's own screener rows.
export interface TickerFactors {
  fpe: number | null
  feps: number | null
  epsTrend: number | null
  tpe: number | null
  tps: number | null
  peg: number | null
  revg: number | null
  pfcf: number | null
  evEbitda: number | null
  beta: number | null
  opMargin: number | null
  de: number | null
  liq: number | null
  shortInt: number | null
  tgt: number | null
  upside: number | null
  mom: number | null
  mr: number | null
  sc: number | null
  sent: number | null
  newsSent: number | null
  instChange: number | null
  insiders: number | null
}

export type FactorsByTicker = Record<string, TickerFactors>

// IB Gateway's live EventSource payload shapes (see ib_price_server.py /
// IB_STREAM_URL) -- prices/positions/account/trades, all keyed by ticker
// except account (a flat tag -> value map, see ACCOUNT_FIELDS).
export interface PriceTick {
  last?: number
  bid?: number
  ask?: number
}
export type PricesByTicker = Record<string, PriceTick>

export interface PositionData {
  shares?: number
  avgCost?: number
}
export type PositionsByTicker = Record<string, PositionData>

export type Account = Record<string, number>

// Today's fills only for a symbol actually traded today (see
// ib_price_server.py's refresh_trades) -- qty/value both signed.
// realizedPnl/commission are IB's own FIFO-cost-basis figures (see
// IBApp.get_today_executions_async's own docstring) -- null, not 0, if
// this connection wasn't alive to see the fill live, e.g. the server
// restarted partway through the day. Not used for open positions
// (rows' own dayPnl formula only reads qty/value), only as
// closedTodayRows' fallback when pnl (reqPnLSingle) has nothing yet for
// that ticker.
export interface Trade {
  qty: number
  value: number
  realizedPnl: number | null
  commission: number | null
}
export type TradesByTicker = Record<string, Trade>

// IBKR's own reqPnLSingle figures (see IBApp.subscribe_position_pnl and
// ib_price_server.py's on_position/on_pnl_single) -- authoritative,
// IB-computed per-position P&L, present for every symbol held at any
// point since the server process started. Deliberately NOT removed when
// `position` goes to 0 (a same-day closed-out position) -- see that
// file's own module docstring on why, and PositionsView.tsx's
// closedTodayRows for what reads position === 0 here as the signal.
export interface PnlSingle {
  dailyPnL: number | null
  unrealizedPnL: number | null
  realizedPnL: number | null
  position: number | null
  value: number | null
}
export type PnlByTicker = Record<string, PnlSingle>

// One row of the main positions table -- tickerInfo + live prices/
// position + computed fields (see the `rows` useMemo) plus every
// TickerFactors field spread in directly (...factorsByTicker[ticker]),
// rather than nested, so FactorCells/computeFactorAverages can read them
// by the same flat key names they already use for Sectors-tab rows.
export interface PositionRow extends Partial<TickerFactors> {
  ticker: string
  name: string
  sector: string | null
  shares: number | null
  avgCost: number | null
  price: number | null
  referencePrice: number | null
  bid: number | null
  ask: number | null
  value: number | null
  pnlPct: number | null
  dayPct: number | null
  dayPnl: number | null
  ern: number | null
  upd: string | null
  rating: string | null
  savgpe: number | null
}

// A symbol traded today (see TradesByTicker) that's now fully closed out
// (0 shares) -- see PositionsView.tsx's closedTodayRows. A much smaller
// field set than PositionRow: no side/sector exposure to group by once a
// position is flat, so this is rendered in its own small table rather
// than folded into the Long/Short/sector tree.
export interface ClosedPositionRow {
  ticker: string
  name: string
  price: number | null
  dayPnl: number | null
}

export interface SectorGroup {
  sector: string | null
  rows: PositionRow[]
  total: number
  dayPnl: number
}

export interface SideGroup {
  side: 'Long' | 'Short'
  sectorGroups: SectorGroup[]
  total: number
  dayPnl: number
  rowCount: number
}

// One side's (Long/Short) value-weighted average across every
// TickerFactors field -- see factorTable.jsx's computeFactorAverages,
// which this wraps; factors' own keys are FACTOR_KEYS (a superset check
// isn't worth doing here since that module is untyped JS, so this stays a
// loose numeric map rather than TickerFactors itself).
export interface WeightedSideFactor {
  side: 'Long' | 'Short'
  count: number
  netValue: number
  sumWeightPct: number | null
  factors: Record<string, number | null>
  beta: number | null
  dollarPer1PctMove: number | null
  dayPnl: number
}

// portfolioVolatilityDecomposition's return shape -- see that function's
// own extensive comment for the dollar-volatility decomposition math.
export interface PortfolioVolResult {
  volDollar: number | null
  cvolByTicker: Map<string, number>
  covered: number
  total: number
}

// portfolioBetaExposure's return shape -- see that function's own comment
// for why the denominator must be gross, not net/signed.
export interface PortfolioBetaResult {
  beta: number | null
  dollarPer1PctMove: number | null
  covered: number
  total: number
}
