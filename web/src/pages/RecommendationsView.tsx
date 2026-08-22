import { useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react'
import { AlertTriangle, Flag, Info, Search, ThumbsDown, ThumbsUp } from 'lucide-react'
import { parseCSV } from '../csv'
import { businessMillisBetween, fmtEarningsDate, useNowTick } from '../earnings'
import { IB_STREAM_URL } from '../ibStream'
import { fmtPct, fmtPrice, ratingClass } from '../screenerFactors'
import { getSectorGroup, sectorGroupLabel } from '../sectorGroups'
import RecommendationsChatbot from '../components/RecommendationsChatbot'
import SectorFilter from '../components/SectorFilter'
import FilterDropdown from '../components/FilterDropdown'
import type {
  Candidate,
  CloseRow,
  HistoryByTicker,
  LivePricesByTicker,
  LiveTick,
  OppositeMatch,
  PositionsByTicker,
  RankedCandidate,
  Reason,
  RecommendationsData,
  RejectedRow,
  ScreenerByTicker,
} from '../interfaces/IRecommendationsView'

// Same click-outside-closes-the-popover hook as PeTable.jsx's Score Formula
// toggle -- duplicated locally rather than shared, this project's existing
// convention for small single-use hooks (see previousClose below).
function useOutsideClick(ref: RefObject<HTMLElement | null>, onOutside: () => void) {
  useEffect(() => {
    function handler(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) onOutside()
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [ref, onOutside])
}

const BUY_RATINGS = new Set(['Strong Buy', 'Buy'])
const SELL_RATINGS = new Set(['Sell', 'Strong Sell'])

// The last close strictly before today, comparing BOTH bar series --
// price_history_daily_3mo.json (IB Gateway's own history, fetched once
// at ib_server.py STARTUP only, so it silently goes stale the
// longer the server runs without a restart) and price_history.json
// (yfinance, refreshed daily via main.py) -- and using whichever one's
// own most recent pre-today entry is actually the NEWER of the two, not
// just "the first one that has any prior-day close at all." Explicit
// bug fix: a plain `previousClose(dailyHistory3mo[t]) ??
// previousClose(monthlyHistory[t])` fallback chain got this wrong in
// practice -- confirmed live on TSLA, price_history_daily_3mo.json
// hadn't been refreshed since Aug 12 (2 trading days stale) while
// price_history.json already had Aug 13's real close, but the ??  never
// fell through to it because the stale IB file still returned a valid
// (just outdated) close, not null. Same helper, same reasoning, as
// PeTable.jsx/PositionsView.jsx's own previousClose (duplicated locally
// there too, not shared -- this project's convention for this
// particular helper).
// {date, close} of the bar previousClose() below actually resolves to --
// pulled out on its own so PreviousCloseFlag can check the DATE without
// duplicating this lookup a third time in this file.
function previousCloseInfo(
  dailyHistory3mo: { date: string; close: number }[] | undefined,
  monthlyHistory: { date: string; close: number }[] | undefined
): { date: string; close: number } | null {
  const lastBarBeforeToday = (series: { date: string; close: number }[] | undefined) => {
    if (!series || series.length === 0) return null
    const today = new Date().toISOString().slice(0, 10)
    for (let i = series.length - 1; i >= 0; i--) {
      const date = series[i].date.slice(0, 10)
      if (date < today) return { date, close: series[i].close }
    }
    return null
  }
  // IB Gateway's own daily bars are the primary source; yfinance's
  // monthly series is only a fallback for a ticker IB doesn't cover at
  // all -- explicit instruction: the two stay genuinely separate series,
  // IB always used when it has anything, never picked against just
  // because yfinance happens to have a fresher date.
  return lastBarBeforeToday(dailyHistory3mo) ?? lastBarBeforeToday(monthlyHistory)
}

function previousClose(
  dailyHistory3mo: { date: string; close: number }[] | undefined,
  monthlyHistory: { date: string; close: number }[] | undefined
): number | null {
  return previousCloseInfo(dailyHistory3mo, monthlyHistory)?.close ?? null
}

// Mirrors scoring.py's own most_recent_completed_trading_day() exactly --
// yesterday, rolled back over the weekend. Not a real market-holiday
// calendar, same "good enough, don't over-engineer it" spirit as that
// function's own docstring -- see PreviousCloseFlag for what this drives.
function mostRecentCompletedTradingDay(): string {
  const d = new Date()
  d.setDate(d.getDate() - 1)
  while (d.getDay() === 0 || d.getDay() === 6) {
    d.setDate(d.getDate() - 1)
  }
  return d.toISOString().slice(0, 10)
}

function fmtPctAbs(v: number): string {
  return (Math.abs(v) * 100).toFixed(1) + '%'
}

function fmtShares(shares: number): string {
  return `${Math.abs(shares).toLocaleString()} sh (${shares > 0 ? 'long' : 'short'})`
}

// live.timestamp (ib_server.py's SSE tick, see PriceStat below) is a
// naive local-time ISO string (datetime.now().isoformat()) -- how long ago
// that snapshot was taken is more useful in a hover tooltip than the raw
// clock time, since what actually matters here is "how stale is this
// price," not what time it happened to be.
function fmtMinutesAgo(timestamp: string | undefined): string {
  if (!timestamp) return 'unknown time'
  const diffMin = Math.round((Date.now() - new Date(timestamp).getTime()) / 60000)
  if (diffMin <= 0) return 'just now'
  if (diffMin === 1) return '1 min ago'
  return `${diffMin} min ago`
}

// scorePercentile is the ticker's true rank position in sorted_screen.csv
// (0 = best, 100 = worst) -- see recommendations.py's build_recommendations
// and this file's own tickerScreener effect for why that's NOT the same as
// score * 100 (score itself clusters non-uniformly around the middle; only
// rank position is uniform). Phrased relative to the direction that
// matters for this row's own rating rather than a raw number, since "62nd
// percentile" reads as meaningless without knowing which end is good.
function percentileLabel(c: Candidate): string | null {
  if (c.scorePercentile === null || c.scorePercentile === undefined) return null
  // Which half of the distribution it's actually in, not which half its
  // rating implies -- a Hold-rated Close card (see closes below) isn't on
  // either extreme, so phrasing off SELL_RATINGS membership (as this used
  // to) would call a 50th-percentile Hold "Top 50%" every time.
  return c.scorePercentile <= 50
    ? `Top ${c.scorePercentile.toFixed(1)}% of the ranked universe`
    : `Bottom ${(100 - c.scorePercentile).toFixed(1)}% of the ranked universe`
}

// Bottom row of every card (RecommendationCard/CloseCard/RejectedCard) --
// GICS sector on the left, percentile badge on the right, explicit
// instruction to put them on the same line. c.sector is actually the
// GRANULAR industry (e.g. "Banks - Regional" -- Yahoo's own "industry"
// field, confusingly named "sector" throughout this app's data -- see
// scoring.py's own module docstring on that naming); getSectorGroup maps
// it up to the 11 broad GICS-style sectors (e.g. "Financial Services",
// shown short as "Financials" via sectorGroupLabel), same mapping the
// Rating Breakdown table's own sector rows use -- the industry itself
// stays in .recommendation-card-stats (see RecommendationCard/
// RejectedCard), unmoved. The screener percentile used to only show up
// buried as the first line of the rationale bullet list
// (RecommendationCard/CloseCard only, not RejectedCard at all, which has
// no rationale list), so it took reading past the header to see where a
// ticker actually sits in sorted_screen.csv. title carries the same full
// "Top/Bottom X% of the ranked universe" sentence percentileLabel already
// writes; the badge itself just shows the bare number, compact enough for
// a corner. Renders nothing only when BOTH sector and percentile are
// missing -- either alone is still worth a footer.
function CardFooter({ c }: { c: Candidate }) {
  const hasPercentile = c.scorePercentile !== null && c.scorePercentile !== undefined
  if (!hasPercentile && !c.sector) return null
  const label = percentileLabel(c)
  const sectorGroup = c.sector ? sectorGroupLabel(getSectorGroup(c.sector)) : null
  return (
    <div className="recommendation-card-footer">
      <span className="recommendation-sector-label">{sectorGroup || '—'}</span>
      {hasPercentile && (
        <span className="recommendation-percentile-badge" title={label ?? undefined}>
          {(c.scorePercentile as number).toFixed(1)}%
        </span>
      )}
    </div>
  )
}

// Good/bad highlighting for each rationale line -- explicit instruction:
// a small green thumbs-up when a factor favors the position, red
// thumbs-down when it works against it, no icon when the reading is
// genuinely neutral (not just "small"). The polarity mirrors by side for
// every factor here: a reading that's bullish for the stock is good news
// for a Long and bad news for a Short, and vice versa -- same mirroring
// RecommendationsView's own eligibleToBuy/eligibleToSell,
// meanReversionOkForLong/OkForShort, and epsTrendOkForLong/OkForShort
// gates already use, just turned into a per-line signal instead of a
// pass/fail gate.
type Signal = 'good' | 'bad' | null

// A stable per-factor key, one per distinct rationale line rationaleLines
// below can produce -- lets the thumb-factor filter (see
// selectedThumbFilters/filterByThumbs) match "MSI: thumbs up" against the
// right line without parsing its display text. Not every line has a
// factor with a real name in scoring.py (oppositeMatchLine has none, so
// it's the one line below with no `factor` field at all -- excluded from
// the filter catalog for that reason).
type RationaleFactor =
  | 'revenueGrowth'
  | 'momentum'
  | 'meanReversion'
  | 'epsTrend'
  | 'shortInterest'
  | 'news'
  | 'insiders'
  | 'insiderOwnership'
  | 'institutions'
  | 'targetUpside'
type RationaleLine = { text: string; signal: Signal; factor?: RationaleFactor }

// Catalog for the thumb-factor filter's own dropdown (see
// selectedThumbFilters next to SectorFilter in .controls) -- explicit
// instruction: "a way to make filters based on thumb factors... only see
// thumbs up in MSI, or thumbs down in Revenue growth." One entry per
// RationaleFactor; label is what the dropdown/filter chip shows.
const THUMB_FACTORS: { key: RationaleFactor; label: string }[] = [
  { key: 'momentum', label: 'MSI' },
  { key: 'meanReversion', label: 'ST-MSI' },
  { key: 'revenueGrowth', label: 'Revenue growth' },
  { key: 'epsTrend', label: 'EPS trend' },
  { key: 'shortInterest', label: 'Short interest' },
  { key: 'news', label: 'News' },
  { key: 'insiders', label: 'Insiders' },
  { key: 'insiderOwnership', label: 'Insider ownership' },
  { key: 'institutions', label: 'Institutions (13F)' },
  { key: 'targetUpside', label: 'Target upside' },
]

// Flattened good/bad pair per factor -- `name` doubles as both
// FilterDropdown's item label and the selection key (see that
// component's own items prop, which uses the same string for both), so
// it has to be human-readable on its own; filterByThumbs looks a
// selected name back up in this list to recover factor/signal.
const THUMB_FILTER_ITEMS: { name: string; factor: RationaleFactor; signal: 'good' | 'bad' }[] = THUMB_FACTORS.flatMap(
  (f) => [
    { name: `${f.label} — up`, factor: f.key, signal: 'good' as const },
    { name: `${f.label} — down`, factor: f.key, signal: 'bad' as const },
  ]
)

// Generic sign mirror for a factor with no dead zone of its own (momentum,
// EPS trend, institutional change, target upside): positive is good for a
// Long/bad for a Short, negative is the reverse, exactly zero is neutral.
function sidedSignal(value: number, side: 'Long' | 'Short'): Signal {
  if (value > 0) return side === 'Long' ? 'good' : 'bad'
  if (value < 0) return side === 'Long' ? 'bad' : 'good'
  return null
}

// Same idea-list entry gate as momentum (sufficientGrowthForLong/
// notTooMuchGrowthForShort below), same "show the reader the gate it
// already cleared" reasoning as that comment -- explicit instruction:
// added on top of the other rationale lines. revenueGrowth lives on
// tickerScreener, not the recommendations.json candidate itself (see
// sufficientGrowthForLong's own comment) -- the longs/shorts pool
// builders copy it onto each RankedCandidate for exactly this line;
// CloseRow already gets it from the same tickerScreener merge its own
// close-reasons check uses. Plain sign, same as momentum/EPS trend --
// positive growth good for a Long/bad for a Short, negative the mirror.
function revenueGrowthLine(c: Candidate, side: 'Long' | 'Short'): RationaleLine | null {
  if (c.revenueGrowth === null || c.revenueGrowth === undefined) return null
  return {
    text: `Revenue growth ${fmtPct(c.revenueGrowth)}`,
    signal: sidedSignal(c.revenueGrowth, side),
    factor: 'revenueGrowth',
  }
}

// momentum is now IBApp's Money Flow Index (or plain RSI on the yfinance-
// fallback tier -- see IBApp.get_momentum) -- a bounded [0, 100] daily-
// timeframe strength/overbought-oversold oscillator, NOT the old signed
// Sharpe-style regression score, so this shows a plain magnitude with a
// conventional zone word (>=70 overbought, <=30 oversold) instead of a
// +/- sign. Every card that reaches the page has already passed the hard
// momentum-direction rule (see eligibleToBuy/eligibleToSell below), so
// this line is showing the reader the gate it already cleared, not a new
// judgment call. Signal mirrors those same gates' own thresholds
// (MOMENTUM_OVERSOLD/MOMENTUM_OVERBOUGHT) rather than a plain sign check
// -- there's no "positive/negative" on a 0-100 scale, only "is this an
// actual overbought/oversold extreme."
function momentumZone(value: number): string {
  if (value > MOMENTUM_OVERBOUGHT) return 'overbought'
  if (value < MOMENTUM_OVERSOLD) return 'oversold'
  return 'neutral'
}

// Mean-reversion signal, not continuation -- see MOMENTUM_OVERSOLD/
// MOMENTUM_OVERBOUGHT's own comment. Oversold favors a Long/disfavors a
// Short, overbought the mirror; anything in between is neither extreme
// and carries no signal at all (null), not a mild lean either way.
function momentumSignal(value: number, side: 'Long' | 'Short'): Signal {
  if (side === 'Long') {
    if (value < MOMENTUM_OVERSOLD) return 'good'
    if (value > MOMENTUM_OVERBOUGHT) return 'bad'
    return null
  }
  if (value > MOMENTUM_OVERBOUGHT) return 'good'
  if (value < MOMENTUM_OVERSOLD) return 'bad'
  return null
}

function momentumLine(c: Candidate, side: 'Long' | 'Short'): RationaleLine | null {
  if (c.momentum === null || c.momentum === undefined) return null
  return {
    text: `MSI ${c.momentum.toFixed(0)} (${momentumZone(c.momentum)})`,
    signal: momentumSignal(c.momentum, side),
    factor: 'momentum',
  }
}

// Shown right next to momentum -- explicit instruction, always alongside
// it rather than only when the mean-reversion gate/close-reason actually
// fires (see meanReversionOkForLong/meanReversionOkForShort and
// buildCloseReasons' own mean-reversion check), so the reader can see the
// reading even on a card where it's nowhere near those gates' own
// overbought/oversold lines (those still gate the idea-list/close-reason
// logic elsewhere in this file -- they just don't gate this icon). Same
// "only computed for CANDLESTICK_TOP_N ranked/held tickers" gap as those
// checks -- omitted, not shown as 0, when absent.
// Now IBApp's hourly Money Flow Index (see momentumLine's own comment on
// the daily leg's equivalent change) -- bounded [0, 100]. Same shape as
// MSI's own signal now (explicit instruction, "same story for ST-MSI"):
// oversold favors a Long/disfavors a Short, overbought the mirror, and
// anything in between (neither extreme) carries no signal at all --
// reusing the same MEAN_REVERSION_OVERBOUGHT/MEAN_REVERSION_OVERSOLD
// bounds the entry gate already uses below, rather than the old plain
// above/below-50 check with no neutral zone.
function meanReversionZone(value: number): string {
  if (value >= MEAN_REVERSION_OVERBOUGHT) return 'overbought'
  if (value <= MEAN_REVERSION_OVERSOLD) return 'oversold'
  return 'neutral'
}

function meanReversionLine(c: Candidate, side: 'Long' | 'Short'): RationaleLine | null {
  if (c.meanReversion === null || c.meanReversion === undefined) return null
  let signal: Signal = null
  if (c.meanReversion >= MEAN_REVERSION_OVERBOUGHT) signal = side === 'Long' ? 'bad' : 'good'
  else if (c.meanReversion <= MEAN_REVERSION_OVERSOLD) signal = side === 'Long' ? 'good' : 'bad'
  return {
    text: `ST-MSI ${c.meanReversion.toFixed(0)} (${meanReversionZone(c.meanReversion)})`,
    signal,
    factor: 'meanReversion',
  }
}

// Same "always shown, not just when the gate/reason fires" treatment as
// meanReversionLine above -- explicit instruction, EPS revision trend
// gates entry now too (see epsTrendOkForLong/epsTrendOkForShort), so a
// reader should see the reading on every card. Reuses epsTrendValue
// (below) rather than a separate field -- see that function's own
// comment for the epsRevision0y/1y averaging it does.
function epsTrendLine(c: Candidate, side: 'Long' | 'Short'): RationaleLine | null {
  const epsTrend = epsTrendValue(c)
  if (epsTrend === null) return null
  return {
    text: `EPS est trend ${fmtPct(epsTrend)} (${epsTrend > 0 ? 'raised' : epsTrend < 0 ? 'cut' : 'flat'})`,
    signal: sidedSignal(epsTrend, side),
    factor: 'epsTrend',
  }
}

// effectiveShortPctOfFloat (defined below, alongside notCrowded) prefers
// FINRA's fresher biweekly figure over yfinance's stale month-end one --
// shown on every card with data, not just when it crosses MAX_SHORT_INTEREST,
// since it's a contrarian scoring input either way (see short_interest_rank),
// not only a risk flag on the Short side. Signal: explicit instruction --
// more than MAX_SHORT_INTEREST of float already short is a GOOD sign for a
// Long (squeeze potential, upside risk) and a BAD one for a Short (squeeze
// risk against the position, the same crowded-short reasoning notCrowded's
// own gate already uses) -- at or under that bar, short interest isn't a
// discriminating signal either way, no icon.
function shortInterestLine(c: Candidate, side: 'Long' | 'Short'): RationaleLine | null {
  const pct = effectiveShortPctOfFloat(c)
  if (pct === null || pct === undefined) return null
  const signal: Signal = pct > MAX_SHORT_INTEREST ? (side === 'Long' ? 'good' : 'bad') : null
  return { text: `Short interest ${fmtPctAbs(pct)} of float`, signal, factor: 'shortInterest' }
}

// What fraction of shares insiders currently hold -- distinct from
// insiders90d's recent TRANSACTION activity (see the rationaleLines call
// site: this line is placed directly below that one, explicit
// instruction). High insider ownership reads as skin-in-the-game
// alignment, bullish for the company -- good for a Long, bad for a
// Short, same as any other bullish-for-the-company reading on this card.
// Gated on MIN_INSIDER_OWNERSHIP -- explicit follow-up instruction:
// nearly every widely-held company shows SOME nonzero insider stake
// (confirmed live: 0.2% was firing "good"), which isn't a real
// skin-in-the-game signal, just noise -- same "only the tail counts"
// treatment MAX_SHORT_INTEREST already gives the short-interest line.
function insiderOwnershipLine(c: Candidate, side: 'Long' | 'Short'): RationaleLine | null {
  if (c.heldPercentInsiders === null || c.heldPercentInsiders === undefined) return null
  const signal: Signal = c.heldPercentInsiders > MIN_INSIDER_OWNERSHIP ? (side === 'Long' ? 'good' : 'bad') : null
  return { text: `Insider ownership ${fmtPctAbs(c.heldPercentInsiders)}`, signal, factor: 'insiderOwnership' }
}

// Builds a matcher for the industry/sector hedge preference: given the
// tickers held on the OPPOSITE side from the list being ranked (short
// positions when ranking Long ideas, long positions when ranking Short
// ideas), returns a function that, for a candidate, finds a held ticker on
// that opposite side sharing its granular industry (c.sector -- yfinance's
// "industry" field, confusingly named "sector" throughout this app's data,
// see CardFooter's own comment) or, failing that, its broad GICS-style
// sector group (getSectorGroup(c.sector), e.g. "Technology") -- an
// opposite-side position in the same industry or sector as an existing
// position is a pairs-style hedge (reduces industry/sector-level risk
// while keeping stock-specific views), which the user explicitly wants
// preferred over an unrelated idea of otherwise similar rank. Industry
// match is checked first (the more specific, more meaningful overlap);
// broad sector-group match is the fallback. Explicitly does NOT fall back
// to the AI-classified theme tags (ticker_themes.json) the way an earlier
// version did -- that zero-shot classifier is best-effort and known to
// mis-tag (see theme_classifier.py's own docstring), and it was actually
// pairing up unrelated names on a shared bad tag in practice (e.g. MAC, a
// mall REIT, "hedging" SNDK, a semiconductor/storage maker, and PARR, an
// oil refiner -- all three wrongly carrying a "consumer_retail" theme tag
// with no real business relationship to justify it).
function buildOppositeMatcher(
  oppositeTickers: string[],
  tickerSector: Record<string, string | null>
): (c: Candidate) => OppositeMatch | null {
  const byIndustry = new Map<string, string[]>()
  const bySectorGroup = new Map<string, string[]>()
  for (const t of oppositeTickers) {
    const industry = tickerSector[t]
    if (!industry) continue
    if (!byIndustry.has(industry)) byIndustry.set(industry, [])
    ;(byIndustry.get(industry) as string[]).push(t)
    const group = getSectorGroup(industry)
    if (!bySectorGroup.has(group)) bySectorGroup.set(group, [])
    ;(bySectorGroup.get(group) as string[]).push(t)
  }
  return function match(c: Candidate): OppositeMatch | null {
    if (!c.sector) return null
    if (byIndustry.has(c.sector)) {
      return { type: 'industry', value: c.sector, tickers: byIndustry.get(c.sector) as string[] }
    }
    const group = getSectorGroup(c.sector)
    if (bySectorGroup.has(group)) {
      return { type: 'sector', value: group, tickers: bySectorGroup.get(group) as string[] }
    }
    return null
  }
}

// Industry match gets the thumbs-up in rationaleLines (see there) -- the
// more specific, more meaningful overlap of the two. Broad sector-group
// match is still shown (still a real, if looser, hedge) but stays neutral.
function oppositeMatchLine(match: OppositeMatch | null, oppositeSideLabel: string): string | null {
  if (!match) return null
  const tickers = match.tickers.join(', ')
  const where = match.type === 'industry' ? `industry (${match.value})` : `sector (${match.value})`
  return `Hedges your ${tickers} ${oppositeSideLabel} — same ${where}`
}

// Every line here reads straight off one field of a recommendations.json
// candidate (see recommendations.py) -- no extra computation, just turning
// numbers into a sentence. A line is omitted rather than shown as "—" when
// its underlying data source has nothing to say (e.g. no 13F match for that
// company name), so a card's rationale only ever lists real signal. Each
// line also carries a good/bad/neutral `signal` -- see the Signal type's
// own comment -- rendered as a small thumbs-up/down by ThumbIcon at each
// call site.
function rationaleLines(
  c: Candidate & { oppositeMatchLine?: string | null; oppositeMatchType?: 'industry' | 'sector' | null },
  side: 'Long' | 'Short'
): RationaleLine[] {
  const lines: RationaleLine[] = []
  // percentileLabel is shown in the card's own CardFooter (bottom-right)
  // instead of buried as a bullet here -- see that
  // component's own comment.

  const revenueGrowth = revenueGrowthLine(c, side)
  if (revenueGrowth) lines.push(revenueGrowth)

  const momentum = momentumLine(c, side)
  if (momentum) lines.push(momentum)

  const meanReversion = meanReversionLine(c, side)
  if (meanReversion) lines.push(meanReversion)

  const epsTrend = epsTrendLine(c, side)
  if (epsTrend) lines.push(epsTrend)

  const shortInterest = shortInterestLine(c, side)
  if (shortInterest) lines.push(shortInterest)

  // Industry-level hedge match is an unambiguous good sign (explicit
  // instruction) regardless of side -- reducing risk in the same specific
  // business is favorable whether the new idea is a Long or a Short.
  // Broad sector-group match is looser and stays neutral (no thumb).
  if (c.oppositeMatchLine) lines.push({ text: c.oppositeMatchLine, signal: c.oppositeMatchType === 'industry' ? 'good' : null })

  const news = c.news7d
  if (news && news.total > 0) {
    lines.push({
      text: `News: ${news.bullish} bulls/${news.bearish} bears last 7d`,
      signal: news.bullish > news.bearish ? (side === 'Long' ? 'good' : 'bad') : news.bearish > news.bullish ? (side === 'Long' ? 'bad' : 'good') : null,
      factor: 'news',
    })
  } else {
    lines.push({ text: 'No news coverage in the last 7 days', signal: null })
  }

  // Insider SELLS are a notoriously noisy signal (10b5-1 pre-scheduled
  // plans, tax, diversification -- mostly routine, not conviction), so a
  // plain buys-vs-sells comparison flagged even a token 2-buy/9-sell
  // quarter as outright bad -- explicit instruction/confirmed impression:
  // that's the wrong read. Any buys at all (even a minority of the
  // period's activity) is the meaningful signal here -- an insider
  // voluntarily putting their own money in despite whatever routine
  // selling is also happening. Only flip bad/good when there's PURE
  // selling (zero buys) -- no insider stepped in to buy at all.
  const insiders = c.insiders90d
  if (insiders && insiders.buys + insiders.sells > 0) {
    lines.push({
      text: `Insiders: ${insiders.buys} buys, ${insiders.sells} sells in last 90d`,
      signal:
        insiders.buys > 0
          ? side === 'Long'
            ? 'good'
            : 'bad'
          : insiders.sells > 0
            ? side === 'Long'
              ? 'bad'
              : 'good'
            : null,
      factor: 'insiders',
    })
  }

  const insiderOwnership = insiderOwnershipLine(c, side)
  if (insiderOwnership) lines.push(insiderOwnership)

  if (c.instChangeQoQ !== null && c.instChangeQoQ !== undefined) {
    lines.push({
      text: `Institutions ${c.instChangeQoQ >= 0 ? 'added' : 'trimmed'} ${fmtPctAbs(c.instChangeQoQ)} (13F)`,
      signal: sidedSignal(c.instChangeQoQ, side),
      factor: 'institutions',
    })
  }

  if (c.targetUpside !== null && c.targetUpside !== undefined) {
    const analysts = c.numberOfAnalystOpinions ? Math.round(c.numberOfAnalystOpinions) : null
    lines.push({
      text: `Target upside ${fmtPct(c.targetUpside)}${analysts ? ` (${analysts} analysts)` : ''}`,
      signal: sidedSignal(c.targetUpside, side),
      factor: 'targetUpside',
    })
  }

  return lines
}

// Small thumbs-up/down next to a rationale line -- see rationaleLines'
// own Signal-typed lines above. Deliberately tiny (13px, vs. SignalIcon's
// 17px header glyph) and inline with the text rather than the card
// header, since this is a per-factor read, not an overall
// portfolio-impact one the way SignalIcon's price-move glyph is. Renders
// nothing for a neutral/null signal -- explicit instruction: no icon
// clutter on a line that isn't actually discriminating.
function ThumbIcon({ signal }: { signal: Signal }) {
  if (signal === 'good') {
    return <ThumbsUp className="recommendation-thumb-icon recommendation-thumb-good" size={13} aria-label="Favorable" />
  }
  if (signal === 'bad') {
    return <ThumbsDown className="recommendation-thumb-icon recommendation-thumb-bad" size={13} aria-label="Unfavorable" />
  }
  return null
}

// Right-aligned tally sitting above the rationale bullets -- explicit
// instruction: a quick up/down count before scanning the individual
// factor lines below. Renders nothing when neither count is nonzero
// (same "no clutter on a non-discriminating card" reasoning ThumbIcon's
// own null case follows).
function ThumbCounts({ lines }: { lines: RationaleLine[] }) {
  const up = lines.filter((l) => l.signal === 'good').length
  const down = lines.filter((l) => l.signal === 'bad').length
  if (!up && !down) return null
  return (
    <div className="recommendation-thumb-counts">
      <span className="recommendation-thumb-count recommendation-thumb-good">
        <ThumbsUp size={13} aria-label="Favorable count" />
        {up}
      </span>
      <span className="recommendation-thumb-count recommendation-thumb-bad">
        <ThumbsDown size={13} aria-label="Unfavorable count" />
        {down}
      </span>
    </div>
  )
}

// Live price (ib_server.py's SSE stream, same as every other tab)
// with a same-shape daily-%-change badge as PeTable.jsx's Price column --
// current price vs. previousClose(dailyHistory3mo, monthlyHistory) (see
// that function for how it picks the fresher of the two history
// sources), falling back to recommendations.json's own (scoring-time)
// price if neither history source covers this ticker yet. `live` can be
// undefined (ib_server.py not running, or hasn't
// snapshotted this ticker yet) -- price/badge both degrade gracefully to
// the static price with no badge at all.
// How far today's move has to run in the position's own favor before
// PriceStat's thumbs-up shows -- a directional hit, not just "not
// adverse" (that's the warning triangle's own, much looser bar).
const FAVORABLE_MOVE_THRESHOLD = 0.05

// Shared by PriceStat (today's price/%-change badge) and SignalIcon (the
// warning-triangle/thumbs-up glyph next to the ticker) so both read off the
// exact same adverse/favorable determination rather than risking the two
// drifting apart.
function computeMoveSignal(
  c: Candidate,
  live: LiveTick | undefined,
  dailyHistory3mo: HistoryByTicker,
  monthlyHistory: HistoryByTicker,
  side: 'Long' | 'Short' | undefined,
  held: boolean | undefined
) {
  const referencePrice = previousClose(dailyHistory3mo[c.ticker], monthlyHistory[c.ticker]) ?? c.price ?? null
  const currentPrice = live?.last ?? c.price ?? null
  const changeRatio = live?.last != null && referencePrice ? live.last / referencePrice - 1 : null
  const adverse = side !== undefined && changeRatio !== null && (side === 'Long' ? changeRatio < 0 : changeRatio > 0)
  // Same FAVORABLE_MOVE_THRESHOLD magnitude as `favorable` below, mirrored
  // onto the adverse side -- explicit instruction: the warning icon turns
  // --orange (a step up from the plain --warn every adverse move already
  // gets) once today's move against the position passes 5%, not merely
  // negative-at-all for a long / positive-at-all for a short.
  const severelyAdverse =
    side !== undefined &&
    changeRatio !== null &&
    (side === 'Long' ? changeRatio < -FAVORABLE_MOVE_THRESHOLD : changeRatio > FAVORABLE_MOVE_THRESHOLD)
  const favorable =
    held &&
    side !== undefined &&
    changeRatio !== null &&
    (side === 'Long' ? changeRatio > FAVORABLE_MOVE_THRESHOLD : changeRatio < -FAVORABLE_MOVE_THRESHOLD)
  return { referencePrice, currentPrice, changeRatio, adverse, severelyAdverse, favorable }
}

// Placed right after the ticker symbol in each card's header (not inline
// with the price -- explicit instruction), sized up from the price-cell's
// own 13px so it reads at a glance next to the bolded ticker. Reuses
// computeMoveSignal so it's always in sync with PriceStat's own price/%
// badge just below.
function SignalIcon({
  c,
  live,
  dailyHistory3mo,
  monthlyHistory,
  side,
  held,
}: {
  c: Candidate
  live?: LiveTick
  dailyHistory3mo: HistoryByTicker
  monthlyHistory: HistoryByTicker
  // Which direction this card actually is (Long/Short) -- not derivable
  // from `c` itself (Candidate carries no side field; direction comes from
  // which section/rating pool the card was rendered into, so every call
  // site passes it in). Drives the warning triangle: a Long card is
  // working against the position when today's move is negative, a Short
  // one when it's positive -- the exact opposite comparison.
  side?: 'Long' | 'Short'
  // Gates the thumbs-up to actual portfolio positions (CloseCard's rows,
  // always true there since To close is exclusively held positions; or a
  // RecommendationCard/RejectedCard already flagged `held`) -- a live
  // candidate idea not yet opened doesn't have a "performance" of its own
  // to celebrate yet, just a signal.
  held?: boolean
}) {
  const { adverse, severelyAdverse, favorable } = computeMoveSignal(c, live, dailyHistory3mo, monthlyHistory, side, held)
  if (adverse) {
    return (
      <AlertTriangle
        className={`recommendation-signal-icon ${severelyAdverse ? 'price-severe-warn-icon' : 'price-warn-icon'}`}
        size={17}
        aria-label="Warning"
        title={
          severelyAdverse
            ? `Moving against this ${side === 'Long' ? 'long' : 'short'} by more than ${fmtPct(FAVORABLE_MOVE_THRESHOLD)} today`
            : `Moving against this ${side === 'Long' ? 'long' : 'short'} today`
        }
      />
    )
  }
  if (favorable) {
    return (
      <ThumbsUp
        className="recommendation-signal-icon price-good-icon"
        size={17}
        aria-label="Strong move in your favor"
        title={`${side === 'Long' ? 'Up' : 'Down'} more than ${fmtPct(FAVORABLE_MOVE_THRESHOLD)} today, working in this ${side === 'Long' ? 'long' : 'short'}'s favor`}
      />
    )
  }
  return null
}

function PriceStat({
  c,
  live,
  dailyHistory3mo,
  monthlyHistory,
  hideLabel,
  side,
  held,
}: {
  c: Candidate
  live?: LiveTick
  dailyHistory3mo: HistoryByTicker
  monthlyHistory: HistoryByTicker
  hideLabel?: boolean
  // Which direction this card actually is (Long/Short) -- not derivable
  // from `c` itself (Candidate carries no side field; direction comes
  // from which section/rating pool the card was rendered into, so every
  // call site passes it in). Fed straight into computeMoveSignal, same as
  // SignalIcon's own copy of this prop -- see that component for what it
  // drives.
  side?: 'Long' | 'Short'
  // See SignalIcon's own `held` doc -- passed through to computeMoveSignal
  // here for the exact same reason, so this component's %-change badge and
  // that one's icon are always reading off one shared determination.
  held?: boolean
}) {
  const { referencePrice, currentPrice, changeRatio } = computeMoveSignal(c, live, dailyHistory3mo, monthlyHistory, side, held)
  const changeClass = changeRatio === null ? '' : Math.abs(changeRatio) <= 0.005 ? 'perf-neutral' : changeRatio >= 0 ? 'perf-pos' : 'perf-neg'
  // Flags whether referencePrice above is genuinely yesterday's close, or
  // a fallback to something older -- explicit instruction, after PUMP
  // showed a positive daily move while actually down: yfinance/IB can
  // both be missing the most recent trading day's bar (confirmed live:
  // yfinance skipped an entire Monday for every ticker checked), which
  // silently turns "today vs. yesterday" into "today vs. several days
  // ago" without anything on the card saying so.
  const previousCloseDate = previousCloseInfo(dailyHistory3mo[c.ticker], monthlyHistory[c.ticker])?.date ?? null
  const previousCloseFresh = previousCloseDate !== null && previousCloseDate >= mostRecentCompletedTradingDay()
  return (
    <div className="stat">
      <span className="n num price-cell">
        <span className="price-value">{fmtPrice(currentPrice)}</span>
        {changeRatio !== null && live && (
          <span
            className={`live-price ${changeClass}`}
            title={`${fmtPrice(live.last ?? null)} ${fmtMinutesAgo(live.timestamp)} vs. yesterday's close ${fmtPrice(referencePrice)}`}
          >
            {fmtPct(changeRatio)}
          </span>
        )}
        <Flag
          className={`recommendation-signal-icon ${previousCloseFresh ? 'price-fresh-flag' : 'price-stale-flag'}`}
          size={13}
          aria-label={previousCloseFresh ? 'Previous close is current' : 'Previous close is stale'}
          title={
            previousCloseDate === null
              ? "No previous close available at all for this ticker -- today's daily move can't be computed."
              : previousCloseFresh
                ? `Yesterday's close (${previousCloseDate}) is available -- today's daily move is computed against it.`
                : `Most recent close on file is from ${previousCloseDate}, expected ${mostRecentCompletedTradingDay()} or newer -- today's daily move may be stale, comparing against an older close than yesterday's.`
          }
        />
      </span>
      {!hideLabel && <span className="l">Price</span>}
    </div>
  )
}

// `held` (already a nonzero position in this ticker, long or short — see
// RecommendationsView's heldTickers) gets a lighter card background
// (recommendation-card-held, var(--surface-2) — the same alternate-surface
// token every other banded table in this app already uses) plus a small
// text badge, since color alone shouldn't be the only signal.
function RecommendationCard({
  c,
  held,
  live,
  dailyHistory3mo,
  monthlyHistory,
  side,
}: {
  c: RankedCandidate
  held: boolean
  live?: LiveTick
  dailyHistory3mo: HistoryByTicker
  monthlyHistory: HistoryByTicker
  side: 'Long' | 'Short'
}) {
  const lines = rationaleLines(c, side)
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
          <SignalIcon c={c} live={live} dailyHistory3mo={dailyHistory3mo} monthlyHistory={monthlyHistory} side={side} held={held} />
          <span className="recommendation-name">{c.name}</span>
        </div>
        <div className="recommendation-badges">
          {held && <span className="recommendation-held-badge">In portfolio</span>}
          <span className={`rec-badge ${ratingClass(c.rating)}`}>{c.rating}</span>
        </div>
      </div>

      <div className="recommendation-card-stats">
        <PriceStat c={c} live={live} dailyHistory3mo={dailyHistory3mo} monthlyHistory={monthlyHistory} hideLabel side={side} held={held} />
        <div className="stat">
          <span className="n num sector-value">{c.sector || '—'}</span>
        </div>
      </div>

      <ThumbCounts lines={lines} />
      <ul className="recommendation-rationale">
        {lines.map((line, i) => (
          <li key={i}>
            {line.text}
            <ThumbIcon signal={line.signal} />
          </li>
        ))}
      </ul>
      <CardFooter c={c} />
    </div>
  )
}

// Always shown as "held" (grey background) -- every row here is, by
// construction, an existing position (see the closes filter below) -- plus
// a "Close long"/"Close short" action tag and an explicit reason sentence
// ahead of the same recent-signal rationale bullets RecommendationCard
// uses, so the "why" isn't just the rating badge.
function CloseCard({
  c,
  live,
  dailyHistory3mo,
  monthlyHistory,
  goodSign,
  badSign,
}: {
  c: CloseRow
  live?: LiveTick
  dailyHistory3mo: HistoryByTicker
  monthlyHistory: HistoryByTicker
  goodSign?: boolean
  badSign?: boolean
}) {
  const lines = rationaleLines(c, c.closeSide)
  return (
    <div
      className={`asset-card recommendation-card recommendation-card-held${goodSign ? ' recommendation-card-goodsign' : ''}${badSign ? ' recommendation-card-badsign' : ''}`}
    >
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
          <SignalIcon c={c} live={live} dailyHistory3mo={dailyHistory3mo} monthlyHistory={monthlyHistory} side={c.closeSide} held />
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
        </div>
        <PriceStat c={c} live={live} dailyHistory3mo={dailyHistory3mo} monthlyHistory={monthlyHistory} hideLabel side={c.closeSide} held />
      </div>

      <ul className="recommendation-close-reasons">
        {c.reasons.map((r, i) => (
          <li key={i}>{r.text}</li>
        ))}
      </ul>

      <ThumbCounts lines={lines} />
      <ul className="recommendation-rationale">
        {lines.map((line, i) => (
          <li key={i}>
            {line.text}
            <ThumbIcon signal={line.signal} />
          </li>
        ))}
      </ul>
      <CardFooter c={c} />
    </div>
  )
}

// A Strong Buy/Strong Sell candidate that failed one of the Long/Short
// idea-list gates -- see buildRejectionReasons. recommendation-card-blocked
// (explicit instruction: light grey, not this app's dark-mode near-black
// default surface) is its own modifier, independent of recommendation-
// card-held's --surface-2 banding -- most of these aren't held positions
// at all, just candidates that scored a top rating but didn't clear an
// opening gate, so reusing "held" styling here would be the wrong signal.
function RejectedCard({
  c,
  held,
  live,
  dailyHistory3mo,
  monthlyHistory,
  side,
}: {
  c: RejectedRow
  held: boolean
  live?: LiveTick
  dailyHistory3mo: HistoryByTicker
  monthlyHistory: HistoryByTicker
  side: 'Long' | 'Short'
}) {
  const lines = rationaleLines(c, side)
  return (
    <div
      className={`asset-card recommendation-card recommendation-card-blocked${held ? ' recommendation-card-held' : ''}`}
    >
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
          <SignalIcon c={c} live={live} dailyHistory3mo={dailyHistory3mo} monthlyHistory={monthlyHistory} side={side} held={held} />
          <span className="recommendation-name">{c.name}</span>
        </div>
        <div className="recommendation-badges">
          {held && <span className="recommendation-held-badge">In portfolio</span>}
          <span className={`rec-badge ${ratingClass(c.rating)}`}>{c.rating}</span>
        </div>
      </div>

      <div className="recommendation-card-stats">
        <PriceStat c={c} live={live} dailyHistory3mo={dailyHistory3mo} monthlyHistory={monthlyHistory} hideLabel side={side} held={held} />
        <div className="stat">
          <span className="n num sector-value">{c.sector || '—'}</span>
        </div>
      </div>

      <ul className="recommendation-close-reasons">
        {c.reasons.map((r, i) => (
          <li key={i}>{r.text}</li>
        ))}
      </ul>

      <ThumbCounts lines={lines} />
      <ul className="recommendation-rationale">
        {lines.map((line, i) => (
          <li key={i}>
            {line.text}
            <ThumbIcon signal={line.signal} />
          </li>
        ))}
      </ul>
      <CardFooter c={c} />
    </div>
  )
}

function RecommendationSection<T>({
  title,
  titleInfo,
  subtitle,
  rows,
  renderCard,
  emptyMessage,
}: {
  title: string
  titleInfo?: ReactNode
  subtitle: string
  rows: T[]
  renderCard: (c: T) => ReactNode
  emptyMessage: string
}) {
  return (
    <section className="recommendation-section">
      <h2 className="recommendation-section-title">
        {title}
        {titleInfo}
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
// positions/prices from ib_server.py's SSE stream (same pattern
// Positions/Sectors/Themes/News already use).
//
// Long = every Strong Buy/Buy candidate that clears the opening gates below,
// sorted best score first; Short = every Strong Sell/Sell candidate that
// clears them, sorted worst score first -- explicit instruction: no top-N
// cutoff, show everything that qualifies rather than an arbitrary top 30.
// Neither side excludes held tickers -- a current long can appear in Long, a
// current short can appear in Short. Held status (side-specific --
// heldLongTickers/heldShortTickers below) drives the "In portfolio" grey
// highlight on whichever side actually holds it.
//
// A hard momentum-direction rule (eligible() below) gates ALL THREE groups
// before ranking, per explicit instruction: never recommend buying (Long,
// or covering/closing a Short) an asset with non-positive momentum, and
// never recommend selling (Short, or closing a Long) an asset with
// non-negative momentum. A candidate with unknown momentum is excluded
// rather than assumed compliant, since the rule is absolute. Confirmed by
// an actual backtest against 2 years of daily bars (regression-momentum,
// same formula/window as production, Fama-MacBeth cross-sectional test):
// momentum is a CONTINUATION signal at every horizon tested (5 to 63
// trading days) -- high momentum predicts higher forward returns, not a
// reversal -- so the short side stays keyed off NEGATIVE momentum
// (shorting the weaker performer), the mirror of the long side, not an
// inverted "short the overextension" rule.
//
// Within each side, a non-held candidate that hedges an existing position
// on the OPPOSITE side (same industry or sector -- see the longs/shorts memos'
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
// instruction, "particularly if momentum is no longer supportive"), a
// still-Buy/Sell score sitting close enough to the Hold boundary that it's
// likely to cross soon (explicit instruction: score-based reasons, not
// beta -- an earlier high-beta check here turned out not to be a useful
// signal in practice), fundamentals rolling over (a long whose EPS estimate
// trend or revenue growth has turned negative, or a short whose EPS
// estimate trend has turned positive), and a short that's become crowded
// (explicit instruction: flag a position "not adequate for the portfolio
// because of too high risk or other reasons"). Position-size concentration
// used to be flagged here too; dropped per explicit instruction -- not a
// useful closing signal on its own.
// MSI (daily Money Flow Index/RSI, bounded [0, 100] -- see
// IBApp.get_momentum) is used here as a mean-reversion overbought/
// oversold read, NOT the continuation treatment scoring.py's own
// momentum_rank sweet-spot curve uses for the composite score -- these
// are two independent, deliberately different treatments of the same
// raw number (see that curve's own comment for why the composite score
// wants "healthy middle-strength" as a quality factor; this page wants
// "is this a genuinely overbought/oversold extreme worth acting on").
// Explicit instruction, confirmed against concrete tickers (LQDA at 28
// should read as a GOOD long signal, not excluded; a reading of 81
// should read as a GOOD short signal): oversold (< MOMENTUM_OVERSOLD)
// is good for a Long/bad for a Short, overbought (> MOMENTUM_OVERBOUGHT)
// is bad for a Long/good for a Short, and -- explicit instruction --
// anything in between carries NO signal at all (null, not "mildly
// good/bad") since it's neither extreme. Same shape now applies to
// ST-MSI/meanReversion below (see MEAN_REVERSION_OVERBOUGHT/OVERSOLD),
// just with its own, separately-tuned band. Keep in sync with
// ib_server.py's own _REC_MOMENTUM_OVERBOUGHT/_REC_MOMENTUM_OVERSOLD by
// hand.
//
// eligibleToBuy/eligibleToSell below are deliberately LOOSER than this
// signal shape, though -- explicit instruction: the idea-list gate only
// BLOCKS the bad extreme (overbought for a Long, oversold for a Short),
// it doesn't REQUIRE the good one. Neutral candidates (and even the
// opposite extreme -- oversold for a Long, overbought for a Short) are
// still eligible; the thumb icon just won't show a strong opinion (or
// will show a positive one) on them.
const MOMENTUM_OVERSOLD = 30
const MOMENTUM_OVERBOUGHT = 70
// Explicit instruction: block, don't require -- a Long is only excluded
// when MSI is overbought (chasing risk); anything else (neutral OR
// oversold) is fine to recommend. Short is the mirror: only excluded
// when oversold, neutral or overbought both fine.
function eligibleToBuy(c: Candidate): boolean {
  return c.momentum !== null && c.momentum !== undefined && c.momentum <= MOMENTUM_OVERBOUGHT
}
function eligibleToSell(c: Candidate): boolean {
  return c.momentum !== null && c.momentum !== undefined && c.momentum >= MOMENTUM_OVERSOLD
}

// FINRA's biweekly-settlement pct-of-float (shortPctOfFloatFinra) is
// fresher than yfinance's shortPercentOfFloat -- the latter only ever
// reflects the month-end settlement, silently skipping FINRA's mid-month
// one (see scoring.load_short_interest_scores). Preferred here; falls back
// to shortPercentOfFloat only for a ticker FINRA doesn't report (thinly
// shorted, or delisted/renamed since).
function effectiveShortPctOfFloat(c: Candidate): number | null | undefined {
  return c.shortPctOfFloatFinra ?? c.shortPercentOfFloat
}

// Short-only: a name already shorted by more than MAX_SHORT_INTEREST of its
// float is a crowded short -- squeeze risk that makes it a worse short idea
// regardless of how it scores otherwise. Explicit instruction: "any company
// with more than 10% of short interest should be blocked and not presented
// as a short opportunity" on the Short list -- originally 0.2, lowered to
// 0.1 per that follow-up instruction. Unlike the momentum gate above (an
// absolute "never" that treats unknown as disqualifying), this is a
// risk-avoidance cap on a known-bad condition -- a candidate with no
// short-interest data at all isn't assumed crowded, so it still passes.
const MAX_SHORT_INTEREST = 0.1
function notCrowded(c: Candidate): boolean {
  const pct = effectiveShortPctOfFloat(c)
  return pct === null || pct === undefined || pct <= MAX_SHORT_INTEREST
}

// insiderOwnershipLine's own materiality bar -- explicit instruction: 5%.
// Below this, ownership isn't treated as a real skin-in-the-game signal
// either way (no icon), same "only the tail counts" idea MAX_SHORT_INTEREST
// applies to the short-interest line above.
const MIN_INSIDER_OWNERSHIP = 0.05

// Revenue-growth gate on the idea lists themselves (separate from the To
// close fundamentals check above, which flags a HELD position after the
// fact) -- explicit instruction: never recommend shorting a name still
// growing revenue faster than REVENUE_GROWTH_THRESHOLD (a real grower is a
// bad short candidate regardless of how it scores), and never recommend a
// long that isn't clearing that same bar (a low/no-growth name is a weak
// long idea even at a great score). Same fail-open treatment as
// notCrowded's shortPercentOfFloat above, not the momentum gate's
// fail-closed one: a candidate with no revenueGrowth data isn't assumed to
// violate either side, so it still passes. revenueGrowth lives on
// tickerScreener (sorted_screen.csv covers the whole universe), not on the
// recommendations.json candidate itself -- see the longs/shorts pools
// below, which look it up by ticker rather than expecting it on `c`.
const REVENUE_GROWTH_THRESHOLD = 0.1
function sufficientGrowthForLong(revenueGrowth: number | null | undefined): boolean {
  return revenueGrowth === null || revenueGrowth === undefined || revenueGrowth >= REVENUE_GROWTH_THRESHOLD
}
function notTooMuchGrowthForShort(revenueGrowth: number | null | undefined): boolean {
  return revenueGrowth === null || revenueGrowth === undefined || revenueGrowth <= REVENUE_GROWTH_THRESHOLD
}

// Short-term mean-reversion gate on the idea lists (mirrors the growth gate
// above) -- meanReversion is now IBApp's hourly Money Flow Index (see
// IBApp.get_momentum), bounded [0, 100], NOT the old signed regression-
// slope trend: high means hourly-overbought, low means hourly-oversold.
// Used here as an entry-timing signal, not a second momentum vote: a
// stock already overbought on the hourly timeframe is a stock a new long
// would be CHASING (bad entry, that move already happened), while a
// stock already oversold on the hourly timeframe is one a new short
// would be chasing the same way -- so a significantly OVERBOUGHT reading
// blocks a new long / flags a held long to close (the exact FIVN
// situation: bought right after a multi-day hourly spike, meanReversion
// already deeply overbought, price mean-reverted down from there), while
// a significantly OVERSOLD reading blocks a new short / flags a held
// short to close. "Significant" is deliberately a wide dead zone
// (MEAN_REVERSION_OVERBOUGHT/MEAN_REVERSION_OVERSOLD, the conventional
// MFI reference lines) rather than any deviation from the midpoint --
// explicit instruction (from the old unbounded-scale version of this
// gate): most readings sit well inside this band just from ordinary
// hourly noise, so gating on any deviation at all would trip constantly.
// Only the tail (roughly the most extreme ~20% of readings either way)
// is meant to count -- momentumLine/meanReversionLine's own thumb-icon
// signal deliberately does NOT use this same wide band (see that
// function's comment), just this hard entry/close gate. Same fail-open
// treatment as the growth/crowded-short gates: a candidate with no
// meanReversion at all (it's only computed for CANDLESTICK_TOP_N
// ranked/held tickers, not the whole universe -- see IBApp.get_momentum)
// isn't assumed to violate either side, so it still passes. Lives on
// tickerScreener, not the recommendations.json candidate, same as
// revenueGrowth above.
const MEAN_REVERSION_OVERBOUGHT = 80
const MEAN_REVERSION_OVERSOLD = 20
function meanReversionOkForLong(meanReversion: number | null | undefined): boolean {
  return meanReversion === null || meanReversion === undefined || meanReversion < MEAN_REVERSION_OVERBOUGHT
}
function meanReversionOkForShort(meanReversion: number | null | undefined): boolean {
  return meanReversion === null || meanReversion === undefined || meanReversion > MEAN_REVERSION_OVERSOLD
}

// Entry-side EPS-estimate-trend gate -- same "even at a great score"
// reasoning already applied to revenueGrowth above (explicit instruction:
// EPS revision trend is important enough to gate NEW ideas on, not just
// flag on a position already held -- buildCloseReasons' own epsTrend
// check further below predates this and only ever looked at open
// positions). Reuses epsTrendValue's own epsRevision0y/1y averaging (see
// its comment) and its same plain-sign check (0), not a magnitude bar
// like REVENUE_GROWTH_THRESHOLD -- "estimates have been cut/raised at
// all in the last 30 days" is itself the signal, no dead zone needed the
// way mean-reversion's ordinary-noise band does. Same fail-open
// treatment as every other idea-list gate: a candidate with no
// epsRevision0y/1y data isn't assumed to violate either side.
function epsTrendOkForLong(epsTrend: number | null): boolean {
  return epsTrend === null || epsTrend >= 0
}
function epsTrendOkForShort(epsTrend: number | null): boolean {
  return epsTrend === null || epsTrend <= 0
}

// To close only (not an opening gate) -- explicit instruction, "review"
// tier: a held position reporting earnings within EARNINGS_REVIEW_HOURS is
// flagged for a look regardless of side, rating, or any other signal --
// an earnings call is a binary, thesis-agnostic volatility event, not
// something the momentum/growth/mean-reversion story says anything about
// either way. Business hours, not calendar days (see earnings.js's own
// businessMillisBetween -- same distance earningsUrgencyClass buckets
// already use elsewhere in this app), so a Friday-evening report doesn't
// read as further out than it actually is in trading days just because a
// weekend sits in between.
const EARNINGS_REVIEW_HOURS = 48
function hoursUntilEarnings(earningsTimestampStart: number | null | undefined, now: number): number | null {
  if (earningsTimestampStart === null || earningsTimestampStart === undefined) return null
  const earningsMs = earningsTimestampStart * 1000
  if (earningsMs <= now) return null
  return businessMillisBetween(now, earningsMs) / 3600000
}

// Fundamentals rolling over, independent of rating/momentum/score: a long
// whose analyst EPS estimates have been cut over the last 30 days
// (epsRevision0y/1y -- see IBApp._eps_revision/scoring.eps_trend_rank) or
// whose trailing revenue growth has gone negative is losing the
// fundamental support for the position even if the composite score/rating
// hasn't caught up yet; a short whose EPS estimates have been raised is
// the mirror case (estimates going up is bearish for a short thesis).
// Averages epsRevision0y/1y the same way eps_trend_rank does when both are
// present, but (unlike that rank) only uses whichever is actually present
// rather than penalizing a missing period -- this is a raw sign check, not
// a ranked score.
function epsTrendValue(c: Candidate | null | undefined): number | null {
  const rev0 = c?.epsRevision0y
  const rev1 = c?.epsRevision1y
  const has0 = rev0 !== null && rev0 !== undefined
  const has1 = rev1 !== null && rev1 !== undefined
  if (has0 && has1) return ((rev0 as number) + (rev1 as number)) / 2
  if (has0) return rev0 as number
  if (has1) return rev1 as number
  return null
}

// scoring.py's own rating_for_percentile buckets (RATING_THRESHOLDS: 0.05,
// 0.20, 0.80, 0.95) -- duplicated here as percentile points (score * 100,
// the same units scorePercentile already uses) since the frontend has no
// access to that Python constant directly. Only the Buy|Hold (20) and
// Sell|Hold (80) boundaries matter below -- those are the ones that flip a
// position into the rating-based close trigger above; Strong Buy/Strong
// Sell have much more room before their own boundary matters.
const HOLD_BOUNDARY_LONG_PCT = 20
const HOLD_BOUNDARY_SHORT_PCT = 80
// How close (in percentile points) counts as "near" a boundary -- an early
// warning before the rating itself has actually crossed, not a hard
// prediction.
const SCORE_BOUNDARY_MARGIN = 3

// Everything that can put a held position in the To close group -- a
// rating contradiction (see the existing closes logic) is the most
// decisive single reason, but explicit instruction was to also flag "too
// high risk or other reasons" even when the rating hasn't (yet) turned:
// momentum alone no longer supporting the side ("particularly if momentum
// is no longer supportive" -- the exact NVDA/MU situation surfaced earlier:
// still Strong Buy rated, but momentum had already gone flat/negative),
// oversized position concentration, a short that's become crowded since it
// was opened, or a still-Buy/Sell-rated score sitting close enough to the
// Hold boundary that it's likely to cross soon (a high-beta check used to
// sit here instead -- dropped as not a useful signal on its own; every
// other check here is either momentum- or score-based instead, per
// explicit instruction). A position can trip more than one of these at
// once -- returns every reason that applies, not just the first match, so
// the card shows the full picture rather than picking one arbitrarily.
function buildCloseReasons({ shares, c, now }: { shares: number; c: Candidate; now: number }): Reason[] {
  const reasons: Reason[] = []
  const isLong = shares > 0
  const rating = c?.rating

  // Not gated on momentum (unlike the Long/Short idea lists' eligibleToBuy/
  // eligibleToSell) -- the rating itself is the decisive signal for an
  // existing position: a held short that's drifted to Hold is done being a
  // short idea regardless of whether momentum has caught up yet. Momentum
  // is tracked as its own independent reason right below, so a position
  // whose rating flips but whose momentum still (for now) agrees shows
  // both the rating reason and, correctly, no momentum reason.
  if (rating) {
    if (isLong && !BUY_RATINGS.has(rating)) {
      reasons.push({ type: 'rating', text: `No longer Buy (${rating}) — reconsider.` })
    } else if (!isLong && !SELL_RATINGS.has(rating)) {
      reasons.push({ type: 'rating', text: `No longer Sell (${rating}) — reconsider.` })
    }
  }

  // Independent of rating -- a held Buy/Strong Buy long entered on an
  // oversold MSI reading is worth flagging once MSI has now reached the
  // OPPOSITE extreme (overbought -- the reversion target was hit, a
  // take-profit/reversal-risk signal), same mirror for a held Sell/
  // Strong Sell short entered overbought once MSI drops to oversold.
  // Same MOMENTUM_OVERSOLD/MOMENTUM_OVERBOUGHT extremes the entry gate
  // uses (see that constant's own comment) -- this isn't a looser bar
  // the way the old continuation-framing check was, since mean-reversion
  // has no natural "in between" trigger point the way a directional
  // crossing does.
  if (c && c.momentum !== null && c.momentum !== undefined) {
    if (isLong && c.momentum > MOMENTUM_OVERBOUGHT) {
      reasons.push({
        type: 'momentum',
        text: `MSI has reached overbought (${c.momentum.toFixed(0)}) — the reversion this long was entered on may be done.`,
      })
    } else if (!isLong && c.momentum < MOMENTUM_OVERSOLD) {
      reasons.push({
        type: 'momentum',
        text: `MSI has reached oversold (${c.momentum.toFixed(0)}) — the reversion this short was entered on may be done.`,
      })
    }
  }

  const epsTrend = epsTrendValue(c)
  if (epsTrend !== null) {
    if (isLong && epsTrend < 0) {
      reasons.push({
        type: 'eps-trend',
        text: `EPS est cut (trend ${fmtPct(epsTrend)}) — reconsider.`,
      })
    } else if (!isLong && epsTrend > 0) {
      reasons.push({
        type: 'eps-trend',
        text: `EPS est raised (trend ${fmtPct(epsTrend)}) — reconsider.`,
      })
    }
  }

  if (isLong && c?.revenueGrowth !== null && c?.revenueGrowth !== undefined && c.revenueGrowth < 0) {
    reasons.push({
      type: 'revenue-growth',
      text: `Revenue growth has turned negative (${fmtPct(c.revenueGrowth)}) — consider closing.`,
    })
  }

  // Same significant-magnitude bar as meanReversionOkForLong/
  // meanReversionOkForShort above (not any deviation from the midpoint --
  // most readings sit well inside this band from ordinary hourly noise).
  // A held long that's spiked hard enough on the hourly timeframe to
  // already be past the same bar that would have blocked opening it
  // fresh today is the FIVN situation -- bought right after a run-up,
  // mean-reverts against you from there. Mirror case for a held short: a
  // hard enough drop that a bounce is due against the short.
  if (c && c.meanReversion !== null && c.meanReversion !== undefined) {
    if (isLong && c.meanReversion >= MEAN_REVERSION_OVERBOUGHT) {
      reasons.push({
        type: 'mean-reversion',
        text: `ST-MSI overbought (${c.meanReversion.toFixed(0)}) — ready for a pullback.`,
      })
    } else if (!isLong && c.meanReversion <= MEAN_REVERSION_OVERSOLD) {
      reasons.push({
        type: 'mean-reversion',
        text: `ST-MSI oversold (${c.meanReversion.toFixed(0)}) — ready for a bounce.`,
      })
    }
  }

  const shortPct = c ? effectiveShortPctOfFloat(c) : null
  if (!isLong && shortPct !== null && shortPct !== undefined && shortPct > MAX_SHORT_INTEREST) {
    reasons.push({
      type: 'crowded-short',
      text: `${fmtPctAbs(shortPct)} of float is already short — this short has become crowded, squeeze risk.`,
    })
  }

  const earningsHoursAway = hoursUntilEarnings(c?.earningsTimestampStart, now)
  if (earningsHoursAway !== null && earningsHoursAway <= EARNINGS_REVIEW_HOURS) {
    reasons.push({
      type: 'earnings',
      text: `Reports earnings ${fmtEarningsDate(c.earningsTimestampStart as number)} — a volatility event coming up either way, worth a look regardless of thesis.`,
    })
  }

  // Still rated Buy/Sell (not yet Hold, so the rating check above hasn't
  // fired), but close enough to the Hold boundary that conviction is
  // visibly fading -- an earlier warning than waiting for the rating
  // label itself to actually change.
  if (c && c.scorePercentile !== null && c.scorePercentile !== undefined) {
    if (isLong && rating === 'Buy' && c.scorePercentile >= HOLD_BOUNDARY_LONG_PCT - SCORE_BOUNDARY_MARGIN) {
      reasons.push({
        type: 'score-boundary',
        text: `Score is only ${(HOLD_BOUNDARY_LONG_PCT - c.scorePercentile).toFixed(1)} points above the Hold boundary — still rated Buy, but conviction is fading.`,
      })
    } else if (!isLong && rating === 'Sell' && c.scorePercentile <= HOLD_BOUNDARY_SHORT_PCT + SCORE_BOUNDARY_MARGIN) {
      reasons.push({
        type: 'score-boundary',
        text: `Score is only ${(c.scorePercentile - HOLD_BOUNDARY_SHORT_PCT).toFixed(1)} points below the Hold boundary — still rated Sell, but conviction is fading.`,
      })
    }
  }

  return reasons
}

// Why a Strong Buy/Strong Sell candidate -- the two ratings with the most
// conviction behind them -- did NOT make the Long/Short idea list, even
// though it cleared the highest ratings bar. Runs the exact same gates the
// longs/shorts pools filter on (eligibleToBuy/eligibleToSell,
// sufficientGrowthForLong/notTooMuchGrowthForShort, notCrowded,
// meanReversionOkForLong/meanReversionOkForShort,
// epsTrendOkForLong/epsTrendOkForShort) rather than a second copy of the
// thresholds, so this can never drift out of sync with what actually
// gates the pools. Long/Short have no ranking cutoff of their own (every
// qualifying candidate is shown), so a nonempty result here always means
// a real gate failure, never "just didn't rank high enough."
function buildRejectionReasons({ c, tickerScreener }: { c: Candidate; tickerScreener: ScreenerByTicker }): Reason[] {
  const reasons: Reason[] = []
  const screenerRow = tickerScreener[c.ticker]
  const revenueGrowth = screenerRow?.revenueGrowth
  const meanReversion = screenerRow?.meanReversion
  const epsTrend = epsTrendValue(screenerRow)

  if (c.rating === 'Strong Buy') {
    if (!eligibleToBuy(c)) {
      reasons.push({
        type: 'momentum',
        text:
          c.momentum === null || c.momentum === undefined
            ? 'MSI data is unavailable.'
            : `MSI is ${c.momentum.toFixed(0)}, overbought (above ${MOMENTUM_OVERBOUGHT}).`,
      })
    }
    if (!sufficientGrowthForLong(revenueGrowth)) {
      reasons.push({
        type: 'revenue-growth',
        text: `Revenue growth is ${fmtPct(revenueGrowth as number)}, below ${fmtPctAbs(REVENUE_GROWTH_THRESHOLD)}.`,
      })
    }
    if (!meanReversionOkForLong(meanReversion)) {
      reasons.push({
        type: 'mean-reversion',
        text: `ST-MSI overbought (${(meanReversion as number).toFixed(0)}) — already spiked.`,
      })
    }
    if (!epsTrendOkForLong(epsTrend)) {
      reasons.push({
        type: 'eps-trend',
        text: `EPS est cut (trend ${fmtPct(epsTrend as number)}).`,
      })
    }
  } else if (c.rating === 'Strong Sell') {
    if (!eligibleToSell(c)) {
      reasons.push({
        type: 'momentum',
        text:
          c.momentum === null || c.momentum === undefined
            ? 'MSI data is unavailable.'
            : `MSI is ${c.momentum.toFixed(0)}, oversold (below ${MOMENTUM_OVERSOLD}).`,
      })
    }
    if (!notCrowded(c)) {
      reasons.push({
        type: 'crowded-short',
        text: `${fmtPctAbs(effectiveShortPctOfFloat(c) as number)} of float already short, over ${fmtPctAbs(MAX_SHORT_INTEREST)}.`,
      })
    }
    if (!notTooMuchGrowthForShort(revenueGrowth)) {
      reasons.push({
        type: 'revenue-growth',
        text: `Revenue growth is ${fmtPct(revenueGrowth as number)}, above ${fmtPctAbs(REVENUE_GROWTH_THRESHOLD)}.`,
      })
    }
    if (!meanReversionOkForShort(meanReversion)) {
      reasons.push({
        type: 'mean-reversion',
        text: `ST-MSI oversold (${(meanReversion as number).toFixed(0)}) — already dropped.`,
      })
    }
    if (!epsTrendOkForShort(epsTrend)) {
      reasons.push({
        type: 'eps-trend',
        text: `EPS est raised (trend ${fmtPct(epsTrend as number)}).`,
      })
    }
  }

  return reasons
}

// Plain-English mirrors of eligibleToBuy/eligibleToSell/notCrowded/
// sufficientGrowthForLong/notTooMuchGrowthForShort/
// epsTrendOkForLong/epsTrendOkForShort (Long/Short) and
// buildCloseReasons (To close) above -- kept as their own lists (rather
// than generated from the functions) the same way PeTable.jsx's
// SCORE_FACTORS mirrors scoring.py's weights: there's no live endpoint
// serving the rule sets themselves, only the resulting candidates/reason
// strings, so these have to be kept in sync by hand whenever the
// corresponding filter/reason function changes.
const LONG_RULES = [
  { label: 'Rating', note: 'Strong Buy or Buy.' },
  { label: 'MSI', note: `Not overbought (${MOMENTUM_OVERBOUGHT} or below) — a mean-reversion read, not continuation; blocks only the overbought extreme, doesn't require oversold. Unknown MSI excluded.` },
  {
    label: 'Revenue growth',
    note: `Trailing revenue growth at or above ${fmtPctAbs(REVENUE_GROWTH_THRESHOLD)}; unknown growth not excluded.`,
  },
  {
    label: 'ST-MSI',
    note: `ST-MSI not significantly overbought — excludes a stock that's already spiked and would be chased; unknown not excluded.`,
  },
  {
    label: 'EPS trend',
    note: 'Consensus EPS estimates not cut; unknown not excluded.',
  },
  {
    label: 'Ranking',
    note: 'Best composite score first. A non-held candidate that hedges an existing short position (same industry/sector) gets a small score bonus.',
  },
]

const SHORT_RULES = [
  { label: 'Rating', note: 'Strong Sell or Sell.' },
  { label: 'MSI', note: `Not oversold (${MOMENTUM_OVERSOLD} or above) — the mirror of Long's rule; blocks only the oversold extreme, doesn't require overbought. Unknown MSI excluded.` },
  {
    label: 'Not crowded',
    note: `No more than ${fmtPctAbs(MAX_SHORT_INTEREST)} of float already sold short; unknown short interest not excluded.`,
  },
  {
    label: 'Revenue growth',
    note: `Trailing revenue growth at or below ${fmtPctAbs(REVENUE_GROWTH_THRESHOLD)}; unknown growth not excluded.`,
  },
  {
    label: 'ST-MSI',
    note: `ST-MSI not significantly oversold — excludes a stock that's already dropped and would be chased; unknown not excluded.`,
  },
  {
    label: 'EPS trend',
    note: 'Consensus EPS estimates not raised; unknown not excluded.',
  },
  {
    label: 'Ranking',
    note: 'Worst composite score first. A non-held candidate that hedges an existing long position (same industry/sector) gets a small score bonus.',
  },
]

const CLOSE_RULES = [
  {
    label: 'Rating contradiction',
    note: 'Held long no longer rated Buy/Strong Buy, or held short no longer rated Sell/Strong Sell — fires regardless of MSI.',
  },
  {
    label: 'MSI reversal',
    note: `Long: MSI has reached overbought (> ${MOMENTUM_OVERBOUGHT}) — the reversion this long was entered on may be done. Short: reached oversold (< ${MOMENTUM_OVERSOLD}) — even if the rating hasn't caught up yet.`,
  },
  {
    label: 'Score near Hold boundary',
    note: `Still rated Buy/Sell, but the composite score is within ${SCORE_BOUNDARY_MARGIN} points of the Hold boundary — conviction fading before the rating itself flips.`,
  },
  {
    label: 'EPS trend reversal',
    note: 'Long: consensus EPS estimates cut. Short: consensus EPS estimates raised.',
  },
  {
    label: 'Revenue growth negative',
    note: 'Long only — trailing revenue growth has turned negative.',
  },
  {
    label: 'ST-MSI reversal',
    note: 'Long: ST-MSI turned significantly overbought (recent spike, pullback due). Short: turned significantly oversold (recent drop, bounce due).',
  },
  {
    label: 'Crowded short',
    note: `Short only — more than ${fmtPctAbs(MAX_SHORT_INTEREST)} of float is already sold short (squeeze risk).`,
  },
  {
    label: 'Earnings coming up',
    note: `Reports within ${EARNINGS_REVIEW_HOURS} business hours — either side, regardless of rating; an earnings call is a volatility event the MSI/growth/ST-MSI story doesn't speak to.`,
  },
]

// Toggle popover next to a section title, same interaction pattern as
// PeTable.jsx's Score Formula toggle (click to open, click outside to
// close) -- shared by Long/Short (their selection/ranking rules) and To
// close (the reasons a held position can be flagged) below.
function RulesInfo({
  label,
  header,
  rules,
  footer,
}: {
  label: string
  header: string
  rules: { label: string; note: string }[]
  footer?: string
}) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  useOutsideClick(wrapRef, () => setOpen(false))

  return (
    <div className="score-formula" ref={wrapRef}>
      <button
        type="button"
        className="score-formula-toggle"
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <Info size={14} />
        <span>{label}</span>
      </button>
      {open && (
        <div className="score-formula-panel">
          <div className="score-formula-header">{header}</div>
          <ul className="score-formula-list">
            {rules.map((r) => (
              <li key={r.label}>
                <span className="score-formula-body">
                  <span className="score-formula-label">{r.label}</span>
                  <span className="score-formula-note">{r.note}</span>
                </span>
              </li>
            ))}
          </ul>
          {footer && <div className="score-formula-footer">{footer}</div>}
        </div>
      )}
    </div>
  )
}

export default function RecommendationsView() {
  const [data, setData] = useState<RecommendationsData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [positions, setPositions] = useState<PositionsByTicker>({})
  const [livePrices, setLivePrices] = useState<LivePricesByTicker>({})
  const [dailyHistory3mo, setDailyHistory3mo] = useState<HistoryByTicker>({})
  const [monthlyHistory, setMonthlyHistory] = useState<HistoryByTicker>({})
  const [tickerSector, setTickerSector] = useState<Record<string, string | null>>({})
  const [tickerScreener, setTickerScreener] = useState<ScreenerByTicker>({})
  // Display-only filters, applied at render time to every section below
  // (Long, Short, both "blocked" lists, To close) via filterBySymbol/
  // filterBySector -- doesn't touch longs/shorts/rejectedStrong*/closes
  // themselves, so ranking, gates, and severity ordering are computed
  // exactly the same whether or not a filter is active; this just
  // narrows what's *shown*. selectedSectorGroups drives the same
  // SectorFilter component Screener uses (explicit instruction: one
  // shared component, not two separate implementations).
  const [symbolFilter, setSymbolFilter] = useState('')
  const [selectedSectorGroups, setSelectedSectorGroups] = useState<Set<string>>(new Set())
  // Thumb-factor filter -- explicit instruction: "a way to make filters
  // based on thumb factors... only see thumbs up in MSI, or thumbs down
  // in Revenue growth," next to the sector dropdown. Each entry is
  // `${RationaleFactor}:${'good'|'bad'}` (see THUMB_FACTORS). Applied
  // ONLY to Long/Short below (filterByThumbs), not the Portfolio/blocked
  // sections -- explicit instruction scoped this to "in long and short."
  // Selecting more than one chip is AND, not OR (same as
  // selectedSectorGroups/symbolFilter combining with each other): a row
  // must match every selected chip to stay visible.
  const [selectedThumbFilters, setSelectedThumbFilters] = useState<Set<string>>(new Set())
  // Which of the five sections below is actually mounted -- see
  // SECTION_TABS below. Only the active one's RecommendationSection (and
  // its full card grid) is rendered at all; the other four are skipped
  // entirely rather than just hidden with CSS, so switching sections
  // actually shrinks the mounted DOM instead of all five sections' cards
  // (Long/Short/both blocked pools/To close, each potentially 50-150+
  // cards) sitting in the tree simultaneously -- confirmed slow to mount
  // as one long page. The five lists themselves (longs/shorts/closes/
  // rejectedStrongBuy/rejectedStrongSell) are still all computed
  // regardless of which tab is active, since the stat-row/tab-bar counts
  // below need every count up front -- this only cuts the per-card
  // render/mount cost, not the list-building one.
  const [activeSection, setActiveSection] = useState<'toClose' | 'long' | 'short' | 'longBlocked' | 'shortBlocked'>('toClose')
  // Live instant for the earnings-within-EARNINGS_REVIEW_HOURS close-review
  // check below -- same ticking clock PeTable.jsx/Asset.jsx already use for
  // earningsUrgencyClass, so a position due to report doesn't need a full
  // data refresh for that flag to age in as the actual moment approaches.
  const now = useNowTick()

  useEffect(() => {
    const source = new EventSource(IB_STREAM_URL)
    source.onmessage = (e) => {
      const { prices, positions: pos } = JSON.parse(e.data)
      setLivePrices(prices || {})
      setPositions(pos || {})
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
  // visible somewhere.
  useEffect(() => {
    fetch('/sorted_screen.csv')
      .then((r) => (r.ok ? r.text() : ''))
      .then((text) => {
        const parsedRows = parseCSV(text)
        // sorted_screen.csv's own `rating` column comes from a row's
        // INDEX in this file's order (main.py's write_sorted_screen_csv:
        // rating_for_percentile(i / n), scored rows first in ascending-
        // score order, unranked/NA rows appended after) -- NOT from the
        // `score` column's own value. score is a weighted average of ~19
        // independent-ish factor percentile ranks, so its distribution
        // clusters tightly around the middle rather than being uniform;
        // `score * 100` (the original version of this) silently
        // mislabeled that clustered value as a percentile -- confirmed
        // wrong in practice: SSRM, genuinely Buy-rated (true rank
        // percentile 14.9, correctly inside the 5-20 Buy band), was
        // showing scorePercentile 46.9 under the old formula, nowhere
        // near that band. Recomputed here the same way
        // recommendations.py now does for its own candidates.
        const scoredCount = parsedRows.filter((row: any) => row.score).length
        const sectors: Record<string, string | null> = {}
        const screener: ScreenerByTicker = {}
        let scoredIndex = 0
        for (const row of parsedRows) {
          sectors[row.ticker] = row.sector || null
          const hasScore = Boolean(row.score)
          const score = hasScore ? Number(row.score) : null
          const scorePercentile = hasScore && scoredCount ? Math.round((scoredIndex / scoredCount) * 1000) / 10 : null
          if (hasScore) scoredIndex++
          screener[row.ticker] = {
            ticker: row.ticker,
            name: row.name || null,
            rating: row.rating || null,
            score,
            scorePercentile,
            momentum: row.momentum ? Number(row.momentum) : null,
            sector: row.sector || null,
            price: row.price ? Number(row.price) : null,
            beta: row.beta ? Number(row.beta) : null,
            shortPercentOfFloat: row.shortPercentOfFloat ? Number(row.shortPercentOfFloat) : null,
            revenueGrowth: row.revenueGrowth ? Number(row.revenueGrowth) : null,
            heldPercentInsiders: row.heldPercentInsiders ? Number(row.heldPercentInsiders) : null,
            epsRevision0y: row.epsRevision0y ? Number(row.epsRevision0y) : null,
            epsRevision1y: row.epsRevision1y ? Number(row.epsRevision1y) : null,
            meanReversion: row.meanReversion ? Number(row.meanReversion) : null,
            earningsTimestampStart: row.earningsTimestampStart ? Number(row.earningsTimestampStart) : null,
          }
        }
        setTickerSector(sectors)
        setTickerScreener(screener)
      })
      .catch(() => {})
  }, [])

  // Side-specific -- a short position must never count as "held" toward the
  // Long section (and vice versa): a ticker that's both a held short AND
  // Buy-rated is a contradiction the To close group flags below, not a
  // reason to highlight it as "In portfolio" on the Long grid. heldCount
  // (all nonzero positions, either side) is only for the masthead's stat.
  const heldLongTickers = useMemo(
    () => new Set(Object.entries(positions).filter(([, p]) => (p?.shares ?? 0) > 0).map(([t]) => t)),
    [positions]
  )
  const heldShortTickers = useMemo(
    () => new Set(Object.entries(positions).filter(([, p]) => (p?.shares ?? 0) < 0).map(([t]) => t)),
    [positions]
  )
  const heldCount = useMemo(() => Object.values(positions).filter((p) => p?.shares).length, [positions])

  // A non-held candidate that hedges an existing OPPOSITE-side position
  // (same industry or sector -- see buildOppositeMatcher) is nudged ahead of
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

  const longs: RankedCandidate[] = useMemo(() => {
    if (!data) return []
    const matcher = buildOppositeMatcher([...heldShortTickers], tickerSector)
    const pool = data.candidates
      .filter(
        (c) =>
          BUY_RATINGS.has(c.rating as string) &&
          eligibleToBuy(c) &&
          sufficientGrowthForLong(tickerScreener[c.ticker]?.revenueGrowth) &&
          meanReversionOkForLong(tickerScreener[c.ticker]?.meanReversion) &&
          epsTrendOkForLong(epsTrendValue(tickerScreener[c.ticker]))
      )
      .map((c) => {
        const held = heldLongTickers.has(c.ticker)
        const match = held ? null : matcher(c)
        const sortScore = (c.score ?? 1) - (match ? HEDGE_BONUS : 0)
        return {
          ...c,
          meanReversion: tickerScreener[c.ticker]?.meanReversion,
          epsRevision0y: tickerScreener[c.ticker]?.epsRevision0y,
          epsRevision1y: tickerScreener[c.ticker]?.epsRevision1y,
          revenueGrowth: tickerScreener[c.ticker]?.revenueGrowth,
          heldPercentInsiders: tickerScreener[c.ticker]?.heldPercentInsiders,
          oppositeMatchLine: oppositeMatchLine(match, 'short'),
          oppositeMatchType: match?.type ?? null,
          _sortScore: sortScore,
        }
      })
    return pool.sort((a, b) => a._sortScore - b._sortScore)
  }, [data, heldShortTickers, heldLongTickers, tickerSector, tickerScreener])

  const shorts: RankedCandidate[] = useMemo(() => {
    if (!data) return []
    const matcher = buildOppositeMatcher([...heldLongTickers], tickerSector)
    const pool = data.candidates
      .filter(
        (c) =>
          SELL_RATINGS.has(c.rating as string) &&
          eligibleToSell(c) &&
          notCrowded(c) &&
          notTooMuchGrowthForShort(tickerScreener[c.ticker]?.revenueGrowth) &&
          meanReversionOkForShort(tickerScreener[c.ticker]?.meanReversion) &&
          epsTrendOkForShort(epsTrendValue(tickerScreener[c.ticker]))
      )
      .map((c) => {
        const held = heldShortTickers.has(c.ticker)
        const match = held ? null : matcher(c)
        const sortScore = (c.score ?? 0) + (match ? HEDGE_BONUS : 0)
        return {
          ...c,
          meanReversion: tickerScreener[c.ticker]?.meanReversion,
          epsRevision0y: tickerScreener[c.ticker]?.epsRevision0y,
          epsRevision1y: tickerScreener[c.ticker]?.epsRevision1y,
          revenueGrowth: tickerScreener[c.ticker]?.revenueGrowth,
          heldPercentInsiders: tickerScreener[c.ticker]?.heldPercentInsiders,
          oppositeMatchLine: oppositeMatchLine(match, 'long'),
          oppositeMatchType: match?.type ?? null,
          _sortScore: sortScore,
        }
      })
    return pool.sort((a, b) => b._sortScore - a._sortScore)
  }, [data, heldLongTickers, heldShortTickers, tickerSector, tickerScreener])

  // Held positions whose own rating now contradicts the side they're held
  // on -- a long position that's drifted to Hold/Sell/Strong Sell, or a
  // short position that's drifted to Hold/Buy/Strong Buy -- fire
  // regardless of momentum (unlike Long/Short's eligibleToBuy/
  // eligibleToSell idea-list gate above): the rating itself is decisive
  // for an existing position, even before momentum has caught up. Like
  // Long/Short, uncapped -- this is a flag on the actual portfolio, not a
  // ranked idea list, so every contradiction shows, however many there
  // are. Sorted by how far the rating leans the
  // "wrong" way for that side, not by side, so the most urgent flags
  // surface first regardless of direction.
  // Built once and shared by closes/overboughtPositions/oversoldPositions/
  // stayPositions below -- all four used to separately rebuild this same
  // 675-candidate Map AND re-merge every held position from scratch,
  // 4x redundant work on every data/positions/tickerScreener change
  // (confirmed as the actual cause of a slow Portfolio-tab mount after
  // Overbought/Oversold/Stay were added). One pass here instead.
  const byTicker = useMemo(() => new Map((data?.candidates ?? []).map((c) => [c.ticker, c])), [data])

  // tickerScreener (sorted_screen.csv) is the baseline -- it covers
  // every rated ticker, refreshed on every screen run -- with the
  // candidate's richer news/insiders/13F/analyst fields (news7d,
  // insiders90d, instChangeQoQ, targetUpside, recommendationMean,
  // numberOfAnalystOpinions -- fields tickerScreener never carries at
  // all) layered UNDER it, not over it: recommendations.json is only
  // rebuilt on its own separate cadence (`python main.py
  // recommendations`), so a ticker whose rating/momentum/score has
  // since moved in a fresher sorted_screen.csv can still have a stale
  // candidate entry sitting around with its OLD values for those same
  // fields -- confirmed in practice with FRPH: held short, drifted
  // from Sell to Hold in sorted_screen.csv, but recommendations.json
  // still had a same-ticker candidate from before that drift with
  // rating "Sell" -- candidate-wins spread order let that stale
  // "Sell" silently beat the fresh "Hold", so the rating-contradiction
  // check below never fired and the position vanished from this list
  // entirely instead of showing the Close flag it should have. Screener
  // fields applied SECOND (spread order matters -- later keys win) so
  // they always override a same-named but stale candidate field, while
  // candidate-only fields still come through untouched since
  // tickerScreener never defines those keys to begin with. Beta is the
  // one candidate-side field this flips back to needing a null check
  // for (tickerScreener doesn't carry it) -- see the high-beta-style
  // checks below, which already treat a missing beta as "doesn't
  // trip", not "worst".
  const heldMerged = useMemo(() => {
    const rows: { ticker: string; shares: number; c: Candidate }[] = []
    for (const [ticker, p] of Object.entries(positions)) {
      if (!p?.shares) continue
      rows.push({ ticker, shares: p.shares, c: { ...byTicker.get(ticker), ...tickerScreener[ticker], ticker } })
    }
    return rows
  }, [positions, tickerScreener, byTicker])

  const closes: CloseRow[] = useMemo(() => {
    const rows: CloseRow[] = []
    for (const { shares, c } of heldMerged) {
      const reasons = buildCloseReasons({ shares, c, now })
      if (reasons.length === 0) continue
      const hasRatingReason = reasons.some((r) => r.type === 'rating')
      const hasMomentumReason = reasons.some((r) => r.type === 'momentum')
      const hasScoreBoundaryReason = reasons.some((r) => r.type === 'score-boundary')
      // Rating contradictions rank highest (most decisive single signal),
      // then unsupportive momentum, then a score close enough to the Hold
      // boundary to likely cross soon, then however many pure risk/
      // fundamentals flags (eps-trend/revenue-growth/crowded-short) apply
      // -- so a position tripping several flags at once still outranks one
      // tripping only a single, milder one.
      const severity =
        (hasRatingReason ? 100 : 0) +
        (hasMomentumReason ? 50 : 0) +
        (hasScoreBoundaryReason ? 30 : 0) +
        reasons.filter((r) => !['rating', 'momentum', 'score-boundary'].includes(r.type)).length * 10
      rows.push({ ...c, closeSide: shares > 0 ? 'Long' : 'Short', shares, reasons, hasRatingReason, _severity: severity })
    }
    return rows.sort((a, b) => b._severity - a._severity)
  }, [heldMerged, now])

  // Every held position whose daily MSI is currently overbought/oversold
  // -- explicit instruction: a Portfolio-wide momentum-extremes read,
  // independent of (and can overlap with) the close-reason flags above.
  // Not conditioned on side the way eligibleToBuy/Sell's BLOCK is --
  // "overbought" here just means the raw MSI reading itself is past
  // MOMENTUM_OVERBOUGHT, regardless of whether that's good or bad news
  // for whichever side actually holds it (a held Short sitting
  // overbought is a GOOD sign for that position, a held Long sitting
  // overbought is a warning -- rationaleLines' own thumb icon still
  // shows that distinction on the card, this list just surfaces "worth
  // a look" candidates on shape alone). reasons/hasRatingReason/
  // _severity are CloseRow-shaped (so these can reuse CloseCard) but
  // carry no actual close-worthiness here -- always "Review", never
  // "Close".
  const overboughtPositions: CloseRow[] = useMemo(() => {
    const rows: CloseRow[] = []
    for (const { shares, c } of heldMerged) {
      if (c.momentum === null || c.momentum === undefined || c.momentum <= MOMENTUM_OVERBOUGHT) continue
      rows.push({
        ...c,
        closeSide: shares > 0 ? 'Long' : 'Short',
        shares,
        reasons: [{ type: 'momentum', text: `MSI ${c.momentum.toFixed(0)} — overbought.` }],
        hasRatingReason: false,
        _severity: c.momentum,
      })
    }
    return rows.sort((a, b) => b._severity - a._severity)
  }, [heldMerged])

  const oversoldPositions: CloseRow[] = useMemo(() => {
    const rows: CloseRow[] = []
    for (const { shares, c } of heldMerged) {
      if (c.momentum === null || c.momentum === undefined || c.momentum >= MOMENTUM_OVERSOLD) continue
      rows.push({
        ...c,
        closeSide: shares > 0 ? 'Long' : 'Short',
        shares,
        reasons: [{ type: 'momentum', text: `MSI ${c.momentum.toFixed(0)} — oversold.` }],
        hasRatingReason: false,
        _severity: -c.momentum,
      })
    }
    return rows.sort((a, b) => b._severity - a._severity)
  }, [heldMerged])

  // Every held position NOT already in one of the three lists above --
  // explicit instruction: the "nothing to see here" bucket, so every
  // held position accounted for across the four Portfolio containers,
  // no ticker silently missing from all of them. Alphabetical (this is
  // a checklist, not a ranked idea list, same reasoning rejectedStrong's
  // own comment gives).
  const stayPositions: CloseRow[] = useMemo(() => {
    const flagged = new Set([...closes, ...overboughtPositions, ...oversoldPositions].map((c) => c.ticker))
    const rows: CloseRow[] = []
    for (const { ticker, shares, c } of heldMerged) {
      if (flagged.has(ticker)) continue
      rows.push({ ...c, closeSide: shares > 0 ? 'Long' : 'Short', shares, reasons: [], hasRatingReason: false, _severity: 0 })
    }
    return rows.sort((a, b) => a.ticker.localeCompare(b.ticker))
  }, [heldMerged, closes, overboughtPositions, oversoldPositions])

  // Strong Buy/Strong Sell candidates -- the top-conviction rating on
  // either end -- that still didn't clear a Long/Short opening gate.
  // Uncapped, same as Long/Short (this is an audit of every top-rated
  // candidate that got blocked, not a ranked idea list), and independent
  // of longs/shorts above other than sharing the same gate functions -- see
  // buildRejectionReasons. Alphabetical by ticker (explicit instruction --
  // this is a lookup list, not a ranked one, so sorting by severity the
  // way closes does would just make a specific ticker harder to find).
  // Split into two so each can sit right after its own side's idea list
  // (Strong Buy after Long, Strong Sell after Short) rather than one
  // combined section -- explicit instruction.
  const rejectedStrong: RejectedRow[] = useMemo(() => {
    if (!data) return []
    const rows: RejectedRow[] = []
    for (const raw of data.candidates) {
      // Same tickerScreener-over-stale-candidate merge as heldMerged
      // above (see that memo's own comment for the FRPH precedent this
      // fixes) -- without it, eligibleToBuy/eligibleToSell inside
      // buildRejectionReasons, and the rationaleLines/ThumbIcon block
      // this card renders, were reading recommendations.json's own
      // possibly-stale momentum/meanReversion/rating instead of
      // sorted_screen.csv's current one, which could show a card that
      // LOOKS like it should pass (or already have a mismatched thumb
      // icon) relative to what's actually on screen elsewhere.
      const c: Candidate = { ...raw, ...tickerScreener[raw.ticker], ticker: raw.ticker }
      if (c.rating !== 'Strong Buy' && c.rating !== 'Strong Sell') continue
      const reasons = buildRejectionReasons({ c, tickerScreener })
      if (reasons.length === 0) continue
      rows.push({ ...c, reasons })
    }
    return rows.sort((a, b) => a.ticker.localeCompare(b.ticker))
  }, [data, tickerScreener])
  const rejectedStrongBuy = useMemo(() => rejectedStrong.filter((c) => c.rating === 'Strong Buy'), [rejectedStrong])
  const rejectedStrongSell = useMemo(() => rejectedStrong.filter((c) => c.rating === 'Strong Sell'), [rejectedStrong])

  // Case-insensitive substring match, not exact-ticker-only -- "PG" also
  // surfaces PGY, matching PeTable.jsx's own search box convention.
  const symbolFilterQuery = symbolFilter.trim().toUpperCase()
  function filterBySymbol<T extends { ticker: string }>(rows: T[]): T[] {
    return symbolFilterQuery ? rows.filter((c) => c.ticker.toUpperCase().includes(symbolFilterQuery)) : rows
  }

  // c.sector here is the granular industry (see CardFooter's own comment
  // on that naming) -- getSectorGroup maps it up to the same broad
  // GICS-style group SectorFilter's own item list is built from.
  function filterBySector<T extends { sector?: string | null }>(rows: T[]): T[] {
    return selectedSectorGroups.size ? rows.filter((c) => selectedSectorGroups.has(getSectorGroup(c.sector))) : rows
  }

  // Explicit instruction: filter EVERY section by thumb-icon factors
  // (e.g. "only thumbs up in MSI"), not just Long/Short -- MSI/ST-MSI/
  // EPS-trend "down" can never appear inside Long/Short at all (the
  // entry gate excludes exactly the value range that would trigger
  // their own 'bad' signal -- e.g. eligibleToBuy already excludes
  // momentum > MOMENTUM_OVERBOUGHT, the same condition momentumSignal
  // calls 'bad' for a Long), so a filter scoped to only those two tabs
  // could never show those combinations no matter how many gate-failing
  // candidates actually have them -- confirmed against AII/MFIN/CPAY/ACR,
  // all four living in Portfolio/blocked sections, not Long/Short.
  // `side` is either a fixed side (Long/Short's own single-side lists)
  // or a per-row resolver (Portfolio's closeSide-carrying rows, which
  // mix both sides in one list). Recomputes rationaleLines per row --
  // same cost the card itself already pays to render its own rationale
  // list, so no new expense, just reused for a second purpose. A row
  // must match every selected chip (AND, see selectedThumbFilters' own
  // comment); a factor whose line doesn't fire at all for a given
  // candidate (e.g. no News in the last 7 days) never matches an
  // up/down chip for it either, same as the card itself showing no icon
  // there.
  function filterByThumbs<T extends Candidate & { oppositeMatchLine?: string | null }>(
    rows: T[],
    side: 'Long' | 'Short' | ((row: T) => 'Long' | 'Short')
  ): T[] {
    if (!selectedThumbFilters.size) return rows
    const selectedItems = THUMB_FILTER_ITEMS.filter((item) => selectedThumbFilters.has(item.name))
    return rows.filter((c) => {
      const lines = rationaleLines(c, typeof side === 'function' ? side(c) : side)
      return selectedItems.every((item) => lines.some((l) => l.factor === item.factor && l.signal === item.signal))
    })
  }

  // Item counts for the thumb-filter dropdown below -- over every
  // section's rows as they stand before the thumb filter itself narrows
  // them (so picking a second chip shows how many the FIRST one alone
  // left, not a count that's already collapsed to whatever's currently
  // selected), same scope as filterByThumbs' own "everywhere" reach.
  const thumbFilterItems: [string, number][] = useMemo(() => {
    const counts = new Map<string, number>()
    const tally = (c: Candidate & { oppositeMatchLine?: string | null }, side: 'Long' | 'Short') => {
      for (const line of rationaleLines(c, side)) {
        if (!line.factor) continue
        const key = `${line.factor}:${line.signal}`
        counts.set(key, (counts.get(key) ?? 0) + 1)
      }
    }
    for (const c of longs) tally(c, 'Long')
    for (const c of shorts) tally(c, 'Short')
    for (const c of rejectedStrongBuy) tally(c, 'Long')
    for (const c of rejectedStrongSell) tally(c, 'Short')
    for (const c of closes) tally(c, c.closeSide)
    for (const c of overboughtPositions) tally(c, c.closeSide)
    for (const c of oversoldPositions) tally(c, c.closeSide)
    for (const c of stayPositions) tally(c, c.closeSide)
    return THUMB_FILTER_ITEMS.map((item) => [item.name, counts.get(`${item.factor}:${item.signal}`) ?? 0])
  }, [longs, shorts, rejectedStrongBuy, rejectedStrongSell, closes, overboughtPositions, oversoldPositions, stayPositions])

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
          <div className="stat">
            <span className="n num">{rejectedStrong.length}</span>
            <span className="l">Strong-rated, blocked</span>
          </div>
        </div>
      </header>

      <div className="controls">
        <div className="search-box">
          <Search />
          <input
            type="text"
            placeholder="Filter by symbol…"
            value={symbolFilter}
            onChange={(e) => setSymbolFilter(e.target.value)}
          />
        </div>
        {data && (
          <SectorFilter
            industries={data.candidates.map((c) => c.sector)}
            selected={selectedSectorGroups}
            onChange={setSelectedSectorGroups}
          />
        )}
        <FilterDropdown
          noun="thumb filter"
          plural="thumb filters"
          items={thumbFilterItems}
          selected={selectedThumbFilters}
          onToggle={(name) =>
            setSelectedThumbFilters((prev) => {
              const next = new Set(prev)
              if (next.has(name)) next.delete(name)
              else next.add(name)
              return next
            })
          }
          onClear={() => setSelectedThumbFilters(new Set())}
        />
      </div>

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
          <div className="recommendation-tabs">
            <button
              type="button"
              className={`recommendation-tab-btn${activeSection === 'toClose' ? ' active' : ''}`}
              onClick={() => setActiveSection('toClose')}
            >
              Portfolio <span className="recommendation-tab-count">{closes.length}</span>
            </button>
            <button
              type="button"
              className={`recommendation-tab-btn${activeSection === 'long' ? ' active' : ''}`}
              onClick={() => setActiveSection('long')}
            >
              Long <span className="recommendation-tab-count">{longs.length}</span>
            </button>
            <button
              type="button"
              className={`recommendation-tab-btn${activeSection === 'short' ? ' active' : ''}`}
              onClick={() => setActiveSection('short')}
            >
              Short <span className="recommendation-tab-count">{shorts.length}</span>
            </button>
            <button
              type="button"
              className={`recommendation-tab-btn${activeSection === 'longBlocked' ? ' active' : ''}`}
              onClick={() => setActiveSection('longBlocked')}
            >
              Long blocked <span className="recommendation-tab-count">{rejectedStrongBuy.length}</span>
            </button>
            <button
              type="button"
              className={`recommendation-tab-btn${activeSection === 'shortBlocked' ? ' active' : ''}`}
              onClick={() => setActiveSection('shortBlocked')}
            >
              Short blocked <span className="recommendation-tab-count">{rejectedStrongSell.length}</span>
            </button>
          </div>

          {activeSection === 'toClose' && (
          <>
          <RecommendationSection
            title="To close"
            titleInfo={
              <RulesInfo
                label="Closing rules"
                header="Any one of these flags a held position for review"
                rules={CLOSE_RULES}
                footer="A position can trip more than one of these at once — the card below lists every reason that applies, ranked with rating contradictions first, then MSI, then the rest."
              />
            }
            subtitle="Held positions tripping a rating/MSI/score-boundary contradiction, a fundamentals reversal, or a crowded-short flag"
            rows={filterByThumbs(filterBySector(filterBySymbol(closes)), (c) => c.closeSide)}
            renderCard={(c) => (
              <CloseCard
                key={c.ticker}
                c={c}
                live={livePrices[c.ticker]}
                dailyHistory3mo={dailyHistory3mo}
                monthlyHistory={monthlyHistory}
              />
            )}
            emptyMessage="No held position currently has a rating/MSI contradiction or a risk flag."
          />
          <RecommendationSection
            title="Overbought"
            subtitle={`Held positions with a daily MSI above ${MOMENTUM_OVERBOUGHT} — not itself a close signal, just a shape worth a look (a held Short sitting here is a good sign, a held Long is a warning)`}
            rows={filterByThumbs(filterBySector(filterBySymbol(overboughtPositions)), (c) => c.closeSide)}
            renderCard={(c) => (
              <CloseCard
                key={c.ticker}
                c={c}
                live={livePrices[c.ticker]}
                dailyHistory3mo={dailyHistory3mo}
                monthlyHistory={monthlyHistory}
                goodSign={c.closeSide === 'Short'}
                badSign={c.closeSide === 'Long'}
              />
            )}
            emptyMessage="No held position currently has an overbought daily MSI."
          />
          <RecommendationSection
            title="Oversold"
            subtitle={`Held positions with a daily MSI below ${MOMENTUM_OVERSOLD} — not itself a close signal, just a shape worth a look (a held Long sitting here is a good sign, a held Short is a warning)`}
            rows={filterByThumbs(filterBySector(filterBySymbol(oversoldPositions)), (c) => c.closeSide)}
            renderCard={(c) => (
              <CloseCard
                key={c.ticker}
                c={c}
                live={livePrices[c.ticker]}
                dailyHistory3mo={dailyHistory3mo}
                monthlyHistory={monthlyHistory}
                goodSign={c.closeSide === 'Long'}
                badSign={c.closeSide === 'Short'}
              />
            )}
            emptyMessage="No held position currently has an oversold daily MSI."
          />
          <RecommendationSection
            title="Stay"
            subtitle="Held positions not flagged To close, Overbought, or Oversold — nothing here needs attention right now"
            rows={filterByThumbs(filterBySector(filterBySymbol(stayPositions)), (c) => c.closeSide)}
            renderCard={(c) => (
              <CloseCard
                key={c.ticker}
                c={c}
                live={livePrices[c.ticker]}
                dailyHistory3mo={dailyHistory3mo}
                monthlyHistory={monthlyHistory}
              />
            )}
            emptyMessage="Every held position is flagged in one of the other three containers."
          />
          </>
          )}
          {activeSection === 'long' && (
          <RecommendationSection
            title="Long"
            titleInfo={<RulesInfo label="Selection rules" header="Every candidate must clear all of these" rules={LONG_RULES} />}
            subtitle={`Strong Buy / Buy with MSI not overbought (≤ ${MOMENTUM_OVERBOUGHT}) and revenue growth ≥ ${fmtPctAbs(REVENUE_GROWTH_THRESHOLD)}, best composite score first`}
            rows={filterByThumbs(filterBySector(filterBySymbol(longs)), 'Long')}
            renderCard={(c) => (
              <RecommendationCard
                key={c.ticker}
                c={c}
                held={heldLongTickers.has(c.ticker)}
                live={livePrices[c.ticker]}
                dailyHistory3mo={dailyHistory3mo}
                monthlyHistory={monthlyHistory}
                side="Long"
              />
            )}
            emptyMessage="No Strong Buy/Buy candidates clearing the MSI/growth/ST-MSI/EPS gates right now."
          />
          )}
          {activeSection === 'longBlocked' && (
          <RecommendationSection
            title="Strong Buy — blocked"
            subtitle="Strong Buy candidates that still failed an MSI, revenue-growth, or ST-MSI gate — see Long's Selection rules for what each gate checks"
            rows={filterByThumbs(filterBySector(filterBySymbol(rejectedStrongBuy)), 'Long')}
            renderCard={(c) => (
              <RejectedCard
                key={c.ticker}
                c={c}
                held={heldLongTickers.has(c.ticker)}
                live={livePrices[c.ticker]}
                dailyHistory3mo={dailyHistory3mo}
                monthlyHistory={monthlyHistory}
                side="Long"
              />
            )}
            emptyMessage="Every current Strong Buy candidate clears the Long opening gates."
          />
          )}
          {activeSection === 'short' && (
          <RecommendationSection
            title="Short"
            titleInfo={<RulesInfo label="Selection rules" header="Every candidate must clear all of these" rules={SHORT_RULES} />}
            subtitle={`Strong Sell / Sell with MSI not oversold (≥ ${MOMENTUM_OVERSOLD}) and revenue growth ≤ ${fmtPctAbs(REVENUE_GROWTH_THRESHOLD)}, worst composite score first — excludes crowded shorts (>${fmtPctAbs(MAX_SHORT_INTEREST)} of float already short)`}
            rows={filterByThumbs(filterBySector(filterBySymbol(shorts)), 'Short')}
            renderCard={(c) => (
              <RecommendationCard
                key={c.ticker}
                c={c}
                held={heldShortTickers.has(c.ticker)}
                live={livePrices[c.ticker]}
                dailyHistory3mo={dailyHistory3mo}
                monthlyHistory={monthlyHistory}
                side="Short"
              />
            )}
            emptyMessage="No Sell/Strong Sell candidates clearing the MSI/growth/ST-MSI/EPS gates right now."
          />
          )}
          {activeSection === 'shortBlocked' && (
          <RecommendationSection
            title="Strong Sell — blocked"
            subtitle="Strong Sell candidates that still failed an MSI, revenue-growth, ST-MSI, or crowded-short gate — see Short's Selection rules for what each gate checks"
            rows={filterByThumbs(filterBySector(filterBySymbol(rejectedStrongSell)), 'Short')}
            renderCard={(c) => (
              <RejectedCard
                key={c.ticker}
                c={c}
                held={heldShortTickers.has(c.ticker)}
                live={livePrices[c.ticker]}
                dailyHistory3mo={dailyHistory3mo}
                monthlyHistory={monthlyHistory}
                side="Short"
              />
            )}
            emptyMessage="Every current Strong Sell candidate clears the Short opening gates."
          />
          )}
        </>
      )}
    </div>
  )
}
