"""
sec_edgar.py — SEC EDGAR fetchers (Form 4 insider transactions, 13F
institutional holdings, XBRL company facts).

EDGAR's own JSON/XML endpoints (data.sec.gov, sec.gov/Archives) are
completely free and need no API key or account -- the only requirement is
a descriptive User-Agent header (SEC_USER_AGENT below) identifying the
requester, and staying under the ~10 req/sec fair-access rate limit (see
_rate_limit). Kept as its own file, separate from social_sentiment.py,
since it's a different data source with its own rate limit and its own
CIK-based addressing scheme -- same "separate download, separate file"
precedent as social_sentiment.py vs. the yfinance fetch in IBApp.py.

Form 4, XBRL company facts, and 13F institutional holdings are all
implemented -- see each section's own docstring below for how it differs
(13F in particular works nothing like the other two; see its intro).

Writes data/sec/form4/insider_transactions.json: {ticker: [{accessionNumber,
filingDate, insiderName, officerTitle, isOfficer, isDirector,
isTenPercentOwner, transactions: [{date, code, shares, pricePerShare,
acquiredDisposed, sharesOwnedAfter}, ...]}, ...]}, merged with whatever
was already in the file so tickers outside the current batch keep their
last known filings. Non-derivative transactions only (ordinary common
stock buys/sells) -- derivative transactions (options, RSUs) aren't
parsed yet.

Writes data/sec/xbrl/company_facts.json: {ticker: {metric: [{end, val, fy},
...]}} -- one entry per fiscal year-end, oldest to newest, for whichever of
XBRL_CONCEPTS' six metrics (revenue, netIncome, operatingIncome,
totalAssets, stockholdersEquity, dilutedEPS) this issuer has annual (10-K)
data for -- see fetch_xbrl_facts_for_ticker. Multi-year history straight
from XBRL, not yfinance's single-point-in-time TTM snapshot (raw_data.json).
Merged across runs, same as insider_transactions.json above.

Writes data/sec/13f/institutional_holdings.json: {ticker: {totalValueUsd,
totalShares, putShares, callShares, holderCount, pctShareChangeQoQ}} --
see fetch_13f_holdings. Unlike Form 4/XBRL, 13F is filed BY institutional
managers ABOUT what they hold, not by the issuer itself, so there's no
per-ticker CIK to query -- this instead downloads SEC's own quarterly
bulk dataset (every manager's holdings, one ~90MB file, TWO of them --
current quarter and prior, to compute pctShareChangeQoQ) and matches rows
to tickers by normalized company name, the only available free join key
(the bulk data has no ticker field, only CUSIP, and there's no free
ticker->CUSIP crosswalk). Overwrites the file wholesale each run rather
than merging, since a stale prior quarter's figures for a ticker outside
the current run would otherwise linger. Both quarterly bulk .zip files
are kept on disk under data/sec/ directly (not data/sec/13f/ -- explicit
instruction) rather than discarded after one use, and reused as-is by a
later run that asks for the same quarter again instead of re-downloading.

INFOTABLE.tsv's PUTCALL column marks a row as an options position (blank
for plain common stock, "Put" or "Call" otherwise) -- totalValueUsd/
totalShares/holderCount/pctShareChangeQoQ only ever count blank (common
stock) rows, i.e. actual share ownership; Put/Call rows are aggregated
separately into putShares/callShares and never blended into the common-
stock totals. Note the 13F schema has no long/short indicator -- a bought
put and a written (sold) put both file identically as PUTCALL="Put" with
a positive SSHPRNAMT, so putShares/callShares mean "disclosed option
exposure of this type," not "bullish" or "bearish" on their own.

Also writes data/sec/13f/institutional_holders.json: {ticker: [{name,
valueUsd, shares, putShares, callShares}, ...]}, sorted by valueUsd
descending and capped to MAX_HOLDERS_PER_TICKER -- the actual named
institutions (e.g. "BERKSHIRE HATHAWAY INC") holding each ticker as of
the CURRENT quarter only, resolved from the same bulk zip's
COVERPAGE.tsv (join key ACCESSION_NUMBER -> FILINGMANAGER_NAME) that
institutional_holdings.json's aggregate totals already come from -- no
extra download. valueUsd/shares here are common-stock-only, same as the
aggregate above; a filer with ONLY options exposure (no common stock) in
a ticker still gets a row, with valueUsd/shares at 0 and putShares/
callShares carrying its option position -- and since the table is
capped to MAX_HOLDERS_PER_TICKER by valueUsd (common-stock value)
descending, such an options-only filer can be pushed out of the top N
even with large option exposure.
"""

import csv
import io
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import requests

