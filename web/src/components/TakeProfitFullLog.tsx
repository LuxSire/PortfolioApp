import { useEffect, useMemo, useState } from 'react'
import { IB_TAKE_PROFIT_FULL_LOG_URL } from '../ibStream'

// Trades page: the COMPLETE log of the trading robot (ib_server.py's
// take_profit_loop) -- every check, window change, skipped name, order, entry
// signal (log only, never sent), IB status change and fill, newest first, in a
// tall scrolling window. The small Positions-page panel shows only the 3
// newest events; this is the long version, read from the on-disk activity log
// (so it spans restarts). Polls GET /api/take-profit/log?full=1 every 30 s.

interface Event {
  time: string
  kind: string
  message: string
}

const LABEL: Record<string, string> = {
  status: 'status',
  window: 'window',
  check: 'check',
  skip: 'waiting',
  order: 'ORDER',
  entry: 'ENTRY (not sent)',
  entry_skip: 'ENTRY skipped',
  beta: 'beta',
  ib: 'IB',
  fill: 'FILLED',
  error: 'ERROR',
}

const FILTERS: { key: string; label: string; kinds: string[] | null }[] = [
  { key: 'all', label: 'Everything', kinds: null },
  { key: 'orders', label: 'Orders, entries & fills', kinds: ['order', 'entry', 'entry_skip', 'ib', 'fill'] },
  { key: 'entries', label: 'Entry signals only', kinds: ['entry', 'entry_skip'] },
  { key: 'problems', label: 'Errors & waiting', kinds: ['error', 'skip'] },
]

function fmtTime(iso: string): string {
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2}:\d{2})/)
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}` : iso
}

export default function TakeProfitFullLog() {
  const [events, setEvents] = useState<Event[] | null>(null)
  const [error, setError] = useState(false)
  const [filter, setFilter] = useState('all')

  useEffect(() => {
    let cancelled = false
    const load = () =>
      fetch(IB_TAKE_PROFIT_FULL_LOG_URL)
        .then((r) => (r.ok ? r.json() : Promise.reject()))
        .then((d: { events: Event[] }) => {
          if (cancelled) return
          setEvents(d.events ?? [])
          setError(false)
        })
        .catch(() => !cancelled && setError(true))
    load()
    const id = setInterval(load, 30000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  const shown = useMemo(() => {
    const kinds = FILTERS.find((f) => f.key === filter)?.kinds
    return (events ?? []).filter((e) => !kinds || kinds.includes(e.kind))
  }, [events, filter])

  return (
    <div className="asset-card trading-robot-full-log">
      <h2 title="Everything the background trading robot did, from data/IB/take_profit_activity.log. Order lines are also in data/IB/orders_sent_log.log.">
        Trading robot — full log
      </h2>
      {error && (
        <p className="status-row">
          Couldn't read the robot log — is ib_server.py running (and restarted since the full-log endpoint was added)?
        </p>
      )}
      {!error && events === null && <p className="status-row">Loading…</p>}
      {events !== null && (
        <>
          <label className="take-profit-log-filter">
            Show{' '}
            <select value={filter} onChange={(e) => setFilter(e.target.value)}>
              {FILTERS.map((f) => (
                <option key={f.key} value={f.key}>
                  {f.label}
                </option>
              ))}
            </select>{' '}
            · {shown.length} of {events.length} events
          </label>
          <div className="trading-robot-full-log-scroll">
            <table>
              <thead>
                <tr>
                  <th className="col-left">Time (Rome)</th>
                  <th className="col-left">Event</th>
                  <th className="col-left">Details</th>
                </tr>
              </thead>
              <tbody>
                {shown.length === 0 && (
                  <tr className="empty-row">
                    <td colSpan={3}>Nothing logged yet.</td>
                  </tr>
                )}
                {shown.map((e, i) => (
                  <tr key={`${e.time}-${i}`} className={['order', 'fill', 'entry'].includes(e.kind) ? 'take-profit-log-order' : ''}>
                    <td className="col-left">{fmtTime(e.time)}</td>
                    <td className={`col-left ${['order', 'fill', 'entry'].includes(e.kind) ? 'good' : e.kind === 'error' ? 'bad' : ''}`}>
                      {LABEL[e.kind] ?? e.kind}
                    </td>
                    <td className="col-left">{e.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
