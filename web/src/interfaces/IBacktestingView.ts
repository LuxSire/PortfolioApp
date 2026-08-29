// Types for BacktestingView.tsx -- GET /backtest.json, written by
// modules/backtest.py (main.py `download_backtest`).

export const GROUPS = [
  'long_strong_buy',
  'long_buy',
  'long_blocked',
  'short_strong_sell',
  'short_sell',
  'short_blocked',
] as const
export type GroupKey = (typeof GROUPS)[number]

export const GROUP_LABEL: Record<GroupKey, string> = {
  long_strong_buy: 'Long · Strong Buy',
  long_buy: 'Long · Buy',
  long_blocked: 'Long blocked',
  short_strong_sell: 'Short · Strong Sell',
  short_sell: 'Short · Sell',
  short_blocked: 'Short blocked',
}

export interface GroupStats {
  // Equal-weight mean POSITION P&L over the week (+stock return for longs,
  // -stock return for shorts) as a fraction (0.012 = +1.2%).
  return: number | null
  count: number
}

export interface BacktestTicker {
  ticker: string
  rating: string
  group: GroupKey
  return: number // position P&L, same sign convention as GroupStats.return
}

export interface BacktestWeek {
  week: string // ISO date of the snapshot (e.g. "2026-08-22")
  entryDate: string | null
  exitDate: string | null
  groups: Record<GroupKey, GroupStats>
  // Gated Strong Buy long leg + gated Strong Sell short leg, summed
  // (dollar-neutral, each leg equal-weight 100% gross).
  portfolio: { return: number | null; count: number }
  tickers: BacktestTicker[]
}

export interface Backtest {
  generatedAt: string
  weeks: BacktestWeek[] // oldest first
}
