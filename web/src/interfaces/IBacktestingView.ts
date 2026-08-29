// Types for BacktestingView.tsx -- GET /backtest.json, written by
// modules/backtest.py (main.py `download_backtest`).

export const RATING_BUCKETS = ['Strong Buy', 'Buy', 'Sell', 'Strong Sell'] as const
export type RatingBucket = (typeof RATING_BUCKETS)[number]

export interface BucketStats {
  // Equal-weight, daily-rebalanced over the week. return / vol are
  // fractions (0.012 = +1.2%); vol is stdev(daily) x sqrt(n_days).
  // sharpe = return / vol (rf ~ 0). null when the bucket had < 2 covered
  // names or no price path.
  return: number | null
  vol: number | null
  sharpe: number | null
  count: number
}

export interface BacktestTicker {
  ticker: string
  rating: RatingBucket
  return: number
}

export interface BacktestWeek {
  week: string // ISO date of the screen snapshot (e.g. "2026-08-22")
  entryDate: string | null // first close used (last bar on/before `week`)
  exitDate: string | null // last close used (last bar on/before week + 7d)
  buckets: Record<RatingBucket, BucketStats>
  tickers: BacktestTicker[]
}

export interface Backtest {
  generatedAt: string
  weeks: BacktestWeek[] // oldest first
}
