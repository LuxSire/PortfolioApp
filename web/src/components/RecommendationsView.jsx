import { useEffect, useMemo, useState } from 'react'
import { parseCSV } from '../csv'
import { IB_STREAM_URL } from '../ibStream'
import { fmtPct, fmtPrice, fmtSigned, ratingClass } from '../screenerFactors'
import RecommendationsChatbot from './RecommendationsChatbot'

const BUY_RATINGS = new Set(['Strong Buy', 'Buy'])
const SELL_RATINGS = new Set(['Sell', 'Strong Sell'])
const ROWS_PER_SIDE = 30

// The last close strictly before today from a {date, close} bar series
// (price_history_daily_3mo.json / price_history.json) -- never today's own
// entry, which both sources can carry as a still-forming bar when fetched
// intraday. Same helper, same reasoning, as PeTable.jsx/PositionsView.jsx's
// own previousClose (duplicated locally there too, not shared -- this
// project's convention for this particular helper).
function previousClose(series) {
  if (!series || series.length === 0) return null
  const today = new Date().toISOString().slice(0, 10)
  for (let i = series.length - 1; i >= 0; i--) {
    if (series[i].date.slice(0, 10) < today) return series[i].close
  }
  return null
}

function fmtPctAbs(v) {
  return (Math.abs(v) * 100).toFixed(1) + '%'
}

function fmtShares(shares) {
  return `${Math.abs(shares).toLocaleString()} sh (${shares > 0 ? 'long' : 'short'})`
}


// scorePercentile is sorted_screen.csv's own score * 100 (0 = best rank in
// the ranked universe, 100 = worst — see scoring.rank_ascending) -- phrased
// relative to the direction that matters for this row's own rating rather
// than a raw number, since "62nd percentile" reads as meaningless without
// knowing which end is good.
function percentileLabel(c) {
  if (c.scorePercentile === null || c.scorePercentile === undefined) return null
  // Which half of the distribution it's actually in, not which half its
  // rating implies -- a Hold-rated Close card (see closes below) isn't on
  // either extreme, so phrasing off SELL_RATINGS membership (as this used
  // to) would call a 50th-percentile Hold "Top 50%" every time.
  return c.scorePercentile <= 50
    ? `Top ${c.scorePercentile.toFixed(1)}% of the ranked universe`
    : `Bottom ${(100 - c.scorePercentile).toFixed(1)}% of the ranked universe`
}

// momentum is IBApp's Sharpe-style regression-slope score (trend divided by
// its own volatility -- see fmtSigned's own comment in screenerFactors.js),
// not a return -- shown as the signed raw number, not a percentage. Every
// card that reaches the page has already passed the hard momentum-direction
// rule (see RecommendationsView's eligible() below), so this line is
// showing the reader the gate it already cleared, not a new judgment call.
function momentumLine(c) {
  if (c.momentum === null || c.momentum === undefined) return null
  return `Momentum ${fmtSigned(c.momentum)} (${c.momentum > 0 ? 'positive' : 'negative'})`
}

// Builds a matcher for the sector/theme hedge preference: given the
// tickers held on the OPPOSITE side from the list being ranked (short
// positions when ranking Long ideas, long positions when ranking Short
// ideas), returns a function that, for a candidate, finds a held ticker on
// that opposite side sharing its sector or a theme tag -- an opposite-side
// position inside the same sector/theme as an existing position is a
// pairs-style hedge (reduces sector/theme-level risk while keeping
// stock-specific views), which the user explicitly wants preferred over an
// unrelated idea of otherwise similar rank. Sector match is checked first
// (a single, unambiguous field); theme match (a ticker can carry several
// tags -- see ticker_themes.json) is the fallback.
function buildOppositeMatcher(oppositeTickers, tickerSector, tickerThemes) {
  const bySector = new Map()
  const byTheme = new Map()
  for (const t of oppositeTickers) {
    const sector = tickerSector[t]
    if (sector) {
      if (!bySector.has(sector)) bySector.set(sector, [])
      bySector.get(sector).push(t)
    }
    for (const theme of tickerThemes[t] || []) {
      if (!byTheme.has(theme)) byTheme.set(theme, [])
      byTheme.get(theme).push(t)
    }
  }
  return function match(c) {
    if (c.sector && bySector.has(c.sector)) {
      return { type: 'sector', value: c.sector, tickers: bySector.get(c.sector) }
    }
    for (const theme of tickerThemes[c.ticker] || []) {
      if (byTheme.has(theme)) {
        return { type: 'theme', value: theme, tickers: byTheme.get(theme) }
      }
    }
    return null
  }
}

