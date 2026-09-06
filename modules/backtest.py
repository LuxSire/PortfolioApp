"""backtest.py -- forward weekly performance of each historical screen's
RECOMMENDATION groups.

For every ``data/output/history/sorted_screen <YYYYMMDD>.csv`` snapshot,
every rated candidate (Strong Buy / Buy / Sell / Strong Sell -- the same
set the Recommendations page draws from) is put into one of six groups
by the same entry gates RecommendationsView.tsx applies:

  long_strong_buy   Strong Buy that clears the long gates
  long_buy          Buy that clears the long gates
  long_blocked      Buy/Strong Buy that fails one (falling-knife/overbought
                    momentum, mean-reversion overbought)
  short_strong_sell Strong Sell that clears the short gates
  short_sell        Sell that clears the short gates
  short_blocked     Sell/Strong Sell that fails one (strong-uptrend/oversold
                    momentum, mean-reversion oversold)

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

Every blocked row also carries ``blockedBy`` -- the specific gate name(s)
it failed (momentum / mean_reversion; a row can fail more
than one), and each week carries a ``blockedBreakdown`` --
per side, per reason, the same {return, count} shape as ``groups`` but
restricted to rows that failed THAT one reason.
This is what actually isolates which single rule is behind a
long_blocked/short_blocked group's overall number, rather than only
knowing the group underperformed for some unspecified mix of reasons.
Reasons aren't mutually exclusive, so a breakdown's counts don't sum back
to the group's own count.

Every week ALSO carries a ``currentModel`` sibling -- the exact same
{groups, portfolio, blockedBreakdown, tickers} shape, but built from a
counterfactual rating: TODAY's modules.scoring.score_rows() re-run on that
SAME week's already-archived factor columns, instead of trusting the
`rating` column the snapshot was actually written with (assigned by
whatever scoring.py was live that week). This answers "what would the
CURRENT model have recommended at the start of that week," scored forward
against the same real price bars -- not just "did the OLD recommendations
survive the new gates" (the top-level fields), which is all a plain gate
re-sync tests. See _rescore_current_model for exactly what can and can't
be reconstructed this way (short version: anything a later derive.py fix
changed the underlying NUMBER of, and simReturn/sentiment/insiders/short-
interest, aren't reconstructable and are left out rather than faked).

Output: ``{generatedAt, weeks: [{week, entryDate, exitDate, groups,
portfolio, blockedBreakdown, tickers, currentModel}]}``, oldest week
first. Recomputed in full every run -- new weeks appear by dropping
another dated snapshot into the history folder. IB daily history only
reaches ~3 months back, so older weeks lose coverage; each group carries
its own ``count``.
"""

import csv
import glob
import json
import os
import re
import statistics
from datetime import date, datetime, timedelta, timezone

from modules.scoring import rating_for_percentile, score_rows

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

# Kept in lockstep with ib_server._REC_* / RecommendationsView.tsx. The
# crowded-short gate (_MAX_SHORT_INTEREST) was removed entirely, from all
# three -- this backtest's own blockedBreakdown showed it was consistently
# counterproductive: every short blocked for crowding would have made a
# good short in both measured weeks.
# Two stale rules just caught by inspection (both applied to the Actual
# AND Current columns alike, since _long/_short_gate_reasons classify
# both -- see _build_week): momentum was still the old one-sided 30/70
# block, and revenue_growth was a gate the live app dropped a while ago
# (replaced by the sim-return gate) but this module never stopped
# checking. Momentum synced to the current no-buy/no-sell zones;
# revenue_growth removed outright rather than re-thresholded, since it no
# longer exists as a gate to sync TO. Not (yet) replaced with a sim-return
# check here -- simReturn/forecastReturn aren't archived per-week
# (sorted_screen <date>.csv has no such column), so it can't be
# reconstructed retroactively; would need main.py to start writing it into
# future snapshots.
_MOMENTUM_OVERSOLD = 20
# Short-side "already crashed, don't short into a bounce" floor. Lowered
# below _MOMENTUM_OVERSOLD (asymmetric on purpose): 2-week zone analysis
# showed names with MSI in 15-20 kept falling -- shorting them stayed
# profitable -- so the no-short band only kicks in below 15. The long
# side still treats <=20 as the buy-the-dip zone (_MOMENTUM_OVERSOLD).
_MOMENTUM_SHORT_OVERSOLD = 15
_MOMENTUM_NO_BUY = 35
_MOMENTUM_NO_SELL = 65
_MOMENTUM_OVERBOUGHT = 80
_MEAN_REVERSION_OVERBOUGHT = 80
_MEAN_REVERSION_OVERSOLD = 20

