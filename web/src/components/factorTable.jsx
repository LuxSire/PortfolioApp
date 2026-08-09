// Shared factor-averaging data/logic — factored out of FactorCells.jsx
// (rather than exported alongside it) because that file's default export
// is a component; exporting plain constants/functions alongside it breaks
// React Fast Refresh (react-refresh/only-export-components), same reason
// screenerFactors.js exists separately from PeTable.jsx.

// Every screener factor column (see screenerFactors.js's COLUMNS) that
// gets averaged into a group-level row — Price/Target are per-ticker
// quotes with no meaningful cross-ticker average, and Rec/Rating are
// categorical, so neither is part of this at all. Score (sc) is
// deliberately NOT here — a weighted score would let a single large
// weight dominate the group's score the same way it dominates every
// valuation/quality factor, but Score is main.py's own composite ranking
// of a stock, already meant to stand on equal footing; see
// computeFactorAverages' own always-plain-average treatment of it.
export const FACTOR_KEYS = [
  'savgpe', 'fpe', 'feps', 'epsTrend', 'tpe', 'peg', 'revg', 'pfcf', 'evEbitda', 'opMargin', 'de',
  'liq', 'shortInt', 'upside', 'mom', 'mr', 'sent', 'newsSent',
]

// The shared factor-columns tail (Avg PE ... Score) rendered by
// FactorCells.jsx — used for the <th> labels, in the same order
// FactorCells' <td>s render — so every table built on this module
// (Positions' Long/Short rows, the Sectors tab's sector/industry/asset
// rows) shows identical columns in identical order. Each page adds its
// own leading columns (Ticker/Name/Pos Value for Positions; Group/#
// Assets for Sectors) before this shared tail.
export const FACTOR_COLUMNS = [
  { key: 'savgpe', label: 'Avg PE' },
  { key: 'fpe', label: 'Fwd PE' },
  { key: 'feps', label: 'Fwd EPS' },
  { key: 'epsTrend', label: 'EPS Trend' },
  { key: 'tpe', label: 'Trail PE' },
  { key: 'peg', label: 'PEG' },
  { key: 'revg', label: 'Rev Growth' },
  { key: 'pfcf', label: 'P/FCF' },
  { key: 'evEbitda', label: 'EV/EBITDA' },
  { key: 'opMargin', label: 'Op Margin' },
  { key: 'de', label: 'D/E' },
  { key: 'liq', label: 'Liq Ratio' },
  { key: 'shortInt', label: 'Short Interest' },
  { key: 'upside', label: 'Upside' },
  { key: 'mom', label: 'Momentum' },
  { key: 'mr', label: 'MeanRev' },
  { key: 'sent', label: 'Sentiment' },
  { key: 'newsSent', label: 'News' },
  { key: 'sc', label: 'Score' },
]

// Weighted (or, with weightFn returning 1 for every row, plain) average of
// every FACTOR_KEYS field across `rows`, plus Score as its own always-
// plain average regardless of weightFn (see FACTOR_KEYS' own comment on
// why) and the total weight actually used (sumWeight — 0 if every row
// had weight 0 or there were no rows). A row missing a given field is
// excluded from just that field's average, not the row entirely.
export function computeFactorAverages(rows, weightFn) {
  const sums = {}
  const weights = {}
  for (const key of FACTOR_KEYS) {
    sums[key] = 0
    weights[key] = 0
  }
  let sumWeight = 0
  let scoreSum = 0
  let scoreCount = 0
  for (const r of rows) {
    if (r.sc !== null && r.sc !== undefined && Number.isFinite(r.sc)) {
      scoreSum += r.sc
      scoreCount += 1
    }
    const w = weightFn(r)
    if (!w) continue
    sumWeight += w
    for (const key of FACTOR_KEYS) {
      const v = r[key]
      if (v === null || v === undefined || !Number.isFinite(v)) continue
      sums[key] += w * v
      weights[key] += w
    }
  }
  const factors = {}
  for (const key of FACTOR_KEYS) factors[key] = weights[key] ? sums[key] / weights[key] : null
  factors.sc = scoreCount ? scoreSum / scoreCount : null
  return { factors, count: rows.length, sumWeight }
}
