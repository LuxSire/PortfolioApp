// Types for HoldersView.tsx (the Holders tab).

// One row of data/sec/13f/institutional_holders.json's per-ticker array
// (see sec_edgar.py's fetch_13f_holdings) -- the raw, un-inverted shape.
export interface RawHolder {
  name: string
  valueUsd: number
  shares: number
}

export type HoldersByTicker = Record<string, RawHolder[]>

// The subset of raw_data.json's per-ticker yfinance payload this page
// actually reads -- see Asset.tsx's own AssetInfo for why the full
// payload stays a loose Record<string, unknown> rather than an
// exhaustive interface; here only sharesOutstanding/name fields matter.
export interface RawTickerInfo {
  sharesOutstanding?: number
  shortName?: string
  longName?: string
}

export type RawDataByTicker = Record<string, RawTickerInfo>

// One institution's position in one ticker, after inverting
// HoldersByTicker -- see buildInstitutionCards.
export interface Holding {
  ticker: string
  name: string | null
  shares: number
  valueUsd: number
  pctOwned: number | null
}

// One institution, with every tracked ticker it holds (not capped --
// HolderCard/HolderModal each decide their own slice of this array).
export interface Institution {
  name: string
  holdings: Holding[]
}
