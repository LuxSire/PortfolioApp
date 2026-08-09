"""
ib_price_server.py — tiny local HTTP server pushing IB Gateway last-price
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

Run: python ib_price_server.py [port] [no_news]   (port default 8765)
Requires IB Gateway running and reachable at 127.0.0.1:4001, same
precondition as IBApp.connect(). Note reqMarketDataType(3) in
IBApp.connect() means delayed (15-20 min) price data unless your account
has a live data subscription and that's changed to type 1.

`no_news` (in either arg position, e.g. `python ib_price_server.py no_news`
or `python ib_price_server.py 8765 no_news`) skips *re-downloading* news
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
                            "trades": {ticker: {qty, value}}} — the full
                           current snapshot, sent on connect and again
                           whenever any of the four changes. Positions cover
                           the whole account, not just the top-ranked
                           tickers: stream_prices_and_positions asks for
                           positions right after connecting and streams all
                           of them, uncapped, filling whatever's left of the
                           MAX_STREAMED_SYMBOLS budget with the
                           highest-ranked screener tickers; on_position
                           catches anything opened later the same way.
                           `prices` ends up covering every held stock plus
                           as many ranked tickers as fit. Stocks only — an
                           option and its underlying share a ticker symbol,
                           which this doesn't disambiguate. `trades` is
                           today's fills only (qty = signed net shares
                           traded today, value = sum(signedQty *
                           fillPrice)) — see refresh_trades / IBApp.
                           get_today_executions_async — and only has an
                           entry for a symbol actually traded today.
                           PositionsView.jsx derives today's realized P&L
                           client-side from `trades` + `positions` + price
                           history for any symbol traded today but no
                           longer held (see its own comments) — mark-to-
                           market vs. yesterday's close, same convention
                           every other daily figure on that page uses, not
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
Both cover the top CANDLESTICK_TOP_N ranked tickers, unioned with every
ticker this process actually streams a live price for (so a held position
outside that ranked set, like an ETF not even in sorted_screen.csv, still
gets covered) — fetched once at startup (see fetch_candlestick_history).
reqHistoricalData's pacing limit (~60 requests/10min, account-wide) means
this can take hours for hundreds of tickers x 2 series; it runs as a
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

One-shot mode: `python ib_price_server.py performance [YYYY-MM-DD]`
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
import sys
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from dateutil.parser import isoparse

from IBApp import IBApp
from main import SORTED_SCREEN_CSV, load_top_tickers
from news_sentiment import clean_headline, score_headlines

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
NEWS_FILE = os.path.join(DATA_DIR, "news.json")
NEWS_SENTIMENT_FILE = os.path.join(DATA_DIR, "news_sentiment.json")
# The query's own range starts 2026-06-30/07-01, but that first day (and
# the bare baseline row before it) isn't a real trading day's worth of
# activity in this account, so the Portfolio tab starts from here instead.
# Only trims rows client-side, same as passing this as fetch_account_
# performance's start_date argument -- doesn't shrink what IBKR generates.
PORTFOLIO_START_DATE = "2026-07-03"
# Candlestick coverage is capped separately from MAX_STREAMED_SYMBOLS — it's
# not a live-data budget, it's how many tickers ~10 hours of IB's paced
# reqHistoricalData (~55 requests/10min, see IBApp.get_ib_historical_bars_async)
# can realistically cover in one run. 500 tickers x 2 series is ~3 hours;
# the full sorted_screen.csv (~1,663 tickers) would be ~10.
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
    Guards two real gotchas: ib_insync can hand back a Decimal (JSON-
    serializable floats only, not Decimal — a bare Decimal here would throw
    inside json.dumps and break every subsequent broadcast, not just this
    one field), and IBKR occasionally reports NaN avgCost for positions
    with an unknown cost basis (e.g. certain corporate actions) — a NaN
    that reaches json.dumps gets written out as the bare token `NaN`, which
    is not valid JSON and makes the browser's JSON.parse reject the whole
    message."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def on_position(position):
    """Fired once per existing position right after reqPositions() is
    called (an initial snapshot burst — see stream_prices_and_positions, which waits
    for this before building its subscription list), then again on every
    future change for the life of the process. The streamed_symbols check
    below is what catches a position opened after that startup batch: a
    newly-held ticker outside the top-ranked set gets its price
    subscription added here, on the fly, using the contract IBKR already
    gave us (a real account position always has a resolved conId, no
    qualifyContracts round-trip needed)."""
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

    Covers the top CANDLESTICK_TOP_N ranked tickers, unioned with
    streamed_tickers (every ticker actually streamed a live price —
    positions and ranked fill alike) so a held position outside the top
    N, like an ETF that isn't even in sorted_screen.csv, still gets
    covered rather than silently dropped.

    Writes HOURLY_HISTORY_FILE and DAILY_HISTORY_FILE once each series is
    done; Asset.jsx fetches them as static files, same as
    main.py's price_history.json."""
    ranked = load_top_tickers(SORTED_SCREEN_CSV, CANDLESTICK_TOP_N)
    tickers = sorted(set(ranked) | set(streamed_tickers))
    print(
        f"Candlestick history covers {len(tickers)} ticker(s): top {len(ranked)} ranked "
        f"+ {len(tickers) - len(ranked)} streamed-but-unranked (e.g. positions outside the top {CANDLESTICK_TOP_N})"
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
    and sorted_screen.csv's own ranking can change between cycles too."""
    while True:
        ranked = load_top_tickers(SORTED_SCREEN_CSV)
        with lock:
            tickers = [t for t in ranked if t not in streamed_symbols]
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



async def stream_prices_and_positions(ranked_tickers):
    """Opens one streaming market-data subscription batch on the already-
    connected shared `app` (see run_ib_client, clientId 0) — every
    currently held position first (never truncated: a P&L blind spot on
    your own holdings is worse than bumping into IBKR's market-data-line
    budget), then the highest-ranked remaining tickers up to
    MAX_STREAMED_SYMBOLS total — and subscribes to account position
    updates. Live ticks/position updates keep arriving afterward purely
    through the event handlers registered below, driven by run_ib_client's
    app.ib.run(); this coroutine itself finishes once setup is done."""
    # IBApp's own tick handler prints a line per tick, meant for a single
    # symbol in a foreground script; too noisy across MAX_STREAMED_SYMBOLS
    # streaming symbols here, so we swap in our own instead.
    app.ib.pendingTickersEvent -= app.on_pending_tickers
    app.ib.pendingTickersEvent += on_pending_tickers
    app.ib.positionEvent += on_position
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
        f"Streaming {len(held_tickers)} held ticker(s) + {len(fill)} top-ranked ticker(s) "
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
    # which unions all_tickers with its own top-CANDLESTICK_TOP_N pull.
    # Started here (not from run_ib_client) because all_tickers — every
    # ticker this process actually streams a price for, not just the ranked
    # pool — only exists once this function has built it.
    asyncio.ensure_future(fetch_candlestick_history(all_tickers))


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
        if parsed.path == "/api/stream":
            self._handle_stream()
        elif parsed.path == "/api/last-prices":
            self._send_json(last_price_by_ticker)
        elif parsed.path == "/api/positions":
            self._send_json(positions_by_ticker)
        elif parsed.path == "/api/account":
            self._send_json(account_status)
        elif parsed.path == "/api/trades":
            self._send_json(trades_by_ticker)
        elif parsed.path == "/api/news":
            with lock:
                snapshot = _news_snapshot()
            self._send_json(snapshot)
        elif parsed.path == "/api/news/article":
            self._handle_news_article(parse_qs(parsed.query))
        else:
            self.send_response(404)
            self.end_headers()

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
    # `python ib_price_server.py performance [YYYY-MM-DD]`
    if args and args[0] == "performance":
        start_date = args[1] if len(args) > 1 else None
        fetch_account_performance(start_date)
        return

    port = int(args[0]) if args else 8765

    # A candidate pool, not a guarantee — stream_prices_and_positions trims this to
    # whatever's left of MAX_STREAMED_SYMBOLS after held positions (which
    # always get a slot) claim theirs.
    tickers = load_top_tickers(SORTED_SCREEN_CSV, MAX_STREAMED_SYMBOLS)
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
