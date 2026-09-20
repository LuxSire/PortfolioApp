"""eulerpool.py -- thin wrapper around Eulerpool's fundamentals/forecasts/
transcripts REST API (https://eulerpool.com/financial-data-api).

There is NO published `eulerpool` package on PyPI (confirmed against PyPI's
own JSON API, not just pip's index cache -- pypi.org/pypi/eulerpool/json
returns a plain 404, same for eulerpool-api/-sdk/pyeulerpool/euler-pool/
eulerpool-client). This module talks to the documented REST API directly
with `httpx` (the one dependency Eulerpool's own docs say their SDK needs
anyway) rather than wrapping a library that doesn't exist. The full route
list -- everything below is taken from there, not guessed -- lives at
https://eulerpool.com/llms-full.txt (their own machine-readable API
reference; a 404 from any real endpoint on this API points you back at
that URL as a "browse all 400+ endpoints" hint).

AUTH: a single API key as a query string param, `?token=...` (no OAuth, no
headers). Read from the EULERKEY env var (see .env / load_dotenv, same
convention modules.IBApp already uses for its own env vars).

TWO THINGS THAT WILL BITE YOU IF YOU CALL THE API DIRECTLY instead of
through this module:

1. Cloudflare sits in front of api.eulerpool.com and blocks the default
   User-Agent httpx/requests/urllib send -- every request 403s with
   {"error code": 1010} (Cloudflare's own "browser signature banned" code,
   not an Eulerpool auth error). A plain browser-like User-Agent string
   (see _HEADERS) is enough to get through; this module always sends one.

2. Every endpoint's `identifier` path param officially accepts ISIN,
   ticker, CUSIP, SEDOL, or WKN -- but confirmed live, passing an ISIN to
   at least /equity/estimates/{isin} returns HTTP 200 with an ENCRYPTED
   payload ({"iv":..., "salt":..., "ciphertext":...}) instead of the
   documented JSON, while the exact same call with the ticker (AAPL vs
   US0378331005) returns clean data. Cause unknown (a cache/CDN edge keyed
   differently per identifier type, most likely) -- the workaround is
   simple: this module's public functions all take a TICKER, never an
   ISIN, and _get raises a clear EulerpoolEncryptedResponseError instead of
   silently handing back ciphertext if this ever recurs on another route.

Coverage varies by security and by your plan -- call get_coverage(ticker)
first if you're about to hit a less-common endpoint (balance sheet, ESG,
supply chain, ownership) on an unfamiliar name; it returns which data
types are actually available rather than making you discover a 404 (or
worse, a silent empty list) after the fact. Confirmed live: AAPL is fully
covered on this key (hasBalanceSheet/hasEstimates/hasESG/hasSupplyChain/
hasOwnership all true).
"""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.eulerpool.com/api/1"

# Cloudflare blocks httpx's default User-Agent outright (see module
# docstring) -- this is the one header that matters. Accept is just
# good manners; the API returns JSON regardless.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Eulerpool doesn't publish a rate limit anywhere in their docs (checked
# the full llms-full.txt reference, no mention). This is a conservative,
# untested guess -- same spirit as sec_edgar.py's own _rate_limit for a
# provider with no documented number -- loosen it if it turns out overly
# cautious, tighten it if a 429 ever shows up (not handled specially below
# yet; add backoff here if that happens in practice).
_MIN_REQUEST_INTERVAL = 0.2  # 5 req/sec
_last_request_at = 0.0
_rate_lock = threading.Lock()  # fetch_rating_changes below calls _get from a thread pool


class EulerpoolError(Exception):
    """Base class for anything this module raises on its own (as opposed
    to httpx's own connection/timeout errors, left to propagate)."""


class EulerpoolEncryptedResponseError(EulerpoolError):
    """Raised when the API returns the {iv, salt, ciphertext} shape
    instead of real JSON -- see module docstring point 2. Always try the
    ticker instead of an ISIN/CUSIP/SEDOL/WKN first; this module's public
    functions already do that, so seeing this means either a NEW endpoint
    got the same treatment, or you called _get directly with something
    other than a ticker."""