function oppositeMatchLine(match, oppositeSideLabel, themeLabels) {
  if (!match) return null
  const tickers = match.tickers.join(', ')
  const where = match.type === 'sector' ? `sector (${match.value})` : `theme (${themeLabels[match.value] || match.value})`
  return `Hedges your ${tickers} ${oppositeSideLabel} — same ${where}`
}

// Every line here reads straight off one field of a recommendations.json
// candidate (see recommendations.py) -- no extra computation, just turning
// numbers into a sentence. A line is omitted rather than shown as "—" when
// its underlying data source has nothing to say (e.g. no 13F match for that
// company name), so a card's rationale only ever lists real signal.
function rationaleLines(c) {
  const lines = []
  const pct = percentileLabel(c)
  if (pct) lines.push(pct)

  const momentum = momentumLine(c)
  if (momentum) lines.push(momentum)

  if (c.oppositeMatchLine) lines.push(c.oppositeMatchLine)

  const news = c.news7d
  if (news && news.total > 0) {
    lines.push(
      `${news.bullish} bullish / ${news.bearish} bearish headline${news.total === 1 ? '' : 's'} in the last 7 days`
    )
  } else {
    lines.push('No news coverage in the last 7 days')
  }

  const insiders = c.insiders90d
  if (insiders && insiders.buys + insiders.sells > 0) {
    lines.push(
      `${insiders.buys} insider open-market buy${insiders.buys === 1 ? '' : 's'}, ${insiders.sells} sell${insiders.sells === 1 ? '' : 's'} in the last 90 days`
    )
  }

  if (c.instChangeQoQ !== null && c.instChangeQoQ !== undefined) {
    lines.push(
      `Institutions ${c.instChangeQoQ >= 0 ? 'added' : 'trimmed'} ${fmtPctAbs(c.instChangeQoQ)} of shares held last quarter (13F)`
    )
  }

  if (c.targetUpside !== null && c.targetUpside !== undefined) {
    const analysts = c.numberOfAnalystOpinions ? Math.round(c.numberOfAnalystOpinions) : null
    lines.push(`Analyst target upside ${fmtPct(c.targetUpside)}${analysts ? ` (${analysts} analysts)` : ''}`)
  }

  return lines
}

// Live price (ib_price_server.py's SSE stream, same as every other tab)
// with a same-shape daily-%-change badge as PeTable.jsx's Price column --
// current price vs. previousClose(dailyHistory3mo) falling back to
// previousClose(monthlyHistory), falling back to recommendations.json's own
// (scoring-time) price if neither history source covers this ticker yet.
// `live` can be undefined (ib_price_server.py not running, or hasn't
// snapshotted this ticker yet) -- price/badge both degrade gracefully to
// the static price with no badge at all.
function PriceStat({ c, live, dailyHistory3mo, monthlyHistory }) {
  const referencePrice = previousClose(dailyHistory3mo[c.ticker]) ?? previousClose(monthlyHistory[c.ticker]) ?? c.price
  const currentPrice = live?.last ?? c.price
  const changeRatio = live?.last != null && referencePrice ? live.last / referencePrice - 1 : null
  const changeClass = changeRatio === null ? '' : Math.abs(changeRatio) <= 0.005 ? 'perf-neutral' : changeRatio >= 0 ? 'perf-pos' : 'perf-neg'
  return (
    <div className="stat">
      <span className="n num price-cell">
        <span className="price-value">{fmtPrice(currentPrice)}</span>
        {changeRatio !== null && (
          <span
            className={`live-price ${changeClass}`}
            title={`${fmtPrice(live.last)} at ${live.timestamp} vs. yesterday's close ${fmtPrice(referencePrice)}`}
          >
            {fmtPct(changeRatio)}
          </span>
        )}
      </span>
      <span className="l">Price</span>
    </div>
  )
}

