"""
main.py — entry point.

download_all():    fetch tickers from symbols.json (active == 1), pull forward/trailing
                    P/E + price-to-FCF from Yahoo Finance, then add the
                    regression-slope momentum score, and write raw_data.json +
                    forward_pe.csv + screen.csv + sorted_screen.csv. Tickers
                    downloaded within the last FRESH_HOURS hours are skipped
                    (their previous row is reused as-is), and tickers whose
                    fetch fails this run (e.g. a transient Yahoo Finance
                    error) fall back to their last successful row instead of
                    disappearing from the outputs. Right after raw_data.json
                    is written, also fetches StockTwits social sentiment
                    (see social_sentiment.py) for the top SENTIMENT_TOP_N
                    tickers of the ranking as it stood before this run
                    (sorted_screen.csv isn't rewritten with fresh scores
                    until later in the same run), writing/merging into
                    social_sentiment.json. This is a separate, unofficial
                    data source that can fail without affecting the rest of
                    the pipeline.
download_prices():  reuse the forward-PE data already in forward_pe.csv and only
                    refresh the momentum score, then rewrite forward_pe.csv +
                    screen.csv + sorted_screen.csv.
download_symbols(): refetch forward-PE + price-performance data for a specific
                    list of tickers only (e.g. ones missing from the outputs
                    after a transient Yahoo Finance failure), merging into the
                    existing forward_pe.csv/raw_data.json, then rewrite
                    screen.csv + sorted_screen.csv. Run via
                    `python main.py symbol TICKER [TICKER ...]`.

Writes:
  raw_data.json     the complete, unfiltered yfinance `info` payload per
                     ticker (every field Yahoo exposes), for discovering
                     fields not yet curated into forward_pe.csv.
  forward_pe.csv    all tickers, sorted by forwardPE ascending.
  screen.csv        only tickers with positive forwardPE (negative or missing
                     priceToFCF is kept, not excluded), sorted by forwardPE
                     ascending.
  sorted_screen.csv screen.csv rows priced at $8+, ranked by a composite score:
                     15% low forwardPE, 10% low forwardPE relative to its
                     sector's average forwardPE, 15% low priceToFCF (negative
                     or missing FCF is treated as a fixed 200 for this factor
                     only), 10% high momentum (regression-slope momentum
                     score, divided by the annualized volatility of daily
                     log returns; missing penalized as worst),
                     10% analyst conviction — the average of high
                     targetUpside and low recommendationMean ranks (negative
                     upside, and a 0 or missing recommendationMean, both
                     penalized as worst), 15% based on
                     forwardPE - trailingPE when trailingPE is positive and
                     finite (infinite or negative trailingPE — the company
                     lost money over the trailing twelve months — penalized
                     as worst for this factor instead of masked behind a
                     placeholder), 5% low pegRatio (negative PEG penalized as
                     worst, not treated as "low"), 10% high revenueGrowth
                     (negative growth penalized as worst, not treated as
                     "low"), 5% low debtToEquity relative to its sector's
                     average debtToEquity (negative or missing debtToEquity
                     penalized as worst, same treatment as pegRatio), 5%
                     liquidity — the average of high quickRatio and high
                     currentRatio ranks (missing penalized as worst). Sorted
                     best (lowest score) first.
  social_sentiment.json  {ticker: {bullish, bearish, tagged, total, score,
                     lastDownload}} StockTwits sentiment for the top
                     SENTIMENT_TOP_N ranked tickers; score is
                     (bullish - bearish) / tagged. Merged across runs, so
                     tickers that drop out of the top N keep their last
                     known score rather than being deleted.
  price_history.json  {ticker: [{date, close}, ...]} trailing ~1 month of
                     daily closes, captured from the same yfinance fetch
                     add_momentum already makes for the momentum score (see
                     IBApp.get_momentum) — no extra network round-trip.
                     Each ticker's series is replaced wholesale on its next
                     fetch (a rolling window, not an accumulated archive).
"""

import csv
import json
import math
import sys
from datetime import datetime, timedelta

from IBApp import IBApp
from social_sentiment import fetch_social_sentiment

