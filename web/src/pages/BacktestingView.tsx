import { Fragment, useEffect, useMemo, useState } from 'react'
import {
  GATE_REASON_LABEL,
  GROUPS,
  GROUP_LABEL,
  type Backtest,
  type BacktestModel,
  type BacktestWeek,
  type GateReason,
  type GroupKey,
} from '../interfaces/IBacktestingView'

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
  // 'actual' = the rating each snapshot actually shipped with that week
  // (whatever scoring.py was live then). 'current' = the SAME week's
  // factor columns re-scored with TODAY's scoring.py (modules/backtest.py's
  // _rescore_current_model) -- "what would the current model have called
  // at the start of that week," not just "did the old picks survive the
  // new gates." The two summary tables below (Recommendation groups, Why
  // blocked) show both side by side unconditionally; this toggle only
  // switches which one the per-ticker Candidates table (much wider
  // per-week already) and the masthead's candidate count reflect.
  const [modelView, setModelView] = useState<'actual' | 'current'>('actual')
  const modelOf = (w: BacktestWeek): BacktestModel => (modelView === 'current' ? w.currentModel : w)

  useEffect(() => {
    fetch('/backtest.json')
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d) => setData(d))
      .catch(() => setError(true))
  }, [])

  const weeks = useMemo(() => data?.weeks ?? [], [data])

  // ticker -> { rating & group from its most recent week, P&L per week }
  const tickerRows = useMemo(() => {
    const map = new Map<
      string,
      { ticker: string; rating: string; group: GroupKey; blockedBy: GateReason[]; byWeek: Record<string, number> }
    >()
    for (const w of weeks) {
      for (const t of modelOf(w).tickers) {
        const row = map.get(t.ticker) ?? { ticker: t.ticker, rating: t.rating, group: t.group, blockedBy: t.blockedBy, byWeek: {} }
        row.rating = t.rating
        row.group = t.group // weeks are oldest-first, so this ends on the latest
        row.blockedBy = t.blockedBy
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [weeks, modelView])

  const visibleRows = groupFilter === 'all' ? tickerRows : tickerRows.filter((r) => r.group === groupFilter)

  // Every gate reason that fired at least once, for either side, in ANY
  // week under EITHER model -- so a reason that only shows up in one
  // week/model still gets its own row (with '—' where it didn't fire),
  // rather than the row set changing between the Actual/Current columns.
  const blockedReasons = useMemo(() => {
    const long = new Set<GateReason>()
    const short = new Set<GateReason>()
    for (const w of weeks) {
      for (const bb of [w.blockedBreakdown, w.currentModel.blockedBreakdown]) {
        for (const r of Object.keys(bb.long ?? {})) long.add(r as GateReason)
        for (const r of Object.keys(bb.short ?? {})) short.add(r as GateReason)
      }
    }
    return { long: [...long], short: [...short] }
  }, [weeks])

  const totalLatest = weeks.length ? GROUPS.reduce((s, g) => s + (modelOf(weeks[weeks.length - 1]).groups[g]?.count ?? 0), 0) : 0

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
              <span className="l">candidates (latest, {modelView === 'current' ? 'current model' : 'actual'})</span>
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
                    <th className="col-left" rowSpan={2}>
                      Group
                    </th>
                    {weeks.map((w) => (
                      <th
                        key={w.week}
                        className="num"
                        colSpan={2}
                        title={w.entryDate && w.exitDate ? `${w.entryDate} → ${w.exitDate}` : undefined}
                      >
                        {fmtWeek(w.week)}
                      </th>
                    ))}
                  </tr>
                  <tr>
                    {weeks.map((w) => (
                      <Fragment key={w.week}>
                        <th className="num" title="The rating this snapshot actually shipped with that week.">
                          Actual
                        </th>
                        <th
                          className="num"
                          title="Same week's factor columns re-scored with TODAY's scoring.py and gates."
                        >
                          Current
                        </th>
                      </Fragment>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {GROUPS.map((g) => (
                    <tr key={g}>
                      <td className={`col-left ${GROUP_CLASS[g]}`}>{GROUP_LABEL[g]}</td>
                      {weeks.map((w) => {
                        const a = w.groups[g]
                        const c = w.currentModel.groups[g]
                        return (
                          <Fragment key={w.week}>
                            <td className={`num ${signClass(a?.return ?? null)}`} title={a?.count ? `${a.count} names` : undefined}>
                              {fmtPct(a?.return ?? null)}
                            </td>
                            <td className={`num ${signClass(c?.return ?? null)}`} title={c?.count ? `${c.count} names` : undefined}>
                              {fmtPct(c?.return ?? null)}
                            </td>
                          </Fragment>
                        )
                      })}
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr>
                    <td className="col-left">Portfolio (Strong Buy + Strong Sell)</td>
                    {weeks.map((w) => {
                      const a = w.portfolio
                      const c = w.currentModel.portfolio
                      return (
                        <Fragment key={w.week}>
                          <td className={`num ${signClass(a?.return ?? null)}`} title={a?.count ? `${a.count} names` : undefined}>
                            {fmtPct(a?.return ?? null)}
                          </td>
                          <td className={`num ${signClass(c?.return ?? null)}`} title={c?.count ? `${c.count} names` : undefined}>
                            {fmtPct(c?.return ?? null)}
                          </td>
                        </Fragment>
                      )
                    })}
                  </tr>
                </tfoot>
              </table>
            </div>
            <p className="dataset-note">
              Equal-weight mean position P&amp;L: +stock return for Long groups, −stock return for Short groups, so
              positive always means the pick worked. n (hover a cell) = candidates with IB daily bars in the window.
              "blocked" = failed a Recommendations entry gate (falling-knife/overbought or oversold/strong-uptrend
              momentum, mean-reversion) — a working gate makes the blocked group worse than its un-blocked counterpart.
              Portfolio = the gated Strong Buy long leg + gated Strong Sell short leg summed (dollar-neutral, each leg
              equal-weight 100% gross). <strong>Actual</strong> = the rating each snapshot shipped with that week;{' '}
              <strong>Current</strong> = that same week's factor columns re-scored with today's scoring.py and gates
              (see modules/backtest.py's own _rescore_current_model for exactly what that can and can't reconstruct).
            </p>
          </section>

          {(blockedReasons.long.length > 0 || blockedReasons.short.length > 0) && (
            <section className="target-section">
              <h2 className="section-heading">Why blocked — by gate reason</h2>
              <p className="dataset-note">
                Breaks each *_blocked group down by the SPECIFIC gate that fired, so an underperforming (or
                outperforming) blocked group can be traced to one rule instead of "some unspecified mix." A row can
                fail more than one gate at once, so counts here don't sum back to the Long/Short blocked group's own
                count above.
              </p>
              {(['long', 'short'] as const).map((side) =>
                blockedReasons[side].length > 0 ? (
                  <div className="table-wrap" key={side}>
                    <table>
                      <thead>
                        <tr>
                          <th className="col-left" rowSpan={2}>
                            {side === 'long' ? 'Long blocked' : 'Short blocked'} — reason
                          </th>
                          {weeks.map((w) => (
                            <th key={w.week} className="num" colSpan={2}>
                              {fmtWeek(w.week)}
                            </th>
                          ))}
                        </tr>
                        <tr>
                          {weeks.map((w) => (
                            <Fragment key={w.week}>
                              <th className="num">Actual</th>
                              <th className="num">Current</th>
                            </Fragment>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {blockedReasons[side].map((reason) => (
                          <tr key={reason}>
                            <td className="col-left">{GATE_REASON_LABEL[reason]}</td>
                            {weeks.map((w) => {
                              const a = w.blockedBreakdown[side]?.[reason]
                              const c = w.currentModel.blockedBreakdown[side]?.[reason]
                              return (
                                <Fragment key={w.week}>
                                  <td className={`num ${signClass(a?.return ?? null)}`} title={a?.count ? `${a.count} names` : undefined}>
                                    {fmtPct(a?.return ?? null)}
                                  </td>
                                  <td className={`num ${signClass(c?.return ?? null)}`} title={c?.count ? `${c.count} names` : undefined}>
                                    {fmtPct(c?.return ?? null)}
                                  </td>
                                </Fragment>
                              )
                            })}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : null
              )}
            </section>
          )}

          <section className="target-section">
            <h2 className="section-heading">Candidates — weekly P&amp;L</h2>
            <div className="tab-bar">
              <button
                type="button"
                className={`tab-btn${modelView === 'actual' ? ' active' : ''}`}
                onClick={() => setModelView('actual')}
                title="The rating this snapshot actually shipped with that week."
              >
                Actual
              </button>
              <button
                type="button"
                className={`tab-btn${modelView === 'current' ? ' active' : ''}`}
                onClick={() => setModelView('current')}
                title="Same week's factor columns re-scored with today's scoring.py and gates."
              >
                Current model
              </button>
            </div>
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
                    <th className="col-left">Blocked by</th>
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
                      <td className="col-left">
                        {r.blockedBy.length ? r.blockedBy.map((g) => GATE_REASON_LABEL[g]).join(', ') : '—'}
                      </td>
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

