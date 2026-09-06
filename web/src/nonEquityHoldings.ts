import { useEffect, useState } from 'react'

// Tickers that IBKR reports as account positions but which are NOT
// directional equity holdings -- cash / short-term-Treasury equivalents
// (IB01 = IBKR's cash sweep, SGOV = 0-3mo T-bill ETF, ...). They must be
// kept out of every exposure / long-short / beta / vol figure in the
// frontend and shown in their own "Bonds & cash equivalents" table.
//
// The live list is data/cash.json (served at /cash.json -- hand-edit that
// file to add or remove one, no rebuild needed). DEFAULT_CASH_TICKERS is
// only the fallback used before the fetch resolves or if it fails.
export const DEFAULT_CASH_TICKERS = ['IB01', 'SGOV'] as const

let cachedPromise: Promise<Set<string>> | null = null

function loadCashTickers(): Promise<Set<string>> {
  if (!cachedPromise) {
    cachedPromise = fetch('/cash.json')
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        const list: unknown = Array.isArray(j) ? j : j?.tickers
        return Array.isArray(list) && list.every((t) => typeof t === 'string')
          ? new Set<string>(list as string[])
          : new Set<string>(DEFAULT_CASH_TICKERS)
      })
      .catch(() => new Set<string>(DEFAULT_CASH_TICKERS))
  }
  return cachedPromise
}

// React hook: the set of cash-equivalent tickers, seeded with the default
// and replaced once /cash.json loads (fetched once per session, cached).
export function useCashEquivalents(): Set<string> {
  const [tickers, setTickers] = useState<Set<string>>(() => new Set(DEFAULT_CASH_TICKERS))
  useEffect(() => {
    let alive = true
    loadCashTickers().then((s) => {
      if (alive) setTickers(s)
    })
    return () => {
      alive = false
    }
  }, [])
  return tickers
}

// Non-hook check against the DEFAULT list only (for module-level / non-React
// call sites). Prefer useCashEquivalents() inside components so a cash.json
// edit is picked up.
export function isNonEquityHolding(ticker: string | null | undefined): boolean {
  return ticker != null && (DEFAULT_CASH_TICKERS as readonly string[]).includes(ticker)
}