// `held` (already a nonzero position in this ticker, long or short — see
// RecommendationsView's heldTickers) gets a lighter card background
// (recommendation-card-held, var(--surface-2) — the same alternate-surface
// token every other banded table in this app already uses) plus a small
// text badge, since color alone shouldn't be the only signal.
function RecommendationCard({ c, held, live, dailyHistory3mo, monthlyHistory }) {
  return (
    <div className={`asset-card recommendation-card${held ? ' recommendation-card-held' : ''}`}>
      <div className="recommendation-card-header">
        <div>
          <a
            href={`#/asset/${encodeURIComponent(c.ticker)}`}
            target="_blank"
            rel="noopener noreferrer"
            className="ticker-link recommendation-ticker"
          >
            {c.ticker}
          </a>
          <span className="recommendation-name">{c.name}</span>
        </div>
        <div className="recommendation-badges">
          {held && <span className="recommendation-held-badge">In portfolio</span>}
          <span className={`rec-badge ${ratingClass(c.rating)}`}>{c.rating}</span>
        </div>
      </div>

      <div className="recommendation-card-stats">
        <PriceStat c={c} live={live} dailyHistory3mo={dailyHistory3mo} monthlyHistory={monthlyHistory} />
        <div className="stat">
          <span className="n num">{c.sector || '—'}</span>
          <span className="l">Sector</span>
        </div>
      </div>

      <ul className="recommendation-rationale">
        {rationaleLines(c).map((line, i) => (
          <li key={i}>{line}</li>
        ))}
      </ul>
    </div>
  )
}

// Always shown as "held" (grey background) -- every row here is, by
// construction, an existing position (see the closes filter below) -- plus
// a "Close long"/"Close short" action tag and an explicit reason sentence
// ahead of the same recent-signal rationale bullets RecommendationCard
// uses, so the "why" isn't just the rating badge.
function CloseCard({ c, live, dailyHistory3mo, monthlyHistory }) {
  return (
    <div className="asset-card recommendation-card recommendation-card-held">
      <div className="recommendation-card-header">
        <div>
          <a
            href={`#/asset/${encodeURIComponent(c.ticker)}`}
            target="_blank"
            rel="noopener noreferrer"
            className="ticker-link recommendation-ticker"
          >
            {c.ticker}
          </a>
          <span className="recommendation-name">{c.name}</span>
        </div>
        <div className="recommendation-badges">
          <span className={`recommendation-action ${c.hasRatingReason ? 'recommendation-action-close' : 'recommendation-action-review'}`}>
            {c.hasRatingReason
              ? c.closeSide === 'Long'
                ? 'Close long'
                : 'Close short'
              : c.closeSide === 'Long'
                ? 'Review long'
                : 'Review short'}
          </span>
          <span className={`rec-badge ${ratingClass(c.rating)}`}>{c.rating || 'Unrated'}</span>
        </div>
      </div>

      <div className="recommendation-card-stats">
        <div className="stat">
          <span className="n num">{fmtShares(c.shares)}</span>
          <span className="l">Position</span>
        </div>
        <PriceStat c={c} live={live} dailyHistory3mo={dailyHistory3mo} monthlyHistory={monthlyHistory} />
      </div>

      <ul className="recommendation-close-reasons">
        {c.reasons.map((r, i) => (
          <li key={i}>{r.text}</li>
        ))}
      </ul>

      <ul className="recommendation-rationale">
        {rationaleLines(c).map((line, i) => (
          <li key={i}>{line}</li>
        ))}
      </ul>
    </div>
  )
}

function RecommendationSection({ title, subtitle, rows, renderCard, emptyMessage }) {
  return (
    <section className="recommendation-section">
      <h2 className="recommendation-section-title">
        {title}
        <span className="recommendation-section-subtitle">{subtitle}</span>
      </h2>
      {rows.length === 0 ? (
        <div className="asset-card">{emptyMessage}</div>
      ) : (
        <div className="recommendation-grid">{rows.map((c) => renderCard(c))}</div>
      )}
    </section>
  )
}

