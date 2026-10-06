import { useMemo } from 'react'
import type { BacktestWeek } from '../interfaces/IBacktestingView'

// Backtesting > Daily tab: the same portfolios as the weekly view, one row
// per TRADING DAY, newest first (explicit instruction). Each weekly cohort
// (formed at that week's Friday snapshot, held up to 5 trading days with the
// 1.6σ take-profit limit) contributes the days of its own holding period, so
// consecutive weeks tile into one continuous daily series. Each value is the
// portfolio's return on the PREVIOUS day's value (long leg + short leg, each
// equal-weight 100%), so compounding a week's days gives its weekly return and
// the Compounded row matches the Weekly tab. When two weekly portfolios are
// both held on one date (a holiday week makes the old one run a day into the
// next, e.g. 14 Sep), that date's value is the two compounded together --
// exactly what the weekly Compounded does. Not a re-ranked daily portfolio:
// the model is only snapshotted weekly.

const COLUMNS: { key: string; label: string; title: string }[] = [
  { key: 'portfolio', label: 'Portfolio', title: 'Long Strong Buy + Short Strong Sell (10% cuts), each leg equal-weight 100%, plus the entry-rule trades (Strong Buy / Strong Sell left out by the daily-move gate or closed by take-profit), 2% of NAV each, held to the week end' },
  { key: 'portfolioRestricted4', label: 'Portfolio 5%', title: 'Same with the 5% cuts (entry candidates restricted to the same cut)' },
  { key: 'portfolioRestricted2', label: 'Portfolio 2.5%', title: 'Same with the 2.5% cuts (entry candidates restricted to the same cut)' },
  { key: 'all_long', label: 'All long', title: 'Benchmark: every rated name that week, equal-weight, treated as a long' },
  { key: 'all_rated_long', label: 'All rated long', title: 'Every name rated for the long side (Strong Buy, Buy, blocked), equal-weight' },
  { key: 'all_rated_short', label: 'All rated short', title: 'Every name rated for the short side (Strong Sell, Sell, blocked), equal-weight, position P&L' },
  { key: 'allRatedLongShort', label: 'All rated long − short', title: 'All rated long + all rated short (each leg equal-weight 100%): the no-filter dollar-neutral baseline' },
  { key: 'long_strong_buy', label: 'Long SB', title: 'Long leg: gated Strong Buy (10%)' },
  { key: 'short_strong_sell', label: 'Short SS', title: 'Short leg: gated Strong Sell (10%)' },
  { key: 'long_strong_buy_restricted_4', label: 'Long SB 5%', title: 'Long leg, 5% cut' },
  { key: 'short_strong_sell_restricted_4', label: 'Short SS 5%', title: 'Short leg, 5% cut' },
  { key: 'long_strong_buy_restricted_2', label: 'Long SB 2.5%', title: 'Long leg, 2.5% cut' },
  { key: 'short_strong_sell_restricted_2', label: 'Short SS 2.5%', title: 'Short leg, 2.5% cut' },
]

const TRADING_DAYS_PER_YEAR = 252

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(2) + '%'
}

function signClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return ''
  return v > 0 ? 'good' : v < 0 ? 'bad' : ''
}

function fmtDate(iso: string): string {
  const d = new Date(iso + 'T00:00:00')
  return d.toLocaleDateString(undefined, { weekday: 'short', year: 'numeric', month: 'short', day: 'numeric' })
}

