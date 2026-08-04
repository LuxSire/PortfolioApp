"""
social_sentiment.py — StockTwits social sentiment fetcher.

Pulls each ticker's most recent public message stream from StockTwits'
unofficial, unauthenticated API and derives a bullish/bearish score from the
sentiment tags on those messages. Kept as its own download, separate from
the yfinance-based fetch in IBApp.py, since it's a different (unofficial,
rate-limited) data source that can fail independently of the main pipeline.

Writes social_sentiment.json: {ticker: {bullish, bearish, tagged, total,
score, lastDownload}}, merged with whatever was already in the file so
tickers outside the current batch keep their last known score.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from curl_cffi.requests.exceptions import RequestException

# Plain `requests` gets a 403 from StockTwits' Cloudflare bot-check; curl_cffi
# impersonating a real Chrome TLS/JA3 fingerprint gets through without one
# (same reason IBApp.py reaches for it elsewhere in this project).
from curl_cffi import requests

STREAM_URL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
SENTIMENT_FILE = "social_sentiment.json"

# StockTwits' unauthenticated API is rate-limited per IP; keep concurrency
# low and back off on 429 instead of hammering it.
MAX_WORKERS = 2
RETRY_SLEEP_SECONDS = 5


def _sentiment_tag(message):
    entities = message.get("entities") or {}
    sentiment = entities.get("sentiment") or {}
    return sentiment.get("basic")


def fetch_symbol_sentiment(symbol):
    """Returns {bullish, bearish, tagged, total, score, lastDownload} for one
    ticker, or None if the fetch failed after retries. score is
    (bullish - bearish) / tagged, in [-1, 1]; None when none of the returned
    messages carry a sentiment tag."""
    now = datetime.now().isoformat(timespec="seconds")
    for attempt in range(3):
        try:
            resp = requests.get(
                STREAM_URL.format(symbol=symbol), impersonate="chrome", timeout=10
            )
            if resp.status_code == 429:
                time.sleep(RETRY_SLEEP_SECONDS)
                continue
            resp.raise_for_status()
            messages = resp.json().get("messages", [])
            tags = [_sentiment_tag(m) for m in messages]
            bullish = tags.count("Bullish")
            bearish = tags.count("Bearish")
            tagged = bullish + bearish
            return {
                "bullish": bullish,
                "bearish": bearish,
                "tagged": tagged,
                "total": len(messages),
                "score": (bullish - bearish) / tagged if tagged else None,
                "lastDownload": now,
            }
        except RequestException:
            if attempt == 2:
                return None
            time.sleep(1.5)
    return None


def fetch_social_sentiment(tickers, max_workers=MAX_WORKERS):
    """Fetches StockTwits sentiment for tickers and merges the result into
    social_sentiment.json. Returns the {ticker: data} dict fetched this
    call (not the full merged file)."""
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_symbol_sentiment, t): t for t in tickers}
        for fut in as_completed(futures):
            symbol = futures[fut]
            data = fut.result()
            if data is not None:
                results[symbol] = data

    try:
        with open(SENTIMENT_FILE) as f:
            existing = json.load(f)
    except FileNotFoundError:
        existing = {}
    existing.update(results)

    with open(SENTIMENT_FILE, "w") as f:
        json.dump(existing, f, indent=2)
    print(f"Wrote {SENTIMENT_FILE} ({len(results)}/{len(tickers)} tickers fetched)")

    return results
