import { useEffect, useMemo, useState } from 'react'
import {
  GATE_REASON_LABEL,
  GROUPS,
  GROUP_LABEL,
  RESTRICTED_GROUP_LABEL,
  RESTRICTED_PCT_4,
  RESTRICTED_PCT_2,
  type Backtest,
  type GateReason,
  type GroupKey,
  type RestrictedGroupKey,
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
// Small-font "n=<count>" next to a row's label (Group column) -- the
// effective number of names behind that row, shown once per row rather
// than repeated in every weekly cell (still available there via `title`
// on hover).
function fmtN(n: number | null | undefined) {
  if (!n) return null
  return <span className="cell-n">n={n}</span>
}
// Average count across only the weeks compoundReturn would actually fold
// in (return !== null/undefined) -- matches compoundReturn's own
// filtering, so the one n shown per row reflects the same weeks its
// Compounded % does.
function avgCount(items: ({ return: number | null; count: number } | null | undefined)[]): number | null {
  const counts = items.filter((i): i is { return: number; count: number } => !!i && i.return !== null && i.return !== undefined).map((i) => i.count)
  if (!counts.length) return null
  return Math.round(counts.reduce((a, b) => a + b, 0) / counts.length)
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
// Weekly volatility (population stdev of the row's own weekly returns,
// same weeks compoundReturn folds in) plus a Sharpe built on TOP of the
// row's compounded return rather than its mean weekly return -- explicit
// instruction: "sharpe ratio based on the compounded return using the
// weekly volatility of the backtesting." So compounded / weekly vol, not
// the standard annualized Sharpe -- no risk-free rate subtracted, since
// none was specified and the group's own edge over 0% is what's being
// sized here.
function weeklyStats(returns: (number | null | undefined)[]): { vol: number | null; sharpe: number | null } {
  const valid = returns.filter((r): r is number => r !== null && r !== undefined)
  const compounded = compoundReturn(valid)
  if (valid.length < 2 || compounded === null) return { vol: null, sharpe: null }
  const mean = valid.reduce((a, b) => a + b, 0) / valid.length
  const variance = valid.reduce((a, r) => a + (r - mean) ** 2, 0) / valid.length
  const vol = Math.sqrt(variance)
  return { vol, sharpe: vol > 0 ? compounded / vol : null }
}
// Simple (arithmetic) mean of the row's weekly returns, same weeks
// compoundReturn folds in -- a week with no return is skipped, not 0%.
function avgWeeklyReturn(returns: (number | null | undefined)[]): number | null {
  const valid = returns.filter((r): r is number => r !== null && r !== undefined)
  return valid.length ? valid.reduce((a, b) => a + b, 0) / valid.length : null
}
function fmtRatio(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return v.toFixed(2)
}
function fmtVol(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return (v * 100).toFixed(2) + '%'
}
// Annualized performance -- the row's own compounded return, scaled up
// from however many weeks it actually covers to a 52-week year:
// (1+compounded)^(52/n) - 1. Same "only weeks this row actually has a
// return for" set as compoundReturn/weeklyStats, so a row with fewer
// weeks on file (a newly-added rating bucket, a blocked-reason group that
// only just started appearing) still annualizes fairly rather than being
// compared on raw un-annualized compounded % against a row with a longer
// history.
function annualizedReturn(returns: (number | null | undefined)[]): number | null {
  const valid = returns.filter((r): r is number => r !== null && r !== undefined)
  const compounded = compoundReturn(valid)
  if (compounded === null || valid.length === 0) return null
  return (1 + compounded) ** (52 / valid.length) - 1
}
// Small-font "avg wk · ann · vol · Sharpe" line, meant to sit right under
// a row's Compounded % (same cell) -- see avgWeeklyReturn/weeklyStats/
// annualizedReturn above. Avg/ann shown even when vol/Sharpe can't be (a
// single-week row has no variance to compute a vol from).
function riskStats(returns: (number | null | undefined)[]) {
  const { vol, sharpe } = weeklyStats(returns)
  const avg = avgWeeklyReturn(returns)
  const ann = annualizedReturn(returns)
  if (ann === null && vol === null) return null
  return (
    <span className="cell-risk-stats">
      avg wk {fmtPct(avg)} · ann {fmtPct(ann)} · vol {fmtVol(vol)} · Sharpe {fmtRatio(sharpe)}
    </span>
  )
}
// Candidates table: a long_strong_buy/short_strong_sell ticker shows the
// TIGHTEST restricted cut its own pct actually clears (2.5% ⊂ 5% ⊂ 7.5%)
// instead of always the plain group label -- explicit instruction. Every
// other group is unaffected (long_buy/*_blocked/hold have no restricted
// tier to check).
function effectiveGroupLabel(group: GroupKey, pct: number | null): string {
  if (pct === null) return GROUP_LABEL[group]
  if (group === 'long_strong_buy') {
    if (pct < RESTRICTED_PCT_2) return RESTRICTED_GROUP_LABEL.long_strong_buy_restricted_2
    if (pct < RESTRICTED_PCT_4) return RESTRICTED_GROUP_LABEL.long_strong_buy_restricted_4
  }
  if (group === 'short_strong_sell') {
    if (pct >= 1 - RESTRICTED_PCT_2) return RESTRICTED_GROUP_LABEL.short_strong_sell_restricted_2
    if (pct >= 1 - RESTRICTED_PCT_4) return RESTRICTED_GROUP_LABEL.short_strong_sell_restricted_4
  }
  return GROUP_LABEL[group]
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
  hold: '',
}
const RESTRICTED_GROUP_CLASS: Record<RestrictedGroupKey, string> = {
  long_strong_buy_restricted_4: 'good',
  long_strong_buy_restricted_2: 'good',
  short_strong_sell_restricted_4: 'bad',
  short_strong_sell_restricted_2: 'bad',
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
// One column per week, paginated WEEKS_PER_PAGE at a time, newest first
// (page 0 = most recent) -- Compounded/n/avg-ann-vol-Sharpe always
// summarize the FULL backtest regardless of the current page.
export default function BacktestingView() {
  const [data, setData] = useState<Backtest | null>(null)
  const [error, setError] = useState(false)
  const [groupFilter, setGroupFilter] = useState<GroupKey | 'all'>('all')
  const [weekPage, setWeekPage] = useState(0)

  useEffect(() => {
    fetch('/backtest.json')
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d) => setData(d))
      .catch(() => setError(true))
  }, [])

  // Full history, oldest-first -- feeds every Compounded/n/avg-ann-vol-
  // Sharpe figure on the page, which always summarizes the WHOLE
  // backtest regardless of which week columns happen to be paged into
  // view below.
  const weeks = useMemo(() => data?.weeks ?? [], [data])
  const WEEKS_PER_PAGE = 4
  // Newest first, paginated -- explicit instruction: the week columns
  // were only ever going to keep expanding rightward otherwise. Page 0 =
  // the most recent WEEKS_PER_PAGE weeks. Resets to page 0 whenever a
  // fresh backtest.json loads (a new week appearing shouldn't leave the
  // viewer stranded on a now-stale page further back).
  const weeksDesc = useMemo(() => [...weeks].reverse(), [weeks])
  const totalWeekPages = Math.max(1, Math.ceil(weeksDesc.length / WEEKS_PER_PAGE))
  useEffect(() => {
    setWeekPage(0)
  }, [data])
  const pagedWeeks = useMemo(
    () => weeksDesc.slice(weekPage * WEEKS_PER_PAGE, weekPage * WEEKS_PER_PAGE + WEEKS_PER_PAGE),
    [weeksDesc, weekPage]
  )

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
        pct: number | null
        blockedBy: GateReason[]
        byWeek: Record<string, number>
        blockedWeeks: Set<string>
      }
    >()
    for (const w of weeks) {
      for (const t of w.currentModel.tickers) {
        const row =
          map.get(t.ticker) ??
          { ticker: t.ticker, rating: t.rating, group: t.group, pct: t.pct, blockedBy: t.blockedBy, byWeek: {}, blockedWeeks: new Set<string>() }
        row.rating = t.rating
        row.group = t.group // weeks are oldest-first, so this ends on the latest
        row.pct = t.pct
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

  // "All long" -- explicit instruction, corrected: EVERY symbol in that
  // week's whole rated universe (all six groups, whichever side each was
  // actually rated for), treated as a LONG position -- a true buy-
  // everything-equally benchmark, not just the subset that happened to
  // get a Buy/Strong Buy rating. t.return is already POSITION P&L (sign
  // applied per the side it was actually rated/grouped for -- positive
  // for a short means the STOCK FELL), so recovering the raw stock return
  // for a short-grouped ticker means un-flipping it (negate) before
  // averaging everyone together as a long.
  const allLongByWeek = useMemo(() => {
    const out: Record<string, { return: number; count: number }> = {}
    for (const w of weeks) {
      const returns = w.currentModel.tickers
        .map((t) => (t.return === null || t.return === undefined ? null : t.group.startsWith('short') ? -t.return : t.return))
        .filter((r): r is number => r !== null)
      if (returns.length) {
        out[w.week] = { return: returns.reduce((a, b) => a + b, 0) / returns.length, count: returns.length }
      }
    }
    return out
  }, [weeks])
  // Plain labeled divider row, spanning the full table width, to visually
  // separate the Long / Short / Portfolio (and Other/baseline) blocks.
  const sectionHeaderRow = (label: string) => (
    <tr className="table-section-header" key={label}>
      <td colSpan={pagedWeeks.length + 2}>{label}</td>
    </tr>
  )
  // One row for a RestrictedGroupKey (5% or 2.5% subset of long_strong_buy/
  // short_strong_sell), same beats-"All long" highlighting as those rows.
  const renderRestrictedRow = (key: RestrictedGroupKey, negateBaseline: boolean) => {
    const compounded = compoundReturn(weeks.map((w) => w.currentModel.groups[key]?.return))
    const baselineRaw = compoundReturn(weeks.map((w) => allLongByWeek[w.week]?.return))
    const baselineCompounded = baselineRaw !== null && negateBaseline ? -baselineRaw : baselineRaw
    const beatsBaselineCompounded = baselineCompounded !== null && compounded !== null && compounded > baselineCompounded
    return (
      <tr key={key}>
        <td className={`col-left ${RESTRICTED_GROUP_CLASS[key]}`}>
          {RESTRICTED_GROUP_LABEL[key]}
          {fmtN(avgCount(weeks.map((w) => w.currentModel.groups[key])))}
        </td>
        <td className={`num ${beatsBaselineCompounded ? 'cell-beats-baseline' : signClass(compounded)}`}>
          {fmtPct(compounded)}
          {riskStats(weeks.map((w) => w.currentModel.groups[key]?.return))}
        </td>
        {pagedWeeks.map((w) => {
          const c = w.currentModel.groups[key]
          const rawBaseline = allLongByWeek[w.week]?.return
          const baseline = rawBaseline !== null && rawBaseline !== undefined && negateBaseline ? -rawBaseline : rawBaseline
          const beatsBaseline =
            baseline !== null && baseline !== undefined && c?.return !== null && c?.return !== undefined && c.return > baseline
          return (
            <td
              key={w.week}
              className={`num ${beatsBaseline ? 'cell-beats-baseline' : signClass(c?.return ?? null)}`}
              title={c?.count ? `${c.count} names` : undefined}
            >
              {fmtPct(c?.return ?? null)}
            </td>
          )
        })}
      </tr>
    )
  }
  // "All long - All short" -- explicit instruction: the naive "just
  // follow every Buy/Strong Buy (or Sell/Strong Sell) call, no gates, no
  // Strong-only filter" baseline PER RATED SIDE, combined into one
  // dollar-neutral book -- a DIFFERENT population from "All long" above
  // (only the tickers actually rated for that side, not the whole
  // universe long-only). Both legs' returns are already POSITION P&L,
  // same convention as every other number on this page, so the combined
  // figure is arithmetically a SUM of the two already-signed series, not
  // a subtraction of raw stock returns (see the table's own dataset-note).
  const allRatedLongByWeek = useMemo(() => {
    const out: Record<string, { return: number; count: number }> = {}
    for (const w of weeks) {
      const returns = w.currentModel.tickers
        .filter((t) => t.group.startsWith('long'))
        .map((t) => t.return)
        .filter((r): r is number => r !== null && r !== undefined)
      if (returns.length) {
        out[w.week] = { return: returns.reduce((a, b) => a + b, 0) / returns.length, count: returns.length }
      }
    }
    return out
  }, [weeks])
  const allRatedShortByWeek = useMemo(() => {
    const out: Record<string, { return: number; count: number }> = {}
    for (const w of weeks) {
      const returns = w.currentModel.tickers
        .filter((t) => t.group.startsWith('short'))
        .map((t) => t.return)
        .filter((r): r is number => r !== null && r !== undefined)
      if (returns.length) {
        out[w.week] = { return: returns.reduce((a, b) => a + b, 0) / returns.length, count: returns.length }
      }
    }
    return out
  }, [weeks])
  const allRatedLongShortByWeek = useMemo(() => {
    const out: Record<string, { return: number; count: number }> = {}
    for (const w of weeks) {
      const l = allRatedLongByWeek[w.week]
      const s = allRatedShortByWeek[w.week]
      if (l && s) out[w.week] = { return: l.return + s.return, count: l.count + s.count }
    }
    return out
  }, [allRatedLongByWeek, allRatedShortByWeek, weeks])

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
              <span className="l">candidates</span>
            </div>
          </div>
        )}
      </header>

      {!error && data && weeks.length > 0 && (
        <div className="week-pagination">
          <button type="button" className="tab-btn" disabled={weekPage >= totalWeekPages - 1} onClick={() => setWeekPage((p) => p + 1)}>
            ← Older
          </button>
          <span className="week-pagination-label">
            {pagedWeeks.length > 0 ? `${fmtWeek(pagedWeeks[pagedWeeks.length - 1].week)} – ${fmtWeek(pagedWeeks[0].week)}` : '—'}
            {' '}(page {weekPage + 1}/{totalWeekPages}, newest first, {WEEKS_PER_PAGE}/page)
          </span>
          <button type="button" className="tab-btn" disabled={weekPage <= 0} onClick={() => setWeekPage((p) => p - 1)}>
            Newer →
          </button>
        </div>
      )}

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
                    <th className="num" title="Compounded across the WHOLE backtest, every week ever scored -- product of (1+return), not the simple sum, and not limited to the page of weeks shown below">
                      Compounded
                    </th>
                    {pagedWeeks.map((w) => (
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
                {/* Portfolio + its baselines first (explicit instruction:
                    Portfolio at the top, Long/Short/Hold at the bottom) --
                    a separate <tbody> rather than <tfoot>, since <tfoot>
                    always renders at the very bottom of a table regardless
                    of where it sits in the markup; sibling <tbody>s render
                    in document order like anything else. */}
                <tbody className="table-emphasis-body">
                  {sectionHeaderRow('Portfolio')}
                  {(
                    [
                      ['Portfolio (Strong Buy + Strong Sell) (2.5%)', (w: (typeof weeks)[number]) => w.currentModel.portfolioRestricted2],
                      ['Portfolio (Strong Buy + Strong Sell) (5%)', (w: (typeof weeks)[number]) => w.currentModel.portfolioRestricted4],
                      ['Portfolio (Strong Buy + Strong Sell) (7.5%)', (w: (typeof weeks)[number]) => w.currentModel.portfolio],
                    ] as const
                  ).map(([label, accessor]) => (
                    <tr key={label}>
                      {(() => {
                        const compounded = compoundReturn(weeks.map((w) => accessor(w)?.return))
                        const baselineCompounded = compoundReturn(weeks.map((w) => allRatedLongShortByWeek[w.week]?.return))
                        const beatsBaselineCompounded =
                          baselineCompounded !== null && compounded !== null && compounded > baselineCompounded
                        return (
                          <>
                            <td className="col-left">
                              {label}
                              {fmtN(avgCount(weeks.map((w) => accessor(w))))}
                            </td>
                            <td className={`num ${beatsBaselineCompounded ? 'cell-beats-baseline' : signClass(compounded)}`}>
                              {fmtPct(compounded)}
                              {riskStats(weeks.map((w) => accessor(w)?.return))}
                            </td>
                          </>
                        )
                      })()}
                      {pagedWeeks.map((w) => {
                        const c = accessor(w)
                        const baseline = allRatedLongShortByWeek[w.week]?.return
                        const beatsBaseline =
                          baseline !== null && baseline !== undefined && c?.return !== null && c?.return !== undefined && c.return > baseline
                        return (
                          <td
                            key={w.week}
                            className={`num ${beatsBaseline ? 'cell-beats-baseline' : signClass(c?.return ?? null)}`}
                            title={c?.count ? `${c.count} names` : undefined}
                          >
                            {fmtPct(c?.return ?? null)}
                          </td>
                        )
                      })}
                    </tr>
                  ))}
                  {sectionHeaderRow('Baselines')}
                  <tr>
                    <td className="col-left">
                      All long (every symbol, held long regardless of rating)
                      {fmtN(avgCount(weeks.map((w) => allLongByWeek[w.week])))}
                    </td>
                    <td className={`num ${signClass(compoundReturn(weeks.map((w) => allLongByWeek[w.week]?.return)))}`}>
                      {fmtPct(compoundReturn(weeks.map((w) => allLongByWeek[w.week]?.return)))}
                      {riskStats(weeks.map((w) => allLongByWeek[w.week]?.return))}
                    </td>
                    {pagedWeeks.map((w) => {
                      const c = allLongByWeek[w.week]
                      return (
                        <td key={w.week} className={`num ${signClass(c?.return ?? null)}`} title={c?.count ? `${c.count} names` : undefined}>
                          {fmtPct(c?.return ?? null)}
                        </td>
                      )
                    })}
                  </tr>
                  <tr>
                    <td className="col-left">
                      All rated long − all rated short (equal-weighted, blocked included)
                      {fmtN(avgCount(weeks.map((w) => allRatedLongShortByWeek[w.week])))}
                    </td>
                    <td className={`num ${signClass(compoundReturn(weeks.map((w) => allRatedLongShortByWeek[w.week]?.return)))}`}>
                      {fmtPct(compoundReturn(weeks.map((w) => allRatedLongShortByWeek[w.week]?.return)))}
                      {riskStats(weeks.map((w) => allRatedLongShortByWeek[w.week]?.return))}
                    </td>
                    {pagedWeeks.map((w) => {
                      const c = allRatedLongShortByWeek[w.week]
                      return (
                        <td key={w.week} className={`num ${signClass(c?.return ?? null)}`} title={c?.count ? `${c.count} names` : undefined}>
                          {fmtPct(c?.return ?? null)}
                        </td>
                      )
                    })}
                  </tr>
                </tbody>
                <tbody>
                  {sectionHeaderRow('Long')}
                  {renderRestrictedRow('long_strong_buy_restricted_2', false)}
                  {renderRestrictedRow('long_strong_buy_restricted_4', false)}
                  {GROUPS.map((g) => {
                    const compounded = compoundReturn(weeks.map((w) => w.currentModel.groups[g]?.return))
                    // long_strong_buy vs. its naive "All long" baseline,
                    // short_strong_sell vs. the OPPOSITE of that same
                    // baseline (-(All long) -- if you'd shorted every
                    // symbol instead of buying it) -- explicit
                    // instruction: dark-green the cell wherever the gated
                    // top tier actually beat the naive buy/sell-everything
                    // baseline for its own side.
                    const baselineCompoundedRaw =
                      g === 'long_strong_buy' || g === 'short_strong_sell'
                        ? compoundReturn(weeks.map((w) => allLongByWeek[w.week]?.return))
                        : null
                    const baselineCompounded =
                      baselineCompoundedRaw !== null && g === 'short_strong_sell' ? -baselineCompoundedRaw : baselineCompoundedRaw
                    const beatsBaselineCompounded = baselineCompounded !== null && compounded !== null && compounded > baselineCompounded
                    return [
                      g === 'short_strong_sell' ? sectionHeaderRow('Short') : null,
                      g === 'short_strong_sell' ? renderRestrictedRow('short_strong_sell_restricted_2', true) : null,
                      g === 'short_strong_sell' ? renderRestrictedRow('short_strong_sell_restricted_4', true) : null,
                      g === 'hold' ? sectionHeaderRow('Other') : null,
                      <tr key={g}>
                        <td className={`col-left ${GROUP_CLASS[g]}`}>
                          {GROUP_LABEL[g]}
                          {fmtN(avgCount(weeks.map((w) => w.currentModel.groups[g])))}
                        </td>
                        <td className={`num ${beatsBaselineCompounded ? 'cell-beats-baseline' : signClass(compounded)}`}>
                          {fmtPct(compounded)}
                          {g !== 'long_blocked' && g !== 'short_blocked' && g !== 'hold'
                            ? riskStats(weeks.map((w) => w.currentModel.groups[g]?.return))
                            : null}
                        </td>
                        {pagedWeeks.map((w) => {
                          const c = w.currentModel.groups[g]
                          const rawBaseline = g === 'long_strong_buy' || g === 'short_strong_sell' ? allLongByWeek[w.week]?.return : null
                          const baseline = rawBaseline !== null && rawBaseline !== undefined && g === 'short_strong_sell' ? -rawBaseline : rawBaseline
                          const beatsBaseline =
                            baseline !== null && baseline !== undefined && c?.return !== null && c?.return !== undefined && c.return > baseline
                          return (
                            <td
                              key={w.week}
                              className={`num ${beatsBaseline ? 'cell-beats-baseline' : signClass(c?.return ?? null)}`}
                              title={c?.count ? `${c.count} names` : undefined}
                            >
                              {fmtPct(c?.return ?? null)}
                            </td>
                          )
                        })}
                      </tr>,
                    ]
                  })}
                </tbody>
              </table>
            </div>
            <p className="dataset-note">
              Equal-weight mean position P&amp;L: +stock return for Long groups, −stock return for Short groups, so
              positive always means the pick worked. Each position is held 5 trading days, or closed earlier at the
              close of the first day that moves ≥ 1.5σ in its favour (σ = its 3-month daily volatility at entry) —
              take-profit. n (next to a row's label, or hover a weekly cell) = candidates
              with IB daily bars in the window. The small line under each Compounded figure is that row's own weekly
              average weekly return (simple mean of its weeks), annualized return, weekly volatility (population stdev
              of its weekly returns) and a Sharpe built on the COMPOUNDED return over that volatility -- not the
              standard annualized Sharpe, and no risk-free rate subtracted. Needs at least 2 weeks with a return to compute;
              "—" otherwise.
              "blocked" = failed a Recommendations entry gate (a daily move beyond ±1σ against the trade, the Trend filter (weak stocks not bought, strong ones not sold), a simulation pointing the wrong way, too-strong revenue growth to short, or earnings within the week) — a working
              gate makes the blocked group worse than its un-blocked counterpart. Short interest is no longer one of
              these — it's a continuous scoring factor now (short_interest_rank), not a gate, after a working gate
              turned out to be excluding the best-performing shorts (see modules/scoring.py's own FACTOR_WEIGHTS
              comment).
              Portfolio = the gated Strong Buy long leg + gated Strong Sell short leg summed (dollar-neutral, each leg
              equal-weight 100% gross). The two rows below it are two DIFFERENT baselines, not variants of the same
              one. "All long" is the whole rated universe that week — every symbol, whichever side it was actually
              rated for, held LONG regardless (short-rated tickers have their position P&amp;L un-flipped back to a
              raw stock return first) — a plain buy-everything-equally benchmark, independent of the model's own
              side calls. "All rated long − all rated short" is narrower and model-aware: every Buy/Strong Buy (or
              Sell/Strong Sell) candidate that week on the side it was actually rated for, blocked rows included, no
              gates or Strong-only filter, combined into one dollar-neutral book the same way Portfolio is — so the
              gap versus Portfolio isolates what the gating/tiering itself is buying you, given the model's own long/
              short calls. Its "−" is the usual long-short trading sense (long this book, short that one), not a
              literal subtraction: short-side returns are already POSITION P&amp;L (sign flipped so a positive
              number means the short worked), so the combined figure is arithmetically a SUM of the two already-
              signed series. Every number here is the <strong>current model</strong> — that week's
              factor columns re-scored with today's scoring.py and gates (see modules/backtest.py's own
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
                    <th className="num" title="Compounded across the WHOLE backtest, every week ever scored -- not limited to the page of weeks shown below">
                      Compounded
                    </th>
                    {pagedWeeks.map((w) => (
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
                        <td className="col-left">
                          {r.sector}
                          {fmtN(avgCount(weeks.map((w) => r.byWeek[w.week])))}
                        </td>
                        <td className={`num ${signClass(compounded)}`}>{fmtPct(compounded)}</td>
                        {pagedWeeks.map((w) => {
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
                          <th className="num" title="Compounded across the WHOLE backtest, every week ever scored -- not limited to the page of weeks shown below">
                            Compounded
                          </th>
                          {pagedWeeks.map((w) => (
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
                              <td className="col-left">
                                {GATE_REASON_LABEL[reason]}
                                {fmtN(avgCount(weeks.map((w) => w.currentModel.blockedBreakdown[side]?.[reason])))}
                              </td>
                              <td className={`num ${signClass(compounded)}`}>{fmtPct(compounded)}</td>
                              {pagedWeeks.map((w) => {
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
                    {pagedWeeks.map((w) => (
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
                        <td className={`col-left ${GROUP_CLASS[r.group]}`}>{effectiveGroupLabel(r.group, r.pct)}</td>
                        <td className="col-left">
                          {r.blockedBy.length ? r.blockedBy.map((g) => GATE_REASON_LABEL[g]).join(', ') : '—'}
                        </td>
                        <td className="col-left">{r.rating}</td>
                        <td className={`num ${signClass(compounded)}`}>{fmtPct(compounded)}</td>
                        {pagedWeeks.map((w) => {
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

