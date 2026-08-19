// Types for RecommendationsView.tsx (the Recommendations tab).
import type { PricePoint } from './IAssetView'

export type HistoryByTicker = Record<string, PricePoint[]>

// data/news_sentiment.json-derived rollup recommendations.py attaches to a
// candidate -- last 7 days of headline sentiment, bullish/bearish counts.
export interface NewsSummary {
  bullish: number
  bearish: number
  total: number
}

// Form4-derived rollup recommendations.py attaches to a candidate -- last
// 90 days of insider open-market activity.
export interface InsiderSummary {
  buys: number
  sells: number
}

// One candidate from data/recommendations.json (see recommendations.py's
// build_recommendations) -- also reused, via the closes memo's `{
// ...byTicker.get(ticker), ...tickerScreener[ticker] }` merge, to represent
// a held position's sorted_screen.csv row layered over its (possibly
// stale) recommendations.json entry, which is why every field here is
// optional/nullable rather than a required candidate shape: a screener-
// only row (a held Hold-rated ticker with no recommendations.json entry at
// all) has none of the news7d/insiders90d/instChangeQoQ/targetUpside/
// numberOfAnalystOpinions fields, and a fresh candidate has no `beta`
// (screener-only). Extra fields the Long/Short/To-close/rejected derived
// row types layer on top (oppositeMatchLine, _sortScore, closeSide,
// shares, reasons, hasRatingReason, _severity) live on their own
// intersection types below rather than here, since they're specific to
// one derivation, not part of the underlying data shape.
export interface Candidate {
  ticker: string
  name?: string | null
  rating?: string | null
  score?: number | null
  scorePercentile?: number | null
  momentum?: number | null
  sector?: string | null
  price?: number | null
  beta?: number | null
  shortPercentOfFloat?: number | null
  // FINRA's biweekly-settlement pct-of-float (see recommendations.py) --
  // fresher than shortPercentOfFloat above, which only ever reflects
  // yfinance's month-end settlement. The crowded-short gate prefers this
  // and falls back to shortPercentOfFloat only when FINRA doesn't report
  // the ticker (thinly shorted, or delisted/renamed since).
  shortPctOfFloatFinra?: number | null
  revenueGrowth?: number | null
  epsRevision0y?: number | null
  epsRevision1y?: number | null
  meanReversion?: number | null
  earningsTimestampStart?: number | null
  news7d?: NewsSummary | null
  insiders90d?: InsiderSummary | null
  instChangeQoQ?: number | null
  targetUpside?: number | null
  numberOfAnalystOpinions?: number | null
}

export interface RecommendationsData {
  candidates: Candidate[]
}

// sorted_screen.csv row shape built by RecommendationsView's own CSV-
// parsing effect (tickerScreener) -- covers the WHOLE screener universe,
// unlike recommendations.json's candidates (RATED_FOR_EXTRAS only). Same
// field set as the Candidate fields it's merged over in the closes memo,
// so it can override a stale candidate value ticker-by-ticker.
export type ScreenerByTicker = Record<string, Candidate>

// One reason a held position is flagged in To close (buildCloseReasons) or
// a Strong Buy/Strong Sell candidate was blocked from Long/Short
// (buildRejectionReasons) -- same shape, both functions.
export interface Reason {
  type: string
  text: string
}

// buildOppositeMatcher's return value for a candidate that hedges an
// existing opposite-side position (same sector or theme).
export interface OppositeMatch {
  type: 'sector' | 'theme'
  value: string
  tickers: string[]
}

// A Long/Short idea-list row -- a Candidate plus the hedge-matcher line (if
// any) and the internal sort key used to rank the pool before slicing to
// ROWS_PER_SIDE.
export interface RankedCandidate extends Candidate {
  oppositeMatchLine?: string | null
  _sortScore: number
}

// A To-close row -- a Candidate (screener-over-stale-candidate merged, see
// Candidate's own comment) plus which side it's held on, the live share
// count, every reason that fired, and the severity used to sort the list.
export interface CloseRow extends Candidate {
  closeSide: 'Long' | 'Short'
  shares: number
  reasons: Reason[]
  hasRatingReason: boolean
  _severity: number
}

// A Strong Buy/Strong Sell candidate that failed an opening gate.
export interface RejectedRow extends Candidate {
  reasons: Reason[]
}

// IB Gateway's live EventSource tick for one ticker (see
// ib_server.py) -- only the two fields PriceStat actually reads.
export interface LiveTick {
  last?: number
  timestamp?: string
}
export type LivePricesByTicker = Record<string, LiveTick>

// The live EventSource positions payload -- shares only (no avgCost/value,
// unlike PositionsView.tsx's own richer PositionData; this page only ever
// needs the sign/count to tell long from short and size the To-close row).
export interface Position {
  shares?: number
}
export type PositionsByTicker = Record<string, Position>