def _rate_limit():
    global _last_request_at
    with _rate_lock:
        wait = _MIN_REQUEST_INTERVAL - (time.time() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.time()


def _get(path, **params):
    """GET {BASE_URL}{path} with the API token + browser UA, ->
    parsed JSON (list or dict, whatever the endpoint returns). Raises
    EulerpoolError if EULERKEY isn't set, httpx.HTTPStatusError on a
    non-2xx response (via raise_for_status -- callers see the real status
    code/body rather than a generic failure), and
    EulerpoolEncryptedResponseError if the response is the undocumented
    encrypted shape (see module docstring)."""
    key = os.getenv("EULERKEY")
    if not key:
        raise EulerpoolError("EULERKEY not set in .env")
    _rate_limit()
    query = {**params, "token": key}
    resp = httpx.get(f"{BASE_URL}{path}", params=query, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and {"iv", "salt", "ciphertext"} <= data.keys():
        raise EulerpoolEncryptedResponseError(
            f"{path} returned an encrypted payload instead of JSON -- "
            "pass a ticker, not an ISIN/CUSIP/SEDOL/WKN (see module docstring)"
        )
    return data


# --------------------------------------------------------------------------- #
#  Equity profile / coverage
# --------------------------------------------------------------------------- #

def get_profile(ticker):
    """Company profile: name, sector, industry, description, employees,
    market cap, shares out, IPO date, website, logo path. Same shape
    yfinance's .info gives you, narrower."""
    return _get(f"/equity/profile/{ticker}")


def get_coverage(ticker):
    """{ticker, hasBalanceSheet, hasEstimates, hasESG, hasSupplyChain,
    hasOwnership, raw: {...}} -- call before a less-common endpoint on an
    unfamiliar name to avoid a 404 (or the encrypted-response gotcha)."""
    return _get(f"/equity/coverage/{ticker}")


def get_fair_value(ticker):
    """{isin, fairValue, fairValueIncome, fairValueRevenue,
    fairValueDividend, lastPrice, upside} -- Eulerpool's own composite DCF-
    style fair value estimate plus its three sub-components (income-,
    revenue-, and dividend-based), `upside` already computed as
    (fairValue/lastPrice - 1) * 100. IMPORTANT: this is a CURRENT SNAPSHOT
    only, not a time series -- confirmed live, calling this twice in a
    row returns two different fairValue/lastPrice pairs (re-computed
    against whatever price is current each call), and there is no
    documented or working date/period/history parameter (tried
    /fair-value/by-isin/{t}/history, /fair-value/history/{t}, and
    period=/history= query params -- all either 404 or silently ignored).
    /fmp/dcf/{ticker} has the same "single current point" limitation.
    There is no way to pull Eulerpool's own past fair-value readings
    retroactively -- the only way to build a history is to start
    snapshotting this yourself going forward (see fetch_fair_values)."""
    return _get(f"/fair-value/by-isin/{ticker}")


# --------------------------------------------------------------------------- #
#  Forecasts / estimates
# --------------------------------------------------------------------------- #

def get_forecast(ticker):
    """Analyst forecast TRAJECTORY: {ticker, as_of, updated_at, data:
    {ISIN, Name, CurrencyCode, yearly: {ebit, ebitda, revenue, netIncome,
    totalAssets, freeCashFlow, totalStockholderEquity,
    totalCashFromOperatingActivities, ratios}, quarterly: {...same
    shape}}} -- each series is a list of {date, value} points running
    several years forward. This is the multi-year projected PATH; for a
    single consensus number per metric (with high/low/analyst-count) see
    get_estimates instead."""
    return _get(f"/equity/forecast/{ticker}")


def get_estimates(ticker):
    """Consensus analyst estimates, one entry per FISCAL YEAR --
    [{period, year, revenueEstimate, revenueHigh, revenueLow,
    revenueAnalysts, epsEstimate, epsHigh, epsLow, epsAnalysts,
    ebitEstimate, ebitHigh, ebitLow, ebitAnalysts, quarterly: [...]}, ...].
    NOT future-only -- confirmed live, AAPL's own array runs 1996 through
    2030 in one block, past actuals and forward consensus mixed together
    with no is-this-an-estimate flag to tell them apart; the only
    reliable way to find "the forward EPS" is to compare each row's
    `period` (a fiscal YEAR END date, e.g. '2026-09-30') against today and
    take the nearest one still in the future -- see get_forward_eps,
    which does exactly that. The high/low/analyst-count triple on each
    metric is the same shape this project's own
    targetLowPrice/targetHighPrice/numberOfAnalystOpinions pattern
    already uses for price targets -- same "how much do analysts agree"
    read, just per-metric instead of just price."""
    return _get(f"/equity/estimates/{ticker}")


def get_forward_estimates(ticker, today=None):
    """(fwd_eps0y, fwd_eps1y, fwd_eps2y, fwd_revenue0y, fwd_revenue1y,
    fwd_revenue2y) -- Eulerpool's own THREE nearest-future fiscal years'
    consensus EPS AND revenue, all pulled from the SAME get_estimates call
    (each row already carries epsEstimate and revenueEstimate together --
    no reason to hit the endpoint twice for two metrics off one array).
    The 0y/1y EPS pair is the same shape this project's own fwdEps0y
    (current, not-yet-completed fiscal year) / fwdEps1y (the one after)
    already carries from yfinance -- see modules.derive's own
    fy_diluted_eps_growth for where that pair is used. fwd_eps2y/
    fwd_revenue2y (the year after THAT, for both metrics) have no
    yfinance counterpart at all -- yfinance's own earningsEstimate
    statement only ever carries '0y'/'+1y' (see modules.derive.
    statement_metrics), and never gave this project a forward revenue
    estimate at any horizon (only a TRAILING revenueGrowth -- see
    modules.derive.reconcile_revenue_growth) -- so both are genuinely new,
    Eulerpool-only figures, not something to blend; see modules.derive.
    reconcile_forward_eps, which passes fwd_eps2y through as-is
    (eulerFwdEps2y) and derives eulerRevGrowth2y =
    fwd_revenue2y/fwd_revenue1y - 1 the same way it derives
    eulerRevGrowth1y from the 0y/1y pair. modules.simulations blends both
    into the year-2 consensus drift nudge (the same role forwardEps/
    anchorEps - 1 plays for year 1, one year further out and averaged
    across an EPS-implied and a revenue-implied reading instead of just
    one) and years 3+ are left on the plain concave reversion-to-peer-
    median schedule -- no third year of drift is pulled from Eulerpool.

    fwd_revenue1y/fwd_revenue0y - 1 (the ORIGINAL, still-used pair) also
    feeds modules.simulations' own ownGrowthRate/industryGrowthRate as a
    third, equally-weighted leg alongside epsTrend and
    marginAdjustedRevenueGrowth -- see that function's own docstring.

    Picked by comparing each row's `period` (fiscal year end,
    'YYYY-MM-DD') against `today` (defaults to date.today()), NOT by
    trusting array order or the presence of a `year` field alone
    (get_estimates mixes past actuals into the same array -- see that
    function's own docstring). None for any slot a ticker doesn't have at
    least that many future fiscal years of coverage for, or doesn't carry
    that particular metric on its estimates row."""
    today = (today or date.today()).isoformat()
    rows = get_estimates(ticker)
    future = sorted(
        (r for r in rows if r.get("period") and r["period"] > today),
        key=lambda r: r["period"],
    )

    def _pick(i, key):
        return future[i].get(key) if len(future) > i else None

    return (
        _pick(0, "epsEstimate"), _pick(1, "epsEstimate"), _pick(2, "epsEstimate"),
        _pick(0, "revenueEstimate"), _pick(1, "revenueEstimate"), _pick(2, "revenueEstimate"),
    )


def get_forward_eps(ticker, today=None):
    """(fwd_eps0y, fwd_eps1y) -- thin wrapper over get_forward_estimates
    for callers that only want the near-term EPS pair; see that function's
    own docstring for the full picture (the 2-years-ahead EPS/revenue
    figures come from the same underlying call, at no extra API cost, for
    anything that wants them)."""
    fwd_eps0y, fwd_eps1y, _, _, _, _ = get_forward_estimates(ticker, today)
    return fwd_eps0y, fwd_eps1y


def get_price_target_news(ticker):
    """Named-analyst price-target calls: {ticker, as_of, updated_at, data:
    [{analystName, analystCompany, priceTarget, adjPriceTarget,
    priceWhenPosted, newsTitle, newsPublisher, newsBaseURL, newsURL,
    publishedDate}, ...]}, newest first. This is the ONE endpoint that
    names an actual person (get_estimates/get_forecast are consensus-only,
    no names) -- confirmed live, only ~40% of entries have analystName
    populated (the rest come from wire coverage that didn't attribute a
    specific person), so treat it as optional per-row, not guaranteed."""
    return _get(f"/equity/price-target-news/{ticker}")


def get_analyst_grades(ticker, limit=None):
    """Dated rating-action history, one row per action: {date,
    grading_company, previous_grade, new_grade, action
    (upgrade/downgrade/init/maintain)}, newest first. FIRM-level
    (grading_company, e.g. "Jefferies"), not an individual analyst's name
    -- see get_price_target_news for named-person data. `limit` is
    documented but NOT actually honored server-side, confirmed live
    (AAPL with limit=10 still returned all ~200 rows) -- passed through
    anyway in case that gets fixed upstream; slice the result yourself if
    you need fewer rows."""
    return _get(f"/equity/analyst-grades/{ticker}", **({"limit": limit} if limit else {}))


def get_rating_changes(ticker):
    """get_analyst_grades(ticker) filtered down to real events --
    action in {upgrade, downgrade} only, "maintain" dropped. On AAPL this
    cuts 200 rows to ~21: maintain is a firm just re-affirming its
    existing rating (typically logged around every earnings print) and
    dominates the raw feed by volume without being a change at all.
    `init` (an initiation, no prior grade to compare) is ALSO dropped
    here -- deliberately, not an oversight: it has no previous_grade to
    diff against, so it can't be classified up/down without a firm-
    agnostic grade taxonomy this endpoint doesn't provide (a "Hold"
    initiation from one firm and a "Buy" initiation from another aren't
    directly comparable without normalizing each firm's own scale first).
    Still newest-first, same row shape as get_analyst_grades. A one-off
    live-call convenience only -- fetch_analyst_grades (the batch/cache
    path feeding modules.scoring) deliberately caches the UNFILTERED
    history instead, maintain included, so percentage-of-each-action-type
    context (see modules.scoring.analyst_grade_mix) isn't thrown away."""
    return [g for g in get_analyst_grades(ticker) if g.get("action") in ("upgrade", "downgrade")]


# --------------------------------------------------------------------------- #
#  Short selling
# --------------------------------------------------------------------------- #

def get_short_volume(ticker, limit=90):
    """[{date, shortVolume, shortExemptVolume, totalVolume, shortRatio,
    market}, ...], newest first -- FINRA's DAILY short-sale volume tape
    (what fraction of that day's trading was short-sold), not to be
    confused with FINRA's biweekly short INTEREST settlement file this
    project already fetches directly in modules.finra (outstanding short
    POSITIONS, a level, updated every two weeks). This is a flow/activity
    read at daily frequency -- genuinely new data this project has never
    ingested, not a fresher copy of something it already has (unlike
    Eulerpool's own /equity/short-interest-positions endpoint, confirmed
    live to just re-wrap the same FINRA settlement file modules.finra
    already pulls straight from the source -- deliberately NOT wrapped
    here, no reason to add a third-party hop in front of data already on
    disk). shortRatio here is shortVolume/totalVolume for that ONE day,
    not FINRA's days-to-cover metric of the same name in the settlement
    file -- see fetch_short_volume, which smooths this over a trailing
    window rather than using a single (noisy) day's reading."""
    return _get(f"/equity/short-volume/{ticker}", limit=limit)


SHORT_VOLUME_FILE = os.path.join("data", "eulerpool", "short_volume.json")
# Trading days averaged into shortVolumeRatio -- smooths the day-to-day
# noise confirmed live (AAPL's own daily ratio swung 0.49 -> 0.59 -> 0.63
# across three consecutive days) while staying far fresher than FINRA's
# biweekly settlement file (modules.finra's own changePercent leg).
SHORT_VOLUME_WINDOW_DAYS = 10


def fetch_short_volume(tickers, out_file=SHORT_VOLUME_FILE, window_days=SHORT_VOLUME_WINDOW_DAYS, max_workers=4):
    """Fetch get_short_volume for every ticker in `tickers`, reduce each
    to a single trailing-window average, OVERWRITE out_file with
    {ticker: {"shortVolumeRatio": ..., "days": N}} -- same same-day-
    snapshot, full-overwrite (no merge, no staleness cooldown) shape as
    fetch_fair_values: the window average is recomputed fresh from
    whatever the last `window_days` trading days look like as of today,
    so there's no meaningful history to preserve across runs the way
    fetch_analyst_grades preserves grade history.

    shortVolumeRatio is the plain mean of each day's shortVolume/
    totalVolume over the most recent `window_days` rows get_short_volume
    returns (skips a day with totalVolume 0 or missing rather than
    counting it as a zero -- a data gap, not a genuine no-short-activity
    day). None (ticker excluded from the result) if fewer than
    window_days // 2 usable days are available, so a name with a
    thin/gappy FINRA tape doesn't get a ratio built off 1-2 noisy points.

    Feeds modules.scoring.load_short_interest_scores, which merges this
    in as a fourth leg of short_interest_rank alongside pctOfFloat/
    daysToCover/changePercent (see that function's own docstring) --
    equal 1/4 weight, same contrarian direction (higher ratio = more of
    that name's trading is short-side = better). Same thread-pool-over-a-
    shared-rate-limit shape as fetch_analyst_grades/fetch_fair_values --
    errors logged and skipped per-ticker rather than aborting the batch."""
    results = {}
    errors = []

    def _one(ticker):
        rows = get_short_volume(ticker, limit=window_days)
        ratios = [
            r["shortVolume"] / r["totalVolume"]
            for r in rows[:window_days]
            if r.get("totalVolume")
        ]
        if len(ratios) < max(1, window_days // 2):
            return None
        return sum(ratios) / len(ratios), len(ratios)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_one, t): t for t in tickers}
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                result = fut.result()
                if result is not None:
                    ratio, n_days = result
                    results[ticker] = {"shortVolumeRatio": ratio, "days": n_days}
            except Exception as e:
                errors.append((ticker, str(e)))

    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    with open(out_file, "w") as f:
        json.dump(results, f)

    print(f"fetch_short_volume: wrote {out_file} ({len(tickers)} requested, "
          f"{len(results)} with a usable ratio, {len(errors)} failed)")
    if errors:
        print("  failed:", ", ".join(t for t, _ in errors[:20]), "..." if len(errors) > 20 else "")
    return results


# --------------------------------------------------------------------------- #
#  Fundamentals (annual + quarterly)
# --------------------------------------------------------------------------- #

def get_income_statement(ticker):
    """Annual income statement, oldest first: revenue, costOfGoodsSold,
    grossIncome, researchDevelopment, sgaExpense, ebit, netIncome, and
    more, one entry per fiscal year end."""
    return _get(f"/equity/incomestatement/{ticker}")


def get_income_statement_quarterly(ticker):
    """Quarterly income statement -- same fields as get_income_statement
    plus diluted_eps and quarter_period (e.g. '1985 Q3'), one entry per
    fiscal quarter end."""
    return _get(f"/equity/income-statement-quarterly/{ticker}")


def get_balance_sheet(ticker):
    """Annual balance sheet: assets, liabilities, equity, goodwill,
    inventory, receivables, debt (short/long term), and more."""
    return _get(f"/equity/balancesheet/{ticker}")


def get_cashflow_statement(ticker):
    """Annual cash flow statement: fcf, capex, operating/investing/
    financing cash flow, dividends paid, and more."""
    return _get(f"/equity/cashflowstatement/{ticker}")


def get_dividends(ticker):
    """[{payDate, period, dividend}, ...], full history oldest first."""
    return _get(f"/equity/dividends/{ticker}")


# --------------------------------------------------------------------------- #
#  Earnings call transcripts
# --------------------------------------------------------------------------- #

def list_earning_calls(ticker):
    """[{id, ticker, datePublished (epoch ms), title, type,
    presentationUrl, transcriptAudioUrl, presentationAvailable,
    transcriptAudioAvailable}, ...], newest first. `id` is what
    get_earning_call_transcript needs -- this list doesn't carry the
    transcript text itself, just enough to find the call you want."""
    return _get(f"/earning-calls/list/{ticker}")


def get_earning_call_transcript(call_id):
    """Full content of one earnings call by its numeric id (from
    list_earning_calls): everything list_earning_calls returns, plus
    parsedContent: {companyParticipants: [...], otherParticipants: [...],
    entries: [{seq, speaker, content}, ...]} -- entries is the actual
    transcript body, in order, one entry per speaker turn (confirmed live
    -- companyParticipants/otherParticipants can come back empty even when
    entries is fully populated, so read the transcript text from entries,
    not from the participants lists). Plus a presentationUrl (PDF) and
    transcriptAudioUrl (mp3) when available for that call."""
    return _get(f"/earning-calls/transcript/{call_id}")


def get_latest_transcript(ticker):
    """Convenience: list_earning_calls(ticker), then fetch the full
    content of the most recent one -- None if there are no calls on file
    for this ticker. For anything more specific (a particular quarter,
    older history), use list_earning_calls yourself and pick the id."""
    calls = list_earning_calls(ticker)
    if not calls:
        return None
    latest = max(calls, key=lambda c: c.get("datePublished") or 0)
    return get_earning_call_transcript(latest["id"])


# --------------------------------------------------------------------------- #
#  Batch fetch + cache -- feeds modules.scoring's own analyst-grade factor
# --------------------------------------------------------------------------- #

GRADES_FILE = os.path.join("data", "eulerpool", "analyst_grades.json")
# Analyst rating actions are sparse (see ANALYST_GRADE_LOOKBACK_DAYS's own
# comment in modules.scoring -- most names go months between events), so
# there's no value re-fetching daily. Same staleness-cooldown pattern
# sec_edgar.py's own CIK_MAP_MAX_AGE_DAYS uses for its (also slow-moving)
# CIK map.
GRADES_MAX_AGE_DAYS = 3


def fetch_analyst_grades(tickers, out_file=GRADES_FILE, max_workers=4, force=False):
    """Fetch get_analyst_grades -- the FULL history, "maintain" included
    -- for every ticker in `tickers`, merge into whatever's already on
    disk at out_file, and write back -- {ticker: [...]}, same "archive,
    don't replace" convention sec_edgar.fetch_form4 uses, so a partial run
    (interrupted, or a subset of tickers re-fetched) doesn't lose earlier
    coverage. A ticker that errors (rate limit, delisted/OTC name
    Eulerpool doesn't cover, timeout) is logged and skipped rather than
    aborting the whole batch -- its LAST cached value (if any) is left
    untouched on disk.

    Deliberately caches the UNFILTERED history rather than
    get_rating_changes' upgrade/downgrade-only subset -- explicit
    instruction: don't discard maintain at fetch time, it's what lets
    modules.scoring.analyst_grade_mix report what fraction of a ticker's
    recent coverage is upgrades/downgrades/maintains, not just a net
    score. Filtering maintain out here would throw that away
    permanently (the next fetch is GRADES_MAX_AGE_DAYS away); filtering
    happens downstream instead, in modules.scoring, cheaply, from data
    already on disk.

    Skipped entirely (returns whatever's on disk, unchanged) if out_file
    already exists and is younger than GRADES_MAX_AGE_DAYS -- pass
    force=True to refetch anyway (e.g. after adding new tickers to the
    universe that the cached file has never covered).

    max_workers overlaps network latency across tickers; it does NOT
    defeat _MIN_REQUEST_INTERVAL's own pacing -- _rate_limit is a single
    lock shared by every thread, so this is still one request every
    _MIN_REQUEST_INTERVAL seconds account-wide, just without a thread
    sitting idle waiting on the network between calls. For the WHOLE
    active universe (~2000 tickers) at 5 req/sec that's still ~7 minutes
    minimum -- a real pull, not instant; call with a short ticker list
    first (or an explicit go-ahead) rather than the full universe by
    default."""
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    try:
        with open(out_file) as f:
            merged = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        merged = {}

    if not force and merged and os.path.exists(out_file):
        age_days = (time.time() - os.path.getmtime(out_file)) / 86400
        if age_days < GRADES_MAX_AGE_DAYS:
            print(f"fetch_analyst_grades: {out_file} is {age_days:.1f}d old "
                  f"(< {GRADES_MAX_AGE_DAYS}d) -- skipping, pass force=True to refetch")
            return merged

    errors = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(get_analyst_grades, t): t for t in tickers}
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                merged[ticker] = fut.result()
            except Exception as e:
                errors.append((ticker, str(e)))

    with open(out_file, "w") as f:
        json.dump(merged, f)

    print(f"fetch_analyst_grades: wrote {out_file} ({len(tickers)} requested, "
          f"{len(tickers) - len(errors)} succeeded, {len(errors)} failed)")
    if errors:
        print("  failed:", ", ".join(t for t, _ in errors[:20]), "..." if len(errors) > 20 else "")
    return merged


