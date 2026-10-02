import { useEffect, useState } from 'react'
import { parseCSV } from '../csv'
import { mostRecentCompletedTradingDay } from '../ibFreshness'
import { IB_DATASET_STATUS_URL } from '../ibStream'

// App-wide, always-visible flag (App.jsx's top tab-bar, pushed to the far
// right) for "is IB's own price cache -- BOTH price_history_daily_3mo.json
// and price_history_hourly.json (main.py's DAILY_3MO_HISTORY_FILE/
// HOURLY_HISTORY_FILE) -- current through T-1's close, for every ACTIVE
// ticker" -- explicit instruction. Fetched ONCE per session (this
// component is mounted in App.jsx, above the tab switch, so it survives
// every page change) rather than per-page, since these files are tens of
// MB and every page that already loads them benefits from the browser's
// own HTTP cache on the second hit anyway.
//
// Restricted to symbols.json's ACTIVE tickers (via sorted_screen.csv,
// which main.py's build_screen_rows already filters to active-only --
// see that function's own docstring) -- explicit instruction: a
// deactivated ticker (GBTG, the "Unknown contract" batch, foreign ADRs
// like BABA/JD/PDD, etc.) shouldn't count against this flag even before
// main.py's own _prune_inactive_tickers gets a chance to physically
// remove it from the cache on the next live IB refresh.
//
// "Fine" = fewer than STALE_TOLERANCE_FRACTION (1%) of active tickers are
// behind -- explicit instruction: a handful of names lagging (an IB
// Gateway pacing hiccup, a single "Unknown contract" warning not yet
// deactivated) shouldn't flip the WHOLE app's flag red; only a genuinely
// broad gap should. "Up to date" per ticker = its own last bar is dated
// mostRecentCompletedTradingDay() or newer (see ibFreshness.ts -- T-1,
// rolled back over the weekend, same convention RecommendationsView.tsx's
// own PreviousCloseFlag already uses per-ticker) -- checked for hourly
// too (just the DATE portion of its last bar, same threshold), not just
// daily.
const STALE_TOLERANCE_FRACTION = 0.01

// The subset of ib_server.py's own DATASETS list (GET /api/dataset-status)
// that's actually expected to refresh daily via `python main.py all` --
// explicit instruction: "all other datasets are no older than 1 business
// day". Deliberately NOT every entry: DatasetTable.tsx's own design
// comment explains why a single staleness cutoff is wrong for most rows
// (quarterly SEC filings, hand-maintained files, etc.), so this list is a
// hand-picked allowlist rather than the whole ~28-entry table. Excluded on
// purpose: ib_daily/ib_hourly (already covered above, per-ticker, by the
// dedicated IB check -- would be a redundant/confusing second red flag for
// the same underlying gap); symbol_universe/theme_taxonomy (hand-
// maintained, no command); news_headlines/news_sentiment/ticker_themes
// (continuous background loops or on-demand, not part of `all`);
// form4/xbrl/13f/short_interest (SEC/FINRA cadence is weekly-to-quarterly,
// not daily); eulerpool_grades (self-throttled to 3 days by design, see
// its own notes); portfolio_performance/trades (Flex Query, 6h server
// auto-refresh, not `all`); backtest (weekly snapshot cadence). One id per
// underlying file where several DATASETS rows share a path (e.g.
// screener_ranking/_prices/_rescore all write SORTED_SCREEN_CSV) to avoid
// checking the same mtime twice under two labels.
const DAILY_PIPELINE_DATASET_IDS = [
  'screener_ranking',
  'screen_data',
  'raw_yahoo_payloads',
  'yahoo_price_history',
  'social_sentiment',
  'eulerpool_fair_value',
  'eulerpool_forward_estimates',
  'eulerpool_short_volume',
  'recommendations',
]

interface DatasetFile {
  id: string
  label: string
  exists: boolean
  mtime: string | null
}

interface DatasetCheck {
  staleLabels: string[]
}

