"""
main.py — entry point. Owns the download pipeline and file I/O; every
individual scoring indicator/factor calculation (and score_rows itself,
which combines them) lives in scoring.py instead -- see that module's own
docstring for the full list of factor functions and their weights.

The pipeline is split into two halves, each runnable on its own:

download():        provider fetch only. Merges the raw yfinance `.info`
                    payload into raw_data.json and the raw yfinance
                    statement DataFrames (income_stmt / quarterly / eps_trend
                    / earnings_estimate) into raw_yf_statements.json. Nothing
                    derived, no screen_data.csv. Tickers fetched within the
                    last FRESH_HOURS are skipped (a full-universe run);
                    an explicit ticker list forces a refetch. Run via
                    `python main.py download`.
recalc():          rebuild EVERYTHING derived from the raw dumps on disk
                    -- screen_data.csv (via modules.derive.build_screen_row),
                    momentum, the revenueGrowth reconcile, sorted_screen.csv,
                    simulations, target portfolio, backtest. Zero network by
                    default (`fresh_momentum=True`, used by `all`/`prices`,
                    adds one yfinance trailing-~1mo close pull for the
                    momentum factor). Run via `python main.py recalc`
                    (alias `rescore`).
download_all():    download() then, concurrently, an IB Gateway daily+hourly
                    bar refresh for the whole active universe (best-effort,
                    skipped if IB Gateway isn't reachable) and a StockTwits
                    social-sentiment fetch for the RATED_FOR_EXTRAS tickers,
                    then recalc(fresh_momentum=True) and a history snapshot.
                    `python main.py all` also calls download_eulerpool()
                    right after (see that function's own docstring) --
                    its own blanket 1-day cooldown means it actually
                    refetches on the first `all` run of each day (and
                    no-ops on any later same-day run). `python main.py
                    all` (`all overwrite`
                    bypasses the IB bar-refresh 3h cooldown AND
                    download_eulerpool's own cooldown).
download_prices():  recalc(fresh_momentum=True) -- rebuild from the raw dumps
                    with a fresh momentum-history pull, no `.info`/statement
                    refetch. `python main.py prices`.
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
download_eulerpool(): fetch all five of Eulerpool's own per-ticker datasets
                    (analyst grades, fair value, forward EPS/revenue,
                    short-volume, historical EPS estimates -- see that
                    function's own docstring) for the ENTIRE scored
                    universe. Gated by ONE blanket 1-day cooldown
                    (EULERPOOL_ALL_MAX_AGE_DAYS) across all five -- no-ops
                    entirely if Eulerpool data of any kind was refreshed
                    within the last day, `overwrite` bypasses it. Run
                    via `python main.py eulerpool`, or automatically as
                    part of `python main.py all` (explicit instruction --
                    the cooldown is what makes that safe to do on every
                    `all` run).
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
                    (The old single-factor backfill commands -- epsvol,
                    epscurrentyear, revgrowth, grossmargin, insiderown,
                    stmtcheck -- are gone: recalc() rebuilds every one of
                    those fields from the raw dumps, so a `download` of the
                    stale tickers + `recalc` covers the same ground.)
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
                    existing screen_data.csv/raw_data.json, then rewrite
                    sorted_screen.csv. Run via
                    `python main.py symbol TICKER [TICKER ...]`.
download_simulations(): EPS-driven Monte Carlo price simulation prototype (see
                    modules/simulations.py's own docstring for the formula) --
                    zero network calls, reads screen_data.csv only, same as
                    rescore(). Writes data/output/simulations.json. Run via
                    `python main.py simulations [TICKER ...]` (defaults to the
                    full active universe when no tickers are given, same as
                    `simulations --all`; a specific ticker list runs just those).

Writes (JSON outputs under DATA_DIR ("data/"); CSVs and symbols.json, a
hand-maintained input rather than generated output, stay at the project
root):
  data/raw_data.json  the complete, unfiltered yfinance `info` payload per
                     ticker (every field Yahoo exposes), for discovering
                     fields not yet curated into screen_data.csv.
  screen_data.csv    all tickers, sorted by forwardPE ascending.
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
                     percentile (top/bottom 7.5% = Strong Buy/Strong Sell,
                     next 12.5% each = Buy/Sell, middle 60% = Hold — same
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
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

from modules.IBApp import IBApp
from modules.scoring import (
    RATING_NA,
    add_avg_liquidity_ratio,
    add_target_upside,
    analyst_consensus_score,
    clamp_eps_revision,
    load_fair_value_scores,
    load_guidance_scores,
    load_insider_scores,
    load_institutional_scores,
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
from modules.sector_groups import get_sector_group
from modules import derive
from modules.backtest import build_backtest
from modules.recommendations import write_recommendations
from modules.sec_edgar import (
    FORM4_FILE,
    GUIDANCE_FILE,
    GUIDANCE_SIGNAL_FILE,
    THIRTEENF_FILE,
    XBRL_FACTS_FILE,
    fetch_13f_holdings,
    fetch_earnings_guidance,
    fetch_form4,
    fetch_xbrl_facts,
    summarize_guidance,
)
from modules.eulerpool import (
    EPS_ESTIMATES_FILE,
    FAIR_VALUE_FILE,
    FORWARD_EPS_FILE,
    GRADES_FILE,
    REVENUE_ESTIMATES_FILE,
    SHORT_VOLUME_FILE,
    TRANSCRIPTS_FILE,
    fetch_analyst_grades,
    fetch_eps_estimates,
    fetch_fair_values,
    fetch_forward_eps,
    fetch_short_volume,
    fetch_transcripts,
)
from modules.social_sentiment import SENTIMENT_FILE, fetch_social_sentiment
from modules.theme_classifier import classify_themes

# Every JSON file a downloader (this module, ib_server.py,
# social_sentiment.py) produces lives here -- keeps the project root from
# filling up with generated output. CSVs (screen_data.csv, sorted_screen.csv)
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
# get_forward_pe's own curated subset (screen_data.csv) -- kept in its
# own subfolder, the yfinance-side counterpart to IB_DIR above (explicit
# instruction).
YFINANCE_DIR = os.path.join(DATA_DIR, "yfinance")
os.makedirs(YFINANCE_DIR, exist_ok=True)

OUTPUT_CSV = os.path.join(YFINANCE_DIR, "screen_data.csv")
SORTED_SCREEN_CSV = os.path.join(OUTPUT_DIR, "sorted_screen.csv")
RAW_DATA_FILE = os.path.join(YFINANCE_DIR, "raw_data.json")
# Raw yfinance statement DataFrames (income_stmt / quarterly_income_stmt /
# eps_trend / earnings_estimate), serialised. Written by download(),
# consumed by recalc() via modules.derive. Replaces statement_check.json,
# whose contents were half our own derivations.
RAW_STATEMENTS_FILE = os.path.join(YFINANCE_DIR, "raw_yf_statements.json")
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
# Same optimiser, Financial Services + Healthcare + Real Estate sector
# groups excluded.
TARGET_PORTFOLIO_EX_FILE = os.path.join(OUTPUT_DIR, "target_portfolio_ex.json")
TARGET_PORTFOLIO_EX_GROUPS = {"Financial Services", "Healthcare", "Real Estate"}
# Dated screen snapshots (sorted_screen <YYYYMMDD>.csv) the backtest scores
# forward against IB's daily bars -- see modules/backtest.py.
HISTORY_DIR = os.path.join(OUTPUT_DIR, "history")
BACKTEST_FILE = os.path.join(OUTPUT_DIR, "backtest.json")
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
    # Per-share book value -- feeds modules.simulations' own fundamental
    # price floor (BOOK_VALUE_FLOOR_MULTIPLE).
    "bookValue",
    "enterpriseValue", "sharesOutstanding", "impliedSharesOutstanding", "enterpriseToEbitda", "beta", "debtToEquity", "LiqRatio", "quickRatio", "currentRatio", "shortRatio", "shortPercentOfFloat",
    "revenueGrowth", "revenueGrowthSource",
    # earningsGrowth is the SCORED blend (see derive.reconcile_earnings_growth);
    # earningsGrowthQ keeps the raw yfinance most-recent-quarter YoY figure.
    "earningsGrowth", "earningsGrowthQ", "earningsGrowthSource",
    # earningsMarginDelta is what earnings_growth_rank actually scores on
    # (see derive.earnings_margin_delta) -- YoY net-margin change per share.
    "earningsMarginDelta", "earningsMarginDeltaSource",
    "returnOnEquity", "profitMargins", "revenuePerShare", "operatingMargins", "grossMargins", "price",
    # yfinance statement cross-check figures (modules.derive.statement_metrics,
    # from raw_yf_statements.json) -- used by the revenueGrowth / earningsGrowth
    # reconciles and available to the Simulations forward-EPS anchor.
    "annualRevenueGrowth", "ttmRevenueGrowth", "latestQuarterEnd",
    "dilutedEpsAnnual", "dilutedEpsGrowth",
    "fwdEps0y", "fwdEps1y", "estimateGrowth1y", "estimateAnalysts", "eulerRevGrowth0y", "eulerRevGrowth1y", "eulerFwdEps2y", "eulerRevGrowth2y",
    "targetMeanPrice", "targetHighPrice", "targetLowPrice", "targetUpside", "recommendationKey",
    "recommendationMean", "numberOfAnalystOpinions", "momentum", "meanReversion", "entryTiming", "earningsMsi", "epsRevision0y",
    "epsRevision1y", "epsVolatility", "heldPercentInsiders", "earningsTimestampStart", "yearReturn", "lastDownload",
    # Trailing average reported-vs-estimate EPS surprise % over the last
    # EARNINGS_SURPRISE_LOOKBACK_QUARTERS actually-reported quarters (see
    # derive.earnings_surprise_from_statements) -- a DIFFERENT signal from
    # epsRevision0y/1y above (analyst ESTIMATES moving before the print);
    # this is the actual beat/miss TRACK RECORD after the fact.
    "earningsSurpriseAvg",
    # Recency-weighted read on the MOST RECENT surprise alone, decayed to
    # 0 over derive.PEAD_DECAY_DAYS (see derive.earnings_pead_from_statements)
    # -- a post-earnings-announcement-drift read, distinct from the
    # slow-moving track record above.
    "earningsPead",
    # Trailing 1-month annualized price volatility (see
    # derive.reconcile_price_volatility) -- feeds RecommendationsView.tsx's
    # low-volatility gate (VOL_GATE_MIN_ANNUALIZED), catching stocks frozen
    # at/near an acquisition price rather than being a scored factor.
    "priceVolAnnualized",
    # Last completed day's move and its size in sd of the prior ~3 months'
    # daily returns (see derive.reconcile_daily_move) -- feeds the daily-move
    # entry gate (no new long above +1 sd, no new short below -1 sd).
    "dailyMove",
    "dailyMoveZ",
    # 10-day Trend Score (derive.reconcile_trend) -- an ENTRY FILTER only,
    # not scored: no new long at <= 35, no new short at >= 65.
    "trend",
]
# FINRA biweekly short-interest figures (finra.SHORT_INTEREST_FILE +
# raw_data.json floatShares, via scoring.load_short_interest_scores) -- the
# EXACT values short_interest_rank scores on, so the Screener's short-
# interest column shows what scoring actually used rather than yfinance's
# staler shortPercentOfFloat/shortRatio pair (which, for a recent IPO like
# NAVN, still reflects the tiny immediate-post-IPO float -- 32.7% vs. the
# real 6.3%). sorted_screen.csv only, not screen_data.csv (which predates
# the FINRA fetch and has no equivalent column). Blank for a ticker FINRA
# doesn't report -- the frontend falls back to shortPercentOfFloat there.
SCREEN_ONLY_FIELDNAMES = [
    "shortPctOfFloatFinra", "shortDaysToCover", "shortChangePercent",
    # Eulerpool's own 10-trading-day trailing average of FINRA's DAILY
    # short-sale volume tape (modules.eulerpool.fetch_short_volume) -- a
    # fourth, independent short-interest leg alongside the three FINRA
    # biweekly-settlement fields above (see scoring.short_interest_rank).
    "shortVolumeRatio",
    # Eulerpool's own [-1, 1] aggregate sell-side stance, RIGHT NOW (see
    # scoring.analyst_consensus_score) -- most-recent grade per firm,
    # averaged. Distinct from targetUpside (price-target math) and from
    # numberOfAnalystOpinions -- this is what coverage currently THINKS,
    # not what price they think it's worth.
    "analystConsensus",
    # simulations.json's own simReturn (see write_sorted_screen_csv's own
    # comment on the _mc_data merge above) -- was already merged into each
    # row dict at write time but never actually persisted to the CSV
    # since it wasn't in this list. Added so modules/backtest.py can start
    # reconstructing the sim-return gate (simReturnOkForLong/ForShort in
    # RecommendationsView.tsx) for future dated snapshots -- it couldn't
    # before, since simReturn was never archived. Only helps GOING
    # FORWARD; every already-archived sorted_screen <date>.csv predates
    # this column and stays without it.
    "simReturn",
]
# sorted_screen.csv shows sector last instead of right after name.
SCREEN_FIELDNAMES = [f for f in FIELDNAMES if f != "sector"] + SCREEN_ONLY_FIELDNAMES + ["sector"]


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
    """Reads an existing screen_data.csv into {ticker: {field: value}}."""
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


def augment_forward_ebitda_inputs(data):
    """Backfill fields needed for scoring.forward_ev_ebitda from raw_data.json.
    Existing screen_data.csv rows from before those fields were curated can
    then be rescored without a network refresh."""
    try:
        with open(RAW_DATA_FILE) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    for symbol, d in data.items():
        raw_row = raw.get(symbol) or {}
        for field in ("enterpriseValue", "sharesOutstanding", "impliedSharesOutstanding"):
            if d.get(field) in (None, "") and raw_row.get(field) is not None:
                d[field] = raw_row.get(field)


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

    Returns [] if the file doesn't exist yet (e.g. first-ever run).

    Cross-checked against symbols.json's LIVE active flag (see
    _active_only) before returning -- sorted_screen.csv only reflects
    whichever tickers were active as of the last `recalc`, so a ticker
    deactivated since then (e.g. from a live IB "Unknown contract"
    warning) would otherwise keep being treated as part of the
    live-priced/downloadable universe until the next recalc happened to
    rebuild the file. Explicit instruction: deactivating a ticker must
    stop it from being fetched immediately, not just eventually."""
    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            scored = (row for row in reader if row.get("score"))
            if n is None:
                return _active_only([row["ticker"] for row in scored])
            return _active_only([row["ticker"] for _, row in zip(range(n), scored)])
    except FileNotFoundError:
        return []