_HISTORY_RE = re.compile(r"sorted_screen[ _](\d{4})(\d{2})(\d{2})\.csv$")


def _f(x):
    try:
        v = float(x)
        return v if v == v else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _long_gate_reasons(row):
    """Every RecommendationsView.tsx long-gate check this row fails, by
    name -- empty list means it clears eligibleToBuy + meanReversionOkForLong.
    A row can fail more than one at once; each is recorded independently
    (not mutually exclusive) so blockedBreakdown below can isolate which
    single rule is actually costing return, instead of only knowing the
    row was blocked for SOME reason.

    Momentum blocks in the falling-knife band (OVERSOLD < mom <= NO_BUY)
    or overbought (mom >= OVERBOUGHT) -- mirrors RecommendationsView.tsx's
    momentumBlocks('Long'). A deep-oversold reading (mom <= OVERSOLD) is
    the buy-the-dip case and is NOT blocked. No revenue-growth check --
    the live gate was replaced by a sim-return gate a while ago (not
    reconstructable here, see _MOMENTUM_OVERSOLD's own comment). No
    EPS-trend check either -- also removed from the live gate:
    backtesting showed it was consistently counterproductive on the short
    side (the largest short_blocked population every week, and
    consistently positive -- i.e. a bad short -- in every week/model
    measured), the same shape of finding that got crowded_short removed."""
    reasons = []
    momentum = _f(row.get("momentum"))
    if momentum is None or (_MOMENTUM_OVERSOLD < momentum <= _MOMENTUM_NO_BUY) or momentum >= _MOMENTUM_OVERBOUGHT:
        reasons.append("momentum")
    mr = _f(row.get("meanReversion"))
    if mr is not None and mr >= _MEAN_REVERSION_OVERBOUGHT:
        reasons.append("mean_reversion")
    return reasons


def _short_gate_reasons(row):
    """Short-side twin of _long_gate_reasons -- RecommendationsView.tsx's
    eligibleToSell + meanReversionOkForShort, each named independently. No
    crowded-short or EPS-trend check (both removed from the live gate,
    see _long_gate_reasons' own comment) and no revenue-growth check
    (same reason as the long side).

    Momentum blocks in the strong-uptrend band (NO_SELL <= mom <
    OVERBOUGHT) or oversold (mom <= SHORT_OVERSOLD, a lower floor than the
    long side's OVERSOLD) -- mirrors RecommendationsView.tsx's
    momentumBlocks('Short'). A deep-overbought reading (mom >= OVERBOUGHT)
    is the short-the-top case and is NOT blocked."""
    reasons = []
    momentum = _f(row.get("momentum"))
    if momentum is None or (_MOMENTUM_NO_SELL <= momentum < _MOMENTUM_OVERBOUGHT) or momentum <= _MOMENTUM_SHORT_OVERSOLD:
        reasons.append("momentum")
    mr = _f(row.get("meanReversion"))
    if mr is not None and mr <= _MEAN_REVERSION_OVERSOLD:
        reasons.append("mean_reversion")
    return reasons


def _group_for(rating, row):
    """(group, blockedBy) -- blockedBy is always [] for a non-blocked
    group (nothing to name), populated only for *_blocked. `rating` is
    passed in separately from `row` (rather than read off row['rating'])
    so the SAME row's factor columns (momentum/growth/etc, which the gate
    functions still read off `row`) can be classified under either the
    rating the snapshot actually shipped with or a re-scored counterfactual
    one -- see _rescore_current_model."""
    if rating in _LONG_RATINGS:
        reasons = _long_gate_reasons(row)
        if reasons:
            return "long_blocked", reasons
        return ("long_strong_buy" if rating == "Strong Buy" else "long_buy"), []
    if rating in _SHORT_RATINGS:
        reasons = _short_gate_reasons(row)
        if reasons:
            return "short_blocked", reasons
        return ("short_strong_sell" if rating == "Strong Sell" else "short_sell"), []
    return None, []


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


_GATE_REASONS = ("momentum", "mean_reversion")


def _blocked_breakdown(long_blocked, short_blocked):
    """{"long": {reason: {return, count}}, "short": {...}} for every gate
    reason that fired at least once this week, isolating which SPECIFIC
    rule is behind a *_blocked group's overall number -- a row blocked by
    two reasons at once counts toward both (reasons aren't mutually
    exclusive, see _long_gate_reasons/_short_gate_reasons), so this can't
    be summed back into the *_blocked group total, only compared against
    it per reason. Only non-empty reasons are included, so e.g. a week
    with no crowded shorts just omits that key rather than showing a
    zero."""
    out = {}
    for side, members in (("long", long_blocked), ("short", short_blocked)):
        by_reason = {}
        for reason in _GATE_REASONS:
            hit = [m for m in members if reason in m["blockedBy"]]
            if hit:
                by_reason[reason] = _group_stats(hit)
        if by_reason:
            out[side] = by_reason
    return out


