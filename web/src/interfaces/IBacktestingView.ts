// Types for BacktestingView.tsx -- GET /backtest.json, written by
// modules/backtest.py (main.py `download_backtest`).

export const GROUPS = [
  'long_strong_buy',
  'long_buy',
  'long_blocked',
  'short_strong_sell',
  'short_sell',
  'short_blocked',
  'hold',
] as const
export type GroupKey = (typeof GROUPS)[number]

export const GROUP_LABEL: Record<GroupKey, string> = {
  // (10%) is scoring.RATING_THRESHOLDS' own cut, called out now that the
  // (5%)/(2.5%) cuts below sit next to it in the same table. Was 6% --
  // widened, same proportional change scoring.RATING_THRESHOLDS itself
  // made (see that constant's own comment).
  long_strong_buy: 'Long · Strong Buy (10%)',
  long_buy: 'Long · Buy',
  long_blocked: 'Long blocked',
  short_strong_sell: 'Short · Strong Sell (10%)',
  short_sell: 'Short · Sell',
  short_blocked: 'Short blocked',
  hold: 'Hold', // no gates -- the unrated middle, held long as a baseline
}

// Two nested, non-partitioning reference stats per side (2.5% ⊂ 5% ⊂ the
// 10% rating cut) -- every candidate is still classified into exactly
// one of the seven GROUPS above, these are strict subsets of
// long_strong_buy/short_strong_sell reported separately. Optional: an
// older archived week may have no `score` column to derive a percentile
// from. The "_4"/"_2"-suffixed KEYS below are legacy names (were 4%/2%
// before this widening) -- left as-is rather than renaming every key/
// group-key string this type feeds into across both this file and
// modules/backtest.py's own JSON output; only the LABELS shown to the
// user changed.
export type RestrictedGroupKey =
  | 'long_strong_buy_restricted_4'
  | 'long_strong_buy_restricted_2'
  | 'short_strong_sell_restricted_4'
  | 'short_strong_sell_restricted_2'
export const RESTRICTED_GROUP_LABEL: Record<RestrictedGroupKey, string> = {
  long_strong_buy_restricted_4: 'Long · Strong Buy (5%)',
  long_strong_buy_restricted_2: 'Long · Strong Buy (2.5%)',
  short_strong_sell_restricted_4: 'Short · Strong Sell (5%)',
  short_strong_sell_restricted_2: 'Short · Strong Sell (2.5%)',
}
// Mirrors modules/backtest.py's RESTRICTED_PCT_4/_2 -- used by
// BacktestingView.tsx's Candidates table to show the TIGHTEST cut a
// long_strong_buy/short_strong_sell ticker's own `pct` actually clears,
// instead of always the plain 10% group label.
export const RESTRICTED_PCT_4 = 0.05
export const RESTRICTED_PCT_2 = 0.025

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
// mean_reversion (the old Reversal Score gate) was removed too --
// backwards on both books (see modules/backtest.py's own removal
// comment). An entry_timing gate (35h hourly formation, see
// modules/derive.py's reconcile_entry_timing) briefly replaced it, then
// was ALSO removed -- explicit instruction: entry_timing was never a
// live gate on RecommendationsView.tsx (deliberately informational-only
// there), so hard-excluding on it here was backtesting a rule the live
// app doesn't actually enforce. The signal itself is unchanged
// (unweighted, informational card line) -- only this module's gate use
// of it is gone.
// growth is SHORT-ONLY (mirroring RecommendationsView.tsx's
// growthBlocksShortEntry exactly -- 10% revenue-growth ceiling) -- added
// after backtest.py was found out of sync with the live gate (S/PANW
// both showed up as clean short_strong_sell weeks with an ugly loss when
// the live app would already refuse to short either today).
// short_interest (a 30%-of-float hard gate) was added the same way, then
// RETIRED again later -- explicit instruction, after a full 5 backtested
// weeks showed it excluded names that compounded +16.95%, almost double
// short_strong_sell's own +9.29%. Folded into scoring.short_interest_rank's
// weight instead, as continuous linear scoring, not a gate -- nothing
// left for this type to name.
// sim_return mirrors simReturnOkForLong/simReturnOkForShort -- only
// checkable GOING FORWARD, since simReturn was never archived into
// sorted_screen <date>.csv until main.py started writing it; any week
// from before that column existed just won't show this reason firing.
// low_vol mirrors RecommendationsView.tsx's lowVolBlocksEntry -- a stock
// whose trailing 1-month annualized price volatility is under 5% is
// blocked on BOTH sides (unlike every other reason here, which is
// side-specific), catching names frozen at/near an acquisition price
// (see modules/derive.py's reconcile_price_volatility).
export type GateReason = 'sim_return' | 'daily_move' | 'trend' | 'growth' | 'earnings' | 'low_vol'

