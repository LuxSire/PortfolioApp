"""
IBApp.py — Interactive Brokers gateway wrapper.

Wraps ib_insync for connection/order/account management.
Core methods for ibkr_pe: get_ibkr_watchlist_tickers() and get_forward_pe().
"""

import asyncio
import html
import json
import logging
import math
import os
import re
import statistics
import sys
import threading
import time
import time as time_module
import xml.etree.ElementTree as ET
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

# Python 3.10+ no longer creates a default event loop automatically.
# ib_insync's eventkit dependency requires one to exist at import time.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import requests
import urllib3
import yfinance as yf
from curl_cffi import requests as curl_requests
from dateutil.parser import isoparse
from dotenv import load_dotenv
from ib_insync import (
    IB, ContFuture, ExecutionFilter, Forex, Future, Index,
    LimitOrder, MarketOrder, Stock, WshEventData, util,
)

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.getLogger("yfinance").setLevel(logging.WARNING)

TRADING_DAYS_PER_YEAR = 252
# 6.5-hour regular session (9:30-16:00 ET) x 252 trading days -- lets
# _regression_momentum annualize an hourly-bar series correctly instead
# of treating each hourly bar as if it were a full trading day.
TRADING_HOURS_PER_YEAR = 1638

# get_momentum returns two independent factors -- daily-timeframe
# momentum and hourly-timeframe mean reversion (see that method's
# docstring) -- each scored in scoring.py as its own 5% of the composite,
# rather than blended into one number with a weight here. Below these bar
# counts a regression is too thin to trust -- momentum falls back to the
# plain yfinance 1-month-daily calculation instead; mean_reversion has no
# fallback and is just left None. ~30 daily bars is roughly 6 weeks; ~30
# hourly bars is roughly a trading week.
MIN_DAILY_BARS_FOR_BLEND = 30
MIN_HOURLY_BARS_FOR_BLEND = 30


def _regression_momentum(closes, periods_per_year=TRADING_DAYS_PER_YEAR):
    """Annualized slope of an OLS regression on log(closes) (bar index vs.
    log price), scaled by the fit's R² and then divided by the annualized
    volatility of log returns — a Sharpe-style risk adjustment on top of
    the Clenow-style trend-quality one. R² penalizes a choppy fit to the
    trend line; the volatility term separately penalizes large swings
    even along a well-fitted trend (e.g. a steady but violently noisy
    climb), so the two catch different shapes of "noisy." Positive means
    a steady uptrend, negative a steady downtrend; magnitude shrinks
    toward 0 for flat or noisy price action regardless of net return.
    None if returns have zero variance (a flat or single-step price
    series) — the volatility denominator is 0 there, and the numerator is
    0 too (a flat series yields R²=0), so it's a real 0/0, not a signal.

    periods_per_year makes this frequency-agnostic: pass
    TRADING_DAYS_PER_YEAR (252) for daily bars, TRADING_HOURS_PER_YEAR
    (1638) for hourly bars — same formula, correctly annualized either
    way, so get_momentum can run it on both a 3-month daily series and a
    recent hourly series and blend the two."""
    n = len(closes)
    ys = [math.log(c) for c in closes]
    xs = list(range(n))
    mean_x = (n - 1) / 2
    mean_y = sum(ys) / n
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    sxx = sum((x - mean_x) ** 2 for x in xs)
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    annualized_slope = math.exp(slope * periods_per_year) - 1
    trend_quality = annualized_slope * r_squared

    log_returns = [ys[i] - ys[i - 1] for i in range(1, n)]
    mean_ret = sum(log_returns) / len(log_returns)
    variance = sum((r - mean_ret) ** 2 for r in log_returns) / (len(log_returns) - 1)
    annualized_vol = math.sqrt(variance * periods_per_year)
    return trend_quality / annualized_vol if annualized_vol > 0 else None


# get_news_article_async's HTML-body cleanup. Block-level tags are matched
# and replaced with a blank line FIRST, before the second pass strips
# every remaining tag outright -- doing it in one pass (strip everything
# straight to a single space) is what the original version of this did,
# and it ran every paragraph together into one unreadable wall of text
# instead of preserving the source's own paragraph breaks.
_HTML_PARA_BREAK_RE = re.compile(r"</p\s*>|<br\s*/?>|</div\s*>|</pre\s*>", re.I)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Some providers (confirmed: Dow Jones, not just the Briefing.com case
# this was originally written for) don't reliably set NewsArticle's own
# articleType to 1 for HTML bodies -- sniff the text itself as a second,
# more reliable signal rather than trusting that flag alone.
_LOOKS_LIKE_HTML_RE = re.compile(r"<[a-zA-Z/][^>]*>")


def _html_article_to_text(html_text):
    """Converts a news article's raw HTML body to readable plain text,
    keeping paragraph breaks as blank lines (see _HTML_PARA_BREAK_RE)
    instead of collapsing the whole article into one run-on block."""
    text = _HTML_PARA_BREAK_RE.sub("\n\n", html_text)
    text = _HTML_TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _eps_revision(current, baseline):
    """(current - baseline) / abs(baseline) -- how much a consensus EPS
    estimate has moved relative to some earlier snapshot of itself (see
    get_forward_pe's use against yfinance's get_eps_trend(), comparing
    "current" to "30daysAgo" for a given period). Positive means
    analysts have been raising the estimate (bullish revision trend),
    negative means cuts. None for a missing/NaN input or a zero baseline
    (nothing to compute a meaningful ratio against)."""
    if current is None or baseline is None:
        return None
    try:
        current = float(current)
        baseline = float(baseline)
    except (TypeError, ValueError):
        return None
    if math.isnan(current) or math.isnan(baseline) or baseline == 0:
        return None
    return (current - baseline) / abs(baseline)


def _eps_volatility(values):
    """stdev(values) / mean(|values|) -- see get_forward_pe's own use
    against yfinance's annual Diluted EPS series (scoring.py's
    eps_volatility_rank: low is better). Divides by the mean of the
    ABSOLUTE values rather than the plain signed mean deliberately --
    confirmed live that a plain coefficient of variation breaks down the
    moment annual EPS goes negative or crosses zero, which happens
    within just 4-5 years even for large, unremarkable names (e.g. Ford:
    -2.06, 1.46, 1.08, -0.49 across four recent annual prints) -- a
    near-zero or negative mean would either blow the ratio up or flip
    its sign in a way that makes "low is better" stop meaning what it
    should. None if fewer than 3 values (yfinance's annual statement
    endpoint caps out around 5 years of history to begin with -- its
    quarterly equivalent caps at the same 5 periods, just each covering
    3 months instead of 12 and picking up seasonality noise a real
    business can have nothing wrong with, so annual is what's actually
    used here despite not buying any more data points), or if the mean
    absolute value is zero (nothing to normalize against)."""
    if len(values) < 3:
        return None
    mean_abs = sum(abs(v) for v in values) / len(values)
    if mean_abs == 0:
        return None
    return statistics.stdev(values) / mean_abs


# operatingMargins is a ratio against a company's own trailing revenue --
# for a company whose revenue only recently went from near-zero to
# something real (e.g. an early-stage aerospace/biotech name shipping its
# first meaningful sales), that denominator being tiny makes the ratio
# explode to a mathematically-correct but practically-meaningless magnitude
# (names like TIPT/SLDP show operatingMargins over +2900% this way).
# scoring.rank_ascending is ordinal so a single extreme value doesn't
# distort *other* tickers' ranks, but it does let a pure base-effect
# artifact claim the single best (or worst) rank ahead of a company with a
# real, still-exceptional number -- clamping keeps that from happening
# while leaving every value inside the band untouched. revenueGrowth has
# the identical base-effect problem (JOBY once read +257,493% off $15K of
# trailing revenue) but is deliberately NOT clamped here -- see
# scoring.growth_rank's own GROWTH_CAP, which clamps only the value it
# ranks on, so the raw number stored here stays visible in the screener.
MARGIN_FLOOR = -3.0  # -300%
MARGIN_CAP = 2.0  # +200%; above this is essentially always a tiny-revenue artifact, not real margin


def _clamp(value, lo, hi):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value):
        return None
    return max(lo, min(hi, value))