def _rescore_current_model(csv_rows):
    """{ticker: rating} using TODAY's modules.scoring.score_rows, re-run on
    THIS SAME week's already-archived factor columns -- "what would the
    CURRENT model have rated this ticker, given only the data that was
    actually on file that week" (a counterfactual against the rating the
    snapshot actually shipped with, from whatever scoring.py was live
    then). Restricted to rows that were part of the real scored universe
    that week (a non-empty `score` column) -- the same price/forwardPE-
    sign exclusion main.py's own MIN_PRICE gate applies before scoring,
    replicated here via "was it scored at all" rather than importing
    main.py's own constant (main.py already imports this module, so the
    reverse would be circular).

    Two things this can NOT reconstruct, both deliberately left out rather
    than faked:
      1. Any later derive.py fix to a factor's own COMPUTATION -- e.g. the
         revenueGrowth corrupted-quarterly-blend fix, the two-margin
         earningsMarginDelta, the earnings-growth floor/estimate-fallback.
         Those need that week's raw provider dumps (yfinance/SEC), which
         aren't archived -- only the derived CSV is. So this rescore runs
         TODAY'S scoring/gate LOGIC over LAST WEEK'S factor VALUES, not a
         full re-derivation.
      2. simReturn/forecastReturn (not archived per-week -- only the
         current simulations.json exists) and sentiment/insiders/short-
         interest (same problem, and using TODAY's snapshot as a stand-in
         would be lookahead bias -- built from data that didn't exist yet
         at the snapshot date). All four are passed as empty/missing to
         score_rows. This is harmless to relative ranking, not a silent
         corruption: a factor with the IDENTICAL rank for every ticker
         (missing) just adds the same constant to every score, changing no
         comparison between tickers."""
    rows = [(r["ticker"], r) for r in csv_rows if r.get("ticker") and _f(r.get("score")) is not None]
    if not rows:
        return {}
    scored = sorted(score_rows(rows), key=lambda item: item[2])
    n = len(scored)
    return {symbol: rating_for_percentile(i / n) for i, (symbol, _, _) in enumerate(scored)}


def _summarize(records):
    """{groups, portfolio, blockedBreakdown, tickers} from one already-
    classified record list -- shared by both the actual-rating model and
    the currentModel counterfactual in _build_week below, so the two stay
    byte-for-byte the same shape."""
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
        (
            {
                "ticker": r["ticker"],
                "rating": r["rating"],
                "group": r["group"],
                "blockedBy": r["blockedBy"],
                "return": round(r["pnl"], 6),
            }
            for r in records
        ),
        key=lambda t: (order[t["group"]], -t["return"]),
    )

    return {
        "groups": groups,
        "portfolio": portfolio,
        "blockedBreakdown": _blocked_breakdown(by_group["long_blocked"], by_group["short_blocked"]),
        "tickers": tickers,
    }


def _build_week(week_iso, csv_path, closes):
    screen_date = date.fromisoformat(week_iso)
    entry_cutoff = week_iso
    exit_cutoff = (screen_date + timedelta(days=7)).isoformat()

    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))

    # Each ticker's price path is computed ONCE and shared by both models
    # below -- it depends only on the ticker/dates, not on which rating
    # classifies it (long vs short, which flips the pnl sign, is applied
    # per-model in _records_for).
    paths = {}
    for row in rows:
        ticker = row.get("ticker")
        if not ticker:
            continue
        path = _window_series(closes.get(ticker), entry_cutoff, exit_cutoff)
        if path:
            paths[ticker] = path

    def _records_for(rating_of):
        records = []
        for row in rows:
            path = paths.get(row.get("ticker"))
            if not path:
                continue
            group, reasons = _group_for(rating_of(row), row)
            if group is None:
                continue
            sign = 1.0 if group.startswith("long") else -1.0
            records.append({
                "ticker": row["ticker"],
                "rating": rating_of(row),
                "group": group,
                "blockedBy": reasons,
                "pnl": sign * (path[-1][1] / path[0][1] - 1),
            })
        return records

    actual_records = _records_for(lambda row: row.get("rating"))
    rescored = _rescore_current_model(rows)
    current_records = _records_for(lambda row: rescored.get(row.get("ticker")))

    result = {
        "week": week_iso,
        "entryDate": min((p[0][0] for p in paths.values()), default=None),
        "exitDate": max((p[-1][0] for p in paths.values()), default=None),
    }
    result.update(_summarize(actual_records))
    result["currentModel"] = _summarize(current_records)
    return result


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