FAIR_VALUE_FILE = os.path.join("data", "eulerpool", "fair_value.json")


def fetch_fair_values(tickers, out_file=FAIR_VALUE_FILE, max_workers=4):
    """Fetch get_fair_value for every ticker in `tickers`, OVERWRITE
    out_file with {ticker: {isin, fairValue, fairValueIncome,
    fairValueRevenue, fairValueDividend, lastPrice, upside}} -- no
    staleness cooldown and no merge-with-existing here, unlike
    fetch_analyst_grades: get_fair_value is a same-day snapshot (see its
    own docstring -- there's no history to preserve across calls, and an
    old snapshot is just wrong the moment prices move), so every call is
    meant to fully replace whatever was cached. If you want a USABLE
    history for a forward-return check (Eulerpool doesn't provide one --
    see get_fair_value), call this on a schedule yourself and archive
    each day's out_file under its own dated path rather than relying on
    this function to do it for you.

    Same thread-pool-over-a-shared-rate-limit shape as
    fetch_analyst_grades (see that function's own docstring) -- errors
    are logged and skipped per-ticker rather than aborting the batch."""
    results = {}
    errors = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(get_fair_value, t): t for t in tickers}
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                results[ticker] = fut.result()
            except Exception as e:
                errors.append((ticker, str(e)))

    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    with open(out_file, "w") as f:
        json.dump(results, f)

    print(f"fetch_fair_values: wrote {out_file} ({len(tickers)} requested, "
          f"{len(tickers) - len(errors)} succeeded, {len(errors)} failed)")
    if errors:
        print("  failed:", ", ".join(t for t, _ in errors[:20]), "..." if len(errors) > 20 else "")
    return results


