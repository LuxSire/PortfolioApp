// Types for SimulationsView.tsx (the Simulations tab) -- sourced from
// data/output/simulations.json (see modules/simulations.py's own docstring
// for the formula), same "screener table over a single JSON file" shape as
// ScreenerView.tsx's own sorted_screen.csv, just with a nested rather than
// flat source shape (each ticker's raw JSON entry is flattened into one
// SimRow -- see SimulationsView.tsx's own parse step).

// One ticker's raw simulate_ticker() output, as it actually sits in
// simulations.json -- either a full result or just an {error} when a
// required input field (forwardEps/price/forwardPE) was missing.
export interface RawSimResult {
  ticker: string
  error?: string
  name?: string | null
  sector?: string | null
  forecastPrice?: number | null
  forecastReturn?: number | null
  currentPrice?: number
  inputs?: {
    muEps: number
    sigmaEps: number
    epsVolatilitySource: string
    epsTrend: number | null
    revenueGrowth: number | null
    confidence: number
    ownPe: number
    industryMedianPe: number | null
    peerCount: number
    peLevel: 'industry' | 'sector' | null
  }
  // The single priced scenario (at industryMedianPe) -- null/absent when
  // even the broad sector didn't clear MIN_PEERS (no peer multiple to
  // price against at all; see modules/simulations.py's own docstring).
  priceAtIndustryMultiple?: SimPriceStats | null
}

export interface SimPriceStats {
  mean: number
  median: number
  stdev: number
  p5: number
  p25: number
  p50: number
  p75: number
  p95: number
  probAboveCurrentPrice: number
}

// Flattened, sortable shape SimulationsView.tsx actually renders -- one
// row per ticker that both simulated successfully AND had an industry/
// sector peer group to price against (error rows, and rows with no
// priceAtIndustryMultiple, are dropped entirely -- see that page's own
// parse step).
export interface SimRow {
  t: string
  n: string
  s: string
  price: number
  forecastPrice: number | null
  forecastReturn: number | null
  muEps: number
  sigmaEpsPct: number | null
  epsVolSource: string
  ownPe: number
  industryPe: number | null
  peerCount: number
  peLevel: 'industry' | 'sector' | null
  epsTrend: number | null
  revenueGrowth: number | null
  confidence: number
  industryMedian: number
  industryReturn: number
  industryP5: number
  industryP95: number
  industryProbAbove: number
}
