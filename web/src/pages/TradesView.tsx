import { useEffect, useMemo, useState } from 'react'
import { IB_STREAM_URL } from '../ibStream'
import { parseCSV } from '../csv'
import type { HistoricalTrade, OpenOrder, TickerInfoByTicker, TradeRow, TradesByTicker } from '../interfaces/ITradesView'

// Whole numbers for the vast majority of fills; only show decimals for a
// rare fractional-share trade.
function fmtShares(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '') + v.toLocaleString(undefined, { maximumFractionDigits: Number.isInteger(v) ? 0 : 4 })
}

function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+$' : '-$') + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })
}

function fmtPrice(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return '$' + v.toFixed(2)
}

// Unsigned, whole-number share count -- an order's own totalQuantity/
// filled/remaining are always non-negative regardless of side (unlike a
// fill's signed qty above, side is carried separately by `action`).
function fmtQty(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return v.toLocaleString(undefined, { maximumFractionDigits: Number.isInteger(v) ? 0 : 4 })
}

// trades_by_ticker (see ib_server.py's refresh_trades /
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
function pnlClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return ''
  return v >= 0 ? 'perf-pos' : 'perf-neg'
}

// Buy/sell side gets the same good/bad-adjacent framing PeTable.jsx's
// rating badges use elsewhere in this app -- a visual side cue at a
// glance, not a judgment on the order itself (buying isn't "good" and
// selling isn't "bad", it's just which direction this particular order
// is signed).
function actionClass(action: string): string {
  return action === 'BUY' ? 'perf-pos' : action === 'SELL' ? 'perf-neg' : ''
}

