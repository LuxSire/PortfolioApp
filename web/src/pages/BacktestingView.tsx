import { useEffect, useMemo, useState } from 'react'
import { GROUPS, GROUP_LABEL, type Backtest, type GroupKey } from '../interfaces/IBacktestingView'

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(2) + '%'
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

const GROUP_CLASS: Record<GroupKey, string> = {
  long_strong_buy: 'good',
  long_buy: 'good',
  long_blocked: '',
  short_strong_sell: 'bad',
  short_sell: 'bad',
  short_blocked: '',
}
const GROUP_ORDER: Record<GroupKey, number> = GROUPS.reduce(
  (acc, g, i) => ({ ...acc, [g]: i }),
  {} as Record<GroupKey, number>,
)

// Every rated Recommendations candidate scored forward one week from each
// dated snapshot (see modules/backtest.py), split by the same entry gates
// the Recommendations page applies: Long / Short vs Long blocked / Short
// blocked. Returns are POSITION P&L (+ for a long that rose, + for a
// short that fell), so on every row a positive number = the call worked.
// One column per week; oldest snapshot first.
export default function BacktestingView() {
  const [data, setData] = useState<Backtest | null>(null)
  const [error, setError] = useState(false)
  const [groupFilter, setGroupFilter] = useState<GroupKey | 'all'>('all')

  useEffect(() => {
    fetch('/backtest.json')
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d) => setData(d))
      .catch(() => setError(true))
  }, [])

  const weeks = useMemo(() => data?.weeks ?? [], [data])

  // ticker -> { rating & group from its most recent week, P&L per week }
  const tickerRows = useMemo(() => {
    const map = new Map<string, { ticker: string; rating: string; group: GroupKey; byWeek: Record<string, number> }>()
    for (const w of weeks) {
      for (const t of w.tickers) {
        const row = map.get(t.ticker) ?? { ticker: t.ticker, rating: t.rating, group: t.group, byWeek: {} }
        row.rating = t.rating
        row.group = t.group // weeks are oldest-first, so this ends on the latest
        row.byWeek[w.week] = t.return
        map.set(t.ticker, row)
      }
    }
    const latest = weeks.length ? weeks[weeks.length - 1].week : null
    return [...map.values()].sort((a, b) => {
      if (GROUP_ORDER[a.group] !== GROUP_ORDER[b.group]) return GROUP_ORDER[a.group] - GROUP_ORDER[b.group]
      const ra = latest ? (a.byWeek[latest] ?? -Infinity) : 0
      const rb = latest ? (b.byWeek[latest] ?? -Infinity) : 0
      return rb - ra
    })
  }, [weeks])

  const visibleRows = groupFilter === 'all' ? tickerRows : tickerRows.filter((r) => r.group === groupFilter)

  const totalLatest = weeks.length
    ? GROUPS.reduce((s, g) => s + (weeks[weeks.length - 1].groups[g]?.count ?? 0), 0)
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
              <span className="l">candidates (latest)</span>
            </div>
          </div>
        )}
      </header>

      {error && (
        <div className="asset-card">
          Couldn't load backtest.json — run <code>python main.py backtest</code> (or the Backtesting row on the Dataset
          tab).
        </div>
      )}
      {!error && !data && <div className="asset-card">Loading…</div>}
      {!error && data && weeks.length === 0 && (
        <div className="asset-card">
          No dated snapshots found. Drop a <code>sorted_screen &lt;YYYYMMDD&gt;.csv</code> into{' '}
          <code>data/output/history/</code> and re-run the backtest.
        </div>
      )}

      {!error && data && weeks.length > 0 && (
        <>
          <section className="target-section">
            <h2 className="section-heading">Recommendation groups — forward one-week P&amp;L (equal weight)</h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th className="col-left">Group</th>
                    {weeks.map((w) => (
                      <th
                        key={w.week}
                        className="num"
                        title={w.entryDate && w.exitDate ? `${w.entryDate} → ${w.exitDate}` : undefined}
                      >
                        {fmtWeek(w.week)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {GROUPS.map((g) => (
                    <tr key={g}>
                      <td className={`col-left ${GROUP_CLASS[g]}`}>{GROUP_LABEL[g]}</td>
                      {weeks.map((w) => {
                        const s = w.groups[g]
                        return (
                          <td
                            key={w.week}
                            className={`num ${signClass(s?.return ?? null)}`}
                            title={s?.count ? `${s.count} names` : undefined}
                          >
                            {fmtPct(s?.return ?? null)}
                          </td>
                        )
                      })}
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr>
                    <td className="col-left">Portfolio (Strong Buy + Strong Sell)</td>
                    {weeks.map((w) => (
                      <td
                        key={w.week}
                        className={`num ${signClass(w.portfolio?.return ?? null)}`}
                        title={w.portfolio?.count ? `${w.portfolio.count} names` : undefined}
                      >
                        {fmtPct(w.portfolio?.return ?? null)}
                      </td>
                    ))}
                  </tr>
                </tfoot>
              </table>
            </div>
            <p className="dataset-note">
              Equal-weight mean position P&amp;L: +stock return for Long groups, −stock return for Short groups, so
              positive always means the pick worked. n (hover a cell) = candidates with IB daily bars in the window.
              "blocked" = failed a Recommendations entry gate (overbought / oversold momentum or mean-reversion, growth
              threshold, crowded short, EPS-trend) — a working gate makes the blocked group worse than its un-blocked
              counterpart. Portfolio = the gated Strong Buy long leg + gated Strong Sell short leg summed (dollar-neutral,
              each leg equal-weight 100% gross).
            </p>
          </section>

          <section className="target-section">
            <h2 className="section-heading">Candidates — weekly P&amp;L</h2>
            <div className="tab-bar">
              {(['all', ...GROUPS] as const).map((g) => (
                <button
                  key={g}
                  type="button"
                  className={`tab-btn${groupFilter === g ? ' active' : ''}`}
                  onClick={() => setGroupFilter(g)}
                >
                  {g === 'all' ? `All (${tickerRows.length})` : GROUP_LABEL[g]}
                </button>
              ))}
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th className="col-left">Ticker</th>
                    <th className="col-left">Group</th>
                    <th className="col-left">Rating</th>
                    {weeks.map((w) => (
                      <th key={w.week}>{fmtWeek(w.week)}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {visibleRows.map((r) => (
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
                      <td className={`col-left ${GROUP_CLASS[r.group]}`}>{GROUP_LABEL[r.group]}</td>
                      <td className="col-left">{r.rating}</td>
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