function checkDatasets(files: DatasetFile[], expected: string): DatasetCheck {
  const staleLabels: string[] = []
  for (const id of DAILY_PIPELINE_DATASET_IDS) {
    const f = files.find((x) => x.id === id)
    if (!f) continue
    const mdate = f.exists && f.mtime ? f.mtime.slice(0, 10) : null
    if (!mdate || mdate < expected) staleLabels.push(f.label)
  }
  return { staleLabels }
}

interface SeriesCheck {
  total: number
  staleCount: number
  staleTickers: string[]
  latestDate: string
  oldestDate: string
}

function checkSeries(data: Record<string, { date: string }[]>, activeTickers: Set<string>, expected: string): SeriesCheck {
  let latestDate = ''
  let oldestDate = ''
  const staleTickers: string[] = []
  let total = 0
  for (const ticker of Object.keys(data)) {
    if (!activeTickers.has(ticker)) continue
    total++
    const bars = data[ticker]
    const lastDate = bars && bars.length ? bars[bars.length - 1].date.slice(0, 10) : null
    if (!lastDate) {
      staleTickers.push(ticker)
      continue
    }
    if (lastDate > latestDate) latestDate = lastDate
    if (!oldestDate || lastDate < oldestDate) oldestDate = lastDate
    if (lastDate < expected) staleTickers.push(ticker)
  }
  return { total, staleCount: staleTickers.length, staleTickers, latestDate, oldestDate: oldestDate || 'unknown' }
}

type State =
  | { status: 'loading' }
  | { status: 'unreachable'; detail: string }
  | { status: 'missing'; detail: string }
  | { status: 'fresh'; daily: SeriesCheck; hourly: SeriesCheck; datasets: DatasetCheck }
  | { status: 'stale'; daily: SeriesCheck; hourly: SeriesCheck; datasets: DatasetCheck }

// 404 (the file genuinely doesn't exist -- e.g. `python main.py ibprices`
// has never actually been run) is a DIFFERENT situation from the fetch/
// proxy call itself failing (ib_server.py isn't running at all, so `npm
// run dev`'s own proxy -- see vite.config.js's STATIC_DATA_PATHS -- can't
// even reach it and returns a 502) -- confirmed live: the two look
// identical from a plain r.ok check, but only one of them means "prices
// were never downloaded."
async function fetchJson(url: string): Promise<Record<string, { date: string }[]>> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(r.status === 404 ? `missing:${url}` : `unreachable:${url}`)
  return r.json()
}

