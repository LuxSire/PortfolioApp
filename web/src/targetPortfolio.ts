import { useEffect, useState } from 'react'

// Tickers in the current target portfolio (data/output/target_portfolio.json:
// the optimiser's final longs + shorts) -- used by the Screener and
// Simulations tables to colour a row dark green (tr.row-target) when a stock
// is a target-portfolio pick you don't already hold. Best-effort: a missing
// file just means no green rows.
export function useTargetPortfolioTickers(): Set<string> {
  const [tickers, setTickers] = useState<Set<string>>(new Set())
  useEffect(() => {
    fetch('/target_portfolio.json')
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((p: { longs?: { ticker: string }[]; shorts?: { ticker: string }[] }) =>
        setTickers(new Set([...(p.longs ?? []), ...(p.shorts ?? [])].map((x) => x.ticker)))
      )
      .catch(() => {})
  }, [])
  return tickers
}

// Row colouring shared by the Screener and Simulations tables: a position in
// the account gets the same background the Target page uses for held rows
// (tr.row-held); a target-portfolio pick you don't hold gets the dark green
// the recommendation cards use (tr.row-target).
export function portfolioRowClass(held: boolean, inTarget: boolean): string {
  return held ? 'row-held' : inTarget ? 'row-target' : ''
}
