"""
ib_server.py — tiny local HTTP server pushing IB Gateway last-price
and position updates to App.jsx over Server-Sent Events while the table is
open.

Talks to IB Gateway via ib_insync's socket API (the same connection
IBApp.connect() already uses), NOT the Client Portal Gateway's REST API —
that's a separate piece of IBKR software this project doesn't have running.
What's actually listening on IBApp.IBKR_BASE_URL's port (4001) is the
classic IB Gateway's TWS-API socket, a raw binary protocol that only
ib_insync speaks; hitting it with HTTPS/REST just gets you a TLS handshake
reset, no matter the client.

Both price ticks and position changes arrive as IB Gateway pushes them
(reqMktData/pendingTickersEvent, reqPositions/positionEvent) — nothing here
polls IB Gateway on a timer. /api/stream then re-pushes the current
snapshot to every connected browser the moment either changes, over one
long-lived SSE connection, instead of the browser polling on its own timer.

Run: python ib_server.py [port] [no_news]   (port default 8765)
Requires IB Gateway running and reachable at 127.0.0.1:4001, same
precondition as IBApp.connect(). Note reqMarketDataType(3) in
IBApp.connect() means delayed (15-20 min) price data unless your account
has a live data subscription and that's changed to type 1.

`no_news` (in either arg position, e.g. `python ib_server.py no_news`
or `python ib_server.py 8765 no_news`) skips *re-downloading* news
headlines from IB this run -- everything else (ticks, positions,
snapshots, trades, candlestick history) is unaffected, and news.json /
news_sentiment.json still get (re)written once at startup from whatever
was already cached on disk (see run_ib_client), so PeTable.jsx's News
Sentiment column keeps working off the last real download instead of
going empty. Useful for a quick restart when you just want prices back
and don't want to wait through FinBERT scoring a fresh backlog of
headlines, or don't want another IB news-headline pacing budget spent
this session.

Endpoints:
  GET /api/stream       -> text/event-stream; each `data:` line is
                           {"prices": {ticker: {last, bid, ask, timestamp}},
                            "positions": {ticker: {shares, avgCost}},
                            "account": {tag: value},
                            "trades": {ticker: {qty, value, realizedPnl,
                            commission}},
                            "pnl": {ticker: {dailyPnL, unrealizedPnL,
                            realizedPnL, position, value}},
                            "openOrders": [{ticker, action, orderType,
                            quantity, limitPrice, auxPrice, status,
                            filled, remaining}, ...]} — the full
                           current snapshot, sent on connect and again
                           whenever any of the six changes. `openOrders`
                           is every currently-working order (see
                           refresh_open_orders / IBApp.
                           get_open_orders_async) -- a LIST, not keyed by
                           ticker like the others, since one ticker can
                           have more than one working order at once.
                           Polled (IB Gateway has no "order changed" push
                           this file subscribes to) every
                           OPEN_ORDERS_REFRESH_SECONDS (30s -- much
                           tighter than `trades`' own 5-minute poll,
                           since a working order can fill or get
                           rejected at any moment, unlike a past fill).
                           `pnl` is
                           IBKR's own reqPnLSingle figures (see IBApp.
                           subscribe_position_pnl and this file's own
                           on_position/on_pnl_single) for every symbol
                           that's been held at any point since this
                           process started -- authoritative, IB-computed
                           per-position P&L, not the price-reconstruction
                           PositionsView.jsx used to compute client-side.
                           Deliberately never removed once a symbol
                           appears here, even after its position goes to
                           0 (fully closed out) -- IBKR keeps reporting
                           dailyPnL/realizedPnL for the rest of the
                           session, which is what lets PositionsView.jsx
                           show a same-day-closed position's P&L instead
                           of it just vanishing the moment shares hit 0
                           (a ticker present in `pnl` with position 0 is
                           exactly that case). Positions cover the whole account, not
                           just the top-ranked tickers: stream_prices_and_positions
                           asks for positions right after connecting and streams all
                           of them, uncapped, filling whatever's left of the
                           MAX_STREAMED_SYMBOLS budget with the
                           highest-ranked RATED_FOR_EXTRAS (Strong Buy/Buy/
                           Sell/Strong Sell) screener tickers; on_position
                           catches anything opened later the same way.
                           `prices` ends up covering every held stock plus
                           as many ranked, actionably-rated tickers as fit.
                           Stocks only — an
                           option and its underlying share a ticker symbol,
                           which this doesn't disambiguate. `trades` is
                           today's fills only (qty = signed net shares
                           traded today, value = sum(signedQty *
                           fillPrice), realizedPnl/commission = IB's own
                           FIFO-cost-basis figures, None rather than 0 if
                           this connection wasn't alive to see a fill live
                           -- see refresh_trades / IBApp.
                           get_today_executions_async's own docstring for
                           why realizedPnl needs a live-tracked fill) — and
                           only has an entry for a symbol actually traded
                           today. PositionsView.jsx separately derives
                           today's realized P&L client-side from `trades` +
                           `positions` + price history for any symbol
                           traded today but no longer held (see its own
                           comments) — mark-to-market vs. yesterday's
                           close, same convention every other daily figure
                           on that page uses, not
                           IB's FIFO-cost-basis definition.
  GET /api/last-prices  -> the current {ticker: {last, bid, ask, timestamp}} snapshot
                           as a one-shot JSON response. Kept for manual/curl
                           debugging.
  GET /api/positions    -> the current {ticker: {shares, avgCost}} snapshot
                           as a one-shot JSON response. Kept for
                           manual/curl debugging.
  GET /api/account      -> the current {tag: value} account-summary
                           snapshot (see IBApp.ACCOUNT_STATUS_TAGS) as a
                           one-shot JSON response. Kept for manual/curl
                           debugging.
  GET /api/trades       -> the current {ticker: {qty, value}} snapshot (see
                           `trades` above) as a one-shot JSON response.
                           Kept for manual/curl debugging.
  GET /api/open-orders  -> the current openOrders list (see above) as a
                           one-shot JSON response. Kept for manual/curl
                           debugging.
  GET /api/news         -> the current {ticker: [{articleId, time,
                           provider, headline, sentiment}, ...]} snapshot
                           (news_by_ticker, newest first per ticker) as a
                           one-shot JSON response. `sentiment` is FinBERT's
                           1 (very bearish) - 5 (very bullish) score (see
                           news_sentiment.py); same rolling NEWS_WINDOW_DAYS
                           window as news.json.

Also writes (not served — Asset.jsx/PeTable.jsx fetch these as static
files, same as main.py's data/price_history.json; all JSON downloader
output lives under data/, see main.py's DATA_DIR):
  data/price_history_hourly.json    {ticker: [{date, open, high, low, close,
                                volume}]} 1 month of hourly bars, IB
                                Gateway's own historical data
                                (reqHistoricalData), for candlestick +
                                volume charts.
  data/price_history_daily_3mo.json Same shape, 3 months of daily bars.
  data/news_sentiment.json          {ticker: {articleId: score}} -- every
                                scored article's raw 1-5 FinBERT score
                                (see news_loop / news_sentiment.py), for
                                PeTable.jsx's screener column to average
                                per ticker itself, same rolling
                                NEWS_WINDOW_DAYS window as news.json.
Both cover the top CANDLESTICK_TOP_N ranked tickers, every RATED_FOR_EXTRAS
ticker (Strong Buy/Buy/Sell/Strong Sell -- CANDLESTICK_TOP_N alone only
ever reaches the best-scoring/Buy end, since it's a top-N slice of a file
sorted ascending by score), and every ticker this process actually streams
a live price for (so a held position outside both of those, like an ETF
not even in sorted_screen.csv, still gets covered) — fetched once at
startup (see fetch_candlestick_history).
reqHistoricalData's pacing limit (IBApp.get_ib_historical_bars_async
defaults to 200 requests/10min, confirmed live against this account well
above IB's ~60/10min textbook figure) means this can still take a while
for hundreds of tickers x 2 series; it runs as a
background asyncio task on the same single IB Gateway connection as
everything else in this file (get_ib_historical_bars_async awaits each
request and sleeps via asyncio.sleep for pacing, instead of blocking),
so it never stalls live price/position streaming.

Any screener ticker (all of sorted_screen.csv) outside MAX_STREAMED_SYMBOLS
(so no live reqMktData subscription) still gets a `prices` entry — just
refreshed every SNAPSHOT_INTERVAL_SECONDS instead of live-ticking, via a
one-shot IB snapshot request (see snapshot_loop), also a background task
on that same single connection. A snapshot resolves once and releases its
market-data line immediately, so this costs nothing against
MAX_STREAMED_SYMBOLS regardless of how many tickers it covers.

Everything IB-facing in this file — live ticks/positions, candlestick
history, and snapshot polling — shares exactly one IB Gateway connection
(clientId 0, run_ib_client) as concurrent asyncio tasks on one event loop,
running in its own background thread; the HTTP server below runs on the
main thread and never touches IB Gateway, so it doesn't add a second
client. IB Gateway/TWS shows one connected API client for this process,
not three.

The streaming server also refreshes PORTFOLIO_PERFORMANCE_FILE on its own —
see performance_loop, one of the background asyncio tasks run_ib_client
starts (same pattern as snapshot_loop/trades_loop): once immediately on
startup, then every PERFORMANCE_REFRESH_SECONDS for as long as the process
stays up. `python ib_server.py performance [YYYY-MM-DD]` (below) is
the standalone one-shot version of the exact same fetch, for a manual
refresh or a specific start date without waiting on the server's own
schedule or running the streaming server at all.

One-shot mode: `python ib_server.py performance [YYYY-MM-DD]`
fetches real day-by-day account NAV history via an IBKR Flex Query — not
the streaming server, doesn't touch IB Gateway on port 4001 at all — then
writes PORTFOLIO_PERFORMANCE_FILE and exits. See fetch_account_performance
for why a Flex Query, not the TWS-API socket, is what this needs.
"""

import asyncio
import csv
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from dateutil.parser import isoparse

from IBApp import IBApp
from main import (
    OUTPUT_CSV,
    RATED_FOR_EXTRAS,
    RAW_DATA_FILE,
    SORTED_SCREEN_CSV,
    SYMBOLS_FILE,
    load_rated_tickers,
    load_top_tickers,
)
from finra import SHORT_INTEREST_FILE
from main import PRICE_HISTORY_FILE as YAHOO_PRICE_HISTORY_FILE
from news_sentiment import clean_headline, score_headlines
from scoring import FACTOR_WEIGHTS, most_recent_completed_trading_day
from sec_edgar import FORM4_FILE, THIRTEENF_FILE, THIRTEENF_HOLDERS_FILE, XBRL_FACTS_FILE
from social_sentiment import SENTIMENT_FILE
from theme_classifier import TAXONOMY_FILE, TICKER_THEMES_FILE

MAX_STREAMED_SYMBOLS = 99  # this account's real ceiling — 100 hits IB error 101, "Max number of tickers has been reached"
# Every JSON file this process writes lives here -- see main.py's DATA_DIR
# for why (and why it's the JSON outputs specifically, not
# sorted_screen.csv, which stays at the root). Duplicated rather than
# imported from main.py's own DATA_DIR since main.py already creates the
# directory on import and this module already imports from main.py
# (SORTED_SCREEN_CSV, load_top_tickers) -- no need for a second os.makedirs.
DATA_DIR = "data"
HOURLY_HISTORY_FILE = os.path.join(DATA_DIR, "price_history_hourly.json")
DAILY_HISTORY_FILE = os.path.join(DATA_DIR, "price_history_daily_3mo.json")
PORTFOLIO_PERFORMANCE_FILE = os.path.join(DATA_DIR, "portfolio_performance.json")
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")
NEWS_FILE = os.path.join(DATA_DIR, "news.json")
NEWS_SENTIMENT_FILE = os.path.join(DATA_DIR, "news_sentiment.json")
RECOMMENDATIONS_FILE = os.path.join(DATA_DIR, "recommendations.json")
# See export_daily_history_on_demand -- a one-off multi-year backtest
# export, deliberately at the project root (not DATA_DIR) since it's
# meant to be moved into git by hand afterward, unlike every other file
# here, which stays gitignored generated output.
TS_EXPORT_FILE = "ts.json"