OUTPUT_CSV = "forward_pe.csv"
SCREEN_CSV = "screen.csv"
SORTED_SCREEN_CSV = "sorted_screen.csv"
RAW_DATA_FILE = "raw_data.json"
PRICE_HISTORY_FILE = "price_history.json"
SYMBOLS_FILE = "symbols.json"
MIN_PRICE = 8
SENTIMENT_TOP_N = 200

FIELDNAMES = [
    "ticker", "name", "sector", "forwardPE", "forwardEps", "trailingPE", "pegRatio", "priceToFCF",
    "debtToEquity", "LiqRatio", "quickRatio", "currentRatio", "revenueGrowth", "price", "targetMeanPrice",
    "targetHighPrice", "targetLowPrice", "targetUpside", "recommendationKey", "recommendationMean",
    "numberOfAnalystOpinions", "momentum", "earningsTimestampStart", "lastDownload",
]
# screen.csv / sorted_screen.csv show sector last instead of right after name.
SCREEN_FIELDNAMES = [f for f in FIELDNAMES if f != "sector"] + ["sector"]


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
    """Reads an existing forward_pe.csv into {ticker: {field: value}}."""
    data = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            data[row["ticker"]] = dict(row)
    return data


def load_top_tickers(path, n):
    """Reads the first n tickers from an existing sorted_screen.csv (already
    ranked best-to-worst by score). Used to scope the social-sentiment
    download to the top of the ranking as it stood before this run started,
    since this run's own scores aren't computed until later. Returns []
    if the file doesn't exist yet (e.g. first-ever run)."""
    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            return [row["ticker"] for _, row in zip(range(n), reader)]
    except FileNotFoundError:
        return []


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def add_momentum(app, data, history_out=None):
    """Adds momentum (regression-slope momentum score over the trailing ~1
    month of daily closes — see IBApp.get_momentum) to each entry in data,
    in place. If history_out is a dict, also captures each ticker's daily
    close series from that same fetch (see write_price_history) — no extra
    network round-trip since IBApp.get_momentum already pulls it."""
    momentum = app.get_momentum(list(data.keys()), history_out=history_out)
    for symbol, d in data.items():
        d["momentum"] = momentum.get(symbol)


def add_momentum_and_persist_history(app, data):
    """add_momentum, plus merging the close-series it captures into
    price_history.json — the common case across all three download_*
    entry points."""
    history = {}
    add_momentum(app, data, history_out=history)
    try:
        with open(PRICE_HISTORY_FILE) as f:
            all_history = json.load(f)
    except FileNotFoundError:
        all_history = {}
    all_history.update(history)
    write_price_history(all_history)


def add_target_upside(data):
    """Adds targetUpside (sell-side mean target price vs. current price) to
    each entry in data, in place."""
    for d in data.values():
        price = to_float(d.get("price"))
        target = to_float(d.get("targetMeanPrice"))
        d["targetUpside"] = target / price - 1 if price and target else None


def add_avg_liquidity_ratio(data):
    """Adds LiqRatio (mean of quickRatio and currentRatio) to each entry in
    data, in place. Left blank when either ratio is missing, rather than
    averaging just the one present value."""
    for d in data.values():
        quick = to_float(d.get("quickRatio"))
        current = to_float(d.get("currentRatio"))
        d["LiqRatio"] = (quick + current) / 2 if quick is not None and current is not None else None


FRESH_HOURS = 12


def is_fresh(last_download, max_age_hours=FRESH_HOURS):
    """True if last_download (an ISO datetime string, e.g. from a previous
    forward_pe.csv row) is within max_age_hours of now. Used to skip
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
    in forward_pe.csv instead of silently dropping them from every output.
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
    forward_pe.csv. Useful for discovering fields to add later."""
    with open(RAW_DATA_FILE, "w") as f:
        json.dump(raw_info, f, indent=2, default=str)
    print(f"Wrote {RAW_DATA_FILE}")


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


def write_screen_csv(data):
    """Only tickers with positive forwardPE, sorted by forwardPE ascending."""
    rows = sorted(screen_rows(data), key=lambda item: to_float(item[1]["forwardPE"]))
    write_csv(SCREEN_CSV, SCREEN_FIELDNAMES, rows)


