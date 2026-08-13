"""
main.py — entry point. Owns the download pipeline and file I/O; every
individual scoring indicator/factor calculation (and score_rows itself,
which combines them) lives in scoring.py instead -- see that module's own
docstring for the full list of factor functions and their weights.

download_all():    fetch tickers from symbols.json (active == 1), pull forward/trailing
                    P/E + price-to-FCF from Yahoo Finance, then add the
                    regression-slope momentum score, and write raw_data.json +
                    forward_pe.csv + sorted_screen.csv. Tickers
                    downloaded within the last FRESH_HOURS hours are skipped
                    (their previous row is reused as-is), and tickers whose
                    fetch fails this run (e.g. a transient Yahoo Finance
                    error) fall back to their last successful row instead of
                    disappearing from the outputs. Right after raw_data.json
                    is written, also fetches StockTwits social sentiment
                    (see social_sentiment.py) for every RATED_FOR_EXTRAS
                    ticker (Strong Buy/Buy/Sell/Strong Sell -- everything
                    outside the broad Hold middle) of the ranking as it
                    stood before this run (sorted_screen.csv isn't
                    rewritten with fresh scores until later in the same
                    run), writing/merging into social_sentiment.json. This
                    is a separate, unofficial data source that can fail
                    without affecting the rest of the pipeline.
download_prices():  reuse the forward-PE data already in forward_pe.csv and only
                    refresh the momentum score, then rewrite forward_pe.csv +
                    sorted_screen.csv.
rescore():          rewrite sorted_screen.csv (and forward_pe.csv) from forward_pe.csv
                    already on disk with ZERO network calls -- not even the momentum
                    refresh download_prices() still does. For when only the scoring
                    itself changed (a scoring.py edit, a manual data fix, a newly
                    backfilled CSV column) and every field on disk is otherwise still
                    good. Run via `python main.py rescore`.
download_form4():   fetch SEC EDGAR Form 4 insider-transaction filings (see
                    sec_edgar.py) for every RATED_FOR_EXTRAS ticker, same
                    scoping as the social-sentiment fetch above -- a
                    separate, independently-rate-limited data source, run
                    on its own via `python main.py form4` rather than
                    folded into download_all.
download_xbrl():    fetch SEC EDGAR XBRL company facts (see sec_edgar.py) --
                    multi-year revenue/income/assets/equity/EPS history --
                    for every RATED_FOR_EXTRAS ticker, same scoping/
                    standalone-download reasoning as download_form4. Run via
                    `python main.py xbrl`.
download_13f():     fetch SEC's latest quarterly bulk 13F institutional-
                    holdings dataset (see sec_edgar.py) for every
                    RATED_FOR_EXTRAS ticker, matched by company name (13F
                    is filed BY managers ABOUT what they hold, not by the
                    issuer, so there's no per-ticker CIK the way Form 4/
                    XBRL have) -- a single bulk download, not one request
                    per ticker. Run via `python main.py 13f`.
download_themes():  classifies tickers' business descriptions
                    (raw_data.json's longBusinessSummary) against a fixed
                    theme taxonomy (see theme_classifier.py) for the
                    Themes tab. With no tickers given, classifies every
                    stock currently held in the IB Gateway account
                    instead (a fresh direct connection, not
                    RATED_FOR_EXTRAS -- this is a portfolio-holdings
                    tool, not a whole-screener-universe one). Run via
                    `python main.py themes` (whole portfolio) or `python
                    main.py themes TICKER [TICKER ...]` (specific
                    tickers only, e.g. right after opening one new
                    position).
download_recommendations(): rebuild data/recommendations.json for the Recommendations
                    tab (see recommendations.py) from sorted_screen.csv's score/
                    rating plus recent-window news/insider/13F signals already on
                    disk -- zero network calls, same as rescore(). Run via
                    `python main.py recommendations`.
run_chat():         ask the Recommendations tab's chatbot a single question with
                    no chat history/live data (see chatbot.answer_question) --
                    a fast, no-HTTP-layer way to test the tool set/system
                    prompt. Run via `python main.py chat "your question here"`.
download_symbols(): refetch forward-PE + price-performance data for a specific
                    list of tickers only (e.g. ones missing from the outputs
                    after a transient Yahoo Finance failure), merging into the
                    existing forward_pe.csv/raw_data.json, then rewrite
                    sorted_screen.csv. Run via
                    `python main.py symbol TICKER [TICKER ...]`.

Writes (JSON outputs under DATA_DIR ("data/"); CSVs and symbols.json, a
hand-maintained input rather than generated output, stay at the project
root):
  data/raw_data.json  the complete, unfiltered yfinance `info` payload per
                     ticker (every field Yahoo exposes), for discovering
                     fields not yet curated into forward_pe.csv.
  forward_pe.csv    all tickers, sorted by forwardPE ascending.
  sorted_screen.csv screen_rows(data) (tickers with positive forwardPE —
                     negative or missing priceToFCF is kept, not excluded)
                     filtered to price >= $8, ranked by a composite score:
                     5% low forwardPE (down from 10%, moved to the EPS
                     trend factor below), 10% low forwardPE relative to its
                     sector's average forwardPE, 5% low priceToFCF
                     (negative or missing FCF treated as a fixed 200 for
                     this factor only) + 5% low enterpriseToEbitda
                     (negative EBITDA ranked worst instead -- two
                     independent cash-flow-valuation factors; see
                     scoring.ev_ebitda_rank), 5% high daily-timeframe momentum
                     (regression-slope momentum score, divided by the
                     annualized volatility of daily log returns -- the
                     3-month IB Gateway daily series where available,
                     else the plain ~1-month yfinance calculation; see
                     IBApp.get_momentum; missing penalized as worst) + 5%
                     low hourly-timeframe mean reversion (the SAME
                     regression-momentum formula as the daily momentum
                     factor above, just on IB Gateway's hourly series --
                     a short-term trend/momentum reading treated as an
                     entry-timing signal, not a second momentum vote: a
                     stock already trending up hard on the hour is one
                     you'd be chasing, so LOW/negative (a stock that's
                     just pulled back) ranks best, the mirror of the
                     daily momentum factor's own "high is better"
                     direction; only populated for the CANDLESTICK_TOP_N
                     ranked/held tickers IB Gateway fetches hourly bars
                     for, no fallback source, missing penalized as worst)
                     -- two independent factors, not blended into one the
                     way this used to work, 5% EPS
                     trend (eps_trend_rank -- average of the current- and
                     next-fiscal-year 30-day consensus EPS estimate
                     revision ranks, from yfinance's get_eps_trend(); see
                     IBApp.get_forward_pe/_eps_revision; missing penalized
                     as worst),
                     10% analyst conviction — the average of high
                     targetUpside, low recommendationMean, and low
                     target-price dispersion ((high-low)/mean) ranks
                     (negative upside, a 0 or missing recommendationMean,
                     and a missing/inconsistent target triple, all
                     penalized as worst; dispersion catches real analyst
                     disagreement the mean alone hides), 5% based on
                     forwardPE - trailingPE when trailingPE is positive and
                     finite (infinite or negative trailingPE — the company
                     lost money over the trailing twelve months — penalized
                     as worst for this factor instead of masked behind a
                     placeholder) — down from 10%, moved to analyst
                     conviction above, 5% low pegRatio (negative PEG penalized as
                     worst, not treated as "low"), 2.5% low trailingPS (price /
                     trailing-twelve-month revenue; missing penalized as worst) —
                     a separate valuation lens from forwardPE/priceToFCF/
                     enterpriseToEbitda, not blended with any of them, that
                     stays meaningful for unprofitable/negative-FCF names those
                     break down for (revenue is essentially never negative,
                     unlike earnings/FCF/EBITDA) — taken out of liquidity's
                     weight below, 7.5% high revenueGrowth
                     (negative growth penalized as worst, not treated as
                     "low" — down from 10%, moved to margins below), 5% low
                     debtToEquity relative to its sector's
                     average debtToEquity (negative or missing debtToEquity
                     penalized as worst, same treatment as pegRatio), 2.5%
                     liquidity — the average of high quickRatio and high
                     currentRatio ranks (missing penalized as worst — down
                     from 5%, moved to trailingPS above), 5%
                     high returnOnEquity (negative ROE penalized as worst,
                     not treated as "low", same treatment as revenueGrowth),
                     5% short interest — the average of high shortRatio and
                     high shortPercentOfFloat ranks (missing penalized as
                     worst) — deliberately contrarian: the more a stock is
                     shorted, the better it scores here. 5% combined news +
                     social sentiment (StockTwits' social_sentiment.json
                     blended with FinBERT-scored headlines in
                     news_sentiment.json — see load_sentiment_scores;
                     missing penalized as worst) — taken out of forwardPE's
                     own weight, previously 15%. 7.5% margins — the average
                     of high profitMargins and high operatingMargins ranks
                     (negative margins penalized as worst, same treatment
                     as revenueGrowth/ROE) — up from 5%, the other 2.5%
                     taken out of revenueGrowth's weight above.
                     Sorted best (lowest score) first. Also carries a
                     `rating` column: a forced-distribution Strong Buy/Buy/
                     Hold/Sell/Strong Sell label from this file's own score
                     percentile (top/bottom 5% = Strong Buy/Strong Sell,
                     next 15% each = Buy/Sell, middle 60% = Hold — same
                     shape as Zacks Rank's bucketing, unlike Wall Street's
                     own analyst consensus, which skews heavily toward
                     "Buy" since sell-side analysts rarely publish Sell
                     ratings). Symmetric bucket sizes guarantee equal
                     Strong Buy / Strong Sell counts (up to rounding).
                     Every priced (>= MIN_PRICE) ticker with a non-positive
                     forwardPE is also appended after the ranked rows, for
                     visibility only -- blank score, rating "NA", alphabetical
                     order, and never picked up by load_top_tickers (so
                     never streamed/snapshotted a live IB price either) --
                     see write_sorted_screen_csv.
  data/social_sentiment.json  {ticker: {bullish, bearish, tagged, total, score,
                     lastDownload}} StockTwits sentiment for every
                     RATED_FOR_EXTRAS ranked ticker; score is
                     (bullish - bearish) / tagged. Merged across runs, so
                     a ticker that drifts into Hold keeps its last known
                     score rather than being deleted.
  data/sec/form4/insider_transactions.json  see sec_edgar.py's own
                     docstring -- SEC EDGAR Form 4 insider-transaction
                     filings for every RATED_FOR_EXTRAS ranked ticker,
                     merged across runs same as social_sentiment.json.
  data/sec/xbrl/company_facts.json  see sec_edgar.py's own docstring --
                     multi-year revenue/income/assets/equity/EPS history
                     from SEC EDGAR XBRL filings, same RATED_FOR_EXTRAS
                     scoping and merge-across-runs behavior.
  data/sec/13f/institutional_holdings.json  see sec_edgar.py's own
                     docstring -- institutional ownership (total value/
                     shares/holder count) per RATED_FOR_EXTRAS ticker from
                     SEC's latest quarterly bulk 13F dataset, matched by
                     company name rather than CIK. Overwritten wholesale
                     each run, unlike the merge-across-runs files above.
  data/price_history.json  {ticker: [{date, close}, ...]} trailing ~1 month of
                     daily closes, captured from the same yfinance fetch
                     add_momentum already makes for the momentum score (see
                     IBApp.get_momentum) — no extra network round-trip.
                     Each ticker's series is replaced wholesale on its next
                     fetch (a rolling window, not an accumulated archive).
"""

