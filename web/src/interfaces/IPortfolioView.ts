// Types for PortfolioView.tsx (the Portfolio tab) — one day's row from
// data/portfolio_performance.json (see ib_price_server.py's
// fetch_account_performance / _parse_portfolio_csv), and the file's own
// top-level shape. Also used by NavChart.jsx/ExposureChart.jsx/
// MonthlyReturnsTable.jsx (still plain JS, so untyped at their own
// boundary, but this is the shape PortfolioView.tsx passes them).
export interface PortfolioDayRow {
  date: string
  cash: number | null
  nav: number | null
  stockLong: number | null
  stockShort: number | null
  stockNet: number | null
  stockGross: number | null
  depositsWithdrawals: number | null
  commissions: number | null
  dividends: number | null
  interest: number | null
  realized: number | null
  unrealized: number | null
}

export interface PortfolioPerformanceData {
  kind: string
  rows?: PortfolioDayRow[]
}
