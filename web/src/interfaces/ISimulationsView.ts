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
    blendedPe: number | null
    peerCount: number
    peLevel: 'industry' | 'sector' | null
  }
  priceAtCurrentMultiple?: SimPriceStats
  priceAtBlendedMultiple?: SimPriceStats | null
  comparison?: {
    medianDiff: number
    medianDiffPct: number | null
    peMultipleRatio: number
    confidence: number
    discountedMedianDiff: number
    discountedMedianDiffPct: number | null
  } | null
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
// row per ticker that simulated successfully (error rows are dropped
// entirely, see that page's own parse step). Null fields are the
// industry-median half of the model when a ticker didn't have enough
// same-industry peers (see modules/simulations.py's MIN_PEERS).
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
  peRatio: number | null
  epsTrend: number | null
  revenueGrowth: number | null
  confidence: number
  curMedian: number
  curReturn: number
  curProbAbove: number
  indMedian: number | null
  indReturn: number | null
  indP5: number | null
  indP95: number | null
  indProbAbove: number | null
  medianDiff: number | null
  medianDiffPct: number | null
  discountedMedianDiff: number | null
  discountedMedianDiffPct: number | null
}
