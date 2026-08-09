import { useMemo } from 'react'

const MONTH_LABELS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function fmtPct(v) {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(2) + '%'
}

// Monthly returns table (years down the rows, months across the columns) —
// the classic hedge-fund-style performance grid. Receives the same raw
// daily performance rows PortfolioView fetches from
// portfolio_performance.json (not a pre-computed return series) and does
// the daily-to-monthly transform itself: each day's return is that day's
// Total P&L (realized + unrealized) over the PRIOR day's NAV — the same
// money-weighted definition PortfolioView's own Total P&L % column and
// Sharpe/Sortino stats use, so a deposit/withdrawal doesn't get misread
// as performance — and a month's return is the geometric compounding of
// its daily returns (the product of (1 + daily), minus 1), not a simple
// sum, which is the correct way to combine returns over time.
export default function MonthlyReturnsTable({ rows }) {
  const { years, cellByYearMonth, ytdByYear } = useMemo(() => {
    const cellByYearMonth = new Map() // "YYYY-MM" -> compounded monthly return
    let prevNav = null
    let currentKey = null
    let compounded = null

    // rows is date-ascending, so a single pass can compound each month's
    // days in order and flush (commit) a month's total the moment the
    // next month's first day is seen.
    const flush = () => {
      if (currentKey !== null && compounded !== null) {
        cellByYearMonth.set(currentKey, compounded - 1)
      }
    }

    for (const r of rows) {
      const totalPnl = r.realized !== null && r.unrealized !== null ? r.realized + r.unrealized : null
      // prevNav only advances on a day with a real NAV, same rule the
      // backend's own _apply_unrealized_from_nav uses, so a gap in the
      // data doesn't corrupt the next real day's return.
      const dailyReturn = totalPnl !== null && prevNav ? totalPnl / prevNav : null
      if (r.nav !== null) prevNav = r.nav
      if (dailyReturn === null) continue

      const key = r.date.slice(0, 7) // "YYYY-MM"
      if (key !== currentKey) {
        flush()
        currentKey = key
        compounded = 1
      }
      compounded *= 1 + dailyReturn
    }
    flush()

    const years = [...new Set([...cellByYearMonth.keys()].map((k) => k.slice(0, 4)))].sort()

    // YTD per year: geometric compounding of that year's own already-
    // compounded monthly cells (not a separate pass over daily rows) —
    // guarantees the YTD column is always exactly consistent with
    // whatever monthly figures are actually displayed next to it.
    const ytdByYear = new Map()
    for (const year of years) {
      let ytd = 1
      let any = false
      for (const [key, monthlyReturn] of cellByYearMonth) {
        if (key.slice(0, 4) === year) {
          ytd *= 1 + monthlyReturn
          any = true
        }
      }
      if (any) ytdByYear.set(year, ytd - 1)
    }

    return { years, cellByYearMonth, ytdByYear }
  }, [rows])

  if (years.length === 0) return null

  return (
    <div className="asset-card">
      <h2>Monthly Performance</h2>
      <div className="table-wrap">
        <table className="monthly-returns-table">
          <thead>
            <tr>
              <th className="col-left">Year</th>
              {MONTH_LABELS.map((label) => (
                <th key={label}>{label}</th>
              ))}
              <th>YTD</th>
            </tr>
          </thead>
          <tbody>
            {years.map((year) => {
              const ytd = ytdByYear.get(year)
              return (
                <tr key={year}>
                  <td className="col-left">{year}</td>
                  {MONTH_LABELS.map((_, i) => {
                    const key = `${year}-${String(i + 1).padStart(2, '0')}`
                    const v = cellByYearMonth.get(key)
                    return (
                      <td key={key} className={`num ${v === undefined ? '' : v >= 0 ? 'good' : 'bad'}`}>
                        {v === undefined ? '—' : fmtPct(v)}
                      </td>
                    )
                  })}
                  <td className={`num ytd-cell ${ytd === undefined ? '' : ytd >= 0 ? 'good' : 'bad'}`}>
                    {ytd === undefined ? '—' : fmtPct(ytd)}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