FORWARD_EPS_FILE = os.path.join("data", "eulerpool", "forward_eps.json")


def fetch_forward_eps(tickers, out_file=FORWARD_EPS_FILE, max_workers=4):
    """Fetch get_forward_estimates for every ticker in `tickers`, OVERWRITE
    out_file with {ticker: {"fwdEps0y": ..., "fwdEps1y": ..., "fwdEps2y":
    ..., "fwdRevenue0y": ..., "fwdRevenue1y": ..., "fwdRevenue2y": ...}}
    -- same same-day-snapshot, full-overwrite (no merge, no staleness
    cooldown) shape as fetch_fair_values, for the same reason:
    get_forward_estimates reads get_estimates fresh each call, so there's
    no history here to preserve across runs. Feeds modules.derive/
    modules.simulations: reconcile_forward_eps blends the 0y/1y EPS pair
    50/50 with this project's own yfinance-sourced fwdEps0y/fwdEps1y into
    "our own" forward EPS; fwdEps2y/fwdRevenue2y have no yfinance
    counterpart to blend against (fwdEps2y passed through as-is,
    fwdRevenue2y turned into eulerRevGrowth2y alongside fwdRevenue1y),
    both feeding simulate_ticker's year-2 consensus drift nudge (an
    EPS-implied and a revenue-implied reading, averaged); and
    fwdRevenue1y/fwdRevenue0y - 1 (the ORIGINAL pair, no yfinance
    counterpart either -- only a TRAILING revenueGrowth exists there) is
    a genuinely new forward growth signal feeding ownGrowthRate/
    industryGrowthRate as a third leg. Same thread-pool-over-a-shared-
    rate-limit shape as fetch_analyst_grades/fetch_fair_values -- errors
    logged and skipped per-ticker rather than aborting the batch."""
    results = {}
    errors = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(get_forward_estimates, t): t for t in tickers}
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                fwd_eps0y, fwd_eps1y, fwd_eps2y, fwd_rev0y, fwd_rev1y, fwd_rev2y = fut.result()
                if any(v is not None for v in (fwd_eps0y, fwd_eps1y, fwd_eps2y, fwd_rev0y, fwd_rev1y, fwd_rev2y)):
                    results[ticker] = {
                        "fwdEps0y": fwd_eps0y, "fwdEps1y": fwd_eps1y, "fwdEps2y": fwd_eps2y,
                        "fwdRevenue0y": fwd_rev0y, "fwdRevenue1y": fwd_rev1y, "fwdRevenue2y": fwd_rev2y,
                    }
            except Exception as e:
                errors.append((ticker, str(e)))

    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    with open(out_file, "w") as f:
        json.dump(results, f)

    print(f"fetch_forward_eps: wrote {out_file} ({len(tickers)} requested, "
          f"{len(results)} with at least one value, {len(errors)} failed)")
    if errors:
        print("  failed:", ", ".join(t for t, _ in errors[:20]), "..." if len(errors) > 20 else "")
    return results