import csv
import json
import os
import sys
from datetime import datetime, timedelta

from IBApp import IBApp
from scoring import (
    RATING_NA,
    add_avg_liquidity_ratio,
    add_target_upside,
    load_insider_scores,
    load_sentiment_scores,
    rating_for_percentile,
    score_rows,
    to_float,
)
from chatbot import answer_question
from recommendations import write_recommendations
from sec_edgar import FORM4_FILE, THIRTEENF_FILE, fetch_13f_holdings, fetch_form4, fetch_xbrl_facts
from social_sentiment import SENTIMENT_FILE, fetch_social_sentiment
from theme_classifier import classify_themes

# Every JSON file a downloader (this module, ib_price_server.py,
# social_sentiment.py) produces lives here -- keeps the project root from
# filling up with generated output. CSVs (forward_pe.csv, sorted_screen.csv)
# and symbols.json (a hand-maintained input, not generated) deliberately
# stay at the root; only the JSON downloader outputs moved. Created on
# import so a fresh checkout doesn't need a manual mkdir before the first
# run.
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

OUTPUT_CSV = "forward_pe.csv"
SORTED_SCREEN_CSV = "sorted_screen.csv"
RAW_DATA_FILE = os.path.join(DATA_DIR, "raw_data.json")
PRICE_HISTORY_FILE = os.path.join(DATA_DIR, "price_history.json")
# Written separately by ib_price_server.py (IB Gateway's own bars, not
# yfinance's), covering only CANDLESTICK_TOP_N ranked/held tickers, not
# the whole universe -- see IBApp.get_momentum, which blends these in for
# whatever ticker they cover and falls back to the plain yfinance
# calculation for everything else. Read-only from here: this pipeline
# doesn't write either file, and doesn't require ib_price_server.py (or
# IB Gateway) to be running -- if they're missing or stale, get_momentum
# just falls back for every ticker, same as before this existed.
DAILY_3MO_HISTORY_FILE = os.path.join(DATA_DIR, "price_history_daily_3mo.json")
HOURLY_HISTORY_FILE = os.path.join(DATA_DIR, "price_history_hourly.json")
# Also written by ib_price_server.py's news_loop (see that module's
# NEWS_SENTIMENT_FILE) -- duplicated here rather than imported since
# ib_price_server.py itself imports SORTED_SCREEN_CSV/load_top_tickers
# from this module, and importing it back would be circular. Read-only
# from here, same as the price-history files above.
NEWS_SENTIMENT_FILE = os.path.join(DATA_DIR, "news_sentiment.json")
# Same duplicated-rather-than-imported reasoning as NEWS_SENTIMENT_FILE above.
NEWS_FILE = os.path.join(DATA_DIR, "news.json")
SYMBOLS_FILE = "symbols.json"
MIN_PRICE = 8
# Strong Buy/Buy/Sell/Strong Sell -- everything except the broad Hold
# middle (60% of the ranked universe, see scoring.RATING_THRESHOLDS) and
# the unranked/NA rows -- scopes both the social-sentiment fetch and the
# SEC EDGAR downloads to names with enough conviction (in either
# direction) to be worth the extra network cost, rather than a flat
# top-N cutoff.
RATED_FOR_EXTRAS = {"Strong Buy", "Buy", "Sell", "Strong Sell"}
# get_forward_pe's usa_only filter would otherwise silently drop every
# ticker here, for two different reasons:
#  - CRSP: yfinance mislabels it foreign-domiciled (reincorporated abroad)
#    despite being an ordinary US-listed, US-focused security -- a data
#    error worth correcting.
#  - ARM/ASML/BIRK/NBIS/ONON: genuinely foreign-domiciled (UK/Netherlands/
#    Switzerland), but explicit instruction is to include ADRs/foreign
#    ordinary-share US-exchange listings in the screener anyway rather
#    than exclude on domicile alone -- not a data error, a deliberate
#    scope choice. LEGN isn't here: yfinance already reports it as US
#    (Legend Biotech Corporation), so usa_only never drops it in the
#    first place; it only needed adding to symbols.json.
# Add a ticker here only after confirming by hand what it actually is --
# genuinely US-focused (CRSP-style) or a foreign name being deliberately
# included (ARM-style) -- not just because usa_only happened to drop it.
COUNTRY_OVERRIDE_TICKERS = {"ARM", "ASML", "BIRK", "CRSP", "NBIS", "ONON"}

