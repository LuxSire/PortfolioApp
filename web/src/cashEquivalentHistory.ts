import { useEffect, useMemo, useState } from 'react'
import { useCashEquivalents } from './nonEquityHoldings'

// Daily market value of the CASH-EQUIVALENT holdings (data/cash.json: IB01,
// SGOV, SHV, ...) so the Exposure bars on the Portfolio and Factsheet pages can
// take them OUT of Long and show them with the cash instead -- explicit
// instruction. The Flex Query's equity summary only has totals (stockLong
// includes these ETFs), so the history is rebuilt from the fills in
// /trades.json: shares held at a date = signed fills up to and including that
// date (this account's cash-equivalent shares are fully explained by the fills
// on file), valued at the last fill price on or before that date (a
// T-bill ETF barely moves, so the error is a fraction of a percent).
interface Fill {
  symbol: string | null
  date: string | null
  quantity: number | null
  price: number | null
}

export function cashEquivalentValueByDate(dates: string[], fills: Fill[], cashTickers: Set<string>): Record<string, number> {
  const bySymbol = new Map<string, Fill[]>()
  for (const f of fills) {
    if (!f.symbol || !f.date || f.quantity === null || f.price === null || !cashTickers.has(f.symbol)) continue
    const list = bySymbol.get(f.symbol) ?? []
    list.push(f)
    bySymbol.set(f.symbol, list)
  }
  for (const list of bySymbol.values()) list.sort((a, b) => (a.date as string).localeCompare(b.date as string))
  const out: Record<string, number> = {}
  for (const date of dates) {
    let total = 0
    for (const list of bySymbol.values()) {
      let qty = 0
      let price = 0
      for (const f of list) {
        if ((f.date as string) > date) break
        qty += f.quantity as number
        price = f.price as number
      }
      if (qty) total += qty * price
    }
    out[date] = total
  }
  return out
}

// {date: value of cash-equivalent holdings} for the given row dates; {} until
// /trades.json (and /cash.json) load, or if they're unavailable.
export function useCashEquivalentValues(dates: string[]): Record<string, number> {
  const cashTickers = useCashEquivalents()
  const [fills, setFills] = useState<Fill[]>([])
  useEffect(() => {
    fetch('/trades.json')
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d) => setFills(d?.rows ?? []))
      .catch(() => {})
  }, [])
  const key = dates.join(',')
  // eslint-disable-next-line react-hooks/exhaustive-deps
  return useMemo(() => cashEquivalentValueByDate(dates, fills, cashTickers), [key, fills, cashTickers])
}
