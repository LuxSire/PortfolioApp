import { useEffect, useState } from 'react'
import { IB_STREAM_URL } from '../ibStream'
import { parseCSV } from '../csv'

// Whole numbers for the vast majority of fills; only show decimals for a
// rare fractional-share trade.
function fmtShares(v) {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '') + v.toLocaleString(undefined, { maximumFractionDigits: Number.isInteger(v) ? 0 : 4 })
}

function fmtMoney(v) {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+$' : '-$') + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })
}

function fmtPrice(v) {
  if (v === null || v === undefined) return '—'
  return '$' + v.toFixed(2)
}

// trades_by_ticker (see ib_price_server.py's refresh_trades /
// IBApp.get_today_executions_async) is already a net-per-ticker
// aggregate of today's fills -- {ticker: {qty, value, realizedPnl,
// commission}} -- not a list of individual executions; that method
// deliberately only keeps the net signed quantity and its matching cost
// (plus, separately, IB's own FIFO-cost-basis realizedPnl/commission),
// not each fill separately. So this is one row per ticker traded today,
// not one row per fill -- "the list of trades" this app actually has to
// show. qty/value are left unsigned (no good/bad color) since a sell
// isn't inherently a worse outcome than a buy -- taking profit on a
// winner is a sell too. realizedPnl IS signed/colored: unlike qty/value,
// a positive figure there is unambiguously a gain. null (not 0) for
// realizedPnl/commission means this connection wasn't alive to see that
// fill live (see get_today_executions_async's own docstring) -- a real
// $0 realized (e.g. a trade that only added to a position, closing
// nothing) is a meaningful reading, kept distinct from "unknown."
function pnlClass(v) {
  if (v === null || v === undefined) return ''
  return v >= 0 ? 'perf-pos' : 'perf-neg'
}

export default function TradesView() {
  const [trades, setTrades] = useState({})
  const [tickerInfo, setTickerInfo] = useState({})

  useEffect(() => {
    fetch('/sorted_screen.csv')
      .then((r) => (r.ok ? r.text() : ''))
      .then((text) => {
        const info = {}
        for (const row of parseCSV(text)) {
          info[row.ticker] = { name: row.name }
        }
        setTickerInfo(info)
      })
      .catch(() => {})
  }, [])

  // Same live source PositionsView.jsx reads `trades` from — best-effort,
  // no polling; a missing/unreachable server just means an empty list.
  useEffect(() => {
    const source = new EventSource(IB_STREAM_URL)
    source.onmessage = (e) => {
      const { trades: tr } = JSON.parse(e.data)
      setTrades(tr || {})
    }
    source.onerror = () => {} // EventSource auto-reconnects; nothing to do here.
    return () => source.close()
  }, [])

  const rows = Object.entries(trades)
    .map(([ticker, t]) => ({
      ticker,
      name: tickerInfo[ticker]?.name ?? null,
      qty: t.qty,
      value: t.value,
      avgPrice: t.qty ? t.value / t.qty : null,
      realizedPnl: t.realizedPnl ?? null,
      commission: t.commission ?? null,
    }))
    .sort((a, b) => Math.abs(b.value ?? 0) - Math.abs(a.value ?? 0))

  return (
    <div className="positions-page trades-page">
      <header className="masthead">
        <div className="title-block">
          <h1>Trades</h1>
        </div>
      </header>

      {rows.length === 0 && (
        <div className="asset-card">No trades today — or ib_price_server.py isn't running.</div>
      )}

      {rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className="col-left">Ticker</th>
                <th className="col-left col-name">Name</th>
                <th>Qty</th>
                <th>Avg Price</th>
                <th>Value</th>
                <th title="IB's own FIFO-cost-basis realized P&amp;L for today's fills — '—' means this connection wasn't alive to see the fill live, not that it was exactly $0.">
                  Realized P&amp;L
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.ticker}>
                  <td className="col-left">
                    <a
                      href={`#/asset/${encodeURIComponent(r.ticker)}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="ticker-link"
                    >
                      {r.ticker}
                    </a>
                  </td>
                  <td className="col-left col-name">{r.name ?? '—'}</td>
                  <td className="num">{fmtShares(r.qty)}</td>
                  <td className="num">{fmtPrice(r.avgPrice)}</td>
                  <td className="num">{fmtMoney(r.value)}</td>
                  <td
                    className={`num tooltip-cell ${pnlClass(r.realizedPnl)}`}
                    data-tip={r.commission !== null ? `Commission: ${fmtMoney(-r.commission)}` : undefined}
                  >
                    {fmtMoney(r.realizedPnl)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
