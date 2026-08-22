"""
finra.py — FINRA equity short interest fetcher.

FINRA publishes consolidated (NYSE + Nasdaq + other US venues, not just
OTC despite the URL's own "otcmarket" path -- a legacy artifact from
before June 2021, when the file covered OTC-traded issues only) equity
short interest twice a month, one flat file per settlement date, as a
plain CDN download -- no API key, no account, no rate limit encountered
in practice. Kept as its own file, separate from sec_edgar.py, since it's
a different host/data source with its own settlement-date addressing
scheme -- same "separate download, separate file" precedent sec_edgar.py
itself follows relative to social_sentiment.py.

Settlement dates are nominally the 15th and the last calendar day of each
month, each shifted to the nearest earlier business day when it lands on
a weekend/holiday, and the file itself is published a few business days
after its own settlement date -- so "today's most recent nominal
settlement" isn't necessarily online yet. fetch_short_interest doesn't
try to replicate FINRA's holiday calendar exactly; it just probes a
handful of candidate dates around each nominal one, most-recent-first,
and uses the first that actually exists (see _find_latest_short_interest_csv).

Writes data/finra/short_interest.json: {ticker: {currentShortPositionQuantity,
previousShortPositionQuantity, changePercent, averageDailyVolumeQuantity,
daysToCoverQuantity, settlementDate}}. Overwrites the file wholesale each
run rather than merging across runs -- same reasoning as sec_edgar.py's
own THIRTEENF_FILE: a stale settlement's figures for a ticker outside the
current run would otherwise linger.
"""

import csv
import json
import os
from datetime import date, timedelta

import requests

DATA_DIR = "data"
FINRA_DIR = os.path.join(DATA_DIR, "finra")
os.makedirs(FINRA_DIR, exist_ok=True)

SHORT_INTEREST_FILE = os.path.join(FINRA_DIR, "short_interest.json")

# FINRA asks requesters to identify themselves in the same "contact
# string, not a credential" spirit as sec_edgar.py's own SEC_USER_AGENT.
FINRA_USER_AGENT = "ibkr_pe short-interest-research andrea.luzzi0@gmail.com"

# Pipe-delimited despite the .csv extension -- confirmed against a live
# download, not assumed. exchangeCode/marketClassCode columns (e.g. NYSE,
# NNM for Nasdaq National Market) confirm this is the full consolidated
# tape, not an OTC-only file.
SHORT_INTEREST_URL_TEMPLATE = "https://cdn.finra.org/equity/otcmarket/biweekly/shrt{date}.csv"


def _candidate_settlement_dates(today=None, lookback_months=3):
    """Plausible FINRA settlement dates to probe, most recent first --
    nominally the 15th and the last calendar day of each of the last
    lookback_months months. Each nominal date gets widened into itself
    plus the 3 days before it (a small window, not FINRA's real holiday
    calendar) so a weekend/holiday shift is still caught without hard-
    coding that schedule -- _find_latest_short_interest_csv tries each of
    these in order and uses the first that actually exists on FINRA's
    CDN, so an overly generous window here just costs a few extra HTTP
    HEAD-equivalent misses, never a wrong answer."""
    today = today or date.today()
    nominal = []
    month_start = today.replace(day=1)
    for _ in range(lookback_months + 1):
        next_month_start = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        last_day = next_month_start - timedelta(days=1)
        mid_month = month_start.replace(day=15)
        nominal += [last_day, mid_month]
        month_start = (month_start - timedelta(days=1)).replace(day=1)
    nominal = sorted({d for d in nominal if d <= today}, reverse=True)

    seen = set()
    result = []
    for base in nominal:
        for offset in range(4):
            candidate = base - timedelta(days=offset)
            if candidate <= today and candidate not in seen:
                seen.add(candidate)
                result.append(candidate)
    return result


def _find_latest_short_interest_csv():
    """Downloads (or reuses an already-downloaded copy of) the most
    recently PUBLISHED FINRA biweekly short interest file, trying
    _candidate_settlement_dates most-recent-first until one actually
    exists. Returns (settlement_date, local_csv_path), or (None, None) if
    nothing in the lookback window is available (e.g. FINRA changed the
    URL scheme). Each date tried is cached to disk under FINRA_DIR by its
    own settlement date, so a rerun for the same settlement never
    re-downloads it."""
    for settlement_date in _candidate_settlement_dates():
        date_str = settlement_date.strftime("%Y%m%d")
        local_path = os.path.join(FINRA_DIR, f"shrt{date_str}.csv")
        if os.path.exists(local_path):
            print(f"Using already-downloaded {local_path}", flush=True)
            return settlement_date, local_path

        url = SHORT_INTEREST_URL_TEMPLATE.format(date=date_str)
        try:
            resp = requests.get(url, headers={"User-Agent": FINRA_USER_AGENT}, timeout=30)
        except requests.RequestException as e:
            print(f"FINRA short interest: {url} failed ({e}), trying an earlier date...")
            continue
        if resp.status_code == 200 and resp.content:
            with open(local_path, "wb") as f:
                f.write(resp.content)
            print(f"Downloaded {url}", flush=True)
            return settlement_date, local_path

    return None, None


def _to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_short_interest(tickers):
    """Downloads FINRA's latest available biweekly settlement file (see
    _find_latest_short_interest_csv) and writes SHORT_INTEREST_FILE with
    just the rows matching `tickers` (symbolCode, FINRA's own ticker
    column -- an exact match, no name-fuzzing needed the way 13F requires,
    since short interest is filed against the issue itself). A ticker not
    in FINRA's file at all (e.g. genuinely zero reported short interest,
    or delisted/renamed since) is simply absent from the returned map --
    scoring.short_interest_rank already ranks a missing score worst, same
    treatment as every other factor's missing data.

    A single flat-file download covering every US equity at once (~22k
    rows), not one request per ticker -- same "bulk download, filter
    locally" shape as sec_edgar.fetch_13f_holdings, just without that
    function's name-matching problem."""
    settlement_date, local_path = _find_latest_short_interest_csv()
    if local_path is None:
        print("Could not find a recent FINRA short interest file -- tried the last few candidate settlement dates.")
        return {}

    wanted = set(tickers)
    result = {}
    with open(local_path, newline="") as f:
        reader = csv.DictReader(f, delimiter="|")
        for row in reader:
            symbol = row.get("symbolCode")
            if symbol not in wanted:
                continue
            result[symbol] = {
                "currentShortPositionQuantity": _to_int(row.get("currentShortPositionQuantity")),
                "previousShortPositionQuantity": _to_int(row.get("previousShortPositionQuantity")),
                "changePercent": _to_float(row.get("changePercent")),
                "averageDailyVolumeQuantity": _to_int(row.get("averageDailyVolumeQuantity")),
                "daysToCoverQuantity": _to_float(row.get("daysToCoverQuantity")),
                "settlementDate": row.get("settlementDate"),
            }

    with open(SHORT_INTEREST_FILE, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Wrote {SHORT_INTEREST_FILE} ({len(result)}/{len(tickers)} tickers matched, settlement {settlement_date})")
    return result