// Combines the pre-computed candidate pool in data/recommendations.json
// (see recommendations.py -- every Strong Buy/Buy/Sell/Strong Sell ticker's
// composite score, momentum, and recent news/insider/13F signals) with live
// positions/prices from ib_price_server.py's SSE stream (same pattern
// Positions/Sectors/Themes/News already use).
//
// Long = top ROWS_PER_SIDE Strong Buy/Buy candidates by best score; Short =
// top ROWS_PER_SIDE Strong Sell/Sell candidates by worst score. Neither
// side excludes held tickers -- a current long can appear in Long, a
// current short can appear in Short -- so positions are eligible on their
// own side without being force-included past a real top-N cut (which would
// break the fixed ROWS_PER_SIDE count the user asked for). Held status (side-specific --
// heldLongTickers/heldShortTickers below) drives the "In portfolio" grey
// highlight on whichever side actually holds it.
//
// A hard momentum-direction rule (eligible() below) gates ALL THREE groups
// before ranking, per explicit instruction: never recommend buying (Long,
// or covering/closing a Short) an asset with non-positive momentum, and
// never recommend selling (Short, or closing a Long) an asset with
// non-negative momentum. A candidate with unknown momentum is excluded
// rather than assumed compliant, since the rule is absolute.
//
// Within each side, a non-held candidate that hedges an existing position
// on the OPPOSITE side (same sector or theme -- see the longs/shorts memos'
// own buildOppositeMatcher/HEDGE_BONUS usage) gets a bounded score nudge so
// it can leapfrog similarly-ranked ideas with no such overlap, without
// out-ranking a clearly stronger idea just for the overlap -- explicit
// instruction: the portfolio's existing exposure should inform which NEW
// trade gets suggested, not just the idea's own standalone rank.
//
// The third group, To close, is a direct read of the live portfolio rather
// than a ranked idea list -- see buildCloseReasons for the full set of
// independent checks (a position can trip more than one at once): a rating
// no longer on its own side (long drifted to Hold/Sell/Strong Sell, short
// drifted to Hold/Buy/Strong Buy -- not just the fully-opposite rating,
// explicit instruction "opposite side or HOLD"), momentum alone no longer
// supporting the side even if the rating hasn't caught up yet (explicit
// instruction, "particularly if momentum is no longer supportive"), and
// three risk-only flags that fire regardless of rating or momentum:
// position-size concentration, a short that's become crowded, and a beta
// high enough to amplify risk well past the position's own dollar weight
// (explicit instruction: flag a position "not adequate for the portfolio
// because of too high risk or other reasons").
function eligibleToBuy(c) {
  return c.momentum !== null && c.momentum !== undefined && c.momentum > 0
}
function eligibleToSell(c) {
  return c.momentum !== null && c.momentum !== undefined && c.momentum < 0
}

// Short-only: a name already shorted by more than MAX_SHORT_INTEREST of its
// float is a crowded short -- squeeze risk that makes it a worse short idea
// regardless of how it scores otherwise. Explicit instruction: "any short
// interest above 20% must not be presented" on the Short list. Unlike the
// momentum gate above (an absolute "never" that treats unknown as
// disqualifying), this is a risk-avoidance cap on a known-bad condition --
// a candidate with no shortPercentOfFloat data isn't assumed crowded, so it
// still passes.
const MAX_SHORT_INTEREST = 0.2
function notCrowded(c) {
  return c.shortPercentOfFloat === null || c.shortPercentOfFloat === undefined || c.shortPercentOfFloat <= MAX_SHORT_INTEREST
}

// Position-size concentration: a common institutional-style guardrail, not
// tied to any one factor's data quality -- a single name over this share of
// net liquidation value is a portfolio-construction risk regardless of how
// well it scores. A high-conviction pick can still be a bad idea to hold
// this large.
const CONCENTRATION_THRESHOLD = 0.1