# GET /api/dataset-status (see _handle_dataset_status) -- every generated
# file the web app's Dataset tab reports on, paired with the exact command
# that regenerates it (None for a hand-maintained input no command
# produces) and a one-line note on what that command needs (network,
# live IB Gateway, or neither). This is the single place that pairing is
# defined; the endpoint just stat()s each path at request time -- nothing
# here is refreshed on a timer, so the tab always reflects the real files
# on disk, not a cached snapshot of them.
# "id" is this row's own stable identity -- what the client sends back to
# say which row's button it means (see _handle_run_dataset) and what the
# frontend keys the table row on. Separate from "path" because more than
# one row can point at the same file (Screener ranking and its Rescore
# row below both regenerate SORTED_SCREEN_CSV, just via a different,
# lighter command), so path alone isn't a safe unique key any more.
#
# "run", where not None, is what that row's single Run button executes
# (see _handle_run_dataset/_run_subprocess_job/_run_in_process_job) --
# exactly one command per row/button, never a choice of several: a row
# that can be regenerated more than one way (all/prices/rescore) gets one
# row per command instead (see Screener ranking/Screener ranking
# (prices)/Screener ranking (rescore) below), so every button always
# just says "Run" and does the one thing its row's label/command/notes
# describe.
#   {"kind": "subprocess", "argv": [...]} spawns `python -u <argv>` (e.g.
#     ["main.py", "rescore"] or ["ib_server.py", "performance"]) as a
#     real child process and streams its stdout.
#   {"kind": "in_process", "target": "daily"|"hourly"} instead calls
#     refresh_daily_history_on_demand/refresh_hourly_history_on_demand
#     directly on this server's own already-connected IB Gateway
#     connection, the same routing `python main.py ibprices`/`ibhprices`
#     themselves fall into once they notice this server is up (see
#     main.py's refresh_ib_daily_history) -- spawning `python main.py
#     ibprices` as a literal child process here would just print that
#     routing message and then sit blocked on an HTTP call back to this
#     same process for however long the fetch takes, with zero per-
#     ticker output of its own to show.
# None means no button: the only command for that file needs an argument
# this UI doesn't collect (`symbol TICKER ...`, `themes TICKER ...` for a
# specific ticker), or there's no one-shot command for that file at all
# (hand-maintained input, or written only by a background loop while the
# server itself runs).
DATASETS = [
    {
        "id": "screener_ranking",
        "path": SORTED_SCREEN_CSV,
        "label": "Screener ranking",
        "command": "python main.py all",
        "notes": "or `prices` (lighter, skips fresh-within-FRESH_HOURS tickers) / `rescore` (offline, no fetch, just re-ranks what's already downloaded) -- see the two rows below",
        "network": "Yahoo Finance",
        "run": {"kind": "subprocess", "argv": ["main.py", "all"]},
    },
    {
        "id": "screener_ranking_prices",
        "path": SORTED_SCREEN_CSV,
        "label": "Screener ranking (prices)",
        "command": "python main.py prices",
        "notes": "reuses forward_pe.csv as-is, only refreshes the momentum score (still hits yfinance once per ticker) -- lighter than `all`, which also re-fetches forward P/E itself",
        "network": "Yahoo Finance",
        "run": {"kind": "subprocess", "argv": ["main.py", "prices"]},
    },
    {
        "id": "screener_ranking_rescore",
        "path": SORTED_SCREEN_CSV,
        "label": "Screener ranking (rescore)",
        "command": "python main.py rescore",
        "notes": "zero network calls -- recomputes momentum/meanReversion from IB's daily/hourly bars + price_history.json's cached yfinance closes already on disk (see add_momentum_from_cache), then re-ranks; for when only the scoring itself changed, or to pick up ibprices/ibhprices without a full fetch",
        "network": None,
        "run": {"kind": "subprocess", "argv": ["main.py", "rescore"]},
    },
    {
        "id": "forward_pe",
        "path": OUTPUT_CSV,
        "label": "Forward P/E (raw, unranked)",
        "command": "python main.py all",
        "notes": "or `prices` / `rescore` -- see the Screener ranking rows above, both of which rewrite this file too",
        "network": "Yahoo Finance",
        "run": {"kind": "subprocess", "argv": ["main.py", "all"]},
    },
    {
        "id": "gross_margins",
        "path": OUTPUT_CSV,
        "label": "Gross margins (backfill)",
        "command": "python main.py grossmargin",
        "notes": "backfills grossMargins (scoring.margin_rank's third component) onto tickers already in forward_pe.csv from before this factor existed -- `all`/`prices` also pick it up as a side effect for any ticker they fetch fresh",
        "network": "Yahoo Finance",
        "run": {"kind": "subprocess", "argv": ["main.py", "grossmargin"]},
    },
    {
        "id": "insider_ownership",
        "path": OUTPUT_CSV,
        "label": "Insider ownership (backfill)",
        "command": "python main.py insiderown",
        "notes": "backfills heldPercentInsiders (scoring.insiders_rank's ownership component) onto tickers already in forward_pe.csv from before this factor existed -- `all`/`prices` also pick it up as a side effect for any ticker they fetch fresh",
        "network": "Yahoo Finance",
        "run": {"kind": "subprocess", "argv": ["main.py", "insiderown"]},
    },
    {
        "id": "symbol_universe",
        "path": SYMBOLS_FILE,
        "label": "Symbol universe",
        "command": None,
        "notes": "hand-maintained input -- not written by any command",
        "network": None,
        "run": None,
    },
    {
        "id": "raw_yahoo_payloads",
        "path": RAW_DATA_FILE,
        "label": "Raw Yahoo Finance payloads",
        "command": "python main.py all",
        "notes": "or `symbol TICKER ...` -- not `prices`/`rescore`, neither of which touches this file",
        "network": "Yahoo Finance",
        "run": {"kind": "subprocess", "argv": ["main.py", "all"]},
    },
    {
        "id": "yahoo_price_history",
        "path": YAHOO_PRICE_HISTORY_FILE,
        "label": "Daily price history, 1mo (Yahoo)",
        "command": "python main.py yfprices",
        "notes": "lightest option -- yfinance closes only, no forward-PE/momentum-score pipeline; `all`/`prices`/`symbol TICKER ...` also refresh this as a side effect",
        "network": "Yahoo Finance",
        "run": {"kind": "subprocess", "argv": ["main.py", "yfprices"]},
    },
    {
        "id": "ib_hourly",
        "path": HOURLY_HISTORY_FILE,
        "label": "Hourly candlesticks (IB Gateway)",
        "command": "python main.py ibhprices",
        "notes": "restarting this server also refreshes it (same fetch, plus daily candlesticks); the CLI command routes through this server's own IB Gateway connection when it's running (see main.py's refresh_ib_hourly_history), so it no longer needs the server stopped first",
        "network": "Live IB Gateway",
        "run": {"kind": "in_process", "target": "hourly"},
    },
    {
        "id": "ib_daily",
        "path": DAILY_HISTORY_FILE,
        "label": "Daily candlesticks, 3mo (IB Gateway)",
        "command": "python main.py ibprices",
        "notes": "restarting this server also refreshes it (same fetch, plus hourly candlesticks); the CLI command routes through this server's own IB Gateway connection when it's running (see main.py's refresh_ib_daily_history), so it no longer needs the server stopped first",
        "network": "Live IB Gateway",
        "run": {"kind": "in_process", "target": "daily"},
    },
    {
        "id": "social_sentiment",
        "path": SENTIMENT_FILE,
        "label": "Social sentiment (StockTwits)",
        "command": "python main.py all",
        "notes": "only `all`, not `prices`/`rescore`",
        "network": "StockTwits",
        "run": {"kind": "subprocess", "argv": ["main.py", "all"]},
    },
    {
        "id": "news_headlines",
        "path": NEWS_FILE,
        "label": "News headlines",
        "command": None,
        "notes": "written by this server's own news_loop while it's running -- no one-shot command",
        "network": "Live IB Gateway",
        "run": None,
    },
    {
        "id": "news_sentiment",
        "path": NEWS_SENTIMENT_FILE,
        "label": "News sentiment (FinBERT)",
        "command": None,
        "notes": "same as News headlines -- scored as part of the same loop",
        "network": "Live IB Gateway",
        "run": None,
    },
    {
        "id": "form4",
        "path": FORM4_FILE,
        "label": "Insider transactions (SEC Form 4)",
        "command": "python main.py form4",
        "notes": None,
        "network": "SEC EDGAR",
        "run": {"kind": "subprocess", "argv": ["main.py", "form4"]},
    },
    {
        "id": "xbrl",
        "path": XBRL_FACTS_FILE,
        "label": "Company financials (SEC XBRL)",
        "command": "python main.py xbrl",
        "notes": None,
        "network": "SEC EDGAR",
        "run": {"kind": "subprocess", "argv": ["main.py", "xbrl"]},
    },
    {
        "id": "13f",
        "path": THIRTEENF_FILE,
        "label": "Institutional holdings (13F)",
        "command": "python main.py 13f",
        "notes": "bulk SEC download, ~90MB",
        "network": "SEC EDGAR",
        "run": {"kind": "subprocess", "argv": ["main.py", "13f"]},
    },
    {
        "id": "short_interest",
        "path": SHORT_INTEREST_FILE,
        "label": "Short interest (FINRA)",
        "command": "python main.py shortinterest",
        "notes": None,
        "network": "FINRA",
        "run": {"kind": "subprocess", "argv": ["main.py", "shortinterest"]},
    },
    {
        "id": "theme_taxonomy",
        "path": TAXONOMY_FILE,
        "label": "Theme taxonomy",
        "command": None,
        "notes": "hand-maintained input -- not written by any command",
        "network": None,
        "run": None,
    },
    {
        "id": "ticker_themes",
        "path": TICKER_THEMES_FILE,
        "label": "Ticker → theme classification",
        "command": "python main.py themes [TICKER ...]",
        "notes": "Run button classifies every held position (no tickers given -- needs a live IB Gateway connection just to list them); for specific tickers instead, use the CLI form, which is fully offline",
        "network": "Live IB Gateway only via the Run button (no tickers)",
        "run": {"kind": "subprocess", "argv": ["main.py", "themes"]},
    },
    {
        "id": "recommendations",
        "path": RECOMMENDATIONS_FILE,
        "label": "Recommendations",
        "command": "python main.py recommendations",
        "notes": "offline, reads only files already on disk; also auto-rebuilt now by anything that rewrites Screener ranking (all/prices/rescore/symbol)",
        "network": None,
        "run": {"kind": "subprocess", "argv": ["main.py", "recommendations"]},
    },
    {
        "id": "portfolio_performance",
        "path": PORTFOLIO_PERFORMANCE_FILE,
        "label": "Portfolio performance (Flex Query)",
        "command": "python ib_server.py performance [YYYY-MM-DD]",
        "notes": "Run button omits the date, which falls back to PORTFOLIO_START_DATE (see fetch_account_performance); for a specific start date instead, use the CLI form -- also auto-refreshed every 6h by the running server regardless",
        "network": "IBKR Flex Web Service (not IB Gateway)",
        "run": {"kind": "subprocess", "argv": ["ib_server.py", "performance"]},
    },
    {
        "id": "trades",
        "path": TRADES_FILE,
        "label": "Past trades (Flex Query)",
        "command": "python ib_server.py trades [YYYY-MM-DD]",
        "notes": "Run button omits the date (no client-side trim); for a specific start date instead, use the CLI form. Merges by tradeID into whatever's already on disk, so repeated runs accumulate rather than only ever showing the query's own configured window",
        "network": "IBKR Flex Web Service (not IB Gateway)",
        "run": {"kind": "subprocess", "argv": ["ib_server.py", "trades"]},
    },
]

# {request path: file path} served fresh from disk on every request (see
# _handle_static_file) -- every file web/'s own sync-data npm script
# copies into web/public/ once at `npm run dev` startup (see
# package.json), which is why every page's own fetch('/sorted_screen.csv')-
# style call used to go stale the moment a Dataset-tab Run (or any CLI
# download) rewrote the REAL file: that public/ copy never updates again
# until the dev server restarts. vite.config.js proxies exactly these
# same request paths to this server during `npm run dev` instead, so
# these keys must match what every page's existing fetch() calls already
# use, unchanged -- keep both dicts in sync by hand if either changes.
STATIC_FILES = {
    "/sorted_screen.csv": SORTED_SCREEN_CSV,
    "/raw_data.json": RAW_DATA_FILE,
    "/social_sentiment.json": SENTIMENT_FILE,
    "/news_sentiment.json": NEWS_SENTIMENT_FILE,
    "/price_history.json": YAHOO_PRICE_HISTORY_FILE,
    "/price_history_hourly.json": HOURLY_HISTORY_FILE,
    "/price_history_daily_3mo.json": DAILY_HISTORY_FILE,
    "/portfolio_performance.json": PORTFOLIO_PERFORMANCE_FILE,
    "/trades.json": TRADES_FILE,
    "/theme_taxonomy.json": TAXONOMY_FILE,
    "/ticker_themes.json": TICKER_THEMES_FILE,
    "/sec/form4/insider_transactions.json": FORM4_FILE,
    "/sec/13f/institutional_holdings.json": THIRTEENF_FILE,
    "/sec/13f/institutional_holders.json": THIRTEENF_HOLDERS_FILE,
    "/recommendations.json": RECOMMENDATIONS_FILE,
    "/ARKK_HOLDINGS.csv": os.path.join(DATA_DIR, "ARKK_HOLDINGS.csv"),
}

# Which DATASETS entries are {ticker: [{date, close, ...}, ...]} time series
# worth a staleness check in the first place -- every other file either
# isn't a series at all (raw_data.json, sorted_screen.csv) or is itself the
# authoritative "last touched" signal (mtime IS the real freshness there).
# These two are exactly the pair the RecommendationsView.tsx/PositionsView.
# tsx/ScreenerView.tsx previousClose() helpers read from -- see their own
# "pick whichever of daily/monthly is more recent" logic.
_PRICE_HISTORY_STALENESS_PATHS = {YAHOO_PRICE_HISTORY_FILE, DAILY_HISTORY_FILE}

# most_recent_completed_trading_day (see scoring.py) is the "what date
# should a settled close exist for by now" heuristic this staleness check
# is built on. Confirmed live (2026-08-18): yfinance's own period="1mo"
# history call skips 2026-08-17 entirely for every ticker checked, so even
# a same-morning re-fetch still shows Friday 08-14 as the latest prior-day
# bar until the next real trading day's close arrives and ages the gap out
# of relevance. That's not a bug in this check -- that's exactly the
# condition the Dataset tab's warning exists to surface, since the same
# gap is what silently flipped the sign on RecommendationsView.tsx's
# daily-move reading for at least one held ticker (PUMP) before it was
# caught.


def _price_history_staleness(path):
    """{"latestBarDate": "YYYY-MM-DD", "expectedBarDate": ..., "stale":
    bool} for a {ticker: [{date, close}, ...]} file -- latestBarDate is
    the MODE (most common) last-bar date BEFORE today across every ticker,
    not simply each series' raw last entry. Today's own bar is deliberately
    excluded from consideration, mirroring previousClose()'s own
    lastBarBeforeToday on the frontend exactly (see PositionsView.tsx/
    RecommendationsView.tsx/ScreenerView.tsx): yfinance's intraday fetch
    often carries a same-day "close" that's really just the latest trade
    price, not a settled close, so a file can have a bar dated today
    while still being missing the one previousClose() actually needs.
    Confirmed live: right after a same-morning re-fetch, this file's raw
    last entry was already today's date, which would've read as "fresh"
    -- but the bar previousClose() actually falls back to was still
    Friday's, 3 calendar days stale, because the real Monday close was
    simply never returned by yfinance for anyone. Using the mode (not the
    max) means a handful of individually-stuck tickers (a delisting, a
    persistent fetch failure) don't flip this on when the universe as a
    whole is current -- the mode is what most tickers actually have,
    which is what a systemic fetch failure actually moves (confirmed
    live: a partial yfinance rate-limit once left ~99% of tickers stuck
    days behind while the file's own mtime still looked fresh, since it
    gets rewritten even when most of its content didn't change). None if
    the file is missing/unparseable or carries no qualifying series at
    all."""
    try:
        with open(path) as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    today = datetime.now().date().isoformat()
    prev_dates = []
    for series in history.values():
        for bar in reversed(series):
            bar_date = bar["date"][:10]
            if bar_date < today:
                prev_dates.append(bar_date)
                break
    if not prev_dates:
        return None
    latest_bar_date = Counter(prev_dates).most_common(1)[0][0]
    expected_bar_date = most_recent_completed_trading_day()
    return {
        "latestBarDate": latest_bar_date,
        "expectedBarDate": expected_bar_date,
        "stale": latest_bar_date < expected_bar_date,
    }
# The query's own range starts 2026-06-30/07-01, but that first day (and
# the bare baseline row before it) isn't a real trading day's worth of
# activity in this account, so the Portfolio tab starts from here instead.
# Only trims rows client-side, same as passing this as fetch_account_
# performance's start_date argument -- doesn't shrink what IBKR generates.
PORTFOLIO_START_DATE = "2026-07-03"
# Candlestick coverage is capped separately from MAX_STREAMED_SYMBOLS — it's
# not a live-data budget, it's how many tickers IB's paced reqHistoricalData
# (200 requests/10min, see IBApp.get_ib_historical_bars_async) can
# realistically cover in one run. 500 tickers x 2 series is ~50 minutes;
# the full sorted_screen.csv (~1,663 tickers) would be ~2.8 hours.
CANDLESTICK_TOP_N = 500
HEARTBEAT_SECONDS = 15

app = IBApp()
last_price_by_ticker = {}
positions_by_ticker = {}
account_status = {}
# {ticker: {"qty": signed net shares traded today, "value": sum(signedQty *
# fillPrice)}} — see IBApp.get_today_executions_async / refresh_trades.
# PositionsView.jsx uses this to mark shares traded today at their own
# fill price instead of assuming the whole position was held since
# yesterday's close.
trades_by_ticker = {}
# [{ticker, action, orderType, quantity, limitPrice, auxPrice, status,
# filled, remaining}, ...] -- every currently-working order (see
# refresh_open_orders / IBApp.get_open_orders_async). A list, not a
# ticker-keyed map (see refresh_open_orders' own comment on why).
open_orders = []
# {ticker: {dailyPnL, unrealizedPnL, realizedPnL, position, value}} -- IBKR's
# own reqPnLSingle figures, kept for every symbol ever held this process
# (never removed on going flat -- see on_position/on_pnl_single below and
# this file's own module docstring). The live PnLSingle OBJECTS themselves
# (not this serializable snapshot) live in _pnl_singles_by_conid/
# _symbol_by_conid just below.
pnl_by_ticker = {}
# conId -> the live PnLSingle object ib_insync updates in place (see
# IBApp.subscribe_position_pnl) -- held onto so on_position doesn't
# re-subscribe a conId it's already watching. conId -> symbol is a
# separate map since PnLSingle updates (on_pnl_single) only carry conId,
# never the symbol itself.
_pnl_singles_by_conid = {}
_symbol_by_conid = {}
lock = threading.Lock()
# Set once by run_ib_client to the asyncio event loop it owns -- lets the
# HTTP handler thread (GET /api/news/article) schedule a one-off coroutine
# (app.get_news_article_async) onto that loop via run_coroutine_threadsafe,
# the same way every other IB Gateway call in this file is confined to
# run_ib_client's single connection/event loop.
ib_loop = None

# Symbols with an active reqMktData subscription — every held position plus
# the highest-ranked tickers up to MAX_STREAMED_SYMBOLS up front, plus
# anything on_position finds a new nonzero position for afterward. Guards
# against double-subscribing the same symbol.
streamed_symbols = set()

# One Queue per connected SSE client; broadcast() drops the latest snapshot
# into each. maxsize=1 because only the current state matters here, not a
# history of every intermediate tick — a slow client just skips ahead to
# whatever's newest instead of falling behind on a backlog.
subscribers = []
subscribers_lock = threading.Lock()


def _extract_price(ticker):
    """Mirrors IBApp.on_pending_tickers' last/bid/ask/close fallback, since
    `last` is often stale or NaN outside of regular trading hours."""
    price = ticker.last
    if price is None or math.isnan(price) or price <= 0:
        for alt in (ticker.bid, ticker.ask, ticker.close):
            if alt and not math.isnan(alt) and alt > 0:
                return alt
        return None
    return price


def broadcast():
    with lock:
        payload = json.dumps({
            "prices": last_price_by_ticker,
            "positions": positions_by_ticker,
            "account": account_status,
            "trades": trades_by_ticker,
            "pnl": pnl_by_ticker,
            "openOrders": open_orders,
        })
    with subscribers_lock:
        for q in subscribers:
            if q.full():
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
            q.put_nowait(payload)


def _clean(v):
    """A real, positive quote, or None — IB Gateway reports NaN (or a
    stale 0/negative) for bid/ask outside regular trading hours or on a
    thinly-quoted symbol, same as the last/close fields _extract_price
    already guards against."""
    return v if v is not None and not math.isnan(v) and v > 0 else None


def on_pending_tickers(tickers):
    now = datetime.now().isoformat(timespec="seconds")
    with lock:
        for t in tickers:
            price = _extract_price(t)
            if price is not None:
                last_price_by_ticker[t.contract.symbol] = {
                    "last": price,
                    "bid": _clean(t.bid),
                    "ask": _clean(t.ask),
                    "timestamp": now,
                }
    broadcast()


