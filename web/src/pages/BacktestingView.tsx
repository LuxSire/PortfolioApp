import { useEffect, useMemo, useState } from 'react'
import { RATING_BUCKETS, type Backtest, type RatingBucket } from '../interfaces/IBacktestingView'

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(2) + '%'
}
function fmtVol(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return (v * 100).toFixed(2) + '%'
}
function fmtRatio(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return v.toFixed(2)
}
function signClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return ''
  return v > 0 ? 'good' : v < 0 ? 'bad' : ''
}
// "2026-08-22" -> "Aug 22 '26"
function fmtWeek(iso: string): string {
  const [y, m, d] = iso.split('-')
  const month = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][Number(m) - 1]
  return `${month} ${Number(d)} '${y.slice(2)}`
}

const BUCKET_CLASS: Record<RatingBucket, string> = {
  'Strong Buy': 'good',
  Buy: 'good',
  Sell: 'bad',
  'Strong Sell': 'bad',
}
const BUCKET_ORDER: Record<RatingBucket, number> = { 'Strong Buy': 0, Buy: 1, Sell: 2, 'Strong Sell': 3 }

// Each historical screen snapshot scored forward one week against IB's
// daily bars (see modules/backtest.py) -- per rating bucket, equal-weight
// weekly return / volatility / Sharpe, then the per-ticker returns
// underneath. One column per week; the first (oldest) snapshot leads.
export default function BacktestingView() {
  const [data, setData] = useState<Backtest | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    fetch('/backtest.json')
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d) => setData(d))
      .catch(() => setError(true))
  }, [])

  const weeks = useMemo(() => data?.weeks ?? [], [data])

  // ticker -> { rating (from its most recent week), return per week }
  const tickerRows = useMemo(() => {
    const map = new Map<string, { ticker: string; rating: RatingBucket; byWeek: Record<string, number> }>()
    for (const w of weeks) {
      for (const t of w.tickers) {
        const row = map.get(t.ticker) ?? { ticker: t.ticker, rating: t.rating, byWeek: {} }
        row.rating = t.rating // weeks are oldest-first, so this ends on the latest
        row.byWeek[w.week] = t.return
        map.set(t.ticker, row)
      }
    }
    const latest = weeks.length ? weeks[weeks.length - 1].week : null
    return [...map.values()].sort((a, b) => {
      if (BUCKET_ORDER[a.rating] !== BUCKET_ORDER[b.rating]) return BUCKET_ORDER[a.rating] - BUCKET_ORDER[b.rating]
      const ra = latest ? (a.byWeek[latest] ?? -Infinity) : 0
      const rb = latest ? (b.byWeek[latest] ?? -Infinity) : 0
      return rb - ra
    })
  }, [weeks])

  const totalLatest = weeks.length
    ? RATING_BUCKETS.reduce((s, b) => s + (weeks[weeks.length - 1].buckets[b]?.count ?? 0), 0)
    : 0

  return (
    <div className="positions-page dataset-page">
      <header className="masthead">
        <div className="title-block">
          <h1>Backtesting</h1>
        </div>
        {data && (
          <div className="stat-row">
            <div className="stat">
              <span className="n num">{weeks.length}</span>
              <span className="l">weeks</span>
            </div>
            <div className="stat">
              <span className="n num">{weeks.length ? fmtWeek(weeks[weeks.length - 1].week) : '—'}</span>
              <span className="l">latest</span>
            </div>
            <div className="stat">
              <span className="n num">{totalLatest}</span>
              <span className="l">tickers scored (latest)</span>
            </div>
          </div>
        )}
      </header>

      {error && <div className="asset-card">Couldn't load backtest.json — run `python main.py backtest` (or the Backtesting row on the Dataset tab).</div>}
      {!error && !data && <div className="asset-card">Loading…</div>}
      {!error && data && weeks.length === 0 && (
        <div className="asset-card">
          No dated screen snapshots found. Drop a <code>sorted_screen &lt;YYYYMMDD&gt;.csv</code> into{' '}
          <code>data/output/history/</code> and re-run the backtest.
        </div>
      )}

      {!error && data && weeks.length > 0 && (
        <>
          <section className="target-section">
            <h2 className="section-heading">Rating buckets — forward one-week performance (equal weight)</h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th className="col-left" rowSpan={2}>
                      Bucket
                    </th>
                    {weeks.map((w) => (
                      <th
                        key={w.week}
                        colSpan={3}
                        title={w.entryDate && w.exitDate ? `${w.entryDate} → ${w.exitDate}` : undefined}
                      >
                        {fmtWeek(w.week)}
                      </th>
                    ))}
                  </tr>
                  <tr>
                    {weeks.map((w) => (
                      <FragmentSubHead key={w.week} />
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {RATING_BUCKETS.map((b) => (
                    <tr key={b}>
                      <td className={`col-left ${BUCKET_CLASS[b]}`}>{b}</td>
                      {weeks.map((w) => {
                        const s = w.buckets[b]
                        return (
                          <FragmentBucketCells
                            key={w.week}
                            ret={s?.return ?? null}
                            vol={s?.vol ?? null}
                            sharpe={s?.sharpe ?? null}
                            count={s?.count ?? 0}
                          />
                        )
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="dataset-note">
              Ret = equal-weight mean of the bucket's weekly stock returns (raw, so a working screen makes Strong Sell
              negative). Vol = stdev of the bucket's equal-weight daily returns × √days. Sharpe = Ret / Vol (rf ≈ 0). n =
              names with IB daily bars in the window.
            </p>
          </section>

          <section className="target-section">
            <h2 className="section-heading">Tickers — weekly return</h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th className="col-left">Ticker</th>
                    <th className="col-left">Rating</th>
                    {weeks.map((w) => (
                      <th key={w.week}>{fmtWeek(w.week)}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {tickerRows.map((r) => (
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
                      <td className={`col-left ${BUCKET_CLASS[r.rating]}`}>{r.rating}</td>
                      {weeks.map((w) => {
                        const v = r.byWeek[w.week]
                        return (
                          <td key={w.week} className={`num ${signClass(v)}`}>
                            {v === undefined ? '—' : fmtPct(v)}
                          </td>
                        )
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </div>
  )
}

function FragmentSubHead() {
  return (
    <>
      <th className="num">Ret</th>
      <th className="num">Vol</th>
      <th className="num">Sharpe</th>
    </>
  )
}

function FragmentBucketCells({
  ret,
  vol,
  sharpe,
  count,
}: {
  ret: number | null
  vol: number | null
  sharpe: number | null
  count: number
}) {
  return (
    <>
      <td className={`num ${signClass(ret)}`} title={count ? `${count} names` : undefined}>
        {fmtPct(ret)}
      </td>
      <td className="num">{fmtVol(vol)}</td>
      <td className={`num ${signClass(sharpe)}`}>{fmtRatio(sharpe)}</td>
    </>
  )
}