export default function TradesView() {
  const [trades, setTrades] = useState<TradesByTicker>({})
  const [openOrders, setOpenOrders] = useState<OpenOrder[]>([])
  const [tickerInfo, setTickerInfo] = useState<TickerInfoByTicker>({})
  const [history, setHistory] = useState<HistoricalTrade[]>([])
  const [historySymbolFilter, setHistorySymbolFilter] = useState('')

  useEffect(() => {
    fetch('/sorted_screen.csv')
      .then((r) => (r.ok ? r.text() : ''))
      .then((text) => {
        const info: TickerInfoByTicker = {}
        for (const row of parseCSV(text)) {
          info[row.ticker] = { name: row.name }
        }
        setTickerInfo(info)
      })
      .catch(() => {})
  }, [])

  // /trades.json (see ib_server.py's fetch_trades_report) -- the
  // downloaded IBKR Flex Query trade history, separate from "Trades
  // Today"'s live per-ticker aggregate above. Fetched once on mount, not
  // polled -- this file only changes when someone re-runs the Dataset
  // tab's "Past trades" Run button or the CLI form, not continuously the
  // way the live SSE stream does.
  useEffect(() => {
    fetch('/trades.json')
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => setHistory(data?.rows ?? []))
      .catch(() => {})
  }, [])

  // Same live source PositionsView.jsx reads `trades`/`pnl` from --
  // best-effort, no polling; a missing/unreachable server just means an
  // empty list either side.
  useEffect(() => {
    const source = new EventSource(IB_STREAM_URL)
    source.onmessage = (e) => {
      const { trades: tr, openOrders: oo } = JSON.parse(e.data)
      setTrades(tr || {})
      setOpenOrders(oo || [])
    }
    source.onerror = () => {} // EventSource auto-reconnects; nothing to do here.
    return () => source.close()
  }, [])

  const rows: TradeRow[] = Object.entries(trades)
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

  // Working orders first (most likely to move soon), then everything
  // else alphabetically -- there's no dollar value to rank by the way
  // trades' own value-descending sort has, since an order's quantity ×
  // limit price isn't capital actually at risk yet.
  const openOrderRows = [...openOrders].sort((a, b) => {
    if (a.status === 'Submitted' && b.status !== 'Submitted') return -1
    if (b.status === 'Submitted' && a.status !== 'Submitted') return 1
    return a.ticker.localeCompare(b.ticker)
  })

  // Most recent first (tradeID as the tiebreaker within a date, so same-
  // day fills stay in a stable, deterministic order across re-renders
  // instead of shuffling on every fetch).
  const historyRows = useMemo(
    () =>
      [...history]
        .filter((t) => !historySymbolFilter || (t.symbol ?? '').toUpperCase().includes(historySymbolFilter.toUpperCase()))
        .sort((a, b) => (b.date ?? '').localeCompare(a.date ?? '') || (b.tradeID ?? '').localeCompare(a.tradeID ?? '')),
    [history, historySymbolFilter]
  )

  return (
    <div className="positions-page trades-page">
      <header className="masthead">
        <div className="title-block">
          <h1>Trades</h1>
        </div>
      </header>

      {rows.length === 0 && openOrderRows.length === 0 && (
        <div className="asset-card">No trades or working orders today — or ib_server.py isn't running.</div>
      )}

      {(rows.length > 0 || openOrderRows.length > 0) && (
        <div className="asset-two-col-row">
          <div className="asset-card">
            <h2>Open Orders</h2>
            {openOrderRows.length === 0 && <p className="status-row">No working orders right now.</p>}
            {openOrderRows.length > 0 && (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th className="col-left">Ticker</th>
                      <th>Side</th>
                      <th>Type</th>
                      <th>Qty</th>
                      <th>Price</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {openOrderRows.map((o, i) => (
                      <tr key={`${o.ticker}-${i}`}>
                        <td className="col-left">
                          <a
                            href={`#/asset/${encodeURIComponent(o.ticker)}`}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="ticker-link"
                            title={tickerInfo[o.ticker]?.name}
                          >
                            {o.ticker}
                          </a>
                        </td>
                        <td className={`num ${actionClass(o.action)}`}>{o.action}</td>
                        <td className="num">{o.orderType}</td>
                        <td
                          className="num tooltip-cell"
                          data-tip={
                            o.filled !== null && o.remaining !== null
                              ? `Filled: ${fmtQty(o.filled)} · Remaining: ${fmtQty(o.remaining)}`
                              : undefined
                          }
                        >
                          {fmtQty(o.quantity)}
                        </td>
                        <td className="num">{fmtPrice(o.limitPrice ?? o.auxPrice)}</td>
                        <td className="num">{o.status}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="asset-card">
            <h2>Trades Today</h2>
            {rows.length === 0 && <p className="status-row">No fills today.</p>}
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
        </div>
      )}

      <div className="asset-card">
        <div className="trades-history-header">
          <h2>Trade History</h2>
          <input
            type="text"
            className="trades-symbol-filter"
            placeholder="Filter by symbol…"
            value={historySymbolFilter}
            onChange={(e) => setHistorySymbolFilter(e.target.value)}
          />
        </div>
        {history.length === 0 && (
          <p className="status-row">
            No downloaded trade history yet — run the "Past trades (Flex Query)" job on the Dataset tab.
          </p>
        )}
        {history.length > 0 && historyRows.length === 0 && <p className="status-row">No trades match "{historySymbolFilter}".</p>}
        {historyRows.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th className="col-left">Date</th>
                  <th className="col-left">Ticker</th>
                  <th className="col-left col-name">Name</th>
                  <th>Side</th>
                  <th>Qty</th>
                  <th>Price</th>
                  <th>Proceeds</th>
                  <th>Commission</th>
                  <th>Realized P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {historyRows.map((t) => (
                  <tr key={t.tradeID ?? `${t.symbol}-${t.date}-${t.quantity}`}>
                    <td className="col-left">{t.date ?? '—'}</td>
                    <td className="col-left">
                      {t.symbol ? (
                        <a
                          href={`#/asset/${encodeURIComponent(t.symbol)}`}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="ticker-link"
                        >
                          {t.symbol}
                        </a>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td className="col-left col-name">{(t.symbol && tickerInfo[t.symbol]?.name) ?? '—'}</td>
                    <td className={`num ${actionClass(t.buySell ?? '')}`}>{t.buySell ?? '—'}</td>
                    <td className="num">{fmtShares(t.quantity)}</td>
                    <td className="num">{fmtPrice(t.price)}</td>
                    <td className="num">{fmtMoney(t.proceeds)}</td>
                    <td className="num">{fmtMoney(t.commission)}</td>
                    <td className={`num ${pnlClass(t.realizedPnl)}`}>{fmtMoney(t.realizedPnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
