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

// The specific RecommendationsView.tsx gate(s) a *_blocked row failed --
// not mutually exclusive, a row can carry more than one. Empty for any
// non-blocked row (nothing to name).
// crowded_short, revenue_growth and eps_trend were all removed from the
// live gate (crowded_short and eps_trend: backtesting showed each was
// consistently counterproductive on the short side; revenue_growth:
// replaced by a sim-return gate) -- none of these three is one of these
// anymore, kept out rather than left as a reason that can never fire.
export type GateReason = 'momentum' | 'mean_reversion'

export const GATE_REASON_LABEL: Record<GateReason, string> = {
  momentum: 'Momentum (MSI)',
  mean_reversion: 'Mean reversion (ST-MSI)',
}

export interface BacktestTicker {
  ticker: string
  rating: string
  group: GroupKey
  blockedBy: GateReason[]
  return: number // position P&L, same sign convention as GroupStats.return
}

// {groups, portfolio, blockedBreakdown, tickers} -- one full classification
// of a week's candidates. BacktestWeek carries two of these: the top-level
// fields (the rating the snapshot actually shipped with that week) and
// `currentModel` (the same week's factor columns re-scored with TODAY's
// modules.scoring -- see modules/backtest.py's _rescore_current_model for
// exactly what that can and can't reconstruct).
export interface BacktestModel {
  groups: Record<GroupKey, GroupStats>
  // Gated Strong Buy long leg + gated Strong Sell short leg, summed
  // (dollar-neutral, each leg equal-weight 100% gross).
  portfolio: { return: number | null; count: number }
  // Per side, per gate reason that fired at least once this week: the
  // same {return, count} shape as `groups`, restricted to *_blocked rows
  // that failed THAT one reason -- isolates which single rule is behind
  // the group's overall number. Reasons aren't mutually exclusive, so
  // counts here don't sum back to groups.long_blocked/short_blocked's own
  // count. A side/reason with zero hits that week is omitted, not zero.
  blockedBreakdown: Partial<Record<'long' | 'short', Partial<Record<GateReason, GroupStats>>>>
  tickers: BacktestTicker[]
}

export interface BacktestWeek extends BacktestModel {
  week: string // ISO date of the snapshot (e.g. "2026-08-22")
  entryDate: string | null
  exitDate: string | null
  currentModel: BacktestModel
}

export interface Backtest {
  generatedAt: string
  weeks: BacktestWeek[] // oldest first
}
