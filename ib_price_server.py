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

Run: python ib_price_server.py [port]   (default 8765)
Requires IB Gateway running and reachable at 127.0.0.1:4001, same
precondition as IBApp.connect(). Note reqMarketDataType(3) in
IBApp.connect() means delayed (15-20 min) price data unless your account
has a live data subscription and that's changed to type 1.

Endpoints:
  GET /api/stream       -> text/event-stream; each `data:` line is
                           {"prices": {ticker: {last, bid, ask, timestamp}},
                            "positions": {ticker: {shares, avgCost}},
                            "account": {tag: value}} — the full current
                           snapshot, sent on connect and again whenever any
                           of the three changes. Positions cover the
                           whole account, not just the top-ranked tickers:
                           start_streaming asks for positions right after
                           connecting and streams all of them, uncapped,
                           filling whatever's left of the MAX_STREAMED_SYMBOLS
                           budget with the highest-ranked screener tickers;
                           on_position catches anything opened later the same
                           way. `prices` ends up covering every held stock
                           plus as many ranked tickers as fit. Stocks only —
                           an option and its underlying share a ticker
                           symbol, which this doesn't disambiguate.
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

Also writes (not served — Asset.jsx fetches these as static files, same as
main.py's price_history.json):
  price_history_hourly.json    {ticker: [{date, open, high, low, close,
                                volume}]} 1 month of hourly bars, IB
                                Gateway's own historical data
                                (reqHistoricalData), for candlestick +
                                volume charts.
  price_history_daily_3mo.json Same shape, 3 months of daily bars.
Both cover the top CANDLESTICK_TOP_N ranked tickers, unioned with every
ticker this process actually streams a live price for (so a held position
outside that ranked set, like an ETF not even in sorted_screen.csv, still
gets covered) — fetched once at startup on a second, separate IB Gateway
connection (see fetch_candlestick_history), since reqHistoricalData's
pacing limit (~60 requests/10min, account-wide) means this can take hours
for hundreds of tickers x 2 series, and running it on the main connection
would stall live price/position streaming for that whole time.

A ticker ranked in the top SNAPSHOT_TOP_N but outside MAX_STREAMED_SYMBOLS
(so no live reqMktData subscription) still gets a `prices` entry — just
refreshed every SNAPSHOT_INTERVAL_SECONDS instead of live-ticking, via a
one-shot IB snapshot request on a third, separate IB Gateway connection
(see run_snapshot_loop). A snapshot resolves once and releases its
market-data line immediately, so this costs nothing against
MAX_STREAMED_SYMBOLS regardless of how many tickers it covers.

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
import sys
import threading
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from IBApp import IBApp
from main import SORTED_SCREEN_CSV, load_top_tickers

MAX_STREAMED_SYMBOLS = 99  # this account's real ceiling — 100 hits IB error 101, "Max number of tickers has been reached"
HOURLY_HISTORY_FILE = "price_history_hourly.json"
DAILY_HISTORY_FILE = "price_history_daily_3mo.json"
PORTFOLIO_PERFORMANCE_FILE = "portfolio_performance.json"
# The query's own range starts 2026-06-30/07-01, but that first day (and
# the bare baseline row before it) isn't a real trading day's worth of
# activity in this account, so the Portfolio tab starts from here instead.
# Only trims rows client-side, same as passing this as fetch_account_
# performance's start_date argument -- doesn't shrink what IBKR generates.
PORTFOLIO_START_DATE = "2026-07-03"
# Candlestick coverage is capped separately from MAX_STREAMED_SYMBOLS — it's
# not a live-data budget, it's how many tickers ~10 hours of IB's paced
# reqHistoricalData (~55 requests/10min, see IBApp.get_ib_historical_bars)
# can realistically cover in one run. 500 tickers x 2 series is ~3 hours;
# the full sorted_screen.csv (~1,663 tickers) would be ~10.
CANDLESTICK_TOP_N = 500
# A distinct clientId from the main streaming connection (0) — IB Gateway
# allows multiple simultaneous API clients, and this needs to be its own
# connection so its pacing-limited reqHistoricalData calls (see
# IBApp.get_ib_historical_bars) don't block the main connection's event
# loop for the many minutes they can take.
CANDLESTICK_CLIENT_ID = 7
HEARTBEAT_SECONDS = 15

app = IBApp()
last_price_by_ticker = {}
positions_by_ticker = {}
account_status = {}
lock = threading.Lock()

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
    called (an initial snapshot burst — see start_streaming, which waits
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
    confirmation landing at IB Gateway — start_streaming's app.ib.sleep(1)
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
    reqAccountUpdates in start_streaming) — an initial burst covering
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


def fetch_candlestick_history(streamed_tickers):
    """Runs in its own thread on its own IB Gateway connection (see
    CANDLESTICK_CLIENT_ID) — reqHistoricalData's pacing limit means
    fetching two series for hundreds of tickers can take hours (see
    IBApp.get_ib_historical_bars), and doing that on the main streaming
    connection would stall live prices/positions the whole time.

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

    asyncio.set_event_loop(asyncio.new_event_loop())
    hist_app = IBApp()
    hist_app.connect(client_id=CANDLESTICK_CLIENT_ID)
    if not hist_app.is_connected:
        print("Could not open a second IB Gateway connection for candlestick history; skipping.")
        return

    print(f"Fetching 1mo hourly bars for {len(tickers)} ticker(s) (this can take a while, paced by IB's rate limit)...")
    hourly = hist_app.get_ib_historical_bars(tickers, "1 M", "1 hour")
    with open(HOURLY_HISTORY_FILE, "w") as f:
        json.dump(hourly, f)
    print(f"Wrote {HOURLY_HISTORY_FILE} ({sum(1 for v in hourly.values() if v)}/{len(tickers)} tickers with bars)")

    print(f"Fetching 3mo daily bars for {len(tickers)} ticker(s)...")
    daily = hist_app.get_ib_historical_bars(tickers, "3 M", "1 day")
    with open(DAILY_HISTORY_FILE, "w") as f:
        json.dump(daily, f)
    print(f"Wrote {DAILY_HISTORY_FILE} ({sum(1 for v in daily.values() if v)}/{len(tickers)} tickers with bars)")

    hist_app.ib.disconnect()


# Prices for tickers ranked in the top SNAPSHOT_TOP_N but NOT among the
# MAX_STREAMED_SYMBOLS this process holds a persistent reqMktData
# subscription for — refreshed periodically via IB's *snapshot* request
# (reqMktData with snapshot=True), which resolves once and releases its
# market-data line immediately rather than holding one open. That's what
# makes this free to run against hundreds of tickers without touching the
# MAX_STREAMED_SYMBOLS budget at all: a snapshot never counts as one of
# the persistent lines error 101 complains about.
SNAPSHOT_TOP_N = 500
SNAPSHOT_INTERVAL_SECONDS = 1800  # 30 minutes
# A third distinct clientId (0 = main streaming connection, CANDLESTICK_
# CLIENT_ID = 7) — same reasoning as the candlestick connection: keeps
# this off the main connection's event loop.
SNAPSHOT_CLIENT_ID = 8


def fetch_snapshot_prices(snapshot_app, tickers):
    """One-shot {last, bid, ask} per ticker via IB's snapshot request.
    Skips (rather than raising on) any ticker make_contract or
    qualifyContracts can't resolve, same as start_streaming's own batch
    subscribe."""
    contracts = {}
    for t in tickers:
        c = snapshot_app.make_contract(t)
        if c is not None:
            contracts[t] = c
    if not contracts:
        return {}

    qualified = snapshot_app.ib.qualifyContracts(*contracts.values())
    unqualified = sorted(set(contracts) - {c.symbol for c in qualified})
    if unqualified:
        print(f"Snapshot: qualifyContracts() rejected: {', '.join(unqualified)}")

    pending = {c.symbol: snapshot_app.ib.reqMktData(c, "", True, False) for c in qualified}
    # IB's snapshot requests typically resolve within a few seconds; this
    # just needs to be comfortably longer than that, not exact.
    snapshot_app.ib.sleep(11)

    results = {}
    for symbol, tk in pending.items():
        price = _extract_price(tk)
        if price is not None:
            results[symbol] = {"last": price, "bid": _clean(tk.bid), "ask": _clean(tk.ask)}
    return results


def run_snapshot_loop():
    """Runs forever in its own thread on its own IB Gateway connection (see
    SNAPSHOT_CLIENT_ID) — same reasoning as fetch_candlestick_history:
    keeps this off the main connection's event loop, so a slow round-trip
    here never risks stalling live prices/positions.

    Every SNAPSHOT_INTERVAL_SECONDS, refreshes the price for every ticker
    ranked in the top SNAPSHOT_TOP_N that isn't already covered by a live
    reqMktData subscription. Recomputed fresh each cycle (not once at
    startup) since streamed_symbols can grow over the process's life as
    on_position picks up newly opened positions, and sorted_screen.csv's
    own ranking can change between cycles too."""
    asyncio.set_event_loop(asyncio.new_event_loop())
    snapshot_app = IBApp()
    # IBApp's own tick handler prints a line per tick, meant for a single
    # symbol in a foreground script — every snapshot tick for up to
    # SNAPSHOT_TOP_N tickers would otherwise flood the console once per
    # cycle, same reasoning as start_streaming's own swap.
    snapshot_app.ib.pendingTickersEvent -= snapshot_app.on_pending_tickers
    snapshot_app.connect(client_id=SNAPSHOT_CLIENT_ID)
    if not snapshot_app.is_connected:
        print("Could not open a third IB Gateway connection for snapshot prices; skipping.")
        return

    while True:
        ranked = load_top_tickers(SORTED_SCREEN_CSV, SNAPSHOT_TOP_N)
        with lock:
            tickers = [t for t in ranked if t not in streamed_symbols]
        if tickers:
            print(f"Snapshot: fetching {len(tickers)} ticker(s) in the top {SNAPSHOT_TOP_N} but outside the live stream...")
            results = fetch_snapshot_prices(snapshot_app, tickers)
            now = datetime.now().isoformat(timespec="seconds")
            with lock:
                for symbol, data in results.items():
                    last_price_by_ticker[symbol] = {**data, "timestamp": now}
            broadcast()
            print(f"Snapshot: got a price for {len(results)}/{len(tickers)} ticker(s)")
        snapshot_app.ib.sleep(SNAPSHOT_INTERVAL_SECONDS)


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
    magnitude) and mtm from ChangeInNAV, all searched document-wide with
    root.iter() since each element carries its own date (reportDate /
    toDate) regardless of which <FlexStatement> it's nested under.

    realized does NOT come from ChangeInNAV's own realized attribute --
    confirmed against a real export that it's unpopulated (always 0) for
    this account, even on days with obvious trading activity. The real
    number lives in each day's own FIFOPerformanceSummaryUnderlying rows
    instead: every <FlexStatement> here is itself scoped to a single day
    (fromDate == toDate), so its nested rows are already that one day's
    figures. totalRealizedPnl, summed across a statement's symbols,
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
        stock_long = float(el.get("stockLong") or 0)
        stock_short = float(el.get("stockShort") or 0)
        by_date[d]["stockLong"] = stock_long
        by_date[d]["stockShort"] = stock_short
        by_date[d]["stockNet"] = stock_long + stock_short
        by_date[d]["stockGross"] = abs(stock_long) + abs(stock_short)

    for el in root.iter("ChangeInNAV"):
        to_date = el.get("toDate")
        if not to_date:
            continue
        d = _iso_date(to_date)
        by_date.setdefault(d, _blank_day(d))
        by_date[d]["mtm"] = float(el.get("mtm") or 0)
        by_date[d]["depositsWithdrawals"] = float(el.get("depositsWithdrawals") or 0)
        by_date[d]["commissions"] = float(el.get("commissions") or 0)
        by_date[d]["dividends"] = float(el.get("dividends") or 0)
        by_date[d]["interest"] = float(el.get("interest") or 0)

    for stmt in root.iter("FlexStatement"):
        to_date = stmt.get("toDate")
        if not to_date:
            continue
        d = _iso_date(to_date)
        fifo_rows = list(stmt.iter("FIFOPerformanceSummaryUnderlying"))
        if not fifo_rows:
            continue
        by_date.setdefault(d, _blank_day(d))
        by_date[d]["realized"] = sum(float(el.get("totalRealizedPnl") or 0) for el in fifo_rows)

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
    NAV's own same-named fields). start_date is ISO 8601 (e.g.
    "2026-07-01"); omit it (or pass None) to fall back to
    PORTFOLIO_START_DATE."""
    token = os.getenv("QUERY_TOKEN")
    query_id = os.getenv("QUERY_ID")
    if not token or not query_id:
        sys.exit(
            "QUERY_TOKEN and QUERY_ID must be set in .env -- generate a token under IBKR "
            "Account Management > Reports > Flex Queries > Flex Web Service Configuration, "
            "and create/note the Query ID of your Flex Query."
        )

    print("Requesting Flex Query statement (IBKR generates it on demand, can take a while for a multi-day query)...")
    raw = IBApp().fetch_flex_query(token, query_id)
    if not raw:
        sys.exit("Flex Query request failed or returned nothing -- check QUERY_TOKEN/QUERY_ID and that the query is active")

    rows = _parse_portfolio_report(raw)
    if not rows:
        sys.exit(
            "Flex Query response had neither an Equity Summary nor a Change in NAV section -- "
            "check the query's sections in IBKR Account Management (see Results.xml / Results.csv for the expected shape)"
        )

    start_date = start_date or PORTFOLIO_START_DATE
    rows = [r for r in rows if r["date"] >= start_date]

    output = {"kind": "daily", "rows": rows}
    with open(PORTFOLIO_PERFORMANCE_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Wrote {PORTFOLIO_PERFORMANCE_FILE}: {len(rows)} day(s), {rows[0]['date']} to {rows[-1]['date']}")
    return output



def start_streaming(ranked_tickers):
    """Connects to IB Gateway, then opens one streaming market-data
    subscription batch — every currently held position first (never
    truncated: a P&L blind spot on your own holdings is worse than
    bumping into IBKR's market-data-line budget), then the highest-ranked
    remaining tickers up to MAX_STREAMED_SYMBOLS total — and subscribes to
    account position updates; runs ib_insync's event loop for the life of
    the process, so this call never returns. Meant to run in its own
    background thread."""
    asyncio.set_event_loop(asyncio.new_event_loop())

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

    app.connect()
    if not app.is_connected:
        print("Could not connect to IB Gateway; /api/stream will keep serving empty snapshots.")
        return

    # No explicit reqAccountUpdates() call needed: ib_insync's own
    # reqAccountUpdates docstring says connect()'s sync step already does
    # this at startup. (Its signature is also just `account: str = ""` in
    # this ib_insync version, not `(subscribe, account)` — an earlier
    # version of this call passed both and crashed with a TypeError.)

    # connect() calls reqMarketDataType(3) (delayed data), but that doesn't
    # take effect synchronously — a reqMktData fired too soon after can race
    # ahead of it, default to requesting live data instead, and fail with
    # error 10168 ("Delayed market data is not enabled") since this account
    # has no live subscription. on_position's reactive subscribe (below)
    # can fire within milliseconds of reqPositions() below returning
    # results, right into that gap, so give the market data type a moment
    # to land first — before reqPositions() is even called, not just before
    # our own batch further down.
    app.ib.sleep(1)

    # Positions live in IB Gateway, not sorted_screen.csv, so they're only
    # knowable once connected: ask for them and pump the event loop for a
    # couple seconds so the initial burst of on_position calls lands and
    # populates positions_by_ticker before we build the subscription list.
    app.ib.reqPositions()
    app.ib.sleep(2)
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

    qualified = app.ib.qualifyContracts(*contracts)
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
        history = app.get_price_history(held_tickers)
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

    # Own thread, own IB Gateway connection — see fetch_candlestick_history,
    # which unions all_tickers with its own top-CANDLESTICK_TOP_N pull.
    # Started here (not from main()) because all_tickers — every ticker this
    # process actually streams a price for, not just the ranked pool — only
    # exists once this function has built it.
    threading.Thread(target=fetch_candlestick_history, args=(all_tickers,), daemon=True).start()

    app.ib.run()


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
        if self.path == "/api/stream":
            self._handle_stream()
        elif self.path == "/api/last-prices":
            self._send_json(last_price_by_ticker)
        elif self.path == "/api/positions":
            self._send_json(positions_by_ticker)
        elif self.path == "/api/account":
            self._send_json(account_status)
        else:
            self.send_response(404)
            self.end_headers()

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
    # One-shot mode, not the streaming server — fetches, writes, exits.
    # `python ib_price_server.py performance [YYYY-MM-DD]`
    if len(sys.argv) > 1 and sys.argv[1] == "performance":
        start_date = sys.argv[2] if len(sys.argv) > 2 else None
        fetch_account_performance(start_date)
        return

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765

    # A candidate pool, not a guarantee — start_streaming trims this to
    # whatever's left of MAX_STREAMED_SYMBOLS after held positions (which
    # always get a slot) claim theirs.
    tickers = load_top_tickers(SORTED_SCREEN_CSV, MAX_STREAMED_SYMBOLS)
    if not tickers:
        sys.exit(f"No tickers found in {SORTED_SCREEN_CSV}; run main.py first")
    # Seed every candidate up front so the response shape is stable from
    # the first push, even before any ticks have arrived.
    last_price_by_ticker.update({t: {"last": None, "bid": None, "ask": None, "timestamp": None} for t in tickers})

    threading.Thread(target=start_streaming, args=(tickers,), daemon=True).start()
    threading.Thread(target=run_snapshot_loop, daemon=True).start()

    server = Server(("localhost", port), Handler)
    print(f"Serving /api/stream on http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