def load_rated_tickers(path, ratings):
    """Reads tickers from an existing sorted_screen.csv whose `rating`
    column (see scoring.rating_for_percentile) is one of `ratings` -- e.g.
    RATED_FOR_EXTRAS, every Strong Buy/Buy/Sell/Strong Sell ticker,
    skipping the broad Hold middle and the unranked/NA rows. Returns []
    if the file doesn't exist yet (e.g. first-ever run). Cross-checked
    against symbols.json's live active flag -- see load_top_tickers' own
    comment for why."""
    try:
        with open(path, newline="") as f:
            return _active_only([row["ticker"] for row in csv.DictReader(f) if row.get("rating") in ratings])
    except FileNotFoundError:
        return []


def load_all_tickers(path):
    """Every ticker in an existing sorted_screen.csv, regardless of
    rating -- explicit instruction: form4/xbrl/eulerpool all download for
    the ENTIRE scored universe now, not just load_rated_tickers'
    RATED_FOR_EXTRAS subset (a Hold-rated name today can become a Buy/Sell
    next week as fundamentals shift, and this factor data is worth having
    on file before that happens rather than fetched reactively). Returns
    [] if the file doesn't exist yet (e.g. first-ever run). Cross-checked
    against symbols.json's live active flag -- see load_top_tickers' own
    comment for why."""
    try:
        with open(path, newline="") as f:
            return _active_only([row["ticker"] for row in csv.DictReader(f) if row.get("ticker")])
    except FileNotFoundError:
        return []


def _active_only(tickers):
    """Filters `tickers` down to symbols.json's LIVE active set -- shared
    by load_top_tickers/load_rated_tickers/load_all_tickers so a ticker
    deactivated after sorted_screen.csv was last written can never leak
    back into a download scope through one of those, regardless of which
    stale rating/score/row it still has on file."""
    active = set(load_tickers(SYMBOLS_FILE))
    return [t for t in tickers if t in active]