EPS_ESTIMATES_FILE = os.path.join("data", "eulerpool", "eps_estimates.json")


def fetch_eps_estimates(tickers, out_file=EPS_ESTIMATES_FILE, max_workers=4):
    """Fetch get_estimates for every ticker in `tickers`, OVERWRITE out_file
    with {ticker: {period: epsEstimate, ...}} -- ONLY the already-completed
    fiscal years (period <= today), keyed by ISO 'YYYY-MM-DD' fiscal-year-
    end date, the SAME shape modules.derive's own SEC/yfinance-sourced
    Diluted EPS series already uses (see eps_volatility_merged) so the two
    can be merged/compared the same way. get_estimates mixes past actuals
    and forward consensus in one array with no is-this-an-estimate flag
    (see that function's own docstring) -- the future rows are dropped
    here since this project already gets forward EPS from
    fetch_forward_eps; this file exists purely to give
    modules.derive.eps_volatility a THIRD historical-EPS source alongside
    SEC company_facts and yfinance's own income_stmt, one that (confirmed
    live, spot-checked against SEC GAAP diluted EPS for MSFT/AMZN) reads
    closer to a "Street"/adjusted EPS -- excluding at least some one-off
    items (AMZN's FY2022 Rivian mark-to-market loss: -$0.27 GAAP vs. -$0.12
    here) that inflate GAAP-based volatility without reflecting genuine
    earnings unpredictability. Coverage depth varies by ticker (spot-check:
    MSFT/AAPL/AMZN ~30+ periods, META/NVDA as few as 14-19) -- shorter than
    SEC's own history for some names, but likely still enough years for a
    meaningful volatility read, and cleaner of one-off noise. Same
    same-day-snapshot, full-overwrite (no merge, no staleness cooldown of
    its own) shape as fetch_forward_eps/fetch_fair_values -- get_estimates
    reads fresh each call, so there's no history here to preserve across
    runs on this end; the OUTPUT file itself IS the history. Same
    thread-pool-over-a-shared-rate-limit shape as the other fetch_*
    functions -- errors logged and skipped per-ticker rather than aborting
    the batch.

    Exact-zero epsEstimate values are dropped as missing-data placeholders,
    not genuine readings -- confirmed live, TPL (Texas Pacific Land, a
    hugely profitable royalty trust with no realistic path to a real
    $0.00 EPS year) has a 2024-12-31 epsEstimate of exactly 0 sandwiched
    between $50.69 (2023) and $6.98 (2025); scanning the full universe,
    316 (ticker, period) pairs across 216 tickers (12% of coverage) land
    on exactly 0, far too common to be genuine breakeven years and
    consistent with Eulerpool using 0 as a null placeholder for some
    periods. Left in, a zero corrupts eps_volatility twice over: it turns
    the transition INTO it into a false -100% YoY move, and the
    transition OUT of it gets silently dropped entirely (dividing by a
    zero prior is undefined -- see eps_volatility's own guard), losing a
    real data point on top of gaining a fake one."""
    today = date.today().isoformat()
    results = {}
    errors = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(get_estimates, t): t for t in tickers}
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                rows = fut.result() or []
                past = {
                    r["period"]: r["epsEstimate"]
                    for r in rows
                    if r.get("period") and r["period"] <= today and r.get("epsEstimate")
                }
                if past:
                    results[ticker] = past
            except Exception as e:
                errors.append((ticker, str(e)))

    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    with open(out_file, "w") as f:
        json.dump(results, f)

    print(f"fetch_eps_estimates: wrote {out_file} ({len(tickers)} requested, "
          f"{len(results)} with at least one historical value, {len(errors)} failed)")
    if errors:
        print("  failed:", ", ".join(t for t, _ in errors[:20]), "..." if len(errors) > 20 else "")
    return results