export default function IbFreshnessBadge() {
  const [state, setState] = useState<State>({ status: 'loading' })

  useEffect(() => {
    let cancelled = false
    Promise.all([
      fetch('/sorted_screen.csv').then((r) => (r.ok ? r.text() : Promise.reject(new Error('unreachable:sorted_screen.csv')))),
      fetchJson('/price_history_daily_3mo.json'),
      fetchJson('/price_history_hourly.json'),
      // Best-effort, separate from the three above: hits ib_server.py's
      // API directly (an absolute URL, not one of vite.config.js's proxied
      // STATIC_DATA_PATHS), so it fails independently of the static-file
      // proxy. Fails open (empty staleLabels) on any error -- a dataset-
      // status hiccup shouldn't flip the whole badge to "unreachable" when
      // the IB price files it actually gates fetched just fine.
      fetch(IB_DATASET_STATUS_URL)
        .then((r) => (r.ok ? r.json() : { files: [] }))
        .catch(() => ({ files: [] })),
    ])
      .then(([csvText, daily, hourly, datasetStatus]) => {
        if (cancelled) return
        // sorted_screen.csv is ALREADY active-only (build_screen_rows
        // filters to symbols.json's active flag before this file is ever
        // written), so every ticker in it is exactly the set this badge
        // should hold IB's cache to.
        const activeTickers = new Set(parseCSV(csvText).map((row: Record<string, string>) => row.ticker))
        const expected = mostRecentCompletedTradingDay()
        const dailyCheck = checkSeries(daily, activeTickers, expected)
        const hourlyCheck = checkSeries(hourly, activeTickers, expected)
        const dailyFine = dailyCheck.total === 0 || dailyCheck.staleCount / dailyCheck.total < STALE_TOLERANCE_FRACTION
        const hourlyFine = hourlyCheck.total === 0 || hourlyCheck.staleCount / hourlyCheck.total < STALE_TOLERANCE_FRACTION
        const datasetCheck = checkDatasets((datasetStatus && datasetStatus.files) || [], expected)
        const datasetsFine = datasetCheck.staleLabels.length === 0
        setState({
          status: dailyFine && hourlyFine && datasetsFine ? 'fresh' : 'stale',
          daily: dailyCheck,
          hourly: hourlyCheck,
          datasets: datasetCheck,
        })
      })
      .catch((e: Error) => {
        if (cancelled) return
        const [kind, url] = e.message.split(':')
        setState({ status: kind === 'missing' ? 'missing' : 'unreachable', detail: url || 'unknown' })
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (state.status === 'loading') {
    return <span className="ib-freshness-badge ib-freshness-loading">IB prices…</span>
  }
  if (state.status === 'unreachable') {
    return (
      <span
        className="ib-freshness-badge ib-freshness-stale"
        title={`Couldn't reach ${state.detail} -- ib_server.py isn't running (this app proxies/serves that path through it; see vite.config.js's STATIC_DATA_PATHS or ib_server.py's own STATIC_FILES). The underlying data on disk may be perfectly fine; this just can't check it right now.`}
      >
        ⚠ IB server unreachable
      </span>
    )
  }
  if (state.status === 'missing') {
    return (
      <span className="ib-freshness-badge ib-freshness-stale" title={`${state.detail} doesn't exist yet -- IB bars have never been fetched. Run \`python main.py ibprices\`.`}>
        ⚠ IB prices never downloaded
      </span>
    )
  }

  const pct = (c: SeriesCheck) => (c.total ? ((c.staleCount / c.total) * 100).toFixed(1) : '0.0')
  const detail =
    `Active tickers only (sorted_screen.csv). Tolerance: <${(STALE_TOLERANCE_FRACTION * 100).toFixed(0)}% behind counts as fine.\n` +
    `Daily: ${state.daily.staleCount}/${state.daily.total} stale (${pct(state.daily)}%), latest ${state.daily.latestDate || 'n/a'}, oldest ${state.daily.oldestDate}` +
    (state.daily.staleCount ? ` -- e.g. ${state.daily.staleTickers.slice(0, 15).join(', ')}${state.daily.staleCount > 15 ? ', …' : ''}` : '') +
    `\nHourly: ${state.hourly.staleCount}/${state.hourly.total} stale (${pct(state.hourly)}%), latest ${state.hourly.latestDate || 'n/a'}, oldest ${state.hourly.oldestDate}` +
    (state.hourly.staleCount ? ` -- e.g. ${state.hourly.staleTickers.slice(0, 15).join(', ')}${state.hourly.staleCount > 15 ? ', …' : ''}` : '') +
    `\n${state.datasets.staleLabels.length ? `Datasets stale: ${state.datasets.staleLabels.join(', ')}` : 'Datasets current (all refreshed within 1 business day).'}` +
    (state.status === 'stale' ? '\nRun `python main.py ibprices`/`all` to refresh.' : '')

  if (state.status === 'fresh') {
    return (
      <span className="ib-freshness-badge ib-freshness-fresh" title={detail}>
        ✓ IB prices current
      </span>
    )
  }
  const worstCount = Math.max(state.daily.staleCount, state.hourly.staleCount)
  const label = state.datasets.staleLabels.length
    ? worstCount
      ? `IB prices + ${state.datasets.staleLabels.length} dataset(s) stale`
      : `${state.datasets.staleLabels.length} dataset(s) stale`
    : `IB prices stale (${worstCount})`
  return (
    <span className="ib-freshness-badge ib-freshness-stale" title={detail}>
      ⚠ {label}
    </span>
  )
}