DATA_DIR = "data"
SEC_DIR = os.path.join(DATA_DIR, "sec")
FORM4_DIR = os.path.join(SEC_DIR, "form4")
THIRTEENF_DIR = os.path.join(SEC_DIR, "13f")
XBRL_DIR = os.path.join(SEC_DIR, "xbrl")
for _dir in (FORM4_DIR, THIRTEENF_DIR, XBRL_DIR):
    os.makedirs(_dir, exist_ok=True)

CIK_MAP_FILE = os.path.join(SEC_DIR, "company_tickers.json")
FORM4_FILE = os.path.join(FORM4_DIR, "insider_transactions.json")
XBRL_FACTS_FILE = os.path.join(XBRL_DIR, "company_facts.json")
THIRTEENF_FILE = os.path.join(THIRTEENF_DIR, "institutional_holdings.json")
# Separate file (not merged into THIRTEENF_FILE's per-ticker aggregate) --
# explicit instruction: named institutional holders, not just the
# aggregate totals THIRTEENF_FILE already carries. See fetch_13f_holdings.
THIRTEENF_HOLDERS_FILE = os.path.join(THIRTEENF_DIR, "institutional_holders.json")
# Per ticker, not a hard SEC limit -- some large-caps have hundreds of
# 13F filers; capped to the top N by position value to keep
# THIRTEENF_HOLDERS_FILE a reasonable size (a name/value/shares row per
# holder, not the full aggregate THIRTEENF_FILE already is).
MAX_HOLDERS_PER_TICKER = 15

# SEC asks every requester to self-identify; this isn't a credential, just
# a contact string SEC can use if a script misbehaves.
SEC_USER_AGENT = "ibkr_pe insider-transaction-research andrea.luzzi0@gmail.com"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
TICKER_CIK_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
XBRL_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
# No versioned/stable API for "give me the latest quarter" -- just this
# page, which lists each quarter's bulk dataset newest-first.
THIRTEENF_DATASETS_PAGE = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"

# Each logical metric maps to a fallback chain of us-gaap XBRL tags -- the
# first tag with USD data wins. Companies migrated tags over time (e.g. the
# ASC 606 revenue-recognition standard moved most issuers off the old
# "Revenues" tag around 2018 onto
# "RevenueFromContractWithCustomerExcludingAssessedTax" -- verified against
# AAPL's own real filings: "Revenues" data stops at fiscal 2018, the new tag
# picks up from there), so a single fixed tag name would silently go empty
# for years on one side of that migration.
XBRL_CONCEPTS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "netIncome": ["NetIncomeLoss"],
    "operatingIncome": ["OperatingIncomeLoss"],
    "totalAssets": ["Assets"],
    "stockholdersEquity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "dilutedEPS": ["EarningsPerShareDiluted"],
    # Weighted-average diluted share count -- an independent read on
    # sharesOutstanding, which yfinance's summary field gets wrong for
    # names with a recent split / ticker migration (confirmed live: it
    # had Gold.com at ~29M vs a real ~1.7B). A loss-maker's diluted count
    # collapses to basic (anti-dilutive), so the basic and combined tags
    # are in the fallback chain.
    "dilutedShares": [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ],
}
# Non-USD XBRL units, keyed by the XBRL_CONCEPTS metric name. EPS is filed
# under "USD/shares", share counts under "shares"; everything else is
# plain "USD". (fetch_xbrl_facts_for_ticker was previously hardcoded to
# "USD", which silently produced ZERO dilutedEPS rows for every ticker.)
XBRL_CONCEPT_UNITS = {
    "dilutedEPS": "USD/shares",
    "dilutedShares": "shares",
}
# totalAssets/stockholdersEquity are balance-sheet figures reported as of a
# single instant (just `end`, no `start`); every other concept here is an
# income-statement figure reported over a start..end duration -- see
# _annual_facts, which filters each shape differently.
XBRL_INSTANT_CONCEPTS = {"totalAssets", "stockholdersEquity"}

# How far back to pull Form 4 filings -- bounds both the request volume
# (a high-filing-frequency mega-cap files dozens a month) and the output
# file size, while still covering "recent insider activity."
FORM4_LOOKBACK_DAYS = 90
# This mapping (new tickers/IPOs aside) barely changes; no need to refetch
# more than about once a week.
CIK_MAP_MAX_AGE_DAYS = 7

MAX_WORKERS = 4
RETRY_SLEEP_SECONDS = 5
# SEC's fair-access policy caps requests at ~10/sec per IP, shared across
# every concurrent worker -- not a per-worker budget -- so this lock/
# timestamp pair is a single global throttle every request (from any
# thread) passes through, rather than relying on MAX_WORKERS alone to
# stay under the cap.
_rate_lock = threading.Lock()
_last_request_time = [0.0]
MIN_REQUEST_INTERVAL = 0.11  # ~9/sec, a small margin under the 10/sec cap


