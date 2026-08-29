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
  // SimPrice -- mean of the simulated-path Monte Carlo (see modules/
  // simulations.py's own comment on the simulated-path block), sourced
  // from a FIXED multiple, only the EPS side is randomized. Deliberately
  // NOT confidence-pulled toward currentPrice the way forecastPrice is --
  // see that module's own comment on why. simPriceDistribution is the
  // full mean/median/stdev/percentile block that mean comes from, same
  // shape as priceAtIndustryMultiple below.
  simPrice?: number | null
  simReturn?: number | null
  // Modified (Israelsen 2005) Sharpe ratio of the simulated-path return
  // distribution -- see modules/simulations.py's "SIMULATED-PATH FORMULA"
  // section for the formula (handles negative excess returns correctly,
  // unlike the plain excess_return/vol ratio).
  simSharpe?: number | null
  simPriceDistribution?: SimPriceStats | null
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
  p20: number
  p50: number
  p80: number
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
  simPrice: number | null
  simVol: number | null
  simSharpe: number | null
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
  industryP20: number
  industryP80: number
  industryProbAbove: number
}