FIELDNAMES = [
    "ticker", "name", "sector", "forwardPE", "forwardEps", "trailingPE", "trailingPS", "pegRatio", "priceToFCF",
    "enterpriseToEbitda", "beta", "debtToEquity", "LiqRatio", "quickRatio", "currentRatio", "shortRatio", "shortPercentOfFloat",
    "revenueGrowth", "returnOnEquity", "profitMargins", "operatingMargins", "price",
    "targetMeanPrice", "targetHighPrice", "targetLowPrice", "targetUpside", "recommendationKey",
    "recommendationMean", "numberOfAnalystOpinions", "momentum", "meanReversion", "epsRevision0y",
    "epsRevision1y", "earningsTimestampStart", "lastDownload",
]
# sorted_screen.csv shows sector last instead of right after name.
SCREEN_FIELDNAMES = [f for f in FIELDNAMES if f != "sector"] + ["sector"]


def load_tickers(path):
    with open(path) as f:
        symbols = json.load(f)
    return sorted({
        s["symbol"].strip().upper()
        for s in symbols
        if s.get("active") == 1 and s.get("symbol")
    })


def load_sectors(path):
    """Reads {ticker: sector} for active symbols that have a curated sector in
    symbols.json. This takes precedence over IBApp's live yfinance lookup, since
    it's where manual corrections (e.g. reclassifying a ticker) are kept."""
    with open(path) as f:
        symbols = json.load(f)
    return {
        s["symbol"].strip().upper(): s["sector"]
        for s in symbols
        if s.get("active") == 1 and s.get("symbol") and s.get("sector")
    }