def rank_ascending(rows, value_fn):
    """Percentile rank (0 = best/lowest, 1 = worst) of rows by value_fn(d); rows
    where value_fn returns None get the worst rank."""
    valid = [(symbol, value_fn(d)) for symbol, d in rows if value_fn(d) is not None]
    valid.sort(key=lambda item: item[1])
    n = len(valid)
    ranks = {symbol: i / (n - 1) if n > 1 else 0 for i, (symbol, _) in enumerate(valid)}
    return {symbol: ranks.get(symbol, 1.0) for symbol, _ in rows}


def sector_avg_forward_pe(rows):
    """Average forwardPE per sector, across rows that have both a sector and a forwardPE."""
    sums, counts = {}, {}
    for _, d in rows:
        sector = d.get("sector")
        fwd_pe = to_float(d.get("forwardPE"))
        if sector and fwd_pe is not None:
            sums[sector] = sums.get(sector, 0) + fwd_pe
            counts[sector] = counts.get(sector, 0) + 1
    return {sector: sums[sector] / counts[sector] for sector in sums}


def sector_avg_debt_to_equity(rows):
    """Average non-negative debtToEquity per sector, across rows that have
    both a sector and a non-negative debtToEquity (negative debtToEquity,
    i.e. negative shareholder equity, is excluded so one distressed company
    doesn't skew its sector's baseline)."""
    sums, counts = {}, {}
    for _, d in rows:
        sector = d.get("sector")
        de = to_float(d.get("debtToEquity"))
        if sector and de is not None and de >= 0:
            sums[sector] = sums.get(sector, 0) + de
            counts[sector] = counts.get(sector, 0) + 1
    return {sector: sums[sector] / counts[sector] for sector in sums}


