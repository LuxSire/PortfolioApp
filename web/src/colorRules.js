// Shared red/green threshold rules for stat coloring, used by both the
// screener table (App.jsx) and the per-asset page (Asset.jsx). Each function
// returns 'good', 'bad', or '' (neutral) for a numeric value; callers map
// that straight onto a CSS class (styles.scss defines both `.good`/`.bad` and
// `td.good`/`td.bad` so it works as either a <span> or <td> class).

// Green below goodBelow, red above badAbove, neutral in between. For
// cheap-below/expensive-above multiples: P/E (fwd/trailing/current-year),
// P/FCF, PEG (goodBelow = badAbove = 1), Price/Book, EV/EBITDA, EV/Revenue,
// Short Ratio. Negative is always red, never green — a negative P/E, P/FCF,
// PEG, etc. means negative earnings/cash flow/growth, not "cheap".
export function rangeClass(v, goodBelow, badAbove) {
  if (typeof v !== 'number') return ''
  if (v < 0) return 'bad'
  if (v < goodBelow) return 'good'
  if (v > badAbove) return 'bad'
  return ''
}

// Mirror of rangeClass for metrics where LOW is bad and HIGH is good, e.g.
// Liq Ratio (quick/current ratio average).
export function inverseRangeClass(v, badBelow, goodAbove) {
  if (typeof v !== 'number') return ''
  if (v < badBelow) return 'bad'
  if (v > goodAbove) return 'good'
  return ''
}

// Below 1 means current liabilities aren't fully covered — red; otherwise
// green. Used for Quick Ratio / Current Ratio.
export function belowOneClass(v) {
  if (typeof v !== 'number') return ''
  return v < 1 ? 'bad' : 'good'
}

// v is a fraction (e.g. 0.18 = 18%); thresholds are given in percent. Red
// below the weak threshold, green above the strong one. Used for ROA/ROE.
export function inversePctThresholdClass(v, badBelowPct, goodAbovePct) {
  if (typeof v !== 'number') return ''
  return inverseRangeClass(v * 100, badBelowPct, goodAbovePct)
}

// Red below last/reference price, green above. Used for analyst price targets.
export function targetClass(target, lastPrice) {
  if (typeof target !== 'number' || typeof lastPrice !== 'number') return ''
  if (target > lastPrice) return 'good'
  if (target < lastPrice) return 'bad'
  return ''
}

// Daily-timeframe (LT) "strength" -- IBApp's Money Flow Index (or RSI on
// the yfinance-fallback tier), bounded [0, 100]. Green at/above 60 (the
// sweet-spot curve's own peak -- see scoring.py's momentum_rank), red
// at/below 30 (its own oversold line), neutral in between -- same
// Explicit instruction: oversold (<30) green, overbought (>70) red,
// neutral (neither extreme) uncolored -- a mean-reversion read of the
// raw Money Flow Index/RSI value itself, same 30/70 bounds
// RecommendationsView.tsx's own MOMENTUM_OVERSOLD/MOMENTUM_OVERBOUGHT
// use, kept in sync by hand. Not direction/side-dependent the way that
// page's Long/Short-specific signal is -- the Screener has no long/short
// concept, this is just "is this reading at an extreme" at a glance.
export function momentumClass(v) {
  if (typeof v !== 'number') return ''
  if (v < 30) return 'good'
  if (v > 70) return 'bad'
  return ''
}

// Hourly-timeframe (ST) overbought/oversold -- IBApp's hourly Money Flow
// Index, bounded [0, 100]. Same oversold/overbought treatment as
// momentumClass above, same 30/70 bounds -- explicit instruction.
export function meanReversionClass(v) {
  if (typeof v !== 'number') return ''
  if (v < 30) return 'good'
  if (v > 70) return 'bad'
  return ''
}

// Positions page: Asset/Security column font color, based solely on the
// Daily % (NAV) column -- green above +0.1%, red below -0.1% -- explicit
// correction, replacing an earlier version keyed off overall gain
// (pnlPct) instead. dayPnl (IBKR's own reqPnLSingle figure) divided by
// the whole account's Net Liquidation value answers "how much did this
// position move MY WHOLE PORTFOLIO today", robust to a position opened
// intraday (unlike a plain day-over-day PRICE % change, which has no
// valid prior-close to compare against for a same-day open). Already
// correctly signed by IBKR for either side (a short's loss is already
// negative), so no Long/Short sign flip needed here.
export function assetPnlClass(dayPnl, netLiq) {
  if (typeof dayPnl !== 'number' || typeof netLiq !== 'number' || netLiq <= 0) return ''
  const ratio = dayPnl / netLiq
  if (ratio > 0.001) return 'good'
  if (ratio < -0.001) return 'bad'
  return ''
}
