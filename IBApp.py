"""
IBApp.py — Interactive Brokers gateway wrapper.

Wraps ib_insync for connection/order/account management.
Core methods for ibkr_pe: get_ibkr_watchlist_tickers() and get_forward_pe().
"""

import asyncio
import json
import logging
import math
import os
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


def _regression_momentum(closes):
    """Annualized slope of an OLS regression on log(closes) (trading-day
    index vs. log price), scaled by the fit's R² and then divided by the
    annualized volatility of daily log returns — a Sharpe-style
    risk adjustment on top of the Clenow-style trend-quality one. R²
    penalizes a choppy fit to the trend line; the volatility term
    separately penalizes large day-to-day swings even along a
    well-fitted trend (e.g. a steady but violently noisy climb), so the
    two catch different shapes of "noisy." Positive means a steady
    uptrend, negative a steady downtrend; magnitude shrinks toward 0 for
    flat or noisy price action regardless of net return. None if daily
    returns have zero variance (a flat or single-step price series) —
    the volatility denominator is 0 there, and the numerator is 0 too
    (a flat series yields R²=0), so it's a real 0/0, not a signal."""
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
    annualized_slope = math.exp(slope * TRADING_DAYS_PER_YEAR) - 1
    trend_quality = annualized_slope * r_squared

    daily_log_returns = [ys[i] - ys[i - 1] for i in range(1, n)]
    mean_ret = sum(daily_log_returns) / len(daily_log_returns)
    variance = sum((r - mean_ret) ** 2 for r in daily_log_returns) / (len(daily_log_returns) - 1)
    annualized_vol = math.sqrt(variance * TRADING_DAYS_PER_YEAR)
    return trend_quality / annualized_vol if annualized_vol > 0 else None


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

    # Shared by format_account_status (a printed table for logging) and
    # get_account_status_dict (a plain {tag: value} dict for programmatic
    # use, e.g. ib_price_server.py's /api/stream) — same curated tags
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

    def get_past_trades(self, days=20):
        try:
            start = datetime.now() - timedelta(days=days)
            return self.ib.reqExecutions(
                ExecutionFilter(time=start.strftime("%Y%m%d-%H:%M:%S"))
            )
        except Exception as e:
            logging.error(f"Error fetching past trades: {e}", exc_info=True)
            return []

    def get_filled_trades(self):
        try:
            self.ib.reqAllOpenOrders()
            self.ib.sleep(0.5)
            enriched = []
            for trade in (t for t in self.ib.trades() if t.orderStatus.status == "Filled"):
                total_pnl = total_commission = total_shares = total_value = 0.0
                for fill in trade.fills:
                    cr = fill.commissionReport
                    total_pnl += getattr(cr, "realizedPNL", 0.0) or 0.0
                    total_commission += getattr(cr, "commission", 0.0) or 0.0
                    shares = getattr(getattr(fill, "execution", None), "shares", 0.0) or 0.0
                    price = getattr(getattr(fill, "execution", None), "price", 0.0) or 0.0
                    total_shares += shares
                    total_value += shares * price
                avg_price = total_value / total_shares if total_shares > 0 else 0.0
                enriched.append({
                    "trade": trade,
                    "realizedPnl": total_pnl,
                    "commission": total_commission,
                    "shares": total_shares,
                    "AvgPrice": avg_price,
                })
            return enriched
        except Exception as e:
            logging.error(f"Error fetching filled trades: {e}", exc_info=True)
            return []

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
    ):
        params = {"t": token, "q": query_id, "v": "3"}
        headers = {"User-Agent": "IB Flex Query Client/1.0"}
        try:
            resp = requests.get(
                f"{base_url}.SendRequest", params=params, headers=headers, timeout=30
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            if root.findtext("Status") != "Success":
                return b""
            ref_code = root.findtext("ReferenceCode")
            if not ref_code:
                return b""
            time.sleep(10)
            get_resp = requests.get(
                f"{base_url}.GetStatement",
                params={"t": token, "q": ref_code, "v": "3"},
                headers=headers,
                timeout=60,
                allow_redirects=True,
            )
            get_resp.raise_for_status()
            return get_resp.content
        except requests.RequestException as e:
            logging.error(f"Flex query error: {e}", exc_info=True)
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

    def get_forward_pe(self, tickers, usa_only=True, max_workers=2, raw_out=None):
        """
        Returns {ticker: {name, forwardPE, forwardEps, trailingPE, pegRatio,
        priceToFCF, debtToEquity, quickRatio, currentRatio, price, sector,
        country, targetMeanPrice, targetHighPrice, targetLowPrice,
        numberOfAnalystOpinions, revenueGrowth, recommendationKey,
        earningsTimestampStart, lastDownload}} from Yahoo Finance. When
        usa_only=True, only US-domiciled companies are returned.

        If raw_out is a dict, the complete, unfiltered yfinance `info` payload
        for every ticker (including ones later dropped by usa_only) is stored
        into it as {ticker: {**info, lastDownload}}, for exploring fields not
        yet curated above.
        """
        now = datetime.now().isoformat(timespec="seconds")

        def fetch(symbol):
            for attempt in range(3):
                try:
                    info = yf.Ticker(symbol).get_info()
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
                    return symbol, {
                        "name": info.get("shortName"),
                        "forwardPE": info.get("forwardPE"),
                        "forwardEps": info.get("forwardEps"),
                        "trailingPE": trailing_pe,
                        "pegRatio": info.get("pegRatio"),
                        "priceToFCF": price_to_fcf,
                        "debtToEquity": info.get("debtToEquity"),
                        "quickRatio": info.get("quickRatio"),
                        "currentRatio": info.get("currentRatio"),
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
                        "recommendationKey": info.get("recommendationKey"),
                        "earningsTimestampStart": info.get("earningsTimestampStart"),
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
            results = {s: d for s, d in results.items() if d.get("country") == "United States"}

        return results

    def get_price_history(self, tickers, max_workers=2):
        """
        Returns {ticker: [{date, close}, ...]} — the trailing ~1 month of
        daily closes from Yahoo Finance, same shape as get_momentum's
        history_out. Standalone (no momentum score computed) for callers
        that just need a recent-price fallback for tickers outside the
        regular screener pipeline — e.g. ib_price_server.py uses this for
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

    def get_ib_historical_bars(self, tickers, duration, bar_size, max_requests_per_10min=55):
        """
        Returns {ticker: [{date, open, high, low, close, volume}, ...]}
        from IB Gateway's own historical data (reqHistoricalData) — unlike
        get_price_history (Yahoo Finance), this uses the live connection
        this instance is already connected with, so it needs self.connect()
        called first. duration/bar_size are IB's own strings, e.g.
        duration="1 M" bar_size="1 hour", or duration="3 M" bar_size="1 day".

        IB enforces a historical-data pacing limit (roughly 60 requests per
        rolling 10-minute window, account-wide, not per-contract); this
        stays under max_requests_per_10min via a sliding window (shared
        across every call on this instance — see self._historical_request_
        times — since the limit is account-wide, not scoped to one call),
        sleeping as needed. For a large ticker list this can take many
        minutes — meant to run in its own thread, not to block anything
        else that needs this connection's event loop pumped in the
        meantime.
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

    def get_momentum(self, tickers, max_workers=2, history_out=None):
        """
        Returns {ticker: momentum_score}, sourced from a single Yahoo Finance
        daily history fetch per ticker (period=1mo). The score is a
        regression-slope momentum (Clenow-style): the annualized slope of a
        linear regression on log(close) over the trailing ~1 month, scaled by
        the fit's R². A steady uptrend scores higher than a choppy one with
        the same net return, since R² shrinks toward 0 for noisy/gappy price
        action. Tickers with fewer than 5 closes are omitted.

        If history_out is a dict, the daily close series from that same
        fetch is stored into it as {ticker: [{date, close}, ...]} — lets
        callers that already need this fetch for momentum persist it for
        charting too, without a second network round-trip.
        """

        def fetch(symbol):
            for attempt in range(3):
                try:
                    hist = yf.Ticker(symbol).history(period="1mo")
                    closes = hist["Close"].dropna()
                    if history_out is not None:
                        history_out[symbol] = [
                            {"date": ts.strftime("%Y-%m-%d"), "close": round(c, 4)}
                            for ts, c in closes.items()
                        ]
                    if len(closes) < 5:
                        return symbol, None
                    return symbol, _regression_momentum(closes.tolist())
                except Exception:
                    if attempt == 2:
                        return symbol, None
                    time.sleep(1.5)

        momentum = {}
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(fetch, s): s for s in tickers}
            for sym, score in (f.result() for f in as_completed(futures)):
                momentum[sym] = score

        return momentum