TRANSCRIPTS_FILE = os.path.join("data", "eulerpool", "transcripts.json")


def fetch_transcripts(tickers, out_file=TRANSCRIPTS_FILE, max_workers=4):
    """Fetches the LATEST earnings-call transcript for every ticker in
    `tickers`, MERGING into whatever's already on `out_file` -- unlike
    fetch_forward_eps/fetch_fair_values' full-overwrite (a same-day
    snapshot with nothing to preserve), a transcript is a genuine
    point-in-time historical record, so a ticker not in this run's
    `tickers` (or one Eulerpool has nothing new for) keeps whatever
    transcript is already cached rather than being dropped.

    {ticker: {id, datePublished, title, entries: [{speaker, content},
    ...]}} -- entries only (parsedContent's actual transcript body, see
    get_earning_call_transcript), not presentationUrl/transcriptAudioUrl,
    to keep this file's size sane; both are re-derivable via
    list_earning_calls/get_earning_call_transcript directly if ever
    needed.

    Two-step per ticker to avoid re-downloading a transcript that hasn't
    changed: list_earning_calls(ticker) first (cheap, no transcript body)
    to find the latest call's id, and only calls the heavier
    get_earning_call_transcript(id) when that id differs from what's
    already cached for this ticker -- earnings calls are quarterly, so
    most runs do nothing for most tickers. A ticker with no calls on
    file at all, or whose cached id is already current, contributes
    nothing this run (not an error) and its existing cache entry (if
    any) is left untouched."""
    existing = {}
    try:
        with open(out_file) as f:
            existing = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    def _fetch_one(ticker):
        calls = list_earning_calls(ticker)
        if not calls:
            return None
        latest = max(calls, key=lambda c: c.get("datePublished") or 0)
        cached = existing.get(ticker)
        if cached and cached.get("id") == latest.get("id"):
            return None  # already up to date
        full = get_earning_call_transcript(latest["id"])
        parsed = full.get("parsedContent") or {}
        return {
            "id": full.get("id"),
            "datePublished": full.get("datePublished"),
            "title": full.get("title"),
            "entries": parsed.get("entries") or [],
        }

    updated = 0
    errors = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch_one, t): t for t in tickers}
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                result = fut.result()
                if result is not None:
                    existing[ticker] = result
                    updated += 1
            except Exception as e:
                errors.append((ticker, str(e)))

    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    with open(out_file, "w") as f:
        json.dump(existing, f)

    print(f"fetch_transcripts: wrote {out_file} ({len(tickers)} requested, "
          f"{updated} new/updated transcript(s), {len(existing)} ticker(s) on file total, {len(errors)} failed)")
    if errors:
        print("  failed:", ", ".join(t for t, _ in errors[:20]), "..." if len(errors) > 20 else "")
    return existing
