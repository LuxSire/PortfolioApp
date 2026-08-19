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
