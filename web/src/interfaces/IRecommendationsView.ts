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
  // Same 4-leg blended short-interest rank (pctOfFloat/daysToCover/
  // changePercent/shortVolumeRatio) ScreenerView.tsx's own Subrank and
  // AssetView.tsx's "Short Interest (blend)" stat show -- see
  // screenerFactors.computeShortInterestRanks' own docstring. A 1-based
  // ordinal rank (best = 1), computed once over the whole universe on
  // tickerScreener, not a per-candidate recompute.
  shortIntRank?: number | null
  revenueGrowth?: number | null
  // Eulerpool's own forward revenue-growth consensus for the year ahead
  // (fwdRevenue1y/fwdRevenue0y - 1, see modules.derive.
  // reconcile_forward_eps) -- a genuinely new forward-looking signal, NOT
  // a blend of two measurements of the same thing the way forwardEps is
  // (revenueGrowth above is TRAILING, yfinance/SEC-reconciled). Lives on
  // tickerScreener like revenueGrowth/forwardPE, same "own file, spread
  // into each Candidate" pattern.
  eulerRevGrowth1y?: number | null
  // yfinance's own raw forward P/E ratio, and forwardEps -- the POST-
  // BLEND figure (50/50 yfinance/Eulerpool, see modules.derive.
  // reconcile_forward_eps), NOT yfinance's unblended forwardEps -- both
  // live on tickerScreener (sorted_screen.csv), same as revenueGrowth.
  // forwardPE isn't recomputed from the blend at write time, so the two
  // can (and usually do) disagree slightly -- fwdPeExpLine below derives
  // its own "Fwd PE (exp)" implied ratio from price/forwardEps rather
  // than trusting forwardPE to already reflect the blend.
  forwardPE?: number | null
  forwardEps?: number | null
  epsRevision0y?: number | null
  epsRevision1y?: number | null
  meanReversion?: number | null
  // A DIFFERENT hourly read from meanReversion above -- 35h formation
  // (~5 trading days), validated specifically against a 1-day-ahead
  // outcome (see modules/derive.py's reconcile_entry_timing), not folded
  // into the composite score (no FACTOR_WEIGHTS entry -- its own edge
  // decays past ~1-2 days, so it has no business influencing which
  // stocks get picked for this app's multi-day hold). Same 0-100,
  // low=recently-fallen/oversold, high=recently-run-up/overbought scale
  // as meanReversion, purely informational here: "is today specifically
  // a good day to place this entry," not "is this a good stock." Lives
  // on tickerScreener like meanReversion, not on the candidate itself.
  entryTiming?: number | null
  // Trailing average reported-vs-estimate EPS surprise % (see
  // modules/derive.py's earnings_surprise_from_statements) -- the
  // beat/miss TRACK RECORD, not an analyst-estimate revision. Lives on
  // tickerScreener like entryTiming, not on the candidate itself.
  earningsSurpriseAvg?: number | null
  // Recency-weighted read on the MOST RECENT surprise alone, decayed to
  // 0 a couple months after the print (see derive.earnings_pead_from_statements)
  // -- a post-earnings-announcement-drift read, distinct from the
  // average above. null once fully decayed, same as never having one.
  earningsPead?: number | null
  // MSI (Money Flow Index) computed on ONLY the last 3 distinct trading
  // dates of the hourly series (modules/IBApp.py _money_flow_index_last_days)
  // -- a short pre-earnings read of where money flow sits heading into the
  // print. Lives on tickerScreener (sorted_screen.csv), same as
  // meanReversion; shown on the card only near an earnings date.
  earningsMsi?: number | null
  earningsTimestampStart?: number | null
  news7d?: NewsSummary | null
  insiders90d?: InsiderSummary | null
  instChangeQoQ?: number | null
  targetUpside?: number | null
  numberOfAnalystOpinions?: number | null
  // Eulerpool's own [-1, 1] aggregate sell-side stance, RIGHT NOW (see
  // modules.scoring.analyst_consensus_score) -- most-recent grade per
  // firm, averaged; 1 = Strong Buy consensus, -1 = Strong Sell. Distinct
  // from targetUpside above (price-target math, not a rating stance) and
  // from numberOfAnalystOpinions (yfinance's own count). Lives on
  // tickerScreener like forwardPE/eulerRevGrowth1y, not on the
  // recommendations.json candidate itself.
  analystConsensus?: number | null
  // What fraction of shares insiders currently hold -- distinct from
  // insiders90d's recent TRANSACTION activity (buys/sells). Lives on
  // tickerScreener like revenueGrowth/meanReversion, not on the
  // recommendations.json candidate itself -- see the longs/shorts pool
  // builders' own revenueGrowth comment for why.
  heldPercentInsiders?: number | null
  // data/output/simulations.json's own forecastReturn (see
  // modules/simulations.py) -- a separate fetch/merge, not part of
  // recommendations.json or sorted_screen.csv (see RecommendationsView's
  // own tickerForecast state/effect). null/undefined for a ticker that
  // simulation had no data for, same as every other optional factor here.
  forecastReturn?: number | null
  // data/output/simulations.json's own simReturn/simSharpe (see modules/
  // simulations.py's "SIMULATED-PATH FORMULA" section) -- the per-path
  // Monte Carlo's own mean simulated return and its Modified (Israelsen)
  // Sharpe ratio, a DIFFERENT number from forecastReturn above (that one
  // is the confidence-pulled-toward-currentPrice deterministic case;
  // these are the un-pulled simulated-path distribution's own stats).
  // Same fetch/merge pattern as forecastReturn -- see RecommendationsView's
  // own tickerSimPerf state/effect.
  simReturn?: number | null
  simSharpe?: number | null
  // data/output/target_portfolio.json's own longs/shorts membership (see
  // modules/portfolio_optimizer.py) -- which side, if either, the
  // Sharpe-maximising optimizer actually selected this ticker for. A
  // separate fetch/merge, same "own file, own state, spread into each
  // Candidate" pattern as forecastReturn above (see RecommendationsView's
  // own tickerTargetSide state/effect). null/undefined for a ticker the
  // optimizer didn't select on either side.
  targetPortfolioSide?: 'Long' | 'Short' | null
  // data/output/target_portfolio.json also carries the full optimizer
  // pre-screen pools, so cards can show where a ticker ranked before the
  // final Sharpe/covariance pass selected the target portfolio.
  targetPoolSide?: 'Long' | 'Short' | null
  targetPoolRank?: number | null
  targetPoolSize?: number | null
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
// existing opposite-side position -- same granular industry (c.sector),
// or failing that the same broad GICS-style sector group (getSectorGroup).
export interface OppositeMatch {
  type: 'industry' | 'sector'
  value: string
  tickers: string[]
}

// A Long/Short idea-list row -- a Candidate plus the hedge-matcher line (if
// any) and the internal sort key used to rank the pool before slicing to
// ROWS_PER_SIDE.
export interface RankedCandidate extends Candidate {
  oppositeMatchLine?: string | null
  oppositeMatchType?: 'industry' | 'sector' | null
  // buildSameIndustryMatcher's line for a candidate that instead stacks
  // industry concentration on top of an existing SAME-side position --
  // always a thumb-down (see rationaleLines), the mirror of the hedge line.
  concentrationLine?: string | null
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
  // Set only by the "Reporting soon" section builder: true when the
  // last-5-session price move already fights (adverse) or already favors
  // (favorable) this position's thesis, heading into an imminent earnings
  // print (the FEIM check -- see earningsMomentumLine in
  // RecommendationsView.tsx). Drives that section's own CloseCard
  // badSign/goodSign per row.
  _earningsMoveAdverse?: boolean
  _earningsMoveFavorable?: boolean
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