// A beta this far from 1 (either direction) means the position swings
// well beyond the broad market's own move -- amplifies this position's
// contribution to portfolio risk beyond what its dollar weight alone
// suggests, for a long or a short alike.
const HIGH_BETA_THRESHOLD = 2.0

// Everything that can put a held position in the To close group -- a
// rating contradiction (see the existing closes logic) is the most
// decisive single reason, but explicit instruction was to also flag "too
// high risk or other reasons" even when the rating hasn't (yet) turned:
// momentum alone no longer supporting the side ("particularly if momentum
// is no longer supportive" -- the exact NVDA/MU situation surfaced earlier:
// still Strong Buy rated, but momentum had already gone flat/negative),
// oversized position concentration, a short that's become crowded since it
// was opened, or a beta high enough to amplify risk well past the
// position's own dollar weight. A position can trip more than one of
// these at once -- returns every reason that applies, not just the first
// match, so the card shows the full picture rather than picking one
// arbitrarily.
function buildCloseReasons({ shares, c, livePrice, netLiquidation }) {
  const reasons = []
  const isLong = shares > 0
  const rating = c?.rating

  if (rating) {
    if (isLong && !BUY_RATINGS.has(rating) && eligibleToSell(c)) {
      reasons.push({ type: 'rating', text: `No longer rated Buy/Strong Buy (currently ${rating}) — consider closing.` })
    } else if (!isLong && !SELL_RATINGS.has(rating) && eligibleToBuy(c)) {
      reasons.push({ type: 'rating', text: `No longer rated Sell/Strong Sell (currently ${rating}) — consider covering.` })
    }
  }

  // Independent of rating -- a held Buy/Strong Buy long whose own momentum
  // has already turned flat or negative (or a held Sell/Strong Sell short
  // whose momentum has turned flat or positive) is worth flagging before
  // the rating catches up, not just after.
  if (c && c.momentum !== null && c.momentum !== undefined) {
    if (isLong && c.momentum <= 0) {
      reasons.push({
        type: 'momentum',
        text: `Momentum is no longer supportive for a long (${fmtSigned(c.momentum)}, ${c.momentum === 0 ? 'flat' : 'negative'}).`,
      })
    } else if (!isLong && c.momentum >= 0) {
      reasons.push({
        type: 'momentum',
        text: `Momentum is no longer supportive for a short (${fmtSigned(c.momentum)}, ${c.momentum === 0 ? 'flat' : 'positive'}).`,
      })
    }
  }

  const price = livePrice ?? c?.price ?? null
  const positionValue = price !== null ? Math.abs(shares) * price : null
  if (positionValue !== null && netLiquidation) {
    const weight = positionValue / netLiquidation
    if (weight > CONCENTRATION_THRESHOLD) {
      reasons.push({
        type: 'concentration',
        text: `${fmtPctAbs(weight)} of net liquidation value is concentrated in this one position (over the ${fmtPctAbs(CONCENTRATION_THRESHOLD)} guideline).`,
      })
    }
  }

  if (!isLong && c?.shortPercentOfFloat !== null && c?.shortPercentOfFloat !== undefined && c.shortPercentOfFloat > MAX_SHORT_INTEREST) {
    reasons.push({
      type: 'crowded-short',
      text: `${fmtPctAbs(c.shortPercentOfFloat)} of float is already short — this short has become crowded, squeeze risk.`,
    })
  }

  if (c?.beta !== null && c?.beta !== undefined && Math.abs(c.beta) > HIGH_BETA_THRESHOLD) {
    reasons.push({
      type: 'beta',
      text: `Beta of ${c.beta.toFixed(2)} amplifies this position's market-risk contribution well beyond its dollar weight.`,
    })
  }

  return reasons
}