def _rate_limit():
    with _rate_lock:
        wait = _last_request_time[0] + MIN_REQUEST_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_request_time[0] = time.time()


def _sec_get(url, timeout=15):
    """Rate-limited GET with SEC's required User-Agent header and the same
    3-attempt retry convention this project's other fetchers use. Returns
    None (rather than raising) after 3 failed attempts, so one bad ticker
    doesn't take down the whole batch. timeout is overridden by callers
    fetching the ~90MB quarterly 13F bulk dataset -- 15s is fine for every
    other request this module makes, but nowhere near enough for that."""
    for attempt in range(3):
        _rate_limit()
        try:
            resp = requests.get(url, headers={"User-Agent": SEC_USER_AGENT}, timeout=timeout)
            if resp.status_code == 429:
                time.sleep(RETRY_SLEEP_SECONDS)
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException:
            if attempt == 2:
                return None
            time.sleep(1.5)
    return None


def _load_cik_map(force_refresh=False):
    """Returns {TICKER: zero-padded-10-digit CIK string}, cached to
    CIK_MAP_FILE and refetched only if older than CIK_MAP_MAX_AGE_DAYS."""
    try:
        stale = (time.time() - os.path.getmtime(CIK_MAP_FILE)) > CIK_MAP_MAX_AGE_DAYS * 86400
    except FileNotFoundError:
        stale = True

    if not stale and not force_refresh:
        with open(CIK_MAP_FILE) as f:
            return json.load(f)

    resp = _sec_get(TICKER_CIK_MAP_URL)
    if resp is None:
        # Fall back to whatever's cached, even if stale, rather than
        # failing the whole run over a transient SEC outage.
        if os.path.exists(CIK_MAP_FILE):
            with open(CIK_MAP_FILE) as f:
                return json.load(f)
        return {}

    raw = resp.json()
    cik_map = {entry["ticker"].upper(): str(entry["cik_str"]).zfill(10) for entry in raw.values()}
    with open(CIK_MAP_FILE, "w") as f:
        json.dump(cik_map, f)
    return cik_map


def _xml_text(el, path):
    child = el.find(path)
    return child.text.strip() if child is not None and child.text else None


def _xml_value(el, path):
    """For the schema's common <path><value>X</value></path> shape."""
    return _xml_text(el, f"{path}/value")


def _xml_float(el, path):
    try:
        return float(_xml_value(el, path))
    except (TypeError, ValueError):
        return None


