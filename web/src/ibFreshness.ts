// Shared "is IB's own price data current" logic -- used by
// RecommendationsView.tsx's per-ticker PreviousCloseFlag AND
// IbFreshnessBadge.tsx's app-wide top-bar flag, so the two can never
// silently disagree about what "current" means.

// Mirrors scoring.py's own most_recent_completed_trading_day() exactly --
// yesterday, rolled back over the weekend. Not a real market-holiday
// calendar, same "good enough, don't over-engineer it" spirit as that
// function's own docstring.
export function mostRecentCompletedTradingDay(): string {
  const d = new Date()
  d.setDate(d.getDate() - 1)
  while (d.getDay() === 0 || d.getDay() === 6) {
    d.setDate(d.getDate() - 1)
  }
  return d.toISOString().slice(0, 10)
}
