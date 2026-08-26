"""
main.py — entry point. Owns the download pipeline and file I/O; every
individual scoring indicator/factor calculation (and score_rows itself,
which combines them) lives in scoring.py instead -- see that module's own
docstring for the full list of factor functions and their weights.

download_all():    fetch tickers from symbols.json (active == 1), pull forward/trailing
                    P/E + price-to-FCF from Yahoo Finance, and concurrently (as
                    two asyncio tasks on one background thread, started right
                    away and joined before scoring -- see _refresh_ib_daily_
                    and_hourly/_run_ib_bar_refresh_in_background) refresh IB
                    Gateway's own daily AND hourly bars for the WHOLE active
                    universe (see refresh_ib_daily_history/refresh_ib_hourly_
                    history -- best-effort, skipped entirely if IB Gateway
                    isn't reachable), then add the Money Flow Index strength/
                    overbought-oversold scores (preferring that IB coverage
                    over the plain yfinance RSI fallback -- daily only;
                    hourly has no fallback -- wherever it exists), and write
                    raw_data.json + forward_pe.csv + sorted_screen.csv.
                    Tickers downloaded within the last FRESH_HOURS hours are
                    skipped (their previous row is reused as-is), and
                    tickers whose fetch fails this run (e.g. a transient
                    Yahoo Finance error) fall back to their last successful
                    row instead of disappearing from the outputs. Right
                    after raw_data.json is written, also fetches StockTwits
                    social sentiment (see social_sentiment.py) for every
                    RATED_FOR_EXTRAS ticker (Strong Buy/Buy/Sell/Strong Sell
                    -- everything outside the broad Hold middle) of the
                    ranking as it stood before this run (sorted_screen.csv
                    isn't rewritten with fresh scores until later in the
                    same run), writing/merging into social_sentiment.json.
                    This is a separate, unofficial data source that can
                    fail without affecting the rest of the pipeline.
download_prices():  reuse the forward-PE data already in forward_pe.csv and
                    refresh the momentum score built from whatever IB Gateway's
                    own daily/hourly bars (and yfinance's price_history.json
                    fallback) currently have on disk, then rewrite forward_pe.csv
                    + sorted_screen.csv. Does NOT refresh IB Gateway's own bars
                    itself -- unlike download_all, this one stays a quick,
                    connection-free rescore; run `ibprices`/`ibhprices` (or `all`)
                    first if those need refreshing too.
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
download_short_interest(): fetch FINRA's latest biweekly equity short
                    interest settlement file (see finra.py) for every
                    RATED_FOR_EXTRAS ticker, same scoping/standalone-
                    download reasoning as download_form4 -- also a single
                    bulk download like 13F above, but matched by ticker
                    symbol directly (FINRA's own file has one, no name-
                    fuzzing needed). Run via `python main.py
                    shortinterest`.
download_ib_prices(): refresh IB Gateway's own 3-month daily bars (see
                    refresh_ib_daily_history/download_ib_daily_history) for
                    the WHOLE active universe (same as `all`'s own scope,
                    not the narrower ranked/rated/held default some other
                    callers use -- explicit instruction), skipping any
                    ticker whose existing bar is already current -- the
                    PRIMARY daily-bar source for momentum/previousClose,
                    yfinance's price_history.json only the fallback. Also
                    run as part of `all` (see download_all), concurrently
                    with the hourly refresh below; kept as its own
                    command too for a refresh without the rest of the
                    pipeline. Gated by the same 3h cooldown as `all` (see
                    IB_REFRESH_STATE_FILE) -- cannot override it itself,
                    only `python main.py all overwrite` can. If
                    ib_server.py is already running, this routes through
                    its own IB Gateway connection via
                    /api/admin/refresh-ib-daily instead of connecting
                    directly (see refresh_ib_daily_history) -- IB Gateway
                    refuses a second simultaneous API connection while
                    that process holds one (confirmed live -- times out
                    regardless of clientId), which used to mean this
                    could only be run with the live server stopped; it no
                    longer does. Run via `python main.py ibprices`.
download_ib_hourly_prices(): the hourly twin of download_ib_prices --
                    refresh IB Gateway's own 1-month hourly bars (see
                    refresh_ib_hourly_history/download_ib_hourly_history)
                    for the same whole-active-universe scope, feeding
                    RecommendationsView's hourly-timeframe mean-reversion
                    factor -- the ONLY source for that factor, no yfinance
                    fallback exists for it. Also run as part of `all`,
                    concurrently with ibprices rather than after it (see
                    download_all); same cooldown/ib_server.py-routing/
                    standalone-command reasoning as download_ib_prices.
                    Run via `python main.py ibhprices`.
download_yfinance_prices(): refresh price_history.json (see
                    add_momentum_and_persist_history/write_price_history)
                    -- yfinance's own daily closes, the fallback source
                    momentum/previousClose use wherever IB Gateway's own
                    bars above don't cover a ticker. The yfinance-only
                    counterpart to `python main.py ibprices`: same
                    "standalone refresh without the rest of the pipeline"
                    reasoning, just for the other data source, and
                    likewise also run as part of `all`/`prices` already
                    (via add_momentum_and_persist_history) -- this is for
                    refreshing it on its own. Doesn't touch IB Gateway at
                    all (get_momentum's IB-bar blending is purely file-
                    based), so no connection conflict with ib_server.py
                    ever applies here. Covers every active ticker in
                    symbols.json, not a ranked/held subset -- yfinance has
                    no pacing limit like IB's to scope around, and
                    price_history.json is meant to cover the whole
                    universe regardless of which download_* entry point
                    wrote it. Run via `python main.py yfprices`.
download_eps_volatility(): refresh just epsVolatility (see
                    IBApp.get_eps_volatility/scoring.eps_volatility_rank)
                    for every ticker already in forward_pe.csv, merging
                    it in and re-deriving sorted_screen.csv's score/
                    rating -- lighter than `all`/`prices`, which both
                    also redo the whole forward-PE/momentum fetch just
                    to pick up this one field. For backfilling it after
                    adding/changing the factor itself, or for a ticker
                    `all`'s own FRESH_HOURS skip left without it (that
                    skip reuses the previous row as-is, so a ticker
                    fetched before this field existed keeps missing it
                    indefinitely otherwise). Doesn't add tickers that
                    aren't already in forward_pe.csv. Run via `python
                    main.py epsvol`.
download_eps_current_year(): refresh just epsCurrentYear (see
                    IBApp.get_eps_current_year) for every ticker already
                    in forward_pe.csv -- same "backfill one factor" role
                    download_eps_volatility plays for that field, for
                    backfilling this newly-added column into tickers
                    already in forward_pe.csv from before it existed
                    (modules/simulations.py's anchorEps blends it into
                    the fallback chain when no industry-median-PE anchor
                    is available). Doesn't add tickers that aren't
                    already in forward_pe.csv. Run via `python main.py
                    epscurrentyear`.
download_revenue_growth(): refresh just revenueGrowth (see
                    IBApp.get_revenue_per_share_growth), dilution-
                    adjusting it in place -- same "backfill one factor"
                    role download_eps_volatility plays for that field,
                    for the tickers already in forward_pe.csv from before
                    this adjustment existed. Run via `python main.py
                    revgrowth`.
download_gross_margins(): refresh just grossMargins (scoring.margin_rank's
                    third component) -- same "backfill one factor" role
                    as the two above. Run via `python main.py
                    grossmargin`.
download_insider_ownership(): refresh just heldPercentInsiders
                    (scoring.insiders_rank's ownership component) --
                    same "backfill one factor" role as the two above.
                    Run via `python main.py insiderown`.
download_themes():  classifies tickers' business descriptions
                    (raw_data.json's longBusinessSummary) against a fixed
                    theme taxonomy (see theme_classifier.py) for the
                    Themes tab. With no tickers given, classifies every
                    stock currently held in the IB Gateway account
                    instead (a fresh direct connection, not
                    RATED_FOR_EXTRAS). Run via `python main.py themes`
                    (held portfolio only), `python main.py themes TICKER
                    [TICKER ...]` (specific tickers only, e.g. right
                    after opening one new position), or `python main.py
                    themes --all` (every RATED_FOR_EXTRAS ticker, same
                    scoping as form4/xbrl/13f below, UNION every held
                    position -- classify_themes never overwrites an
                    already-tagged ticker, so this is a safe "catch up
                    the unclassified ones" run, just a slow one over
                    hundreds of tickers on a local model).
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
download_simulations(): EPS-driven Monte Carlo price simulation prototype (see
                    modules/simulations.py's own docstring for the formula) --
                    zero network calls, reads forward_pe.csv only, same as
                    rescore(). Writes data/output/simulations.json. Run via
                    `python main.py simulations [TICKER ...]` (defaults to the
                    full active universe when no tickers are given, same as
                    `simulations --all`; a specific ticker list runs just those).

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
                     scoring.ev_ebitda_rank), 5% daily-timeframe "strength"
                     (Money Flow Index -- the volume-weighted analog of
                     RSI, from the 3-month IB Gateway daily series where
                     available, else plain close-only RSI on the ~1-month
                     yfinance fallback; see IBApp.get_momentum/
                     _money_flow_index/_relative_strength_index -- scored
                     via a fixed sweet-spot curve, not "high is better":
                     see scoring.momentum_rank, peaks at 60, penalizes
                     both weak/oversold AND extreme overbought; missing
                     penalized as worst) + 5% hourly-timeframe overbought/
                     oversold (the SAME Money Flow Index, just on IB
                     Gateway's hourly series -- a short-term entry-timing
                     signal, not a second strength vote: a stock already
                     overbought on the hour is one you'd be chasing, so
                     LOW/oversold ranks best, a direct linear read
                     (rank = value / 100), the mirror of the daily
                     factor's own sweet-spot shape; only populated for
                     the CANDLESTICK_TOP_N ranked/held tickers IB Gateway
                     fetches hourly bars for, no fallback source, missing
                     ranked NEUTRAL not worst -- see scoring.mean_reversion_rank)
                     -- two independent factors, not blended into one the
                     way this used to work, 5% EPS
                     trend (eps_trend_rank -- average of the current- and
                     next-fiscal-year 30-day consensus EPS estimate
                     revision ranks, from yfinance's get_eps_trend(); see
                     IBApp.get_forward_pe/_eps_revision; missing penalized
                     as worst),
                     7% analyst conviction — the average of high
                     targetUpside, low recommendationMean, and low
                     target-price dispersion ((high-low)/mean) ranks
                     (negative upside, a 0 or missing recommendationMean,
                     and a missing/inconsistent target triple, all
                     penalized as worst; dispersion catches real analyst
                     disagreement the mean alone hides) — down from 7.5%,
                     moved to short interest below, 5% based on
                     forwardPE - trailingPE when trailingPE is positive and
                     finite (infinite or negative trailingPE — the company
                     lost money over the trailing twelve months — penalized
                     as worst for this factor instead of masked behind a
                     placeholder) — down from 10%, moved to analyst
                     conviction above, 5% low pegRatio (negative PEG penalized as
                     worst, not treated as "low"), 2% low trailingPS (price /
                     trailing-twelve-month revenue; missing penalized as worst) —
                     a separate valuation lens from forwardPE/priceToFCF/
                     enterpriseToEbitda, not blended with any of them, that
                     stays meaningful for unprofitable/negative-FCF names those
                     break down for (revenue is essentially never negative,
                     unlike earnings/FCF/EBITDA) — taken out of liquidity's
                     weight below, down from 2.5%, moved to short interest
                     below, 8% high revenueGrowth
                     (negative growth penalized as worst, not treated as
                     "low" — down from 10%, moved to margins below, then
                     back up from 7.5% to close the 0.5% gap the short
                     interest reweighting below had left), 5% low
                     debtToEquity relative to its sector's
                     average debtToEquity (negative or missing debtToEquity
                     penalized as worst, same treatment as pegRatio), 2%
                     liquidity — the average of high quickRatio and high
                     currentRatio ranks (missing penalized as worst — down
                     from 5%, then 2.5%, moved to trailingPS above and
                     short interest below), 3%
                     high returnOnEquity (negative ROE penalized as worst,
                     not treated as "low", same treatment as revenueGrowth —
                     down from 5%, moved to short interest below), 8% short
                     interest — the average of high pct-of-float, high
                     days-to-cover, and high change-percent ranks (missing
                     penalized as worst) — deliberately contrarian: the more
                     a stock is shorted, and the faster short interest is
                     growing, the better it scores here. pct-of-float is
                     FINRA's currentShortPositionQuantity divided by
                     raw_data.json's floatShares (a fresher read of the same
                     ratio yfinance's own shortPercentOfFloat approximates,
                     since FINRA settles biweekly and yfinance only reflects
                     the month-end settlement), days-to-cover and
                     change-percent (period-over-period % change in short
                     interest, no yfinance equivalent) both come straight
                     from FINRA's latest biweekly settlement file — see
                     finra.py and scoring.load_short_interest_scores — up
                     from 5% (previously yfinance-only: high shortRatio and
                     high shortPercentOfFloat), the other 3% taken out of
                     analyst conviction, trailingPS, liquidity, and ROE
                     above. 5% combined news +
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
                     percentile (top/bottom 6% = Strong Buy/Strong Sell,
                     next 14% each = Buy/Sell, middle 60% = Hold — same
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
  data/finra/short_interest.json  see finra.py's own docstring --
                     currentShortPositionQuantity/previousShortPositionQuantity/
                     changePercent/averageDailyVolumeQuantity/
                     daysToCoverQuantity per RATED_FOR_EXTRAS ticker from
                     FINRA's latest biweekly settlement file, matched by
                     ticker symbol directly. Overwritten wholesale each
                     run, same as institutional_holdings.json above.
  data/price_history.json  {ticker: [{date, close}, ...]} trailing ~1 month of
                     daily closes, captured from the same yfinance fetch
                     add_momentum already makes for the momentum score (see
                     IBApp.get_momentum) — no extra network round-trip.
                     Each ticker's series is replaced wholesale on its next
                     fetch (a rolling window, not an accumulated archive).
  data/price_history_daily_3mo.json  {ticker: [{date, open, high, low,
                     close, volume}, ...]} trailing ~3 months of IB
                     Gateway's own daily bars (see
                     download_ib_daily_history/refresh_ib_daily_history) --
                     the PRIMARY daily-bar source for momentum/
                     previousClose, yfinance's price_history.json above
                     only the fallback where this doesn't cover a ticker.
                     Written on demand by `python main.py ibprices`
                     ONLY -- not by prices/all (IB Gateway won't accept a
                     second simultaneous connection while
                     ib_server.py is already running one, confirmed
                     live). Only actually fetches a ticker whose existing
                     entry is missing or older than
                     scoring.most_recent_completed_trading_day(), and
                     merges into whatever's already on disk rather than
                     replacing it wholesale. Best-effort: skipped
                     entirely, with every other output unaffected, if IB
                     Gateway isn't reachable or refuses the connection.
                     Also written independently by ib_server.py's
                     own fetch_candlestick_history (same file, nearly the
                     same scope, kept current for the live app
                     separately) -- that writer overwrites the file with
                     just its own scoped fetch each time it runs (unlike
                     this command's merge), so a ticker only `ibprices`'
                     own scope happened to cover could be dropped again
                     next time the live server's own refresh runs.
"""

