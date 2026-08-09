// Shared screener column/formatting definitions — factored out of
// PeTable.jsx (rather than exported straight from it) because that file's
// default export is a component; exporting plain constants/functions
// alongside it breaks React Fast Refresh (react-refresh/only-export-
// components). PositionsView.jsx's value-weighted portfolio-factors table
// imports from here to render the exact same columns/formatting as the
// screener without duplicating (and risking drifting from) this logic.

export const COLUMNS = [
  { key: 't', label: 'Ticker', className: 'col-left col-ticker' },
  { key: 'n', label: 'Name', className: 'col-left col-name', sortable: false },
  { key: 's', label: 'Industry', className: 'col-left' },
  { key: 'possize', label: 'Position', fmt: 'num0', sortable: false },
  { key: 'posval', label: 'Pos Value', fmt: 'money', sortable: false },
  { key: 'rating', label: 'Rating', className: 'col-rec' },
  { key: 'savgpe', label: 'Avg PE', fmt: 'num2' },
  { key: 'fpe', label: 'Fwd PE', fmt: 'num2' },
  { key: 'feps', label: 'Fwd EPS', fmt: 'price' },
  { key: 'epsTrend', label: 'EPS Trend', fmt: 'pct' },
  { key: 'tpe', label: 'Trail PE', fmt: 'num2' },
  { key: 'peg', label: 'PEG', fmt: 'num2' },
  { key: 'revg', label: 'Rev Growth', fmt: 'pct' },
  { key: 'pfcf', label: 'P/FCF', fmt: 'num2' },
  { key: 'evEbitda', label: 'EV/EBITDA', fmt: 'num2' },
  { key: 'opMargin', label: 'Op Margin', fmt: 'pct' },
  { key: 'de', label: 'D/E', fmt: 'num2' },
  { key: 'liq', label: 'Liq Ratio', fmt: 'num2' },
  { key: 'shortInt', label: 'Short Interest', fmt: 'pct' },
  { key: 'p', label: 'Price', fmt: 'price' },
  { key: 'tgt', label: 'Target', fmt: 'price' },
  { key: 'upside', label: 'Upside', fmt: 'pct' },
  { key: 'rec', label: 'Rec', className: 'col-rec' },
  { key: 'mom', label: 'Momentum', fmt: 'signed' },
  { key: 'mr', label: 'MeanRev', fmt: 'signed' },
  { key: 'sent', label: 'Sentiment', fmt: 'num2' },
  { key: 'newsSent', label: 'News', fmt: 'num2' },
  { key: 'sc', label: 'Score', fmt: 'score' },
  { key: 'rank', label: 'Percentile' },
  { key: 'upd', label: 'Updated', className: 'col-left' },
]

export function toNum(v) {
  if (v === undefined || v === null || v === '') return null
  const n = Number(v)
  return Number.isNaN(n) ? null : n
}

export function fmtNum(v) {
  if (v === null) return '—'
  if (!Number.isFinite(v)) return v > 0 ? '∞' : '−∞'
  return v.toFixed(1)
}

export function fmtPct(v) {
  if (v === null) return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%'
}

// Momentum is a Sharpe-style risk-adjusted ratio (trend slope divided by
// its own volatility — see IBApp._regression_momentum), not a return, so
// it doesn't get fmtPct's *100/'%' treatment; a signed 2-decimal number
// reads as "units of risk-adjusted trend strength" instead of implying a
// percentage return that isn't what this value means.
export function fmtSigned(v) {
  if (v === null) return '—'
  if (!Number.isFinite(v)) return v > 0 ? '∞' : '−∞'
  return (v >= 0 ? '+' : '') + v.toFixed(2)
}

// debtToEquity comes from yfinance already in percentage units (150.5 means
// 150.5%), unlike revg/upside/perf which are fractions — no *100 here.
export function fmtDebtToEquity(v) {
  if (v === null) return '—'
  return v.toFixed(1) + '%'
}

export function fmtPrice(v) {
  if (v === null) return '—'
  return '$' + v.toFixed(1)
}

export function fmtScore(v) {
  if (v === null) return '—'
  return v.toFixed(3)
}

// StockTwits sentiment score: (bullish - bearish) / tagged messages, in
// [-1, 1]. Shown with an explicit sign since 0 is a meaningful midpoint
// (evenly split), not "no signal" — that's what '—' is for.
export function fmtSentiment(v) {
  if (v === null) return '—'
  return (v >= 0 ? '+' : '') + v.toFixed(2)
}

const REC_LABELS = {
  strong_buy: 'Strong Buy',
  buy: 'Buy',
  hold: 'Hold',
  underperform: 'Underperform',
  sell: 'Sell',
  none: 'N/A',
}

// strong_buy green, buy light green, hold/none/unknown neutral white,
// underperform yellow, sell red.
const REC_CLASSES = {
  strong_buy: 'rec-strong-buy',
  buy: 'rec-buy',
  underperform: 'rec-underperform',
  sell: 'rec-sell',
}

export function recLabel(key) {
  if (!key) return '—'
  return REC_LABELS[key] || key
}

export function recClass(key) {
  return REC_CLASSES[key] || 'rec-neutral'
}

// main.py's own forced-distribution rating (see rating_for_percentile) —
// a Zacks-Rank-style label from this screener's own score percentile, not
// Wall Street's analyst consensus (that's the separate Rec column). Reuses
// the same .rec-badge visual language; rec-strong-sell is new (a more
// intense red than plain rec-sell, mirroring strong-buy vs. buy).
const RATING_CLASSES = {
  'Strong Buy': 'rec-strong-buy',
  Buy: 'rec-buy',
  Hold: 'rec-neutral',
  Sell: 'rec-sell',
  'Strong Sell': 'rec-strong-sell',
}

export function ratingClass(rating) {
  return RATING_CLASSES[rating] || 'rec-neutral'
}

// news_sentiment.json (see ib_price_server.py's news_loop) is
// {ticker: {articleId: score}} — every headline FinBERT scored 1 (very
// bearish) to 5 (very bullish) over the same rolling news.json window.
// Neutral (score 3) headlines are dropped before averaging — routine
// filings/dividend notices shouldn't pull a ticker's score toward
// "neutral" any more than having no news at all would; they just
// shouldn't count.
export function avgNewsSentiment(articles) {
  if (!articles) return { avg: null, count: 0 }
  const scores = Object.values(articles).filter((s) => s !== 3)
  if (!scores.length) return { avg: null, count: 0 }
  return { avg: scores.reduce((a, b) => a + b, 0) / scores.length, count: scores.length }
}
