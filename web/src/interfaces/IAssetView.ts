// Types for AssetView.tsx (the single-ticker asset page) — kept in their own
// file per this project's convention of separating type declarations from
// the components that use them, one file per component.

// The yfinance raw_data.json payload for one ticker -- Record<string,
// unknown> rather than an exhaustively-typed interface, since every field
// is optional/uncertain from an external source and every access already
// goes through a formatter (fmtPrice/fmtPct/fmtNum/fmtValue) that safely
// narrows via typeof at the point of use -- the same runtime-checked
// looseness this file's plain-JS predecessor had, just with that
// looseness made explicit in the type instead of implicit.
export type AssetInfo = Record<string, unknown>

// One row of data/sec/13f/institutional_holders.json's per-ticker array
// (see sec_edgar.py's fetch_13f_holdings). valueUsd/shares are common-stock
// only; putShares/callShares are separate options exposure (SEC's 13F
// schema has no long/short indicator, so these mean "disclosed option
// exposure of this type," not bullish/bearish -- see sec_edgar.py's
// module docstring) and can be nonzero even when shares is 0 (an
// options-only filer).
export interface Holder {
  name: string
  valueUsd: number
  shares: number
  putShares: number
  callShares: number
}

// One entry from GET /api/news (ib_price_server.py).
export interface NewsArticle {
  articleId: string
  time: string
  provider: string
  headline: string
  sentiment: number | null
}

// One point of data/price_history.json (yfinance daily closes).
export interface PricePoint {
  date: string
  close: number
}

// One bar of data/price_history_hourly.json or
// data/price_history_daily_3mo.json (IB Gateway historical bars).
export interface CandlePoint {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}