def _parse_form4_xml(xml_bytes):
    """Parses one Form 4 <ownershipDocument> into {insiderName,
    officerTitle, isOfficer, isDirector, isTenPercentOwner, transactions}.
    transactions covers nonDerivativeTransaction entries only (ordinary
    common stock) -- derivativeTransaction (options/RSUs) isn't parsed."""
    root = ET.fromstring(xml_bytes)
    owner = root.find("reportingOwner")
    owner_name = None
    officer_title = is_officer = is_director = is_ten_pct = None
    if owner is not None:
        owner_id = owner.find("reportingOwnerId")
        if owner_id is not None:
            owner_name = _xml_text(owner_id, "rptOwnerName")
        rel = owner.find("reportingOwnerRelationship")
        if rel is not None:
            officer_title = _xml_text(rel, "officerTitle")
            is_officer = _xml_text(rel, "isOfficer")
            is_director = _xml_text(rel, "isDirector")
            is_ten_pct = _xml_text(rel, "isTenPercentOwner")

    transactions = [
        {
            "date": _xml_value(tx, "transactionDate"),
            # Unlike the other transaction fields, transactionCode is a
            # plain enumerated string with no <value>/<footnoteId> wrapper
            # (verified against a real filing) -- direct text, not _xml_value.
            "code": _xml_text(tx, "transactionCoding/transactionCode"),
            "shares": _xml_float(tx, "transactionAmounts/transactionShares"),
            "pricePerShare": _xml_float(tx, "transactionAmounts/transactionPricePerShare"),
            "acquiredDisposed": _xml_value(tx, "transactionAmounts/transactionAcquiredDisposedCode"),
            "sharesOwnedAfter": _xml_float(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction"),
        }
        for tx in root.findall(".//nonDerivativeTransaction")
    ]
    return {
        "insiderName": owner_name,
        "officerTitle": officer_title,
        "isOfficer": is_officer,
        "isDirector": is_director,
        "isTenPercentOwner": is_ten_pct,
        "transactions": transactions,
    }


def _list_form4_filings(cik, since_date):
    """[{accessionNumber, filingDate, primaryDocument}, ...] for form=="4"
    filings on or after since_date (a "YYYY-MM-DD" string), from the
    issuer's own submissions feed. Only searches the "recent" filings
    array SEC returns inline (up to ~1000 filings across every form
    type for that issuer) -- older filings would need the paginated
    files SEC references separately, not fetched here since
    FORM4_LOOKBACK_DAYS is well inside that window for any issuer this
    screener covers."""
    resp = _sec_get(SUBMISSIONS_URL.format(cik=cik))
    if resp is None:
        return []
    recent = resp.json().get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    return [
        {
            "accessionNumber": recent["accessionNumber"][i],
            "filingDate": recent["filingDate"][i],
            "primaryDocument": recent["primaryDocument"][i],
        }
        for i, form in enumerate(forms)
        if form == "4" and recent["filingDate"][i] >= since_date
    ]


def _raw_document_url(cik_no_padding, accession_number, primary_document):
    # primaryDocument from the submissions feed (e.g.
    # "xslF345X06/form4.xml") points at EDGAR's XSL-rendered HTML view of
    # the filing, not the underlying data -- the raw XML with the same
    # basename sits one level up, directly in the accession folder
    # (verified against a real filing: xslF345X06/form4.xml renders as
    # HTML, plain form4.xml at the accession root is the actual XML).
    accession_no_dashes = accession_number.replace("-", "")
    filename = primary_document.rsplit("/", 1)[-1]
    return f"https://www.sec.gov/Archives/edgar/data/{cik_no_padding}/{accession_no_dashes}/{filename}"


def fetch_form4_for_ticker(cik, since_date):
    """Every Form 4 filing (see _list_form4_filings) for one issuer CIK,
    each parsed into _parse_form4_xml's shape plus its own
    accessionNumber/filingDate. Skips (doesn't fail the batch over) a
    filing whose document fails to fetch or doesn't parse as XML."""
    cik_no_padding = str(int(cik))
    filings_out = []
    for filing in _list_form4_filings(cik, since_date):
        url = _raw_document_url(cik_no_padding, filing["accessionNumber"], filing["primaryDocument"])
        resp = _sec_get(url)
        if resp is None:
            continue
        try:
            parsed = _parse_form4_xml(resp.content)
        except ET.ParseError:
            continue
        parsed["accessionNumber"] = filing["accessionNumber"]
        parsed["filingDate"] = filing["filingDate"]
        filings_out.append(parsed)
    return filings_out


def fetch_form4(tickers, max_workers=MAX_WORKERS, lookback_days=FORM4_LOOKBACK_DAYS):
    """Fetches Form 4 insider-transaction filings for tickers and merges
    the result into insider_transactions.json. Tickers with no CIK match
    (delisted, non-EDGAR-registered, etc.) are silently skipped. Returns
    the {ticker: [...]} dict fetched this call (not the full merged
    file)."""
    cik_map = _load_cik_map()
    since_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    def fetch_one(ticker):
        cik = cik_map.get(ticker.upper())
        if cik is None:
            return ticker, None
        return ticker, fetch_form4_for_ticker(cik, since_date)

    total = len(tickers)
    # Each ticker is 1 submissions-list request plus 1 per filing found, all
    # sharing the single ~9 req/sec global throttle (see _rate_limit) -- a
    # full run over hundreds of tickers can take a while with nothing to
    # show for it otherwise, so this prints one line per ticker as it
    # finishes (flush=True since this is commonly run with stdout
    # redirected to a log file, e.g. `python main.py form4 > log 2>&1 &`,
    # where the default block-buffering would otherwise hide progress
    # until the buffer fills or the process exits).
    print(
        f"Fetching Form 4 filings for {total} tickers from SEC EDGAR "
        f"(~9 req/sec rate limit, {lookback_days}-day lookback -- this can take a while)...",
        flush=True,
    )

    results = {}
    no_cik = 0
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_one, t): t for t in tickers}
        for fut in as_completed(futures):
            ticker, filings = fut.result()
            done += 1
            if filings is None:
                no_cik += 1
                print(f"[{done}/{total}] {ticker}: no CIK match, skipped", flush=True)
                continue
            results[ticker] = filings
            tx_count = sum(len(f["transactions"]) for f in filings)
            print(
                f"[{done}/{total}] {ticker}: {len(filings)} filing(s), {tx_count} transaction(s)",
                flush=True,
            )

    try:
        with open(FORM4_FILE) as f:
            existing = json.load(f)
    except FileNotFoundError:
        existing = {}
    existing.update(results)

    with open(FORM4_FILE, "w") as f:
        json.dump(existing, f, indent=2)
    print(f"Wrote {FORM4_FILE} ({len(results)}/{len(tickers)} tickers fetched, {no_cik} had no CIK match)")

    return results