def score_rows(rows):
    """Composite score per (symbol, d): 15% low forwardPE, 10% low forwardPE
    relative to its sector's average forwardPE, 15% low priceToFCF (negative
    or missing FCF treated as a fixed 200 for this factor only), 10% high
    momentum (regression-slope momentum score, now also divided by the
    annualized volatility of daily log returns — see
    IBApp._regression_momentum; missing ranked worst), 10% analyst
    conviction — the average of high targetUpside and low recommendationMean
    ranks (negative upside, and a 0 or missing recommendationMean, both
    ranked worst; upside alone says "analysts expect it to rise", pairing it
    with recommendationMean asks whether they're also confident enough to
    call it a buy, since a mean price target can look high just because it's
    dragged up by a stale or thinly-covered outlier), 15% based on
    forwardPE - trailingPE when trailingPE is positive and finite (more
    negative is better; negative or infinite trailingPE — i.e. the company
    lost money over the trailing twelve months — ranked worst instead of
    being substituted with a value that let it look artificially good), 5%
    low pegRatio (negative PEG ranked worst, not best), 10% high
    revenueGrowth (negative growth ranked worst, not just low), 5% low
    debtToEquity relative to its sector's average debtToEquity
    (debtToEquity - sector avg; negative or missing debtToEquity ranked
    worst, same treatment as pegRatio), 5% liquidity — the average of high
    quickRatio and high currentRatio ranks (missing ranked worst). Lower
    score is better."""
    pe_ranks = rank_ascending(rows, lambda d: to_float(d.get("forwardPE")))

    def effective_price_to_fcf(d):
        fcf = to_float(d.get("priceToFCF"))
        # Negative or missing priceToFCF (negative or unavailable free cash
        # flow) carries no real cash-flow signal; treat it as a fixed 200
        # instead of excluding it, the same treatment pe_vs_trailing gives
        # negative/infinite trailingPE.
        return 200 if fcf is None or fcf < 0 else fcf

    fcf_ranks = rank_ascending(rows, effective_price_to_fcf)

    def neg_perf(field):
        def key(d):
            perf = to_float(d.get(field))
            return -perf if perf is not None else None
        return key

    def neg_if_positive(field):
        """Like neg_perf, but zero/negative values are excluded from the
        normal magnitude-based ranking and fall back to the worst rank
        instead — the same treatment peg_ranks gives negative pegRatio.
        Used for factors where "negative" is a qualitatively different,
        much worse signal than "low positive", not just more of the same."""
        def key(d):
            value = to_float(d.get(field))
            return -value if value is not None and value > 0 else None
        return key

    momentum_ranks = rank_ascending(rows, neg_perf("momentum"))
    upside_ranks = rank_ascending(rows, neg_if_positive("targetUpside"))
    growth_ranks = rank_ascending(rows, neg_if_positive("revenueGrowth"))

    def recommendation_key(d):
        value = to_float(d.get("recommendationMean"))
        # 1 = strong buy, 5 = strong sell; 0 shows up for a couple of
        # thinly-covered tickers (1 analyst) and isn't a real position on
        # that scale, so it's treated as missing rather than "better than
        # strong buy".
        return value if value is not None and value > 0 else None

    recommendation_ranks = rank_ascending(rows, recommendation_key)
    analyst_ranks = {
        symbol: (upside_ranks[symbol] + recommendation_ranks[symbol]) / 2 for symbol, _ in rows
    }

    def pe_vs_trailing(d):
        trailing_pe = to_float(d.get("trailingPE"))
        fwd_pe = to_float(d.get("forwardPE"))
        # Infinite or negative trailingPE means the company lost money over
        # the trailing twelve months and carries no real earnings signal;
        # rank it worst instead of substituting a fixed placeholder, since
        # fwd_pe - placeholder would otherwise look like the best possible
        # diff for cheap stocks (the opposite of what negative earnings
        # should signal).
        if trailing_pe is None or fwd_pe is None or not math.isfinite(trailing_pe) or trailing_pe < 0:
            return None
        return fwd_pe - trailing_pe

    diff_ranks = rank_ascending(rows, pe_vs_trailing)
    peg_ranks = rank_ascending(
        rows,
        lambda d: to_float(d.get("pegRatio")) if (to_float(d.get("pegRatio")) or 0) > 0 else None,
    )
    # Negative debtToEquity comes from negative shareholder equity (financial
    # distress), not "low debt"; rank it worst, same treatment as negative PEG.
    debt_sector_avg = sector_avg_debt_to_equity(rows)

    def debt_relative(d):
        sector = d.get("sector")
        de = to_float(d.get("debtToEquity"))
        avg = debt_sector_avg.get(sector)
        if de is None or de < 0 or not sector or avg is None:
            return None
        return de - avg

    debt_ranks = rank_ascending(rows, debt_relative)

    def liquidity_rank_key(field):
        def key(d):
            value = to_float(d.get(field))
            return -value if value is not None else None
        return key

    quick_ranks = rank_ascending(rows, liquidity_rank_key("quickRatio"))
    current_ranks = rank_ascending(rows, liquidity_rank_key("currentRatio"))
    liquidity_ranks = {
        symbol: (quick_ranks[symbol] + current_ranks[symbol]) / 2 for symbol, _ in rows
    }

    sector_avg = sector_avg_forward_pe(rows)

    def sector_relative_pe(d):
        sector = d.get("sector")
        fwd_pe = to_float(d.get("forwardPE"))
        avg = sector_avg.get(sector)
        return fwd_pe / avg if sector and fwd_pe is not None and avg else None

    sector_ranks = rank_ascending(rows, sector_relative_pe)

    scored = []
    for symbol, d in rows:
        score = (
            pe_ranks[symbol] * 0.15
            + sector_ranks[symbol] * 0.10
            + fcf_ranks[symbol] * 0.15
            + momentum_ranks[symbol] * 0.10
            + analyst_ranks[symbol] * 0.10
            + diff_ranks[symbol] * 0.15
            + peg_ranks[symbol] * 0.05
            + growth_ranks[symbol] * 0.10
            + debt_ranks[symbol] * 0.05
            + liquidity_ranks[symbol] * 0.05
        )
        scored.append((symbol, d, score))
    return scored