import asyncio
import csv
import functools
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from modules.IBApp import IBApp
from modules.scoring import (
    RATING_NA,
    add_avg_liquidity_ratio,
    add_target_upside,
    clamp_eps_revision,
    load_insider_scores,
    load_sentiment_scores,
    load_short_interest_scores,
    most_recent_completed_trading_day,
    rating_for_percentile,
    score_rows,
    to_float,
)
from modules.chatbot import answer_question
from modules.finra import SHORT_INTEREST_FILE, fetch_short_interest
from modules.simulations import run_iter as run_eps_simulations_iter
from modules.portfolio_optimizer import build_target_portfolio
from modules.recommendations import write_recommendations
from modules.sec_edgar import FORM4_FILE, THIRTEENF_FILE, fetch_13f_holdings, fetch_form4, fetch_xbrl_facts
from modules.social_sentiment import SENTIMENT_FILE, fetch_social_sentiment
from modules.theme_classifier import classify_themes

# Every JSON file a downloader (this module, ib_server.py,
# social_sentiment.py) produces lives here -- keeps the project root from
# filling up with generated output. CSVs (forward_pe.csv, sorted_screen.csv)
# live here too now (explicit instruction -- previously deliberately kept
# at the root); symbols.json (a hand-maintained input, not generated)
# stays at the root, the only generated-output exception left there.
# Created on import so a fresh checkout doesn't need a manual mkdir before
# the first run.
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

# IB Gateway's own downloader output (bars, Flex Query XML exports, news
# headlines) -- see DAILY_3MO_HISTORY_FILE/HOURLY_HISTORY_FILE/NEWS_FILE
# below and ib_server.py's own NAVs.xml -- kept in its own subfolder
# rather than loose in DATA_DIR (explicit instruction).
IB_DIR = os.path.join(DATA_DIR, "IB")
os.makedirs(IB_DIR, exist_ok=True)

# The rewritten-every-run/computed screener outputs (see download_all/
# rescore/write_full_csv/write_sorted_screen_csv, ib_server.py's own
# FinBERT news_sentiment.json, and modules/recommendations.py's own
# recommendations.json, blended from sorted_screen.csv + several other
# sources rather than straight IB/yfinance output) -- separated from
# DATA_DIR's other, more numerous single-purpose downloader caches
# (explicit instruction).
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Yahoo Finance's own downloader output -- see get_forward_pe's raw_out
# param (raw_data.json is the complete, unfiltered yfinance payload),
# add_momentum's own yfinance fetch (price_history.json), and
# get_forward_pe's own curated subset (forward_pe.csv) -- kept in its
# own subfolder, the yfinance-side counterpart to IB_DIR above (explicit
# instruction).
YFINANCE_DIR = os.path.join(DATA_DIR, "yfinance")
os.makedirs(YFINANCE_DIR, exist_ok=True)

