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
  { key: 'p', label: 'Price', fmt: 'price' },
  { key: 'beta', label: 'Beta', fmt: 'num2' },
  { key: 'savgpe', label: 'Avg PE', fmt: 'num2' },
  { key: 'fpe', label: 'Fwd PE', fmt: 'num2' },
  { key: 'feps', label: 'Fwd EPS', fmt: 'price' },
  { key: 'epsTrend', label: 'EPS Trend', fmt: 'pct' },
  { key: 'tpe', label: 'Trail PE', fmt: 'num2' },
  { key: 'tps', label: 'Trail PS', fmt: 'num2' },
  { key: 'peg', label: 'PEG', fmt: 'num2' },
  { key: 'revg', label: 'Rev Growth', fmt: 'pct' },
  { key: 'earnG', label: 'Earn Growth', fmt: 'pct' },
  { key: 'pfcf', label: 'P/FCF', fmt: 'num2' },
  { key: 'evEbitda', label: 'EV/EBITDA', fmt: 'num2' },
  { key: 'opMargin', label: 'Op Margin', fmt: 'pct' },
  { key: 'epsVol', label: 'EPS Vol', fmt: 'pct0' },
  { key: 'de', label: 'D/E', fmt: 'num2' },
  { key: 'liq', label: 'Liq Ratio', fmt: 'num2' },
  { key: 'shortInt', label: 'Short Interest', fmt: 'pct' },
  { key: 'tgt', label: 'Target', fmt: 'price' },
  { key: 'upside', label: 'Upside', fmt: 'pct' },
  { key: 'rec', label: 'Rec', className: 'col-rec' },
  { key: 'mom', label: 'MSI', fmt: 'index100' },
  { key: 'mr', label: 'ST-MSI', fmt: 'index100' },
  { key: 'sent', label: 'Sentiment', fmt: 'index100' },
  { key: 'newsSent', label: 'News', fmt: 'index100' },
  { key: 'instChange', label: 'Inst Change', fmt: 'index100' },
  { key: 'insiders', label: 'Insiders', fmt: 'index100' },
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
  if (v === null || v === undefined) return '—'
  if (!Number.isFinite(v)) return v > 0 ? '∞' : '−∞'
  return v.toFixed(1)
}

export function fmtPct(v) {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%'
}

// No sign, no decimals -- for a magnitude, not a signed change (e.g.
// epsVol, which is never negative and doesn't need fmtPct's own "+"
// prefix implying a direction that isn't really there).
export function fmtPct0(v) {
  if (v === null || v === undefined) return '—'
  return Math.round(v * 100) + '%'
}

// debtToEquity comes from yfinance already in percentage units (150.5 means
// 150.5%), unlike revg/upside/perf which are fractions — no *100 here.
export function fmtDebtToEquity(v) {
  if (v === null || v === undefined) return '—'
  return v.toFixed(1) + '%'
}

export function fmtPrice(v) {
  if (v === null || v === undefined) return '—'
  return '$' + v.toFixed(1)
}

export function fmtScore(v) {
  if (v === null || v === undefined) return '—'
  return v.toFixed(3)
}

// StockTwits sentiment score: (bullish - bearish) / tagged messages, in
// [-1, 1]. Shown with an explicit sign since 0 is a meaningful midpoint
// (evenly split), not "no signal" — that's what '—' is for.
export function fmtSentiment(v) {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '') + v.toFixed(2)
}

// Momentum/MeanRev/Sentiment/News/Insiders sit on genuinely different raw
// scales (a bounded [0, 100] Money Flow Index/RSI reading for the first
// two, a bounded [-1, 1] ratio for the other three) — rankTo100 rescales
// all five onto one common, visually comparable [-100, 100] index by
// RANK (ordinal position in the sorted dataset), not by raw magnitude.
// Scale-agnostic by design, so it kept working unchanged when momentum/
// meanReversion switched from an unbounded Sharpe-style regression score
// to MFI/RSI: a min-max rescale (value's linear position between the
// observed min and max) was tried first, back when momentum's raw range
// really was unbounded (confirmed live at the time: -4..+305 purely
// because of one outlier ticker, which put 95% of every OTHER ticker
// below -90) -- a single extreme reading crushed the entire rest of the
// distribution into a sliver near one end, which is exactly the
// degenerate case rank-based avoids: the lowest value is still -100, the
// highest still +100, but everything between is spread out by ordinal
// position, immune to how far away the extremes happen to sit. Same
// principle scoring.py's own rank_ascending already uses for the backend
// composite score, for the same reason.
//
// Takes the whole column at once (not one value at a time, the way
// min-max's old value/min/max signature did) since rank needs the full
// sorted set to know each value's position — returns an array the same
// length as `values`, in the same order, with null preserved for a
// missing/non-finite input rather than assigning it a rank.
//
// Ties share the AVERAGE rank of their span, not each getting a
// different position by array order -- Insiders' raw score in
// particular is a small discrete ratio (buys-sells)/(buys+sells), so
// dozens of tickers commonly land on the exact same value (e.g. every
// "all sells, no buys" ticker is -1.0); without this they'd arbitrarily
// scatter across nearby-but-different scores (-100, -99.6, -99.1, ...)
// instead of all showing the one identical score their identical signal
// actually earned.
export function rankTo100(values) {
  const withIndex = values.map((v, i) => ({ v, i }))
  const valid = withIndex.filter((x) => x.v !== null && Number.isFinite(x.v))
  valid.sort((a, b) => a.v - b.v)
  const n = valid.length
  const result = new Array(values.length).fill(null)
  let i = 0
  while (i < n) {
    let j = i
    while (j + 1 < n && valid[j + 1].v === valid[i].v) j++
    const avgRank = (i + j) / 2
    const scaled = n > 1 ? (avgRank / (n - 1)) * 200 - 100 : 0
    for (let k = i; k <= j; k++) result[valid[k].i] = scaled
    i = j + 1
  }
  return result
}

// Shared display treatment for the normalizeTo100 family — not a
// percentage, not a raw ratio, just a signed rescaled index.
export function fmtIndex100(v) {
  if (v === null || v === undefined) return '—'
  return (v >= 0 ? '+' : '') + v.toFixed(1)
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

// news_sentiment.json (see ib_server.py's news_loop) is
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

// insider_transactions.json (see sec_edgar.py's fetch_form4) is
// {ticker: [{..., transactions: [{code, ...}, ...]}, ...]} — one entry
// per SEC Form 4 filing, each carrying its own list of non-derivative
// transactions. Only open-market codes count as a discretionary buy/sell
// signal (P = purchase, S = sale) — routine compensation mechanics
// (M = option exercise, F = tax withholding, A = grant, G = gift, ...)
// aren't a conviction signal and are excluded, same reasoning
// avgNewsSentiment's neutral-headline exclusion above uses: they
// shouldn't pull the score toward anything, they just shouldn't count.
export function avgInsiderScore(filings) {
  if (!filings) return { avg: null, buys: 0, sells: 0 }
  let buys = 0
  let sells = 0
  for (const filing of filings) {
    for (const tx of filing.transactions || []) {
      if (tx.code === 'P') buys += 1
      else if (tx.code === 'S') sells += 1
    }
  }
  const total = buys + sells
  return { avg: total ? (buys - sells) / total : null, buys, sells }
}