def _safe_float(v):
    """Coerces to a plain float, or None if that's not possible/meaningful.
    Guards three real gotchas: ib_insync can hand back a Decimal (JSON-
    serializable floats only, not Decimal — a bare Decimal here would throw
    inside json.dumps and break every subsequent broadcast, not just this
    one field); IBKR occasionally reports NaN avgCost for positions with an
    unknown cost basis (e.g. certain corporate actions) — a NaN that
    reaches json.dumps gets written out as the bare token `NaN`, which is
    not valid JSON and makes the browser's JSON.parse reject the whole
    message; and PnLSingle's realizedPnL is IBKR's C++ DBL_MAX sentinel
    (sys.float_info.max, 1.7976931348623157e+308 -- not NaN, so the
    isnan() check alone doesn't catch it) for a conId with no realized P&L
    yet today -- confirmed live (GOOG, a long held but not traded today).
    That's a real (JSON-legal) float, so it would otherwise sail through
    to the frontend as a nonsense multi-hundred-digit number instead of
    the "nothing realized today" it actually means."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or abs(f) >= sys.float_info.max:
        return None
    return f


def on_position(position):
    """Fired once per existing position right after reqPositions() is
    called (an initial snapshot burst — see stream_prices_and_positions, which waits
    for this before building its subscription list), then again on every
    future change for the life of the process. The streamed_symbols check
    below is what catches a position opened after that startup batch: a
    newly-held ticker outside the top-ranked set gets its price
    subscription added here, on the fly, using the contract IBKR already
    gave us (a real account position always has a resolved conId, no
    qualifyContracts round-trip needed).

    Also subscribes reqPnLSingle the same reactive way, the first time a
    given conId is seen with a nonzero position -- see IBApp.
    subscribe_position_pnl. Deliberately NOT re-subscribed or torn down
    when this same conId later reports shares back to 0 (a closed
    position): the whole point is that IBKR keeps reporting that conId's
    dailyPnL/realizedPnL for the rest of the session even after it goes
    flat (see this file's own module docstring) -- tearing the
    subscription down here the moment shares hit 0 would throw that
    away right when it becomes useful."""
    symbol = position.contract.symbol
    shares = _safe_float(position.position)
    with lock:
        if not shares:
            positions_by_ticker.pop(symbol, None)
        else:
            positions_by_ticker[symbol] = {"shares": shares, "avgCost": _safe_float(position.avgCost)}

    if shares and symbol not in streamed_symbols:
        streamed_symbols.add(symbol)
        c = position.contract
        # IBKR's position report reflects the contract's actual execution/
        # booking venue (e.g. BATS for a stock also listed elsewhere) —
        # that's not necessarily an exchange this account has market-data
        # permissions for. Every other contract in this codebase routes
        # through SMART (see IBApp.make_contract); force this one to match
        # rather than silently subscribing to a venue that returns nothing.
        c.exchange = "SMART"
        print(f"reqMktData (from position): {symbol} (conId={c.conId}, exchange={c.exchange})")
        app.ib.reqMktData(c, "", False, False)

    con_id = position.contract.conId
    if shares and con_id not in _pnl_singles_by_conid:
        _symbol_by_conid[con_id] = symbol
        print(f"reqPnLSingle (from position): {symbol} (conId={con_id})")
        _pnl_singles_by_conid[con_id] = app.subscribe_position_pnl(con_id)

    broadcast()


def on_pnl_single(pnl_single):
    """Fired on every reqPnLSingle update for a conId subscribed by
    on_position above -- IBKR pushes these on its own cadence (roughly
    once a second per subscribed conId while the market's open),
    independent of price ticks or position changes, so this needs its
    own broadcast() call rather than piggybacking on-position's. Keeps
    reporting (position 0, dailyPnL/realizedPnL still populated,
    unrealizedPnL 0) for a symbol closed out earlier today -- see this
    file's own module docstring -- since on_position never cancels the
    subscription just because shares went to 0."""
    symbol = _symbol_by_conid.get(pnl_single.conId)
    if symbol is None:
        return
    with lock:
        pnl_by_ticker[symbol] = {
            "dailyPnL": _safe_float(pnl_single.dailyPnL),
            "unrealizedPnL": _safe_float(pnl_single.unrealizedPnL),
            "realizedPnL": _safe_float(pnl_single.realizedPnL),
            "position": _safe_float(pnl_single.position),
            "value": _safe_float(pnl_single.value),
        }
    broadcast()


# Attempts per symbol, so a genuinely-unfixable 10168 (e.g. no market
# data permission for that contract at all) doesn't retry forever.
_market_data_retries = {}


def on_market_data_error(reqId, errorCode, errorString, contract):
    """Retries a reqMktData call that failed with error 10168 ("Requested
    market data is not subscribed. Delayed market data is not enabled.").
    This fires when a request races ahead of reqMarketDataType(3)'s own
    confirmation landing at IB Gateway — stream_prices_and_positions' asyncio.sleep(1)
    before reqPositions() narrows that window but doesn't close it, since
    IB Gateway's ack for reqMarketDataType has no fixed latency; whichever
    of on_position's reactive subscribes happen to fire before it lands
    get this error, and ib_insync itself never retries a failed request,
    so a symbol caught in that window would otherwise go the entire
    process lifetime with no live price. By the time this error round-
    trips back here, reqMarketDataType(3) is certain to have landed, so
    the retry succeeds."""
    if errorCode != 10168 or contract is None:
        return
    symbol = contract.symbol
    attempts = _market_data_retries.get(symbol, 0)
    if attempts >= 3:
        print(f"Giving up on {symbol} after {attempts} reqMktData retries (error 10168)")
        return
    _market_data_retries[symbol] = attempts + 1
    print(f"Retrying reqMktData for {symbol} after error 10168 (attempt {attempts + 1})")
    app.ib.reqMktData(contract, "", False, False)


def on_account_value(av):
    """Fired per account-value update once subscribed (see
    reqAccountUpdates in stream_prices_and_positions) — an initial burst covering
    everything IBKR reports, then again whenever a value changes. Only
    IBApp.ACCOUNT_STATUS_TAGS are kept, same curated set
    format_account_status prints as a table."""
    if av.tag not in IBApp.ACCOUNT_STATUS_TAGS:
        return
    with lock:
        try:
            account_status[av.tag] = float(av.value)
        except (TypeError, ValueError):
            account_status[av.tag] = av.value
    broadcast()


async def fetch_candlestick_history(streamed_tickers):
    """Runs as a background asyncio task on the single shared IB Gateway
    connection (see run_ib_client) — get_ib_historical_bars_async awaits
    each reqHistoricalData call and paces via asyncio.sleep rather than
    blocking, so fetching two series for hundreds of tickers (which can
    take hours) never stalls live prices/positions or snapshot polling
    sharing that same connection's event loop.

    Covers the top CANDLESTICK_TOP_N ranked tickers, unioned with every
    RATED_FOR_EXTRAS ticker (Strong Buy/Buy/Sell/Strong Sell) and with
    streamed_tickers (every ticker actually streamed a live price —
    positions and ranked fill alike) so a held position outside the top
    N, like an ETF that isn't even in sorted_screen.csv, still gets
    covered rather than silently dropped.

    The RATED_FOR_EXTRAS union matters because CANDLESTICK_TOP_N alone
    only ever reaches the BEST-scoring end of the ranking -- load_top_tickers
    takes the first N rows of a file already sorted ascending by score, so
    a Strong Sell sitting near the bottom of ~1900 scored tickers is never
    in that slice no matter how large N is, only a held position would
    pull it in via streamed_tickers. Confirmed in practice: before this
    union, 96% of Buy/Strong Buy tickers had hourly bars (and so a real
    meanReversion reading) but only 11% of Sell/Strong Sell ones did --
    explicit instruction to fix that gap, since RecommendationsView.jsx's
    Short-side mean-reversion gate is only as good as this coverage.
    Same RATED_FOR_EXTRAS scope MAX_STREAMED_SYMBOLS already uses to fill
    out live-price streaming (see this module's own docstring above), just
    applied here too.

    Writes HOURLY_HISTORY_FILE and DAILY_HISTORY_FILE once each series is
    done; Asset.jsx fetches them as static files, same as
    main.py's price_history.json."""
    ranked = set(load_top_tickers(SORTED_SCREEN_CSV, CANDLESTICK_TOP_N))
    rated = set(load_rated_tickers(SORTED_SCREEN_CSV, RATED_FOR_EXTRAS))
    covered = ranked | rated
    tickers = sorted(covered | set(streamed_tickers))
    print(
        f"Candlestick history covers {len(tickers)} ticker(s): top {len(ranked)} ranked "
        f"+ {len(rated - ranked)} rated-for-extras-but-outside-top-N "
        f"+ {len(set(tickers) - covered)} streamed-but-otherwise-uncovered "
        f"(e.g. positions outside both of those)"
    )

    print(f"Fetching 1mo hourly bars for {len(tickers)} ticker(s) (this can take a while, paced by IB's rate limit)...")
    hourly = await app.get_ib_historical_bars_async(tickers, "1 M", "1 hour")
    with open(HOURLY_HISTORY_FILE, "w") as f:
        json.dump(hourly, f)
    print(f"Wrote {HOURLY_HISTORY_FILE} ({sum(1 for v in hourly.values() if v)}/{len(tickers)} tickers with bars)")

    print(f"Fetching 3mo daily bars for {len(tickers)} ticker(s)...")
    daily = await app.get_ib_historical_bars_async(tickers, "3 M", "1 day")
    with open(DAILY_HISTORY_FILE, "w") as f:
        json.dump(daily, f)
    print(f"Wrote {DAILY_HISTORY_FILE} ({sum(1 for v in daily.values() if v)}/{len(tickers)} tickers with bars)")


async def refresh_daily_history_on_demand(log_fn=None):
    """POST /api/admin/refresh-ib-daily (see _handle_refresh_ib_daily) --
    runs main.py's own download_ib_daily_history logic (same staleness
    gate: only a ticker whose existing bar is missing or older than
    most_recent_completed_trading_day() actually gets refetched) on THIS
    process's already-connected clientId-0 IB Gateway connection, instead
    of main.py opening a second one itself. Exists because IB Gateway
    refuses a second simultaneous API connection while this server is
    running (confirmed live, times out regardless of clientId -- see
    main.py's IB_HISTORY_CLIENT_ID comment) -- `python main.py ibprices`
    calls this endpoint instead of app.connect()-ing whenever it detects
    this server is up (see main.py's refresh_ib_daily_history).

    Same scope as main.py's own call: top CANDLESTICK_TOP_N ranked union
    RATED_FOR_EXTRAS union every currently-held ticker -- held read
    straight off positions_by_ticker (already populated live by
    on_position) rather than a fresh reqPositions() round-trip. Merges
    into the existing DAILY_HISTORY_FILE rather than replacing it
    wholesale, unlike fetch_candlestick_history's own startup fetch --
    same reasoning as download_ib_daily_history's own docstring: the
    staleness gate here means a given call may only touch a handful of
    tickers out of the full scope.

    log_fn, if given, is called with a line of progress text as it
    happens (one ticker at a time via get_ib_historical_bars_async's own
    on_ticker, plus a line before/after the batch) -- used by the
    Dataset tab's Run button (see _run_in_process_job) to show live
    per-ticker progress; the HTTP endpoint above calls this with no
    log_fn, unchanged from before."""
    ranked = set(load_top_tickers(SORTED_SCREEN_CSV, CANDLESTICK_TOP_N))
    rated = set(load_rated_tickers(SORTED_SCREEN_CSV, RATED_FOR_EXTRAS))
    with lock:
        held = set(positions_by_ticker)
    tickers = sorted(ranked | rated | held)

    try:
        with open(DAILY_HISTORY_FILE) as f:
            existing = json.load(f)
    except FileNotFoundError:
        existing = {}
    expected = most_recent_completed_trading_day()
    stale = [t for t in tickers if not existing.get(t) or existing[t][-1]["date"][:10] < expected]
    if not stale:
        if log_fn:
            log_fn(f"IB daily history already current for all {len(tickers)} candidate ticker(s); skipping IB Gateway fetch")
        return {"skipped": True, "tickersTotal": len(tickers)}

    if log_fn:
        log_fn(f"Fetching IB 3mo daily bars for {len(stale)}/{len(tickers)} stale/missing ticker(s) (paced, can take a while)...")
    fresh = await app.get_ib_historical_bars_async(stale, "3 M", "1 day", on_ticker=(lambda s: log_fn(f"Fetching {s}...")) if log_fn else None)
    existing.update(fresh)
    with open(DAILY_HISTORY_FILE, "w") as f:
        json.dump(existing, f)
    got = sum(1 for v in fresh.values() if v)
    if log_fn:
        log_fn(f"Wrote {DAILY_HISTORY_FILE} ({got}/{len(stale)} fetched tickers had bars; {len(existing)} tickers total on file)")
    return {"skipped": False, "tickersTotal": len(tickers), "staleFetched": len(stale), "gotBars": got}


async def refresh_hourly_history_on_demand(log_fn=None):
    """POST /api/admin/refresh-ib-hourly (see _handle_refresh_ib_hourly)
    -- the hourly twin of refresh_daily_history_on_demand: same reason
    for existing, same scope, same on-demand/on-this-connection approach,
    same log_fn use (see that function's docstring), just against
    HOURLY_HISTORY_FILE via "1 M"/"1 hour" bars instead of the daily
    file/duration."""
    ranked = set(load_top_tickers(SORTED_SCREEN_CSV, CANDLESTICK_TOP_N))
    rated = set(load_rated_tickers(SORTED_SCREEN_CSV, RATED_FOR_EXTRAS))
    with lock:
        held = set(positions_by_ticker)
    tickers = sorted(ranked | rated | held)

    try:
        with open(HOURLY_HISTORY_FILE) as f:
            existing = json.load(f)
    except FileNotFoundError:
        existing = {}
    expected = most_recent_completed_trading_day()
    stale = [t for t in tickers if not existing.get(t) or existing[t][-1]["date"][:10] < expected]
    if not stale:
        if log_fn:
            log_fn(f"IB hourly history already current for all {len(tickers)} candidate ticker(s); skipping IB Gateway fetch")
        return {"skipped": True, "tickersTotal": len(tickers)}

    if log_fn:
        log_fn(f"Fetching IB 1mo hourly bars for {len(stale)}/{len(tickers)} stale/missing ticker(s) (paced, can take a while)...")
    fresh = await app.get_ib_historical_bars_async(stale, "1 M", "1 hour", on_ticker=(lambda s: log_fn(f"Fetching {s}...")) if log_fn else None)
    existing.update(fresh)
    with open(HOURLY_HISTORY_FILE, "w") as f:
        json.dump(existing, f)
    got = sum(1 for v in fresh.values() if v)
    if log_fn:
        log_fn(f"Wrote {HOURLY_HISTORY_FILE} ({got}/{len(stale)} fetched tickers had bars; {len(existing)} tickers total on file)")
    return {"skipped": False, "tickersTotal": len(tickers), "staleFetched": len(stale), "gotBars": got}


async def export_daily_history_on_demand(duration, log_fn=None):
    """POST /api/admin/export-daily-history (see
    _handle_export_daily_history) -- a one-off bulk daily-bars pull for
    backtesting/analysis, NOT the regular scoring pipeline (that's
    refresh_daily_history_on_demand, DAILY_3MO_HISTORY_FILE, always "3
    M"). Same connection-routing reasoning as every other on-demand
    endpoint here, same ranked/rated/held ticker scope as
    refresh_daily_history_on_demand -- but takes `duration` as a
    parameter (e.g. "2 Y") instead of hardcoding "3 M", and always
    fetches every ticker in scope rather than only the stale ones, since
    an export like this wants one consistent, complete pull, not a
    merge with whatever partial history happens to already be on disk.
    Writes TS_EXPORT_FILE (ts.json) fresh each call -- overwritten
    wholesale, not merged, since this is a standalone snapshot for
    someone to move into git themselves, not a file the rest of this
    app reads back."""
    ranked = set(load_top_tickers(SORTED_SCREEN_CSV, CANDLESTICK_TOP_N))
    rated = set(load_rated_tickers(SORTED_SCREEN_CSV, RATED_FOR_EXTRAS))
    with lock:
        held = set(positions_by_ticker)
    tickers = sorted(ranked | rated | held)

    if log_fn:
        log_fn(f"Fetching {duration} of daily bars for {len(tickers)} ticker(s) (paced, can take a while)...")
    fresh = await app.get_ib_historical_bars_async(
        tickers, duration, "1 day", on_ticker=(lambda s: log_fn(f"Fetching {s}...")) if log_fn else None
    )
    with open(TS_EXPORT_FILE, "w") as f:
        json.dump(fresh, f)
    got = sum(1 for v in fresh.values() if v)
    if log_fn:
        log_fn(f"Wrote {TS_EXPORT_FILE} ({got}/{len(tickers)} tickers had bars)")
    return {"tickersTotal": len(tickers), "gotBars": got, "outputPath": TS_EXPORT_FILE}


# Prices for every screener ticker (all of sorted_screen.csv, not just a
# top-ranked slice) NOT among the MAX_STREAMED_SYMBOLS this process holds
# a persistent reqMktData subscription for — refreshed periodically via
# IB's *snapshot* request (reqMktData with snapshot=True), which resolves
# once and releases its market-data line immediately rather than holding
# one open. That's what makes this free to run against the whole screener
# universe without touching the MAX_STREAMED_SYMBOLS budget at all: a
# snapshot never counts as one of the persistent lines error 101
# complains about.
SNAPSHOT_INTERVAL_SECONDS = 1200  # 20 minutes
# The full screener universe is ~1,600+ tickers — firing that many
# reqMktData snapshot requests in one burst risks IB's soft ~50
# messages/sec socket rate limit (unlike reqHistoricalData, ib_insync
# doesn't pace reqTickersAsync for you). Chunking with a short pause
# between chunks keeps each burst well under that limit; 20 minutes gives
# plenty of slack for ~9 chunks of 1s each to add up to nothing.
SNAPSHOT_CHUNK_SIZE = 200
SNAPSHOT_CHUNK_DELAY_SECONDS = 1


async def fetch_snapshot_prices(tickers):
    """One-shot {last, bid, ask} per ticker via ib_insync's reqTickersAsync
    (a snapshot reqMktData per contract, awaited concurrently within each
    chunk and released the moment each resolves — no separate connection
    and no fixed sleep(11) needed, unlike the old per-symbol sync
    version). Skips (rather than raising on) any ticker make_contract or
    qualifyContracts can't resolve, same as stream_prices_and_positions'
    own batch subscribe."""
    contracts = {}
    for t in tickers:
        c = app.make_contract(t)
        if c is not None:
            contracts[t] = c
    if not contracts:
        return {}

    qualified = await app.ib.qualifyContractsAsync(*contracts.values())
    unqualified = sorted(set(contracts) - {c.symbol for c in qualified})
    if unqualified:
        print(f"Snapshot: qualifyContracts() rejected: {', '.join(unqualified)}")

    results = {}
    for i in range(0, len(qualified), SNAPSHOT_CHUNK_SIZE):
        chunk = qualified[i : i + SNAPSHOT_CHUNK_SIZE]
        snapshot_tickers = await app.ib.reqTickersAsync(*chunk)
        for tk in snapshot_tickers:
            price = _extract_price(tk)
            if price is not None:
                results[tk.contract.symbol] = {"last": price, "bid": _clean(tk.bid), "ask": _clean(tk.ask)}
        if i + SNAPSHOT_CHUNK_SIZE < len(qualified):
            await asyncio.sleep(SNAPSHOT_CHUNK_DELAY_SECONDS)
    return results


async def snapshot_loop():
    """Runs forever as a background asyncio task on the single shared IB
    Gateway connection (see run_ib_client) — reqTickersAsync + asyncio.sleep
    mean a slow round-trip here never risks stalling live prices/positions
    or the candlestick fetch sharing that same connection's event loop.

    Every SNAPSHOT_INTERVAL_SECONDS, refreshes the price for every ticker
    in sorted_screen.csv that isn't already covered by a live reqMktData
    subscription — the whole screener, not just a top-ranked slice, so
    every asset in the Screener tab shows a price even when it's far
    outside the MAX_STREAMED_SYMBOLS live-streamed set. Recomputed fresh
    each cycle (not once at startup) since streamed_symbols can grow over
    the process's life as on_position picks up newly opened positions,
    and sorted_screen.csv's own ranking can change between cycles too.

    Ordered by _priority_tickers -- whatever's actually rendering as a
    card on the Recommendations tab (Long/Short first, then the
    Strong Buy/Strong Sell "blocked" audit section, see that function's
    own docstring) goes first, then every other ranked ticker. A full
    sweep of ~1,600+ tickers can take several SNAPSHOT_CHUNK_SIZE-chunks
    to complete, and the old plain-file-order sweep meant Strong Sell
    (sorted_screen.csv's very last rows) was consistently the LAST thing
    refreshed each cycle -- exactly backwards from priority. This way the
    names actually on-screen right now get a fresh price earliest in the
    cycle even while the rest of the sweep is still in progress."""
    while True:
        priority = _priority_tickers()
        ranked = load_top_tickers(SORTED_SCREEN_CSV)
        ordered = priority + [t for t in ranked if t not in priority]
        with lock:
            tickers = [t for t in ordered if t not in streamed_symbols]
        if tickers:
            print(f"Snapshot: fetching {len(tickers)} screener ticker(s) outside the live stream...")
            results = await fetch_snapshot_prices(tickers)
            now = datetime.now().isoformat(timespec="seconds")
            with lock:
                for symbol, data in results.items():
                    last_price_by_ticker[symbol] = {**data, "timestamp": now}
            broadcast()
            print(f"Snapshot: got a price for {len(results)}/{len(tickers)} ticker(s)")
        await asyncio.sleep(SNAPSHOT_INTERVAL_SECONDS)


# A trade isn't pushed like a tick — IB Gateway has no "fills changed"
# event this file subscribes to — so today's executions need polling.
# Cheap and not paced (unlike reqHistoricalData), so a short interval is
# fine; this just trades off how stale a same-day trade's contribution to
# `trades` can be against how often it's worth re-asking.
TRADES_REFRESH_SECONDS = 300  # 5 minutes


async def refresh_trades():
    """Re-fetches today's fills (IBApp.get_today_executions_async) and
    replaces trades_by_ticker wholesale — each call already returns every
    fill for the day, not just new ones since last time, so there's
    nothing to merge incrementally."""
    global trades_by_ticker
    trades = await app.get_today_executions_async()
    with lock:
        trades_by_ticker = trades
    broadcast()
    if trades:
        print(f"Trades: {len(trades)} symbol(s) traded today: {', '.join(sorted(trades))}")


async def trades_loop():
    """Runs forever as a background asyncio task on the single shared IB
    Gateway connection (see run_ib_client), same pattern as snapshot_loop.
    Refreshes immediately on startup (the while loop's first iteration
    runs before its first sleep) so a trade made earlier today is already
    reflected in the very first /api/stream snapshot, not just after the
    first TRADES_REFRESH_SECONDS tick."""
    while True:
        await refresh_trades()
        await asyncio.sleep(TRADES_REFRESH_SECONDS)


# Same "no push event to key off, so poll" situation as trades above, but
# a materially shorter interval -- a working order is actionable (it can
# fill or get rejected any moment) in a way a past fill isn't, so letting
# this run as stale as TRADES_REFRESH_SECONDS would show a stale "still
# working" order well after it's actually filled or been cancelled.
OPEN_ORDERS_REFRESH_SECONDS = 30


def _serialize_open_trade(t):
    """One ib_insync Trade (order + contract + live orderStatus) down to
    the plain fields TradesView.tsx actually shows -- quantity/filled/
    remaining coerced through _safe_float for the same reasons every
    other numeric field in this file is (Decimal/NaN aren't JSON-safe).
    limitPrice/auxPrice are None for an order type that doesn't use them
    (e.g. a plain market order has no lmtPrice) rather than IB's own 0.0/
    unset-sentinel reading, which would otherwise show as a real $0 price."""
    o, st = t.order, t.orderStatus
    return {
        "ticker": t.contract.symbol,
        "action": o.action,
        "orderType": o.orderType,
        "quantity": _safe_float(o.totalQuantity),
        "limitPrice": _safe_float(o.lmtPrice) or None,
        "auxPrice": _safe_float(o.auxPrice) or None,
        "status": st.status,
        "filled": _safe_float(st.filled),
        "remaining": _safe_float(st.remaining),
    }


async def refresh_open_orders():
    """Re-fetches every currently-working order (IBApp.
    get_open_orders_async) and replaces open_orders wholesale -- same
    "each call already returns the full current set, nothing to merge
    incrementally" shape as refresh_trades. A list, not a {ticker: ...}
    map like trades_by_ticker -- a single ticker can have more than one
    working order at once (e.g. separate buy and sell orders), which a
    ticker-keyed map couldn't represent."""
    global open_orders
    trades = await app.get_open_orders_async()
    with lock:
        open_orders = [_serialize_open_trade(t) for t in trades]
    broadcast()
    if open_orders:
        print(f"Open orders: {len(open_orders)} working order(s): {', '.join(o['ticker'] for o in open_orders)}")


async def open_orders_loop():
    """Runs forever as a background asyncio task on the single shared IB
    Gateway connection (see run_ib_client), same pattern as trades_loop --
    refreshes immediately on startup, then every OPEN_ORDERS_REFRESH_SECONDS."""
    while True:
        await refresh_open_orders()
        await asyncio.sleep(OPEN_ORDERS_REFRESH_SECONDS)


# ---------------------------------------------------------------------- #
#  News headlines (IBApp.get_news_headlines_async)                       #
# ---------------------------------------------------------------------- #
# Both how far back get_news_headlines_async asks IB for on each fetch and
# the _prune_and_write_news cutoff below share this one constant -- a
# month gives NewsView.jsx (the News tab) and the screener's News Sentiment
# column real history to work with, instead of the 3-day window this
# started at, which only really served the live per-ticker news panel/
# popup.
NEWS_WINDOW_DAYS = 30
# Full screener is ~1,600+ tickers; get_news_headlines_async's own pacing
# budget (~55 requests/10min, no documented IB limit otherwise) makes one
# full pass take multiple hours -- see that method's docstring. Chunking
# here means news.json gets updated incrementally as each chunk finishes,
# instead of the whole multi-hour pass being invisible until the end.
NEWS_CHUNK_SIZE = 40
# Gap between finishing one full pass over the screener and starting the
# next one -- headlines don't need to be fresher than this to be useful.
NEWS_LOOP_PASS_DELAY_SECONDS = 1800  # 30 minutes

# Dow Jones runs a recurring auto-generated "<Company> Files 8K - <reason>
# ><TICKER>" headline for every SEC Form 8-K filing (routine or material --
# the headline text alone doesn't say which). It's pure filing-noise, not
# news to score or show: no scoring adjustment fixes it, so unlike
# news_sentiment.py's own headline-pattern handling (which retags a
# headline's sentiment) this one is dropped entirely at merge time, before
# it ever reaches news_by_ticker/news.json.
_FILES_8K_RE = re.compile(r"\bFiles 8K\b", re.I)

# {ticker: {articleId: {articleId, time, provider, headline, sentiment}}}
# -- keyed by articleId (not a flat list) so re-fetching an already-seen
# headline within the rolling window on a later pass is a no-op merge,
# never a duplicate. Mutated from this connection's own asyncio event
# loop (news_loop) but, unlike fetch_candlestick_history's file writes,
# also read from the HTTP handler thread (GET /api/news) -- every access
# (read or write) must hold `lock`.
news_by_ticker = {}


def _load_news_file():
    """Seeds news_by_ticker from an existing news.json on startup, so a
    server restart doesn't forget headlines still inside the rolling
    window and re-request them from IB before it has to. Called from
    run_ib_client, which races the HTTP server thread starting up (not
    strictly "before" it any more, since a no_news run has nothing else
    to sequence this against) -- reassignment happens under `lock` so a
    concurrent GET /api/news never observes a half-built news_by_ticker."""
    global news_by_ticker
    try:
        with open(NEWS_FILE) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    seeded = {ticker: {a["articleId"]: a for a in articles} for ticker, articles in raw.items()}
    with lock:
        news_by_ticker = seeded


def _news_snapshot():
    """news_by_ticker (dict-of-dicts, keyed by articleId for cheap merges)
    reshaped into {ticker: [article, ...]}, newest first per ticker -- the
    shape both news.json and GET /api/news serve. Caller must hold `lock`."""
    return {
        ticker: sorted(articles.values(), key=lambda a: a["time"], reverse=True)
        for ticker, articles in news_by_ticker.items()
    }


def _news_sentiment_snapshot():
    """news_by_ticker collapsed to just {ticker: {articleId: score}} --
    everything PeTable.jsx's screener column (averaged per ticker) needs,
    without the headline/time/provider fields news.json already carries.
    A ticker with no scored articles yet (sentiment scoring is best-effort,
    on first sight only -- see news_loop) is left out entirely, same as a
    ticker with no news at all. Caller must hold `lock`."""
    out = {}
    for ticker, articles in news_by_ticker.items():
        scored = {aid: a["sentiment"] for aid, a in articles.items() if "sentiment" in a}
        if scored:
            out[ticker] = scored
    return out


def _prune_and_write_news():
    """Drops anything older than NEWS_WINDOW_DAYS from news_by_ticker and
    writes news.json (newest headline first per ticker) and
    news_sentiment.json (per-article FinBERT scores, for the screener's
    averaged column) -- keeps both files a rolling window instead of
    growing forever."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=NEWS_WINDOW_DAYS)
    with lock:
        for ticker in list(news_by_ticker):
            kept = {}
            for article_id, article in news_by_ticker[ticker].items():
                try:
                    article_time = isoparse(article["time"])
                except (ValueError, TypeError):
                    continue
                if article_time.tzinfo is None:
                    article_time = article_time.replace(tzinfo=timezone.utc)
                if article_time >= cutoff:
                    kept[article_id] = article
            if kept:
                news_by_ticker[ticker] = kept
            else:
                news_by_ticker.pop(ticker, None)
        snapshot = _news_snapshot()
        sentiment_snapshot = _news_sentiment_snapshot()
    with open(NEWS_FILE, "w") as f:
        json.dump(snapshot, f)
    with open(NEWS_SENTIMENT_FILE, "w") as f:
        json.dump(sentiment_snapshot, f)


async def _backfill_news_sentiment():
    """One-shot pass, scheduled once at startup regardless of no_news (see
    run_ib_client), over whatever news_by_ticker was seeded with from disk
    (see _load_news_file) -- cleans and FinBERT-scores any article that
    predates scoring entirely (e.g. news.json written before
    news_sentiment.py existed) or just hasn't been reached yet by
    news_loop's own per-chunk scoring, same treatment news_loop gives a
    newly fetched article. Without this, a no_news run against an
    unscored cache would leave news_sentiment.json permanently empty --
    no_news skips the only thing that would otherwise ever score them."""
    with lock:
        unscored = [a for articles in news_by_ticker.values() for a in articles.values() if "sentiment" not in a]
        for article in unscored:
            article["headline"] = clean_headline(article.get("headline"))
    if unscored:
        print(f"News: backfilling FinBERT sentiment for {len(unscored)} cached article(s) without one...")
        # Same as news_loop's own scoring: CPU-bound, offloaded to a worker
        # thread, and done without `lock` held so it doesn't block ticks,
        # snapshots, or GET /api/news for however long this takes.
        scores = await asyncio.to_thread(score_headlines, [a["headline"] for a in unscored])
        with lock:
            for article, score in zip(unscored, scores):
                article["sentiment"] = score
        print(f"News: backfilled {len(unscored)} article(s)")
    _prune_and_write_news()


async def news_loop():
    """Runs forever as a background asyncio task on the single shared IB
    Gateway connection (see run_ib_client), same pattern as snapshot_loop.

    Cycles over the FULL screener universe (every ranked ticker in
    sorted_screen.csv, not just a top-N slice -- see main.py's
    load_top_tickers) in NEWS_CHUNK_SIZE chunks, so news.json gets
    incremental updates throughout a pass instead of only at the very
    end of what IB's pacing makes a multi-hour cycle. Headlines are
    merged into news_by_ticker keyed by articleId (see
    IBApp.get_news_headlines_async), so re-seeing an already-known
    headline on a later pass is a no-op, not a duplicate. Each new
    article is run through FinBERT (news_sentiment.score_headlines) once,
    on first sight, and gets a `sentiment` field (1 very bearish - 5 very
    bullish); _prune_and_write_news then drops anything older than
    NEWS_WINDOW_DAYS and rewrites news.json after every chunk.

    load_top_tickers is re-read fresh at the start of each full pass (not
    cached once at startup) since sorted_screen.csv's ranking, and the
    set of tickers with a score at all, can change between passes.
    Assumes news_by_ticker is already seeded (see run_ib_client's
    _load_news_file call) -- not done here, so that a no_news run (which
    never starts this loop at all) still seeds it for GET /api/news to
    serve the existing rolling window from disk."""
    while True:
        tickers = load_top_tickers(SORTED_SCREEN_CSV)
        print(f"News: starting a pass over {len(tickers)} screener ticker(s)...")
        for i in range(0, len(tickers), NEWS_CHUNK_SIZE):
            chunk = tickers[i : i + NEWS_CHUNK_SIZE]
            try:
                results = await app.get_news_headlines_async(chunk, days=NEWS_WINDOW_DAYS)
            except Exception as e:
                print(f"News: chunk starting at {i} failed: {e}")
                continue
            # Only ever add articleIds not already in the bucket -- an
            # unconditional overwrite would clobber the `sentiment` score
            # FinBERT already attached below the first time this articleId
            # was seen, since a re-fetched article comes back from IB with
            # no `sentiment` key at all.
            new_articles = []
            with lock:
                for ticker, articles in results.items():
                    if not articles:
                        continue
                    bucket = news_by_ticker.setdefault(ticker, {})
                    for article in articles:
                        article_id = article.get("articleId")
                        if article_id and article_id not in bucket:
                            headline = clean_headline(article.get("headline"))
                            if _FILES_8K_RE.search(headline):
                                continue
                            article["headline"] = headline
                            bucket[article_id] = article
                            new_articles.append(article)
            if new_articles:
                # FinBERT inference is CPU-bound (see news_sentiment.py) --
                # offloaded to a worker thread (and run without `lock` held,
                # unlike the merge above) so it doesn't stall this
                # connection's shared event loop, or block GET /api/news,
                # for however long a batch of ~40 tickers' worth of
                # headlines takes.
                scores = await asyncio.to_thread(score_headlines, [a["headline"] for a in new_articles])
                with lock:
                    for article, score in zip(new_articles, scores):
                        article["sentiment"] = score
            _prune_and_write_news()
            print(f"News: {min(i + NEWS_CHUNK_SIZE, len(tickers))}/{len(tickers)} done, news.json updated")
        print(f"News: pass complete, sleeping {NEWS_LOOP_PASS_DELAY_SECONDS}s before the next one")
        await asyncio.sleep(NEWS_LOOP_PASS_DELAY_SECONDS)


# ---------------------------------------------------------------------- #
#  Account performance (Flex Query)                                      #
# ---------------------------------------------------------------------- #
# Real day-by-day cash/NAV/realized/unrealized -- accounting for actual
# trades, dividends, fees, deposits/withdrawals -- isn't obtainable over
# the TWS-API socket this file otherwise uses (reqAccountSummary/reqPnL
# are live-only, no historical call exists), and there's no Client Portal
# Gateway REST server running locally to ask instead (see the module
# docstring). IBKR's Flex Query web service is the one place this data
# actually lives: IBApp.fetch_flex_query hits it directly over the open
# internet, so none of the local-port-4001 TLS trouble applies here.
#
# This project's query has returned both XML and CSV in practice -- the
# query's own Report Format is an IBKR Account Management setting, not
# something this code controls, and it's already changed once mid-project
# (Results.csv, then Results.xml, both kept in the repo root as reference
# samples of the exact shape each format takes). _parse_portfolio_report
# below sniffs the response's first byte to dispatch between them.
#
# Either way, IBKR concatenates one full copy of every configured section
# per calendar day when a query spans a multi-day range with daily-
# frequency sections -- in XML that's one <FlexStatement> per day (see
# Results.xml), each with its own <EquitySummaryInBase> and <ChangeInNAV>;
# in CSV it's the same repeated-header-block shape _flex_csv_sections
# already handles. Only two sections matter here either way.


def _iso_date(yyyymmdd):
    return f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def _blank_day(d):
    return {
        "date": d, "cash": None, "nav": None, "mtm": None, "realized": None, "unrealized": None,
        "stockLong": None, "stockShort": None, "stockNet": None, "stockGross": None,
        "depositsWithdrawals": None, "commissions": None, "dividends": None, "interest": None,
    }


def _apply_unrealized_from_nav(rows):
    """Overwrites each row's `unrealized` with (today's NAV - yesterday's
    NAV) - today's realized - today's depositsWithdrawals - today's
    commissions - today's dividends - today's interest: the standard
    decomposition of a day's total NAV move into its components (total
    change = realized + change in unrealized + cash flows + fees +
    income), solved for the unrealized side. Deposits/withdrawals move
    NAV without being any kind of trading P&L at all, commissions are a
    real cost but not a market-driven mark on a position, and dividends
    and interest are cash income rather than a price move on the
    position/balance generating them, so all four need backing out for
    `unrealized` to mean "P&L from price movement on what's still open"
    -- without them, e.g. a withdrawal would show up as a paper loss that
    never happened, or a dividend or interest payment as a paper gain.

    Needed in the first place because ChangeInNAV's own changeInUnrealized
    attribute is unpopulated (always 0) for this account, and summing
    FIFOPerformanceSummaryUnderlying's totalUnrealizedPnl per day gives a
    level (the mark on positions still open that day), not this day's own
    change -- differencing that level day over day would get corrupted by
    positions simply entering or leaving the set as they open and close.
    Deriving it from NAV instead sidesteps that: NAV already nets every
    position regardless of which ones are open on a given day. The first
    row has no prior NAV to diff against, so its unrealized stays None."""
    prev_nav = None
    for row in rows:  # rows is already date-ascending
        if (
            prev_nav is not None
            and row["nav"] is not None
            and row["realized"] is not None
            and row["depositsWithdrawals"] is not None
            and row["commissions"] is not None
            and row["dividends"] is not None
            and row["interest"] is not None
        ):
            row["unrealized"] = (
                (row["nav"] - prev_nav)
                - row["realized"]
                - row["depositsWithdrawals"]
                - row["commissions"]
                - row["dividends"]
                - row["interest"]
            )
        else:
            row["unrealized"] = None
        if row["nav"] is not None:
            prev_nav = row["nav"]
    return rows

EQUITY_SUMMARY_HEADER = (
    "ClientAccountID", "ReportDate", "Cash", "CashLong", "CashShort",
    "Stock", "StockLong", "StockShort", "Options", "OptionsLong", "OptionsShort",
    "Bonds", "BondsLong", "BondsShort", "Funds", "FundsLong", "FundsShort",
    "Total", "TotalLong", "TotalShort", "CurrencyPrimary",
)
CHANGE_IN_NAV_HEADER = (
    "ClientAccountID", "FromDate", "ToDate", "StartingValue", "EndingValue",
    "Mtm", "Realized", "ChangeInUnrealized", "DepositsWithdrawals",
    "Commissions", "Dividends", "Interest", "CurrencyPrimary",
)


def _flex_csv_sections(text):
    """Splits a multi-section Flex Query CSV export into {header_tuple:
    [rows]}. A header row is any row whose first column is literally the
    string "ClientAccountID" -- every section's header starts with it, and
    it's never a real value there (real rows carry an actual account ID
    like "U12043450"), so it's an unambiguous marker regardless of how
    many times a section repeats (once per day, here) or how many
    distinct sections the query has configured. Rows for the same header
    accumulate across every occurrence, so a section repeated once per
    day naturally merges into one continuous list."""
    sections = {}
    current = None
    for row in csv.reader(text.splitlines()):
        if not row:
            continue
        if row[0] == "ClientAccountID":
            current = tuple(row)
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(row)
    return sections


def _parse_portfolio_csv(text):
    """Parses the two sections that matter for the Portfolio tab out of a
    Flex Query CSV export, joined by date into one row per day: {date
    (ISO 8601), cash, nav, mtm, realized, unrealized, stockLong,
    stockShort, stockNet, stockGross}. cash/nav/stockLong/stockShort come
    from "Equity Summary by Report Date in Base Currency" (nav = its
    Total column, already net long+short -- stockNet is that same figure
    restated from the two side columns directly, stockGross is their
    combined magnitude); mtm/realized come from "Change in NAV" (its own
    unrealized/changeInUnrealized column is NOT used -- see
    _apply_unrealized_from_nav for why). The Equity Summary section
    carries one extra leading date (the day before the range starts,
    IBKR's opening balance) with no matching Change in NAV row; it's
    kept, just with those other fields left None, since it's still a
    real cash/NAV data point."""
    sections = _flex_csv_sections(text)
    equity_rows = sections.get(EQUITY_SUMMARY_HEADER, [])
    nav_rows = sections.get(CHANGE_IN_NAV_HEADER, [])
    if not equity_rows and not nav_rows:
        return []

    by_date = {}
    for row in equity_rows:
        d = _iso_date(row[1])
        by_date.setdefault(d, _blank_day(d))
        by_date[d]["cash"] = float(row[2])
        by_date[d]["nav"] = float(row[17])
        stock_long, stock_short = float(row[6]), float(row[7])
        by_date[d]["stockLong"] = stock_long
        by_date[d]["stockShort"] = stock_short
        by_date[d]["stockNet"] = stock_long + stock_short
        by_date[d]["stockGross"] = abs(stock_long) + abs(stock_short)

    for row in nav_rows:
        d = _iso_date(row[2])
        by_date.setdefault(d, _blank_day(d))
        by_date[d]["mtm"] = float(row[5])
        by_date[d]["realized"] = float(row[6])
        by_date[d]["depositsWithdrawals"] = float(row[8])
        by_date[d]["commissions"] = float(row[9])
        by_date[d]["dividends"] = float(row[10])
        by_date[d]["interest"] = float(row[11])

    rows = sorted(by_date.values(), key=lambda r: r["date"])
    return _apply_unrealized_from_nav(rows)


def _parse_portfolio_xml(text):
    """Parses a Flex Query XML export into one row per day: {date, cash,
    nav, mtm, realized, unrealized, stockLong, stockShort, stockNet,
    stockGross}.

    cash/nav/stockLong/stockShort come from EquitySummaryByReportDateInBase
    (nav = its `total`, already net long+short -- stockNet restates that
    from the two side values directly, stockGross is their combined
    magnitude), searched document-wide with root.iter() since it carries
    its own date (reportDate) regardless of which <FlexStatement> it's
    nested under. ChangeInNAV, FIFOPerformanceSummaryUnderlying, and
    CashReportCurrency are all instead read per-<FlexStatement> (via its
    own toDate) rather than trusting an own toDate attribute on the child
    element itself -- confirmed some query configurations (e.g. this
    account's "NAVs" query) omit that attribute on ChangeInNAV even
    though it's always present and identical on the enclosing
    <FlexStatement>, which every <FlexStatement> here is itself scoped to
    a single day for (fromDate == toDate) anyway.

    realized does NOT come from ChangeInNAV's own realized attribute --
    confirmed against a real export that it's unpopulated (always 0) for
    this account, even on days with obvious trading activity. The real
    number lives in each day's own FIFOPerformanceSummaryUnderlying rows
    instead: totalRealizedPnl, summed across a statement's symbols,
    behaves like a real daily flow (confirmed: it resets to 0 or swings
    both directions day to day, which a cumulative-to-date total could
    never do). unrealized is derived separately -- see
    _apply_unrealized_from_nav."""
    root = ET.fromstring(text)
    by_date = {}
    for el in root.iter("EquitySummaryByReportDateInBase"):
        report_date = el.get("reportDate")
        if not report_date:
            continue
        d = _iso_date(report_date)
        by_date.setdefault(d, _blank_day(d))
        by_date[d]["cash"] = float(el.get("cash"))
        by_date[d]["nav"] = float(el.get("total"))
        # A query configured without the Long/Short Stock columns omits
        # these attributes entirely rather than reporting them as 0 --
        # `or 0` would silently turn "not in this query" into a real
        # (wrong) zero, which then corrupts a merge with a fuller fetch
        # that DOES have the real figure (see _merge_portfolio_rows: a
        # field is only overlaid when the new source has a non-None
        # value). Leave stockLong/stockShort/stockNet/stockGross alone
        # entirely when genuinely absent, instead of defaulting to 0.
        if el.get("stockLong") is not None or el.get("stockShort") is not None:
            stock_long = float(el.get("stockLong") or 0)
            stock_short = float(el.get("stockShort") or 0)
            by_date[d]["stockLong"] = stock_long
            by_date[d]["stockShort"] = stock_short
            by_date[d]["stockNet"] = stock_long + stock_short
            by_date[d]["stockGross"] = abs(stock_long) + abs(stock_short)

    for stmt in root.iter("FlexStatement"):
        to_date = stmt.get("toDate")
        if not to_date:
            continue
        d = _iso_date(to_date)

        change_in_nav = stmt.find("ChangeInNAV")
        if change_in_nav is not None:
            by_date.setdefault(d, _blank_day(d))
            by_date[d]["mtm"] = float(change_in_nav.get("mtm") or 0)
            by_date[d]["depositsWithdrawals"] = float(change_in_nav.get("depositsWithdrawals") or 0)
            by_date[d]["commissions"] = float(change_in_nav.get("commissions") or 0)
            by_date[d]["dividends"] = float(change_in_nav.get("dividends") or 0)
            by_date[d]["interest"] = float(change_in_nav.get("interest") or 0)

        fifo_rows = list(stmt.iter("FIFOPerformanceSummaryUnderlying"))
        if fifo_rows:
            by_date.setdefault(d, _blank_day(d))
            by_date[d]["realized"] = sum(float(el.get("totalRealizedPnl") or 0) for el in fifo_rows)

        # CashReportCurrency: only present in a query configured without
        # ChangeInNAV -- one row per currency sub-bucket the account
        # touches, then a final base-currency aggregate row last.
        # Confirmed against this same account's ChangeInNAV for an
        # overlapping date: the last row's commissions/dividends match
        # exactly. Only fills what ChangeInNAV hasn't already supplied, so
        # a fuller fetch's data (which also has mtm/interest, which
        # CashReportCurrency doesn't carry) always wins.
        if by_date.get(d, {}).get("commissions") is None:
            cash_rows = list(stmt.iter("CashReportCurrency"))
            if cash_rows:
                aggregate = cash_rows[-1]
                by_date.setdefault(d, _blank_day(d))
                by_date[d]["commissions"] = float(aggregate.get("commissions") or 0)
                by_date[d]["dividends"] = float(aggregate.get("dividends") or 0)
                by_date[d]["depositsWithdrawals"] = float(aggregate.get("deposits") or 0) + float(
                    aggregate.get("withdrawals") or 0
                )

    rows = sorted(by_date.values(), key=lambda r: r["date"])
    return _apply_unrealized_from_nav(rows)


def _parse_portfolio_report(raw_bytes):
    """Dispatches to the XML or CSV parser based on the response's first
    non-whitespace character ('<' for XML, anything else for CSV) — see
    the module-level comment above for why this account's query has
    needed both."""
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    if text.lstrip().startswith("<"):
        return _parse_portfolio_xml(text)
    return _parse_portfolio_csv(text)


# Manually re-exported from IBKR Account Management (Reports > Flex
# Queries > Run), same shape the live Flex Query API returns -- checked as
# a fallback when the API itself fails (see fetch_account_performance).
# NAVs.xml (kept under data/, like every other downloader JSON/export --
# see main.py's DATA_DIR) is this account's second, narrower query
# (Equity Summary + Change in NAV + FIFO, no options/bonds/funds
# breakdown -- see _parse_portfolio_xml); whichever file is actually
# newest wins, not whichever is listed first. Results.xml/Results.csv
# stay at the root as historical reference samples of the two report
# formats this account's query has actually returned (see the comment
# above _iso_date) -- neither currently exists on disk; this tuple just
# keeps both formats recognized if one is ever re-exported again.
# Gitignored: these are real account exports.
LOCAL_FLEX_EXPORT_FILES = ("Results.xml", "Results.csv", os.path.join(DATA_DIR, "NAVs.xml"))


def _local_flex_export_fallback():
    """Finds the most recently modified file among LOCAL_FLEX_EXPORT_FILES
    that's newer than PORTFOLIO_PERFORMANCE_FILE's current mtime -- i.e.
    actually fresher data, not just re-parsing whatever export already
    produced the file on disk. Returns (path, rows), or (None, None) if no
    candidate file exists, none is fresher than the current output, or the
    freshest one fails to parse."""
    candidates = [f for f in LOCAL_FLEX_EXPORT_FILES if os.path.exists(f)]
    if not candidates:
        return None, None
    newest = max(candidates, key=os.path.getmtime)

    try:
        current_mtime = os.path.getmtime(PORTFOLIO_PERFORMANCE_FILE)
    except FileNotFoundError:
        current_mtime = 0
    if os.path.getmtime(newest) <= current_mtime:
        return None, None

    with open(newest, "rb") as f:
        raw = f.read()
    rows = _parse_portfolio_report(raw)
    return (newest, rows) if rows else (None, None)


def _merge_portfolio_rows(existing, new_rows):
    """Merges a freshly parsed set of rows into the existing set, by date,
    field by field -- a date only in `existing` (e.g. the current fetch's
    source is a narrower query, like "NAVs", that doesn't cover it) is
    kept as-is, not dropped; a date in both gets each field upgraded from
    `new_rows` only where the new source actually has a non-None value
    for it, so a narrower fetch can freshen cash/NAV/commissions without
    blanking out realized/stockLong/etc. an earlier fuller fetch already
    established; a date only in `new_rows` is added as-is. Returns a
    date-ascending list -- unrealized isn't recomputed here, since which
    fields actually changed can shift what's derivable; call
    _apply_unrealized_from_nav on the result."""
    by_date = {r["date"]: dict(r) for r in existing}
    for row in new_rows:
        d = row["date"]
        if d not in by_date:
            by_date[d] = dict(row)
            continue
        for field, value in row.items():
            if field != "date" and value is not None:
                by_date[d][field] = value
    return sorted(by_date.values(), key=lambda r: r["date"])


def fetch_account_performance(start_date=None):
    """Fetches real, IB-computed daily cash/NAV/realized/unrealized via a
    Flex Query and writes PORTFOLIO_PERFORMANCE_FILE as
    {"kind": "daily", "rows": [...]}. Requires QUERY_TOKEN and QUERY_ID in
    the environment (see .env) -- a token from IBKR Account Management's
    Flex Web Service Configuration, and the query ID of a Flex Query with
    "Equity Summary by Report Date in Base Currency", "Change in NAV", and
    "Realized and Unrealized Performance Summary" (the FIFO one, not
    MTD/YTD) all configured at Daily frequency over the date range you
    want (the range itself lives in the query definition on IBKR's side,
    not in this call -- start_date here only trims rows client-side, it
    doesn't ask IBKR for a different range; see _parse_portfolio_xml for
    why realized/unrealized come from the FIFO section, not Change in
    NAV's own same-named fields). A narrower query -- e.g. one configured
    with just Equity Summary + Cash Report, no Change in NAV/FIFO, like
    this account's "NAVs" query -- still works, just without
    realized/stockLong/etc. for the dates it covers; see
    _parse_portfolio_xml's CashReportCurrency handling. start_date is ISO
    8601 (e.g. "2026-07-01"); omit it (or pass None) to fall back to
    PORTFOLIO_START_DATE.

    The live API has proven flaky in practice (IBKR's error 1001,
    "Statement could not be generated at this time", sometimes for a
    while) -- rather than failing outright, this falls back to the
    freshest local Results.xml/Results.csv export (see
    _local_flex_export_fallback) if one is newer than the current
    PORTFOLIO_PERFORMANCE_FILE. Without that fallback, a failed run
    silently leaves the old (possibly days-stale) output file in place
    with no visible error in the UI.

    Whatever this run's source produces is then merged into the existing
    PORTFOLIO_PERFORMANCE_FILE (see _merge_portfolio_rows) rather than
    replacing it outright -- a date or field the new source doesn't cover
    is kept from what's already on file, not lost."""
    token = os.getenv("QUERY_TOKEN")
    query_id = os.getenv("QUERY_ID")

    rows = None
    source = None
    if token and query_id:
        print("Requesting Flex Query statement (IBKR generates it on demand, can take a while for a multi-day query)...")
        raw = IBApp().fetch_flex_query(token, query_id)
        if raw:
            rows = _parse_portfolio_report(raw)
        if rows:
            source = "live Flex Query"
        else:
            print("Live Flex Query fetch failed or returned no usable sections; checking for a fresher local export...")
    else:
        print("QUERY_TOKEN/QUERY_ID not set in .env; checking for a local export instead...")

    if not rows:
        path, fallback_rows = _local_flex_export_fallback()
        if fallback_rows:
            rows = fallback_rows
            source = path
            print(f"Using {path} (newer than the current {PORTFOLIO_PERFORMANCE_FILE})")

    if not rows:
        sys.exit(
            "Flex Query fetch failed and no fresher local Results.xml/Results.csv export was found -- "
            "check QUERY_TOKEN/QUERY_ID and that the query is active, or re-export Results.xml from IBKR "
            "Account Management (Reports > Flex Queries > Run) and try again"
        )

    try:
        with open(PORTFOLIO_PERFORMANCE_FILE) as f:
            existing_rows = json.load(f).get("rows", [])
    except FileNotFoundError:
        existing_rows = []
    merged_count = len(rows)
    rows = _merge_portfolio_rows(existing_rows, rows)
    rows = _apply_unrealized_from_nav(rows)
    if len(rows) != merged_count:
        print(f"Merged with {PORTFOLIO_PERFORMANCE_FILE}: {len(rows)} day(s) total ({merged_count} from this fetch)")

    start_date = start_date or PORTFOLIO_START_DATE
    rows = [r for r in rows if r["date"] >= start_date]

    output = {"kind": "daily", "rows": rows}
    with open(PORTFOLIO_PERFORMANCE_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Wrote {PORTFOLIO_PERFORMANCE_FILE} (source: {source}): {len(rows)} day(s), {rows[0]['date']} to {rows[-1]['date']}")
    return output


PERFORMANCE_REFRESH_SECONDS = 6 * 3600  # 6 hours -- daily-granularity NAV data doesn't need finer than this, and the Flex Query API is slow/flaky (see fetch_account_performance's own docstring), so a patient interval beats a tight one.


async def performance_loop():
    """Runs forever as a background asyncio task on the single shared IB
    Gateway connection (see run_ib_client) -- keeps
    PORTFOLIO_PERFORMANCE_FILE (the Portfolio tab's NAV history) refreshed
    automatically for as long as this server is running, instead of only
    ever updating via the separate one-shot `python ib_server.py
    performance` command (previously the ONLY way this file got updated --
    there was no loop calling fetch_account_performance at all, which is
    why the Portfolio tab could silently sit days stale with nobody
    noticing). Refreshes immediately on startup (so a fresh server start
    backfills whatever trading days piled up since the last run), then
    every PERFORMANCE_REFRESH_SECONDS.

    fetch_account_performance is a blocking synchronous HTTP round-trip
    (IBKR's Flex Web Service has no async client, and can take
    10-100+ seconds polling for the statement to finish generating -- see
    its own docstring) -- run via asyncio.to_thread so it never stalls
    this process's live price/position streaming the way calling it
    directly on this shared event loop would.

    fetch_account_performance also calls sys.exit() on a hard failure (no
    live fetch AND no local export fallback) -- correct for the one-shot
    CLI mode, where killing the process on failure is fine, but fatal here:
    called from this loop unguarded, one flaky Flex Query response would
    take down the ENTIRE server, live price streaming included. Caught
    explicitly (SystemExit isn't an Exception subclass, so a bare `except
    Exception` wouldn't catch it) and logged instead, so this loop just
    retries next cycle."""
    while True:
        try:
            await asyncio.to_thread(fetch_account_performance)
        except SystemExit as e:
            print(f"performance_loop: fetch_account_performance exited without writing (code {e.code}) -- will retry next cycle")
        except Exception as e:
            print(f"performance_loop: fetch_account_performance failed: {e}")
        await asyncio.sleep(PERFORMANCE_REFRESH_SECONDS)


# ---------------------------------------------------------------------- #
#  Past trades (Flex Query)                                              #
# ---------------------------------------------------------------------- #
# Same Flex Web Service the Portfolio tab's NAV history already uses (see
# fetch_account_performance's own docstring for why: reqExecutions over
# the TWS-API socket only reliably surfaces TODAY's fills, not real
# history -- see IBApp.get_past_trades). A separate Flex Query
# (QUERY_TRADES_ID, same QUERY_TOKEN as the NAV query -- one Flex Web
# Service token authorizes every query configured under it) with a "Trade
# Confirmation" or "Trades" section configured, over whatever date range
# that query itself is set to on IBKR's side (same as the NAV query, the
# range lives in the query definition, not in this request).
#
# Unlike the NAV query, this account's Trades query hasn't been run live
# yet as of this writing, so the exact column set is unconfirmed -- IBKR's
# XML export uses fixed attribute names (symbol, tradeDate, buySell,
# quantity, tradePrice, ...) but its CSV export's column headers are
# whatever the query's own Report Format configuration picked (commonly
# more human-readable, e.g. "T. Price" instead of "tradePrice") and can
# differ from account to account. _normalize_trade_fields below is
# deliberately alias-tolerant (tries several known spellings per logical
# field, case-insensitively) rather than hardcoded to one naming
# convention, and always keeps the complete original field set under
# "raw" -- so a real trade is never silently dropped just because this
# account's query happens to use a column name that isn't in the alias
# list, and the actual raw values are there to check by hand.


def _first(fields, *keys):
    """Case-insensitive lookup trying multiple candidate key spellings in
    order, returning the first present non-empty value (or None) -- see
    the module comment above for why: IBKR's Trades CSV export's column
    headers aren't as fixed as the XML attribute names are."""
    lower = {k.lower(): v for k, v in fields.items()}
    for key in keys:
        v = lower.get(key.lower())
        if v not in (None, ""):
            return v
    return None


def _trade_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize_trade_fields(fields):
    """One raw {field name: string value} dict (an XML <Trade> element's
    .attrib, or a CSV Trades-section row zipped against its own header) --
    normalized into the shape TradesView renders, plus the untouched
    original under "raw" (see module comment above)."""
    trade_date = _first(fields, "tradeDate", "Date/Time", "TradeDate", "Date")
    date = None
    if trade_date:
        digits = "".join(ch for ch in str(trade_date) if ch.isdigit())
        if len(digits) >= 8:
            date = _iso_date(digits[:8])
    return {
        "tradeID": _first(fields, "tradeID", "TradeID"),
        "symbol": _first(fields, "symbol", "Symbol"),
        "assetCategory": _first(fields, "assetCategory", "AssetClass", "Asset Category"),
        "currency": _first(fields, "currency", "Currency"),
        "date": date,
        "buySell": _first(fields, "buySell", "Buy/Sell"),
        "quantity": _trade_float(_first(fields, "quantity", "Quantity")),
        "price": _trade_float(_first(fields, "tradePrice", "T. Price", "TradePrice")),
        "proceeds": _trade_float(_first(fields, "proceeds", "Proceeds")),
        "commission": _trade_float(_first(fields, "ibCommission", "Comm/Fee", "IBCommission", "Commission")),
        "netCash": _trade_float(_first(fields, "netCash", "Net Cash", "NetCash")),
        "realizedPnl": _trade_float(_first(fields, "fifoPnlRealized", "Realized P/L", "RealizedPL", "MTM P/L")),
        "openClose": _first(fields, "openCloseIndicator", "Open/Close", "Code"),
        "orderType": _first(fields, "orderType", "OrderType"),
        "exchange": _first(fields, "exchange", "Exchange"),
        "raw": fields,
    }


def _parse_trades_xml(text):
    root = ET.fromstring(text)
    return [_normalize_trade_fields(el.attrib) for el in root.iter("Trade")]


def _parse_trades_csv(text):
    """Trades section(s) out of a Flex Query CSV export -- reuses
    _flex_csv_sections (already generic, not portfolio-specific: any
    section whose rows start with a repeated header block splits out on
    its own). Picks whichever section(s) look like trade rows (a Symbol
    column plus a Quantity or T. Price column) rather than assuming
    there's exactly one section, since a query can have more than one
    trade-shaped section (e.g. Trades and Trade Confirmation both
    enabled)."""
    sections = _flex_csv_sections(text)
    trades = []
    for header, rows in sections.items():
        lower = {h.lower() for h in header}
        if "symbol" not in lower:
            continue
        if not ({"quantity", "t. price", "tradeprice"} & lower):
            continue
        for row in rows:
            fields = dict(zip(header, row))
            trades.append(_normalize_trade_fields(fields))
    return trades


def _parse_trades_report(raw_bytes):
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    if text.lstrip().startswith("<"):
        return _parse_trades_xml(text)
    return _parse_trades_csv(text)


def fetch_trades_report(start_date=None):
    """Fetches this account's real trade history via a Flex Query and
    writes TRADES_FILE as {"kind": "trades", "rows": [...]}. Requires
    QUERY_TOKEN and QUERY_TRADES_ID in the environment (see .env) -- the
    same Flex Web Service token the NAV query already uses (see
    fetch_account_performance), and the query ID of a separate Flex Query
    with a Trades/Trade Confirmation section configured (the date range
    lives in that query's own definition on IBKR's side, same as the NAV
    query -- start_date here only trims rows client-side afterward).

    Merges into whatever's already in TRADES_FILE by tradeID (a trade,
    once executed, never changes -- unlike the NAV query's per-day rows,
    there's no "freshen this row's fields" case to handle, just "add
    whatever's new"), so a query whose configured range doesn't cover the
    account's full history still accumulates a growing local record
    across repeated runs rather than only ever showing its own window.
    A row with no tradeID (shouldn't happen, but _normalize_trade_fields
    doesn't assume the account's query configuration always includes it)
    is kept unconditionally rather than risked being deduped away."""
    token = os.getenv("QUERY_TOKEN")
    query_id = os.getenv("QUERY_TRADES_ID")
    if not (token and query_id):
        sys.exit("QUERY_TOKEN/QUERY_TRADES_ID not set in .env -- see IBKR Account Management's Flex Web Service Configuration")

    print("Requesting Trades Flex Query statement (IBKR generates it on demand, can take a while)...")
    raw = IBApp().fetch_flex_query(token, query_id)
    if not raw:
        sys.exit("Flex Query fetch failed -- check QUERY_TOKEN/QUERY_TRADES_ID and that the query is active")
    trades = _parse_trades_report(raw)
    if not trades:
        sys.exit("Trades Flex Query returned no rows -- check the query has a Trades/Trade Confirmation section configured")

    try:
        with open(TRADES_FILE) as f:
            existing = json.load(f).get("rows", [])
    except FileNotFoundError:
        existing = []
    by_id = {r["tradeID"]: r for r in existing if r.get("tradeID")}
    no_id = [r for r in existing if not r.get("tradeID")]
    for t in trades:
        if t.get("tradeID"):
            by_id[t["tradeID"]] = t
        else:
            no_id.append(t)
    rows = list(by_id.values()) + no_id
    rows.sort(key=lambda r: (r.get("date") or "", r.get("tradeID") or ""))

    if start_date:
        rows = [r for r in rows if (r.get("date") or "") >= start_date]

    output = {"kind": "trades", "rows": rows}
    with open(TRADES_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Wrote {TRADES_FILE}: {len(rows)} trade(s)" + (f", {rows[0]['date']} to {rows[-1]['date']}" if rows else ""))
    return output


def _interleave(a, b):
    """[a0, b0, a1, b1, ...], trailing off into whichever list is longer
    once the other runs out -- used by _priority_tickers below to blend two
    same-size-ish priority buckets fairly instead of exhausting one before
    the other ever gets a slot."""
    out = []
    for x, y in zip(a, b):
        out.append(x)
        out.append(y)
    out.extend(a[len(b):])
    out.extend(b[len(a):])
    return out


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Recommendations tab entry-gate thresholds -- MUST match
# RecommendationsView.tsx's own MOMENTUM_THRESHOLD/REVENUE_GROWTH_THRESHOLD/
# MEAN_REVERSION_THRESHOLD/MAX_SHORT_INTEREST exactly (kept in sync by hand,
# same "duplicated, not shared" convention this project already uses for
# previousClose across three frontend files -- see _passes_long_gates/
# _passes_short_gates below for why this needed a Python copy at all).
_REC_MOMENTUM_THRESHOLD = 0.5
_REC_REVENUE_GROWTH_THRESHOLD = 0.1
_REC_MEAN_REVERSION_THRESHOLD = 10
_REC_MAX_SHORT_INTEREST = 0.1


def _rec_eps_trend(row):
    vals = [v for v in (_to_float(row.get("epsRevision0y")), _to_float(row.get("epsRevision1y"))) if v is not None]
    return sum(vals) / len(vals) if vals else None


def _passes_long_gates(row):
    """Mirrors RecommendationsView.tsx's eligibleToBuy +
    sufficientGrowthForLong + meanReversionOkForLong + epsTrendOkForLong --
    the exact set of checks a Buy/Strong Buy candidate must clear to
    appear in the Long list. See _priority_tickers' own docstring for why
    this needed replicating in Python at all: without it, this file has no
    way to tell "will actually show up on the Recommendations page" apart
    from "is RATED_FOR_EXTRAS," and the Long/Short lists are a much
    smaller, gated subset of that."""
    momentum = _to_float(row.get("momentum"))
    if momentum is None or momentum < _REC_MOMENTUM_THRESHOLD:
        return False
    growth = _to_float(row.get("revenueGrowth"))
    if growth is not None and growth < _REC_REVENUE_GROWTH_THRESHOLD:
        return False
    mr = _to_float(row.get("meanReversion"))
    if mr is not None and mr >= _REC_MEAN_REVERSION_THRESHOLD:
        return False
    eps = _rec_eps_trend(row)
    if eps is not None and eps < 0:
        return False
    return True


def _passes_short_gates(row):
    """Mirrors RecommendationsView.tsx's eligibleToSell + notCrowded +
    notTooMuchGrowthForShort + meanReversionOkForShort +
    epsTrendOkForShort -- the Short list's own gate set."""
    momentum = _to_float(row.get("momentum"))
    if momentum is None or momentum >= 0:
        return False
    short_pct = _to_float(row.get("shortPercentOfFloat"))
    if short_pct is not None and short_pct > _REC_MAX_SHORT_INTEREST:
        return False
    growth = _to_float(row.get("revenueGrowth"))
    if growth is not None and growth > _REC_REVENUE_GROWTH_THRESHOLD:
        return False
    mr = _to_float(row.get("meanReversion"))
    if mr is not None and mr <= -_REC_MEAN_REVERSION_THRESHOLD:
        return False
    eps = _rec_eps_trend(row)
    if eps is not None and eps > 0:
        return False
    return True


def _priority_tickers():
    """RATED_FOR_EXTRAS tickers (Strong Buy/Buy/Sell/Strong Sell) from
    sorted_screen.csv, reordered so whatever would ACTUALLY appear on the
    Recommendations tab goes first -- explicit instruction: prices/price
    changes for every recommendation shown, as a priority, over tickers
    that are merely RATED_FOR_EXTRAS but don't actually surface anywhere
    on that page. Three tiers, in order:

      1. page_tickers -- candidates that clear the Long/Short entry gates
         (_passes_long_gates/_passes_short_gates) and so actually render
         as a card in Long or Short, interleaved best/worst-score-first
         the same way those two lists sort themselves. This used to be
         nothing but "every Strong Buy/Strong Sell, regardless of
         gates" -- which meant the scarce 99-line live-stream budget (and
         the front of a 20-minute snapshot sweep) went to Strong-tier
         names that FAILED a gate and never even render on the page,
         while a plain Buy/Sell that cleared every gate and IS sitting on
         the page as an actionable idea could go the entire sweep with
         no live price at all.
      2. Every remaining Strong Buy/Strong Sell not already in tier 1 --
         still worth a price even though they failed a gate, since they
         render in the "Strong Buy/Strong Sell -- blocked" audit section.
      3. Everything else RATED_FOR_EXTRAS covers (plain Buy/Sell that
         failed a gate) -- doesn't render on the Recommendations page at
         all, lowest priority.

    Both stream_prices_and_positions' live-budget fill (see main()) and
    snapshot_loop's periodic sweep order consult this, so the scarce
    99-line live budget, and whichever ticker gets fetched EARLIEST in a
    20-minute snapshot cycle, both go to what the Recommendations tab is
    actually showing right now."""
    try:
        with open(SORTED_SCREEN_CSV, newline="") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return []

    def tickers_for(rating):
        return [r["ticker"] for r in rows if r.get("rating") == rating]

    def by_score(rows_subset, worst_first):
        return [
            r["ticker"]
            for r in sorted(rows_subset, key=lambda r: _to_float(r.get("score")) or 0, reverse=not worst_first)
        ]

    long_ok = by_score([r for r in rows if r.get("rating") in ("Strong Buy", "Buy") and _passes_long_gates(r)], worst_first=False)
    short_ok = by_score([r for r in rows if r.get("rating") in ("Strong Sell", "Sell") and _passes_short_gates(r)], worst_first=True)
    page_tickers = _interleave(long_ok, short_ok)

    # Sell/Strong Sell reversed (worst score first) -- the most extreme,
    # most-actionable end of each bucket goes first, same "worst first"
    # convention the Recommendations tab's own Short list already uses.
    strong = _interleave(tickers_for("Strong Buy"), list(reversed(tickers_for("Strong Sell"))))
    rest = _interleave(tickers_for("Buy"), list(reversed(tickers_for("Sell"))))

    seen = set(page_tickers)
    remaining_strong = [t for t in strong if t not in seen]
    seen.update(remaining_strong)
    remaining_rest = [t for t in rest if t not in seen]
    return page_tickers + remaining_strong + remaining_rest


async def stream_prices_and_positions(ranked_tickers):
    """Opens one streaming market-data subscription batch on the already-
    connected shared `app` (see run_ib_client, clientId 0) — every
    currently held position first (never truncated: a P&L blind spot on
    your own holdings is worse than bumping into IBKR's market-data-line
    budget), then the highest-ranked remaining tickers from
    `ranked_tickers` up to MAX_STREAMED_SYMBOLS total (ranked_tickers is
    already RATED_FOR_EXTRAS-filtered by the caller -- see main() -- so a
    Hold-rated ticker never competes for one of these slots regardless of
    its raw rank position) — and subscribes to account position updates.
    Live ticks/position updates keep arriving afterward purely through
    the event handlers registered below, driven by run_ib_client's
    app.ib.run(); this coroutine itself finishes once setup is done."""
    # IBApp's own tick handler prints a line per tick, meant for a single
    # symbol in a foreground script; too noisy across MAX_STREAMED_SYMBOLS
    # streaming symbols here, so we swap in our own instead.
    app.ib.pendingTickersEvent -= app.on_pending_tickers
    app.ib.pendingTickersEvent += on_pending_tickers
    app.ib.positionEvent += on_position
    app.ib.pnlSingleEvent += on_pnl_single
    app.ib.accountValueEvent += on_account_value
    # Alongside IBApp's own on_error (still just logs) — this one retries
    # the specific race that leaves a held position with no live price
    # for the rest of the process (see on_market_data_error).
    app.ib.errorEvent += on_market_data_error

    # No explicit reqAccountUpdates() call needed: ib_insync's own
    # reqAccountUpdates docstring says connect()'s sync step already does
    # this at startup. (Its signature is also just `account: str = ""` in
    # this ib_insync version, not `(subscribe, account)` — an earlier
    # version of this call passed both and crashed with a TypeError.)
    # That sync step's own initial AccountValue burst fires BEFORE the
    # accountValueEvent += on_account_value line above ever runs, though
    # -- app.connect() (called before this coroutine even starts)
    # already completed it -- so on_account_value never saw those first
    # values and account_status started out missing every tag until each
    # one happened to change again on its own. Confirmed live: only
    # NetLiquidation (recalculated on every price tick, so it always
    # fires a fresh event soon) ever showed up on /api/account; slower-
    # moving tags like AvailableFunds/BuyingPower/ExcessLiquidity could
    # sit unpopulated for the life of the process. ib_insync still has
    # that whole initial burst cached in app.ib.accountValues() though,
    # so replay it through the same handler now to seed account_status
    # from it directly, same "seed from existing state, then track live"
    # pattern _load_news_file() uses for news_by_ticker.
    for av in app.ib.accountValues():
        on_account_value(av)

    # connect() calls reqMarketDataType(3) (delayed data), but that doesn't
    # take effect synchronously — a reqMktData fired too soon after can race
    # ahead of it, default to requesting live data instead, and fail with
    # error 10168 ("Delayed market data is not enabled") since this account
    # has no live subscription. on_position's reactive subscribe (below)
    # can fire within milliseconds of reqPositions() below returning
    # results, right into that gap, so give the market data type a moment
    # to land first — before reqPositions() is even called, not just before
    # our own batch further down.
    await asyncio.sleep(1)

    # Positions live in IB Gateway, not sorted_screen.csv, so they're only
    # knowable once connected: ask for them and pump the event loop for a
    # couple seconds so the initial burst of on_position calls lands and
    # populates positions_by_ticker before we build the subscription list.
    await app.ib.reqPositionsAsync()
    await asyncio.sleep(2)
    with lock:
        held_tickers = sorted(positions_by_ticker)
    if len(held_tickers) > MAX_STREAMED_SYMBOLS:
        print(
            f"{len(held_tickers)} held tickers exceed the {MAX_STREAMED_SYMBOLS}-symbol budget on their own; "
            "streaming all of them anyway, zero room left for ranked screener tickers"
        )

    budget = max(0, MAX_STREAMED_SYMBOLS - len(held_tickers))
    fill = [t for t in ranked_tickers if t not in positions_by_ticker][:budget]
    all_tickers = held_tickers + fill

    # on_position's own reactive subscribe (see below) may have already
    # claimed some of these during the sleep() above — it doesn't wait for
    # this batch. Re-requesting market data for an already-streamed symbol
    # wastes a second line on it for nothing, which is exactly what was
    # blowing the MAX_STREAMED_SYMBOLS budget and surfacing as IB error 101
    # ("Max number of tickers has been reached").
    to_subscribe = [t for t in all_tickers if t not in streamed_symbols]
    print(f"Held tickers ({len(held_tickers)}): {', '.join(held_tickers)}")
    print(
        f"Streaming {len(held_tickers)} held ticker(s) + {len(fill)} top-ranked Strong Buy/Buy/Sell/Strong Sell ticker(s) "
        f"({len(all_tickers) - len(to_subscribe)} already subscribed by on_position)"
    )

    contracts = [app.make_contract(t) for t in to_subscribe]
    unmade = [t for t, c in zip(to_subscribe, contracts) if c is None]
    if unmade:
        print(f"make_contract() returned nothing for: {', '.join(unmade)}")
    contracts = [c for c in contracts if c is not None]

    qualified = await app.ib.qualifyContractsAsync(*contracts)
    unqualified = sorted({c.symbol for c in contracts} - {c.symbol for c in qualified})
    if unqualified:
        print(f"qualifyContracts() rejected: {', '.join(unqualified)}")
    print(f"Qualified {len(qualified)}/{len(to_subscribe)} contracts")

    for c in qualified:
        streamed_symbols.add(c.symbol)
        print(f"reqMktData: {c.symbol} (conId={c.conId}, exchange={c.exchange})")
        app.ib.reqMktData(c, "", False, False)

    still_missing = sorted(set(held_tickers) - streamed_symbols)
    if still_missing:
        print(f"WARNING: {len(still_missing)} held ticker(s) got no price subscription at all: {', '.join(still_missing)}")

    # A held ticker can end up with no live price for reasons a restart
    # alone won't fix — outside the screener universe entirely (never in
    # symbols.json, e.g. an ETF), or missing account market-data
    # permissions for its exchange (see the ARKK/BATS case). Either way,
    # the position still needs *a* price for Value/Weight/P&L to mean
    # anything, so fetch each held ticker's last month of yfinance daily
    # closes (same fetch shape as IBApp.get_momentum's history_out) and
    # seed last_price_by_ticker with the most recent one — but only where
    # nothing live has come in yet, so this never clobbers a real IB tick.
    if held_tickers:
        print(f"Fetching 1mo price history for {len(held_tickers)} held ticker(s) as a fallback for missing live prices...")
        # get_price_history is a blocking yfinance call — offloaded to a
        # worker thread so it doesn't stall this connection's shared event
        # loop (ticks, snapshot polling) for however long yfinance takes.
        history = await asyncio.to_thread(app.get_price_history, held_tickers)
        with lock:
            filled = [
                ticker
                for ticker, series in history.items()
                if series and last_price_by_ticker.get(ticker, {}).get("last") is None
            ]
            for ticker in filled:
                series = history[ticker]
                last_price_by_ticker[ticker] = {"last": series[-1]["close"], "timestamp": series[-1]["date"]}
        if filled:
            print(f"Filled {len(filled)} held ticker(s) with a historical fallback price: {', '.join(sorted(filled))}")
        broadcast()

    # Background task on this same connection — see fetch_candlestick_history,
    # which unions all_tickers with its own top-CANDLESTICK_TOP_N pull and
    # every RATED_FOR_EXTRAS ticker.
    # Started here (not from run_ib_client) because all_tickers — every
    # ticker this process actually streams a price for, not just the ranked
    # pool — only exists once this function has built it.
    asyncio.ensure_future(fetch_candlestick_history(all_tickers))


# Dataset tab's Run button (see DATASETS' own "run" field,
# _handle_run_dataset/_handle_run_status below, and DatasetView.tsx) --
# one job at a time, globally, across every row: two dataset-refresh
# commands racing each other (e.g. two yfinance pulls, or an IB fetch
# racing the in-process ibprices/ibhprices path against itself) would
# just corrupt each other's output the same way running two of these by
# hand in separate terminals already would, so this is a single mutable
# slot rather than a job queue/table. Holds the MOST RECENT job even
# after it finishes (not cleared back to None) so a client that starts
# polling a few seconds late, or reloads the page mid-run, still sees
# it.
_job_lock = threading.Lock()
_current_job = None  # {"id", "label", "status": "running"|"done"|"error", "log": [line, ...], "returncode", "startedAt", "endedAt"}


def _job_log(line):
    """Appends one line to the current job's log, under _job_lock. Safe
    to call from any thread -- the subprocess reader thread
    (_run_subprocess_job) and the ib_loop asyncio thread (via the
    log_fn callbacks refresh_daily_history_on_demand/
    refresh_hourly_history_on_demand accept) both do."""
    with _job_lock:
        if _current_job is not None:
            _current_job["log"].append(line)


def _job_finish(status, returncode=None):
    with _job_lock:
        if _current_job is not None:
            _current_job["status"] = status
            _current_job["returncode"] = returncode
            _current_job["endedAt"] = datetime.now(timezone.utc).isoformat()


def _run_subprocess_job(argv):
    """Runs `python <argv>` as a real child process (see DATASETS' "run":
    [{"kind": "subprocess", "argv": [...], ...}, ...] options -- argv is
    everything after the interpreter, e.g. ["main.py", "rescore"] or
    ["ib_server.py", "performance"]) -- genuine process isolation, same
    as running it by hand in a terminal, with its stdout captured line
    by line into the current job's log as it prints
    (IBApp.get_forward_pe/get_momentum's own per-ticker "Fetching X..."
    lines, plus each mode's own summary prints). stderr is merged into
    stdout so a traceback shows up in the log too, not just in this
    server's own console. Runs in its own thread (started by
    _handle_run_dataset) so the HTTP request that kicked the job off can
    return immediately instead of blocking for however long the fetch
    takes.

    -u (unbuffered): stdout is block-buffered, not line-buffered, the
    moment Python detects it's writing to a pipe instead of a real
    terminal -- without this, the child's print() lines would all sit in
    that buffer and arrive here in one burst when the process exits
    (or the buffer happens to fill), instead of streaming in as each
    ticker is actually fetched, defeating the whole point of a live
    log."""
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", *argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as e:
        _job_log(f"Failed to start: {e}")
        _job_finish("error")
        return
    for line in proc.stdout:
        _job_log(line.rstrip("\n"))
    returncode = proc.wait()
    _job_finish("done" if returncode == 0 else "error", returncode)


def _run_in_process_job(target):
    """Runs the IB daily/hourly on-demand refresh directly on this
    server's own already-connected IB Gateway connection (see DATASETS'
    "run": {"kind": "in_process", ...} rows, and
    refresh_daily_history_on_demand/refresh_hourly_history_on_demand's
    own docstrings for why those two rows need this instead of
    _run_subprocess_job: `python main.py ibprices`/`ibhprices` as a
    child process would just route straight back to this same server
    over HTTP and block there with no per-ticker output of its own).
    Submits the coroutine onto ib_loop via run_coroutine_threadsafe and
    blocks this thread on its result -- same pattern
    _handle_refresh_ib_daily/_handle_refresh_ib_hourly already use for
    the HTTP-routed callers, just with log_fn wired to this job's log."""
    if ib_loop is None or not app.is_connected:
        _job_log("IB Gateway not connected")
        _job_finish("error")
        return
    coro = refresh_daily_history_on_demand(log_fn=_job_log) if target == "daily" else refresh_hourly_history_on_demand(log_fn=_job_log)
    try:
        future = asyncio.run_coroutine_threadsafe(coro, ib_loop)
        result = future.result(timeout=3600)
    except Exception as e:
        _job_log(f"Failed: {e}")
        _job_finish("error")
        return
    if result.get("error"):
        _job_log(result["error"])
        _job_finish("error")
    else:
        _job_finish("done", 0)


def _start_job(row_id, label, run_cfg):
    """Claims the single job slot for `row_id`/`run_cfg` (a DATASETS
    row's own "id"/"run") and starts it in a background thread; False
    (no-op) if a job is already running, regardless of which row it's
    for -- see _current_job's own comment on why this is global, not
    per-row."""
    global _current_job
    with _job_lock:
        if _current_job is not None and _current_job["status"] == "running":
            return False
        _current_job = {
            "id": row_id,
            "label": label,
            "status": "running",
            "log": [],
            "returncode": None,
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "endedAt": None,
        }
    if run_cfg["kind"] == "subprocess":
        threading.Thread(target=_run_subprocess_job, args=(run_cfg["argv"],), daemon=True).start()
    else:
        threading.Thread(target=_run_in_process_job, args=(run_cfg["target"],), daemon=True).start()
    return True


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # socketserver's default handle_error prints a full traceback for
        # ANY exception mid-request, including a client resetting the
        # connection before we even finish reading its request line — routine
        # (tab refresh, an EventSource reconnecting) and not a real failure.
        # Anything else still gets the traceback.
        if sys.exc_info()[0] in (ConnectionResetError, BrokenPipeError):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # BaseHTTPRequestHandler logs every request to stderr by default; too noisy for a poll/stream endpoint.

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in STATIC_FILES:
            self._handle_static_file(STATIC_FILES[parsed.path])
        elif parsed.path == "/api/stream":
            self._handle_stream()
        elif parsed.path == "/api/last-prices":
            self._send_json(last_price_by_ticker)
        elif parsed.path == "/api/positions":
            self._send_json(positions_by_ticker)
        elif parsed.path == "/api/account":
            self._send_json(account_status)
        elif parsed.path == "/api/trades":
            self._send_json(trades_by_ticker)
        elif parsed.path == "/api/open-orders":
            self._send_json(open_orders)
        elif parsed.path == "/api/news":
            with lock:
                snapshot = _news_snapshot()
            self._send_json(snapshot)
        elif parsed.path == "/api/news/article":
            self._handle_news_article(parse_qs(parsed.query))
        elif parsed.path == "/api/dataset-status":
            self._handle_dataset_status()
        elif parsed.path == "/api/scoring-formula":
            self._handle_scoring_formula()
        elif parsed.path == "/api/admin/run-status":
            self._handle_run_status()
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        # Browsers preflight a POST carrying a Content-Type: application/json
        # body (not a CORS "simple" content type, unlike the plain GETs
        # every other endpoint here only ever receives) with an OPTIONS
        # request before /api/chat's own POST -- without an explicit
        # response here, the browser blocks the real request before it's
        # even sent.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/chat":
            self._handle_chat()
        elif parsed.path == "/api/admin/refresh-ib-daily":
            self._handle_refresh_ib_daily()
        elif parsed.path == "/api/admin/refresh-ib-hourly":
            self._handle_refresh_ib_hourly()
        elif parsed.path == "/api/admin/export-daily-history":
            self._handle_export_daily_history()
        elif parsed.path == "/api/admin/run-dataset":
            self._handle_run_dataset()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_run_dataset(self):
        """POST /api/admin/run-dataset -- {"id": <a DATASETS entry's own
        "id">} starts that row's single Run button action (see DATASETS'
        own "run" field and _start_job) as a background job and returns
        immediately with {"started": true}, or {"error": str} if the id
        doesn't match a runnable row or a job is already running (see
        _current_job's own comment: one job at a time, globally). Poll
        GET /api/admin/run-status for progress -- this endpoint doesn't
        block on the job itself, unlike /api/admin/refresh-ib-daily/
        -hourly above."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except (ValueError, json.JSONDecodeError):
            self.send_response(400)
            self.end_headers()
            return
        row_id = body.get("id")
        entry = next((e for e in DATASETS if e["id"] == row_id), None)
        if entry is None or entry["run"] is None:
            self._send_json({"error": "This dataset has no runnable command"})
            return
        started = _start_job(entry["id"], entry["label"], entry["run"])
        if not started:
            with _job_lock:
                running_label = _current_job["label"] if _current_job else None
            self._send_json({"error": f"Already running: {running_label}"})
            return
        self._send_json({"started": True})

    def _handle_run_status(self):
        """GET /api/admin/run-status -> the current (or most recently
        finished) job's {"id", "label", "status": "idle"|"running"|
        "done"|"error", "log": [str, ...], "returncode", "startedAt",
        "endedAt"} -- polled by the Dataset tab's Run button every
        second or so while a job is running (see DatasetView.tsx).
        "idle" (every other field null/empty) if nothing has been run
        yet this session."""
        with _job_lock:
            if _current_job is None:
                self._send_json(
                    {
                        "id": None,
                        "label": None,
                        "status": "idle",
                        "log": [],
                        "returncode": None,
                        "startedAt": None,
                        "endedAt": None,
                    }
                )
                return
            snapshot = dict(_current_job)
        self._send_json(snapshot)

    def _handle_refresh_ib_daily(self):
        """POST /api/admin/refresh-ib-daily -- see
        refresh_daily_history_on_demand. Blocks this request thread
        (ThreadingHTTPServer, so only this one request thread, same as
        _handle_news_article's blocking IB call above) until the fetch
        finishes -- can take a while when many tickers are stale, paced by
        IB's rate limit, hence the long timeout; main.py's own caller uses
        a matching one."""
        if ib_loop is None or not app.is_connected:
            self._send_json({"error": "IB Gateway not connected"})
            return
        try:
            future = asyncio.run_coroutine_threadsafe(refresh_daily_history_on_demand(), ib_loop)
            result = future.result(timeout=3600)
        except Exception as e:
            self._send_json({"error": str(e)})
            return
        self._send_json(result)

    def _handle_refresh_ib_hourly(self):
        """POST /api/admin/refresh-ib-hourly -- the hourly twin of
        _handle_refresh_ib_daily; see refresh_hourly_history_on_demand."""
        if ib_loop is None or not app.is_connected:
            self._send_json({"error": "IB Gateway not connected"})
            return
        try:
            future = asyncio.run_coroutine_threadsafe(refresh_hourly_history_on_demand(), ib_loop)
            result = future.result(timeout=3600)
        except Exception as e:
            self._send_json({"error": str(e)})
            return
        self._send_json(result)

    def _handle_export_daily_history(self):
        """POST /api/admin/export-daily-history -- {"duration": "2 Y"}
        (duration optional, defaults to "2 Y") -- see
        export_daily_history_on_demand. Blocks this request thread
        until the whole fetch finishes; unlike /api/admin/refresh-ib-
        daily above, this always fetches every ticker in scope (no
        staleness gate), so it routinely takes much longer -- a 2-hour
        timeout, not 1, to comfortably cover a multi-hundred-ticker
        pull at IB's paced rate limit."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except (ValueError, json.JSONDecodeError):
            self.send_response(400)
            self.end_headers()
            return
        duration = body.get("duration") or "2 Y"
        if ib_loop is None or not app.is_connected:
            self._send_json({"error": "IB Gateway not connected"})
            return
        try:
            future = asyncio.run_coroutine_threadsafe(export_daily_history_on_demand(duration), ib_loop)
            result = future.result(timeout=7200)
        except Exception as e:
            self._send_json({"error": str(e)})
            return
        self._send_json(result)

    def _handle_chat(self):
        """POST /api/chat -- {"question": str, "history": [{"role",
        "content"}, ...]} -> {"answer": str} (or {"error": str}). Lazily
        imports chatbot (langchain + langchain-ollama, only needed here) so
        a machine without those installed, or without Ollama running,
        still gets a normal price-streaming server -- only this one
        endpoint fails, with a plain-language error, until they're set up.
        Snapshots positions/prices/account under `lock` the same instant
        every other GET endpoint already reads them from, so the chatbot's
        live-data tools (see chatbot._make_live_tools) see one consistent
        moment, not values changing mid-request. A local LLM tool-calling
        loop can take real time, but that's fine to block on here --
        Handler runs under ThreadingHTTPServer (see Server below), so this
        only ties up its own request thread, same as
        _handle_news_article's blocking IB call above never stalling live
        price streaming."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self.send_response(400)
            self.end_headers()
            return
        question = (body.get("question") or "").strip()
        if not question:
            self.send_response(400)
            self.end_headers()
            return
        history = body.get("history") or []

        with lock:
            live_state = {
                "positions": dict(positions_by_ticker),
                "prices": dict(last_price_by_ticker),
                "account": dict(account_status),
            }
        try:
            import chatbot

            answer = chatbot.answer_question(question, history, live_state)
        except Exception as e:
            self._send_json({"error": str(e)})
            return
        self._send_json({"answer": answer})

    def _handle_news_article(self, query):
        """Lazy on-demand article body fetch (see IBApp.get_news_article_async)
        -- ?ticker=...&articleId=... identifies the article; providerCode
        is looked up server-side from the already-cached headline (same
        news_by_ticker bucket GET /api/news reads) rather than trusting a
        client-supplied value."""
        ticker = (query.get("ticker") or [None])[0]
        article_id = (query.get("articleId") or [None])[0]
        if not ticker or not article_id:
            self.send_response(400)
            self.end_headers()
            return
        with lock:
            article = news_by_ticker.get(ticker, {}).get(article_id)
        provider = article.get("provider") if article else None
        if not provider:
            self.send_response(404)
            self.end_headers()
            return
        if ib_loop is None or not app.is_connected:
            self._send_json({"error": "IB Gateway not connected"})
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                app.get_news_article_async(provider, article_id), ib_loop
            )
            text = future.result(timeout=20)
        except Exception as e:
            self._send_json({"error": str(e)})
            return
        self._send_json({"text": text})

    def _send_json(self, data):
        with lock:
            body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        # Local-only tool hit straight from the Vite dev server / a
        # file:// build; no auth or sensitive data in the response, so a
        # wildcard origin is fine here.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_dataset_status(self):
        """GET /api/dataset-status -> {"files": [{id, path, label,
        command, notes, network, exists, mtime (ISO 8601 UTC, None if
        missing), sizeBytes, latestBarDate, expectedBarDate, stale,
        canRun}, ...]} -- one entry per DATASETS descriptor, in that
        list's order (the web app's Dataset tab renders it as-is, no
        client-side sort). id is that row's own stable identity (see
        DATASETS' own comment on why it's separate from path); canRun is
        just entry["run"] is not None -- the Dataset tab's Run button
        (see _handle_run_dataset) reads it to decide whether to show a
        button for that row at all, and sends `id` back to say which
        row's command to run; the actual run config (kind/argv/target)
        stays server-side, never sent to the client. Reads mtime/size
        straight off disk on every request rather than caching -- this
        endpoint is a diagnostic the user opens occasionally, not
        something polled, so there's no reason to trade staleness for
        speed here the way /api/stream's own snapshot does.

        latestBarDate/expectedBarDate/stale (see _price_history_staleness)
        are only populated for the two price-history time-series files --
        None for everything else. This is a genuinely different freshness
        signal than mtime: mtime just says the file was WRITTEN recently,
        which turned out to be true even during a real incident where a
        partial yfinance fetch failure left ~99% of tickers' actual last
        bar days behind (the file still gets rewritten -- and its mtime
        still updates -- from the tickers that DID succeed plus everything
        carried over unchanged)."""
        files = []
        for entry in DATASETS:
            path = entry["path"]
            try:
                st = os.stat(path)
                exists = True
                mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
                size_bytes = st.st_size
            except FileNotFoundError:
                exists = False
                mtime = None
                size_bytes = None
            staleness = _price_history_staleness(path) if exists and path in _PRICE_HISTORY_STALENESS_PATHS else None
            files.append(
                {
                    "id": entry["id"],
                    "path": path,
                    "label": entry["label"],
                    "command": entry["command"],
                    "notes": entry["notes"],
                    "network": entry["network"],
                    "exists": exists,
                    "mtime": mtime,
                    "sizeBytes": size_bytes,
                    "latestBarDate": staleness["latestBarDate"] if staleness else None,
                    "expectedBarDate": staleness["expectedBarDate"] if staleness else None,
                    "stale": staleness["stale"] if staleness else None,
                    "canRun": entry["run"] is not None,
                }
            )
        self._send_json({"files": files})

    def _handle_static_file(self, path):
        """Serves one of STATIC_FILES fresh from disk on every request --
        see that dict's own comment for why (the web app used to read a
        one-time-copied snapshot in web/public/ instead, which went
        stale the moment the real file was rewritten without a dev-
        server restart). 404 if the file doesn't exist yet (e.g. a
        download that's never actually been run) -- same as a missing
        static file would 404 under Vite's own public/ serving, so
        every caller's existing `r.ok ? ... : ...`/`.catch()` fallback
        handling doesn't need to change at all."""
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/csv" if path.endswith(".csv") else "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_scoring_formula(self):
        """GET /api/scoring-formula -> {"factors": [{"key", "label",
        "standardWeight", "financialsWeight", "utilitiesWeight",
        "realEstateWeight"}, ...]} -- one row per scoring.FACTOR_WEIGHTS
        entry, in that dict's insertion order (the Scoring tab renders
        it as-is, no client-side sort). Reads straight from
        scoring.FACTOR_WEIGHTS -- the exact dict score_rows itself sums
        over -- rather than hand-copying the numbers into this file or
        the frontend, so the formula columns the Scoring tab shows can
        never drift from what actually gets computed into
        sorted_screen.csv's score column."""
        factors = [
            {
                "key": key,
                "label": label,
                "standardWeight": standard,
                "financialsWeight": financials,
                "utilitiesWeight": utilities,
                "realEstateWeight": real_estate,
            }
            for key, (label, standard, financials, utilities, real_estate) in FACTOR_WEIGHTS.items()
        ]
        self._send_json({"factors": factors})

    def _handle_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q = queue.Queue(maxsize=1)
        with lock:
            q.put_nowait(json.dumps({
                "prices": last_price_by_ticker,
                "positions": positions_by_ticker,
                "account": account_status,
                "trades": trades_by_ticker,
                "pnl": pnl_by_ticker,
                "openOrders": open_orders,
            }))
        with subscribers_lock:
            subscribers.append(q)
        try:
            while True:
                try:
                    payload = q.get(timeout=HEARTBEAT_SECONDS)
                    self.wfile.write(f"data: {payload}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")  # keeps the connection alive and surfaces a dead one fast
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with subscribers_lock:
                if q in subscribers:
                    subscribers.remove(q)


def main():
    # `no_news` is a bare flag, not a positional value -- pull it out
    # first so it can sit in either position (`no_news 8765` or
    # `8765 no_news`) without disturbing the performance/port parsing below.
    args = [a for a in sys.argv[1:] if a != "no_news"]
    no_news = len(args) != len(sys.argv[1:])

    # One-shot mode, not the streaming server — fetches, writes, exits.
    # `python ib_server.py performance [YYYY-MM-DD]`
    if args and args[0] == "performance":
        start_date = args[1] if len(args) > 1 else None
        fetch_account_performance(start_date)
        return

    # `python ib_server.py trades [YYYY-MM-DD]`
    if args and args[0] == "trades":
        start_date = args[1] if len(args) > 1 else None
        fetch_trades_report(start_date)
        return

    port = int(args[0]) if args else 8765

    # A candidate pool, not a guarantee — stream_prices_and_positions trims this to
    # whatever's left of MAX_STREAMED_SYMBOLS after held positions (which
    # always get a slot) claim theirs. Restricted to RATED_FOR_EXTRAS
    # (Strong Buy/Buy/Sell/Strong Sell, same rating-based scope main.py's
    # SEC/social-sentiment downloads already use) rather than a flat
    # top-N-by-rank cutoff, so the scarce 99-symbol IB Gateway budget goes
    # to names worth acting on instead of Hold-rated middle-of-the-pack
    # ones -- whatever's actually rendering as a Long/Short card on the
    # Recommendations tab goes first within that filtered set (see
    # _priority_tickers), just truncated to the budget here.
    tickers = _priority_tickers()[:MAX_STREAMED_SYMBOLS]
    if not tickers:
        sys.exit(f"No tickers found in {SORTED_SCREEN_CSV}; run main.py first")
    # Seed every candidate up front so the response shape is stable from
    # the first push, even before any ticks have arrived.
    last_price_by_ticker.update({t: {"last": None, "bid": None, "ask": None, "timestamp": None} for t in tickers})

    if no_news:
        print("no_news: skipping news_loop for this run")
    threading.Thread(target=run_ib_client, args=(tickers, no_news), daemon=True).start()

    server = Server(("localhost", port), Handler)
    print(f"Serving /api/stream on http://localhost:{port}")
    server.serve_forever()


def run_ib_client(tickers, no_news=False):
    """Owns the single IB Gateway connection (clientId 0) for the life of
    the process — live price/position streaming (stream_prices_and_
    positions), candlestick history (fetch_candlestick_history), snapshot
    polling (snapshot_loop), today's-fills polling (trades_loop), and
    news headlines (news_loop, unless no_news) all share this one
    connection as concurrent asyncio tasks on one event loop, instead of
    each opening its own clientId. IB Gateway/TWS shows exactly one
    connected API client for this whole process. Runs forever in its own
    thread — the HTTP server (see main()) runs on the main thread instead
    and never touches IB Gateway, so it doesn't add a second client."""
    global ib_loop
    ib_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(ib_loop)
    app.connect()
    if not app.is_connected:
        print("Could not connect to IB Gateway; /api/stream will keep serving empty snapshots.")
        return
    asyncio.ensure_future(stream_prices_and_positions(tickers))
    asyncio.ensure_future(snapshot_loop())
    asyncio.ensure_future(trades_loop())
    asyncio.ensure_future(open_orders_loop())
    asyncio.ensure_future(performance_loop())
    # Seeds news_by_ticker from news.json either way, so GET /api/news
    # still serves the existing rolling window even when no_news skips
    # starting the loop that fetches new headlines. _backfill_news_sentiment
    # (also scheduled regardless of no_news) then scores whatever's cached
    # but unscored and writes news.json/news_sentiment.json from it --
    # no_news only means "don't spend IB's news-headline pacing budget
    # re-downloading this run", not "stop producing those files": without
    # this, a no_news run against a cache that predates FinBERT scoring
    # would leave news_sentiment.json permanently empty, since no_news
    # skips the only other thing (news_loop) that would ever score them.
    _load_news_file()
    asyncio.ensure_future(_backfill_news_sentiment())
    if not no_news:
        asyncio.ensure_future(news_loop())
    app.ib.run()


if __name__ == "__main__":
    main()