OUTPUT_CSV = os.path.join(YFINANCE_DIR, "forward_pe.csv")
SORTED_SCREEN_CSV = os.path.join(OUTPUT_DIR, "sorted_screen.csv")
RAW_DATA_FILE = os.path.join(YFINANCE_DIR, "raw_data.json")
PRICE_HISTORY_FILE = os.path.join(YFINANCE_DIR, "price_history.json")
# IB Gateway's own bars, not yfinance's -- see IBApp.get_momentum, which
# blends these in for whatever ticker they cover and falls back to the
# plain yfinance calculation for everything else. `all`, standalone
# `ibprices`, and standalone `ibhprices` all cover the WHOLE active
# universe (see download_all/download_ib_prices/download_ib_hourly_
# prices) -- only ib_server.py's own narrower ranked/rated/held default
# scope (used by its Dataset-tab Run buttons/startup fetch when no
# explicit ticker list is given) stays smaller. IB's paced
# historical-data limit (HISTORICAL_PACING_MAX_REQUESTS/
# HISTORICAL_PACING_WINDOW_SECONDS, see IBApp.get_ib_historical_bars)
# makes even that narrower scope take minutes, let alone the full
# ~2,300-ticker universe, which is why `all` runs this fetch in the
# background rather than blocking on it upfront, and why an
# explicit-scope request is gated by a 3h cooldown (see
# IB_REFRESH_STATE_FILE/_ib_refresh_recently_completed below) rather
# than being free to re-run on every routine `all`/`ibprices` call.
# Refreshed as part of `all` and standalone via `python main.py
# ibprices` (see download_ib_daily_history/refresh_ib_daily_history
# below) -- routed through ib_server.py's own connection instead of
# opening a second one
# when that process is already running (IB Gateway won't accept a second
# simultaneous connection while ib_server.py's own
# fetch_candlestick_history (same file, same scope) is already running
# one live). Neither file is required for the prices/all pipeline to run,
# though: missing/stale/no-IB-Gateway just means get_momentum falls back
# to yfinance for every ticker, same as before either existed.
DAILY_3MO_HISTORY_FILE = os.path.join(IB_DIR, "price_history_daily_3mo.json")
HOURLY_HISTORY_FILE = os.path.join(IB_DIR, "price_history_hourly.json")
# Same value as ib_server.py's own CANDLESTICK_TOP_N -- duplicated,
# not imported, for the same circular-import reason SORTED_SCREEN_CSV/
# load_top_tickers are duplicated-by-reference in the comment above
# rather than imported the other way around (ib_server.py imports
# FROM main.py, never the reverse). Keep the two values in sync by hand.
CANDLESTICK_TOP_N = 500
# Also written by ib_server.py's news_loop (see that module's
# NEWS_SENTIMENT_FILE) -- duplicated here rather than imported since
# ib_server.py itself imports SORTED_SCREEN_CSV/load_top_tickers
# from this module, and importing it back would be circular. Read-only
# from here, same as the price-history files above.
NEWS_SENTIMENT_FILE = os.path.join(OUTPUT_DIR, "news_sentiment.json")
# Same duplicated-rather-than-imported reasoning as NEWS_SENTIMENT_FILE above.
NEWS_FILE = os.path.join(IB_DIR, "news.json")
SIMULATIONS_FILE = os.path.join(OUTPUT_DIR, "simulations.json")
TARGET_PORTFOLIO_FILE = os.path.join(OUTPUT_DIR, "target_portfolio.json")
RECOMMENDATIONS_FILE = os.path.join(OUTPUT_DIR, "recommendations.json")
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
    "ticker", "name", "sector", "forwardPE", "forwardEps", "epsCurrentYear", "trailingPE", "trailingPS", "pegRatio", "priceToFCF",
    "enterpriseToEbitda", "beta", "debtToEquity", "LiqRatio", "quickRatio", "currentRatio", "shortRatio", "shortPercentOfFloat",
    "revenueGrowth", "returnOnEquity", "profitMargins", "operatingMargins", "grossMargins", "price",
    "targetMeanPrice", "targetHighPrice", "targetLowPrice", "targetUpside", "recommendationKey",
    "recommendationMean", "numberOfAnalystOpinions", "momentum", "meanReversion", "epsRevision0y",
    "epsRevision1y", "epsVolatility", "heldPercentInsiders", "earningsTimestampStart", "yearReturn", "lastDownload",
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
    normalize_eps_revisions(data)
    return data


def normalize_eps_revisions(data):
    """Clamp cached EPS-revision ratios before scoring or rewriting them."""
    for d in data.values():
        for field in ("epsRevision0y", "epsRevision1y"):
            revision = clamp_eps_revision(d.get(field))
            d[field] = revision if revision is not None else None