export default function BacktestDailyTable({ weeks }: { weeks: BacktestWeek[] }) {
  // {date: {key: return}}, one entry per trading day across all weeks.
  const { rows, totals } = useMemo(() => {
    const byDate = new Map<string, Record<string, number | null>>()
    for (const w of weeks) {
      const d = w.currentModel.daily
      if (!d) continue
      d.dates.forEach((date, i) => {
        const row = byDate.get(date) ?? {}
        for (const c of COLUMNS) {
          const v = d.series[c.key]?.[i] ?? null
          const prev = row[c.key]
          // two weekly portfolios on the same date: compound them (see the header comment)
          row[c.key] = v === null ? (prev ?? null) : prev === null || prev === undefined ? v : (1 + prev) * (1 + v) - 1
        }
        byDate.set(date, row)
      })
    }
    const dates = [...byDate.keys()].sort().reverse()
    const totals: Record<
      string,
      { compounded: number | null; avg: number | null; annReturn: number | null; annVol: number | null; sharpe: number | null }
    > = {}
    for (const c of COLUMNS) {
      const vals = dates.map((dt) => byDate.get(dt)?.[c.key]).filter((v): v is number => v !== null && v !== undefined)
      const compounded = vals.length ? vals.reduce((acc, v) => acc * (1 + v), 1) - 1 : null
      const avg = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null
      // Annualized over 252 trading days: return = (1+compounded)^(252/days)-1
      // (geometric), vol = sample stdev of the daily values x sqrt(252), Sharpe
      // = mean daily / stdev x sqrt(252) -- no risk-free rate, same as the Weekly tab.
      const sd =
        vals.length > 1 && avg !== null ? Math.sqrt(vals.reduce((a, v) => a + (v - avg) ** 2, 0) / (vals.length - 1)) : null
      totals[c.key] = {
        compounded,
        avg,
        annReturn: compounded !== null && vals.length ? Math.pow(1 + compounded, TRADING_DAYS_PER_YEAR / vals.length) - 1 : null,
        annVol: sd !== null ? sd * Math.sqrt(TRADING_DAYS_PER_YEAR) : null,
        sharpe: sd && avg !== null ? (avg / sd) * Math.sqrt(TRADING_DAYS_PER_YEAR) : null,
      }
    }
    return { rows: dates.map((date) => ({ date, values: byDate.get(date) as Record<string, number | null> })), totals }
  }, [weeks])

  if (rows.length === 0) return <div className="asset-card">No daily data yet -- re-run <code>python main.py backtest</code>.</div>

  return (
    <div className="asset-card backtest-daily">
      <h2 title="Return of each portfolio on each trading day (newest first). Each week's cohort is held up to 5 trading days from its Friday snapshot, with the take-profit limit; compounding a week's daily values gives its weekly return.">
        Daily backtest
      </h2>
      <p className="news-industry-note">
        {rows.length} trading days, {rows[rows.length - 1].date} to {rows[0].date} · return per day, each leg equal-weight 100%
      </p>
      <div className="backtest-daily-scroll">
        <table>
          <thead>
            <tr>
              <th className="col-left">Date</th>
              {COLUMNS.map((c) => (
                <th key={c.key} title={c.title}>
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr className="backtest-daily-total">
              <td className="col-left">Compounded</td>
              {COLUMNS.map((c) => (
                <td key={c.key} className={`num ${signClass(totals[c.key].compounded)}`}>
                  {fmtPct(totals[c.key].compounded)}
                </td>
              ))}
            </tr>
            <tr className="backtest-daily-total">
              <td className="col-left">Average / day</td>
              {COLUMNS.map((c) => (
                <td key={c.key} className={`num ${signClass(totals[c.key].avg)}`}>
                  {fmtPct(totals[c.key].avg)}
                </td>
              ))}
            </tr>
            <tr className="backtest-daily-total">
              <td className="col-left" title="(1 + Compounded)^(252 / trading days) − 1">Ann. return</td>
              {COLUMNS.map((c) => (
                <td key={c.key} className={`num ${signClass(totals[c.key].annReturn)}`}>
                  {fmtPct(totals[c.key].annReturn)}
                </td>
              ))}
            </tr>
            <tr className="backtest-daily-total">
              <td className="col-left" title="Sample standard deviation of the daily values × √252">Ann. vol</td>
              {COLUMNS.map((c) => (
                <td key={c.key} className="num">
                  {totals[c.key].annVol === null ? '—' : (totals[c.key].annVol! * 100).toFixed(2) + '%'}
                </td>
              ))}
            </tr>
            <tr className="backtest-daily-total">
              <td className="col-left" title="Average daily value ÷ standard deviation × √252, no risk-free rate">Sharpe</td>
              {COLUMNS.map((c) => (
                <td key={c.key} className={`num ${signClass(totals[c.key].sharpe)}`}>
                  {totals[c.key].sharpe === null ? '—' : totals[c.key].sharpe!.toFixed(2)}
                </td>
              ))}
            </tr>
            {rows.map((r) => (
              <tr key={r.date}>
                <td className="col-left">{fmtDate(r.date)}</td>
                {COLUMNS.map((c) => (
                  <td key={c.key} className={`num ${signClass(r.values[c.key])}`}>
                    {fmtPct(r.values[c.key])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
