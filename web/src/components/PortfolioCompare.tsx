import { useMemo } from 'react'
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { PortfolioDayRow } from '../interfaces/IPortfolioView'
import { dayTotalPnl } from '../portfolioPnl'

// Portfolio page's "Compare" tab (explicit instruction): the live portfolio and the three
// backtest portfolios (Strong Buy + Strong Sell, 10% / 5% / 2.5%) on one chart, every
// series rebased to 100 on BASE_DATE (23 Sep 2026, the close before the live
// track record starts) and compounded from its own daily Total P&L %. The table under
// the chart lists exactly those daily returns and the index levels they produce.

export const COMPARE_BASE_DATE = '2026-09-23'
const CHART_FONT = 'ui-monospace, "SF Mono", "Cascadia Code", "Roboto Mono", Menlo, monospace'
const TICK = { fill: 'var(--muted)', fontSize: 10, fontFamily: CHART_FONT }

export type BacktestJson = {
  weeks?: { week: string; currentModel?: { daily?: { dates: string[]; series: Record<string, (number | null)[]> } } }[]
}

export const COMPARE_SERIES = [
  { key: 'live', label: 'Live (IB)', color: 'var(--accent)' },
  { key: 'portfolio', label: 'Backtest 10%', color: '#e08a1e' },
  { key: 'portfolioRestricted4', label: 'Backtest 5%', color: '#2f9e6e' },
  { key: 'portfolioRestricted2', label: 'Backtest 2.5%', color: '#b5476b' },
] as const

// Daily return by date for a backtest series, stitched across the weekly windows (a
// later week wins on an overlapping date).
export function backtestReturns(json: BacktestJson, key: string): Record<string, number> {
  const out: Record<string, number> = {}
  for (const w of [...(json.weeks ?? [])].sort((a, b) => a.week.localeCompare(b.week))) {
    const d = w.currentModel?.daily
    const series = d?.series?.[key]
    if (!d || !series) continue
    d.dates.forEach((date, i) => {
      const v = series[i]
      if (v !== null && v !== undefined) out[date] = v
    })
  }
  return out
}

// Live daily return = day's Total P&L / prior-day NAV (deposits never count as performance).
function liveReturns(rows: PortfolioDayRow[]): Record<string, number> {
  const out: Record<string, number> = {}
  let prevNav: number | null = null
  for (const r of rows) {
    const pnl = dayTotalPnl(r)
    if (pnl !== null && prevNav) out[r.date] = pnl / prevNav
    if (r.nav !== null) prevNav = r.nav
  }
  return out
}

