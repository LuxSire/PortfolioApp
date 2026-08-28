// Shared formatting for FinBERT-scored headlines (see news_sentiment.py /
// ib_server.py's news_loop) -- used by both Asset.jsx's NewsPanel and
// NewsPopup.jsx's per-ticker news table, so the two views can't drift.

// FinBERT's 1 (very bearish) - 5 (very bullish) score. Labeled rather than
// shown as a bare number since there's no natural unit for a reader to
// anchor "3.6" to the way there is for a price or a percentage.
export const SENTIMENT_LABEL = { 1: 'S Bearish', 2: 'Bearish', 3: 'Neutral', 4: 'Bullish', 5: 'S Bullish' }

export function sentimentClass(score) {
  if (typeof score !== 'number') return ''
  if (score <= 2) return 'bad'
  if (score >= 4) return 'good'
  return ''
}

// headline_importance's 0 (Low) / 1 (Medium) / 3 (High) star count -- a
// separate "does this headline matter at all" read, independent of
// sentiment above (see news_sentiment.py's own module comment on why
// these are deliberately different questions). Rendered as literal star
// characters rather than a number, same "no natural unit" reasoning
// SENTIMENT_LABEL's own comment gives -- 3 stars for a genuine earnings/
// guidance/FDA/recall event, 1 for routine-but-real news, none at all for
// a recognized noise template (a market recap, a routine Form 4 filing,
// a solicitation ad, etc.).
const IMPORTANCE_LABEL = { 0: 'Low importance', 1: 'Medium importance', 3: 'High importance' }

export function importanceStars(value) {
  if (typeof value !== 'number' || value <= 0) return ''
  return '★'.repeat(value)
}

export function importanceTitle(value) {
  if (typeof value !== 'number') return ''
  return IMPORTANCE_LABEL[value] || ''
}

// IB Gateway's own historical-news timestamp has no timezone suffix but is
// UTC (matches ib_server.py's news_by_ticker/isoparse handling) --
// appending 'Z' before parsing keeps this consistent with that, rather than
// letting the browser interpret it as local time.
export function fmtNewsTime(iso) {
  const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z')
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}
