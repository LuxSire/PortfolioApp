// Types for ScoringView.tsx (the Scoring tab) — one entry from GET
// /api/scoring-formula (see ib_server.py's _handle_scoring_formula /
// scoring.py's FACTOR_WEIGHTS).
export interface ScoringFactor {
  key: string
  label: string
  // Weight [0, 1] applied to this factor for a ticker outside every
  // special sector below / a Financials-sector ticker / a Utilities-
  // sector ticker / a Real-Estate-sector ticker respectively (see
  // scoring.py's STANDARD_WEIGHTS/FINANCIALS_WEIGHTS/UTILITIES_WEIGHTS/
  // REAL_ESTATE_WEIGHTS and is_financials_sector/is_utilities_sector/
  // is_real_estate_sector). Each column sums to 1.0 across every factor.
  standardWeight: number
  financialsWeight: number
  utilitiesWeight: number
  realEstateWeight: number
}

// One row of the Rating Breakdown table -- sorted_screen.csv's `rating`
// column (see main.py's rating_for_percentile), counted per `sector`
// (really an industry, e.g. "Banks - Regional" -- see scoring.py's own
// module docstring on that naming). na counts a priced ticker with a
// non-positive forwardPE (main.py's write_sorted_screen_csv appends
// these unranked, RATING_NA, never scored at all) -- worth keeping as
// its own column rather than dropping it, since a sector where most
// tickers never even reach scoring (e.g. Biotechnology) is itself part
// of the picture, not just noise to filter out.
export interface SectorRatingRow {
  sector: string
  strongBuy: number
  buy: number
  hold: number
  sell: number
  strongSell: number
  na: number
  total: number
  scored: number
  buyPct: number
  sellPct: number
}