const fmtPct = (v: number | null | undefined) => (v === null || v === undefined ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}%`)
const fmtIdx = (v: number | null | undefined) => (v === null || v === undefined ? '—' : v.toFixed(2))
const fmtAxisDate = (iso: string) => new Date(iso + 'T00:00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
const fmtDate = (iso: string) => new Date(iso + 'T00:00:00').toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })

// Sharpe chart: cumulative (since the base date) annualized Sharpe as of each day, from the
// daily returns above -- mean excess return / sample std dev x sqrt(252). Same risk-free
// convention as the rest of the page: 3.5%/yr for the live portfolio, 0 for the
// backtests. Needs MIN_SHARPE_DAYS observations; before that a Sharpe is just noise.
const TRADING_DAYS = 252
const MIN_SHARPE_DAYS = 5
const RISK_FREE: Record<string, number> = { live: 0.035, portfolio: 0, portfolioRestricted4: 0, portfolioRestricted2: 0 }

function cumulativeSharpe(returnsByRow: (number | null)[], riskFreeAnnual: number): (number | null)[] {
  const excess: number[] = []
  return returnsByRow.map((r) => {
    if (r === null) return null
    excess.push(r - riskFreeAnnual / TRADING_DAYS)
    const n = excess.length
    if (n < MIN_SHARPE_DAYS) return null
    const mean = excess.reduce((a, b) => a + b, 0) / n
    const sd = Math.sqrt(excess.reduce((a, b) => a + (b - mean) ** 2, 0) / (n - 1))
    return sd ? (mean / sd) * Math.sqrt(TRADING_DAYS) : null
  })
}

type Row = { date: string } & Record<string, number | null | string>

export default function PortfolioCompare({ liveRows, backtestJson }: { liveRows: PortfolioDayRow[] | null; backtestJson: BacktestJson | null }) {
  const table = useMemo(() => {
    if (!liveRows || !backtestJson) return null
    const rets: Record<string, Record<string, number>> = { live: liveReturns(liveRows) }
    for (const s of COMPARE_SERIES) if (s.key !== 'live') rets[s.key] = backtestReturns(backtestJson, s.key)
    // The window starts on the live portfolio's first date, ends on the latest date of any series.
    const liveFirst = liveRows.find((r) => r.date > COMPARE_BASE_DATE)?.date
    if (!liveFirst) return null
    const dates = [...new Set(Object.values(rets).flatMap((m) => Object.keys(m)))].filter((d) => d >= liveFirst).sort()
    const level: Record<string, number> = Object.fromEntries(COMPARE_SERIES.map((s) => [s.key, 100]))
    const rows: Row[] = [{ date: COMPARE_BASE_DATE, ...Object.fromEntries(COMPARE_SERIES.flatMap((s) => [[`${s.key}_idx`, 100], [`${s.key}_ret`, null]])) }]
    for (const date of dates) {
      const row: Row = { date }
      for (const s of COMPARE_SERIES) {
        const r = rets[s.key][date]
        if (r === undefined) {
          row[`${s.key}_ret`] = null
          row[`${s.key}_idx`] = null // series has no data for this day (e.g. live past its last NAV)
        } else {
          level[s.key] *= 1 + r
          row[`${s.key}_ret`] = r
          row[`${s.key}_idx`] = level[s.key]
        }
      }
      rows.push(row)
    }
    return rows
  }, [liveRows, backtestJson])

  if (!table) return <p className="status-row">Loading…</p>
  const all = table.flatMap((r) => COMPARE_SERIES.map((s) => r[`${s.key}_idx`] as number | null)).filter((v): v is number => v !== null)
  const lo = Math.min(...all)
  const hi = Math.max(...all)
  const pad = (hi - lo || 1) * 0.1
  // Plain calculation, not a hook: it sits after the "Loading" early return above.
  const perSharpe = Object.fromEntries(
    COMPARE_SERIES.map((s) => [s.key, cumulativeSharpe(table.map((r) => r[`${s.key}_ret`] as number | null), RISK_FREE[s.key])]),
  )
  const sharpeRows = table.map((r, i) => ({ date: r.date, ...Object.fromEntries(COMPARE_SERIES.map((s) => [`${s.key}_sh`, perSharpe[s.key][i]])) }))
  const sharpeVals = sharpeRows.flatMap((r) => COMPARE_SERIES.map((s) => (r as Record<string, unknown>)[`${s.key}_sh`] as number | null)).filter((v): v is number => v !== null)
  const shLo = sharpeVals.length ? Math.min(...sharpeVals, 0) : -1
  const shHi = sharpeVals.length ? Math.max(...sharpeVals, 0) : 1
  const shPad = (shHi - shLo || 1) * 0.1
  const lastSharpe = (key: string) => [...sharpeRows].reverse().map((r) => (r as Record<string, unknown>)[`${key}_sh`] as number | null).find((v) => v !== null)
  const last = (key: string) => [...table].reverse().find((r) => r[`${key}_idx`] !== null)?.[`${key}_idx`] as number | undefined

  return (
    <>
      <div className="asset-card">
        <h2>
          Live vs backtest portfolios <span className="stat-subvalue">index, 100 = {fmtAxisDate(COMPARE_BASE_DATE)}</span>
        </h2>
        <div className="chart-legend">
          {COMPARE_SERIES.map((s) => (
            <span key={s.key} style={{ marginRight: 14 }}>
              <span style={{ display: 'inline-block', width: 10, height: 10, borderRadius: 2, background: s.color, marginRight: 5 }} />
              {s.label} <strong>{fmtIdx(last(s.key))}</strong>
            </span>
          ))}
        </div>
        <div className="chart-wrap">
          <ResponsiveContainer width="100%" height={300}>
            <LineChart data={table} margin={{ top: 12, right: 4, bottom: 4, left: 4 }}>
              <CartesianGrid stroke="var(--line)" vertical={false} />
              <XAxis dataKey="date" tickFormatter={fmtAxisDate} tick={TICK} axisLine={false} tickLine={false} minTickGap={24} />
              <YAxis domain={[lo - pad, hi + pad]} tickFormatter={(v: number) => v.toFixed(1)} orientation="right" width={52} axisLine={false} tickLine={false} tick={TICK} />
              <ReferenceLine y={100} stroke="var(--muted)" strokeDasharray="3 3" strokeOpacity={0.7} />
              <Tooltip
                offset={12}
                labelFormatter={(d) => fmtDate(String(d))}
                formatter={(v, name) => [fmtIdx(typeof v === 'number' ? v : null), name]}
                contentStyle={{ background: 'var(--surface)', border: '1px solid var(--line)', borderRadius: 6, fontSize: 12 }}
              />
              {COMPARE_SERIES.map((s) => (
                <Line key={s.key} type="monotone" dataKey={`${s.key}_idx`} name={s.label} stroke={s.color} strokeWidth={2} dot={false} connectNulls={false} />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="asset-card">
        <h2 title={`Cumulative annualized Sharpe since the base date: mean excess daily return / std dev x sqrt(252), shown from day ${MIN_SHARPE_DAYS}. Risk-free rate 3.5%/yr for the live portfolio, 0 for the backtests.`}>
          Sharpe ratio <span className="stat-subvalue">cumulative, annualized, from day {MIN_SHARPE_DAYS}</span>
        </h2>
        <div className="chart-legend">
          {COMPARE_SERIES.map((s) => (
            <span key={s.key} style={{ marginRight: 14 }}>
              <span style={{ display: 'inline-block', width: 10, height: 10, borderRadius: 2, background: s.color, marginRight: 5 }} />
              {s.label} <strong>{lastSharpe(s.key)?.toFixed(2) ?? '—'}</strong>
            </span>
          ))}
        </div>
        <div className="chart-wrap">
          <ResponsiveContainer width="100%" height={260}>
            <LineChart data={sharpeRows} margin={{ top: 12, right: 4, bottom: 4, left: 4 }}>
              <CartesianGrid stroke="var(--line)" vertical={false} />
              <XAxis dataKey="date" tickFormatter={fmtAxisDate} tick={TICK} axisLine={false} tickLine={false} minTickGap={24} />
              <YAxis domain={[shLo - shPad, shHi + shPad]} tickFormatter={(v: number) => v.toFixed(1)} orientation="right" width={52} axisLine={false} tickLine={false} tick={TICK} />
              <ReferenceLine y={0} stroke="var(--muted)" strokeDasharray="3 3" strokeOpacity={0.7} />
              <Tooltip
                offset={12}
                labelFormatter={(d) => fmtDate(String(d))}
                formatter={(v, name) => [typeof v === 'number' ? v.toFixed(2) : '—', name]}
                contentStyle={{ background: 'var(--surface)', border: '1px solid var(--line)', borderRadius: 6, fontSize: 12 }}
              />
              {COMPARE_SERIES.map((s) => (
                <Line key={s.key} type="monotone" dataKey={`${s.key}_sh`} name={s.label} stroke={s.color} strokeWidth={2} dot={false} connectNulls={false} />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="table-wrap positions-table-wrap">
        <table>
          <thead>
            <tr>
              <th className="col-left" rowSpan={2}>Date</th>
              {COMPARE_SERIES.map((s) => (
                <th key={s.key} colSpan={2} style={{ textAlign: 'center', color: s.color }}>{s.label}</th>
              ))}
            </tr>
            <tr>
              {COMPARE_SERIES.flatMap((s) => [
                <th key={`${s.key}r`} title="Daily Total P&L / prior-day NAV (live) or the backtest's daily portfolio return">Total P&amp;L %</th>,
                <th key={`${s.key}i`} title="Compounded index, 100 = the base date">Index</th>,
              ])}
            </tr>
          </thead>
          <tbody>
            {[...table].reverse().map((r) => (
              <tr key={r.date}>
                <td className="col-left">{r.date === COMPARE_BASE_DATE ? `${fmtDate(r.date)} (base)` : fmtDate(r.date)}</td>
                {COMPARE_SERIES.flatMap((s) => {
                  const ret = r[`${s.key}_ret`] as number | null
                  return [
                    <td key={`${s.key}r`} className={`num ${ret === null ? '' : ret >= 0 ? 'good' : 'bad'}`}>{fmtPct(ret)}</td>,
                    <td key={`${s.key}i`} className="num">{fmtIdx(r[`${s.key}_idx`] as number | null)}</td>,
                  ]
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
