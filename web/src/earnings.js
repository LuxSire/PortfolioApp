// Shared earnings-date urgency logic — used by PeTable.jsx (colors the
// Name cell) and Asset.jsx (labels the next earnings call), so both read
// the same signal off the same yfinance fields.

function dayKey(d) {
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`
}

// earningsTimestampStart is Unix epoch seconds; lastDownload is the ISO
// timestamp the row was fetched at. Calendar day (not raw hour count) is
// what decides "same day": already reported today = green, still due later
// today = red. Otherwise it's a calendar-day count from lastDownload's day —
// 1-2 days out = orange, 2-5 days out = yellow.
export function earningsUrgencyClass(earningsEpochSec, lastDownload) {
  if (typeof earningsEpochSec !== 'number' || !lastDownload) return ''
  const ernDate = new Date(earningsEpochSec * 1000)
  const refDate = new Date(lastDownload)
  if (Number.isNaN(ernDate.getTime()) || Number.isNaN(refDate.getTime())) return ''

  if (dayKey(ernDate) === dayKey(refDate)) {
    return ernDate.getTime() <= refDate.getTime() ? 'earnings-today-past' : 'earnings-today-future'
  }

  const ernMidnight = new Date(ernDate.getFullYear(), ernDate.getMonth(), ernDate.getDate()).getTime()
  const refMidnight = new Date(refDate.getFullYear(), refDate.getMonth(), refDate.getDate()).getTime()
  const daysAway = (ernMidnight - refMidnight) / 86400000

  if (daysAway >= 1 && daysAway <= 2) return 'earnings-near'
  if (daysAway > 2 && daysAway <= 5) return 'earnings-soon'
  return ''
}

// earningsTimestampStart is Unix epoch seconds — human-readable for
// tooltips/labels rather than the raw number a reader would have to
// mentally convert.
export function fmtEarningsDate(epochSec) {
  if (typeof epochSec !== 'number') return '—'
  const d = new Date(epochSec * 1000)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}
