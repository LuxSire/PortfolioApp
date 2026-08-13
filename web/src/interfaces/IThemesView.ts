// Types for ThemesView.tsx (the Themes tab).

// One entry of data/theme_taxonomy.json.
export interface Theme {
  key: string
  label: string
  description: string
}

// data/ticker_themes.json -- ticker -> [theme keys], hand-curated (see
// ThemesView's own component comment).
export type TickerThemesByTicker = Record<string, string[]>

// sorted_screen.csv-derived name/price lookup, keyed by ticker.
export interface TickerInfo {
  name: string
  price: number | null
}
export type TickerInfoByTicker = Record<string, TickerInfo>

// The live EventSource positions/prices payload.
export interface Position {
  shares?: number
}
export type PositionsByTicker = Record<string, Position>

export interface LiveTick {
  last?: number
}
export type LivePricesByTicker = Record<string, LiveTick>

// One held position row -- see ThemesView's own `rows` useMemo.
export interface PositionRow {
  ticker: string
  name: string | null
  shares: number | undefined
  price: number | null
  value: number | null
  themeKeys: string[]
}

// One theme's exposure bucket -- a Theme plus every tagged holding's own
// allocated share of value (see themeRows' own comment on the even-split
// convention) and the resulting net/gross totals.
export interface ThemeBucket extends Theme {
  tickers: (PositionRow & { allocatedValue: number | null })[]
  netValue: number
  grossValue: number
}
