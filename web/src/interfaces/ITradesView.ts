// Types for TradesView.tsx (the Trades tab).

// One ticker's net today's-fills aggregate from the live EventSource
// stream (see ib_price_server.py's refresh_trades /
// IBApp.get_today_executions_async) -- already netted per ticker, not a
// list of individual executions. realizedPnl/commission null (not 0)
// means this connection wasn't alive to see that fill live.
export interface Trade {
  qty: number
  value: number
  realizedPnl?: number | null
  commission?: number | null
}
export type TradesByTicker = Record<string, Trade>

// sorted_screen.csv-derived name lookup, keyed by ticker.
export interface TickerInfo {
  name: string
}
export type TickerInfoByTicker = Record<string, TickerInfo>

// One row of the Trades table -- a Trade merged with its tickerInfo name
// and a derived average fill price.
export interface TradeRow {
  ticker: string
  name: string | null
  qty: number
  value: number
  avgPrice: number | null
  realizedPnl: number | null
  commission: number | null
}