# ---------------------------------------------------------------------- #
#  XBRL company facts                                                     #
# ---------------------------------------------------------------------- #
def _annual_facts(concept_data, instant):
    """[{end, val, fy}, ...] oldest to newest, one entry per fiscal
    year-end, from a single XBRL concept's unit array (USD, or USD/shares
    for EPS and shares for share counts -- see XBRL_CONCEPT_UNITS and
    fetch_xbrl_facts_for_ticker). Restricted to form=="10-K" (the annual
    report's own reported figures, not a 10-Q's quarterly one) -- duration
    concepts (instant=False) are further restricted to entries whose
    start..end span 300-400 days, since even a 10-K's own USD array carries
    shorter comparative-quarter durations (e.g. just Q4) alongside the real
    full-year figure. Deduplicated by end date, keeping whichever entry was
    filed most recently for a given fiscal year-end (a prior year restated
    in a later 10-K) rather than the first one encountered."""
    by_end = {}
    for point in concept_data:
        if point.get("form") != "10-K" or point.get("val") is None:
            continue
        end = point.get("end")
        if end is None:
            continue
        if not instant:
            start = point.get("start")
            if not start:
                continue
            span_days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
            if not (300 < span_days < 400):
                continue
        existing = by_end.get(end)
        if existing is None or point.get("filed", "") >= existing.get("filed", ""):
            by_end[end] = point
    return sorted(
        [{"end": end, "val": p["val"], "fy": p.get("fy")} for end, p in by_end.items()],
        key=lambda x: x["end"],
    )


def _quarterly_facts(concept_data, annual, keep_last=20):
    """[{end, val}, ...] oldest->newest of DISCRETE 3-month revenue, from a
    concept's pooled duration points. 10-Q filings carry the 3-month figure
    for Q1-Q3 directly (start..end span 80-100 days); Q4 is never in a
    10-Q, so it's derived as the fiscal-year total (from `annual`, the
    _annual_facts output) minus the three discrete quarters that end within
    it. Dedup by end date, most-recently-filed wins. Used to rebuild a
    trailing-quarter revenue-growth blend (see main._blend_quarterly)."""
    by_end = {}
    for point in concept_data:
        val, end, start = point.get("val"), point.get("end"), point.get("start")
        if val is None or not end or not start:
            continue
        span_days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
        if not (80 < span_days < 100):
            continue
        existing = by_end.get(end)
        if existing is None or point.get("filed", "") >= existing[1]:
            by_end[end] = (val, point.get("filed", ""))
    quarters = {end: v for end, (v, _) in by_end.items()}
    for a in annual:
        fy_end = a["end"]
        if fy_end in quarters:
            continue
        fe = datetime.fromisoformat(fy_end)
        preceding = sorted(
            e for e in quarters if 0 < (fe - datetime.fromisoformat(e)).days < 320
        )
        if len(preceding) >= 3:
            quarters[fy_end] = a["val"] - sum(quarters[e] for e in preceding[-3:])
    ordered = sorted(({"end": e, "val": v} for e, v in quarters.items()), key=lambda x: x["end"])
    return ordered[-keep_last:]


def fetch_xbrl_facts_for_ticker(cik):
    """{metric: [{end, val, fy}, ...]} for every XBRL_CONCEPTS metric this
    issuer has annual (10-K) data for. Every fallback tag in a metric's
    chain gets pooled together (not just the first with any data) before
    _annual_facts filters/dedupes -- verified against a real case this
    would otherwise get wrong: AAPL's own "Revenues" tag has real data,
    just only through fiscal 2018 (it stopped being used at the ASC 606
    migration -- see XBRL_CONCEPTS' comment), so stopping at the first
    non-empty tag would silently truncate revenue at 2018 and never look
    at the newer tag carrying 2019 onward. A metric is omitted entirely
    if none of its tags have any data at all (e.g. a REIT with no
    EarningsPerShareDiluted tag)."""
    resp = _sec_get(XBRL_FACTS_URL.format(cik=cik))
    if resp is None:
        return {}
    usgaap = resp.json().get("facts", {}).get("us-gaap", {})

    result = {}
    for metric, tags in XBRL_CONCEPTS.items():
        instant = metric in XBRL_INSTANT_CONCEPTS
        unit = XBRL_CONCEPT_UNITS.get(metric, "USD")
        combined = []
        for tag in tags:
            points = usgaap.get(tag, {}).get("units", {}).get(unit)
            if points:
                combined.extend(points)
        annual = _annual_facts(combined, instant)
        if annual:
            result[metric] = annual
        if metric == "revenue":
            quarterly = _quarterly_facts(combined, annual)
            if quarterly:
                result["revenueQuarterly"] = quarterly
    return result