def _load_json_or_empty(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def add_momentum(app, data, history_out=None, checkpoint=None):
    """Adds momentum + meanReversion (see IBApp.get_momentum -- momentum
    from DAILY_3MO_HISTORY_FILE where it covers a ticker, else the plain
    trailing-~1-month yfinance calculation; meanReversion from
    HOURLY_HISTORY_FILE only, no fallback) to each entry in data, in
    place. If history_out is a dict, also captures each ticker's daily
    close series from that same yfinance fetch (see write_price_history)
    — no extra network round-trip since IBApp.get_momentum already pulls
    it. checkpoint is passed straight through to get_momentum (partial
    price-history save mid-fetch)."""
    momentum = app.get_momentum(
        list(data.keys()),
        history_out=history_out,
        checkpoint=checkpoint,
        daily_3mo_by_ticker=_load_json_or_empty(DAILY_3MO_HISTORY_FILE),
        hourly_by_ticker=_load_json_or_empty(HOURLY_HISTORY_FILE),
    )
    for symbol, d in data.items():
        result = momentum.get(symbol) or {}
        d["momentum"] = result.get("momentum")
        d["meanReversion"] = result.get("mean_reversion")
        d["earningsMsi"] = result.get("earnings_msi")


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
        d["earningsMsi"] = result.get("earnings_msi")


def _latest_expected_close_date(now=None):
    """ISO date of the most recent trading day whose daily close should
    already be published. Weekend-aware (steps back over Sat/Sun) and
    steps back one more day when it's still early in the UTC day and the
    US cash close for `today` may not have landed yet. Holidays are NOT
    modelled: on a market holiday this points at a day with no bar, so
    every ticker just looks stale and gets refetched -- harmless, just the
    old full-refresh behaviour for that one run."""
    now = now or datetime.now(timezone.utc)
    d = now.date()
    if now.hour < 22:  # ~US cash close + settle margin, in UTC
        d -= timedelta(days=1)
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d -= timedelta(days=1)
    return d.isoformat()


def add_momentum_and_persist_history(app, data, force=False):
    """add_momentum, plus merging the close-series it captures into
    price_history.json — the common case across all three download_*
    entry points (download_all/download_prices/download_yfinance_prices
    -- the last of these is `python main.py yfprices`, the dedicated
    yfinance-only command). Also updates MISSINGS_FILE's "yfinance" key
    (see _update_missings) with every ticker the fetch came back with no
    closes for at all -- yf.Ticker(symbol).history() either raised on
    all 3 attempts (see IBApp.get_momentum's own fetch closure) or
    returned zero rows.

    Incremental by default: a ticker whose cached price_history.json
    series already ends on _latest_expected_close_date() is NOT re-fetched
    -- its momentum/meanReversion are recomputed from the cached closes
    (get_momentum_from_disk) instead, and only the stale/missing tickers
    hit the network. get_momentum also checkpoints price_history.json part
    way through the fetch, so an interrupted run keeps its progress and
    the next run resumes on just the remainder. force=True re-fetches the
    whole universe regardless (CLI: `prices overwrite` / `yfprices
    overwrite`)."""
    cached = _load_json_or_empty(PRICE_HISTORY_FILE)
    expected = _latest_expected_close_date()

    def _is_fresh(t):
        series = cached.get(t)
        return bool(series) and (series[-1].get("date") or "") >= expected

    fresh = set() if force else {t for t in data if _is_fresh(t)}
    stale = [t for t in data if t not in fresh]
    if fresh:
        print(f"price_history.json: {len(fresh)} ticker(s) already current "
              f"(close on {expected}); fetching {len(stale)}")

    history = {}
    if stale:
        add_momentum(
            app, {t: data[t] for t in stale}, history_out=history,
            checkpoint=lambda partial: write_price_history({**cached, **partial}),
        )
    if fresh:
        disk_mom = app.get_momentum_from_disk(
            sorted(fresh),
            daily_3mo_by_ticker=_load_json_or_empty(DAILY_3MO_HISTORY_FILE),
            hourly_by_ticker=_load_json_or_empty(HOURLY_HISTORY_FILE),
            yfinance_history_by_ticker=cached,
        )
        for t, m in disk_mom.items():
            data[t]["momentum"] = m.get("momentum")
            data[t]["meanReversion"] = m.get("mean_reversion")
            data[t]["earningsMsi"] = m.get("earnings_msi")

    all_history = {**_load_json_or_empty(PRICE_HISTORY_FILE), **history}
    write_price_history(all_history)
    _update_missings("yfinance", [t for t in stale if not history.get(t)])


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
    opening a second one (see that function's docstring for why).

    Returns that process's own OS PID (via GET /api/admin/pid -- see
    ib_server.py's _handle_pid) on success, or None if unreachable --
    callers only ever check truthiness (a PID is never 0), so this reads
    the same as the plain bool it used to return, but also gives log
    lines something concrete to name ("deferring to PID 12345") instead
    of just "ib_server.py is already running." Falls back to True (still
    "running", just with no PID to report) if the process answers but
    this specific request fails for some other reason -- a partial
    failure here shouldn't make an otherwise-healthy server look down."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/last-prices", timeout=timeout) as _:
            pass
    except (urllib.error.URLError, OSError):
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/admin/pid", timeout=timeout) as resp:
            return json.loads(resp.read()).get("pid") or True
    except (urllib.error.URLError, OSError, ValueError):
        return True


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


def _call_ib_server_job_endpoint(path, port, timeout, tickers, overwrite):
    """POST an ib_server.py admin endpoint that runs as a tracked
    _current_job (see that module's _handle_refresh_ib_daily/-hourly --
    both now claim that job slot and log through it via log_fn) and print
    its progress to THIS process's own terminal as it happens, instead of
    just blocking silently until the single (up to 2hr) response finally
    arrives -- explicit fix: routing through ib_server.py's own IB Gateway
    connection (see _ib_server_running's own comment on why this routing
    exists at all) used to mean this process had zero visibility into
    progress, only ib_server.py's own stdout did.

    Fires the actual request in a background thread (so this thread is
    free to poll) and polls GET /api/admin/run-status every 10s, printing
    only the log lines not already seen (log is append-only and never
    shrinks while a job runs, so a length-based watermark is enough) --
    each one prefixed to make clear it's ib_server.py's own progress, not
    this process's. Returns the request thread's actual JSON result
    ({"skipped": bool, "tickersTotal": int, ...} or {"error": str}); a
    poll failure is swallowed and retried next cycle rather than treated
    as fatal -- a missed progress line doesn't mean the actual fetch
    (running in the other thread, on ib_server.py's own connection)
    failed too."""
    data = json.dumps({"tickers": tickers, "overwrite": overwrite}).encode() if tickers is not None or overwrite else b""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST", data=data)

    result_holder = {}

    def _do_request():
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result_holder["result"] = json.loads(resp.read())

    thread = threading.Thread(target=_do_request, daemon=True)
    thread.start()

    seen = 0
    while thread.is_alive():
        thread.join(timeout=10)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/admin/run-status", timeout=5) as resp:
                status = json.loads(resp.read())
        except (urllib.error.URLError, OSError, ValueError):
            continue
        log = status.get("log", [])
        for line in log[seen:]:
            print(f"  [ib_server.py] {line}")
        seen = len(log)

    return result_holder.get("result", {"error": "ib_server.py request thread ended with no result"})


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
    can take a while. See _call_ib_server_job_endpoint for how progress
    reaches this process's own terminal while that's happening."""
    return _call_ib_server_job_endpoint("/api/admin/refresh-ib-daily", port, timeout, tickers, overwrite)


def _refresh_ib_hourly_via_server(port=IB_SERVER_PORT, timeout=7200, tickers=None, overwrite=False):
    """POST /api/admin/refresh-ib-hourly on ib_server.py's already-running
    process (see that module's refresh_hourly_history_on_demand) -- the
    hourly-bars twin of _refresh_ib_daily_via_server, same shape/timeout/
    tickers/overwrite-passthrough/progress-polling reasoning."""
    return _call_ib_server_job_endpoint("/api/admin/refresh-ib-hourly", port, timeout, tickers, overwrite)


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


def _merge_bar_series(existing, fresh):
    """{ticker: [bars]} merge for DAILY_3MO_HISTORY_FILE/HOURLY_HISTORY_FILE
    -- combines `fresh` (a batch of IB's own trailing-window bars, e.g.
    "3 M") into `existing` BY DATE per ticker, rather than the
    `existing.update(fresh)` this replaced, which let a routine refetch
    silently drop every bar older than that request's own window. IB's
    "3 M" duration always means "3 months back from right now," so a
    ticker refetched today loses everything before ~3 months ago under
    plain dict.update -- confirmed exactly this way live: backtesting a
    week from August would eventually go dark once "today" drifted far
    enough past it, purely because a LATER, unrelated refresh happened to
    touch that ticker again. Explicit instruction: price history (or any
    other data) must never be removed just because new data came in --
    these files should only ever grow.

    A ticker only in `existing` is kept untouched. A ticker only in
    `fresh` is added. A ticker in both has its old and new bars unioned by
    `date` (fresh's own bar wins on an exact-date collision -- a
    revised/completed bar should replace a stale one, not be dropped),
    sorted back into date order. An EMPTY fresh result for a ticker (the
    fetch came back with nothing -- a bad symbol, a transient API error)
    leaves `existing` for that ticker alone rather than erasing it --
    same "never remove on a failed/partial fetch" principle.

    Returns a new dict; does not mutate either input."""
    merged = dict(existing)
    for ticker, new_bars in fresh.items():
        if not new_bars:
            continue
        old_bars = existing.get(ticker) or []
        by_date = {b.get("date"): b for b in old_bars}
        by_date.update({b.get("date"): b for b in new_bars})
        merged[ticker] = sorted(by_date.values(), key=lambda b: b.get("date") or "")
    return merged


def _prune_inactive_tickers(bars_dict, keep_extra=()):
    """{ticker: [bars]} with every key NOT in symbols.json's live active
    set (see load_tickers) OR `keep_extra` removed -- explicit
    instruction: a deactivated ticker (GBTG, CRML, the "Unknown contract"
    batch, etc.) should eventually disappear from DAILY_3MO_HISTORY_FILE/
    HOURLY_HISTORY_FILE entirely, not just stop being refetched. This is a
    DELIBERATE prune, distinct from _merge_bar_series's own "never drop a
    bar just because a refetch happened" guarantee just above -- that one
    protects against ACCIDENTALLY losing history for a ticker still in
    scope; this one INTENTIONALLY drops a ticker that's fallen out of
    scope entirely. `keep_extra` is this call's own `tickers` argument
    (whatever scope the caller actually requested) -- a currently-HELD
    position that's been deactivated in symbols.json still needs its
    price history for the Positions/Recommendations "To close" review
    (see refresh_ib_daily_history's own held-ticker union), so it's kept
    as long as some caller is still explicitly asking for it, even though
    it's no longer in the live active set on its own.

    Returns a new dict; does not mutate the input. Call sites print how
    many tickers this actually dropped, so a prune is visible in the
    logs, not silent."""
    keep = set(load_tickers(SYMBOLS_FILE)) | set(keep_extra)
    return {t: v for t, v in bars_dict.items() if t in keep}


async def download_ib_daily_history(app, tickers):
    """Refreshes DAILY_3MO_HISTORY_FILE (IB Gateway's own 3-month daily
    bars -- see that constant's own comment) for `tickers`, via an
    already-connected `app`. Only actually fetches a ticker whose
    existing entry is missing or older than
    most_recent_completed_trading_day() -- IB's paced historical-data
    limit (200 requests/6min, see IBApp.get_ib_historical_bars_async)
    still makes a large stale ticker list take a while, and this runs on
    every `prices`/`all` call, so a day where the data's already current
    does no IB Gateway work at all. Merges BY DATE into the existing file
    (see _merge_bar_series) rather than replacing a refetched ticker's
    whole array wholesale -- explicit instruction: never let a later
    refresh silently drop bars older than that request's own "3 M"
    window (IB's "3 M" is always relative to NOW, not to when the file
    was first seeded, so plain dict.update used to lose history here).
    Async (awaits
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
    existing = _merge_bar_series(existing, fresh)
    before = len(existing)
    existing = _prune_inactive_tickers(existing, keep_extra=tickers)
    pruned = before - len(existing)
    with open(DAILY_3MO_HISTORY_FILE, "w") as f:
        json.dump(existing, f)
    got = sum(1 for v in fresh.values() if v)
    print(f"Wrote {DAILY_3MO_HISTORY_FILE} ({got}/{len(stale)} fetched tickers had bars; {len(existing)} tickers total on file"
          + (f", {pruned} inactive ticker(s) pruned" if pruned else "") + ")")
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
    running_pid = _ib_server_running()
    if running_pid:
        pid_note = f" (PID {running_pid})" if isinstance(running_pid, int) else ""
        print(f"ib_server.py is already running on port {IB_SERVER_PORT}{pid_note} -- refreshing via its own IB Gateway connection instead of opening a second one")
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
    """Refreshes HOURLY_HISTORY_FILE (IB Gateway's own 3-month hourly
    bars) for `tickers`, via an already-connected `app` -- the hourly
    twin of download_ib_daily_history, same staleness gate (only a
    ticker whose existing entry is missing or older than
    most_recent_completed_trading_day() gets refetched -- date-only,
    same as the daily check, even though these bars carry a time-of-day
    too: "has at least one bar from the most recent session" is what
    matters here, not which hour), same _merge_bar_series merge-by-date
    (never dropping older bars a plain dict.update would have), same
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
    print(f"Fetching IB 3mo hourly bars for {len(stale)}/{len(tickers)} stale/missing ticker(s) (paced, can take a while)...")
    fresh = await app.get_ib_historical_bars_async(stale, "3 M", "1 hour", on_ticker=_progress_printer("Hourly", len(stale)))
    existing = _merge_bar_series(existing, fresh)
    before = len(existing)
    existing = _prune_inactive_tickers(existing, keep_extra=tickers)
    pruned = before - len(existing)
    with open(HOURLY_HISTORY_FILE, "w") as f:
        json.dump(existing, f)
    got = sum(1 for v in fresh.values() if v)
    print(f"Wrote {HOURLY_HISTORY_FILE} ({got}/{len(stale)} fetched tickers had bars; {len(existing)} tickers total on file"
          + (f", {pruned} inactive ticker(s) pruned" if pruned else "") + ")")
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
    running_pid = _ib_server_running()
    if running_pid:
        pid_note = f" (PID {running_pid})" if isinstance(running_pid, int) else ""
        print(f"ib_server.py is already running on port {IB_SERVER_PORT}{pid_note} -- refreshing via its own IB Gateway connection instead of opening a second one")
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


def download_yfinance_prices(force=False):
    """Refreshes price_history.json (see
    add_momentum_and_persist_history/write_price_history) on its own, via
    `python main.py yfprices` (add `overwrite` to force a full re-pull
    instead of the default incremental "only stale tickers" fetch) -- the
    yfinance-only counterpart to
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
    into screen_data.csv/sorted_screen.csv, but nothing here writes
    those, only price_history.json."""
    app = IBApp()
    tickers = load_tickers(SYMBOLS_FILE)
    print(f"Loaded {len(tickers)} active tickers from {SYMBOLS_FILE}")
    add_momentum_and_persist_history(app, {t: {} for t in tickers}, force=force)


FRESH_HOURS = 8


def is_fresh(last_download, max_age_hours=FRESH_HOURS):
    """True if last_download (an ISO datetime string, e.g. from a previous
    screen_data.csv row) is within max_age_hours of now. Used to skip
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
    in screen_data.csv instead of silently dropping them from every output.
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
    screen_data.csv. Useful for discovering fields to add later."""
    with open(RAW_DATA_FILE, "w") as f:
        json.dump(raw_info, f, indent=2, default=str)
    print(f"Wrote {RAW_DATA_FILE}")


def write_raw_statements(raw_stmts):
    """Writes the raw serialised yfinance statement DataFrames per ticker
    (see RAW_STATEMENTS_FILE). Provider output only -- every derived value
    comes off this in recalc() via modules.derive."""
    with open(RAW_STATEMENTS_FILE, "w") as f:
        json.dump(raw_stmts, f, default=str)
    print(f"Wrote {RAW_STATEMENTS_FILE} ({sum(1 for v in raw_stmts.values() if v)} tickers with data)")


def build_screen_rows():
    """The screen_data.csv row dict for every active ticker that has a raw
    yfinance .info dump -- built purely from RAW_DATA_FILE +
    RAW_STATEMENTS_FILE via modules.derive.build_screen_row. No network.
    This is what IBApp.get_forward_pe used to assemble inline during the
    fetch. symbols.json sector overrides are layered on by the caller."""
    raw_info = _load_json_or_empty(RAW_DATA_FILE)
    raw_stmts = _load_json_or_empty(RAW_STATEMENTS_FILE)
    active = set(load_tickers(SYMBOLS_FILE))
    data = {}
    for ticker, info in raw_info.items():
        if ticker not in active or not isinstance(info, dict) or info.get("error"):
            continue
        data[ticker] = derive.build_screen_row(info, raw_stmts.get(ticker))
    return data


# yfinance statement DataFrames (4 calls/ticker) change quarterly, not
# hourly, and throttle far harder than .info -- so their freshness is
# tracked independently (a longer TTL, against RAW_STATEMENTS_FILE's own
# per-entry _fetchedAt) and they're fetched in chunks that each merge +
# flush, so a killed/throttled run keeps everything gathered so far.
STATEMENTS_FRESH_HOURS = 72
STATEMENTS_CHUNK = 80


def download(tickers=None):
    """Provider fetch only -- no derived output, no screen_data.csv. Merges
    the raw yfinance `.info` payload into RAW_DATA_FILE and the raw
    statement DataFrames into RAW_STATEMENTS_FILE. `tickers` defaults to
    the whole active universe; an explicit list forces a refetch, otherwise
    a ticker fresh within FRESH_HOURS (.info) / STATEMENTS_FRESH_HOURS
    (statements) is skipped. Run via `python main.py download`; `python
    main.py all` runs this then recalc(). SEC / FINRA / IB-bar / sentiment
    fetches stay their own separate commands."""
    app = IBApp()
    explicit = bool(tickers)
    universe = sorted({t.strip().upper() for t in tickers}) if explicit else load_tickers(SYMBOLS_FILE)
    print(f"download: {len(universe)} tickers")

    # --- yfinance .info ---
    raw_info = _load_json_or_empty(RAW_DATA_FILE)
    info_stale = universe if explicit else [
        t for t in universe if not is_fresh((raw_info.get(t) or {}).get("lastDownload"))
    ]
    if info_stale:
        fetched = app.get_yf_info(info_stale, usa_only=True, country_overrides=COUNTRY_OVERRIDE_TICKERS)
        raw_info.update(fetched)
        write_raw_data(raw_info)
    print(f"  info: {len(info_stale)} stale, {len(universe) - len(info_stale)} fresh")

    # --- yfinance statements (independent freshness, chunked + merged) ---
    raw_stmts = _load_json_or_empty(RAW_STATEMENTS_FILE)
    us = [
        t for t in universe
        if isinstance(raw_info.get(t), dict) and not raw_info[t].get("error")
        and (raw_info[t].get("country") == "United States" or t in (COUNTRY_OVERRIDE_TICKERS or ()))
    ]
    # A cached entry missing "earningsDates" entirely counts as stale too,
    # regardless of _fetchedAt -- a one-time backfill condition so a
    # newly-added statement key (see get_yf_statements' own getter tuple)
    # reaches every ticker on its next `download` rather than waiting out
    # the full 72h freshness window for each one, or requiring an
    # expensive explicit-list refetch of the whole universe just to pick
    # up one new field. Once every cached entry has been refreshed at
    # least once past this point, this clause is permanently a no-op --
    # left in rather than removed, since it costs nothing and protects
    # the next such addition too.
    stmt_stale = us if explicit else [
        t for t in us
        if "earningsDates" not in (raw_stmts.get(t) or {})
        or not is_fresh((raw_stmts.get(t) or {}).get("_fetchedAt"), STATEMENTS_FRESH_HOURS)
    ]
    print(f"  statements: {len(stmt_stale)} to fetch (of {len(us)} US-domiciled)")
    now = datetime.now().isoformat(timespec="seconds")
    for i in range(0, len(stmt_stale), STATEMENTS_CHUNK):
        chunk = stmt_stale[i:i + STATEMENTS_CHUNK]
        got = app.get_yf_statements(chunk, max_workers=2)
        for ticker, entry in got.items():
            if entry:  # empty {} = all 4 calls threw; keep any prior entry
                entry["_fetchedAt"] = now
                raw_stmts[ticker] = entry
        write_raw_statements(raw_stmts)
        with_data = sum(1 for v in raw_stmts.values() if v.get("incomeStmt") or v.get("quarterlyIncomeStmt"))
        print(f"    [{min(i + STATEMENTS_CHUNK, len(stmt_stale))}/{len(stmt_stale)}] {with_data} tickers with statement data")


def recalc(fresh_momentum=False, force_prices=False):
    """Rebuilds every derived artifact from the raw provider dumps on disk.
    screen_data.csv (from RAW_DATA_FILE + RAW_STATEMENTS_FILE via
    modules.derive), momentum, the revenueGrowth reconcile against
    company_facts.json, then sorted_screen.csv + simulations + target
    portfolio + backtest. Run via `python main.py recalc` (alias
    `rescore`); `all` runs download() then this.

    fresh_momentum=True (used by `all` / `prices`) does the one yfinance
    call recalc otherwise avoids -- the trailing ~1mo daily closes that
    add_momentum_and_persist_history needs for tickers with no IB bars,
    persisted to price_history.json. That fetch is INCREMENTAL: only
    tickers whose cached close isn't already current get re-pulled unless
    force_prices=True. The default (fresh_momentum=False) reads only
    cached bars, so `recalc` on its own makes ZERO network calls."""
    app = IBApp()
    data = build_screen_rows()
    print(f"recalc: built {len(data)} screen rows from raw dumps")
    apply_sector_overrides(data, load_sectors(SYMBOLS_FILE))
    if fresh_momentum:
        add_momentum_and_persist_history(app, data, force=force_prices)
    else:
        add_momentum_from_cache(app, data)
    # Overwrites the MSI-based momentum add_momentum_from_cache/
    # add_momentum_and_persist_history just set with the next-day Reversal
    # Score (see derive.reconcile_momentum's own comment) -- zero network
    # calls, reads DAILY_3MO_HISTORY_FILE straight off disk, same as every
    # other reconcile_* below (the hourly file is passed but unused) -- this
    # is also
    # where the retired reconcile_mean_reversion/meanReversion call used
    # to live (see derive.py's own retirement comment on that function).
    derive.reconcile_momentum(
        data, _load_json_or_empty(DAILY_3MO_HISTORY_FILE), _load_json_or_empty(HOURLY_HISTORY_FILE)
    )
    # Trend Score as its own `trend` field -- entry filter only, no scoring
    # weight (see derive.reconcile_trend's section comment).
    derive.reconcile_trend(
        data, _load_json_or_empty(DAILY_3MO_HISTORY_FILE), _load_json_or_empty(HOURLY_HISTORY_FILE)
    )
    # New, deliberately unscored field -- a 1-trading-day-ahead entry-
    # timing read (35h hourly formation, see derive.reconcile_entry_timing's
    # own comment for why this is a different window/horizon from
    # meanReversion above, not a duplicate of it). No FACTOR_WEIGHTS entry.
    derive.reconcile_entry_timing(data, _load_json_or_empty(HOURLY_HISTORY_FILE))
    # Low-volatility gate input (see derive.reconcile_price_volatility's own
    # comment) -- catches acquisition-capped/frozen tickers. Reuses the same
    # already-loaded daily history, no extra fetch.
    derive.reconcile_price_volatility(data, _load_json_or_empty(DAILY_3MO_HISTORY_FILE))
    # Daily-move gate input (see derive.reconcile_daily_move) -- same file.
    derive.reconcile_daily_move(data, _load_json_or_empty(DAILY_3MO_HISTORY_FILE))
    _xbrl = _load_json_or_empty(XBRL_FACTS_FILE)
    _raw_stmts = _load_json_or_empty(RAW_STATEMENTS_FILE)
    # Recomputes trailingEps/trailingPE from the 4 most recent quarters in
    # quarterlyIncomeStmt directly, rather than trusting yfinance's own
    # `.info` summary fields -- explicit instruction: those can lag their
    # OWN quarterlyIncomeStmt endpoint by a full quarter (confirmed live
    # on FEIM), so REPORTED earnings should be reflected as soon as this
    # (zero-network) reconciliation next runs after a fresh download,
    # not whenever yfinance's own summary cache happens to catch up. Runs
    # BEFORE reconcile_eps_volatility/reconcile_forward_eps below since
    # both are independent of it, but keeping the "most foundational
    # figure first" ordering this function already uses elsewhere.
    derive.reconcile_trailing_eps(data, _raw_stmts, _xbrl)
    derive.reconcile_revenue_growth(data, _xbrl)
    derive.reconcile_earnings_growth(data, _xbrl)
    derive.reconcile_eps_volatility(
        data, _xbrl, _raw_stmts, _load_json_or_empty(EPS_ESTIMATES_FILE)
    )
    derive.reconcile_forward_eps(
        data, _load_json_or_empty(FORWARD_EPS_FILE), _load_json_or_empty(REVENUE_ESTIMATES_FILE), _xbrl
    )
    # Needs forwardEps (just reconciled above) and earningsGrowth (reconciled
    # earlier by reconcile_earnings_growth) -- must run after both.
    derive.reconcile_peg_ratio(data)
    add_target_upside(data)
    add_avg_liquidity_ratio(data)
    write_full_csv(data)
    download_simulations()
    write_sorted_screen_csv(data)
    download_target_portfolio()
    download_backtest()
    print(f"recalc: wrote {SORTED_SCREEN_CSV} (and downstream).")


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
    sentiment_scores = load_sentiment_scores(SENTIMENT_FILE, NEWS_SENTIMENT_FILE)
    institutional_scores = load_institutional_scores(THIRTEENF_FILE)
    insider_scores = load_insider_scores(FORM4_FILE)
    short_interest_scores = load_short_interest_scores(SHORT_INTEREST_FILE, RAW_DATA_FILE, SHORT_VOLUME_FILE)
    fair_value_scores = load_fair_value_scores(FAIR_VALUE_FILE)
    consensus_scores = analyst_consensus_score(GRADES_FILE)
    guidance_scores = load_guidance_scores(GUIDANCE_SIGNAL_FILE)

    # Inject the two simulations.json return estimates into each row dict so
    # forecast_return_rank can read them directly -- that factor is an
    # equal-weight blend of a rank on each (see its docstring):
    #   forecastReturn -- simulate_ticker's confidence-weighted fair value
    #     vs. currentPrice (the deterministic, shrunk-toward-price point
    #     estimate behind forecastPrice).
    #   simReturn -- the Monte Carlo simulated-path price distribution's
    #     MEAN vs. currentPrice (raw, not confidence-shrunk).
    # simSharpe rides along for reference but is no longer scored here.
    #   probAboveCurrentPrice -- share of Monte Carlo paths ending above
    #     today's price, from simPriceDistribution; scored separately by
    #     sim_prob_above_rank alongside forecast_return_rank.
    # Tickers not present in the file (not yet simulated, simulated with an
    # error, or with no industry-multiple scenario to derive a forecast
    # from) are simply left without the fields -- forecast_return_rank
    # ranks them worst, same treatment as every other factor's missing data.
    try:
        with open(SIMULATIONS_FILE) as _mcf:
            _mc_data = {}
            for entry in json.load(_mcf):
                if "ticker" not in entry or entry.get("error"):
                    continue
                _fret, _sret = entry.get("forecastReturn"), entry.get("simReturn")
                _dist = entry.get("simPriceDistribution") or {}
                if _fret is not None or _sret is not None:
                    _mc_data[entry["ticker"]] = {
                        "forecastReturn": _fret,
                        "simReturn": _sret,
                        "simSharpe": entry.get("simSharpe"),
                        "probAboveCurrentPrice": _dist.get("probAboveCurrentPrice"),
                    }
    except (FileNotFoundError, json.JSONDecodeError):
        _mc_data = {}
    rows = [(s, {**d, **_mc_data[s]} if s in _mc_data else d) for s, d in rows]

    scored = sorted(
        score_rows(rows, sentiment_scores, insider_scores, short_interest_scores, fair_value_scores, consensus_scores, guidance_scores, institutional_scores),
        key=lambda item: item[2],
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

    def _with_short_interest(symbol, d):
        """d plus the four short-interest fields and analystConsensus (see
        SCREEN_ONLY_FIELDNAMES) pulled from the same short_interest_scores/
        consensus_scores maps score_rows was just handed -- so the CSV
        column and the composite score can never disagree. Three of the
        short-interest fields are FINRA's own biweekly settlement figures;
        shortVolumeRatio is Eulerpool's daily short-volume-tape average, a
        fourth and independent leg (see scoring.load_short_interest_scores/
        short_interest_rank). analystConsensus is Eulerpool's own
        [-1, 1] aggregate sell-side stance (see
        scoring.analyst_consensus_score) -- where coverage stands RIGHT
        NOW, not the same thing as targetUpside's price-target math."""
        si = short_interest_scores.get(symbol) or {}
        return {
            **d,
            "shortPctOfFloatFinra": si.get("pctOfFloat"),
            "shortDaysToCover": si.get("daysToCover"),
            "shortChangePercent": si.get("changePercent"),
            "shortVolumeRatio": si.get("shortVolumeRatio"),
            "analystConsensus": consensus_scores.get(symbol),
        }

    fieldnames = SCREEN_FIELDNAMES + ["score", "rating"]
    with open(SORTED_SCREEN_CSV, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(fieldnames)
        for i, (symbol, d, score) in enumerate(scored):
            rating = rating_for_percentile(i / n) if n else ""
            row = _with_short_interest(symbol, d)
            writer.writerow([symbol] + [row.get(field, "") for field in SCREEN_FIELDNAMES[1:]] + [score, rating])
        for symbol, d in unranked:
            row = _with_short_interest(symbol, d)
            writer.writerow([symbol] + [row.get(field, "") for field in SCREEN_FIELDNAMES[1:]] + ["", RATING_NA])
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
    write_recommendations(
        SORTED_SCREEN_CSV, NEWS_FILE, FORM4_FILE, THIRTEENF_FILE, SHORT_INTEREST_FILE, RAW_DATA_FILE, RATED_FOR_EXTRAS,
        simulations_file=SIMULATIONS_FILE,
    )


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
    their previous screen_data.csv row is reused as-is, rather than
    re-fetched."""
    tickers = load_tickers(SYMBOLS_FILE)
    print(f"Loaded {len(tickers)} active tickers from {SYMBOLS_FILE}")

    ib_bar_thread = threading.Thread(target=_run_ib_bar_refresh_in_background, args=(tickers, overwrite), daemon=True)
    ib_bar_thread.start()

    # 1. fetch raw provider data (yfinance .info + statements)
    download()

    # Social sentiment, scoped to the ranking as it stood before this run
    # (sorted_screen.csv isn't rewritten with fresh scores until recalc).
    rated_tickers = load_rated_tickers(SORTED_SCREEN_CSV, RATED_FOR_EXTRAS)
    if rated_tickers:
        fetch_social_sentiment(rated_tickers)
    else:
        print(f"No existing {SORTED_SCREEN_CSV} yet; skipping social sentiment download")

    print("Waiting for background IB daily/hourly bar refresh to finish before scoring momentum...")
    ib_bar_thread.join()

    # 2. rebuild everything derived from the raw dumps -- `all overwrite`
    # also forces the full (non-incremental) yfinance daily-close re-pull.
    recalc(fresh_momentum=True, force_prices=overwrite)
    snapshot_screen_history()
    # recalc() already built the backtest once, but BEFORE this run's own
    # sorted_screen <Friday>.csv snapshot existed -- re-run it here, at the
    # very end, so backtest.json reflects the snapshot just written (and the
    # freshest IB daily bars from the join above). Zero network, ~1s.
    download_backtest()


def snapshot_screen_history():
    """Copies the just-written sorted_screen.csv (which the Backtesting tab
    scores forward -- see modules/backtest.py) and recommendations.json
    into HISTORY_DIR, both dated with the current week's FRIDAY.

    Fri/Sat/Sun snapshot under the Friday that just occurred; Mon-Thu
    ("after Sunday") under the coming Friday -- so every run within one
    trading week lands on the same filenames and just overwrites them, and
    new files only appear once the week rolls over."""
    today = date.today()
    wd = today.weekday()  # Mon=0 .. Fri=4, Sat=5, Sun=6
    friday = today - timedelta(days=wd - 4) if wd >= 4 else today + timedelta(days=4 - wd)
    os.makedirs(HISTORY_DIR, exist_ok=True)
    for src in (SORTED_SCREEN_CSV, RECOMMENDATIONS_FILE):
        stem, ext = os.path.splitext(os.path.basename(src))
        dest = os.path.join(HISTORY_DIR, f"{stem} {friday:%Y%m%d}{ext}")
        shutil.copyfile(src, dest)
        print(f"Snapshotted {src} -> {dest}")


def download_prices(force=False):
    """`recalc(fresh_momentum=True)` -- rebuild everything from the raw
    dumps on disk, with a fresh yfinance trailing-~1mo daily-close pull for
    the momentum factor (the one thing plain `recalc` doesn't refetch).
    That pull is INCREMENTAL -- only tickers whose cached daily close isn't
    already current get re-fetched; `python main.py prices overwrite`
    forces the full universe. Does NOT re-pull `.info` / statements -- run
    `python main.py download` (or `all`) for that. IB Gateway's own daily
    bars stay a separate on-demand step (`python main.py ibprices`), since
    IB Gateway won't accept a second API connection while ib_server.py
    holds one open."""
    recalc(fresh_momentum=True, force_prices=force)


def download_short_interest():
    """Fetches FINRA's latest biweekly equity short interest settlement
    file (see finra.fetch_short_interest) for the ENTIRE scored universe
    (explicit instruction, same widened scope as download_form4/
    download_xbrl/download_13f/download_eulerpool), even though FINRA's
    own file is a single bulk download covering the whole market
    regardless of how many tickers get filtered out of it -- widening
    this just keeps more of SHORT_INTEREST_FILE's local copy populated,
    not a change in what gets fetched over the wire. A separate download,
    run on its own via `python main.py shortinterest` rather than folded
    into download_all -- it hits a different, independently-rate-limited
    host (FINRA's CDN, not Yahoo Finance), same reasoning as every other
    standalone fetch in this file."""
    tickers = load_all_tickers(SORTED_SCREEN_CSV)
    if not tickers:
        print(f"No existing {SORTED_SCREEN_CSV} yet; run `python main.py all` first")
        return
    fetch_short_interest(tickers)


def download_form4():
    """Fetches SEC EDGAR Form 4 insider-transaction filings (see
    sec_edgar.fetch_form4) for the ENTIRE scored universe (every ticker in
    sorted_screen.csv, not just RATED_FOR_EXTRAS -- explicit instruction).
    A separate download, run on its own via `python main.py form4` rather
    than folded into download_all -- it hits a different rate-limited
    external service (SEC EDGAR, not Yahoo Finance) on its own schedule,
    same reasoning as social_sentiment.py being a standalone fetch."""
    tickers = load_all_tickers(SORTED_SCREEN_CSV)
    if not tickers:
        print(f"No existing {SORTED_SCREEN_CSV} yet; run `python main.py all` first")
        return
    fetch_form4(tickers)


def download_xbrl():
    """Fetches SEC EDGAR XBRL company facts (see sec_edgar.fetch_xbrl_facts)
    -- multi-year revenue/income/assets/equity/EPS history -- for the
    ENTIRE scored universe, same scoping and same standalone-download
    reasoning as download_form4 above. Run via `python main.py xbrl`."""
    tickers = load_all_tickers(SORTED_SCREEN_CSV)
    if not tickers:
        print(f"No existing {SORTED_SCREEN_CSV} yet; run `python main.py all` first")
        return
    fetch_xbrl_facts(tickers)


def download_13f():
    """Fetches SEC's latest quarterly bulk 13F institutional-holdings
    dataset (see sec_edgar.fetch_13f_holdings) for the ENTIRE scored
    universe, matched by company name rather than CIK -- 13F is filed BY
    institutional managers ABOUT what they hold, not by the issuer, so
    there's no per-ticker CIK to query the way Form 4/XBRL have; see that
    function's own docstring. A single ~90MB bulk download covering every
    filer at once, not one request per ticker, so widening this from
    RATED_FOR_EXTRAS to the full universe costs nothing extra in fetch
    time -- it's just matching more names client-side against data
    already downloaded. Run via `python main.py 13f`."""
    try:
        with open(SORTED_SCREEN_CSV, newline="") as f:
            ticker_names = {row["ticker"]: row["name"] for row in csv.DictReader(f) if row.get("name")}
    except FileNotFoundError:
        ticker_names = {}
    if not ticker_names:
        print(f"No existing {SORTED_SCREEN_CSV} yet; run `python main.py all` first")
        return
    fetch_13f_holdings(ticker_names)


EULERPOOL_ALL_MAX_AGE_DAYS = 1


def download_eulerpool():
    """Fetches all five of Eulerpool's own per-ticker datasets for the
    ENTIRE scored universe (explicit instruction: same widened scope as
    download_form4/download_xbrl/download_13f, not just RATED_FOR_EXTRAS):

      1. Analyst upgrade/downgrade history (modules.eulerpool.
         fetch_analyst_grades) -- the FULL grade history, "maintain"
         included, feeding modules.scoring's load_analyst_grade_scores/
         analyst_consensus_score.
      2. Fair-value snapshot (modules.eulerpool.fetch_fair_values) --
         feeding modules.scoring.load_fair_value_scores (fair_value_rank,
         part of the composite score in every column).
      3. Forward-EPS/revenue consensus (modules.eulerpool.fetch_forward_eps)
         -- feeding modules.derive.reconcile_forward_eps, which blends the
         EPS pair 50/50 with yfinance's own fwdEps0y/fwdEps1y into "our
         own" forward EPS used everywhere (including forwardEps, the
         figure modules.simulations actually anchors its EPS path on),
         and derives eulerRevGrowth1y (a new forward revenue-growth signal
         with no yfinance equivalent) feeding both exp_revenue_growth_rank
         and modules.simulations' own ownGrowthRate. Also derives
         eulerRevGrowth0y (Last-FY-actual -> This-FY, a SEPARATE
         ownGrowthRate/industryGrowthRate leg from eulerRevGrowth1y) using
         step 5's own past-revenue-actuals output below.
      4. Daily short-volume ratio (modules.eulerpool.fetch_short_volume)
         -- a trailing 10-trading-day average of FINRA's own daily short-
         sale volume tape, feeding modules.scoring.load_short_interest_
         scores as a fourth leg of short_interest_rank (see that
         function's own docstring) -- genuinely new, higher-frequency data
         this project never ingested before (distinct from FINRA's
         biweekly short-INTEREST settlement file modules.finra already
         pulls directly).
      5. Historical EPS-estimate series (modules.eulerpool.
         fetch_eps_estimates) -- feeding modules.derive.eps_volatility as
         a third historical-EPS source alongside SEC company_facts and
         yfinance's own income_stmt (see reconcile_eps_volatility and
         fetch_eps_estimates' own docstring for why: spot-checked against
         SEC GAAP diluted EPS for MSFT/AMZN, it reads closer to a "Street"/
         adjusted EPS, excluding at least some one-off items that inflate
         GAAP-based volatility without reflecting genuine earnings
         unpredictability). The SAME get_estimates call also writes
         REVENUE_ESTIMATES_FILE (past-actual revenue, same shape) -- step
         3's eulerRevGrowth0y above is the only consumer of that one.

    Explicit instruction: ALL FIVE now share ONE blanket 1-day cooldown
    (EULERPOOL_ALL_MAX_AGE_DAYS) at this function's own entry point,
    keyed off GRADES_FILE's mtime (the one file every prior run always
    touches) -- if Eulerpool data of ANY kind was downloaded within the
    last day, the ENTIRE step is skipped, not just re-checked
    per-dataset the way step 1 alone used to self-throttle (steps 2-4 used
    to always refetch regardless of age; that's gone now). This is what
    lets `python main.py all` include this step on every run (a new
    behavior -- previously eulerpool downloads were `python main.py
    eulerpool`-only, standalone, own schedule) without hitting Eulerpool's
    API on every single `all` run.

    Pass `overwrite` as the first CLI arg (or `all overwrite`) to bypass
    the cooldown and force every step's refetch regardless of age. Run
    via `python main.py eulerpool` (`eulerpool overwrite` to force), or
    automatically as part of `python main.py all`."""
    tickers = load_all_tickers(SORTED_SCREEN_CSV)
    if not tickers:
        print(f"No existing {SORTED_SCREEN_CSV} yet; run `python main.py all` first")
        return
    force = len(sys.argv) > 2 and sys.argv[2] == "overwrite"
    if not force and os.path.exists(GRADES_FILE):
        age_days = (time.time() - os.path.getmtime(GRADES_FILE)) / 86400
        if age_days < EULERPOOL_ALL_MAX_AGE_DAYS:
            print(f"Eulerpool data already refreshed {age_days:.1f}d ago "
                  f"(< {EULERPOOL_ALL_MAX_AGE_DAYS}d) -- skipping all Eulerpool "
                  f"downloads. Pass 'overwrite' to force.")
            return
    fetch_analyst_grades(tickers, out_file=GRADES_FILE, force=force)
    fetch_fair_values(tickers, out_file=FAIR_VALUE_FILE)
    fetch_forward_eps(tickers, out_file=FORWARD_EPS_FILE)
    fetch_short_volume(tickers, out_file=SHORT_VOLUME_FILE)
    fetch_eps_estimates(tickers, out_file=EPS_ESTIMATES_FILE, revenue_out_file=REVENUE_ESTIMATES_FILE)


# Deliberately its OWN standalone step, NOT folded into download_eulerpool's
# blanket 5-dataset fetch above -- explicit prior lesson (see
# feedback_no_unprompted_long_running_fetches.md's "Repeat violation #2"):
# a narrowly-scoped fetch should be its own call, not reached via a
# blanket step that would also re-touch the other four already-fresh
# Eulerpool datasets. Scoped to Strong Buy/Strong Sell only -- explicit
# instruction, not RATED_FOR_EXTRAS' wider Buy/Sell-included set -- since
# this is meant to feed a from-scratch look at whether transcripts are
# even worth building a signal from, not a full-universe ingestion yet.
def download_transcripts():
    """Fetches the latest earnings-call transcript (modules.eulerpool.
    fetch_transcripts) for every currently Strong Buy/Strong Sell ticker
    in sorted_screen.csv. Run via `python main.py transcripts` any time
    after the ranking has been refreshed (`all`/`recalc`)."""
    tickers = load_rated_tickers(SORTED_SCREEN_CSV, {"Strong Buy", "Strong Sell"})
    if not tickers:
        print(f"No existing {SORTED_SCREEN_CSV} yet, or no Strong Buy/Strong Sell tickers currently -- run `python main.py all` first")
        return
    fetch_transcripts(tickers, out_file=TRANSCRIPTS_FILE)


def download_guidance():
    """Fetches 8-K earnings-release guidance snippets (modules.sec_edgar.
    fetch_earnings_guidance) for every currently Strong Buy/Strong Sell
    ticker in sorted_screen.csv -- same scope as download_transcripts,
    same "its own narrow step, not folded into a blanket one" reasoning.
    Run via `python main.py guidance` any time after the ranking has been
    refreshed (`all`/`recalc`).

    Also re-runs summarize_guidance() to refresh GUIDANCE_SIGNAL_FILE from
    whatever snippets are now on file -- this used to be a one-off manual
    step when the guidance factor was first built, which would have left
    GUIDANCE_SIGNAL_FILE silently stale on the next real fetch (the same
    shape of staleness bug found and fixed elsewhere this session)."""
    tickers = load_rated_tickers(SORTED_SCREEN_CSV, {"Strong Buy", "Strong Sell"})
    if not tickers:
        print(f"No existing {SORTED_SCREEN_CSV} yet, or no Strong Buy/Strong Sell tickers currently -- run `python main.py all` first")
        return
    fetch_earnings_guidance(tickers, out_file=GUIDANCE_FILE)
    summarize_guidance()


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
    write_recommendations(
        SORTED_SCREEN_CSV, NEWS_FILE, FORM4_FILE, THIRTEENF_FILE, SHORT_INTEREST_FILE, RAW_DATA_FILE, RATED_FOR_EXTRAS,
        simulations_file=SIMULATIONS_FILE,
    )


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
    """Refetch raw yfinance data for specific tickers only (e.g. ones that
    hit a transient error during a full run and are missing from every
    output), then a full recalc. `download()` already merges into
    RAW_DATA_FILE / RAW_STATEMENTS_FILE without a FRESH_HOURS skip for an
    explicit ticker list, and recalc() rebuilds screen_data.csv +
    everything downstream from all the raw dumps. Run via
    `python main.py symbol TICKER [TICKER ...]`."""
    download(symbols)
    recalc(fresh_momentum=True)


def download_simulations(tickers=None):
    """EPS-driven Monte Carlo price simulation prototype (see
    modules/simulations.py's own docstring for the full formula) -- zero
    network calls, reads screen_data.csv only, same as rescore(). Explicit
    instruction: defaults to the FULL active universe currently in
    screen_data.csv (same scope the Screener itself covers -- what feeds
    the Simulations tab) when no tickers are given at all, same as
    `simulations --all`; given specific tickers, runs just those instead
    (e.g. for a quick one-off check). Writes SIMULATIONS_FILE. Every
    single simulated ticker is logged to the terminal as it completes (via
    run_eps_simulations_iter, not the whole-list-at-once run()), so a
    full-universe run's progress is visible the entire time rather than
    going silent until everything finishes."""
    data = load_pe_data(OUTPUT_CSV)
    # Eulerpool's per-firm rating consensus (see modules.scoring.
    # analyst_consensus_score), merged in here rather than read from
    # OUTPUT_CSV directly -- it isn't a screen_data.csv column, it's
    # computed on demand from GRADES_FILE the same way score_rows'
    # own consensus_scores is. Feeds simulate_ticker's
    # TARGET_BLEND_WEIGHT_PER_CONSENSUS (see that constant's own
    # docstring) as "analystConsensus" on each row; a ticker Eulerpool
    # has no coverage for just doesn't get the key, same missing-data
    # fallback simulate_ticker already uses for targetMeanPrice/
    # numberOfAnalystOpinions.
    for t, consensus in analyst_consensus_score(GRADES_FILE).items():
        if t in data:
            data[t]["analystConsensus"] = consensus
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
    simulations.json and writes TARGET_PORTFOLIO_FILE (the full universe)
    plus TARGET_PORTFOLIO_EX_FILE (a variant with the sector groups in
    TARGET_PORTFOLIO_EX_GROUPS -- Financial Services, Healthcare, Real
    Estate -- excluded; TargetView switches between the two). Zero network
    calls -- purely computes from files already on disk. Run via `python
    main.py target`, or called automatically at the end of `all`,
    `prices`, and `rescore` pipelines so TargetView always reflects the
    latest screener and simulation state."""
    variants = [
        (TARGET_PORTFOLIO_FILE, None),
        (TARGET_PORTFOLIO_EX_FILE, TARGET_PORTFOLIO_EX_GROUPS),
    ]
    for path, exclude in variants:
        try:
            result = build_target_portfolio(RECOMMENDATIONS_FILE, SIMULATIONS_FILE, exclude_groups=exclude)
        except Exception as exc:
            print(f"target portfolio optimiser failed ({os.path.basename(path)}): {exc}")
            continue
        with open(path, "w") as f:
            json.dump(result, f, indent=2)
        n_long = len(result.get("longs", []))
        n_short = len(result.get("shorts", []))
        sharpe = result.get("stats", {}).get("sharpe")
        print(f"Wrote {path}: {n_long}L + {n_short}S"
              + (f", portfolio Sharpe {sharpe:.2f}" if sharpe is not None else ""))


def download_backtest():
    """Scores every dated screen snapshot in HISTORY_DIR (sorted_screen
    <YYYYMMDD>.csv) forward 5 trading days (modules.backtest.
    HOLDING_TRADING_DAYS) against IB's daily bars and writes
    BACKTEST_FILE -- per rating bucket, equal-weight: weekly return,
    weekly volatility, Sharpe (see modules/backtest.py). Zero network
    calls; purely computes from files already on disk. Run via `python
    main.py backtest`, and also chained onto `rescore` so a fresh daily-
    bar pull is reflected. New weeks appear by dropping another
    sorted_screen <date>.csv into HISTORY_DIR -- nothing here to change."""
    try:
        result = build_backtest(HISTORY_DIR, DAILY_3MO_HISTORY_FILE, HOURLY_HISTORY_FILE)
    except Exception as exc:
        print(f"backtest failed: {exc}")
        return
    with open(BACKTEST_FILE, "w") as f:
        json.dump(result, f, indent=2)
    weeks = result.get("weeks", [])
    print(f"Wrote {BACKTEST_FILE}: {len(weeks)} week(s) "
          + ", ".join(f"{w['week']} ({sum(g['count'] for g in w['groups'].values())} candidates)" for w in weeks))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "prices"
    if mode == "all":
        # `all overwrite` bypasses IB_REFRESH_STATE_FILE's 3h cooldown --
        # see download_all's own overwrite param. The only command that can.
        download_all(overwrite=(len(sys.argv) > 2 and sys.argv[2] == "overwrite"))
        # Eulerpool downloads (explicit instruction) -- own blanket 1-day
        # cooldown inside download_eulerpool itself (EULERPOOL_ALL_MAX_AGE_
        # DAYS), so this is safe to call on every `all` run: it no-ops
        # immediately unless Eulerpool data is actually stale. `all
        # overwrite` forces it through the same way it forces the IB
        # refresh through, above.
        download_eulerpool()
        # Account performance Flex Query (explicit instruction): same as
        # `python ib_server.py performance` -- IBKR's Flex web service, not IB
        # Gateway -- which also saves the raw statement as Results.xml.
        # Run as a subprocess because ib_server.py imports from this module
        # (a direct import would be circular). Last, and non-fatal: an IBKR
        # "statement could not be generated" (error 1001) is common and
        # shouldn't fail the whole `all` run.
        import subprocess
        _repo = os.path.dirname(os.path.abspath(__file__))
        print("Fetching the account performance Flex Query (Results.xml / portfolio_performance.json)...")
        _rc = subprocess.run([sys.executable, os.path.join(_repo, "ib_server.py"), "performance"], cwd=_repo).returncode
        if _rc != 0:
            print(f"Account performance Flex Query did not complete (exit {_rc}) -- portfolio_performance.json left as it was")
    elif mode == "download":
        download(sys.argv[2:] if len(sys.argv) > 2 else None)
    elif mode in ("recalc", "rescore"):
        recalc()
    elif mode == "prices":
        download_prices(force=(len(sys.argv) > 2 and sys.argv[2] == "overwrite"))
    elif mode == "form4":
        download_form4()
    elif mode == "xbrl":
        download_xbrl()
    elif mode == "13f":
        download_13f()
    elif mode == "eulerpool":
        download_eulerpool()
    elif mode == "transcripts":
        download_transcripts()
    elif mode == "guidance":
        download_guidance()
    elif mode == "shortinterest":
        download_short_interest()
    elif mode == "ibprices":
        download_ib_prices()
    elif mode == "ibhprices":
        download_ib_hourly_prices()
    elif mode == "yfprices":
        download_yfinance_prices(force=(len(sys.argv) > 2 and sys.argv[2] == "overwrite"))
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
    elif mode == "backtest":
        download_backtest()
    else:
        sys.exit(
            f"Unknown mode {mode!r}, expected 'all', 'download', 'recalc' ('rescore'), 'prices', 'form4', "
            "'xbrl', '13f', 'eulerpool', 'shortinterest', 'ibprices', 'ibhprices', 'yfprices', 'themes', "
            "'recommendations', 'chat', 'symbol', 'simulations', 'target', or 'backtest'"
        )
