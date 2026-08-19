// Shared formatting for FinBERT-scored headlines (see news_sentiment.py /
// ib_server.py's news_loop) -- used by both Asset.jsx's NewsPanel and
// NewsPopup.jsx's per-ticker news table, so the two views can't drift.

// FinBERT's 1 (very bearish) - 5 (very bullish) score. Labeled rather than
// shown as a bare number since there's no natural unit for a reader to
// anchor "3.6" to the way there is for a price or a percentage.
export const SENTIMENT_LABEL = { 1: 'Very Bearish', 2: 'Bearish', 3: 'Neutral', 4: 'Bullish', 5: 'Very Bullish' }

export function sentimentClass(score) {
  if (typeof score !== 'number') return ''
  if (score <= 2) return 'bad'
  if (score >= 4) return 'good'
  return ''
}

// IB Gateway's own historical-news timestamp has no timezone suffix but is
// UTC (matches ib_server.py's news_by_ticker/isoparse handling) --
// appending 'Z' before parsing keeps this consistent with that, rather than
// letting the browser interpret it as local time.
export function fmtNewsTime(iso) {
  const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z')
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}
