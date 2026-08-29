"""backtest.py -- forward weekly performance of each historical screen's
RECOMMENDATION groups.

For every ``data/output/history/sorted_screen <YYYYMMDD>.csv`` snapshot,
every rated candidate (Strong Buy / Buy / Sell / Strong Sell -- the same
set the Recommendations page draws from) is put into one of six groups
by the same entry gates RecommendationsView.tsx applies:

  long_strong_buy   Strong Buy that clears the long gates
  long_buy          Buy that clears the long gates
  long_blocked      Buy/Strong Buy that fails one (overbought momentum,
                    mean-reversion overbought, weak growth, negative EPS-trend)
  short_strong_sell Strong Sell that clears the short gates
  short_sell        Sell that clears the short gates
  short_blocked     Sell/Strong Sell that fails one (oversold momentum,
                    mean-reversion oversold, too much growth, crowded short,
                    positive EPS-trend)

Each candidate's forward one-week return is taken from IB's daily bars
(``data/IB/price_history_daily_3mo.json``):

  entry = last close on/before the screen date   (Fri for a Sat date)
  exit  = last close on/before the screen date + 7 days

then per group, equal-weight:

  return : mean POSITION P&L -- +stock return for longs, -stock return
           for shorts, so a positive number always means the pick worked

The point of the long_blocked / short_blocked groups is to check whether
the gates actually help: a working gate makes blocked picks worse than
un-blocked ones. Each week also carries a ``portfolio`` -- the gated
Strong Buy long leg + gated Strong Sell short leg summed (dollar-neutral,
each leg equal-weight 100% gross).

Output: ``{generatedAt, weeks: [{week, entryDate, exitDate, groups,
portfolio, tickers}]}``, oldest week first. Recomputed in full every run -- new weeks
appear by dropping another dated snapshot into the history folder. IB
daily history only reaches ~3 months back, so older weeks lose coverage;
each group carries its own ``count``.
"""

import csv
import glob
import json
import os
import re
import statistics
from datetime import date, datetime, timedelta, timezone

GROUPS = [
    "long_strong_buy",
    "long_buy",
    "long_blocked",
    "short_strong_sell",
    "short_sell",
    "short_blocked",
]

_LONG_RATINGS = {"Strong Buy", "Buy"}
_SHORT_RATINGS = {"Strong Sell", "Sell"}

# Kept in lockstep with ib_server._REC_* / RecommendationsView.tsx.
_MOMENTUM_OVERSOLD = 30
_MOMENTUM_OVERBOUGHT = 70
_REVENUE_GROWTH_THRESHOLD = 0.1
_MEAN_REVERSION_OVERBOUGHT = 80
_MEAN_REVERSION_OVERSOLD = 20
_MAX_SHORT_INTEREST = 0.1

_HISTORY_RE = re.compile(r"sorted_screen[ _](\d{4})(\d{2})(\d{2})\.csv$")


def _f(x):
    try:
        v = float(x)
        return v if v == v else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _eps_trend(row):
    vals = [v for v in (_f(row.get("epsRevision0y")), _f(row.get("epsRevision1y"))) if v is not None]
    return sum(vals) / len(vals) if vals else None


def _short_pct(row):
    return _f(row.get("shortPctOfFloatFinra")) or _f(row.get("shortPercentOfFloat"))


def _passes_long_gates(row):
    """RecommendationsView.tsx's eligibleToBuy + sufficientGrowthForLong +
    meanReversionOkForLong + epsTrendOkForLong. Momentum gate BLOCKS
    overbought, doesn't REQUIRE oversold."""
    momentum = _f(row.get("momentum"))
    if momentum is None or momentum > _MOMENTUM_OVERBOUGHT:
        return False
    growth = _f(row.get("revenueGrowth"))
    if growth is not None and growth < _REVENUE_GROWTH_THRESHOLD:
        return False
    mr = _f(row.get("meanReversion"))
    if mr is not None and mr >= _MEAN_REVERSION_OVERBOUGHT:
        return False
    eps = _eps_trend(row)
    if eps is not None and eps < 0:
        return False
    return True


