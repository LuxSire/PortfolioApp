// Timing of the simulated mispricing (forecastReturn) -- explicit instruction.
//
// The gap is an expectation (taken as is, no haircut) and is repriced on earnings
// dates: 6 quarterly earnings events starting at the stock's NEXT earnings date. That
// first date already carries 1/3 of the gap; the other 5 share the remaining 2/3
// equally (2/15 each). Between events the price does not move toward the gap.
//
// expectedReturnOverDays(gap, d1, 365) = the part of the gap realized within the next
// year -- the figure the Positions page's Expected Portfolio Performance uses, so it
// sits on the same one-year basis as the annualized volatility in the Sharpe.

export const REPRICING_EVENTS = 6
export const FIRST_EVENT_SHARE = 1 / 3
const QUARTER_DAYS = 91
const DEFAULT_DAYS_TO_NEXT_EARNINGS = 45 // no earnings date on file: mid-quarter

// Days from `nowSec` to the next earnings date. earningsTimestampStart is unix seconds;
// an already-past date is rolled forward by whole quarters (the next report).
export function daysToNextEarnings(earningsTs: number | null | undefined, nowSec = Date.now() / 1000): number {
  if (!earningsTs || !Number.isFinite(earningsTs)) return DEFAULT_DAYS_TO_NEXT_EARNINGS
  let d = (earningsTs - nowSec) / 86400
  while (d < 0) d += QUARTER_DAYS
  return d
}

// Fraction of the gap realized within `horizonDays`, given d1 days to the first event.
export function realizedFraction(d1: number, horizonDays = 365): number {
  const rest = (1 - FIRST_EVENT_SHARE) / (REPRICING_EVENTS - 1)
  let f = 0
  for (let k = 0; k < REPRICING_EVENTS; k++) {
    if (d1 + k * QUARTER_DAYS <= horizonDays) f += k === 0 ? FIRST_EVENT_SHARE : rest
  }
  return f
}

export function expectedReturnOverDays(gap: number, d1: number, horizonDays = 365): number {
  return gap * realizedFraction(d1, horizonDays)
}
