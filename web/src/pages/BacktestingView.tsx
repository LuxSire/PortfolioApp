import { useEffect, useMemo, useState } from 'react'
import {
  GATE_REASON_LABEL,
  GROUPS,
  GROUP_LABEL,
  type Backtest,
  type GateReason,
  type GroupKey,
} from '../interfaces/IBacktestingView'
import { getSectorGroup } from '../sectorGroups'

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(2) + '%'
}
function signClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return ''
  return v > 0 ? 'good' : v < 0 ? 'bad' : ''
}
// Product of (1+r) across every week that actually has a return, minus 1
// -- e.g. +2%, -1%, +3% compounds to +4.06%, not the +4% simple sum. A
// week with no return for this row (undefined/null -- candidate wasn't
// rated that week, a blocked-reason group had 0 names, etc.) is skipped
// rather than treated as a flat 0%, so a row's compounded figure only
// reflects weeks it actually had a position in.
function compoundReturn(returns: (number | null | undefined)[]): number | null {
  const valid = returns.filter((r): r is number => r !== null && r !== undefined)
  if (valid.length === 0) return null
  return valid.reduce((acc, r) => acc * (1 + r), 1) - 1
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

// Every rated Recommendations candidate scored forward 5 trading days from
// each dated snapshot (see modules/backtest.py's HOLDING_TRADING_DAYS),
// split by the same entry gates
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

  // ticker -> { rating & group from its most recent week, P&L per week }.
  // byWeek keeps the raw (informational) return even for a week the
  // candidate was *_blocked, same as the Why-blocked table above -- a
  // FEIM-shaped -40.9% blocked week is exactly the kind of number worth
  // seeing. blockedWeeks tracks which of those weeks were blocked so the
  // Compounded column (below) can exclude them: no real position was
  // taken that week, so it shouldn't be multiplied into a compounded P&L
  // as if it were an actual trade.
  const tickerRows = useMemo(() => {
    const map = new Map<
      string,
      {
        ticker: string
        rating: string
        group: GroupKey
        blockedBy: GateReason[]
        byWeek: Record<string, number>
        blockedWeeks: Set<string>
      }
    >()
    for (const w of weeks) {
      for (const t of w.currentModel.tickers) {
        const row =
          map.get(t.ticker) ?? { ticker: t.ticker, rating: t.rating, group: t.group, blockedBy: t.blockedBy, byWeek: {}, blockedWeeks: new Set<string>() }
        row.rating = t.rating
        row.group = t.group // weeks are oldest-first, so this ends on the latest
        row.blockedBy = t.blockedBy
        row.byWeek[w.week] = t.return
        if (t.group.endsWith('_blocked')) row.blockedWeeks.add(w.week)
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

  // Same population as the "Recommendation groups" table (current-model,
  // rescored) but restricted to actual positions -- *_blocked rows
  // excluded, same reasoning the Portfolio row above already applies --
  // then bucketed by the broad Yahoo-style sector group (getSectorGroup,
  // the same grouping SectorFilter/Positions use) instead of long/short.
  // Equal-weight mean of position P&L per sector per week, same sign
  // convention as everywhere else on this page (positive = the call
  // worked, long or short).
  const sectorRows = useMemo(() => {
    const bySector = new Map<string, Map<string, number[]>>()
    for (const w of weeks) {
      for (const t of w.currentModel.tickers) {
        if (t.group === 'long_blocked' || t.group === 'short_blocked') continue
        const sector = getSectorGroup(t.sector ?? undefined)
        if (!bySector.has(sector)) bySector.set(sector, new Map())
        const byWeek = bySector.get(sector) as Map<string, number[]>
        if (!byWeek.has(w.week)) byWeek.set(w.week, [])
        byWeek.get(w.week)!.push(t.return)
      }
    }
    return [...bySector.entries()]
      .map(([sector, byWeek]) => {
        const stats: Record<string, { return: number; count: number }> = {}
        for (const [week, returns] of byWeek) {
          stats[week] = { return: returns.reduce((a, b) => a + b, 0) / returns.length, count: returns.length }
        }
        return { sector, byWeek: stats }
      })
      .sort((a, b) => {
        const ca = compoundReturn(weeks.map((w) => a.byWeek[w.week]?.return)) ?? -Infinity
        const cb = compoundReturn(weeks.map((w) => b.byWeek[w.week]?.return)) ?? -Infinity
        return cb - ca
      })
  }, [weeks])

  // Every gate reason that fired at least once, for either side, in ANY
  // week -- so a reason that only shows up in one week still gets its own
  // row (with '—' elsewhere), rather than the row set changing week to week.
  const blockedReasons = useMemo(() => {
    const long = new Set<GateReason>()
    const short = new Set<GateReason>()
    for (const w of weeks) {
      const bb = w.currentModel.blockedBreakdown
      for (const r of Object.keys(bb.long ?? {})) long.add(r as GateReason)
      for (const r of Object.keys(bb.short ?? {})) short.add(r as GateReason)
    }
    return { long: [...long], short: [...short] }
  }, [weeks])

  const totalLatest = weeks.length ? GROUPS.reduce((s, g) => s + (weeks[weeks.length - 1].currentModel.groups[g]?.count ?? 0), 0) : 0

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
              <span className="l">candidates (latest, current model)</span>
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
            <h2 className="section-heading">Recommendation groups — forward 5-trading-day P&amp;L (equal weight)</h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th className="col-left">Group</th>
                    <th className="num" title="Compounded across every week shown -- product of (1+return), not the simple sum">
                      Compounded
                    </th>
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
                  {GROUPS.map((g) => {
                    const compounded = compoundReturn(weeks.map((w) => w.currentModel.groups[g]?.return))
                    return (
                      <tr key={g}>
                        <td className={`col-left ${GROUP_CLASS[g]}`}>{GROUP_LABEL[g]}</td>
                        <td className={`num ${signClass(compounded)}`}>{fmtPct(compounded)}</td>
                        {weeks.map((w) => {
                          const c = w.currentModel.groups[g]
                          return (
                            <td key={w.week} className={`num ${signClass(c?.return ?? null)}`} title={c?.count ? `${c.count} names` : undefined}>
                              {fmtPct(c?.return ?? null)}
                            </td>
                          )
                        })}
                      </tr>
                    )
                  })}
                </tbody>
                <tfoot>
                  <tr>
                    <td className="col-left">Portfolio (Strong Buy + Strong Sell)</td>
                    <td className={`num ${signClass(compoundReturn(weeks.map((w) => w.currentModel.portfolio?.return)))}`}>
                      {fmtPct(compoundReturn(weeks.map((w) => w.currentModel.portfolio?.return)))}
                    </td>
                    {weeks.map((w) => {
                      const c = w.currentModel.portfolio
                      return (
                        <td key={w.week} className={`num ${signClass(c?.return ?? null)}`} title={c?.count ? `${c.count} names` : undefined}>
                          {fmtPct(c?.return ?? null)}
                        </td>
                      )
                    })}
                  </tr>
                </tfoot>
              </table>
            </div>
            <p className="dataset-note">
              Equal-weight mean position P&amp;L: +stock return for Long groups, −stock return for Short groups, so
              positive always means the pick worked. n (hover a cell) = candidates with IB daily bars in the window.
              "blocked" = failed a Recommendations entry gate (weak/strong-momentum continuation, a bad entry-timing
              day, or earnings within the week) — a working gate makes the blocked group worse than its un-blocked
              counterpart.
              Portfolio = the gated Strong Buy long leg + gated Strong Sell short leg summed (dollar-neutral, each leg
              equal-weight 100% gross). Every number here is the <strong>current model</strong> — that week's factor
              columns re-scored with today's scoring.py and gates (see modules/backtest.py's own
              _rescore_current_model for exactly what that can and can't reconstruct), not the rating the snapshot
              actually shipped with that week.
            </p>
          </section>

          <section className="target-section">
            <h2 className="section-heading">By sector — forward 5-trading-day P&amp;L (equal weight)</h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th className="col-left">Sector</th>
                    <th className="num" title="Compounded across every week shown -- product of (1+return), not the simple sum">
                      Compounded
                    </th>
                    {weeks.map((w) => (
                      <th key={w.week} className="num">
                        {fmtWeek(w.week)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {sectorRows.map((r) => {
                    const compounded = compoundReturn(weeks.map((w) => r.byWeek[w.week]?.return))
                    return (
                      <tr key={r.sector}>
                        <td className="col-left">{r.sector}</td>
                        <td className={`num ${signClass(compounded)}`}>{fmtPct(compounded)}</td>
                        {weeks.map((w) => {
                          const c = r.byWeek[w.week]
                          return (
                            <td key={w.week} className={`num ${signClass(c?.return ?? null)}`} title={c?.count ? `${c.count} names` : undefined}>
                              {fmtPct(c?.return ?? null)}
                            </td>
                          )
                        })}
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            <p className="dataset-note">
              Same current-model, position-signed P&amp;L as the table above (positive always means the pick worked,
              long or short), pooled into the broad Yahoo-style sector groups (SectorFilter/Positions' own grouping)
              instead of long/short — and restricted to actual positions, *_blocked rows excluded, same as the
              Portfolio row above. Rows sorted by compounded return, best first.
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
                          <th className="col-left">{side === 'long' ? 'Long blocked' : 'Short blocked'} — reason</th>
                          <th className="num" title="Compounded across every week shown -- product of (1+return), not the simple sum">
                            Compounded
                          </th>
                          {weeks.map((w) => (
                            <th key={w.week} className="num">
                              {fmtWeek(w.week)}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {blockedReasons[side].map((reason) => {
                          const compounded = compoundReturn(
                            weeks.map((w) => w.currentModel.blockedBreakdown[side]?.[reason]?.return)
                          )
                          return (
                            <tr key={reason}>
                              <td className="col-left">{GATE_REASON_LABEL[reason]}</td>
                              <td className={`num ${signClass(compounded)}`}>{fmtPct(compounded)}</td>
                              {weeks.map((w) => {
                                const c = w.currentModel.blockedBreakdown[side]?.[reason]
                                return (
                                  <td key={w.week} className={`num ${signClass(c?.return ?? null)}`} title={c?.count ? `${c.count} names` : undefined}>
                                    {fmtPct(c?.return ?? null)}
                                  </td>
                                )
                              })}
                            </tr>
                          )
                        })}
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
                    <th className="num" title="Compounded across every week this ticker has a return for -- product of (1+return), not the simple sum">
                      Compounded
                    </th>
                    {weeks.map((w) => (
                      <th key={w.week}>{fmtWeek(w.week)}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {visibleRows.map((r) => {
                    // Blocked weeks excluded from Compounded -- no real
                    // position was taken that week, so it shouldn't count
                    // as a realized leg of the trade sequence (see
                    // tickerRows' own blockedWeeks comment above).
                    const compounded = compoundReturn(weeks.map((w) => (r.blockedWeeks.has(w.week) ? undefined : r.byWeek[w.week])))
                    return (
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
                        <td className={`num ${signClass(compounded)}`}>{fmtPct(compounded)}</td>
                        {weeks.map((w) => {
                          const v = r.byWeek[w.week]
                          const blocked = r.blockedWeeks.has(w.week)
                          return (
                            <td
                              key={w.week}
                              className={`num ${blocked ? 'cell-blocked' : signClass(v)}`}
                              title={blocked ? 'Blocked that week -- not counted as a real position, excluded from Compounded' : undefined}
                            >
                              {v === undefined ? '—' : blocked ? `(${fmtPct(v)})` : fmtPct(v)}
                            </td>
                          )
                        })}
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </div>
  )
}