def load_top_tickers(path, n=None):
    """Reads the first n tickers from an existing sorted_screen.csv (already
    ranked best-to-worst by score), or every ranked ticker in the file if n
    is None. Used to scope the social-sentiment download to the top of the
    ranking as it stood before this run started, since this run's own
    scores aren't computed until later -- and, unbounded, by
    ib_server.py to decide which tickers get a live/snapshot IB
    price at all.

    Skips any row with no `score` -- see write_sorted_screen_csv, which
    appends negative-forwardPE tickers at the end of the file, unscored
    and unranked, for visibility in the Screener only. Unranked isn't
    "ranked last"; it's not part of this ranking, so it shouldn't count
    toward a top-N slice or (for ib_server.py's unbounded calls)
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


def add_momentum_from_cache(app, data):
    """add_momentum's no-fetch counterpart, for rescore(): recomputes
    momentum/meanReversion purely from files already on disk (see
    IBApp.get_momentum_from_disk) -- IB Gateway's own daily/hourly bars
    (DAILY_3MO_HISTORY_FILE/HOURLY_HISTORY_FILE, refreshed by ibprices/
    ibhprices) with price_history.json's already-cached yfinance closes
    as the fallback source, instead of a fresh yfinance fetch. Doesn't
    touch price_history.json itself -- nothing new was fetched to
    persist into it, unlike add_momentum_and_persist_history."""
    momentum = app.get_momentum_from_disk(
        list(data.keys()),
        daily_3mo_by_ticker=_load_json_or_empty(DAILY_3MO_HISTORY_FILE),
        hourly_by_ticker=_load_json_or_empty(HOURLY_HISTORY_FILE),
        yfinance_history_by_ticker=_load_json_or_empty(PRICE_HISTORY_FILE),
    )
    for symbol, d in data.items():
        result = momentum.get(symbol) or {}
        d["momentum"] = result.get("momentum")
        d["meanReversion"] = result.get("mean_reversion")


def add_momentum_and_persist_history(app, data):
    """add_momentum, plus merging the close-series it captures into
    price_history.json — the common case across all three download_*
    entry points (download_all/download_prices/download_yfinance_prices
    -- the last of these is `python main.py yfprices`, the dedicated
    yfinance-only command). Also updates MISSINGS_FILE's "yfinance" key
    (see _update_missings) with every ticker `history` came back with no
    closes for at all -- yf.Ticker(symbol).history() either raised on
    all 3 attempts (see IBApp.get_momentum's own fetch closure) or
    returned zero rows."""
    history = {}
    add_momentum(app, data, history_out=history)
    try:
        with open(PRICE_HISTORY_FILE) as f:
            all_history = json.load(f)
    except FileNotFoundError:
        all_history = {}
    all_history.update(history)
    write_price_history(all_history)
    _update_missings("yfinance", [t for t in data if not history.get(t)])


def _ib_gateway_reachable(host="127.0.0.1", port=4001, timeout=2):
    """Quick TCP probe, not a real connect/handshake -- just enough to
    tell "IB Gateway/TWS isn't even listening" (the common case: running
    `python main.py prices` without it open) apart from "listening but
    something else is wrong", which IBApp.connect's own retry-then-
    sys.exit(1) already handles its own way. Explicit instruction: IB is
    the PRIMARY daily-bar source for recommendations/screener scoring,
    yfinance only the fallback -- a "fallback" that can take down this
    entire yfinance-only pipeline the moment IB Gateway simply isn't
    running would defeat the point, so this check runs before ever
    touching app.connect()."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ib_server.py's own default port (see that module's main()) -- not
# configurable here since main.py has no way to know a custom port was
# passed to a separately-running ib_server.py process; only matters for
# _ib_server_running/_refresh_ib_daily_via_server below, and only when
# ib_server.py is actually up.
IB_SERVER_PORT = 8765


def _ib_server_running(port=IB_SERVER_PORT, timeout=2):
    """Quick HTTP probe for whether ib_server.py's own process is up on
    this machine -- distinct from _ib_gateway_reachable's TCP probe of
    Gateway itself. Used by refresh_ib_daily_history to route through
    that process's already-connected IB Gateway connection instead of
    opening a second one (see that function's docstring for why)."""
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/last-prices", timeout=timeout)
        return True
    except (urllib.error.URLError, OSError):
        return False


# Shared with ib_server.py (same DATA_DIR-relative path, both processes
# read/write it) -- tracks when an EXPLICIT-scope (tickers passed
# in, not the implicit ranked/rated/held default) IB daily/hourly
# refresh last actually completed, regardless of which process/entry
# point ran it. See _ib_refresh_recently_completed/_mark_ib_refresh_
# completed below.
IB_REFRESH_STATE_FILE = os.path.join(DATA_DIR, "ib_refresh_state.json")
IB_REFRESH_COOLDOWN_SECONDS = 3 * 3600

# {"ib_daily": [...], "ib_hourly": [...], "yfinance": [...]} -- tickers
# each data source came back with literally nothing for on the most
# recent run that actually checked them (download_ib_daily_history/
# download_ib_hourly_history/add_momentum's own yfinance fetch, and
# ib_server.py's own refresh_daily_history_on_demand/refresh_hourly_
# history_on_demand twins -- see _update_missings below). Explicit
# instruction: both the IB "price" refreshes and the yfinance fetch
# should surface what they couldn't get, not just silently carry the
# ticker forward with no data. Each key is overwritten wholesale by
# whichever process last actually checked that source (not merged/
# accumulated -- a ticker that recovers should disappear next run), but
# the other keys are left alone, since e.g. `ibprices` alone shouldn't
# blow away what the last yfinance run found missing.
MISSINGS_FILE = os.path.join(DATA_DIR, "missings.json")


def _update_missings(key, missing_tickers):
    """Overwrites MISSINGS_FILE's `key` entry with `missing_tickers`
    (sorted, deduped) -- see MISSINGS_FILE's own comment for why this is
    a wholesale replace of just this one key, not a merge/accumulation."""
    try:
        with open(MISSINGS_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state[key] = sorted(set(missing_tickers))
    with open(MISSINGS_FILE, "w") as f:
        json.dump(state, f)


def _ib_refresh_recently_completed(kind):
    """True if an explicit-scope IB `kind` ("daily"/"hourly") refresh
    completed within the last IB_REFRESH_COOLDOWN_SECONDS (3h) --
    checked by refresh_ib_daily_history/refresh_ib_hourly_history (and
    their ib_server.py twins, against the same IB_REFRESH_STATE_FILE)
    before attempting a fetch, so repeated `all`/`ibprices`/`ibhprices`
    runs within a few hours of each other don't re-hit IB Gateway for a
    full ~2340-ticker pull that just happened. Explicit instruction: no
    command should be able to force a fresh pull within the cooldown
    window except `python main.py all overwrite` (see download_all's own
    overwrite param)."""
    try:
        with open(IB_REFRESH_STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    completed_at = state.get(f"{kind}_completed_at")
    if not completed_at:
        return False
    try:
        completed = datetime.fromisoformat(completed_at)
    except ValueError:
        return False
    return (datetime.now() - completed).total_seconds() < IB_REFRESH_COOLDOWN_SECONDS


def _mark_ib_refresh_completed(kind):
    """Records `kind` ("daily"/"hourly") as having just completed an
    explicit-scope IB refresh, for _ib_refresh_recently_completed's own
    cooldown check above. Merges into IB_REFRESH_STATE_FILE rather than
    overwriting it wholesale, since daily/hourly are tracked
    independently and either main.py or ib_server.py may be the one
    updating it."""
    try:
        with open(IB_REFRESH_STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state[f"{kind}_completed_at"] = datetime.now().isoformat()
    with open(IB_REFRESH_STATE_FILE, "w") as f:
        json.dump(state, f)


def _refresh_ib_daily_via_server(port=IB_SERVER_PORT, timeout=7200, tickers=None, overwrite=False):
    """POST /api/admin/refresh-ib-daily on ib_server.py's already-running
    process (see that module's refresh_daily_history_on_demand) --
    {"skipped": bool, "tickersTotal": int, ...} on success, or
    {"error": str} if IB Gateway wasn't connected on that end. tickers,
    if given, is sent as the request body's JSON {"tickers": [...]} to
    request that exact scope instead of ib_server.py's own ranked/rated/
    held default -- download_all's own full-universe call needs this.
    overwrite, if True, is sent alongside to bypass ib_server.py's own
    copy of the 3h cooldown check too (see IB_REFRESH_STATE_FILE) --
    belt-and-suspenders with the local check in refresh_ib_daily_history,
    for the case where ib_server.py's on-disk record is ahead of what
    this process last saw. timeout is long (2hr, matching ib_server.py's
    own handler): a large stale ticker list, paced by IB's rate limit,
    can take a while, and this blocks until ib_server.py's fetch
    actually finishes."""
    data = json.dumps({"tickers": tickers, "overwrite": overwrite}).encode() if tickers is not None or overwrite else b""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/admin/refresh-ib-daily", method="POST", data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _refresh_ib_hourly_via_server(port=IB_SERVER_PORT, timeout=7200, tickers=None, overwrite=False):
    """POST /api/admin/refresh-ib-hourly on ib_server.py's already-running
    process (see that module's refresh_hourly_history_on_demand) -- the
    hourly-bars twin of _refresh_ib_daily_via_server, same shape/timeout/
    tickers/overwrite-passthrough reasoning."""
    data = json.dumps({"tickers": tickers, "overwrite": overwrite}).encode() if tickers is not None or overwrite else b""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/admin/refresh-ib-hourly", method="POST", data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _progress_printer(label, total):
    """Returns an on_ticker callback (see IBApp.get_ib_historical_bars_
    async's own docstring) that prints "{label} {i}/{total}: {symbol}..."
    right before each request goes out -- for download_ib_daily_history/
    download_ib_hourly_history's own direct-connect fetch, which
    otherwise prints nothing between its start-of-batch and end-of-batch
    lines for however long a large stale list takes (no per-ticker
    progress the way ib_server.py's on-demand refresh has via its own
    on_ticker, and IBApp's own internal logging.info messages are
    silently dropped since this codebase never configures a logging
    handler) -- explicit instruction: real-time visibility into a
    full-universe pull that can run for an hour or more."""
    count = 0

    def on_ticker(symbol):
        nonlocal count
        count += 1
        print(f"{label}  {symbol}  {count}/{total}")

    return on_ticker


async def download_ib_daily_history(app, tickers):
    """Refreshes DAILY_3MO_HISTORY_FILE (IB Gateway's own 3-month daily
    bars -- see that constant's own comment) for `tickers`, via an
    already-connected `app`. Only actually fetches a ticker whose
    existing entry is missing or older than
    most_recent_completed_trading_day() -- IB's paced historical-data
    limit (200 requests/6min, see IBApp.get_ib_historical_bars_async)
    still makes a large stale ticker list take a while, and this runs on
    every `prices`/`all` call, so a day where the data's already current
    does no IB Gateway work at all. Merges into the existing file rather
    than replacing it wholesale (unlike ib_server.py's own version of
    this fetch, which always refetches its whole scope) -- exactly
    because this staleness gate means a given run may only be touching a
    handful of tickers out of the full scope. Async (awaits
    get_ib_historical_bars_async, not the sync get_ib_historical_bars)
    so refresh_ib_daily_history/refresh_ib_hourly_history can run
    concurrently as asyncio tasks (see download_all) the same way
    ib_server.py's own on-demand refreshes do, instead of two separate
    OS threads each blocking on its own sync call.

    Also updates MISSINGS_FILE's "ib_daily" key (see _update_missings)
    with every ticker in `tickers` that still has no bars at all after
    this call -- whether that's because this run's own fetch came back
    empty for it, or because it was already empty on disk and not even
    stale enough to retry."""
    try:
        with open(DAILY_3MO_HISTORY_FILE) as f:
            existing = json.load(f)
    except FileNotFoundError:
        existing = {}
    expected = most_recent_completed_trading_day()
    stale = [t for t in tickers if not existing.get(t) or existing[t][-1]["date"][:10] < expected]
    if not stale:
        print(f"IB daily history already current for all {len(tickers)} candidate ticker(s); skipping IB Gateway fetch")
        _update_missings("ib_daily", [t for t in tickers if not existing.get(t)])
        return
    print(f"Fetching IB 3mo daily bars for {len(stale)}/{len(tickers)} stale/missing ticker(s) (paced, can take a while)...")
    fresh = await app.get_ib_historical_bars_async(stale, "3 M", "1 day", on_ticker=_progress_printer("Daily", len(stale)))
    existing.update(fresh)
    with open(DAILY_3MO_HISTORY_FILE, "w") as f:
        json.dump(existing, f)
    got = sum(1 for v in fresh.values() if v)
    print(f"Wrote {DAILY_3MO_HISTORY_FILE} ({got}/{len(stale)} fetched tickers had bars; {len(existing)} tickers total on file)")
    _update_missings("ib_daily", [t for t in tickers if not existing.get(t)])


# Distinct from ib_server.py's own clientId 0 (see that module's
# run_ib_client) -- confirmed live that connecting a second client with
# the SAME id while that server is already running just times out
# (IB Gateway/TWS treats clientId as a per-connection identity, not
# something two simultaneous connections can share), which without the
# is_connected check below took the whole `prices` pipeline down with it.
IB_HISTORY_CLIENT_ID = 7

# Separate id for refresh_ib_hourly_history's own direct connection --
# download_all now runs the daily and hourly refreshes concurrently in
# their own threads (each with its own IBApp(), see download_all), so
# they need distinct clientIds to connect to IB Gateway at the same time
# rather than colliding on IB_HISTORY_CLIENT_ID above.
IB_HOURLY_HISTORY_CLIENT_ID = 8


async def refresh_ib_daily_history(app, tickers=None, overwrite=False):
    """Connects `app` to IB Gateway (if it isn't already) just long enough
    to refresh daily bars (see download_ib_daily_history) for `tickers`,
    then disconnects again if this call is the one that connected it.
    Async (awaits connect_async/download_ib_daily_history/reqPositions
    Async, and offloads the sync ib_server.py-routing HTTP call to a
    thread via run_in_executor) so this can run concurrently with
    refresh_ib_hourly_history as two asyncio tasks on one event loop --
    see download_all, which gathers both -- the same concurrency model
    ib_server.py itself uses for its own on-demand refreshes, instead of
    each blocking its own separate OS thread.

    tickers=None (the default -- ib_server.py's own startup/Dataset-tab-
    default scope) uses the same scope ib_server.py's own
    fetch_candlestick_history mirrors: top CANDLESTICK_TOP_N ranked (from
    sorted_screen.csv as it stood before this run -- same "scoped to the
    ranking as it stood before this run" precedent download_all's own
    social-sentiment step already uses) union RATED_FOR_EXTRAS union
    every currently-held stock. The RATED_FOR_EXTRAS/held unions matter
    for the same reason documented there: a Strong Sell near the bottom
    of ~1900 ranked tickers, or a held ETF that isn't even in the
    screener universe (e.g. ARKK), would otherwise never be covered no
    matter how large CANDLESTICK_TOP_N is. Passing an explicit `tickers`
    list instead (download_all and download_ib_prices both now do --
    explicit instruction: `ibprices`/`all` should both be able to cover
    the WHOLE active universe, not just this narrower default) skips
    that union entirely and uses exactly what's given -- and, unlike the
    tickers=None default, is gated by a 3h cooldown (see
    _ib_refresh_recently_completed/IB_REFRESH_STATE_FILE): if an
    explicit-scope refresh already completed within the last 3h, this
    no-ops immediately without even checking IB Gateway, unless
    overwrite=True (only download_all's own `python main.py all
    overwrite` sets that -- explicit instruction: no other command
    should be able to force a fresh pull inside the cooldown window).

    No-ops (prints and returns) if IB Gateway isn't even reachable, or if
    app.connect_async() didn't actually succeed (e.g. rejected/timed out
    for some other reason) -- rather than letting connect_async's own
    retry-then-sys.exit(1) take down the rest of this pipeline's
    yfinance-only work, or (the bug this guard replaced) silently
    proceeding to call reqPositionsAsync/reqHistoricalData on a
    connection that never came up and crashing on ConnectionError
    instead.

    If ib_server.py is already running, routes through its own
    /api/admin/refresh-ib-daily endpoint (passing `tickers`/`overwrite`
    along in the request body when given) instead of connecting `app`
    directly -- IB Gateway refuses a second simultaneous API connection
    while that process holds one (confirmed live, times out regardless
    of clientId -- see IB_HISTORY_CLIENT_ID's own comment below), which
    used to be exactly what made this need "typically run with the live
    server stopped." This is what removes that requirement."""
    if tickers is not None and not overwrite and _ib_refresh_recently_completed("daily"):
        print(f"IB daily history was already fully refreshed within the last {IB_REFRESH_COOLDOWN_SECONDS // 3600}h -- skipping (run `python main.py all overwrite` to force)")
        return
    if not _ib_gateway_reachable():
        print("IB Gateway not reachable at 127.0.0.1:4001 -- skipping IB daily-history refresh (yfinance-only this run)")
        return
    if _ib_server_running():
        print(f"ib_server.py is already running on port {IB_SERVER_PORT} -- refreshing via its own IB Gateway connection instead of opening a second one")
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, functools.partial(_refresh_ib_daily_via_server, tickers=tickers, overwrite=overwrite))
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            print(f"ib_server.py refresh request failed ({e}) -- skipping IB daily-history refresh (yfinance-only this run)")
            return
        if result.get("error"):
            print(f"ib_server.py could not refresh IB daily history: {result['error']}")
        elif result["skipped"]:
            print(f"IB daily history already current for all {result['tickersTotal']} candidate ticker(s) (via ib_server.py); skipping")
        else:
            print(
                f"Wrote {DAILY_3MO_HISTORY_FILE} via ib_server.py "
                f"({result['gotBars']}/{result['staleFetched']} fetched tickers had bars; {result['tickersTotal']} tickers total in scope)"
            )
        if tickers is not None and not result.get("error"):
            _mark_ib_refresh_completed("daily")
        return
    if tickers is None:
        ranked = set(load_top_tickers(SORTED_SCREEN_CSV, CANDLESTICK_TOP_N))
        rated = set(load_rated_tickers(SORTED_SCREEN_CSV, RATED_FOR_EXTRAS))
        was_connected = app.is_connected
        if not was_connected:
            await app.connect_async(client_id=IB_HISTORY_CLIENT_ID)
        if not app.is_connected:
            print("Could not connect to IB Gateway (see the error logged above) -- skipping IB daily-history refresh (yfinance-only this run)")
            return
        try:
            held = {p.contract.symbol for p in await app.ib.reqPositionsAsync() if p.contract.secType == "STK" and p.position != 0}
            await download_ib_daily_history(app, sorted(ranked | rated | held))
        finally:
            if not was_connected:
                app.disconnect()
        return
    was_connected = app.is_connected
    if not was_connected:
        await app.connect_async(client_id=IB_HISTORY_CLIENT_ID)
    if not app.is_connected:
        print("Could not connect to IB Gateway (see the error logged above) -- skipping IB daily-history refresh (yfinance-only this run)")
        return
    try:
        await download_ib_daily_history(app, tickers)
        _mark_ib_refresh_completed("daily")
    finally:
        if not was_connected:
            app.disconnect()


def download_ib_prices():
    """Refreshes IB Gateway's own daily bars (see refresh_ib_daily_history)
    on its own, via `python main.py ibprices` -- explicit-scope, the
    WHOLE active universe (same as symbols.json's own active tickers,
    same list `all` uses), not the narrower ranked/rated/held default.
    Also run as part of `all` (see download_all) with that same
    full-universe scope; this standalone call is for a refresh without
    the rest of the pipeline. Both this and `all` share the same 3h
    cooldown (see IB_REFRESH_STATE_FILE) -- if a full-universe refresh
    already completed within the last 3h (by either this command or
    `all`), this no-ops immediately; only `python main.py all overwrite`
    can force a fresh pull inside that window, this command cannot
    override it itself. Still deliberately excluded from `prices` (see
    download_prices' own docstring): that command runs routinely and IB
    Gateway won't accept a second simultaneous API connection while
    ib_server.py is already holding one open, so this needs to be
    something the user chooses to run rather than something a routine
    command silently attempts every time."""
    app = IBApp()
    asyncio.run(refresh_ib_daily_history(app, load_tickers(SYMBOLS_FILE)))


async def download_ib_hourly_history(app, tickers):
    """Refreshes HOURLY_HISTORY_FILE (IB Gateway's own 1-month hourly
    bars) for `tickers`, via an already-connected `app` -- the hourly
    twin of download_ib_daily_history, same staleness gate (only a
    ticker whose existing entry is missing or older than
    most_recent_completed_trading_day() gets refetched -- date-only,
    same as the daily check, even though these bars carry a time-of-day
    too: "has at least one bar from the most recent session" is what
    matters here, not which hour), same merge-not-replace behavior, same
    MISSINGS_FILE "ib_hourly" tracking, and same async reasoning (see
    download_ib_daily_history's own docstring)."""
    try:
        with open(HOURLY_HISTORY_FILE) as f:
            existing = json.load(f)
    except FileNotFoundError:
        existing = {}
    expected = most_recent_completed_trading_day()
    stale = [t for t in tickers if not existing.get(t) or existing[t][-1]["date"][:10] < expected]
    if not stale:
        print(f"IB hourly history already current for all {len(tickers)} candidate ticker(s); skipping IB Gateway fetch")
        _update_missings("ib_hourly", [t for t in tickers if not existing.get(t)])
        return
    print(f"Fetching IB 1mo hourly bars for {len(stale)}/{len(tickers)} stale/missing ticker(s) (paced, can take a while)...")
    fresh = await app.get_ib_historical_bars_async(stale, "1 M", "1 hour", on_ticker=_progress_printer("Hourly", len(stale)))
    existing.update(fresh)
    with open(HOURLY_HISTORY_FILE, "w") as f:
        json.dump(existing, f)
    got = sum(1 for v in fresh.values() if v)
    print(f"Wrote {HOURLY_HISTORY_FILE} ({got}/{len(stale)} fetched tickers had bars; {len(existing)} tickers total on file)")
    _update_missings("ib_hourly", [t for t in tickers if not existing.get(t)])


async def refresh_ib_hourly_history(app, tickers=None, overwrite=False):
    """The hourly twin of refresh_ib_daily_history -- same connect/scope/
    tickers-param/overwrite-param/cooldown/async/ib_server.py-routing
    behavior, just for HOURLY_HISTORY_FILE via download_ib_hourly_history
    and /api/admin/refresh-ib-hourly instead of the daily file/endpoint.
    See that function's docstring for the full reasoning; not repeated
    here."""
    if tickers is not None and not overwrite and _ib_refresh_recently_completed("hourly"):
        print(f"IB hourly history was already fully refreshed within the last {IB_REFRESH_COOLDOWN_SECONDS // 3600}h -- skipping (run `python main.py all overwrite` to force)")
        return
    if not _ib_gateway_reachable():
        print("IB Gateway not reachable at 127.0.0.1:4001 -- skipping IB hourly-history refresh (yfinance-only this run)")
        return
    if _ib_server_running():
        print(f"ib_server.py is already running on port {IB_SERVER_PORT} -- refreshing via its own IB Gateway connection instead of opening a second one")
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, functools.partial(_refresh_ib_hourly_via_server, tickers=tickers, overwrite=overwrite))
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            print(f"ib_server.py refresh request failed ({e}) -- skipping IB hourly-history refresh (yfinance-only this run)")
            return
        if result.get("error"):
            print(f"ib_server.py could not refresh IB hourly history: {result['error']}")
        elif result["skipped"]:
            print(f"IB hourly history already current for all {result['tickersTotal']} candidate ticker(s) (via ib_server.py); skipping")
        else:
            print(
                f"Wrote {HOURLY_HISTORY_FILE} via ib_server.py "
                f"({result['gotBars']}/{result['staleFetched']} fetched tickers had bars; {result['tickersTotal']} tickers total in scope)"
            )
        if tickers is not None and not result.get("error"):
            _mark_ib_refresh_completed("hourly")
        return
    if tickers is None:
        ranked = set(load_top_tickers(SORTED_SCREEN_CSV, CANDLESTICK_TOP_N))
        rated = set(load_rated_tickers(SORTED_SCREEN_CSV, RATED_FOR_EXTRAS))
        was_connected = app.is_connected
        if not was_connected:
            await app.connect_async(client_id=IB_HOURLY_HISTORY_CLIENT_ID)
        if not app.is_connected:
            print("Could not connect to IB Gateway (see the error logged above) -- skipping IB hourly-history refresh (yfinance-only this run)")
            return
        try:
            held = {p.contract.symbol for p in await app.ib.reqPositionsAsync() if p.contract.secType == "STK" and p.position != 0}
            await download_ib_hourly_history(app, sorted(ranked | rated | held))
        finally:
            if not was_connected:
                app.disconnect()
        return
    was_connected = app.is_connected
    if not was_connected:
        await app.connect_async(client_id=IB_HOURLY_HISTORY_CLIENT_ID)
    if not app.is_connected:
        print("Could not connect to IB Gateway (see the error logged above) -- skipping IB hourly-history refresh (yfinance-only this run)")
        return
    try:
        await download_ib_hourly_history(app, tickers)
        _mark_ib_refresh_completed("hourly")
    finally:
        if not was_connected:
            app.disconnect()


def download_ib_hourly_prices():
    """Refreshes IB Gateway's own hourly bars (see
    refresh_ib_hourly_history) on its own, via `python main.py
    ibhprices` -- the hourly twin of download_ib_prices/`python main.py
    ibprices`: explicit-scope, the whole active universe, same 3h
    cooldown (see download_ib_prices' own docstring -- same reasoning
    applies here, just for HOURLY_HISTORY_FILE). Also run as part of
    `all` alongside the daily refresh (both concurrently, not
    sequentially -- see download_all), with that same full-universe
    scope. Kept standalone here for a refresh without the rest of the
    pipeline, and likewise excluded from `prices` for the same reason
    (see download_ib_prices' own docstring)."""
    app = IBApp()
    asyncio.run(refresh_ib_hourly_history(app, load_tickers(SYMBOLS_FILE)))


def download_yfinance_prices():
    """Refreshes price_history.json (see
    add_momentum_and_persist_history/write_price_history) on its own, via
    `python main.py yfprices` -- the yfinance-only counterpart to
    download_ib_prices/`python main.py ibprices`, same "standalone
    refresh without the rest of the pipeline" reasoning, just for the
    other data source. Doesn't touch IB Gateway at all: get_momentum's
    IB-bar blending (daily_3mo_by_ticker/hourly_by_ticker) is purely
    file-based, so app.connect() is never called here and there's no
    connection conflict with ib_server.py to route around. Covers every
    active ticker in symbols.json -- yfinance has no pacing limit like
    IB's to scope a ranked/held subset around, and price_history.json is
    meant to cover the whole universe regardless of which download_*
    entry point wrote it (see get_momentum's own docstring). The
    momentum/meanReversion values this incidentally computes are
    discarded -- add_momentum_and_persist_history normally feeds them
    into forward_pe.csv/sorted_screen.csv, but nothing here writes
    those, only price_history.json."""
    app = IBApp()
    tickers = load_tickers(SYMBOLS_FILE)
    print(f"Loaded {len(tickers)} active tickers from {SYMBOLS_FILE}")
    add_momentum_and_persist_history(app, {t: {} for t in tickers})


def download_eps_volatility():
    """Refreshes just epsVolatility (see IBApp.get_eps_volatility) for
    every ticker already in forward_pe.csv, merging it into that file
    and re-deriving sorted_screen.csv's score/rating from the result --
    via `python main.py epsvol`. Lighter than `all`/`prices`, which both
    also redo the whole forward-PE/momentum fetch just to pick up this
    one field; exists for backfilling it after adding/changing the
    factor itself, or for a ticker `all`'s own FRESH_HOURS skip left
    without it (that skip reuses the previous row as-is, so a ticker
    fetched before this field existed keeps missing it indefinitely
    otherwise). Tickers not already in forward_pe.csv are left alone --
    this only updates existing rows, it doesn't fetch forward-PE/sector/
    price from scratch for a ticker that has none yet."""
    app = IBApp()
    data = load_pe_data(OUTPUT_CSV)
    print(f"Loaded {len(data)} tickers from {OUTPUT_CSV}")
    eps_volatility = app.get_eps_volatility(list(data.keys()))
    updated = 0
    for ticker, value in eps_volatility.items():
        if ticker in data:
            data[ticker]["epsVolatility"] = value
            if value is not None:
                updated += 1
    write_full_csv(data)
    write_sorted_screen_csv(data)
    print(f"Updated epsVolatility for {updated}/{len(data)} tickers")


def download_eps_current_year():
    """Refreshes just epsCurrentYear (see IBApp.get_eps_current_year) for
    every ticker already in forward_pe.csv, merging it into that file and
    re-deriving sorted_screen.csv's score/rating from the result -- via
    `python main.py epscurrentyear`. Same "backfill one factor without
    redoing the whole pipeline" role download_eps_volatility plays for
    that field; exists specifically for backfilling this newly-added
    column into tickers already in forward_pe.csv from before it existed
    (modules/simulations.py's anchorEps now blends it into the fallback
    chain when no industry-median-PE anchor is available). Tickers not
    already in forward_pe.csv are left alone, same as
    download_eps_volatility."""
    app = IBApp()
    data = load_pe_data(OUTPUT_CSV)
    print(f"Loaded {len(data)} tickers from {OUTPUT_CSV}")
    eps_current_year = app.get_eps_current_year(list(data.keys()))
    updated = 0
    for ticker, value in eps_current_year.items():
        if ticker in data and value is not None:
            data[ticker]["epsCurrentYear"] = value
            updated += 1
    write_full_csv(data)
    write_sorted_screen_csv(data)
    print(f"Updated epsCurrentYear for {updated}/{len(data)} tickers")


def download_revenue_growth():
    """Refreshes just revenueGrowth (see IBApp.get_revenue_per_share_growth)
    for every ticker already in forward_pe.csv, dilution-adjusting it in
    place, merging into that file and re-deriving sorted_screen.csv's
    score/rating from the result -- via `python main.py revgrowth`. Same
    "backfill one factor without redoing the whole pipeline" role
    download_eps_volatility plays for that field; exists specifically for
    the ~2340 tickers already in forward_pe.csv from before this
    adjustment existed, whose revenueGrowth is still Yahoo's raw,
    dilution-blind ratio until backfilled. Tickers not already in
    forward_pe.csv are left alone, same as download_eps_volatility."""
    app = IBApp()
    data = load_pe_data(OUTPUT_CSV)
    print(f"Loaded {len(data)} tickers from {OUTPUT_CSV}")
    revenue_growth = app.get_revenue_per_share_growth(list(data.keys()))
    updated = 0
    for ticker, value in revenue_growth.items():
        # Unlike epsVolatility (usually already None going in),
        # revenueGrowth already holds a real -- if unadjusted -- value for
        # most tickers here; only overwrite it on an actual result, so a
        # transient per-ticker fetch failure (value is None) leaves the
        # existing reading in place instead of blanking it out.
        if ticker in data and value is not None:
            data[ticker]["revenueGrowth"] = value
            updated += 1
    write_full_csv(data)
    write_sorted_screen_csv(data)
    print(f"Updated revenueGrowth for {updated}/{len(data)} tickers")


def download_gross_margins():
    """Refreshes just grossMargins (scoring.margin_rank's third component)
    for every ticker already in forward_pe.csv -- via `python main.py
    grossmargin`. Same "backfill one factor without redoing the whole
    pipeline" role download_eps_volatility/download_revenue_growth
    already play for theirs. Kept fully separate from
    download_insider_ownership below -- distinct factors, distinct
    commands. Tickers not already in forward_pe.csv are left alone, same
    as the others."""
    app = IBApp()
    data = load_pe_data(OUTPUT_CSV)
    print(f"Loaded {len(data)} tickers from {OUTPUT_CSV}")
    gross_margins = app.get_gross_margins(list(data.keys()))
    updated = 0
    for ticker, value in gross_margins.items():
        if ticker in data and value is not None:
            data[ticker]["grossMargins"] = value
            updated += 1
    write_full_csv(data)
    write_sorted_screen_csv(data)
    print(f"Updated grossMargins for {updated}/{len(data)} tickers")


def download_insider_ownership():
    """Refreshes just heldPercentInsiders (scoring.insiders_rank's
    ownership component) for every ticker already in forward_pe.csv --
    via `python main.py insiderown`. Same "backfill one factor without
    redoing the whole pipeline" role download_eps_volatility/
    download_revenue_growth already play for theirs. Kept fully separate
    from download_gross_margins above -- distinct factors, distinct
    commands. Tickers not already in forward_pe.csv are left alone, same
    as the others."""
    app = IBApp()
    data = load_pe_data(OUTPUT_CSV)
    print(f"Loaded {len(data)} tickers from {OUTPUT_CSV}")
    ownership = app.get_insider_ownership(list(data.keys()))
    updated = 0
    for ticker, value in ownership.items():
        if ticker in data and value is not None:
            data[ticker]["heldPercentInsiders"] = value
            updated += 1
    write_full_csv(data)
    write_sorted_screen_csv(data)
    print(f"Updated heldPercentInsiders for {updated}/{len(data)} tickers")


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
    normalize_eps_revisions(data)
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
    snapshot IB price universe ib_server.py builds from this file."""
    normalize_eps_revisions(data)
    rows = [(s, d) for s, d in screen_rows(data) if (to_float(d.get("price")) or 0) >= MIN_PRICE]
    sentiment_scores = load_sentiment_scores(SENTIMENT_FILE, NEWS_SENTIMENT_FILE, THIRTEENF_FILE)
    insider_scores = load_insider_scores(FORM4_FILE)
    short_interest_scores = load_short_interest_scores(SHORT_INTEREST_FILE, RAW_DATA_FILE)

    # Inject simReturn (forecastReturn, simulate_ticker's own confidence-
    # weighted fair value vs. currentPrice) from simulations.json into
    # each row dict so forecast_return_rank can read it directly.
    # Tickers not present in the file (not yet simulated, simulated with an
    # error, or with no industry-multiple scenario to derive a forecast
    # from) are simply left without the field -- forecast_return_rank ranks
    # them worst, same treatment as every other factor's missing data.
    try:
        with open(SIMULATIONS_FILE) as _mcf:
            _mc_data = {}
            for entry in json.load(_mcf):
                if "ticker" not in entry or entry.get("error"):
                    continue
                _ret = entry.get("forecastReturn")
                if _ret is not None:
                    _mc_data[entry["ticker"]] = _ret
    except (FileNotFoundError, json.JSONDecodeError):
        _mc_data = {}
    rows = [(s, {**d, "simReturn": _mc_data[s]} if s in _mc_data else d) for s, d in rows]

    scored = sorted(
        score_rows(rows, sentiment_scores, insider_scores, short_interest_scores), key=lambda item: item[2]
    )
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
    write_recommendations(SORTED_SCREEN_CSV, NEWS_FILE, FORM4_FILE, THIRTEENF_FILE, SHORT_INTEREST_FILE, RAW_DATA_FILE, RATED_FOR_EXTRAS)


async def _refresh_ib_daily_and_hourly(tickers, overwrite):
    """Runs refresh_ib_daily_history and refresh_ib_hourly_history
    concurrently as two asyncio tasks on one event loop -- see
    download_all's own docstring for why -- the same concurrency model
    ib_server.py's own on-demand refreshes use (concurrent coroutines on
    one loop), rather than two separate OS threads each blocking on its
    own sync call. Each still gets its own IBApp() instance/IB Gateway
    connection when connecting directly (see IB_HISTORY_CLIENT_ID/
    IB_HOURLY_HISTORY_CLIENT_ID) -- gathering them on one loop makes the
    IB calls themselves async, it doesn't merge the two connections into
    one."""
    await asyncio.gather(
        refresh_ib_daily_history(IBApp(), tickers, overwrite),
        refresh_ib_hourly_history(IBApp(), tickers, overwrite),
    )


def _run_ib_bar_refresh_in_background(tickers, overwrite):
    """Sync entry point for download_all's own background thread (see
    that function). asyncio.run() needs a thread with no event loop
    already driving it, which download_all's own synchronous call stack
    doesn't have, so this one dedicated thread owns an event loop for
    the life of _refresh_ib_daily_and_hourly's gather -- the same
    "dedicated background thread owns its own asyncio event loop"
    pattern ib_server.py's run_ib_client uses for its own IB connection,
    just torn down again at the end of this one-shot call instead of
    living for the process's whole lifetime. Exceptions aren't expected
    here: refresh_ib_daily_history/refresh_ib_hourly_history each catch
    their own connection/HTTP failures internally and print+return
    rather than raising, so none are caught specifically in this
    wrapper."""
    asyncio.run(_refresh_ib_daily_and_hourly(tickers, overwrite))


def download_all(overwrite=False):
    """Full pipeline: fetch forward P/E data from Yahoo Finance, then the
    momentum score. Also refreshes IB Gateway's own daily AND hourly bars
    (see refresh_ib_daily_history/refresh_ib_hourly_history) for the
    WHOLE active universe (not just ranked/rated/held, unlike
    ib_server.py's own default scope) -- explicit instruction: the
    MFI/RSI daily-strength and hourly overbought/oversold factors should
    be able to count on real IB bars for as much of the universe as
    possible, since the hourly one in particular has no yfinance fallback
    at all. Both run as concurrent asyncio tasks (see
    _refresh_ib_daily_and_hourly) inside ONE background thread (see
    _run_ib_bar_refresh_in_background -- explicit instruction: main.py's
    own direct IB Gateway calls should be async, the way ib_server.py's
    are, not sync calls parallelized across separate OS threads) --
    started right away and joined just before add_momentum_and_persist_
    history (which is what actually reads DAILY_3MO_HISTORY_FILE/
    HOURLY_HISTORY_FILE back off disk) so they overlap with the Yahoo
    Finance/forward-PE work below rather than serializing after it.
    Best-effort: silently skipped if IB Gateway isn't reachable at all
    (see _ib_gateway_reachable). A full-universe pull paced by IB's ~200
    requests/6min limit (HISTORICAL_PACING_MAX_REQUESTS/
    HISTORICAL_PACING_WINDOW_SECONDS, see IBApp.get_ib_historical_bars_
    async) can still take on the order of an hour or more each. Note the
    daily/hourly refreshes' own separate IBApp() instances (see
    IB_HISTORY_CLIENT_ID/IB_HOURLY_HISTORY_CLIENT_ID) still pace
    independently (not a shared budget -- see self._historical_request_
    times' own comment) when connecting to IB Gateway directly, so
    running both at once draws roughly double that rate from the real
    account-wide limit combined; when ib_server.py is already running
    instead, both route through its single connection/budget instead, so
    this doubling doesn't apply there.

    overwrite (only True via `python main.py all overwrite`) forces both
    refreshes through even if an explicit-scope IB refresh already
    completed within the last IB_REFRESH_COOLDOWN_SECONDS (3h) --
    otherwise (the default, also what `ibprices`/`ibhprices` always use,
    with no override of their own) that cooldown makes them no-op
    immediately instead of re-hitting IB Gateway for a full pull that
    just ran. See _ib_refresh_recently_completed/IB_REFRESH_STATE_FILE.

    Tickers downloaded within the last FRESH_HOURS hours are skipped and
    their previous forward_pe.csv row is reused as-is, rather than
    re-fetched."""
    app = IBApp()
    tickers = load_tickers(SYMBOLS_FILE)
    print(f"Loaded {len(tickers)} active tickers from {SYMBOLS_FILE}")

    ib_bar_thread = threading.Thread(target=_run_ib_bar_refresh_in_background, args=(tickers, overwrite), daemon=True)
    ib_bar_thread.start()

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
    print("Waiting for background IB daily/hourly bar refresh to finish before scoring momentum...")
    ib_bar_thread.join()
    add_momentum_and_persist_history(app, data)
    add_target_upside(data)
    add_avg_liquidity_ratio(data)
    write_full_csv(data)
    download_simulations()
    write_sorted_screen_csv(data)
    download_target_portfolio()


def download_prices():
    """Reuse forward-PE data already in forward_pe.csv; only refresh the
    momentum score. IB Gateway's own daily bars (see
    refresh_ib_daily_history) are a separate, on-demand step now --
    `python main.py ibprices` -- not bundled in here, since IB Gateway
    apparently won't accept a second simultaneous API connection while
    ib_server.py is already holding one open (confirmed live: times
    out regardless of clientId), so this needs to be something the user
    chooses to run, typically with the live server stopped, rather than
    something every routine `prices` call silently attempts and skips."""
    app = IBApp()
    data = load_pe_data(OUTPUT_CSV)
    print(f"Loaded {len(data)} tickers from {OUTPUT_CSV}")

    apply_sector_overrides(data, load_sectors(SYMBOLS_FILE))
    add_momentum_and_persist_history(app, data)
    add_target_upside(data)
    add_avg_liquidity_ratio(data)
    write_full_csv(data)
    download_simulations()
    write_sorted_screen_csv(data)
    download_target_portfolio()


def rescore():
    """Rewrites sorted_screen.csv (and forward_pe.csv, for the reapplied
    sector overrides/derived fields) purely from files already on disk --
    zero network calls, unlike download_prices (which hits yfinance once
    per ticker) or download_all. This does include recomputing momentum/
    meanReversion (see add_momentum_from_cache) from whatever IB Gateway's
    own daily/hourly bars (DAILY_3MO_HISTORY_FILE/HOURLY_HISTORY_FILE) and
    price_history.json (yfinance's cached closes, the fallback source)
    currently have on disk -- e.g. right after `ibprices`/`ibhprices`
    refreshed those bars, without needing a full `prices`/`all` fetch just
    to see the new scores. Run `python main.py prices` (or `all`) instead
    if the underlying files themselves are stale and need re-fetching.

    Otherwise, for when only the scoring itself changed (a scoring.py
    formula/weight edit, a manual data fix, a newly backfilled CSV
    column) and every other field on disk is still perfectly good --
    score_rows, add_target_upside, add_avg_liquidity_ratio, and
    write_sorted_screen_csv are all pure computation over data already in
    memory, so there's nothing here that needs a live fetch."""
    data = load_pe_data(OUTPUT_CSV)
    print(f"Loaded {len(data)} tickers from {OUTPUT_CSV}")

    app = IBApp()
    add_momentum_from_cache(app, data)
    apply_sector_overrides(data, load_sectors(SYMBOLS_FILE))
    add_target_upside(data)
    add_avg_liquidity_ratio(data)
    write_full_csv(data)
    download_simulations()
    write_sorted_screen_csv(data)
    download_target_portfolio()
    print(f"Rescored and wrote {SORTED_SCREEN_CSV} (and {OUTPUT_CSV}) -- no network calls made.")


def download_short_interest():
    """Fetches FINRA's latest biweekly equity short interest settlement
    file (see finra.fetch_short_interest) for every RATED_FOR_EXTRAS
    ticker in the ranking as it currently stands on disk -- same scoping
    as download_form4 below, even though FINRA's own file is a single
    bulk download covering the whole market regardless of how many
    tickers get filtered out of it, for consistency with every other
    RATED_FOR_EXTRAS-scoped source (Form 4, 13F, sentiment) and to keep
    SHORT_INTEREST_FILE's own size predictable. A separate download, run
    on its own via `python main.py shortinterest` rather than folded into
    download_all -- it hits a different, independently-rate-limited host
    (FINRA's CDN, not Yahoo Finance), same reasoning as every other
    standalone fetch in this file."""
    tickers = load_rated_tickers(SORTED_SCREEN_CSV, RATED_FOR_EXTRAS)
    if not tickers:
        print(f"No existing {SORTED_SCREEN_CSV} yet; run `python main.py all` first")
        return
    fetch_short_interest(tickers)


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
    sec_edgar.FORM4_FILE), the latest 13F quarter (sec_edgar.
    THIRTEENF_FILE), and FINRA's latest short interest settlement (finra.
    SHORT_INTEREST_FILE + raw_data.json's floatShares) -- zero network
    calls, same "just recompute" reasoning as rescore(). Run via
    `python main.py recommendations` any time after the pieces it reads
    from have been refreshed (`all`/`prices`, `form4`, `13f`,
    `shortinterest`)."""
    write_recommendations(SORTED_SCREEN_CSV, NEWS_FILE, FORM4_FILE, THIRTEENF_FILE, SHORT_INTEREST_FILE, RAW_DATA_FILE, RATED_FOR_EXTRAS)


def _get_held_tickers():
    """Every stock ticker currently held in the IB Gateway account, for
    download_themes' no-arguments case. Connects to IB Gateway directly
    (same IBApp.connect() pattern download_all/download_prices already
    use), not through ib_server.py's HTTP API -- that's a separate
    process that may or may not be running, whereas a direct connection
    is the one pattern every other IB-touching function in this file
    already relies on. secType == "STK" only (matching this project's
    "stocks only" convention elsewhere, e.g. ib_server.py's own
    docstring): an option and its underlying share a ticker symbol,
    which this doesn't disambiguate. Uses IB_HISTORY_CLIENT_ID, not the
    default clientId 0 -- confirmed live that connecting a second client
    with the same id ib_server.py's own persistent connection
    already uses just times out rather than coexisting."""
    app = IBApp()
    app.connect(client_id=IB_HISTORY_CLIENT_ID)
    if not app.is_connected:
        print("Could not connect to IB Gateway (see the error logged above) -- no held tickers to report")
        return []
    positions = app.ib.reqPositions()
    app.disconnect()
    return sorted({p.contract.symbol for p in positions if p.contract.secType == "STK" and p.position != 0})


def download_themes(tickers=None):
    """Classifies tickers' business descriptions against the theme
    taxonomy (see theme_classifier.classify_themes) for the Themes tab.
    With no tickers given, classifies every stock currently held in the
    IB Gateway account instead (see _get_held_tickers) -- run via
    `python main.py themes` with no arguments to recompute for the whole
    portfolio, `python main.py themes TICKER [TICKER ...]` for specific
    ones (e.g. right after opening a brand new position, when you don't
    want to wait on a full account query for just one ticker), or `python
    main.py themes --all` for every RATED_FOR_EXTRAS ticker in the
    ranking (same scoping as download_form4/download_xbrl/download_13f,
    not literally every row in sorted_screen.csv -- Hold-rated names
    aren't worth the local-model compute) UNION every currently held
    ticker (see _get_held_tickers) -- a held position sitting at Hold (or
    one outside the tracked screener universe entirely) would otherwise
    fall through both scopes and never get classified. classify_themes
    only ever fills in tickers with no existing entry (see its own
    docstring), so --all is a safe, idempotent "catch up whatever's
    unclassified" run, not a full reclassification -- it will NOT touch
    or overwrite already-tagged tickers, held or otherwise. This is a
    local model (facebook/bart-large-mnli via transformers, no network
    call beyond the one-time model download) running once per ticker on
    CPU -- --all over hundreds of tickers can take a long while, unlike
    the near-instant held-positions/explicit-ticker cases above."""
    if tickers == ["--all"]:
        tickers = sorted(set(load_rated_tickers(SORTED_SCREEN_CSV, RATED_FOR_EXTRAS)) | set(_get_held_tickers()))
        if not tickers:
            print(f"No existing {SORTED_SCREEN_CSV} yet; run `python main.py all` first")
            return
        print(f"--all: classifying every RATED_FOR_EXTRAS ticker plus every held position ({len(tickers)} total, unclassified ones only)")
    elif tickers:
        tickers = sorted({t.strip().upper() for t in tickers})
    else:
        tickers = _get_held_tickers()
        print(f"No tickers given -- classifying all {len(tickers)} currently held ticker(s): {', '.join(tickers)}")
    classify_themes(tickers)


def run_chat(question):
    """Manual, no-HTTP-layer way to test the Recommendations tab's chatbot
    (see chatbot.answer_question) -- a single question, no chat history, no
    live positions/prices/account (those only exist inside the running
    ib_server.py process; see that module's /api/chat handler for
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


def download_simulations(tickers=None):
    """EPS-driven Monte Carlo price simulation prototype (see
    modules/simulations.py's own docstring for the full formula) -- zero
    network calls, reads forward_pe.csv only, same as rescore(). Explicit
    instruction: defaults to the FULL active universe currently in
    forward_pe.csv (same scope the Screener itself covers -- what feeds
    the Simulations tab) when no tickers are given at all, same as
    `simulations --all`; given specific tickers, runs just those instead
    (e.g. for a quick one-off check). Writes SIMULATIONS_FILE. Every
    single simulated ticker is logged to the terminal as it completes (via
    run_eps_simulations_iter, not the whole-list-at-once run()), so a
    full-universe run's progress is visible the entire time rather than
    going silent until everything finishes."""
    data = load_pe_data(OUTPUT_CSV)
    if not tickers or tickers == ["--all"]:
        tickers = sorted(data.keys())
    else:
        tickers = sorted({t.strip().upper() for t in tickers})
    print(f"Loaded {len(data)} tickers from {OUTPUT_CSV}; simulating {len(tickers)}")

    results = []
    total_tickers = len(tickers)
    count = 0
    for r in run_eps_simulations_iter(tickers, data):
        results.append(r)
        count += 1
        if "error" in r:
            print(f"simulations  {r['ticker']}  {count}/{total_tickers} -- {r['error']}")
        else:
            print(f"simulations  {r['ticker']}  {count}/{total_tickers}")

    with open(SIMULATIONS_FILE, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Wrote {SIMULATIONS_FILE}")

    errored = [r for r in results if "error" in r]
    ok = [r for r in results if "error" not in r]
    with_industry = [r for r in ok if r["priceAtIndustryMultiple"] is not None]
    print(f"{len(ok)}/{len(results)} simulated OK ({len(errored)} skipped -- missing data); "
          f"{len(with_industry)} had enough peers for an industry-median comparison")


def download_target_portfolio():
    """Runs the Sharpe-maximising portfolio optimiser (see modules/
    portfolio_optimizer.py) over the current recommendations.json and
    simulations.json and writes TARGET_PORTFOLIO_FILE. Zero network calls
    -- purely computes from files already on disk. Run via
    `python main.py target`, or called automatically at the end of
    `all`, `prices`, and `rescore` pipelines so TargetView always
    reflects the latest screener and simulation state."""
    try:
        result = build_target_portfolio(RECOMMENDATIONS_FILE, SIMULATIONS_FILE)
    except Exception as exc:
        print(f"target portfolio optimiser failed: {exc}")
        return
    with open(TARGET_PORTFOLIO_FILE, "w") as f:
        json.dump(result, f, indent=2)
    n_long = len(result.get("longs", []))
    n_short = len(result.get("shorts", []))
    stats = result.get("stats", {})
    sharpe = stats.get("sharpe")
    print(f"Wrote {TARGET_PORTFOLIO_FILE}: {n_long}L + {n_short}S"
          + (f", portfolio Sharpe {sharpe:.2f}" if sharpe is not None else ""))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "prices"
    if mode == "all":
        # `all overwrite` bypasses IB_REFRESH_STATE_FILE's 3h cooldown --
        # see download_all's own overwrite param. The only command that can.
        download_all(overwrite=(len(sys.argv) > 2 and sys.argv[2] == "overwrite"))
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
    elif mode == "shortinterest":
        download_short_interest()
    elif mode == "ibprices":
        download_ib_prices()
    elif mode == "ibhprices":
        download_ib_hourly_prices()
    elif mode == "yfprices":
        download_yfinance_prices()
    elif mode == "epsvol":
        download_eps_volatility()
    elif mode == "epscurrentyear":
        download_eps_current_year()
    elif mode == "revgrowth":
        download_revenue_growth()
    elif mode == "grossmargin":
        download_gross_margins()
    elif mode == "insiderown":
        download_insider_ownership()
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
    elif mode == "simulations":
        download_simulations(sys.argv[2:] if len(sys.argv) > 2 else None)
    elif mode == "target":
        download_target_portfolio()
    else:
        sys.exit(
            f"Unknown mode {mode!r}, expected 'all', 'prices', 'rescore', 'form4', 'xbrl', '13f', 'shortinterest', "
            "'ibprices', 'ibhprices', 'yfprices', 'epsvol', 'epscurrentyear', 'revgrowth', 'grossmargin', 'insiderown', 'themes', 'recommendations', 'chat', "
            "'symbol', 'simulations', or 'target'"
        )
