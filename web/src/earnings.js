// Shared earnings-date urgency logic — used by PeTable.jsx (colors the
// Name cell) and Asset.jsx (labels the next earnings call), so both read
// the same signal off the same yfinance fields.

import { useEffect, useState } from 'react'

// Re-renders whatever calls this every `intervalMs` (default 1 minute) so
// earnings-urgency colors computed against the real current instant stay
// accurate as time actually passes, not just when fresh data happens to
// arrive — a color computed once at mount (or only refreshed on the next
// data fetch) would otherwise sit stale, e.g. "due within 24 hours" never
// aging into "24-72 hours" on its own.
export function useNowTick(intervalMs = 60000) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs)
    return () => clearInterval(id)
  }, [intervalMs])
  return now
}

const HOUR = 3600000
const DAY = 86400000

function isWeekendUTC(ms) {
  const day = new Date(ms).getUTCDay() // 0 = Sunday, 6 = Saturday
  return day === 0 || day === 6
}

// Milliseconds of "business time" between two instants (from <= to) —
// UTC Saturday/Sunday don't count at all, so a stretch that crosses a
// weekend measures as just its Mon-Fri portion, however many wall-clock
// hours actually elapsed. Walks day by day (at most ~6 iterations even
// for a 108-hour span), not hour by hour, so there's no rounding error
// at the boundaries. Exported for RecommendationsView.jsx's own earnings-
// within-N-days close-review check — same weekend-aware distance this
// file's own earningsUrgencyClass buckets already use, not a second,
// calendar-day-only implementation that would treat a Friday-evening
// earnings call as further out than it actually is in trading days.
export function businessMillisBetween(from, to) {
  if (to <= from) return 0
  let total = 0
  let cursor = from
  while (cursor < to) {
    const d = new Date(cursor)
    const nextMidnightUTC = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + 1)
    const segmentEnd = Math.min(nextMidnightUTC, to)
    if (!isWeekendUTC(cursor)) total += segmentEnd - cursor
    cursor = segmentEnd
  }
  return total
}

function utcDayKey(ms) {
  const d = new Date(ms)
  return `${d.getUTCFullYear()}-${d.getUTCMonth()}-${d.getUTCDate()}`
}

// earningsTimestampStart is Unix epoch seconds — an unambiguous UTC
// instant, no timezone guessing needed. `now` is the current instant in
// ms since epoch (pass Date.now(), ideally sourced from useNowTick above
// so this stays live). Four buckets, checked in order:
//   - earnings-reported (green): already happened, either later on the
//     same UTC calendar day or on the previous UTC calendar day.
//   - earnings-imminent (bright red): due within the next 24 BUSINESS
//     hours — UTC Saturday/Sunday don't count (see businessMillisBetween),
//     so a report due Monday morning still reads as imminent on a Friday
//     evening rather than looking safely ~2.5 calendar days out.
//   - earnings-near (orange): 24-72 business hours out.
//   - earnings-soon (yellow): 72-108 business hours out.
//   - '' otherwise — reported further in the past, or due further out
//     than 108 business hours.
export function earningsUrgencyClass(earningsEpochSec, now) {
  if (typeof earningsEpochSec !== 'number' || typeof now !== 'number') return ''
  const earningsMs = earningsEpochSec * 1000
  if (Number.isNaN(earningsMs)) return ''

  if (earningsMs <= now) {
    const earningsDay = utcDayKey(earningsMs)
    if (earningsDay === utcDayKey(now) || earningsDay === utcDayKey(now - DAY)) {
      return 'earnings-reported'
    }
    return ''
  }

  const businessHoursAway = businessMillisBetween(now, earningsMs) / HOUR
  if (businessHoursAway <= 24) return 'earnings-imminent'
  if (businessHoursAway <= 72) return 'earnings-near'
  if (businessHoursAway <= 108) return 'earnings-soon'
  return ''
}

// earningsTimestampStart is Unix epoch seconds — human-readable for
// tooltips/labels rather than the raw number a reader would have to
// mentally convert. It's not just a date: yfinance already bakes a real
// time-of-day into it (e.g. 12:30 UTC for a before-open report, 20:00 UTC
// for after-close) even for a confirmed (non-estimated) date, so this
// shows that time too, rendered in the viewer's own local timezone like
// every other timestamp in this app, plus a before-open/after-close
// label derived from the UTC hour (NYSE's session roughly maps to
// 13:30-20:00 UTC across both DST states, so <13:00 UTC is reliably
// pre-market and >=20:00 UTC is reliably post-market).
export function fmtEarningsDate(epochSec) {
  if (typeof epochSec !== 'number') return '—'
  const d = new Date(epochSec * 1000)
  if (Number.isNaN(d.getTime())) return '—'
  const datePart = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
  const timePart = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  const utcHour = d.getUTCHours()
  const session = utcHour < 13 ? 'before open' : utcHour >= 20 ? 'after close' : null
  return session ? `${datePart}, ${timePart} (${session})` : `${datePart}, ${timePart}`
}