def write_sorted_screen_csv(data):
    """screen_rows() filtered to price >= MIN_PRICE, ranked by score ascending (best first)."""
    rows = [(s, d) for s, d in screen_rows(data) if (to_float(d.get("price")) or 0) >= MIN_PRICE]
    scored = sorted(score_rows(rows), key=lambda item: item[2])

    fieldnames = SCREEN_FIELDNAMES + ["score"]
    with open(SORTED_SCREEN_CSV, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(fieldnames)
        for symbol, d, score in scored:
            writer.writerow([symbol] + [d.get(field, "") for field in SCREEN_FIELDNAMES[1:]] + [score])
    print(f"Wrote {SORTED_SCREEN_CSV}")


def download_all():
    """Full pipeline: fetch forward P/E data from Yahoo Finance, then the momentum score.
    Tickers downloaded within the last FRESH_HOURS hours are skipped and their
    previous forward_pe.csv row is reused as-is, rather than re-fetched."""
    app = IBApp()
    tickers = load_tickers(SYMBOLS_FILE)
    print(f"Loaded {len(tickers)} active tickers from {SYMBOLS_FILE}")

    try:
        previous = load_pe_data(OUTPUT_CSV)
    except FileNotFoundError:
        previous = {}
    stale = [t for t in tickers if not is_fresh(previous.get(t, {}).get("lastDownload"))]
    fresh = len(tickers) - len(stale)
    if fresh:
        print(f"Skipping {fresh} tickers downloaded within the last {FRESH_HOURS}h")

    raw_info = {}
    fetched = app.get_forward_pe(stale, usa_only=True, raw_out=raw_info)
    print(f"{len(fetched)} are USA-domiciled with Yahoo Finance data")

    data = {t: previous[t] for t in tickers if t not in stale and t in previous}
    data.update(fetched)
    fill_missing_from_previous(data, tickers, previous)

    try:
        with open(RAW_DATA_FILE) as f:
            all_raw = json.load(f)
    except FileNotFoundError:
        all_raw = {}
    all_raw.update(raw_info)
    write_raw_data(all_raw)

    # Scoped to the ranking as it stood before this run (sorted_screen.csv
    # isn't rewritten with fresh scores until below) — see load_top_tickers.
    top_tickers = load_top_tickers(SORTED_SCREEN_CSV, SENTIMENT_TOP_N)
    if top_tickers:
        fetch_social_sentiment(top_tickers)
    else:
        print(f"No existing {SORTED_SCREEN_CSV} yet; skipping social sentiment download")

    apply_sector_overrides(data, load_sectors(SYMBOLS_FILE))
    add_momentum_and_persist_history(app, data)
    add_target_upside(data)
    add_avg_liquidity_ratio(data)
    write_full_csv(data)
    write_screen_csv(data)
    write_sorted_screen_csv(data)


def download_prices():
    """Reuse forward-PE data already in forward_pe.csv; only refresh the momentum score."""
    app = IBApp()
    data = load_pe_data(OUTPUT_CSV)
    print(f"Loaded {len(data)} tickers from {OUTPUT_CSV}")

    apply_sector_overrides(data, load_sectors(SYMBOLS_FILE))
    add_momentum_and_persist_history(app, data)
    add_target_upside(data)
    add_avg_liquidity_ratio(data)
    write_full_csv(data)
    write_screen_csv(data)
    write_sorted_screen_csv(data)


def download_symbols(symbols):
    """Refetch forward-PE + price-performance data for specific tickers only
    (e.g. ones that hit a transient Yahoo Finance error during a full run
    and are silently missing from every output), merging the results into
    the existing forward_pe.csv/raw_data.json rather than refetching the
    whole universe, then rewriting screen.csv + sorted_screen.csv."""
    symbols = sorted({s.strip().upper() for s in symbols})
    app = IBApp()

    raw_info = {}
    fetched = app.get_forward_pe(symbols, usa_only=True, raw_out=raw_info)
    print(f"Fetched {len(fetched)}/{len(symbols)} requested tickers")
    missing = sorted(set(symbols) - set(fetched))
    if missing:
        print(f"No USA-domiciled Yahoo Finance data for: {missing}")

    try:
        with open(RAW_DATA_FILE) as f:
            all_raw = json.load(f)
    except FileNotFoundError:
        all_raw = {}
    all_raw.update(raw_info)
    write_raw_data(all_raw)

    apply_sector_overrides(fetched, load_sectors(SYMBOLS_FILE))
    add_momentum_and_persist_history(app, fetched)
    add_target_upside(fetched)
    add_avg_liquidity_ratio(fetched)

    data = load_pe_data(OUTPUT_CSV)
    data.update(fetched)
    write_full_csv(data)
    write_screen_csv(data)
    write_sorted_screen_csv(data)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "prices"
    if mode == "all":
        download_all()
    elif mode == "prices":
        download_prices()
    elif mode == "symbol":
        if len(sys.argv) < 3:
            sys.exit("Usage: python main.py symbol TICKER [TICKER ...]")
        download_symbols(sys.argv[2:])
    else:
        sys.exit(f"Unknown mode {mode!r}, expected 'all', 'prices', or 'symbol'")