def _passes_short_gates(row):
    """RecommendationsView.tsx's eligibleToSell + notCrowded +
    notTooMuchGrowthForShort + meanReversionOkForShort +
    epsTrendOkForShort. Momentum gate BLOCKS oversold."""
    momentum = _f(row.get("momentum"))
    if momentum is None or momentum < _MOMENTUM_OVERSOLD:
        return False
    sp = _short_pct(row)
    if sp is not None and sp > _MAX_SHORT_INTEREST:
        return False
    growth = _f(row.get("revenueGrowth"))
    if growth is not None and growth > _REVENUE_GROWTH_THRESHOLD:
        return False
    mr = _f(row.get("meanReversion"))
    if mr is not None and mr <= _MEAN_REVERSION_OVERSOLD:
        return False
    eps = _eps_trend(row)
    if eps is not None and eps > 0:
        return False
    return True


def _group_for(row):
    rating = row.get("rating")
    if rating in _LONG_RATINGS:
        if not _passes_long_gates(row):
            return "long_blocked"
        return "long_strong_buy" if rating == "Strong Buy" else "long_buy"
    if rating in _SHORT_RATINGS:
        if not _passes_short_gates(row):
            return "short_blocked"
        return "short_strong_sell" if rating == "Strong Sell" else "short_sell"
    return None


def _history_files(history_dir):
    """[(week_iso, path), ...], one per date, oldest first."""
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
    """{ticker: [(date_iso, close), ...]} sorted by date."""
    try:
        with open(daily_file) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    closes = {}
    for ticker, bars in raw.items():
        series = sorted(
            ((b["date"][:10], float(b["close"])) for b in bars or [] if b.get("date") and b.get("close") is not None)
        )
        if series:
            closes[ticker] = series
    return closes


def _window_series(series, entry_cutoff, exit_cutoff):
    """Close path for one ticker's weekly return: last bar on/before
    entry_cutoff, then every bar after it up to exit_cutoff. None when
    there's no entry bar or nothing after it in the window."""
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


def _group_stats(members):
    """{return, count} for one group. members carry `pnl` (signed weekly
    P&L). return is the equal-weight mean of the members' weekly P&L."""
    if not members:
        return {"return": None, "count": 0}
    return {
        "return": round(statistics.fmean(m["pnl"] for m in members), 6),
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
        group = _group_for(row)
        ticker = row.get("ticker")
        if group is None or not ticker:
            continue
        path = _window_series(closes.get(ticker), entry_cutoff, exit_cutoff)
        if not path:
            continue
        sign = 1.0 if group.startswith("long") else -1.0
        records.append({
            "ticker": ticker,
            "rating": row.get("rating"),
            "group": group,
            "pnl": sign * (path[-1][1] / path[0][1] - 1),
            "entry_d": path[0][0],
            "exit_d": path[-1][0],
        })

    by_group = {g: [r for r in records if r["group"] == g] for g in GROUPS}
    groups = {g: _group_stats(by_group[g]) for g in GROUPS}

    # Dollar-neutral book: the gated Strong Buy longs + gated Strong Sell
    # shorts, each leg equal-weight & 100% gross. P&L is just the sum of
    # the two group P&Ls (already position-signed). None if either leg is
    # empty for the week.
    sb, ss = groups["long_strong_buy"], groups["short_strong_sell"]
    portfolio = {
        "return": round(sb["return"] + ss["return"], 6) if sb["return"] is not None and ss["return"] is not None else None,
        "count": sb["count"] + ss["count"],
    }

    order = {g: i for i, g in enumerate(GROUPS)}
    tickers = sorted(
        ({"ticker": r["ticker"], "rating": r["rating"], "group": r["group"], "return": round(r["pnl"], 6)} for r in records),
        key=lambda t: (order[t["group"]], -t["return"]),
    )

    return {
        "week": week_iso,
        "entryDate": min((r["entry_d"] for r in records), default=None),
        "exitDate": max((r["exit_d"] for r in records), default=None),
        "groups": groups,
        "portfolio": portfolio,
        "tickers": tickers,
    }


def build_backtest(history_dir, daily_file):
    """See module docstring. Returns the JSON-ready result dict. Weeks with
    no price coverage yet -- the newest snapshot whose forward week hasn't
    finished, or any week older than IB's ~3-month daily history -- are
    dropped rather than rendered as an all-blank column."""
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