def fetch_xbrl_facts(tickers, max_workers=MAX_WORKERS):
    """Fetches annual XBRL fundamentals (see fetch_xbrl_facts_for_ticker)
    for tickers and merges the result into company_facts.json. Tickers
    with no CIK match are silently skipped. Returns the {ticker: {...}}
    dict fetched this call (not the full merged file)."""
    cik_map = _load_cik_map()
    total = len(tickers)
    # Each ticker is exactly 1 request (the whole company facts payload,
    # then curated down to XBRL_CONCEPTS locally) -- unlike Form 4's
    # variable per-filing request count, so this is the more predictable
    # of the two to estimate runtime for. flush=True for the same
    # log-redirected-to-a-file reason fetch_form4's progress printing is.
    print(
        f"Fetching XBRL company facts for {total} tickers from SEC EDGAR "
        f"(~9 req/sec rate limit -- this can take a while)...",
        flush=True,
    )

    def fetch_one(ticker):
        cik = cik_map.get(ticker.upper())
        if cik is None:
            return ticker, None
        return ticker, fetch_xbrl_facts_for_ticker(cik)

    results = {}
    no_cik = 0
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_one, t): t for t in tickers}
        for fut in as_completed(futures):
            ticker, facts = fut.result()
            done += 1
            if facts is None:
                no_cik += 1
                print(f"[{done}/{total}] {ticker}: no CIK match, skipped", flush=True)
                continue
            results[ticker] = facts
            print(f"[{done}/{total}] {ticker}: {len(facts)} metric(s)", flush=True)

    try:
        with open(XBRL_FACTS_FILE) as f:
            existing = json.load(f)
    except FileNotFoundError:
        existing = {}
    existing.update(results)

    with open(XBRL_FACTS_FILE, "w") as f:
        json.dump(existing, f, indent=2)
    print(f"Wrote {XBRL_FACTS_FILE} ({len(results)}/{len(tickers)} tickers fetched, {no_cik} had no CIK match)")

    return results


# ---------------------------------------------------------------------- #
#  13F institutional holdings                                             #
# ---------------------------------------------------------------------- #
_GENERIC_SUFFIX_RE = re.compile(
    r"\b(INCORPORATED|CORPORATION|COMPANY|LIMITED|HOLDINGS?|GROUP|INC|CORP|CO|LTD|LLC|PLC|SA|NV|AG)\b\.?"
)
_PUNCT_RE = re.compile(r"[.,&'\-]")


def _normalize_issuer_name(name):
    """Upper-cases, strips punctuation and generic corporate suffixes
    (Inc/Corp/Ltd/Holdings/...) so "Apple Inc." and "APPLE INC" (or
    "Apple, Inc.") compare equal -- the only available free join key
    between our own ticker universe and SEC's bulk 13F dataset, which has
    no ticker symbol at all (holdings are reported by CUSIP + issuer
    name, and there's no free ticker->CUSIP crosswalk). Verified against
    10 real large-caps (AAPL/MSFT/NVDA/... ) before relying on it.
    Best-effort, not exact: doesn't disambiguate multiple share classes of
    the same issuer (e.g. GOOGL/GOOG both normalize to "ALPHABET", so
    holdings of either would attribute to whichever ticker this run asked
    for), and a sufficiently generic name could in principle collide with
    an unrelated company."""
    name = _PUNCT_RE.sub("", name.upper())
    name = _GENERIC_SUFFIX_RE.sub("", name)
    return re.sub(r"\s+", " ", name).strip()


def _13f_bulk_urls(n=2):
    """The n most recent quarterly bulk 13F dataset .zip URLs, newest
    first, scraped off THIRTEENF_DATASETS_PAGE (its own listing is
    newest-first -- there's no versioned/stable API for "give me the
    latest N quarters", just this page). Returns fewer than n (possibly
    empty) if the page's shape doesn't match what's expected (e.g. SEC
    redesigns the page) or it simply doesn't have n entries, rather than
    silently substituting some other quarter."""
    resp = _sec_get(THIRTEENF_DATASETS_PAGE)
    if resp is None:
        return []
    matches = re.findall(r'href="(/files/structureddata/data/form-13f-data-sets/[^"]+\.zip)"', resp.text)
    return ["https://www.sec.gov" + m for m in matches[:n]]


