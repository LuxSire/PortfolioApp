"""backtest.py -- forward weekly performance of each historical screen's
rating buckets.

For every ``data/output/history/sorted_screen <YYYYMMDD>.csv`` snapshot,
joins each rated ticker to its forward one-week return from IB's daily
bars (``data/IB/price_history_daily_3mo.json``):

  entry = last close on/before the screen date   (e.g. Fri for a Sat date)
  exit  = last close on/before the screen date + 7 days

then aggregates per rating bucket, equal-weight:

  return : mean of the bucket members' weekly returns
  vol    : stdev of the bucket's equal-weight DAILY return series over the
           week, x sqrt(n_days)  -> weekly volatility
  sharpe : return / vol          (risk-free ~ 0 over a single week)

Sell / Strong Sell buckets carry the RAW stock return (a good screen makes
Strong Sell negative), not a short-side P&L -- one sign convention across
all five buckets.

Output: ``{generatedAt, weeks: [{week, entryDate, exitDate, buckets,
tickers}]}``, oldest week first. Recomputed in full every run (cheap,
deterministic) -- new weeks appear simply by dropping another
``sorted_screen <date>.csv`` into the history folder. IB daily history only
reaches back ~3 months, so weeks older than that lose price coverage; each
bucket carries its own ``count`` so the table can show how many names it
actually spans.
"""

import csv
import glob
import json
import math
import os
import re
import statistics
from datetime import date, datetime, timedelta, timezone

# Fixed display / iteration order. Hold and "NA" (priced but unscored)
# are skipped -- Hold is the ~950-name middle-of-the-pack bucket with only
# partial IB daily-bar coverage and no directional thesis to score.
RATING_BUCKETS = ["Strong Buy", "Buy", "Sell", "Strong Sell"]

_HISTORY_RE = re.compile(r"sorted_screen[ _](\d{4})(\d{2})(\d{2})\.csv$")


def _history_files(history_dir):
    """[(week_iso, path), ...] for every sorted_screen <YYYYMMDD>.csv in
    history_dir (space or underscore before the date), one per date,
    sorted oldest first."""
    by_week = {}
    for path in glob.glob(os.path.join(history_dir, "sorted_screen *.csv")) + glob.glob(
        os.path.join(history_dir, "sorted_screen_*.csv")
    ):
        m = _HISTORY_RE.search(os.path.basename(path))
        if not m:
            continue
        by_week.setdefault(f"{m.group(1)}-{m.group(2)}-{m.group(3)}", path)
    return sorted(by_week.items())


def _load_daily_closes(daily_file):
    """{ticker: [(date_iso, close), ...]} sorted by date, from IB's
    {ticker: [{date, close, ...}]} daily-bar file."""
    try:
        with open(daily_file) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    closes = {}
    for ticker, bars in raw.items():
        series = []
        for b in bars or []:
            d = (b.get("date") or "")[:10]
            c = b.get("close")
            if d and c is not None:
                series.append((d, float(c)))
        if series:
            series.sort()
            closes[ticker] = series
    return closes


def _window_series(series, entry_cutoff, exit_cutoff):
    """The close path used for one ticker's weekly return: the last bar
    on/before entry_cutoff, followed by every bar after that up to and
    including exit_cutoff. None when there's no entry bar or nothing after
    it in the window."""
    if not series:
        return None
    entry_idx = None
    for i, (d, _) in enumerate(series):
        if d <= entry_cutoff:
            entry_idx = i
        else:
            break
    if entry_idx is None:
        return None
    path = [series[entry_idx]]
    for d, c in series[entry_idx + 1:]:
        if d <= exit_cutoff:
            path.append((d, c))
        else:
            break
    return path if len(path) >= 2 else None


def _bucket_stats(members):
    """{return, vol, sharpe, count} for one rating bucket. members is a
    list of per-ticker dicts with `ret` (weekly) and `path` (the
    _window_series close path). Equal-weight, daily-rebalanced."""
    if not members:
        return {"return": None, "vol": None, "sharpe": None, "count": 0}

    weekly = statistics.fmean(m["ret"] for m in members)

    # Equal-weight portfolio's daily return per trading day, keyed by the
    # day's date so members that miss a bar just don't contribute to that
    # step rather than misaligning the whole series.
    step_returns = {}
    for m in members:
        path = m["path"]
        for (_, prev_c), (cur_d, cur_c) in zip(path, path[1:]):
            if prev_c:
                step_returns.setdefault(cur_d, []).append(cur_c / prev_c - 1)
    port_daily = [statistics.fmean(v) for _, v in sorted(step_returns.items())]

    vol = sharpe = None
    if len(port_daily) >= 2:
        vol = statistics.stdev(port_daily) * math.sqrt(len(port_daily))
        if vol > 0:
            sharpe = weekly / vol

    return {
        "return": round(weekly, 6),
        "vol": round(vol, 6) if vol is not None else None,
        "sharpe": round(sharpe, 4) if sharpe is not None else None,
        "count": len(members),
    }


def _build_week(week_iso, csv_path, closes):
    screen_date = date.fromisoformat(week_iso)
    entry_cutoff = week_iso
    exit_cutoff = (screen_date + timedelta(days=7)).isoformat()

    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))

    records = []
    for row in rows:
        rating = row.get("rating")
        ticker = row.get("ticker")
        if rating not in RATING_BUCKETS or not ticker:
            continue
        path = _window_series(closes.get(ticker), entry_cutoff, exit_cutoff)
        if not path:
            continue
        records.append({
            "ticker": ticker,
            "rating": rating,
            "ret": path[-1][1] / path[0][1] - 1,
            "path": path,
        })

    by_bucket = {b: [r for r in records if r["rating"] == b] for b in RATING_BUCKETS}
    buckets = {b: _bucket_stats(by_bucket[b]) for b in RATING_BUCKETS}

    all_dates = [d for r in records for d, _ in r["path"]]
    entry_date = min(all_dates) if all_dates else None
    exit_date = max(all_dates) if all_dates else None

    order = {b: i for i, b in enumerate(RATING_BUCKETS)}
    tickers = sorted(
        ({"ticker": r["ticker"], "rating": r["rating"], "return": round(r["ret"], 6)} for r in records),
        key=lambda t: (order[t["rating"]], -t["return"]),
    )

    return {
        "week": week_iso,
        "entryDate": entry_date,
        "exitDate": exit_date,
        "buckets": buckets,
        "tickers": tickers,
    }


def build_backtest(history_dir, daily_file):
    """See module docstring. Returns the JSON-ready result dict. Weeks with
    no price coverage yet -- the most recent snapshot, whose forward week
    hasn't finished, or any week older than IB's ~3-month daily history --
    are dropped rather than rendered as an all-blank column."""
    closes = _load_daily_closes(daily_file)
    weeks = [
        w
        for week_iso, path in _history_files(history_dir)
        if (w := _build_week(week_iso, path, closes))["tickers"]
    ]
    return {
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "weeks": weeks,
    }