export default function RecommendationsView() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [positions, setPositions] = useState({})
  const [livePrices, setLivePrices] = useState({})
  const [account, setAccount] = useState({})
  const [dailyHistory3mo, setDailyHistory3mo] = useState({})
  const [monthlyHistory, setMonthlyHistory] = useState({})
  const [tickerSector, setTickerSector] = useState({})
  const [tickerThemes, setTickerThemes] = useState({})
  const [themeLabels, setThemeLabels] = useState({})
  const [tickerScreener, setTickerScreener] = useState({})

  useEffect(() => {
    const source = new EventSource(IB_STREAM_URL)
    source.onmessage = (e) => {
      const { prices, positions: pos, account: acc } = JSON.parse(e.data)
      setLivePrices(prices || {})
      setPositions(pos || {})
      setAccount(acc || {})
    }
    source.onerror = () => {}
    return () => source.close()
  }, [])

  useEffect(() => {
    let cancelled = false
    fetch('/recommendations.json')
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
        return r.json()
      })
      .then((d) => {
        if (!cancelled) setData(d)
      })
      .catch((e) => {
        if (!cancelled) setError(e.message)
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    fetch('/price_history_daily_3mo.json')
      .then((r) => (r.ok ? r.json() : {}))
      .then(setDailyHistory3mo)
      .catch(() => {})
    fetch('/price_history.json')
      .then((r) => (r.ok ? r.json() : {}))
      .then(setMonthlyHistory)
      .catch(() => {})
  }, [])

  // tickerSector/tickerScreener cover the WHOLE universe (sorted_screen.csv,
  // not just recommendations.json's RATED_FOR_EXTRAS candidates) -- a held
  // position rated Hold has no entry in recommendations.json at all (Form4/
  // 13F/social-sentiment downloads are scoped to RATED_FOR_EXTRAS too, so
  // it wouldn't have that signal data even if it did), but still needs its
  // sector (for the opposite-side hedge matcher below) and its rating/
  // score/momentum (for the closes fallback below -- see that memo) to be
  // visible somewhere. ticker_themes.json/theme_taxonomy.json are the same
  // two files ThemesView.jsx reads, for the theme half of the hedge
  // matcher.
  useEffect(() => {
    fetch('/sorted_screen.csv')
      .then((r) => (r.ok ? r.text() : ''))
      .then((text) => {
        const sectors = {}
        const screener = {}
        for (const row of parseCSV(text)) {
          sectors[row.ticker] = row.sector || null
          const score = row.score ? Number(row.score) : null
          screener[row.ticker] = {
            ticker: row.ticker,
            name: row.name || null,
            rating: row.rating || null,
            score,
            scorePercentile: score !== null ? Math.round(score * 1000) / 10 : null,
            momentum: row.momentum ? Number(row.momentum) : null,
            sector: row.sector || null,
            price: row.price ? Number(row.price) : null,
            beta: row.beta ? Number(row.beta) : null,
            shortPercentOfFloat: row.shortPercentOfFloat ? Number(row.shortPercentOfFloat) : null,
          }
        }
        setTickerSector(sectors)
        setTickerScreener(screener)
      })
      .catch(() => {})
    fetch('/ticker_themes.json')
      .then((r) => (r.ok ? r.json() : {}))
      .then(setTickerThemes)
      .catch(() => {})
    fetch('/theme_taxonomy.json')
      .then((r) => (r.ok ? r.json() : []))
      .then((themes) => setThemeLabels(Object.fromEntries(themes.map((t) => [t.key, t.label]))))
      .catch(() => {})
  }, [])

  // Side-specific -- a short position must never count as "held" toward the
  // Long section (and vice versa): a ticker that's both a held short AND
  // Buy-rated is a contradiction the To close group flags below, not a
  // reason to highlight it as "In portfolio" on the Long grid. heldCount
  // (all nonzero positions, either side) is only for the masthead's stat.
  const heldLongTickers = useMemo(
    () => new Set(Object.entries(positions).filter(([, p]) => p?.shares > 0).map(([t]) => t)),
    [positions]
  )
  const heldShortTickers = useMemo(
    () => new Set(Object.entries(positions).filter(([, p]) => p?.shares < 0).map(([t]) => t)),
    [positions]
  )
  const heldCount = useMemo(() => Object.values(positions).filter((p) => p?.shares).length, [positions])

  // A non-held candidate that hedges an existing OPPOSITE-side position
  // (same sector or theme -- see buildOppositeMatcher) is nudged ahead of
  // an otherwise similarly-ranked idea with no such overlap, per explicit
  // instruction: "an opposite position inside a theme or sector of a
  // current position is preferable". A bounded score adjustment (HEDGE_BONUS
  // -- 5 percentile points of the 0..1 composite score, same units
  // scorePercentile already displays), not a hard tier: a match can leapfrog
  // ideas within roughly that margin, but can't out-rank a genuinely much
  // better idea just for overlapping a sector. An earlier version tried a
  // hard "all matches beat all non-matches" tier and it was too strong in
  // practice -- on this portfolio's real data it filled the whole Long list
  // with sector-matched names scoring 0.41-0.49, pushing out non-matched
  // ideas scoring 0.27-0.37 (a clearly worse trade-off than what "preferable"
  // should mean). Held candidates are excluded from the matcher's own input
  // (checking a position against itself is meaningless) and never get the
  // hedge rationale line (it's not "a new trade" if you already hold it) or
  // the bonus.
  const HEDGE_BONUS = 0.05

  const longs = useMemo(() => {
    if (!data) return []
    const matcher = buildOppositeMatcher([...heldShortTickers], tickerSector, tickerThemes)
    const pool = data.candidates
      .filter((c) => BUY_RATINGS.has(c.rating) && eligibleToBuy(c))
      .map((c) => {
        const held = heldLongTickers.has(c.ticker)
        const match = held ? null : matcher(c)
        const sortScore = (c.score ?? 1) - (match ? HEDGE_BONUS : 0)
        return { ...c, oppositeMatchLine: oppositeMatchLine(match, 'short', themeLabels), _sortScore: sortScore }
      })
    return pool.sort((a, b) => a._sortScore - b._sortScore).slice(0, ROWS_PER_SIDE)
  }, [data, heldShortTickers, heldLongTickers, tickerSector, tickerThemes, themeLabels])

  const shorts = useMemo(() => {
    if (!data) return []
    const matcher = buildOppositeMatcher([...heldLongTickers], tickerSector, tickerThemes)
    const pool = data.candidates
      .filter((c) => SELL_RATINGS.has(c.rating) && eligibleToSell(c) && notCrowded(c))
      .map((c) => {
        const held = heldShortTickers.has(c.ticker)
        const match = held ? null : matcher(c)
        const sortScore = (c.score ?? 0) + (match ? HEDGE_BONUS : 0)
        return { ...c, oppositeMatchLine: oppositeMatchLine(match, 'long', themeLabels), _sortScore: sortScore }
      })
    return pool.sort((a, b) => b._sortScore - a._sortScore).slice(0, ROWS_PER_SIDE)
  }, [data, heldLongTickers, heldShortTickers, tickerSector, tickerThemes, themeLabels])

  // Held positions whose own rating now contradicts the side they're held
  // on -- a long position that's drifted to Sell/Strong Sell, or a short
  // position that's drifted to Buy/Strong Buy -- filtered by the same
  // momentum gate as Long/Short above: closing a Long is a sell (needs
  // eligibleToSell), closing a Short is a buy/cover (needs eligibleToBuy).
  // Not capped to ROWS_PER_SIDE like Long/Short -- this is a flag on the
  // actual portfolio, not a ranked idea list, so every contradiction that
  // clears the momentum gate shows, however many there are. Sorted by how
  // far the rating leans the "wrong" way for that side, not by side, so the
  // most urgent flags surface first regardless of direction.
  const closes = useMemo(() => {
    if (!data) return []
    const byTicker = new Map(data.candidates.map((c) => [c.ticker, c]))
    const netLiquidation = account?.NetLiquidation ?? null
    const rows = []
    for (const [ticker, p] of Object.entries(positions)) {
      if (!p?.shares) continue
      // recommendations.json only covers RATED_FOR_EXTRAS, so a held
      // position that's drifted to Hold has no entry there -- fall back to
      // tickerScreener (sorted_screen.csv, every rated ticker) for its
      // rating/score/momentum/beta/short-interest in that case. Prefer the
      // candidate when both exist: it carries news/insiders/13F/analyst
      // signal tickerScreener alone doesn't. Concentration risk needs only
      // a live price, so an entirely unscored ticker (e.g. an ETF outside
      // the screener universe) can still trip that one check even with c
      // falling all the way back to an empty object.
      const c = byTicker.get(ticker) || tickerScreener[ticker] || {}
      const reasons = buildCloseReasons({ shares: p.shares, c, livePrice: livePrices[ticker]?.last, netLiquidation })
      if (reasons.length === 0) continue
      const hasRatingReason = reasons.some((r) => r.type === 'rating')
      const hasMomentumReason = reasons.some((r) => r.type === 'momentum')
      // Rating contradictions rank highest (most decisive single signal),
      // then unsupportive momentum, then however many risk-only flags
      // (concentration/crowded-short/high-beta) apply -- so a position
      // tripping several risk flags at once still outranks one tripping
      // only a single, milder one.
      const severity =
        (hasRatingReason ? 100 : 0) +
        (hasMomentumReason ? 50 : 0) +
        reasons.filter((r) => r.type !== 'rating' && r.type !== 'momentum').length * 10
      rows.push({ ...c, closeSide: p.shares > 0 ? 'Long' : 'Short', shares: p.shares, reasons, hasRatingReason, _severity: severity })
    }
    return rows.sort((a, b) => b._severity - a._severity)
  }, [data, positions, tickerScreener, livePrices, account])

  return (
    <div className="positions-page positions-unbounded">
      <header className="masthead">
        <div className="title-block">
          <h1>Recommendations</h1>
        </div>
        <div className="stat-row">
          <div className="stat">
            <span className="n num">{longs.length}</span>
            <span className="l">Long</span>
          </div>
          <div className="stat">
            <span className="n num">{shorts.length}</span>
            <span className="l">Short</span>
          </div>
          <div className="stat">
            <span className="n num">{closes.length}</span>
            <span className="l">To close</span>
          </div>
          <div className="stat">
            <span className="n num">{heldCount}</span>
            <span className="l">held positions</span>
          </div>
        </div>
      </header>

      <RecommendationsChatbot />

      {error && <div className="asset-card">Couldn't load recommendations: {error}</div>}
      {!error && !data && <div className="asset-card">Loading…</div>}
      {!error && data && data.candidates.length === 0 && (
        <div className="asset-card">
          No candidates yet — run <code>python main.py recommendations</code> after <code>python main.py all</code>
          (or <code>prices</code>) has produced a ranked <code>sorted_screen.csv</code>.
        </div>
      )}

      {!error && data && data.candidates.length > 0 && (
        <>
          <RecommendationSection
            title="Long"
            subtitle="Strong Buy / Buy with positive momentum, best composite score first"
            rows={longs}
            renderCard={(c) => (
              <RecommendationCard
                key={c.ticker}
                c={c}
                held={heldLongTickers.has(c.ticker)}
                live={livePrices[c.ticker]}
                dailyHistory3mo={dailyHistory3mo}
                monthlyHistory={monthlyHistory}
              />
            )}
            emptyMessage="No Strong Buy/Buy candidates with positive momentum right now."
          />
          <RecommendationSection
            title="Short"
            subtitle="Strong Sell / Sell with negative momentum, worst composite score first — excludes crowded shorts (>20% of float already short)"
            rows={shorts}
            renderCard={(c) => (
              <RecommendationCard
                key={c.ticker}
                c={c}
                held={heldShortTickers.has(c.ticker)}
                live={livePrices[c.ticker]}
                dailyHistory3mo={dailyHistory3mo}
                monthlyHistory={monthlyHistory}
              />
            )}
            emptyMessage="No Sell/Strong Sell candidates with negative momentum right now."
          />
          <RecommendationSection
            title="To close"
            subtitle="Held positions no longer rated on their own side (opposite rating or Hold), momentum-gated the same way as Long/Short"
            rows={closes}
            renderCard={(c) => (
              <CloseCard
                key={c.ticker}
                c={c}
                live={livePrices[c.ticker]}
                dailyHistory3mo={dailyHistory3mo}
                monthlyHistory={monthlyHistory}
              />
            )}
            emptyMessage="No held position's rating currently contradicts its side (after the momentum gate)."
          />
        </>
      )}
    </div>
  )
}
