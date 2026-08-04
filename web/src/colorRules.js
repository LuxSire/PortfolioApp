// Shared red/green threshold rules for stat coloring, used by both the
// screener table (App.jsx) and the per-asset page (Asset.jsx). Each function
// returns 'good', 'bad', or '' (neutral) for a numeric value; callers map
// that straight onto a CSS class (index.css defines both `.good`/`.bad` and
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