def apply_sector_overrides(data, sectors):
    for symbol, d in data.items():
        if symbol in sectors:
            d["sector"] = sectors[symbol]


def load_pe_data(path):
    """Reads an existing forward_pe.csv into {ticker: {field: value}}."""
    data = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            data[row["ticker"]] = dict(row)
    return data


def load_top_tickers(path, n=None):
    """Reads the first n tickers from an existing sorted_screen.csv (already
    ranked best-to-worst by score), or every ranked ticker in the file if n
    is None. Used to scope the social-sentiment download to the top of the
    ranking as it stood before this run started, since this run's own
    scores aren't computed until later -- and, unbounded, by
    ib_price_server.py to decide which tickers get a live/snapshot IB
    price at all.

    Skips any row with no `score` -- see write_sorted_screen_csv, which
    appends negative-forwardPE tickers at the end of the file, unscored
    and unranked, for visibility in the Screener only. Unranked isn't
    "ranked last"; it's not part of this ranking, so it shouldn't count
    toward a top-N slice or (for ib_price_server.py's unbounded calls)
    ever be treated as part of the live-priced universe.

    Returns [] if the file doesn't exist yet (e.g. first-ever run)."""
    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            scored = (row for row in reader if row.get("score"))
            if n is None:
                return [row["ticker"] for row in scored]
            return [row["ticker"] for _, row in zip(range(n), scored)]
    except FileNotFoundError:
        return []


def load_rated_tickers(path, ratings):
    """Reads tickers from an existing sorted_screen.csv whose `rating`
    column (see scoring.rating_for_percentile) is one of `ratings` -- e.g.
    RATED_FOR_EXTRAS, every Strong Buy/Buy/Sell/Strong Sell ticker,
    skipping the broad Hold middle and the unranked/NA rows. Returns []
    if the file doesn't exist yet (e.g. first-ever run)."""
    try:
        with open(path, newline="") as f:
            return [row["ticker"] for row in csv.DictReader(f) if row.get("rating") in ratings]
    except FileNotFoundError:
        return []


