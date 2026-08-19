import { useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react'
import { AlertTriangle, Flag, Info, Search, ThumbsUp } from 'lucide-react'
import { parseCSV } from '../csv'
import { businessMillisBetween, fmtEarningsDate, useNowTick } from '../earnings'
import { IB_STREAM_URL } from '../ibStream'
import { fmtPct, fmtPrice, fmtSigned, ratingClass } from '../screenerFactors'
import RecommendationsChatbot from '../components/RecommendationsChatbot'
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

// Bottom-right corner badge on every card (RecommendationCard/CloseCard/
// RejectedCard) -- the screener percentile used to only show up buried as
// the first line of the rationale bullet list (RecommendationCard/
// CloseCard only, not RejectedCard at all, which has no rationale list),
// so it took reading past the header to see where a ticker actually sits
// in sorted_screen.csv. title carries the same full "Top/Bottom X% of the
// ranked universe" sentence percentileLabel already writes; the badge
// itself just shows the bare number, compact enough for a corner.
function PercentileBadge({ c }: { c: Candidate }) {
  if (c.scorePercentile === null || c.scorePercentile === undefined) return null
  const label = percentileLabel(c)
  return (
    <div className="recommendation-card-footer">
      <span className="recommendation-percentile-badge" title={label ?? undefined}>
        {c.scorePercentile.toFixed(1)}%
      </span>
    </div>
  )
}

// momentum is IBApp's Sharpe-style regression-slope score (trend divided by
// its own volatility -- see fmtSigned's own comment in screenerFactors.js),
// not a return -- shown as the signed raw number, not a percentage. Every
// card that reaches the page has already passed the hard momentum-direction
// rule (see RecommendationsView's eligible() below), so this line is
// showing the reader the gate it already cleared, not a new judgment call.
function momentumLine(c: Candidate): string | null {
  if (c.momentum === null || c.momentum === undefined) return null
  return `Momentum ${fmtSigned(c.momentum)} (${c.momentum > 0 ? 'positive' : 'negative'})`
}

// Shown right next to momentum -- explicit instruction, always alongside
// it rather than only when the mean-reversion gate/close-reason actually
// fires (see meanReversionOkForLong/meanReversionOkForShort and
// buildCloseReasons' own mean-reversion check), so the reader can see the
// reading even on a card where it's nowhere near the ±MEAN_REVERSION_THRESHOLD
// significance bar. Same "only computed for CANDLESTICK_TOP_N ranked/held
// tickers" gap as those checks -- omitted, not shown as 0, when absent.
function meanReversionLine(c: Candidate): string | null {
  if (c.meanReversion === null || c.meanReversion === undefined) return null
  return `Hourly momentum ${fmtSigned(c.meanReversion)} (${c.meanReversion > 0 ? 'positive' : 'negative'})`
}

// Same "always shown, not just when the gate/reason fires" treatment as
// meanReversionLine above -- explicit instruction, EPS revision trend
// gates entry now too (see epsTrendOkForLong/epsTrendOkForShort), so a
// reader should see the reading on every card. Reuses epsTrendValue
// (below) rather than a separate field -- see that function's own
// comment for the epsRevision0y/1y averaging it does.
function epsTrendLine(c: Candidate): string | null {
  const epsTrend = epsTrendValue(c)
  if (epsTrend === null) return null
  return `EPS est trend ${fmtPct(epsTrend)} (${epsTrend > 0 ? 'raised' : epsTrend < 0 ? 'cut' : 'flat'})`
}

// effectiveShortPctOfFloat (defined below, alongside notCrowded) prefers
// FINRA's fresher biweekly figure over yfinance's stale month-end one --
// shown on every card with data, not just when it crosses MAX_SHORT_INTEREST,
// since it's a contrarian scoring input either way (see short_interest_rank),
// not only a risk flag on the Short side.
function shortInterestLine(c: Candidate): string | null {
  const pct = effectiveShortPctOfFloat(c)
  if (pct === null || pct === undefined) return null
  return `Short interest ${fmtPctAbs(pct)} of float`
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
function buildOppositeMatcher(
  oppositeTickers: string[],
  tickerSector: Record<string, string | null>,
  tickerThemes: Record<string, string[]>
): (c: Candidate) => OppositeMatch | null {
  const bySector = new Map<string, string[]>()
  const byTheme = new Map<string, string[]>()
  for (const t of oppositeTickers) {
    const sector = tickerSector[t]
    if (sector) {
      if (!bySector.has(sector)) bySector.set(sector, [])
      ;(bySector.get(sector) as string[]).push(t)
    }
    for (const theme of tickerThemes[t] || []) {
      if (!byTheme.has(theme)) byTheme.set(theme, [])
      ;(byTheme.get(theme) as string[]).push(t)
    }
  }
  return function match(c: Candidate): OppositeMatch | null {
    if (c.sector && bySector.has(c.sector)) {
      return { type: 'sector', value: c.sector, tickers: bySector.get(c.sector) as string[] }
    }
    for (const theme of tickerThemes[c.ticker] || []) {
      if (byTheme.has(theme)) {
        return { type: 'theme', value: theme, tickers: byTheme.get(theme) as string[] }
      }
    }
    return null
  }
}

function oppositeMatchLine(match: OppositeMatch | null, oppositeSideLabel: string, themeLabels: Record<string, string>): string | null {
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
function rationaleLines(c: Candidate & { oppositeMatchLine?: string | null }): string[] {
  const lines: string[] = []
  // percentileLabel is shown as its own PercentileBadge in the card's
  // bottom-right corner instead of buried as a bullet here -- see that
  // component's own comment.

  const momentum = momentumLine(c)
  if (momentum) lines.push(momentum)

  const meanReversion = meanReversionLine(c)
  if (meanReversion) lines.push(meanReversion)

  const epsTrend = epsTrendLine(c)
  if (epsTrend) lines.push(epsTrend)

  const shortInterest = shortInterestLine(c)
  if (shortInterest) lines.push(shortInterest)

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
      `${insiders.buys} insider buy${insiders.buys === 1 ? '' : 's'}, ${insiders.sells} sell${insiders.sells === 1 ? '' : 's'} in the last 90 days`
    )
  }

  if (c.instChangeQoQ !== null && c.instChangeQoQ !== undefined) {
    lines.push(
      `Institutions ${c.instChangeQoQ >= 0 ? 'added' : 'trimmed'} ${fmtPctAbs(c.instChangeQoQ)} (13F)`
    )
  }

  if (c.targetUpside !== null && c.targetUpside !== undefined) {
    const analysts = c.numberOfAnalystOpinions ? Math.round(c.numberOfAnalystOpinions) : null
    lines.push(`Target upside ${fmtPct(c.targetUpside)}${analysts ? ` (${analysts} analysts)` : ''}`)
  }

  return lines
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

      <ul className="recommendation-rationale">
        {rationaleLines(c).map((line, i) => (
          <li key={i}>{line}</li>
        ))}
      </ul>
      <PercentileBadge c={c} />
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
}: {
  c: CloseRow
  live?: LiveTick
  dailyHistory3mo: HistoryByTicker
  monthlyHistory: HistoryByTicker
}) {
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

      <ul className="recommendation-rationale">
        {rationaleLines(c).map((line, i) => (
          <li key={i}>{line}</li>
        ))}
      </ul>
      <PercentileBadge c={c} />
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
      <PercentileBadge c={c} />
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
// before ranking, per explicit instruction: positive daily-timeframe (LT)
// momentum is a good entry signal regardless of side -- for a Long it
// means the stock is trending up; for a Short it means the stock is
// rallying hard enough to be a good "short the overextension" candidate,
// not that it's already falling. So both eligibleToBuy and eligibleToSell
// require momentum at or above MOMENTUM_THRESHOLD; there is no separate,
// lower bar for shorts the way there used to be. The ST (hourly)
// mean-reversion gate right below is untouched by this and stays
// direction-specific -- it's an entry-timing check (don't chase a move
// that already happened), not a second momentum vote. A candidate with
// unknown momentum is excluded rather than assumed compliant, since the
// rule is absolute.
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
// MOMENTUM_THRESHOLD: explicit instruction -- a bare positive sign isn't
// enough conviction to recommend opening a position (long or short), only
// to keep holding one already open (see buildCloseReasons' own separate,
// looser <=0 momentum-reversal check below, deliberately not raised to
// this same bar -- that one flags an existing position early, before its
// momentum has even fully reversed, not just before it's "strong").
// Originally 1, lowered to 0.5 per explicit follow-up instruction (still
// meaningfully above a bare positive sign, just a lower bar for entry
// conviction than before). Shorts now use the exact same bar as longs --
// explicit instruction: positive LT momentum is a good signal for
// entering a short too, the same as it is for a long, not the mirror-image
// negative-momentum bar this used to be.
const MOMENTUM_THRESHOLD = 0.5
function eligibleToBuy(c: Candidate): boolean {
  return c.momentum !== null && c.momentum !== undefined && c.momentum >= MOMENTUM_THRESHOLD
}
function eligibleToSell(c: Candidate): boolean {
  return c.momentum !== null && c.momentum !== undefined && c.momentum >= MOMENTUM_THRESHOLD
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
// regardless of how it scores otherwise. Explicit instruction: "any short
// interest above 20% must not be presented" on the Short list. Unlike the
// momentum gate above (an absolute "never" that treats unknown as
// disqualifying), this is a risk-avoidance cap on a known-bad condition --
// a candidate with no short-interest data at all isn't assumed crowded, so
// it still passes.
const MAX_SHORT_INTEREST = 0.2
function notCrowded(c: Candidate): boolean {
  const pct = effectiveShortPctOfFloat(c)
  return pct === null || pct === undefined || pct <= MAX_SHORT_INTEREST
}

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
// above) -- meanReversion is an hourly-timeframe regression-slope trend
// (see IBApp.get_momentum), same sign convention and formula as the daily
// `momentum` field, just measured on the hourly bars: positive means a
// steady hourly UPtrend, negative a steady hourly DOWNtrend. Used here as
// an entry-timing signal, not a second momentum vote: a stock already
// trending up hard on the hourly timeframe is a stock a new long would be
// CHASING (bad entry, that move already happened), while a stock trending
// down hard on the hourly timeframe is one a new short would be chasing
// the same way -- so a significantly POSITIVE reading blocks a new long /
// flags a held long to close (the exact FIVN situation: bought at $33.29
// right after a multi-day hourly spike, meanReversion already deeply
// positive, price mean-reverted down from there), while a significantly
// NEGATIVE reading blocks a new short / flags a held short to close.
// "Significant" is deliberately a wide dead zone (MEAN_REVERSION_THRESHOLD),
// not any nonzero reading -- explicit instruction: most tickers sit within
// a few points of zero just from ordinary hourly noise (see the universe's
// own distribution: the 25th/75th percentiles are only about -0.1/+4), so
// gating on sign alone would trip constantly on noise. Only the tail
// (roughly the most extreme ~10% of readings either way) is meant to
// count. Same fail-open treatment as the growth/crowded-short gates: a
// candidate with no meanReversion at all (it's only computed for
// CANDLESTICK_TOP_N ranked/held tickers, not the whole universe -- see
// IBApp.get_momentum) isn't assumed to violate either side, so it still
// passes. Lives on tickerScreener, not the recommendations.json candidate,
// same as revenueGrowth above.
const MEAN_REVERSION_THRESHOLD = 10
function meanReversionOkForLong(meanReversion: number | null | undefined): boolean {
  return meanReversion === null || meanReversion === undefined || meanReversion < MEAN_REVERSION_THRESHOLD
}
function meanReversionOkForShort(meanReversion: number | null | undefined): boolean {
  return meanReversion === null || meanReversion === undefined || meanReversion > -MEAN_REVERSION_THRESHOLD
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

  // Independent of rating -- a held position (long or short) whose own
  // momentum has already turned flat or negative is worth flagging before
  // the rating catches up, not just after. Same bar for both sides now:
  // positive LT momentum is the supportive reading for a short same as a
  // long, so it turning flat/negative undercuts either thesis the same
  // way (see eligibleToBuy/eligibleToSell above).
  if (c && c.momentum !== null && c.momentum !== undefined && c.momentum <= 0) {
    reasons.push({
      type: 'momentum',
      text: `Momentum is no longer supportive for a ${isLong ? 'long' : 'short'} (${fmtSigned(c.momentum)}, ${c.momentum === 0 ? 'flat' : 'negative'}).`,
    })
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
  // meanReversionOkForShort above (not any nonzero reading -- most tickers
  // sit within a few points of zero from ordinary hourly noise). A held
  // long that's spiked hard enough on the hourly timeframe to already be
  // past the same bar that would have blocked opening it fresh today is
  // the FIVN situation -- bought right after a run-up, mean-reverts
  // against you from there. Mirror case for a held short: a hard enough
  // drop that a bounce is due against the short.
  if (c && c.meanReversion !== null && c.meanReversion !== undefined) {
    if (isLong && c.meanReversion >= MEAN_REVERSION_THRESHOLD) {
      reasons.push({
        type: 'mean-reversion',
        text: `ST momentum positive (${fmtSigned(c.meanReversion)}) — ready for a pullback.`,
      })
    } else if (!isLong && c.meanReversion <= -MEAN_REVERSION_THRESHOLD) {
      reasons.push({
        type: 'mean-reversion',
        text: `ST momentum negative (${fmtSigned(c.meanReversion)}) — ready for a bounce.`,
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
            ? 'Momentum data is unavailable.'
            : `Momentum is ${fmtSigned(c.momentum)}, below ${MOMENTUM_THRESHOLD}.`,
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
        text: `ST momentum positive (${fmtSigned(meanReversion as number)}) — already spiked.`,
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
            ? 'Momentum data is unavailable.'
            : `Momentum is ${fmtSigned(c.momentum)}, below ${MOMENTUM_THRESHOLD}.`,
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
        text: `ST momentum negative (${fmtSigned(meanReversion as number)}) — already dropped.`,
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
  { label: 'Momentum', note: `Daily-timeframe momentum at or above ${MOMENTUM_THRESHOLD}; unknown momentum excluded.` },
  {
    label: 'Revenue growth',
    note: `Trailing revenue growth at or above ${fmtPctAbs(REVENUE_GROWTH_THRESHOLD)}; unknown growth not excluded.`,
  },
  {
    label: 'Mean reversion',
    note: `ST momentum not significantly positive — excludes a stock that's already spiked and would be chased; unknown not excluded.`,
  },
  {
    label: 'EPS trend',
    note: 'Consensus EPS estimates not cut; unknown not excluded.',
  },
  {
    label: 'Ranking',
    note: 'Best composite score first. A non-held candidate that hedges an existing short position (same sector/theme) gets a small score bonus.',
  },
]

const SHORT_RULES = [
  { label: 'Rating', note: 'Strong Sell or Sell.' },
  {
    label: 'Momentum',
    note: `Daily-timeframe momentum at or above ${MOMENTUM_THRESHOLD} -- same bar as Long, positive LT momentum is a good short-the-overextension signal, not a bad one; unknown momentum excluded.`,
  },
  {
    label: 'Not crowded',
    note: `No more than ${fmtPctAbs(MAX_SHORT_INTEREST)} of float already sold short; unknown short interest not excluded.`,
  },
  {
    label: 'Revenue growth',
    note: `Trailing revenue growth at or below ${fmtPctAbs(REVENUE_GROWTH_THRESHOLD)}; unknown growth not excluded.`,
  },
  {
    label: 'Mean reversion',
    note: `ST momentum not significantly negative — excludes a stock that's already dropped and would be chased; unknown not excluded.`,
  },
  {
    label: 'EPS trend',
    note: 'Consensus EPS estimates not raised; unknown not excluded.',
  },
  {
    label: 'Ranking',
    note: 'Worst composite score first. A non-held candidate that hedges an existing long position (same sector/theme) gets a small score bonus.',
  },
]

const CLOSE_RULES = [
  {
    label: 'Rating contradiction',
    note: 'Held long no longer rated Buy/Strong Buy, or held short no longer rated Sell/Strong Sell — fires regardless of momentum.',
  },
  {
    label: 'Momentum reversal',
    note: 'Momentum has gone flat or negative — same bar for a long or a short — even if the rating hasn’t caught up yet.',
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
    label: 'Mean reversion reversal',
    note: 'Long: ST momentum turned significantly positive (recent spike, pullback due). Short: turned significantly negative (recent drop, bounce due).',
  },
  {
    label: 'Crowded short',
    note: `Short only — more than ${fmtPctAbs(MAX_SHORT_INTEREST)} of float is already sold short (squeeze risk).`,
  },
  {
    label: 'Earnings coming up',
    note: `Reports within ${EARNINGS_REVIEW_HOURS} business hours — either side, regardless of rating; an earnings call is a volatility event the momentum/growth/mean-reversion story doesn't speak to.`,
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
  const [tickerThemes, setTickerThemes] = useState<Record<string, string[]>>({})
  const [themeLabels, setThemeLabels] = useState<Record<string, string>>({})
  const [tickerScreener, setTickerScreener] = useState<ScreenerByTicker>({})
  // Display-only filter, applied at render time to every section below
  // (Long, Short, both "blocked" lists, To close) via filterBySymbol --
  // doesn't touch longs/shorts/rejectedStrong*/closes themselves, so
  // ranking, gates, and severity ordering are computed exactly the same
  // whether or not a filter is active; this just narrows what's *shown*.
  const [symbolFilter, setSymbolFilter] = useState('')
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
  // visible somewhere. ticker_themes.json/theme_taxonomy.json are the same
  // two files ThemesView.jsx reads, for the theme half of the hedge
  // matcher.
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
    fetch('/ticker_themes.json')
      .then((r) => (r.ok ? r.json() : {}))
      .then(setTickerThemes)
      .catch(() => {})
    fetch('/theme_taxonomy.json')
      .then((r) => (r.ok ? r.json() : []))
      .then((themes) => setThemeLabels(Object.fromEntries(themes.map((t: any) => [t.key, t.label]))))
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

  const longs: RankedCandidate[] = useMemo(() => {
    if (!data) return []
    const matcher = buildOppositeMatcher([...heldShortTickers], tickerSector, tickerThemes)
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
          oppositeMatchLine: oppositeMatchLine(match, 'short', themeLabels),
          _sortScore: sortScore,
        }
      })
    return pool.sort((a, b) => a._sortScore - b._sortScore)
  }, [data, heldShortTickers, heldLongTickers, tickerSector, tickerThemes, themeLabels, tickerScreener])

  const shorts: RankedCandidate[] = useMemo(() => {
    if (!data) return []
    const matcher = buildOppositeMatcher([...heldLongTickers], tickerSector, tickerThemes)
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
          oppositeMatchLine: oppositeMatchLine(match, 'long', themeLabels),
          _sortScore: sortScore,
        }
      })
    return pool.sort((a, b) => b._sortScore - a._sortScore)
  }, [data, heldLongTickers, heldShortTickers, tickerSector, tickerThemes, themeLabels, tickerScreener])

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
  const closes: CloseRow[] = useMemo(() => {
    if (!data) return []
    const byTicker = new Map(data.candidates.map((c) => [c.ticker, c]))
    const rows: CloseRow[] = []
    for (const [ticker, p] of Object.entries(positions)) {
      if (!p?.shares) continue
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
      const c: Candidate = { ...byTicker.get(ticker), ...tickerScreener[ticker], ticker }
      const reasons = buildCloseReasons({ shares: p.shares, c, now })
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
      rows.push({ ...c, closeSide: p.shares > 0 ? 'Long' : 'Short', shares: p.shares, reasons, hasRatingReason, _severity: severity })
    }
    return rows.sort((a, b) => b._severity - a._severity)
  }, [data, positions, tickerScreener, now])

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
    for (const c of data.candidates) {
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
              To close <span className="recommendation-tab-count">{closes.length}</span>
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
          <RecommendationSection
            title="To close"
            titleInfo={
              <RulesInfo
                label="Closing rules"
                header="Any one of these flags a held position for review"
                rules={CLOSE_RULES}
                footer="A position can trip more than one of these at once — the card below lists every reason that applies, ranked with rating contradictions first, then momentum, then the rest."
              />
            }
            subtitle="Held positions tripping a rating/momentum/score-boundary contradiction, a fundamentals reversal, or a crowded-short flag"
            rows={filterBySymbol(closes)}
            renderCard={(c) => (
              <CloseCard
                key={c.ticker}
                c={c}
                live={livePrices[c.ticker]}
                dailyHistory3mo={dailyHistory3mo}
                monthlyHistory={monthlyHistory}
              />
            )}
            emptyMessage="No held position currently has a rating/momentum contradiction or a risk flag."
          />
          )}
          {activeSection === 'long' && (
          <RecommendationSection
            title="Long"
            titleInfo={<RulesInfo label="Selection rules" header="Every candidate must clear all of these" rules={LONG_RULES} />}
            subtitle={`Strong Buy / Buy with momentum ≥ ${MOMENTUM_THRESHOLD} and revenue growth ≥ ${fmtPctAbs(REVENUE_GROWTH_THRESHOLD)}, best composite score first`}
            rows={filterBySymbol(longs)}
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
            emptyMessage="No Strong Buy/Buy candidates with positive momentum right now."
          />
          )}
          {activeSection === 'longBlocked' && (
          <RecommendationSection
            title="Strong Buy — blocked"
            subtitle="Strong Buy candidates that still failed a momentum, revenue-growth, or mean-reversion gate — see Long's Selection rules for what each gate checks"
            rows={filterBySymbol(rejectedStrongBuy)}
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
            subtitle={`Strong Sell / Sell with negative momentum and revenue growth ≤ ${fmtPctAbs(REVENUE_GROWTH_THRESHOLD)}, worst composite score first — excludes crowded shorts (>${fmtPctAbs(MAX_SHORT_INTEREST)} of float already short)`}
            rows={filterBySymbol(shorts)}
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
            emptyMessage="No Sell/Strong Sell candidates with negative momentum right now."
          />
          )}
          {activeSection === 'shortBlocked' && (
          <RecommendationSection
            title="Strong Sell — blocked"
            subtitle="Strong Sell candidates that still failed a momentum, revenue-growth, mean-reversion, or crowded-short gate — see Short's Selection rules for what each gate checks"
            rows={filterBySymbol(rejectedStrongSell)}
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