def _aggregate_13f_bulk(url, wanted, collect_holders=False):
    """Downloads one quarterly bulk 13F dataset (every institutional
    manager's holdings) and aggregates it down to just the issuers in
    `wanted` ({normalized issuer name: ticker}, see
    _normalize_issuer_name), matched by name since the bulk data has no
    ticker/CIK-of-issuer field. Returns {ticker: {totalValueUsd,
    totalShares, putShares, callShares, holderCount}}, where holderCount
    counts distinct original 13F-HR filings (SUBMISSIONTYPE=="13F-HR",
    excluding "13F-HR/A" amendments so an amended filing doesn't get
    double-counted as a second holder) that reported an actual common-
    stock (non-option) position in that issuer -- an accession whose ONLY
    row for this issuer is a Put/Call doesn't count as a holder. VALUE is
    already whole USD, not thousands, per SEC's 2023 13F reporting-format
    update -- verified against a real Berkshire Hathaway holding: value /
    share count landed right on Ally Financial's actual per-share price,
    not 1000x off.

    Options rows (INFOTABLE.tsv's PUTCALL column is "Put" or "Call" rather
    than blank) are kept OUT of totalValueUsd/totalShares -- summing them
    in would conflate actual ownership with option exposure (a Put's
    SSHPRNAMT is shares the writer/holder has exposure to, not shares
    owned; a Call's is notional shares underlying the contract, not owned
    either) -- and instead aggregated separately into putShares/callShares.
    See this module's own docstring for why there's no way to tell a
    bought option from a written one in this data.

    If collect_holders, ALSO returns a second dict, {ticker: [{name,
    valueUsd, shares, putShares, callShares}, ...]}, sorted by valueUsd
    descending and capped to MAX_HOLDERS_PER_TICKER -- the actual
    institution names, resolved from this same zip's COVERPAGE.tsv
    (ACCESSION_NUMBER -> FILINGMANAGER_NAME; see SEC's
    form_13f_readme.pdf ยง5.2) rather than the CUSIP/accession numbers
    INFOTABLE.tsv alone carries. Only worth reading COVERPAGE.tsv (a
    second full pass' worth of parsing) when the caller actually wants
    names -- fetch_13f_holdings only asks for this on the current quarter,
    not the prior one it only uses for a QoQ delta. A filer with options
    but no common-stock row in this issuer still gets a row here
    (valueUsd/shares 0, putShares/callShares carrying the position),
    since the cap/sort is by valueUsd -- an options-only filer can be
    pushed out of the top N even with large option exposure.

    The downloaded zip is saved to SEC_DIR directly, i.e. data/sec/ (named
    after the URL's own filename, e.g. "01mar2026-31may2026_form13f.zip")
    -- not THIRTEENF_DIR/data/sec/13f/, and not a temp file discarded
    immediately after either -- explicit instruction to keep the raw SEC
    data on disk under data/sec itself. Reused as-is (no re-download) if a
    prior run already fetched that exact quarter's file. SUBMISSION.tsv/
    INFOTABLE.tsv/COVERPAGE.tsv themselves are never extracted to their
    own loose files -- they're read directly out of the saved zip's
    entries via zipfile.ZipFile.open, one row at a time (INFOTABLE alone
    is ~3.5M rows; the zip is the only thing that touches disk).

    A single ~90MB download covering every filer at once, unlike Form 4/
    XBRL's one-request-per-ticker pattern."""
    local_zip_path = os.path.join(SEC_DIR, os.path.basename(url))
    if os.path.exists(local_zip_path):
        print(f"Using already-downloaded {local_zip_path}", flush=True)
    else:
        print(f"Downloading quarterly 13F bulk dataset from {url} (~90MB, this can take a while)...", flush=True)
        resp = _sec_get(url, timeout=180)
        if resp is None:
            print(f"Failed to download {url} after 3 attempts.")
            return ({}, {}) if collect_holders else {}
        with open(local_zip_path, "wb") as f:
            f.write(resp.content)

    results = {}
    holder_accessions = {}
    holder_rows = {}  # ticker -> {accession_number: {valueUsd, shares, putShares, callShares}}, collect_holders only
    with zipfile.ZipFile(local_zip_path) as zf:
        with zf.open("SUBMISSION.tsv") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"), delimiter="\t")
            original_accessions = {row["ACCESSION_NUMBER"] for row in reader if row["SUBMISSIONTYPE"] == "13F-HR"}
        print(f"{len(original_accessions)} original (non-amendment) 13F-HR filings this quarter", flush=True)

        with zf.open("INFOTABLE.tsv") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"), delimiter="\t")
            for row in reader:
                if row["ACCESSION_NUMBER"] not in original_accessions:
                    continue
                ticker = wanted.get(_normalize_issuer_name(row["NAMEOFISSUER"]))
                if ticker is None:
                    continue
                value = int(row["VALUE"] or 0)
                shares = int(row["SSHPRNAMT"] or 0)
                put_call = row["PUTCALL"]  # "" (common stock), "Put", or "Call"
                agg = results.setdefault(
                    ticker, {"totalValueUsd": 0, "totalShares": 0, "putShares": 0, "callShares": 0}
                )
                if put_call == "Put":
                    agg["putShares"] += shares
                elif put_call == "Call":
                    agg["callShares"] += shares
                else:
                    agg["totalValueUsd"] += value
                    agg["totalShares"] += shares
                    holder_accessions.setdefault(ticker, set()).add(row["ACCESSION_NUMBER"])
                if collect_holders:
                    # A single filer can carry more than one INFOTABLE row
                    # for the same issuer (split lots, share classes that
                    # normalize to the same ticker -- see
                    # _normalize_issuer_name, or separate common-stock and
                    # Put/Call rows), so this accumulates rather than
                    # assumes one row per (ticker, accession).
                    entry = holder_rows.setdefault(ticker, {}).setdefault(
                        row["ACCESSION_NUMBER"], {"valueUsd": 0, "shares": 0, "putShares": 0, "callShares": 0}
                    )
                    if put_call == "Put":
                        entry["putShares"] += shares
                    elif put_call == "Call":
                        entry["callShares"] += shares
                    else:
                        entry["valueUsd"] += value
                        entry["shares"] += shares

        filer_name_by_accession = {}
        if collect_holders:
            with zf.open("COVERPAGE.tsv") as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"), delimiter="\t")
                filer_name_by_accession = {
                    row["ACCESSION_NUMBER"]: row["FILINGMANAGER_NAME"]
                    for row in reader
                    if row["ACCESSION_NUMBER"] in original_accessions
                }

    # holder_accessions only ever gets accessions from the common-stock
    # (blank PUTCALL) branch above, so a ticker matched ONLY via Put/Call
    # rows has no entry there -- .get(..., ()) covers that as 0 holders,
    # rather than a KeyError.
    for ticker, agg in results.items():
        agg["holderCount"] = len(holder_accessions.get(ticker, ()))

    if not collect_holders:
        return results

    holders = {}
    for ticker, per_accession in holder_rows.items():
        rows = [
            {
                "name": filer_name_by_accession.get(accession, "Unknown filer"),
                "valueUsd": v["valueUsd"],
                "shares": v["shares"],
                "putShares": v["putShares"],
                "callShares": v["callShares"],
            }
            for accession, v in per_accession.items()
        ]
        rows.sort(key=lambda r: -r["valueUsd"])
        holders[ticker] = rows[:MAX_HOLDERS_PER_TICKER]

    return results, holders