def _load_json_or_empty(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def add_momentum(app, data, history_out=None):
    """Adds momentum + meanReversion (see IBApp.get_momentum -- momentum
    from DAILY_3MO_HISTORY_FILE where it covers a ticker, else the plain
    trailing-~1-month yfinance calculation; meanReversion from
    HOURLY_HISTORY_FILE only, no fallback) to each entry in data, in
    place. If history_out is a dict, also captures each ticker's daily
    close series from that same yfinance fetch (see write_price_history)
    — no extra network round-trip since IBApp.get_momentum already pulls
    it."""
    momentum = app.get_momentum(
        list(data.keys()),
        history_out=history_out,
        daily_3mo_by_ticker=_load_json_or_empty(DAILY_3MO_HISTORY_FILE),
        hourly_by_ticker=_load_json_or_empty(HOURLY_HISTORY_FILE),
    )
    for symbol, d in data.items():
        result = momentum.get(symbol) or {}
        d["momentum"] = result.get("momentum")
        d["meanReversion"] = result.get("mean_reversion")


def add_momentum_and_persist_history(app, data):
    """add_momentum, plus merging the close-series it captures into
    price_history.json — the common case across all three download_*
    entry points."""
    history = {}
    add_momentum(app, data, history_out=history)
    try:
        with open(PRICE_HISTORY_FILE) as f:
            all_history = json.load(f)
    except FileNotFoundError:
        all_history = {}
    all_history.update(history)
    write_price_history(all_history)


FRESH_HOURS = 8


def is_fresh(last_download, max_age_hours=FRESH_HOURS):
    """True if last_download (an ISO datetime string, e.g. from a previous
    forward_pe.csv row) is within max_age_hours of now. Used to skip
    re-fetching tickers that were already downloaded recently."""
    if not last_download:
        return False
    try:
        dt = datetime.fromisoformat(last_download)
    except ValueError:
        return False
    return datetime.now() - dt < timedelta(hours=max_age_hours)


def fill_missing_from_previous(data, tickers, previous=None):
    """For active tickers missing from this run's fetch (e.g. a transient
    Yahoo Finance error), fall back to their last successfully fetched row
    in forward_pe.csv instead of silently dropping them from every output.
    Tickers no longer in the active list are left out either way."""
    missing = [t for t in tickers if t not in data]
    if not missing:
        return
    if previous is None:
        try:
            previous = load_pe_data(OUTPUT_CSV)
        except FileNotFoundError:
            previous = {}
    kept = 0
    for t in missing:
        if t in previous:
            data[t] = previous[t]
            kept += 1
    print(f"{len(missing)} tickers missing from this fetch; kept {kept} from previous {OUTPUT_CSV}")


def write_raw_data(raw_info):
    """Writes the complete, unfiltered yfinance `info` payload per ticker —
    every field Yahoo Finance exposes, not just the ones curated into
    forward_pe.csv. Useful for discovering fields to add later."""
    with open(RAW_DATA_FILE, "w") as f:
        json.dump(raw_info, f, indent=2, default=str)
    print(f"Wrote {RAW_DATA_FILE}")


def write_price_history(history):
    """Writes {ticker: [{date, close}, ...]} — the trailing ~1 month of
    daily closes captured alongside the momentum fetch (see add_momentum),
    for charting. Overwritten with each ticker's latest fetch rather than
    accumulated, since yfinance's period="1mo" call is already a rolling
    window, not an appending history."""
    with open(PRICE_HISTORY_FILE, "w") as f:
        json.dump(history, f)
    print(f"Wrote {PRICE_HISTORY_FILE}")


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(fieldnames)
        for symbol, d in rows:
            writer.writerow([symbol] + [d.get(field, "") for field in fieldnames[1:]])
    print(f"Wrote {path}")


def write_full_csv(data):
    rows = sorted(
        data.items(),
        key=lambda item: (
            to_float(item[1].get("forwardPE")) is None,
            to_float(item[1].get("forwardPE")) or 0,
        ),
    )
    write_csv(OUTPUT_CSV, FIELDNAMES, rows)


def screen_rows(data):
    """Tickers with positive forwardPE. Negative or missing priceToFCF
    (negative or unavailable free cash flow) is kept, not excluded —
    score_rows penalizes it there instead by treating it as 200."""
    filtered = []
    for symbol, d in data.items():
        fwd_pe = to_float(d.get("forwardPE"))
        if fwd_pe is not None and fwd_pe > 0:
            filtered.append((symbol, d))
    return filtered


def write_sorted_screen_csv(data):
    """screen_rows() filtered to price >= MIN_PRICE, ranked by score ascending
    (best first). Also assigns each row a `rating` from its percentile
    position in this ranking -- see rating_for_percentile.

    Followed by every other priced (>= MIN_PRICE) ticker with a non-positive
    forwardPE -- shown for visibility in the Screener only, appended after
    every real ranked row with a blank score and rating RATING_NA ("NA")
    rather than scored alongside them: forwardPE feeds three separate
    scoring factors (its own rank, sector-relative rank, and the
    forwardPE-vs-trailingPE diff), and a
    negative value would corrupt all three under naive ascending-is-better
    ranking (most negative sorting as "cheapest"/best, the opposite of what
    it means). Simpler and safer to keep them out of scoring entirely than
    to special-case every factor that touches forwardPE. load_top_tickers
    skips these blank-score rows, so they also never enter the live/
    snapshot IB price universe ib_price_server.py builds from this file."""
    rows = [(s, d) for s, d in screen_rows(data) if (to_float(d.get("price")) or 0) >= MIN_PRICE]
    sentiment_scores = load_sentiment_scores(SENTIMENT_FILE, NEWS_SENTIMENT_FILE, THIRTEENF_FILE)
    insider_scores = load_insider_scores(FORM4_FILE)
    scored = sorted(score_rows(rows, sentiment_scores, insider_scores), key=lambda item: item[2])
    n = len(scored)

    scored_symbols = {s for s, _, _ in scored}
    unranked = [
        (s, d)
        for s, d in data.items()
        if s not in scored_symbols
        and (to_float(d.get("price")) or 0) >= MIN_PRICE
        and (fwd_pe := to_float(d.get("forwardPE"))) is not None
        and fwd_pe <= 0
    ]
    unranked.sort(key=lambda item: item[0])  # alphabetical -- nothing else to rank them by

    fieldnames = SCREEN_FIELDNAMES + ["score", "rating"]
    with open(SORTED_SCREEN_CSV, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(fieldnames)
        for i, (symbol, d, score) in enumerate(scored):
            rating = rating_for_percentile(i / n) if n else ""
            writer.writerow([symbol] + [d.get(field, "") for field in SCREEN_FIELDNAMES[1:]] + [score, rating])
        for symbol, d in unranked:
            writer.writerow([symbol] + [d.get(field, "") for field in SCREEN_FIELDNAMES[1:]] + ["", RATING_NA])
    print(f"Wrote {SORTED_SCREEN_CSV}: {len(scored)} ranked + {len(unranked)} unranked (negative forwardPE) ticker(s)")

    # Every caller of write_sorted_screen_csv (download_all, download_prices,
    # rescore, download_symbols) must also refresh data/recommendations.json
    # right after -- explicit instruction, after a real staleness bug: a
    # ticker's rating/score/momentum can drift between one sorted_screen.csv
    # write and the next `python main.py recommendations` run (they used to
    # be separate, easy-to-forget steps), and until that catches up,
    # RecommendationsView.jsx's Long/Short lists and the "Strong Buy/Sell —
    # blocked" audit rank and gate candidates against the STALE snapshot
    # baked into recommendations.json, not today's real numbers -- silently
    # wrong rather than erroring. Confirmed live: DINO's rank in the Long
    # pool was 41st against a several-hours-stale recommendations.json,
    # 8th once rebuilt from the sorted_screen.csv just written above.
    # write_recommendations is the same zero-network "just recompute from
    # files already on disk" operation download_recommendations() wraps for
    # the CLI (`python main.py recommendations`) -- safe to always chain
    # here, not just run on request.
    write_recommendations(SORTED_SCREEN_CSV, NEWS_FILE, FORM4_FILE, THIRTEENF_FILE, RATED_FOR_EXTRAS)


def download_all():
    """Full pipeline: fetch forward P/E data from Yahoo Finance, then the momentum score.
    Tickers downloaded within the last FRESH_HOURS hours are skipped and their
    previous forward_pe.csv row is reused as-is, rather than re-fetched."""
    app = IBApp()
    tickers = load_tickers(SYMBOLS_FILE)
    print(f"Loaded {len(tickers)} active tickers from {SYMBOLS_FILE}")

    try:
        previous = load_pe_data(OUTPUT_CSV)
    except FileNotFoundError:
        previous = {}
    stale = [t for t in tickers if not is_fresh(previous.get(t, {}).get("lastDownload"))]
    fresh = len(tickers) - len(stale)
    if fresh:
        print(f"Skipping {fresh} tickers downloaded within the last {FRESH_HOURS}h")

    raw_info = {}
    fetched = app.get_forward_pe(stale, usa_only=True, raw_out=raw_info, country_overrides=COUNTRY_OVERRIDE_TICKERS)
    print(f"{len(fetched)} are USA-domiciled with Yahoo Finance data")

    data = {t: previous[t] for t in tickers if t not in stale and t in previous}
    data.update(fetched)
    fill_missing_from_previous(data, tickers, previous)

    try:
        with open(RAW_DATA_FILE) as f:
            all_raw = json.load(f)
    except FileNotFoundError:
        all_raw = {}
    all_raw.update(raw_info)
    write_raw_data(all_raw)

    # Scoped to the ranking as it stood before this run (sorted_screen.csv
    # isn't rewritten with fresh scores until below) — see load_rated_tickers.
    rated_tickers = load_rated_tickers(SORTED_SCREEN_CSV, RATED_FOR_EXTRAS)
    if rated_tickers:
        fetch_social_sentiment(rated_tickers)
    else:
        print(f"No existing {SORTED_SCREEN_CSV} yet; skipping social sentiment download")

    apply_sector_overrides(data, load_sectors(SYMBOLS_FILE))
    add_momentum_and_persist_history(app, data)
    add_target_upside(data)
    add_avg_liquidity_ratio(data)
    write_full_csv(data)
    write_sorted_screen_csv(data)


def download_prices():
    """Reuse forward-PE data already in forward_pe.csv; only refresh the momentum score."""
    app = IBApp()
    data = load_pe_data(OUTPUT_CSV)
    print(f"Loaded {len(data)} tickers from {OUTPUT_CSV}")

    apply_sector_overrides(data, load_sectors(SYMBOLS_FILE))
    add_momentum_and_persist_history(app, data)
    add_target_upside(data)
    add_avg_liquidity_ratio(data)
    write_full_csv(data)
    write_sorted_screen_csv(data)


def rescore():
    """Rewrites sorted_screen.csv (and forward_pe.csv, for the reapplied
    sector overrides/derived fields) purely from forward_pe.csv already on
    disk -- zero network calls, unlike download_prices (which still hits
    yfinance once per ticker to refresh the momentum score) or download_all.
    momentum/meanReversion are left exactly as forward_pe.csv already has
    them; run `python main.py prices` (or `all`) instead if those need
    refreshing too.

    For when only the scoring itself changed (a scoring.py formula/weight
    edit, a manual data fix, a newly backfilled CSV column) and every
    other field on disk is still perfectly good -- score_rows,
    add_target_upside, add_avg_liquidity_ratio, and write_sorted_screen_csv
    are all pure computation over data already in memory, so there's
    nothing here that needs a live fetch."""
    data = load_pe_data(OUTPUT_CSV)
    print(f"Loaded {len(data)} tickers from {OUTPUT_CSV}")

    apply_sector_overrides(data, load_sectors(SYMBOLS_FILE))
    add_target_upside(data)
    add_avg_liquidity_ratio(data)
    write_full_csv(data)
    write_sorted_screen_csv(data)
    print(f"Rescored and wrote {SORTED_SCREEN_CSV} (and {OUTPUT_CSV}) -- no network calls made.")


def download_form4():
    """Fetches SEC EDGAR Form 4 insider-transaction filings (see
    sec_edgar.fetch_form4) for every RATED_FOR_EXTRAS ticker in the
    ranking as it currently stands on disk. A separate download, run on
    its own via `python main.py form4` rather than folded into
    download_all -- it hits a different rate-limited external service
    (SEC EDGAR, not Yahoo Finance) on its own schedule, same reasoning as
    social_sentiment.py being a standalone fetch."""
    tickers = load_rated_tickers(SORTED_SCREEN_CSV, RATED_FOR_EXTRAS)
    if not tickers:
        print(f"No existing {SORTED_SCREEN_CSV} yet; run `python main.py all` first")
        return
    fetch_form4(tickers)


def download_xbrl():
    """Fetches SEC EDGAR XBRL company facts (see sec_edgar.fetch_xbrl_facts)
    -- multi-year revenue/income/assets/equity/EPS history -- for every
    RATED_FOR_EXTRAS ticker, same scoping and same standalone-download
    reasoning as download_form4 above. Run via `python main.py xbrl`."""
    tickers = load_rated_tickers(SORTED_SCREEN_CSV, RATED_FOR_EXTRAS)
    if not tickers:
        print(f"No existing {SORTED_SCREEN_CSV} yet; run `python main.py all` first")
        return
    fetch_xbrl_facts(tickers)


def download_13f():
    """Fetches SEC's latest quarterly bulk 13F institutional-holdings
    dataset (see sec_edgar.fetch_13f_holdings) for every RATED_FOR_EXTRAS
    ticker, matched by company name rather than CIK -- 13F is filed BY
    institutional managers ABOUT what they hold, not by the issuer, so
    there's no per-ticker CIK to query the way Form 4/XBRL have; see that
    function's own docstring. A single ~90MB bulk download covering every
    filer at once, not one request per ticker. Run via `python main.py
    13f`."""
    try:
        with open(SORTED_SCREEN_CSV, newline="") as f:
            ticker_names = {
                row["ticker"]: row["name"]
                for row in csv.DictReader(f)
                if row.get("rating") in RATED_FOR_EXTRAS and row.get("name")
            }
    except FileNotFoundError:
        ticker_names = {}
    if not ticker_names:
        print(f"No existing {SORTED_SCREEN_CSV} yet; run `python main.py all` first")
        return
    fetch_13f_holdings(ticker_names)


def download_recommendations():
    """Rebuilds data/recommendations.json for the Recommendations tab (see
    recommendations.py's own docstring) purely from files already on disk
    -- sorted_screen.csv's score/rating, data/news.json, SEC EDGAR Form 4 (
    sec_edgar.FORM4_FILE), and the latest 13F quarter (sec_edgar.
    THIRTEENF_FILE) -- zero network calls, same "just recompute" reasoning
    as rescore(). Run via `python main.py recommendations` any time after
    the pieces it reads from have been refreshed (`all`/`prices`, `form4`,
    `13f`)."""
    write_recommendations(SORTED_SCREEN_CSV, NEWS_FILE, FORM4_FILE, THIRTEENF_FILE, RATED_FOR_EXTRAS)


def _get_held_tickers():
    """Every stock ticker currently held in the IB Gateway account, for
    download_themes' no-arguments case. Connects to IB Gateway directly
    (same IBApp.connect() pattern download_all/download_prices already
    use), not through ib_price_server.py's HTTP API -- that's a separate
    process that may or may not be running, whereas a direct connection
    is the one pattern every other IB-touching function in this file
    already relies on. secType == "STK" only (matching this project's
    "stocks only" convention elsewhere, e.g. ib_price_server.py's own
    docstring): an option and its underlying share a ticker symbol,
    which this doesn't disambiguate."""
    app = IBApp()
    app.connect()
    positions = app.ib.reqPositions()
    app.ib.disconnect()
    return sorted({p.contract.symbol for p in positions if p.contract.secType == "STK" and p.position != 0})


def download_themes(tickers=None):
    """Classifies tickers' business descriptions against the theme
    taxonomy (see theme_classifier.classify_themes) for the Themes tab.
    With no tickers given, classifies every stock currently held in the
    IB Gateway account instead (see _get_held_tickers) -- run via
    `python main.py themes` with no arguments to recompute for the whole
    portfolio, or `python main.py themes TICKER [TICKER ...]` for
    specific ones (e.g. right after opening a brand new position, when
    you don't want to wait on a full account query for just one
    ticker)."""
    if tickers:
        tickers = sorted({t.strip().upper() for t in tickers})
    else:
        tickers = _get_held_tickers()
        print(f"No tickers given -- classifying all {len(tickers)} currently held ticker(s): {', '.join(tickers)}")
    classify_themes(tickers)


def run_chat(question):
    """Manual, no-HTTP-layer way to test the Recommendations tab's chatbot
    (see chatbot.answer_question) -- a single question, no chat history, no
    live positions/prices/account (those only exist inside the running
    ib_price_server.py process; see that module's /api/chat handler for
    the real thing). Fast iteration on the tool set/system prompt without
    restarting the server or going through the browser. Run via `python
    main.py chat "your question here"`."""
    print(answer_question(question))


def download_symbols(symbols):
    """Refetch forward-PE + price-performance data for specific tickers only
    (e.g. ones that hit a transient Yahoo Finance error during a full run
    and are silently missing from every output), merging the results into
    the existing forward_pe.csv/raw_data.json rather than refetching the
    whole universe, then rewriting sorted_screen.csv."""
    symbols = sorted({s.strip().upper() for s in symbols})
    app = IBApp()

    raw_info = {}
    fetched = app.get_forward_pe(symbols, usa_only=True, raw_out=raw_info, country_overrides=COUNTRY_OVERRIDE_TICKERS)
    print(f"Fetched {len(fetched)}/{len(symbols)} requested tickers")
    missing = sorted(set(symbols) - set(fetched))
    if missing:
        print(f"No USA-domiciled Yahoo Finance data for: {missing}")

    try:
        with open(RAW_DATA_FILE) as f:
            all_raw = json.load(f)
    except FileNotFoundError:
        all_raw = {}
    all_raw.update(raw_info)
    write_raw_data(all_raw)

    apply_sector_overrides(fetched, load_sectors(SYMBOLS_FILE))
    add_momentum_and_persist_history(app, fetched)
    add_target_upside(fetched)
    add_avg_liquidity_ratio(fetched)

    data = load_pe_data(OUTPUT_CSV)
    data.update(fetched)
    write_full_csv(data)
    write_sorted_screen_csv(data)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "prices"
    if mode == "all":
        download_all()
    elif mode == "prices":
        download_prices()
    elif mode == "rescore":
        rescore()
    elif mode == "form4":
        download_form4()
    elif mode == "xbrl":
        download_xbrl()
    elif mode == "13f":
        download_13f()
    elif mode == "themes":
        download_themes(sys.argv[2:] if len(sys.argv) > 2 else None)
    elif mode == "recommendations":
        download_recommendations()
    elif mode == "chat":
        if len(sys.argv) < 3:
            sys.exit('Usage: python main.py chat "your question here"')
        run_chat(" ".join(sys.argv[2:]))
    elif mode == "symbol":
        if len(sys.argv) < 3:
            sys.exit("Usage: python main.py symbol TICKER [TICKER ...]")
        download_symbols(sys.argv[2:])
    else:
        sys.exit(
            f"Unknown mode {mode!r}, expected 'all', 'prices', 'rescore', 'form4', 'xbrl', '13f', 'themes', "
            "'recommendations', 'chat', or 'symbol'"
        )