class IBApp:

    IBKR_BASE_URL = "https://localhost:4001/v1/api"

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def __init__(self, DEBUG_MODE=False):
        logging.getLogger("ib_insync").setLevel(logging.WARNING)
        logging.getLogger("client").setLevel(logging.WARNING)
        self.price_update_callbacks = []
        self.mode = DEBUG_MODE
        self.account = os.getenv("IB_ACCOUNT", "")
        self.ib = IB()
        self.prices = {}
        self.price_history = {}
        self.is_connected = False
        self.last_price_update = None
        self.price_updates_received = 0
        self.market_data_available = True
        self.connection_attempts = 0
        self.max_connection_attempts = 3
        # Shared across every get_ib_historical_bars() call on this
        # instance, not reset per call — IB's historical-data pacing limit
        # is account-wide, so two calls back to back (e.g. hourly then
        # daily bars for the same tickers) draw from the same budget.
        self._historical_request_times = deque()

        self.ib.pendingTickersEvent += self.on_pending_tickers
        self.ib.errorEvent += self.on_error
        self.ib.orderStatusEvent += self.on_order_status

        self.data_dir = "Data"
        os.makedirs(self.data_dir, exist_ok=True)

    def connect(self, client_id=0):
        try:
            if not self.is_connected:
                if self.connection_attempts >= self.max_connection_attempts:
                    logging.error(
                        f"Failed to connect after {self.max_connection_attempts} attempts. Exiting..."
                    )
                    sys.exit(1)
                logging.info("Connecting to IB Gateway...")
                self.ib.connect("127.0.0.1", 4001, clientId=client_id)
                self.is_connected = True
                self.ib.reqMarketDataType(3)
                self.ib.reqPnL(self.account)
                self.connection_attempts = 0
                logging.info("Connected to IB Gateway")
        except Exception as e:
            self.connection_attempts += 1
            logging.error(f"Connection error: {e}", exc_info=True)
            self.is_connected = False

    def disconnect(self):
        if self.is_connected:
            self.ib.disconnect()
            self.is_connected = False
            logging.info("Disconnected from IB Gateway")

    # ------------------------------------------------------------------ #
    #  Event handlers                                                      #
    # ------------------------------------------------------------------ #

    def on_error(self, reqId, errorCode, errorString, contract):
        informational = {2100, 2101, 2102, 2103, 2104, 2105, 2106, 2107, 2108, 2158}
        suppressed = {10167}
        if errorCode in suppressed:
            pass
        elif errorCode in informational:
            logging.debug(f"IB Info {errorCode}: {errorString} (reqId: {reqId})")
        else:
            logging.error(f"IB Error {errorCode}: {errorString} (reqId: {reqId})")

    def on_order_status(self, trade):
        try:
            symbol = getattr(trade.contract, "symbol", "N/A")
            status = getattr(trade.orderStatus, "status", "N/A")
            action = getattr(trade.order, "action", "N/A")
            qty = getattr(trade.order, "totalQuantity", "N/A")
            msg = (
                f"Order status: symbol={symbol}, status={status}, "
                f"action={action}, totalQuantity={qty}"
            )
            lmt = getattr(trade.order, "lmtPrice", None)
            if lmt:
                msg += f", lmtPrice={lmt}"
            logging.info(msg)
        except Exception as e:
            logging.info(f"Order status update (error extracting details): {e}")

    def on_pending_tickers(self, tickers):
        for ticker in tickers:
            last = ticker.last
            bid = ticker.bid if ticker.bid and ticker.bid > 0 else last
            ask = ticker.ask if ticker.ask and ticker.ask > 0 else last
            if not last or math.isnan(last) or last <= 0:
                for alt in (bid, ask, ticker.close):
                    if alt and not math.isnan(alt) and alt > 0:
                        last = alt
                        break
            if last and not math.isnan(last) and last > 0:
                self.onPriceUpdate(ticker.contract, last, bid, ask)

    def onPriceUpdate(self, contract, price, bid=None, ask=None):
        try:
            symbol = contract.symbol
            timestamp = datetime.now()
            self.last_price_update = timestamp
            self.price_updates_received += 1
            self.market_data_available = True
            self.price_history.setdefault(symbol, {})[timestamp] = price
            print(f"\rPrice update — {symbol}: ${price:.2f}", end="", flush=True)
            for cb in self.price_update_callbacks:
                try:
                    cb(symbol, price, bid, ask)
                except Exception as e:
                    logging.error(f"Error in price update callback: {e}")
        except Exception as e:
            logging.error(f"Error in onPriceUpdate: {e}", exc_info=True)

    def add_price_update_callback(self, callback):
        self.price_update_callbacks.append(callback)

    # ------------------------------------------------------------------ #
    #  Account                                                             #
    # ------------------------------------------------------------------ #

    def get_available_funds(self):
        try:
            value = next(
                (v.value for v in self.ib.accountValues() if v.tag == "AvailableFunds"),
                None,
            )
            return float(value) if value is not None else 0.0
        except Exception as e:
            logging.error(f"Error fetching available funds: {e}", exc_info=True)
            return 0.0

    def cash(self):
        return next((v.value for v in self.ib.accountValues() if v.tag == "TotalCashValue"), None)

    def total_value(self):
        return next((v.value for v in self.ib.accountValues() if v.tag == "NetLiquidation"), None)

    def realized_pnl(self):
        return next((v.value for v in self.ib.accountValues() if v.tag == "RealizedPnL"), None)

    def unrealized_pnl(self):
        return next((v.value for v in self.ib.accountValues() if v.tag == "UnrealizedPnL"), None)

    def buying_power(self):
        return next((v.value for v in self.ib.accountValues() if v.tag == "BuyingPower"), None)

    def daily_pnl(self):
        pnls = self.ib.pnl(self.account)
        return sum(p.dailyPnL for p in pnls if not math.isnan(p.dailyPnL))

    def subscribe_position_pnl(self, con_id):
        """Subscribes to reqPnLSingle for one contract -- IBKR's own
        per-POSITION daily/unrealized/realized P&L (dailyPnL/unrealizedPnL/
        realizedPnL/position/value), not the account-wide aggregate
        daily_pnl()/reqPnL above already covers. Returns the live PnLSingle
        object ib_insync updates in place as ticks arrive (same "hold onto
        the object, read its current attributes any time" pattern
        reqMktData's Ticker objects use) -- callers should keep the
        returned reference rather than re-requesting it. Idempotent on
        IBKR's own side: re-subscribing the same (account, modelCode,
        conId) triple returns the existing subscription's object rather
        than opening a duplicate.

        Deliberately never cancelled by a position going flat (see
        ib_server.py's on_position) -- IBKR keeps reporting
        dailyPnL/realizedPnL for a conId that was fully closed out earlier
        today, position 0 and unrealizedPnL 0, for the rest of the
        session. That's what lets a same-day-closed position still show a
        real P&L instead of vanishing the moment shares hit 0."""
        return self.ib.reqPnLSingle(self.account, "", con_id)

    # Shared by format_account_status (a printed table for logging) and
    # get_account_status_dict (a plain {tag: value} dict for programmatic
    # use, e.g. ib_server.py's /api/stream) — same curated tags
    # either way.
    ACCOUNT_STATUS_TAGS = {
        "TotalCashValue", "NetLiquidation", "AvailableFunds",
        "ExcessLiquidity", "BuyingPower", "UnrealizedPnL",
        "RealizedPnL", "DailyPnL",
    }

    def get_account_status(self):
        try:
            return self.ib.accountValues()
        except Exception as e:
            logging.error(f"Error fetching account status: {e}", exc_info=True)
            return []

    def get_account_status_dict(self):
        """Returns {tag: value} for the same curated ACCOUNT_STATUS_TAGS
        format_account_status prints as a table, values coerced to float
        where possible — for callers that want the values themselves
        rather than a printed table."""
        try:
            account_values = self.ib.accountValues()
        except Exception as e:
            logging.error(f"Error fetching account status: {e}", exc_info=True)
            return {}
        result = {}
        for v in account_values:
            if v.tag not in self.ACCOUNT_STATUS_TAGS:
                continue
            try:
                result[v.tag] = float(v.value)
            except (TypeError, ValueError):
                result[v.tag] = v.value
        return result

    def format_account_status(self, account_values):
        rows = [v for v in account_values if v.tag in self.ACCOUNT_STATUS_TAGS]
        if not rows:
            return "Account Status: None"
        headers = ["tag", "value", "currency"]
        col_widths = [
            max(len(str(getattr(r, h, ""))) for r in rows + [type("H", (), {h: h for h in headers})()])
            for h in headers
        ]
        header_line = " | ".join(h.ljust(w) for h, w in zip(["Tag", "Value", "Currency"], col_widths))
        sep = "-+-".join("-" * w for w in col_widths)
        body = [
            " | ".join(str(getattr(r, h, "")).ljust(w) for h, w in zip(headers, col_widths))
            for r in rows
        ]
        return "\n".join([header_line, sep] + body + [sep, header_line])

    def log_account_status(self):
        try:
            table = self.format_account_status(self.ib.accountValues())
            logging.info("\nAccount Status:\n" + table)
        except Exception as e:
            logging.error(f"Error logging account status: {e}", exc_info=True)

    # ------------------------------------------------------------------ #
    #  Trades / Orders                                                     #
    # ------------------------------------------------------------------ #

    def get_open_trades(self):
        try:
            self.ib.reqAllOpenOrders()
            self.ib.sleep(0.5)
            return [t for t in self.ib.trades() if t.orderStatus.status not in ("Filled", "Cancelled")]
        except Exception as e:
            logging.error(f"Error fetching open trades: {e}", exc_info=True)
            return []

    async def get_open_orders_async(self):
        """Async-safe equivalent of get_open_trades, for a caller sharing
        this instance's IB Gateway connection with other concurrent work
        on the same event loop (see ib_server.py's refresh_open_
        orders). reqAllOpenOrdersAsync(), not the plain reqAllOpenOrders()
        get_open_trades above uses -- that sync wrapper calls
        loop.run_until_complete() internally, which raises "This event
        loop is already running" when called from a coroutine already
        executing on that same loop (confirmed live: every
        refresh_open_orders cycle logging exactly that RuntimeError,
        caught by this method's own try/except below and silently
        returning [] each time -- which was also why open orders never
        showed up in the Trades tab despite the order genuinely existing
        on IBKR's side). reqAllOpenOrdersAsync() returns the resolved
        list directly once IB Gateway responds, so there's no separate
        asyncio.sleep-then-read-the-cache step needed the way the old
        (broken) version had."""
        try:
            return await self.ib.reqAllOpenOrdersAsync()
        except Exception as e:
            logging.error(f"Error fetching open orders: {e}", exc_info=True)
            return []

    def get_past_trades(self, days=20):
        try:
            start = datetime.now() - timedelta(days=days)
            return self.ib.reqExecutions(
                ExecutionFilter(time=start.strftime("%Y%m%d-%H:%M:%S"))
            )
        except Exception as e:
            logging.error(f"Error fetching past trades: {e}", exc_info=True)
            return []

    async def get_today_executions_async(self):
        """Returns {symbol: {"qty": signed net shares traded today,
        "value": sum(signedQty * fillPrice), "realizedPnl": ...,
        "commission": ...}} from today's fills. qty/value are the raw
        ingredients a correct daily P&L needs to mark shares traded today
        at their own fill price instead of assuming the whole position
        was held since yesterday's close (which silently misprices any
        symbol traded intraday, e.g. a same-day buy/sell) -- aggregated
        per symbol from reqExecutionsAsync, which finds every execution
        IB has on record for the account today regardless of which client
        placed it or whether this connection was alive to see it live.

        realizedPnl/commission come from a DIFFERENT source, self.ib.trades()
        (this connection's own live-tracked Trade objects), not the fills
        above -- IB's commissionReport (the only source of realizedPNL) is
        a separate, unkeyed event stream that only reliably lands on a
        live-tracked fill; a fill sourced from reqExecutionsAsync's
        historical query is left holding an empty, all-zero
        CommissionReport() by ib_insync itself (see wrapper.py's
        execDetails/commissionReport: commission reports get matched
        against fills already in the live cache, with no guarantee one
        arrives before a historical query's own response finishes).
        Verified live: summing realizedPnl here across every symbol traded
        today matched the account's own RealizedPnL figure (see
        ACCOUNT_STATUS_TAGS) exactly. None (not 0) for a symbol
        reqExecutionsAsync found today but this connection wasn't alive to
        see fill live (e.g. the server restarted partway through the day)
        -- a real 0 (a share-adding trade that hasn't closed anything yet)
        is a meaningful reading, not the same as "unknown," so the two
        stay distinct.

        "Today" is midnight in this machine's local timezone, same
        convention the rest of this file uses for daily boundaries --
        computed timezone-aware (not naive) specifically so it can also be
        compared directly against fill.time below, which ib_insync always
        reports as UTC-aware.

        Async so callers already sharing this instance's IB Gateway
        connection with other concurrent work (live ticks, snapshot
        polling) can await it without blocking that connection's event
        loop, unlike get_past_trades' blocking reqExecutions."""
        start_of_day = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            fills = await self.ib.reqExecutionsAsync(
                ExecutionFilter(time=start_of_day.strftime("%Y%m%d-%H:%M:%S"))
            )
        except Exception as e:
            logging.error(f"Error fetching today's executions: {e}", exc_info=True)
            return {}

        trades = {}
        for fill in fills:
            symbol = fill.contract.symbol
            ex = fill.execution
            signed_qty = ex.shares if ex.side == "BOT" else -ex.shares
            entry = trades.setdefault(
                symbol, {"qty": 0.0, "value": 0.0, "realizedPnl": None, "commission": None}
            )
            entry["qty"] += signed_qty
            entry["value"] += signed_qty * ex.price

        for trade in self.ib.trades():
            symbol = trade.contract.symbol
            if symbol not in trades:
                continue
            for live_fill in trade.fills:
                if live_fill.time < start_of_day:
                    continue
                cr = live_fill.commissionReport
                entry = trades[symbol]
                entry["realizedPnl"] = (entry["realizedPnl"] or 0.0) + (getattr(cr, "realizedPNL", 0.0) or 0.0)
                entry["commission"] = (entry["commission"] or 0.0) + (getattr(cr, "commission", 0.0) or 0.0)
        return trades

    def get_orders_and_trades(self):
        try:
            trades = self.ib.trades()
            open_trades = [t for t in trades if t.orderStatus.status not in ("Filled", "Cancelled")]

            def _fill_price(trade):
                if trade.fills and hasattr(trade.fills[0], "price"):
                    return trade.fills[0].price
                p = getattr(trade.orderStatus, "avgFillPrice", None)
                return p if p not in (None, 0, 0.0) else None

            return {
                "open_orders": [
                    {
                        "symbol": o.contract.symbol,
                        "action": o.order.action,
                        "quantity": o.order.totalQuantity,
                        "order_type": o.order.orderType,
                        "status": o.orderStatus.status,
                        "price": getattr(o.order, "lmtPrice", None) or None,
                    }
                    for o in open_trades
                ],
                "trades": [
                    {
                        "symbol": t.contract.symbol,
                        "action": t.order.action,
                        "quantity": t.order.totalQuantity,
                        "order_type": t.order.orderType,
                        "status": t.orderStatus.status,
                        "filled": t.orderStatus.filled,
                        "price": _fill_price(t),
                    }
                    for t in trades
                ],
            }
        except Exception as e:
            logging.error(f"Error fetching orders/trades: {e}", exc_info=True)
            return {}

    def format_orders_and_trades(self, orders_and_trades):
        def _table(items, headers):
            if not items:
                return "None"
            widths = [
                max(len(str(r.get(h, ""))) for r in items + [{h: h}])
                for h in headers
            ]
            header = " | ".join(h.ljust(w) for h, w in zip(headers, widths))
            sep = "-+-".join("-" * w for w in widths)
            rows = [
                " | ".join(str(r.get(h, "")).ljust(w) for h, w in zip(headers, widths))
                for r in items
            ]
            return "\n".join([header, sep] + rows + [sep, header])

        open_orders = orders_and_trades.get("open_orders", [])
        trades = sorted(orders_and_trades.get("trades", []), key=lambda x: x.get("symbol", ""))
        return "\n".join([
            "Open Orders:\n" + _table(open_orders, ["symbol", "action", "quantity", "order_type", "status", "price"]),
            "Trades:\n" + _table(trades, ["symbol", "action", "quantity", "order_type", "status", "filled", "price"]),
        ])

    def place_order(self, asset, action, quantity, order_type="MKT", limit_price=None, transmit=False):
        try:
            contract = self.make_contract(getattr(asset, "symbol", None), asset)
            outside_rth = True
            if order_type == "MKT":
                order = MarketOrder(action, quantity, transmit=transmit, outsideRth=outside_rth)
            elif order_type == "LMT" and limit_price is not None:
                order = LimitOrder(action, quantity, limit_price, transmit=transmit, outsideRth=outside_rth)
            else:
                logging.error(f"Invalid order_type={order_type} or missing limit_price")
                return None

            trade = self.ib.placeOrder(contract, order)
            if trade is None:
                logging.error(f"placeOrder returned None for {getattr(asset, 'symbol', '?')}")
                return None

            def on_status(t):
                try:
                    s = t.orderStatus
                    logging.info(
                        f"Order {getattr(asset, 'symbol', '?')}: {s.status}, "
                        f"filled={s.filled}, remaining={s.remaining}"
                    )
                    if s.status == "Filled":
                        cur = getattr(asset, "qty", 0) or 0
                        asset.qty = cur + s.filled if action == "BUY" else cur - s.filled
                        asset.qty_remaining = s.remaining
                except Exception as e:
                    logging.error(f"Order callback error: {e}", exc_info=True)

            trade.filledEvent += on_status
            logging.info(f"Placed {order_type} {action} x{quantity} {getattr(asset, 'symbol', '?')}")
            return trade
        except Exception as e:
            logging.error(f"Error placing order: {e}", exc_info=True)
            return None

    def update_order(self, order_id, new_limit_price, transmit=True):
        try:
            try:
                new_price = float(new_limit_price)
            except Exception:
                logging.error(f"Non-numeric limit price: {new_limit_price}")
                return None

            threshold = 0.0005
            for t in self.ib.trades():
                order_obj = getattr(t, "order", None)
                if not order_obj:
                    continue
                oid = getattr(order_obj, "orderId", None) or getattr(order_obj, "permId", None)
                if str(oid) != str(order_id):
                    continue

                contract = getattr(t, "contract", None)
                if contract is None:
                    return None

                old_price = None
                try:
                    old_price = float(getattr(order_obj, "lmtPrice", None) or 0) or None
                except Exception:
                    pass

                if old_price and old_price > 0 and abs(new_price - old_price) / old_price <= threshold:
                    return t

                if str(getattr(order_obj, "orderType", "")).upper() == "LMT":
                    order_obj.lmtPrice = new_price
                    order_obj.transmit = transmit
                    return self.ib.placeOrder(contract, order_obj)

                new_order = LimitOrder(
                    getattr(order_obj, "action", None),
                    getattr(order_obj, "totalQuantity", None),
                    new_price,
                    transmit=transmit,
                )
                try:
                    new_order.orderId = order_obj.orderId
                except Exception:
                    pass
                return self.ib.placeOrder(contract, new_order)

            logging.warning(f"Order {order_id} not found")
            return None
        except Exception as e:
            logging.error(f"Error updating order {order_id}: {e}", exc_info=True)
            return None

    def cancel_order(self, order_id):
        try:
            for t in self.ib.trades():
                order_obj = getattr(t, "order", None)
                if not order_obj:
                    continue
                oid = getattr(order_obj, "orderId", None) or getattr(order_obj, "permId", None)
                if str(oid) != str(order_id):
                    continue
                status = str(getattr(t.orderStatus, "status", "")).lower()
                if status in ("cancelled", "filled", "inactive"):
                    return False
                if status == "pendingcancel":
                    return True
                remaining = getattr(t.orderStatus, "remaining", None)
                if remaining is not None and float(remaining) == 0.0:
                    return False
                self.ib.cancelOrder(order_obj)
                self.ib.sleep(0.1)
                return True
            logging.warning(f"Order {order_id} not found")
            return False
        except Exception as e:
            logging.error(f"Error cancelling order {order_id}: {e}", exc_info=True)
            return False

    # ------------------------------------------------------------------ #
    #  Contract helpers                                                    #
    # ------------------------------------------------------------------ #

    def assign_qualified_contract(self, contract):
        try:
            qualified = self.ib.qualifyContracts(contract)
            if qualified:
                return qualified[0]
            logging.warning(f"Could not qualify contract for {getattr(contract, 'symbol', '?')}")
            return None
        except Exception as e:
            logging.error(f"Error qualifying contract: {e}", exc_info=True)
            return None

    def make_contract(self, symbol, asset=None, default_future_exchange="CME"):
        try:
            atype = getattr(asset, "type", "").upper() if asset else ""
            if atype == "CRYPTO":
                from ib_insync import Crypto
                return Crypto(symbol, "PAXOS", "USD")
            if atype in ("FX", "FOREX"):
                return Forex(symbol)
            if atype == "FUTURES":
                now = datetime.now()
                year = now.year if now.month < 6 else now.year + 1
                return Future(
                    symbol=symbol,
                    exchange=default_future_exchange,
                    currency="USD",
                    lastTradeDateOrContractMonth=f"{year}06",
                )
            if atype == "INDEX":
                return Index(
                    symbol=symbol,
                    exchange=getattr(asset, "market", "SMART"),
                    currency=getattr(asset, "currency", "USD"),
                )
            market = getattr(asset, "market", "SMART") if asset else "SMART"
            currency = getattr(asset, "currency", "USD") if asset else "USD"
            isin = getattr(asset, "isin", None) if asset else None
            if isin:
                return Stock(symbol, market, currency, secIdType="ISIN", secId=isin)
            return Stock(symbol, market, currency)
        except Exception as e:
            logging.debug(f"Error creating contract for {symbol}: {e}")
            return None

    # ------------------------------------------------------------------ #
    #  Market data status                                                  #
    # ------------------------------------------------------------------ #

    def check_market_data_status(self):
        if self.last_price_update is None:
            logging.warning("No live market data received yet")
            self.market_data_available = False
        else:
            age = datetime.now() - self.last_price_update
            if age > timedelta(minutes=5):
                logging.warning(f"No price updates for {age.total_seconds():.0f}s")
                self.market_data_available = False
            else:
                logging.info(
                    f"Market data OK: {self.price_updates_received} updates, "
                    f"last {age.total_seconds():.0f}s ago"
                )
                self.market_data_available = True

    def get_market_data_type_description(self):
        return "Delayed (Type 3) — 15-20 minute delayed data"

    def start_background_processing(self):
        def loop():
            while self.is_connected:
                try:
                    logging.debug("Processing IB events...")
                except Exception as e:
                    if self.is_connected:
                        logging.error(f"Background loop error: {e}")
                    break
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        return t

    # ------------------------------------------------------------------ #
    #  Flex query                                                          #
    # ------------------------------------------------------------------ #

    def fetch_flex_query(
        self,
        token,
        query_id,
        base_url="https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService",
        max_statement_attempts=6,
        poll_interval=15,
    ):
        """Returns the raw Flex Query report (XML or CSV bytes) from a live
        SendRequest/GetStatement round trip against IBKR's Flex Web
        Service, or b"" if that round trip didn't produce one -- every
        failure branch below prints the actual reason (bad token/query id,
        IBKR error code, timeout) so a silent b"" here is always
        explainable from the console, not just inferred from the caller
        falling back to a local export."""
        params = {"t": token, "q": query_id, "v": "3"}
        headers = {"User-Agent": "IB Flex Query Client/1.0"}
        try:
            resp = requests.get(
                f"{base_url}.SendRequest", params=params, headers=headers, timeout=30
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            status = root.findtext("Status")
            if status != "Success":
                print(
                    f"Flex query SendRequest failed: status={status!r} "
                    f"errorCode={root.findtext('ErrorCode')!r} "
                    f"errorMessage={root.findtext('ErrorMessage')!r} "
                    "-- check QUERY_TOKEN/QUERY_ID in .env"
                )
                return b""
            ref_code = root.findtext("ReferenceCode")
            if not ref_code:
                print(f"Flex query SendRequest returned Success but no ReferenceCode: {resp.text!r}")
                return b""
            print(f"Flex query SendRequest accepted (reference {ref_code}); IBKR is generating the statement...")
            time.sleep(10)
            # A statement not yet ready comes back as a small
            # <FlexStatementResponse> status wrapper (Status=Warn,
            # ErrorCode 1019 "Statement generation in progress") rather
            # than the real report -- confirmed happening in practice, not
            # hypothetical. That's a routine "not ready yet", distinct
            # from Status=Fail (a real failure, e.g. error 1001), so it's
            # worth a short poll loop instead of giving up on the first
            # attempt.
            for attempt in range(max_statement_attempts):
                get_resp = requests.get(
                    f"{base_url}.GetStatement",
                    params={"t": token, "q": ref_code, "v": "3"},
                    headers=headers,
                    timeout=60,
                    allow_redirects=True,
                )
                get_resp.raise_for_status()
                content = get_resp.content
                if content.lstrip().startswith(b"<FlexStatementResponse"):
                    try:
                        wrapper = ET.fromstring(content)
                        wrapper_status = wrapper.findtext("Status")
                    except ET.ParseError:
                        wrapper_status = None
                        wrapper = None
                    if wrapper_status == "Warn" and attempt < max_statement_attempts - 1:
                        print(
                            f"Flex query statement still generating "
                            f"(attempt {attempt + 1}/{max_statement_attempts}), "
                            f"polling again in {poll_interval}s..."
                        )
                        time.sleep(poll_interval)
                        continue
                    # A real failure (Status=Fail) or a Warn we've run out of
                    # attempts for -- either way this wrapper is an error
                    # notice, not a report, so it must not be handed back to
                    # the caller as if it were one (that used to happen here).
                    print(
                        f"Flex query GetStatement did not return a report: "
                        f"status={wrapper_status!r} "
                        f"errorCode={(wrapper.findtext('ErrorCode') if wrapper is not None else None)!r} "
                        f"errorMessage={(wrapper.findtext('ErrorMessage') if wrapper is not None else None)!r}"
                    )
                    return b""
                print(f"Flex query GetStatement succeeded: downloaded {len(content)} bytes")
                return content
            return b""
        except requests.RequestException as e:
            print(f"Flex query request failed: {e}")
            return b""

    # ------------------------------------------------------------------ #
    #  Core project methods                                                #
    # ------------------------------------------------------------------ #

    def get_ibkr_watchlist_tickers(self, watchlist_names=None):
        """
        Returns a sorted, deduplicated list of ticker symbols from IBKR watchlists
        via the Client Portal Gateway REST API (https://localhost:4001).

        watchlist_names: optional list of watchlist names to filter by.
        """
        # curl_cffi uses libcurl and tolerates IB Gateway's self-signed cert
        # and non-standard TLS teardown that breaks Python 3.12+ ssl module.
        session = curl_requests.Session()
        try:
            resp = session.get(
                f"{self.IBKR_BASE_URL}/iserver/watchlists", verify=False, timeout=10
            )
            resp.raise_for_status()
            watchlists = resp.json().get("data", {}).get("watchlists", [])

            tickers = set()
            for wl in watchlists:
                name = wl.get("name")
                if watchlist_names and name not in watchlist_names:
                    continue
                wl_resp = session.get(
                    f"{self.IBKR_BASE_URL}/iserver/watchlist",
                    params={"id": wl["id"]},
                    verify=False,
                    timeout=10,
                )
                wl_resp.raise_for_status()
                for inst in wl_resp.json().get("instruments", []):
                    symbol = inst.get("ticker") or inst.get("name")
                    if not symbol:
                        continue
                    symbol = symbol.strip().upper()
                    if "." in symbol:
                        continue  # skip FX pairs / foreign suffixes
                    tickers.add(symbol)

            return sorted(tickers)
        except Exception as e:
            logging.error(f"Error fetching IBKR watchlist tickers: {e}", exc_info=True)
            return []

    def get_forward_pe(self, tickers, usa_only=True, max_workers=2, raw_out=None, country_overrides=None):
        """
        Returns {ticker: {name, forwardPE, forwardEps, trailingPE, trailingPS,
        pegRatio, priceToFCF, enterpriseToEbitda, beta, debtToEquity, quickRatio,
        currentRatio, shortRatio, shortPercentOfFloat, price, sector,
        country, targetMeanPrice, targetHighPrice, targetLowPrice,
        numberOfAnalystOpinions, revenueGrowth, returnOnEquity,
        profitMargins, operatingMargins, recommendationKey,
        recommendationMean, earningsTimestampStart, epsRevision0y,
        epsRevision1y, epsVolatility, lastDownload}} from
        Yahoo Finance. When
        usa_only=True, only US-domiciled companies are returned.

        epsRevision0y/epsRevision1y are the consensus EPS estimate's
        30-day revision trend (see _eps_revision) for the current ("0y")
        and next ("+1y") fiscal year, from yfinance's get_eps_trend() --
        a separate request per ticker alongside get_info(), best-effort:
        a failure there doesn't fail the whole ticker (unlike get_info()
        itself, which is what the retry loop below is really for), it
        just leaves both fields None.

        epsVolatility (see _eps_volatility) is stdev/mean(|value|) of the
        last (up to) 5 years' annual Diluted EPS, from yfinance's
        income_stmt -- another separate, best-effort request alongside
        get_info()/get_eps_trend(), same "failure just leaves it None"
        contract. Low is better (scoring.eps_volatility_rank):
        earnings that swing wildly quarter to quarter are a real quality/
        predictability signal distinct from epsRevision0y/1y's own
        forward-looking estimate-trend one.

        operatingMargins is clamped (see MARGIN_FLOOR / MARGIN_CAP above) to
        keep a near-zero-revenue name's base-effect artifact from reading as
        a genuine extreme. revenueGrowth is stored uncapped -- the raw
        number is what the screener displays; scoring.growth_rank applies
        GROWTH_CAP itself, only for ranking, so the actual figure stays
        visible while the composite score still isn't distorted by it.

        country_overrides, if given, is a collection of tickers to keep
        regardless of what yfinance reports for `country` -- e.g. CRSP is
        Swiss-incorporated despite being an ordinary US-listed, US-focused
        security, which usa_only would otherwise silently drop. A manual
        correction, same spirit as main.py's sector overrides, just applied
        here since usa_only filters a symbol out of the results entirely
        rather than just mislabeling a field on it.

        If raw_out is a dict, the complete, unfiltered yfinance `info` payload
        for every ticker (including ones later dropped by usa_only) is stored
        into it as {ticker: {**info, lastDownload}}, for exploring fields not
        yet curated above.
        """
        now = datetime.now().isoformat(timespec="seconds")

        def fetch(symbol):
            print(f"Fetching {symbol}...")
            for attempt in range(3):
                try:
                    yt = yf.Ticker(symbol)
                    info = yt.get_info()
                    if raw_out is not None:
                        raw_out[symbol] = {**info, "lastDownload": now}
                    market_cap = info.get("marketCap")
                    free_cashflow = info.get("freeCashflow")
                    price_to_fcf = (
                        market_cap / free_cashflow
                        if market_cap and free_cashflow
                        else None
                    )
                    price = info.get("currentPrice") or info.get("regularMarketPrice")
                    trailing_eps = info.get("trailingEps")
                    # Yahoo's trailingPE is None whenever trailing EPS is negative
                    # (it suppresses negative P/E instead of reporting it); compute
                    # it ourselves so a company's negative earnings stay visible.
                    trailing_pe = info.get("trailingPE")
                    if trailing_pe is None and price and trailing_eps:
                        trailing_pe = price / trailing_eps
                    eps_revision_0y = None
                    eps_revision_1y = None
                    try:
                        eps_trend = yt.get_eps_trend()
                        if eps_trend is not None and not eps_trend.empty:
                            if "0y" in eps_trend.index:
                                eps_revision_0y = _eps_revision(
                                    eps_trend.loc["0y", "current"], eps_trend.loc["0y", "30daysAgo"]
                                )
                            if "+1y" in eps_trend.index:
                                eps_revision_1y = _eps_revision(
                                    eps_trend.loc["+1y", "current"], eps_trend.loc["+1y", "30daysAgo"]
                                )
                    except Exception as e:
                        logging.info(f"get_forward_pe: {symbol} eps trend failed: {e}")
                    eps_volatility = None
                    try:
                        annual = yt.income_stmt
                        if annual is not None and "Diluted EPS" in annual.index:
                            eps_volatility = _eps_volatility(annual.loc["Diluted EPS"].dropna().tolist())
                    except Exception as e:
                        logging.info(f"get_forward_pe: {symbol} annual EPS volatility failed: {e}")
                    return symbol, {
                        "name": info.get("shortName"),
                        "forwardPE": info.get("forwardPE"),
                        "forwardEps": info.get("forwardEps"),
                        "trailingPE": trailing_pe,
                        "trailingPS": info.get("priceToSalesTrailing12Months"),
                        "pegRatio": info.get("pegRatio"),
                        "priceToFCF": price_to_fcf,
                        "debtToEquity": info.get("debtToEquity"),
                        "quickRatio": info.get("quickRatio"),
                        "currentRatio": info.get("currentRatio"),
                        "shortRatio": info.get("shortRatio"),
                        "shortPercentOfFloat": info.get("shortPercentOfFloat"),
                        "price": price,
                        # "industry" (e.g. "Semiconductors") rather than the
                        # coarser "sector" (e.g. "Technology"), so peer groups
                        # in the sector-relative scoring are meaningful. Falls
                        # back to this live value only when symbols.json has no
                        # curated override for the ticker (see main.load_sectors).
                        "sector": info.get("industry"),
                        "country": info.get("country"),
                        "targetMeanPrice": info.get("targetMeanPrice"),
                        "targetHighPrice": info.get("targetHighPrice"),
                        "targetLowPrice": info.get("targetLowPrice"),
                        "numberOfAnalystOpinions": info.get("numberOfAnalystOpinions"),
                        "revenueGrowth": info.get("revenueGrowth"),
                        "returnOnEquity": info.get("returnOnEquity"),
                        "profitMargins": info.get("profitMargins"),
                        "operatingMargins": _clamp(info.get("operatingMargins"), MARGIN_FLOOR, MARGIN_CAP),
                        "enterpriseToEbitda": info.get("enterpriseToEbitda"),
                        "beta": info.get("beta"),
                        "recommendationKey": info.get("recommendationKey"),
                        "recommendationMean": info.get("recommendationMean"),
                        "earningsTimestampStart": info.get("earningsTimestampStart"),
                        "epsRevision0y": eps_revision_0y,
                        "epsRevision1y": eps_revision_1y,
                        "epsVolatility": eps_volatility,
                        "lastDownload": now,
                    }
                except Exception as e:
                    if attempt == 2:
                        return symbol, {"error": str(e)}
                    time.sleep(1.5)

        results = {}
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(fetch, s): s for s in tickers}
            for sym, data in (f.result() for f in as_completed(futures)):
                results[sym] = data

        if usa_only:
            overrides = country_overrides or ()
            results = {
                s: d for s, d in results.items() if s in overrides or d.get("country") == "United States"
            }

        return results

    def get_eps_volatility(self, tickers, max_workers=2):
        """Returns {ticker: epsVolatility} (see _eps_volatility) from
        yfinance's income_stmt alone -- a lighter fetch than
        get_forward_pe's full get_info()+get_eps_trend()+income_stmt
        bundle, for refreshing just this one figure (e.g. right after
        adding/changing the factor itself, or backfilling tickers a
        FRESH_HOURS-skipped `all`/`prices` run left without it) without
        redoing the whole forward-PE/momentum pipeline. Same best-
        effort-per-ticker contract as get_forward_pe's own EPS-
        volatility fetch -- a ticker that fails still comes back with
        None, not omitted from the returned dict, since the caller is
        merging this into rows that already exist."""

        def fetch(symbol):
            print(f"Fetching {symbol}...")
            try:
                annual = yf.Ticker(symbol).income_stmt
                if annual is not None and "Diluted EPS" in annual.index:
                    return symbol, _eps_volatility(annual.loc["Diluted EPS"].dropna().tolist())
            except Exception as e:
                logging.info(f"get_eps_volatility: {symbol} failed: {e}")
            return symbol, None

        results = {}
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(fetch, s): s for s in tickers}
            for sym, value in (f.result() for f in as_completed(futures)):
                results[sym] = value
        return results

    def get_price_history(self, tickers, max_workers=2):
        """
        Returns {ticker: [{date, close}, ...]} — the trailing ~1 month of
        daily closes from Yahoo Finance, same shape as get_momentum's
        history_out. Standalone (no momentum score computed) for callers
        that just need a recent-price fallback for tickers outside the
        regular screener pipeline — e.g. ib_server.py uses this for
        IBKR positions on tickers the screener never fetches (not in
        symbols.json) or whose live IB quote is unavailable (missing
        market data permissions for that ticker's exchange).
        """

        def fetch(symbol):
            for attempt in range(3):
                try:
                    hist = yf.Ticker(symbol).history(period="1mo")
                    closes = hist["Close"].dropna()
                    return symbol, [
                        {"date": ts.strftime("%Y-%m-%d"), "close": round(c, 4)}
                        for ts, c in closes.items()
                    ]
                except Exception:
                    if attempt == 2:
                        return symbol, []
                    time.sleep(1.5)

        history = {}
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(fetch, s): s for s in tickers}
            for sym, series in (f.result() for f in as_completed(futures)):
                history[sym] = series

        return history

    def get_ib_historical_bars(self, tickers, duration, bar_size, max_requests_per_10min=200):
        """
        Returns {ticker: [{date, open, high, low, close, volume}, ...]}
        from IB Gateway's own historical data (reqHistoricalData) — unlike
        get_price_history (Yahoo Finance), this uses the live connection
        this instance is already connected with, so it needs self.connect()
        called first. duration/bar_size are IB's own strings, e.g.
        duration="1 M" bar_size="1 hour", or duration="3 M" bar_size="1 day".

        IB's documented historical-data pacing limit is ~60 requests per
        rolling 10-minute window, account-wide, not per-contract -- but
        confirmed live against this account well above that (100 tickers
        in 5 minutes with no pacing violations), so the default here is
        200/10min rather than the conservative textbook figure. Stays
        under max_requests_per_10min via a sliding window (shared across
        every call on this instance — see self._historical_request_times
        — since the limit is account-wide, not scoped to one call),
        sleeping as needed if it's ever actually reached. For a large
        ticker list this can still take a while — meant to run in its own
        thread, not to block anything else that needs this connection's
        event loop pumped in the meantime.
        """
        contracts = [self.make_contract(t) for t in tickers]
        contracts = [c for c in contracts if c is not None]
        qualified = self.ib.qualifyContracts(*contracts)
        logging.info(f"get_ib_historical_bars: qualified {len(qualified)}/{len(tickers)} contracts")

        history = {}
        request_times = self._historical_request_times
        for i, c in enumerate(qualified):
            now = time.monotonic()
            while request_times and now - request_times[0] > 600:
                request_times.popleft()
            if len(request_times) >= max_requests_per_10min:
                sleep_for = 600 - (now - request_times[0]) + 1
                logging.info(
                    f"get_ib_historical_bars: pacing limit reached ({i}/{len(qualified)} done), "
                    f"sleeping {sleep_for:.0f}s"
                )
                self.ib.sleep(sleep_for)
            request_times.append(time.monotonic())

            try:
                bars = self.ib.reqHistoricalData(
                    c,
                    endDateTime="",
                    durationStr=duration,
                    barSizeSetting=bar_size,
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=1,
                )
                history[c.symbol] = [
                    {
                        "date": (b.date.strftime("%Y-%m-%d %H:%M:%S") if hasattr(b.date, "strftime") else str(b.date)),
                        "open": b.open,
                        "high": b.high,
                        "low": b.low,
                        "close": b.close,
                        "volume": b.volume,
                    }
                    for b in bars
                ]
            except Exception as e:
                logging.error(f"get_ib_historical_bars: {c.symbol} failed: {e}")
                history[c.symbol] = []

        return history

    async def get_ib_historical_bars_async(self, tickers, duration, bar_size, max_requests_per_10min=200, on_ticker=None):
        """Async twin of get_ib_historical_bars, same pacing/request logic,
        for callers that share this instance's IB Gateway connection with
        other concurrent work (live ticks, snapshot polling) on the same
        asyncio event loop — awaiting reqHistoricalDataAsync and sleeping
        via asyncio.sleep (instead of the sync method's blocking
        self.ib.reqHistoricalData/self.ib.sleep) means the many minutes or
        hours a large ticker list takes never stalls anything else sharing
        that loop, the way calling the sync version from a coroutine would.

        on_ticker, if given, is called with each symbol right before its
        request goes out -- e.g. ib_server.py's on-demand refresh
        endpoints thread this through to append a log line per ticker for
        the Dataset tab's Run button, without this method needing to know
        anything about that job-log mechanism itself."""
        contracts = [self.make_contract(t) for t in tickers]
        contracts = [c for c in contracts if c is not None]
        qualified = await self.ib.qualifyContractsAsync(*contracts)
        logging.info(f"get_ib_historical_bars_async: qualified {len(qualified)}/{len(tickers)} contracts")

        history = {}
        request_times = self._historical_request_times
        for i, c in enumerate(qualified):
            now = time.monotonic()
            while request_times and now - request_times[0] > 600:
                request_times.popleft()
            if len(request_times) >= max_requests_per_10min:
                sleep_for = 600 - (now - request_times[0]) + 1
                logging.info(
                    f"get_ib_historical_bars_async: pacing limit reached ({i}/{len(qualified)} done), "
                    f"sleeping {sleep_for:.0f}s"
                )
                await asyncio.sleep(sleep_for)
            request_times.append(time.monotonic())

            if on_ticker:
                on_ticker(c.symbol)
            try:
                bars = await self.ib.reqHistoricalDataAsync(
                    c,
                    endDateTime="",
                    durationStr=duration,
                    barSizeSetting=bar_size,
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=1,
                )
                history[c.symbol] = [
                    {
                        "date": (b.date.strftime("%Y-%m-%d %H:%M:%S") if hasattr(b.date, "strftime") else str(b.date)),
                        "open": b.open,
                        "high": b.high,
                        "low": b.low,
                        "close": b.close,
                        "volume": b.volume,
                    }
                    for b in bars
                ]
            except Exception as e:
                logging.error(f"get_ib_historical_bars_async: {c.symbol} failed: {e}")
                history[c.symbol] = []

        return history

    async def _req_historical_news_async(self, conId, provider_codes, start, end, total_results, timeout=20):
        """Same request ib.reqHistoricalNewsAsync makes, but with a
        configurable timeout instead of that method's hard-coded 4
        seconds (see ib_insync/ib.py) -- confirmed live that 4s is too
        short for this account/query shape (reqHistoricalNewsAsync:
        Timeout on almost every call), while the request itself
        eventually does complete given more time."""
        reqId = self.ib.client.getReqId()
        future = self.ib.wrapper.startReq(reqId)
        start_fmt = util.formatIBDatetime(start)
        end_fmt = util.formatIBDatetime(end)
        self.ib.client.reqHistoricalNews(reqId, conId, provider_codes, start_fmt, end_fmt, total_results, [])
        try:
            await asyncio.wait_for(future, timeout)
            return future.result()
        except asyncio.TimeoutError:
            logging.error(f"_req_historical_news_async: timeout after {timeout}s for conId={conId}")
            return None

    async def get_news_headlines_async(self, tickers, days=3, max_headlines_per_ticker=100, max_requests_per_10min=55):
        """Returns {ticker: [{articleId, time, provider, headline}, ...]} —
        every headline from the last `days` days per ticker (bounded by
        max_headlines_per_ticker, IB's per-request cap), from whichever
        news providers this account is subscribed to (see
        reqNewsProviders — confirmed for this account: Briefing.com and
        Dow Jones only, no Reuters/Benzinga/Zacks). Headlines only, no
        article body (see reqNewsArticle for that; not used here).

        articleId (e.g. "DJ-N$1f1057ff") is IB's own provider-qualified,
        per-article identifier — the caller (ib_server.py's
        news_loop) uses it to dedupe when merging into a rolling-window
        news.json across repeated fetches.

        IB doesn't publish an explicit pacing limit for reqHistoricalNews,
        unlike reqHistoricalData's documented ~60-requests-per-10-minutes
        rule. This errs conservative with its own INDEPENDENT sliding-
        window budget (not shared with get_ib_historical_bars_async's
        self._historical_request_times — different IB request category,
        no evidence they draw from the same account-wide bucket),
        defaulting to that same 55-requests-per-10-minutes assumption in
        the absence of documented guidance otherwise. At that rate, a
        full pass over a large ticker list takes multiple hours; see
        ib_server.py's news_loop for how that's handled (a
        continuous background cycle that saves progress as it goes, not
        a one-shot that only writes at the very end)."""
        providers = await self.ib.reqNewsProvidersAsync()
        provider_codes = "+".join(p.code for p in providers)
        if not provider_codes:
            logging.error("get_news_headlines_async: no subscribed news providers on this account")
            return {}

        contracts = [self.make_contract(t) for t in tickers]
        contracts = [c for c in contracts if c is not None]
        qualified = await self.ib.qualifyContractsAsync(*contracts)
        logging.info(f"get_news_headlines_async: qualified {len(qualified)}/{len(tickers)} contracts")

        start = datetime.utcnow() - timedelta(days=days)
        end = datetime.utcnow()

        results = {}
        request_times = deque()
        for i, c in enumerate(qualified):
            now = time.monotonic()
            while request_times and now - request_times[0] > 600:
                request_times.popleft()
            if len(request_times) >= max_requests_per_10min:
                sleep_for = 600 - (now - request_times[0]) + 1
                logging.info(
                    f"get_news_headlines_async: pacing limit reached ({i}/{len(qualified)} done), "
                    f"sleeping {sleep_for:.0f}s"
                )
                await asyncio.sleep(sleep_for)
            request_times.append(time.monotonic())

            try:
                headlines = await self._req_historical_news_async(
                    c.conId, provider_codes, start, end, max_headlines_per_ticker
                )
                results[c.symbol] = [
                    {
                        "articleId": h.articleId,
                        "time": h.time.isoformat() if hasattr(h.time, "isoformat") else str(h.time),
                        "provider": h.providerCode,
                        "headline": h.headline,
                    }
                    for h in (headlines or [])
                ]
            except Exception as e:
                logging.error(f"get_news_headlines_async: {c.symbol} failed: {e}")
                results[c.symbol] = []

        return results

    async def get_news_article_async(self, provider_code, article_id, timeout=15):
        """Fetches ONE article's full body via reqNewsArticle — the
        lazy, on-demand counterpart to get_news_headlines_async's
        headline-only bulk fetch. Never called across the whole
        screener; ib_server.py's GET /api/news/article calls this
        only when a user actually expands a specific headline in the
        UI, since body text is both far larger than a headline and
        subject to the same undocumented per-account news pacing limits
        get_news_headlines_async already has to budget for.

        Returns plain text, or None if the article has no body / the
        request times out. articleType 0 is plain text already;
        articleType 1 (documented for Briefing.com, but seen from Dow
        Jones here too) is HTML -- converted to readable plain text (see
        _html_article_to_text) since callers only ever display this as
        prose, not render it as markup. articleType alone isn't trusted
        to catch every HTML body (see _LOOKS_LIKE_HTML_RE's own
        comment), so the text itself is sniffed for tags too."""
        try:
            article = await asyncio.wait_for(
                self.ib.reqNewsArticleAsync(provider_code, article_id), timeout
            )
        except asyncio.TimeoutError:
            logging.error(f"get_news_article_async: timeout for {provider_code}/{article_id}")
            return None
        except Exception as e:
            logging.error(f"get_news_article_async: {provider_code}/{article_id} failed: {e}")
            return None
        text = getattr(article, "articleText", None) if article else None
        if not text:
            return None
        if getattr(article, "articleType", 0) == 1 or _LOOKS_LIKE_HTML_RE.search(text):
            text = _html_article_to_text(text)
        return text

    def get_momentum(
        self, tickers, max_workers=2, history_out=None, daily_3mo_by_ticker=None, hourly_by_ticker=None
    ):
        """
        Returns {ticker: {"momentum": ..., "mean_reversion": ...}} -- two
        independent factors (scoring.py's momentum_rank and
        mean_reversion_rank each score their own 5% of the composite),
        not blended into one number the way this used to work.

        momentum: regression-momentum (see _regression_momentum) on the
        3-month IB Gateway daily series (daily_3mo_by_ticker -- see
        ib_server.py's price_history_daily_3mo.json) when it has at
        least MIN_DAILY_BARS_FOR_BLEND bars, else the original
        single-source fallback: a Yahoo Finance daily history fetch
        (period=1mo), regression-momentum on that alone. None only if
        neither source has enough closes.

        mean_reversion: the SAME regression-momentum formula as `momentum`
        above, just measured on the hourly IB Gateway series
        (hourly_by_ticker -- see ib_server.py's
        price_history_hourly.json, only fetched for CANDLESTICK_TOP_N
        ranked/held tickers, not the whole universe) instead of the daily
        one, when it has at least MIN_HOURLY_BARS_FOR_BLEND bars -- a
        second, short-term-timeframe momentum reading, same sign
        convention as `momentum` (positive = hourly uptrend, negative =
        hourly downtrend), used as an entry-timing signal rather than a
        second momentum vote: a stock already trending up hard on THIS
        timeframe is, read against a long entry, a stock you'd be chasing
        (bad timing) rather than catching early, and read against a long
        that's already held, a stock that may be due for a pullback
        (worth a look) -- see RecommendationsView.tsx's
        meanReversionOkForLong/meanReversionOkForShort and
        buildCloseReasons for exactly how each side reads this sign.
        There's no fallback data source for hourly bars the way momentum
        has one (the yfinance call here is daily-only), so this is None
        for any ticker IB Gateway hasn't fetched hourly candlesticks for.

        The yfinance fetch happens for every ticker regardless of which
        source `momentum` ends up using, since other parts of this app
        (price_history.json) depend on history_out covering the whole
        universe, not just the ones missing IB data. If history_out is a
        dict, that fetch's daily close series is stored into it as
        {ticker: [{date, close}, ...]}.
        """
        daily_3mo_by_ticker = daily_3mo_by_ticker or {}
        hourly_by_ticker = hourly_by_ticker or {}

        def ib_daily_momentum(symbol):
            daily_series = daily_3mo_by_ticker.get(symbol)
            if not daily_series or len(daily_series) < MIN_DAILY_BARS_FOR_BLEND:
                return None
            return _regression_momentum([b["close"] for b in daily_series], TRADING_DAYS_PER_YEAR)

        def hourly_mean_reversion(symbol):
            hourly_series = hourly_by_ticker.get(symbol)
            if not hourly_series or len(hourly_series) < MIN_HOURLY_BARS_FOR_BLEND:
                return None
            return _regression_momentum([b["close"] for b in hourly_series], TRADING_HOURS_PER_YEAR)

        def fetch(symbol):
            print(f"Fetching {symbol}...")
            ib_mom = ib_daily_momentum(symbol)
            reversion = hourly_mean_reversion(symbol)
            for attempt in range(3):
                try:
                    hist = yf.Ticker(symbol).history(period="1mo")
                    closes = hist["Close"].dropna()
                    if history_out is not None:
                        history_out[symbol] = [
                            {"date": ts.strftime("%Y-%m-%d"), "close": round(c, 4)}
                            for ts, c in closes.items()
                        ]
                    if ib_mom is not None:
                        return symbol, {"momentum": ib_mom, "mean_reversion": reversion}
                    if len(closes) < 5:
                        return symbol, {"momentum": None, "mean_reversion": reversion}
                    return symbol, {
                        "momentum": _regression_momentum(closes.tolist()),
                        "mean_reversion": reversion,
                    }
                except Exception:
                    if attempt == 2:
                        # yfinance failed, but an IB-based momentum
                        # reading doesn't need it -- only actually give
                        # up on momentum if we have neither; mean_reversion
                        # never depended on yfinance at all.
                        return symbol, {"momentum": ib_mom, "mean_reversion": reversion}
                    time.sleep(1.5)

        momentum = {}
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(fetch, s): s for s in tickers}
            for sym, result in (f.result() for f in as_completed(futures)):
                momentum[sym] = result

        return momentum

    def get_momentum_from_disk(self, tickers, daily_3mo_by_ticker=None, hourly_by_ticker=None, yfinance_history_by_ticker=None):
        """Same {ticker: {"momentum": ..., "mean_reversion": ...}} shape as
        get_momentum, but purely from files already on disk -- no live
        yfinance fetch, and no IB Gateway connection needed either despite
        living on this class (self.ib is never touched). For main.py's
        rescore(): recomputing momentum after IB Gateway's own daily/
        hourly bars (see ibprices/ibhprices) have been refreshed
        shouldn't require a live fetch when nothing about the ticker
        universe or its yfinance closes has actually changed since the
        last real fetch.

        momentum: IB's daily_3mo_by_ticker (see get_momentum's own
        docstring) where it has at least MIN_DAILY_BARS_FOR_BLEND bars,
        else regression-momentum on yfinance_history_by_ticker's already-
        cached daily closes (price_history.json -- the same series
        get_momentum's own yfinance fetch would otherwise capture into
        history_out) if that has at least 5 closes, else None -- same
        two-source precedence as get_momentum, just reading the fallback
        from disk instead of fetching it fresh.

        mean_reversion: identical to get_momentum -- IB's
        hourly_by_ticker only, no fallback source exists for it either
        way, fetched live or not."""
        daily_3mo_by_ticker = daily_3mo_by_ticker or {}
        hourly_by_ticker = hourly_by_ticker or {}
        yfinance_history_by_ticker = yfinance_history_by_ticker or {}

        def ib_daily_momentum(symbol):
            daily_series = daily_3mo_by_ticker.get(symbol)
            if not daily_series or len(daily_series) < MIN_DAILY_BARS_FOR_BLEND:
                return None
            return _regression_momentum([b["close"] for b in daily_series], TRADING_DAYS_PER_YEAR)

        def hourly_mean_reversion(symbol):
            hourly_series = hourly_by_ticker.get(symbol)
            if not hourly_series or len(hourly_series) < MIN_HOURLY_BARS_FOR_BLEND:
                return None
            return _regression_momentum([b["close"] for b in hourly_series], TRADING_HOURS_PER_YEAR)

        momentum = {}
        for symbol in tickers:
            ib_mom = ib_daily_momentum(symbol)
            reversion = hourly_mean_reversion(symbol)
            if ib_mom is not None:
                momentum[symbol] = {"momentum": ib_mom, "mean_reversion": reversion}
                continue
            cached = yfinance_history_by_ticker.get(symbol)
            closes = [b["close"] for b in cached] if cached else []
            mom = _regression_momentum(closes) if len(closes) >= 5 else None
            momentum[symbol] = {"momentum": mom, "mean_reversion": reversion}

        return momentum
