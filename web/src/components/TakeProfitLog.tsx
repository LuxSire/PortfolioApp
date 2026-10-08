import { useEffect, useState } from 'react'
import { IB_TAKE_PROFIT_LOG_URL } from '../ibStream'

// A sell (order, entry signal, IB status or fill) is shown in red, a buy in green.
function isSell(e: { message: string; order?: { action?: string } }): boolean {
  return e.order?.action ? e.order.action === 'SELL' : /\bSELL\b/.test(e.message)
}

// Positions page "Trading robot" panel -- explicit instruction: show the log of the
// background take-profit loop (ib_server.py's take_profit_loop), i.e. WHEN IT
// RUNS and WHEN IT PLACES AN ORDER. Polls GET /api/take-profit/log every
// 30 s; newest event first. A missing/unreachable server (or an ib_server
// that hasn't been restarted since the endpoint was added) just shows a
// short message -- best-effort, same as the other ib_server-backed panels.

interface TakeProfitEvent {
  time: string
  kind: 'status' | 'window' | 'check' | 'skip' | 'order' | 'entry' | 'entry_skip' | 'consider' | 'beta' | 'ib' | 'fill' | 'error'
  message: string
  order?: { action?: string }
}

interface TakeProfitLogData {
  status: { state: string; lastCheck: string | null; nextCheck: string | null; windowOpen: boolean | null }
  config: {
    enabled: boolean
    dryRun: boolean
    window: string
    intervalMinutes: number
    triggerSd: number
    limitSd: number
    maxStepSd: number
  }
  events: TakeProfitEvent[]
}

const POLL_MS = 30000
// Only the 3 newest events (explicit instruction): a new event pushes the oldest one out.
const SHOWN = 3

// "2026-10-02T16:10:00+02:00" -> "02 Oct 16:10:00" (the server already stamps
// events in Europe/Rome, the time zone the trading window is defined in).
function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const m = iso.match(/^\d{4}-(\d{2})-(\d{2})T(\d{2}:\d{2}:\d{2})/)
  if (!m) return iso
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
  return `${m[2]} ${months[Number(m[1]) - 1]} ${m[3]}`
}

const KIND_LABEL: Record<TakeProfitEvent['kind'], string> = {
  status: 'status',
  window: 'window',
  check: 'check',
  skip: 'waiting',
  order: 'ORDER',
  entry: 'ENTRY (not sent)',
  entry_skip: 'ENTRY skipped',
  consider: 'ENTRY considered',
  beta: 'beta',
  ib: 'IB',
  fill: 'FILLED',
  error: 'ERROR',
}

export default function TakeProfitLog() {
  const [data, setData] = useState<TakeProfitLogData | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    let cancelled = false
    const load = () =>
      fetch(IB_TAKE_PROFIT_LOG_URL)
        .then((r) => (r.ok ? r.json() : Promise.reject()))
        .then((d: TakeProfitLogData) => {
          if (cancelled) return
          setData(d)
          setError(false)
        })
        .catch(() => !cancelled && setError(true))
    load()
    const id = setInterval(load, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  const events = (data?.events ?? []).slice(0, SHOWN)

  return (
    <div className="asset-card take-profit-log">
      <h2
        title="Background loop in ib_server.py: closes a held position with a limit order once it is up 1.5σ on the day, and places a closing trade (limit 0.1σ from the offer/bid) for a held position that is no longer Strong Buy/Strong Sell and is up on the day. The log is also written to data/IB/take_profit_activity.log."
      >
        Trading robot
      </h2>

      {error && (
        <p className="status-row">
          Couldn't read the take-profit log — is ib_server.py running (and restarted since the take-profit loop was added)?
        </p>
      )}
      {!error && !data && <p className="status-row">Loading…</p>}

      {data && (
        <>
          <div className="take-profit-log-scroll">
            <table>
              <thead>
                <tr>
                  <th className="col-left">Time (Rome)</th>
                  <th className="col-left">Event</th>
                  <th className="col-left">Details</th>
                </tr>
              </thead>
              <tbody>
                {events.length === 0 && (
                  <tr className="empty-row">
                    <td colSpan={3}>Nothing logged yet.</td>
                  </tr>
                )}
                {events.map((e, i) => (
                  <tr key={`${e.time}-${i}`} className={['order', 'fill', 'entry'].includes(e.kind) ? (isSell(e) ? 'take-profit-log-order take-profit-log-sell' : 'take-profit-log-order') : ''}>
                    <td className="col-left">{fmtTime(e.time)}</td>
                    <td className={`col-left ${e.kind === 'error' ? 'bad' : ['order', 'fill', 'entry', 'ib'].includes(e.kind) ? (isSell(e) ? 'bad' : 'good') : ''}`}>
                      {KIND_LABEL[e.kind] ?? e.kind}
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