def fetch_13f_holdings(ticker_names):
    """Aggregates SEC's latest TWO quarterly bulk 13F datasets (see
    _aggregate_13f_bulk) down to just the tickers in ticker_names
    ({ticker: company name}). For each ticker matched in the latest
    quarter: {totalValueUsd, totalShares, putShares, callShares,
    holderCount} (that quarter's snapshot, see _aggregate_13f_bulk) plus
    pctShareChangeQoQ -- the
    percent change in totalShares vs. the prior quarter, i.e. whether
    institutions net-bought or net-sold, None if the ticker wasn't
    matched in the prior quarter's dataset (e.g. no institutional holders
    that quarter, or a name-matching miss -- see _normalize_issuer_name)
    or its prior totalShares was 0. Also writes THIRTEENF_HOLDERS_FILE,
    the named top-MAX_HOLDERS_PER_TICKER institutions per ticker for the
    CURRENT quarter only (the prior quarter is only ever used for the QoQ
    delta above, never surfaced by name).

    Two ~90MB downloads (current + prior quarter), each a single bulk
    file covering every filer at once -- unlike Form 4/XBRL's
    one-request-per-ticker pattern. Each is cached to disk under
    THIRTEENF_DIR by _aggregate_13f_bulk, so a rerun for the same quarter
    (e.g. after this ticker universe changes) doesn't re-download either
    file."""
    urls = _13f_bulk_urls(2)
    if not urls:
        print("Could not find any 13F bulk dataset URLs -- SEC may have redesigned the listing page.")
        return {}

    wanted = {_normalize_issuer_name(name): ticker for ticker, name in ticker_names.items()}

    current, holders = _aggregate_13f_bulk(urls[0], wanted, collect_holders=True)
    prior = _aggregate_13f_bulk(urls[1], wanted) if len(urls) > 1 else {}
    if not prior:
        print("No prior-quarter 13F dataset available -- pctShareChangeQoQ will be None for every ticker.")

    for ticker, agg in current.items():
        prior_shares = prior.get(ticker, {}).get("totalShares")
        if prior_shares:
            agg["pctShareChangeQoQ"] = (agg["totalShares"] - prior_shares) / abs(prior_shares)
        else:
            agg["pctShareChangeQoQ"] = None

    with open(THIRTEENF_FILE, "w") as f:
        json.dump(current, f, indent=2)
    print(f"Wrote {THIRTEENF_FILE} ({len(current)}/{len(ticker_names)} tickers matched)")

    with open(THIRTEENF_HOLDERS_FILE, "w") as f:
        json.dump(holders, f, indent=2)
    print(f"Wrote {THIRTEENF_HOLDERS_FILE} ({len(holders)} ticker(s), up to {MAX_HOLDERS_PER_TICKER} named holders each)")

    return current
