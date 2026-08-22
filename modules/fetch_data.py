"""
fetch_data.py

Two data-fetch functions:
  - get_ibkr_watchlist_tickers(): pulls stock tickers out of your IBKR watchlists via
    the Client Portal Gateway's local REST API.
  - get_forward_pe(tickers): pulls forward/trailing P/E from Yahoo Finance, filtered
    to USA-domiciled companies only.

Requires:
    pip install requests yfinance

For get_ibkr_watchlist_tickers(): the IBKR Client Portal Gateway must be running and
logged in (open https://localhost:4001 in a browser and authenticate first). This
talks to the Gateway's own REST API, not the Claude/MCP connector.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3
import yfinance as yf

# The Gateway uses a self-signed cert by default.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

IBKR_BASE_URL = "https://localhost:4001/v1/api"


def get_ibkr_watchlist_tickers(watchlist_names=None, base_url=IBKR_BASE_URL):
    """
    Returns a deduplicated, sorted list of ticker symbols from your IBKR watchlists.

    watchlist_names: optional list of watchlist names to restrict to (e.g. ["Buys", "Tech"]).
                      If None, every non-empty watchlist is included.
    """
    resp = requests.get(f"{base_url}/iserver/watchlists", verify=False, timeout=10)
    resp.raise_for_status()
    watchlists = resp.json().get("data", {}).get("watchlists", [])

    tickers = set()
    for wl in watchlists:
        name = wl.get("name")
        if watchlist_names and name not in watchlist_names:
            continue

        wl_resp = requests.get(
            f"{base_url}/iserver/watchlist",
            params={"id": wl["id"]},
            verify=False,
            timeout=10,
        )
        wl_resp.raise_for_status()
        instruments = wl_resp.json().get("instruments", [])

        for inst in instruments:
            symbol = inst.get("ticker") or inst.get("name")
            if not symbol:
                continue
            symbol = symbol.strip().upper()
            if "." in symbol:
                # Skips FX pairs (e.g. "USD.CHF") and foreign-suffixed tickers.
                continue
            tickers.add(symbol)

    return sorted(tickers)


def get_forward_pe(tickers, usa_only=True, max_workers=6):
    """
    Returns {ticker: {name, forwardPE, trailingPE, price, country}} sourced from Yahoo Finance.
    When usa_only=True, tickers whose Yahoo-reported country isn't "United States" are dropped
    (this excludes foreign ADRs/dual-listings like BABA, TCEHY, RIO, ABBN, etc.).
    """

    def fetch(symbol):
        for attempt in range(3):
            try:
                info = yf.Ticker(symbol).get_info()
                return symbol, {
                    "name": info.get("shortName"),
                    "forwardPE": info.get("forwardPE"),
                    "trailingPE": info.get("trailingPE"),
                    "price": info.get("currentPrice") or info.get("regularMarketPrice"),
                    "country": info.get("country"),
                }
            except Exception as e:
                if attempt == 2:
                    return symbol, {"error": str(e)}
                time.sleep(1.5)

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch, s): s for s in tickers}
        for fut in as_completed(futures):
            sym, data = fut.result()
            results[sym] = data

    if usa_only:
        results = {
            sym: d for sym, d in results.items() if d.get("country") == "United States"
        }

    return results
