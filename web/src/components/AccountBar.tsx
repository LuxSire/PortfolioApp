import { useEffect, useState } from 'react'
import { IB_STREAM_URL } from '../ibStream'

// App-wide account summary, mounted ONCE in App.jsx between the tab bar and
// the active page (so it sits in the same place on every tab and every page
// starts below it). Owns its own IB stream subscription; same tags/order as
// IBApp.ACCOUNT_STATUS_TAGS, P&L fields get the good/bad sign coloring.
const ACCOUNT_FIELDS: { tag: string; label: string; signed?: boolean }[] = [
  { tag: 'NetLiquidation', label: 'Net Liquidation' },
  { tag: 'TotalCashValue', label: 'Total Cash' },
  { tag: 'AvailableFunds', label: 'Available Funds' },
  { tag: 'ExcessLiquidity', label: 'Excess Liquidity' },
  { tag: 'BuyingPower', label: 'Buying Power' },
  { tag: 'UnrealizedPnL', label: 'Unrealized P&L', signed: true },
  { tag: 'RealizedPnL', label: 'Realized P&L', signed: true },
]

function fmtMoney(v: number): string {
  return '$' + v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

export default function AccountBar() {
  const [account, setAccount] = useState<Record<string, number>>({})

  useEffect(() => {
    const source = new EventSource(IB_STREAM_URL)
    source.onmessage = (e) => {
      const { account: acc } = JSON.parse(e.data)
      setAccount(acc || {})
    }
    source.onerror = () => {} // EventSource auto-reconnects; nothing to do here.
    return () => source.close()
  }, [])

  if (Object.keys(account).length === 0) return null
  return (
    <div className="asset-card account-bar">
      <h2>Account</h2>
      <div className="asset-stat-grid">
        {ACCOUNT_FIELDS.filter((f) => account[f.tag] !== undefined).map((f) => {
          const v = account[f.tag]
          const valueClass = f.signed ? (v >= 0 ? 'good' : 'bad') : undefined
          return (
            <div className="asset-stat" key={f.tag}>
              <span className={`n num${valueClass ? ` ${valueClass}` : ''}`}>{fmtMoney(v)}</span>
              <span className="l">{f.label}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