export const GATE_REASON_LABEL: Record<GateReason, string> = {
  sim_return: 'Simulation return (wrong direction)',
  daily_move: 'Daily move beyond ±1σ (3-month)',
  trend: 'Trend filter (no long ≤30, no short ≥70)',
  growth: 'Revenue growth too strong to short (>10%)',
  earnings: 'Earnings within the week',
  low_vol: 'Volatility too low (<5% annualized, likely acquisition-capped)',
}

export interface BacktestTicker {
  ticker: string
  rating: string
  group: GroupKey
  blockedBy: GateReason[]
  sector: string | null // granular industry, straight from that week's sorted_screen.csv row
  return: number // position P&L, same sign convention as GroupStats.return
  // 0 (best)..1 (worst) score percentile that week -- null when it
  // couldn't be reconstructed (see modules/backtest.py's own `pct`
  // comments). Used by BacktestingView.tsx's Candidates table to show
  // the tightest RESTRICTED_PCT_4/_2 cut a long_strong_buy/
  // short_strong_sell ticker actually clears, instead of always (10%).
  pct: number | null
}

// {groups, portfolio, blockedBreakdown, tickers} -- one full classification
// of a week's candidates. BacktestWeek carries two of these: the top-level
// fields (the rating the snapshot actually shipped with that week) and
// `currentModel` (the same week's factor columns re-scored with TODAY's
// modules.scoring -- see modules/backtest.py's _rescore_current_model for
// exactly what that can and can't reconstruct).
export interface BacktestModel {
  groups: Record<GroupKey, GroupStats> & Partial<Record<RestrictedGroupKey, GroupStats>>
  // Gated Strong Buy long leg + gated Strong Sell short leg, summed
  // (dollar-neutral, each leg equal-weight 100% gross). portfolioRestricted4/
  // 2 are the same combination using the (5%)/(2.5%) legs instead of (10%).
  portfolio: { return: number | null; count: number }
  portfolioRestricted4: { return: number | null; count: number }
  portfolioRestricted2: { return: number | null; count: number }
  // Per side, per gate reason that fired at least once this week: the
  // same {return, count} shape as `groups`, restricted to *_blocked rows
  // that failed THAT one reason -- isolates which single rule is behind
  // the group's overall number. Reasons aren't mutually exclusive, so
  // counts here don't sum back to groups.long_blocked/short_blocked's own
  // count. A side/reason with zero hits that week is omitted, not zero.
  blockedBreakdown: Partial<Record<'long' | 'short', Partial<Record<GateReason, GroupStats>>>>
  // Day-by-day returns of the week's holding period (modules/backtest.py's
  // _daily_series): `dates` are the trading days after entry, each series
  // key holds one value per date (null when that portfolio is empty) and
  // adds up to that portfolio's weekly return.
  daily?: BacktestDaily | null
  tickers: BacktestTicker[]
}

export interface BacktestDaily {
  dates: string[]
  series: Record<string, number[] | null>
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
