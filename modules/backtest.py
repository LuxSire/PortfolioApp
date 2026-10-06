"""backtest.py -- forward weekly performance of each historical screen's
RECOMMENDATION groups.

For every ``data/output/history/sorted_screen <YYYYMMDD>.csv`` snapshot,
every rated candidate (Strong Buy / Buy / Sell / Strong Sell -- the same
set the Recommendations page draws from -- plus Hold, as an unrated
baseline) is put into one of seven groups by the same entry gates
RecommendationsView.tsx applies:

  long_strong_buy   Strong Buy that clears the long gates
  long_buy          Buy that clears the long gates
  long_blocked      Buy/Strong Buy that fails one (weak momentum, a
                    simulation saying it should fall, earnings within
                    the week, frozen/acquisition-capped volatility)
  short_strong_sell Strong Sell that clears the short gates
  short_sell        Sell that clears the short gates
  short_blocked     Sell/Strong Sell that fails one (strong momentum, a
                    simulation saying it should rise, strong revenue
                    growth, earnings within the week, frozen/
                    acquisition-capped volatility)
  hold              The broad Hold middle, held long, no gates -- a
                    "not rated at all" baseline for the six above

Each candidate's forward return is taken from IB's daily bars
(``data/IB/price_history_daily_3mo.json``), held exactly HOLDING_TRADING_DAYS
(5) TRADING days -- not a flat 7 CALENDAR days, which used to drift between
3 and 5 actual trading days depending on where a weekend or holiday fell:

  entry = last close on/before the screen date   (Fri for a Sat date)
  exit  = the 5th trading-day close after entry (fewer only if the price
          cache runs out first)

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

from modules import derive
from modules.scoring import rating_for_percentile, score_rows

GROUPS = [
    "long_strong_buy",
    "long_buy",
    "long_blocked",
    "short_strong_sell",
    "short_sell",
    "short_blocked",
    "hold",
]

_LONG_RATINGS = {"Strong Buy", "Buy"}
_SHORT_RATINGS = {"Strong Sell", "Sell"}

# Kept in lockstep with ib_server._REC_* / RecommendationsView.tsx. An
# OLDER crowded-short gate (_MAX_SHORT_INTEREST, a lower/broader
# threshold) was removed entirely, from all three -- this backtest's own
# blockedBreakdown showed it was consistently counterproductive: every
# short blocked for crowding would have made a good short in both
# measured weeks. A DIFFERENT, narrower short-interest gate (30% of
# float) was added back later after VITL's 32%-of-float short lost 23.1%
# in one week -- then retired AGAIN still later, once a full 5 backtested
# weeks showed it excluded names that compounded +16.95%, almost double
# short_strong_sell's own +9.29% -- costing real return for a squeeze
# risk that's real but rare. Folded into scoring.short_interest_rank's
# weight instead, as continuous linear scoring rather than a gate -- see
# that factor's own FACTOR_WEIGHTS comment.
# Two stale rules just caught by inspection (both applied to the Actual
# AND Current columns alike, since _long/_short_gate_reasons classify
# both -- see _build_week): momentum was still the old one-sided 30/70
# block, and revenue_growth was a gate the live app dropped a while ago
# (replaced by the sim-return gate) but this module never stopped
# checking. Momentum synced to the current no-buy/no-sell zones;
# revenue_growth removed outright rather than re-thresholded, since it no
# longer exists as a gate to sync TO. UPDATE: sim_return now checked (see
# _long_gate_reasons/_short_gate_reasons' own paragraphs) -- main.py
# started writing simReturn into sorted_screen <date>.csv, so this is
# reconstructable GOING FORWARD; every week archived before that column
# existed still has nothing to check (fails open, same as any other
# missing-data case).
# Trend (momentum) gate REMOVED (was _MOMENTUM_NO_BUY=35/_NO_SELL=65) --
# explicit instruction (2026-10-01): its effect on 2-5 day forward returns isn't established. Kept in sync with RecommendationsView.tsx/ib_server.py/
# portfolio_optimizer.py; `momentum` (now the next-day Reversal Score) is
# still a scored factor.
# meanReversion gate REMOVED (was _MEAN_REVERSION_OVERBOUGHT=80/OVERSOLD=20)
# -- kept in sync with RecommendationsView.tsx's own removal of
# meanReversionOkForLong/meanReversionOkForShort: the factor-performance
# check found meanReversion backwards on both books (long rho=+0.071 when
# this gate's "overbought predicts a pullback" premise needs it negative;
# short rho=-0.106, same problem mirrored), so blocking an entry on it had
# no empirical support.
#
# An entry_timing gate (90/10 on derive.reconcile_entry_timing's 35h
# signal) briefly replaced it here, then was REMOVED too -- explicit
# instruction: this module is meant to backtest what the live app
# actually does, and entry_timing was never a live gate on
# RecommendationsView.tsx (deliberately informational-only there, since a
# human reading the card can choose to wait a day -- a fixed weekly
# backtest snapshot can't model that choice, so hard-excluding on it here
# was testing a rule the live app doesn't enforce). See
# derive.reconcile_entry_timing/RecommendationsView.tsx's entryTimingLine
# for what the signal itself still does (unweighted, informational card
# line + still recomputed in this module for no-lookahead correlation
# checks elsewhere, just no longer a gate).

# A short-only growth gate, added here to catch up with
# RecommendationsView.tsx's own growthBlocksShortEntry -- this module had
# drifted out of sync with it (confirmed live: S/PANW at 20.6%/24.8%
# trailing revenue growth showed up as clean short_strong_sell weeks with
# an ugly loss when the live app would already refuse to short either
# today). Exact same thresholds/fields as the live gate, so this module's
# blocked/unblocked split actually matches what the live app would do,
# not just what it used to do.
#
# The short-interest gate that used to sit alongside this one
# (_short_interest_blocks/_effective_short_pct, mirroring
# RecommendationsView.tsx's own shortInterestBlocksEntry) is RETIRED --
# explicit instruction, after confirming live over 5 backtested weeks
# that names it excluded compounded +16.95%, almost double
# short_strong_sell's own +9.29% -- the gate was costing real return, not
# just avoiding squeeze risk. Folded into scoring.short_interest_rank's
# weight instead (see modules/scoring.py's own FACTOR_WEIGHTS comment) --
# a continuous, linear scoring effect, not a gate, so there is nothing
# left for this module to mirror here.
# Short-side revenue-growth gate -- explicit instruction: never short a
# stock whose trailing AND expected (Eulerpool) revenue growth are BOTH above
# 10% (corrected from "either": one figure alone no longer blocks).
# ENFORCED here and in modules/portfolio_optimizer.py, matching the
# Recommendations page's shortGrowthBlocksEntry.
_SHORT_GROWTH_CEILING = 0.10


def _growth_blocks_short(row):
    trailing = _f(row.get("revenueGrowth"))
    expected = _f(row.get("eulerRevGrowth1y"))
    return (trailing is not None and expected is not None
            and trailing > _SHORT_GROWTH_CEILING and expected > _SHORT_GROWTH_CEILING)


# Mirrors RecommendationsView.tsx's VOL_GATE_MIN_ANNUALIZED / lowVolBlocksEntry
# -- see derive.reconcile_price_volatility's own comment for the motivation
# (acquisition-capped/frozen tickers). Side-agnostic: blocks BOTH long and
# short, unlike every other gate here. Same forward-only caveat as
# sim_return above: priceVolAnnualized was never archived into
# sorted_screen <date>.csv until main.py started writing it, so this
# fails open (not blocked) for every already-archived week.
_VOL_GATE_MIN_ANNUALIZED = 0.05


def _low_vol_blocks(row):
    vol = _f(row.get("priceVolAnnualized"))
    return vol is not None and vol < _VOL_GATE_MIN_ANNUALIZED


# Daily-move gate -- mirrors RecommendationsView.tsx's dailyMoveBlocks: no
# new long when the entry day's move is above +DAILY_MOVE_GATE_SD sd of the
# stock's prior ~3 months of daily returns, no new short below -that.
# dailyMoveZ is recomputed as of each week's own date in
# _recompute_momentum_asof (derive.reconcile_daily_move), so it applies to
# every archived week, not only ones written after the column existed.
# Trend entry filter -- mirrors RecommendationsView.tsx's trendBlocks: no new
# long at trend < derive.TREND_NO_BUY ("don't buy weak stocks"), no new
# short at trend > derive.TREND_NO_SELL ("don't sell strong stocks"). Not a
# scored factor. Recomputed as of each week in _recompute_momentum_asof.
# Missing never blocks.
def _trend_blocks(row, side):
    t = _f(row.get("trend"))
    if t is None:
        return False
    return t < derive.TREND_NO_BUY if side == "long" else t > derive.TREND_NO_SELL


def _daily_move_blocks(row, side):
    z = _f(row.get("dailyMoveZ"))
    if z is None:
        return False
    return z > derive.DAILY_MOVE_GATE_SD if side == "long" else z < -derive.DAILY_MOVE_GATE_SD

_HISTORY_RE = re.compile(r"sorted_screen[ _](\d{4})(\d{2})(\d{2})\.csv$")


def _f(x):
    try:
        v = float(x)
        return v if v == v else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _earnings_blocks(row, entry_cutoff, exit_cutoff):
    """True when this row's earningsTimestampStart falls anywhere in the
    SAME entry->exit week (entry_cutoff/exit_cutoff, both 'YYYY-MM-DD')
    this week's own forward return is measured over -- explicit
    instruction: exclude every stock reporting DURING the week in
    question, not just within a fixed short window from the screen date.
    Motivated by BBW (-23% Strong Buy) and CRWD (-13.8% Strong Sell), both
    clean earnings-day gaps with no visible pre-earnings setup (BBW
    reported 5 days after its screen date, CRWD 4 days -- a narrow 2-day
    window would have missed both; this full-week window catches either).
    Missing earningsTimestampStart does NOT block (fail-open, same
    convention every other optional factor here uses)."""
    ts = _f(row.get("earningsTimestampStart"))
    if ts is None:
        return False
    earnings_date = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    return entry_cutoff <= earnings_date <= exit_cutoff


def _long_gate_reasons(row, entry_cutoff, exit_cutoff):
    """Every RecommendationsView.tsx long-gate check this row fails, by
    name -- empty list means it clears eligibleToBuy. A row can fail more
    than one at once; each is recorded independently (not mutually
    exclusive) so blockedBreakdown below can isolate which single rule is
    actually costing return, instead of only knowing the row was blocked
    for SOME reason.

    No momentum check -- the Trend gate is removed (see the comment
    where _MOMENTUM_* used to live). No EPS-trend check
    -- removed from the live gate: backtesting showed it was consistently
    counterproductive on the short side (the largest short_blocked
    population every week, and consistently positive -- i.e. a bad short
    -- in every week/model measured), the same shape of finding that got
    crowded_short removed. No entry_timing check either (that gate was
    tried and then removed -- see this module's own top-of-file comment
    for why: it was never a live gate, so it didn't belong in a backtest
    of the live app's own behavior).

    sim_return: mirrors RecommendationsView.tsx's simReturnOkForLong --
    blocked when the Monte Carlo simulation says the price should FALL
    (simReturn < 0). Only checkable going forward: simReturn was never
    archived into sorted_screen <date>.csv until main.py started writing
    it (see SCREEN_ONLY_FIELDNAMES's own comment), so this fails open
    (not blocked) for every already-archived week that predates that
    column, same as every other missing-data case here."""
    reasons = []
    sim_return = _f(row.get("simReturn"))
    if sim_return is not None and sim_return < 0:
        reasons.append("sim_return")
    if _daily_move_blocks(row, "long"):
        reasons.append("daily_move")
    if _trend_blocks(row, "long"):
        reasons.append("trend")
    if _earnings_blocks(row, entry_cutoff, exit_cutoff):
        reasons.append("earnings")
    if _low_vol_blocks(row):
        reasons.append("low_vol")
    return reasons


def _short_gate_reasons(row, entry_cutoff, exit_cutoff):
    """Short-side twin of _long_gate_reasons -- RecommendationsView.tsx's
    eligibleToSell, each named independently. No EPS-trend check (removed
    from the live gate, see _long_gate_reasons' own comment).

    No momentum check -- removed, same as _long_gate_reasons.

    No entry_timing check -- same removal as _long_gate_reasons' own.

    growth: never short trailing OR expected revenue growth > 10% -- see
    _growth_blocks_short's own comment. No short_interest check -- that
    gate is retired, see this module's own top-of-file comment.

    sim_return: mirrors RecommendationsView.tsx's simReturnOkForShort --
    blocked when the Monte Carlo simulation says the price should RISE
    (simReturn > 0). Same forward-only caveat as _long_gate_reasons' own
    sim_return paragraph -- fails open for any week archived before
    simReturn started being written to sorted_screen <date>.csv."""
    reasons = []
    sim_return = _f(row.get("simReturn"))
    if sim_return is not None and sim_return > 0:
        reasons.append("sim_return")
    if _daily_move_blocks(row, "short"):
        reasons.append("daily_move")
    if _trend_blocks(row, "short"):
        reasons.append("trend")
    if _growth_blocks_short(row):
        reasons.append("growth")
    if _earnings_blocks(row, entry_cutoff, exit_cutoff):
        reasons.append("earnings")
    if _low_vol_blocks(row):
        reasons.append("low_vol")
    return reasons


def _group_for(rating, row, entry_cutoff, exit_cutoff):
    """(group, blockedBy) -- blockedBy is always [] for a non-blocked
    group (nothing to name), populated only for *_blocked. `rating` is
    passed in separately from `row` (rather than read off row['rating'])
    so the SAME row's factor columns (momentum/growth/etc, which the gate
    functions still read off `row`) can be classified under either the
    rating the snapshot actually shipped with or a re-scored counterfactual
    one -- see _rescore_current_model. entry_cutoff/exit_cutoff (this
    week's own forward-return window) are passed straight through to the
    earnings gate -- see _earnings_blocks."""
    if rating in _LONG_RATINGS:
        reasons = _long_gate_reasons(row, entry_cutoff, exit_cutoff)
        if reasons:
            return "long_blocked", reasons
        return ("long_strong_buy" if rating == "Strong Buy" else "long_buy"), []
    if rating in _SHORT_RATINGS:
        reasons = _short_gate_reasons(row, entry_cutoff, exit_cutoff)
        if reasons:
            return "short_blocked", reasons
        return ("short_strong_sell" if rating == "Strong Sell" else "short_sell"), []
    if rating == "Hold":
        return "hold", []  # no gates -- see module docstring's "hold" line
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


_GATE_REASONS = ("sim_return", "daily_move", "trend", "growth", "earnings", "low_vol")

# Two stricter, nested cuts of Strong Buy/Strong Sell (2.5% ⊂ 5% ⊂ the
# rating's own 10% -- scoring.RATING_THRESHOLDS) -- reported as extra
# reference stats in _summarize, not real gates: missing a cut still
# fully counts toward long_strong_buy/short_strong_sell, just not the
# tighter subset. The RESTRICTED_PCT_4/_2 NAMES are legacy (were 4%/2%
# before scoring.RATING_THRESHOLDS itself widened from 6% to 7.5%, same
# proportional widening applied here) -- left as-is rather than renaming
# these plus every "_4"/"_2"-suffixed group key/label that reads off
# them, which would be a much larger, purely cosmetic rename touching the
# frontend's own GroupKey/RestrictedGroupKey types too.
RESTRICTED_PCT_4 = 0.05
RESTRICTED_PCT_2 = 0.025


def _percentile_ranks(pairs):
    """{ticker: i/n} for (ticker, score) pairs, ascending by score -- 0 is
    the best score that week, approaching 1 the worst. Same convention
    main.py used to write the archived `rating` column."""
    ordered = sorted(pairs, key=lambda p: p[1])
    n = len(ordered)
    if n == 0:
        return {}
    return {ticker: i / n for i, (ticker, _) in enumerate(ordered)}


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
    """({ticker: rating}, {ticker: pct}) using TODAY's modules.scoring.
    score_rows, re-run on THIS SAME week's already-archived factor
    columns -- pct is the 0 (best)..1 (worst) percentile position
    rating_for_percentile was called with, exposed separately for the
    RESTRICTED_PCT_4/_2 cuts. "what would the
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
        return {}, {}
    scored = sorted(score_rows(rows), key=lambda item: item[2])
    n = len(scored)
    ratings = {symbol: rating_for_percentile(i / n) for i, (symbol, _, _) in enumerate(scored)}
    pcts = {symbol: i / n for i, (symbol, _, _) in enumerate(scored)}
    return ratings, pcts


def _daily_series(members, records):
    """{"dates": [...], "series": {key: [daily return, ...]}} for one week's
    holding period. Each series is built from the equal-weight mean of its
    members' CUMULATIVE signed P&L from entry (buy-and-hold, frozen after a
    take-profit fill), differenced day by day, and then expressed as a return
    on the PREVIOUS day's value, d / (1 + cum_prev) -- so compounding a week's
    daily values reproduces that group's weekly return exactly (and compounding
    across weeks reproduces the weekly page's Compounded). The three
    portfolios are the long leg + the short leg, each leg 100% equal-weight
    (the leg-balanced rule): their P&L increments are summed first, then
    rebased the same way. A date a position has no bar for carries its
    previous cumulative value. None when the week has no positions with a
    price path."""
    axis = max((list(r["cumByDate"]) for r in records if r.get("cumByDate")), key=len, default=[])
    if not axis:
        return None

    def cum_at(rec, day):
        value = 0.0
        for d, v in rec["cumByDate"].items():  # insertion order = date order
            if d > day:
                break
            value = v
        return value

    increments = {}
    for key, group_members in members.items():
        if not group_members:
            increments[key] = None
            continue
        cum = [sum(cum_at(m, d) for m in group_members) / len(group_members) for d in axis]
        increments[key] = [c - (cum[i - 1] if i else 0.0) for i, c in enumerate(cum)]

    def leg_sum(a, b):
        if increments.get(a) is None or increments.get(b) is None:
            return None
        return [x + y for x, y in zip(increments[a], increments[b])]

    increments["portfolio"] = leg_sum("long_strong_buy", "short_strong_sell")
    increments["portfolioRestricted4"] = leg_sum("long_strong_buy_restricted_4", "short_strong_sell_restricted_4")
    increments["portfolioRestricted2"] = leg_sum("long_strong_buy_restricted_2", "short_strong_sell_restricted_2")
    increments["allRatedLongShort"] = leg_sum("all_rated_long", "all_rated_short")

    series = {}
    for key, inc in increments.items():
        if inc is None:
            series[key] = None
            continue
        out, cum = [], 0.0
        for d in inc:
            out.append(round(d / (1.0 + cum), 6))  # return on the previous day's value
            cum += d
        series[key] = out
    return {"dates": axis, "series": series}


def _summarize(records):
    """{groups, portfolio, blockedBreakdown, tickers} from one already-
    classified record list -- shared by both the actual-rating model and
    the currentModel counterfactual in _build_week below, so the two stay
    byte-for-byte the same shape."""
    by_group = {g: [r for r in records if r["group"] == g] for g in GROUPS}
    groups = {g: _group_stats(by_group[g]) for g in GROUPS}
    daily_members = {
        "long_strong_buy": by_group["long_strong_buy"],
        "short_strong_sell": by_group["short_strong_sell"],
        "long_strong_buy_restricted_4": [r for r in by_group["long_strong_buy"] if r["pct"] is not None and r["pct"] < RESTRICTED_PCT_4],
        "long_strong_buy_restricted_2": [r for r in by_group["long_strong_buy"] if r["pct"] is not None and r["pct"] < RESTRICTED_PCT_2],
        "short_strong_sell_restricted_4": [r for r in by_group["short_strong_sell"] if r["pct"] is not None and r["pct"] >= 1 - RESTRICTED_PCT_4],
        "short_strong_sell_restricted_2": [r for r in by_group["short_strong_sell"] if r["pct"] is not None and r["pct"] >= 1 - RESTRICTED_PCT_2],
    }
    # Baselines shown next to the portfolios on the Daily tab (same definitions
    # as the Weekly tab's "All long" / "All rated long" / "All rated short"):
    # all_long = every rated name, treated as a long (a short-grouped name's
    # position P&L is un-flipped back to the stock's own return); all_rated_*
    # = the names actually rated for that side, position P&L as is.
    daily_members["all_long"] = [
        r if not r["group"].startswith("short") else {**r, "cumByDate": {d: -v for d, v in r["cumByDate"].items()}}
        for r in records
    ]
    daily_members["all_rated_long"] = [r for r in records if r["group"].startswith("long")]
    daily_members["all_rated_short"] = [r for r in records if r["group"].startswith("short")]
    daily = _daily_series(daily_members, records)

    # Nested restricted subsets (see RESTRICTED_PCT_4/_2 above) -- not a
    # partition, so missing a cut doesn't remove a name from
    # long_strong_buy/short_strong_sell. `pct` is None when _records_for
    # had no `score` column to reconstruct a percentile from that week --
    # excluded rather than guessed.
    groups["long_strong_buy_restricted_4"] = _group_stats(
        [r for r in by_group["long_strong_buy"] if r["pct"] is not None and r["pct"] < RESTRICTED_PCT_4]
    )
    groups["long_strong_buy_restricted_2"] = _group_stats(
        [r for r in by_group["long_strong_buy"] if r["pct"] is not None and r["pct"] < RESTRICTED_PCT_2]
    )
    groups["short_strong_sell_restricted_4"] = _group_stats(
        [r for r in by_group["short_strong_sell"] if r["pct"] is not None and r["pct"] >= 1 - RESTRICTED_PCT_4]
    )
    groups["short_strong_sell_restricted_2"] = _group_stats(
        [r for r in by_group["short_strong_sell"] if r["pct"] is not None and r["pct"] >= 1 - RESTRICTED_PCT_2]
    )

    # Dollar-neutral book: gated Strong Buy longs + gated Strong Sell
    # shorts, each leg equal-weight & 100% gross, P&L summed (already
    # position-signed); None if either leg is empty. Same combination one
    # nesting level down each time, using the 5%/2.5% legs instead of 10%.
    def _portfolio(long_key, short_key):
        sb, ss = groups[long_key], groups[short_key]
        return {
            "return": round(sb["return"] + ss["return"], 6) if sb["return"] is not None and ss["return"] is not None else None,
            "count": sb["count"] + ss["count"],
        }

    portfolio = _portfolio("long_strong_buy", "short_strong_sell")
    portfolio_restricted_4 = _portfolio("long_strong_buy_restricted_4", "short_strong_sell_restricted_4")
    portfolio_restricted_2 = _portfolio("long_strong_buy_restricted_2", "short_strong_sell_restricted_2")

    order = {g: i for i, g in enumerate(GROUPS)}
    tickers = sorted(
        (
            {
                "ticker": r["ticker"],
                "rating": r["rating"],
                "group": r["group"],
                "blockedBy": r["blockedBy"],
                "sector": r.get("sector"),
                "return": round(r["pnl"], 6),
                "pct": r["pct"],
            }
            for r in records
        ),
        key=lambda t: (order[t["group"]], -t["return"]),
    )

    return {
        "groups": groups,
        "portfolio": portfolio,
        "portfolioRestricted4": portfolio_restricted_4,
        "portfolioRestricted2": portfolio_restricted_2,
        "blockedBreakdown": _blocked_breakdown(by_group["long_blocked"], by_group["short_blocked"]),
        "daily": daily,
        "tickers": tickers,
    }


def _recompute_momentum_asof(rows, week_iso, daily_history, hourly_history):
    """Mutates `rows` (this week's already-loaded CSV rows) in place:
    overwrites momentum/entryTiming with what modules.derive.
    reconcile_momentum/reconcile_entry_timing would ACTUALLY have computed
    as of week_iso, using only price/volume bars dated on or before that
    day -- no lookahead. Explicit instruction, after confirming live that
    _rescore_current_model's own documented limitation (re-running
    today's ranking LOGIC over that week's already-archived, still-stale
    factor VALUES) was hiding the Trend Score's real effect entirely for
    already-backtested weeks: applying new logic to old numbers left most
    weeks' Strong Buy/Strong Sell group membership byte-for-byte
    unchanged. This closes that specific gap for momentum/entryTiming
    (still not for any OTHER factor's own computation change -- see
    _rescore_current_model's own docstring, point 1 -- those still need
    that week's raw provider dumps, which aren't archived).

    daily_history/hourly_history are the FULL (untruncated, current-day)
    loaded DAILY_3MO_HISTORY_FILE/HOURLY_HISTORY_FILE dicts -- truncated
    to <= week_iso HERE, per call, rather than the caller doing it once,
    so build_backtest can load each raw file exactly once and share it
    across every week's own cutoff. momentum is now the next-day Reversal
    Score, computed from daily bars only (hourly_trunc is passed to
    reconcile_momentum but unused) -- meanReversion/reconcile_mean_reversion
    is gone entirely (see derive.py's own retirement comment).

    Coverage caveat, confirmed live: HOURLY_HISTORY_FILE only carries
    ~3-4 weeks of history at any given time, so a week_iso older than
    that has almost no hourly bars to truncate down to (confirmed live:
    2026-08-22 had usable hourly data for only 8 of 1,770 tickers) --
    reconcile_entry_timing's own graceful-degrade (keep whatever was
    already on the row -- entryTiming simply stays unset if there's
    nothing to fall back to, since it's a new field, not a stale-value
    carryover) means entryTiming silently misses out for a week that old,
    while momentum (daily-only, backed by the 3-month-deep daily file) is
    reliably recomputed much further back.

    Also recomputes entryTiming here (same truncated hourly_trunc, 35h
    formation -- see derive.reconcile_entry_timing) -- no longer a gate
    (see this module's own top-of-file comment on its removal), but still
    recomputed no-lookahead so any correlation check run against this
    module's tickers still reflects what the signal would ACTUALLY have
    read at week_iso, not today's live value -- same reasoning momentum
    gets above."""
    data = {r["ticker"]: r for r in rows if r.get("ticker")}
    daily_trunc = {t: [b for b in bars if (b.get("date") or "")[:10] <= week_iso] for t, bars in daily_history.items()}
    hourly_trunc = {t: [b for b in bars if (b.get("date") or "")[:10] <= week_iso] for t, bars in hourly_history.items()}
    derive.reconcile_momentum(data, daily_trunc, hourly_trunc)
    derive.reconcile_trend(data, daily_trunc, hourly_trunc)
    derive.reconcile_daily_move(data, daily_trunc)
    derive.reconcile_entry_timing(data, hourly_trunc)


HOLDING_TRADING_DAYS = 5

# Take-profit -- explicit instruction (2026-10-01), mirroring the AUTOMATIC
# live orders ib_server.py places (see its take_profit_loop): once a held
# position is up TAKE_PROFIT_TRIGGER_SD sd on the day (vs. the previous
# close), a closing LIMIT is placed at previous close x (1 +/-
# TAKE_PROFIT_LIMIT_SD sd). With daily bars that limit fills iff the day's
# HIGH (long) / LOW (short) reaches it -- reaching 1.6 sd implies 1.5 sd
# was crossed first, so the trigger never changes whether it fills -- at
# the limit, or at the OPEN if the stock gapped through it. A day order
# that doesn't fill expires and is re-armed the next day; otherwise the
# position is held the full HOLDING_TRADING_DAYS. sd = sample stdev of the
# up-to-derive.DAILY_MOVE_SD_DAYS daily returns ending at the entry bar
# (known at entry, no lookahead). On the 6 archived weeks: filled on ~16%
# of Strong Buy/Strong Sell positions (+0.87% each vs holding), portfolio
# +1.05% (hold) / +1.21% (old exit-at-close-on-a-1.5sd-day) -> +1.29%/wk.
TAKE_PROFIT_TRIGGER_SD = 1.5
TAKE_PROFIT_LIMIT_SD = 1.6


def _entry_sigma(series, entry_date):
    """Sample stdev of the daily returns over the up-to-DAILY_MOVE_SD_DAYS
    bars ending at entry_date, or None with fewer than DAILY_MOVE_MIN_DAYS
    returns (then the take-profit rule simply doesn't apply)."""
    if not series:
        return None
    idx = next((i for i, (d, _) in enumerate(series) if d == entry_date), None)
    if idx is None:
        return None
    hist = [c for _, c in series[max(0, idx - derive.DAILY_MOVE_SD_DAYS): idx + 1]]
    rets = [b / a - 1 for a, b in zip(hist, hist[1:]) if a]
    if len(rets) < derive.DAILY_MOVE_MIN_DAYS:
        return None
    sd = statistics.stdev(rets)
    return sd if sd > 0 else None


# ---- Intraday (hourly-bar) execution of the take-profit / entry rules -------
# Explicit instruction (2026-10-03): the rules are tested on HOURLY bars, not just
# daily highs/lows, mirroring the Trading robot (ib_server.py, which checks every
# 10 minutes): at each hourly bar CLOSE, if the price is >= TAKE_PROFIT_TRIGGER_SD
# sigma away from the previous daily close IN THE RULE'S DIRECTION and it moved no
# more than TAKE_PROFIT_MAX_STEP_SD sigma FURTHER in that direction since the previous
# bar's close (the one-sided stability test, 1 hour, same as the live robot's lookback; not applied on the day's last hourly bar), an order is placed
# -- ONE per ticker per day (like the robot). If the bar's close is already
# beyond the TAKE_PROFIT_LIMIT_SD sigma level the order rests on the passive
# quote, modelled as filled at that bar's CLOSE; otherwise it is a resting limit at
# previous close x (1 +/- LIMIT sigma) that fills at the first LATER bar of the
# day whose high/low reaches it (at that bar's open if it gapped through), and
# expires unfilled at the close. A day with no hourly bars falls back to the
# daily-bar approximation (limit fills iff the day's high/low reaches it).
TAKE_PROFIT_MAX_STEP_SD = 0.5  # over 1 hour, here and in ib_server.py


def _hourly_by_day(bars):
    """{day: [(open, high, low, close), ...]} in time order from a ticker's
    hourly bars."""
    out = {}
    for b in sorted(bars or [], key=lambda x: x.get("date") or ""):
        d = (b.get("date") or "")[:10]
        o, h, l, c = _f(b.get("open")), _f(b.get("high")), _f(b.get("low")), _f(b.get("close"))
        if d and None not in (o, h, l, c):
            out.setdefault(d, []).append((o, h, l, c))
    return out


def _intraday_fill(day_bars, prev_close, sigma, direction, entry=False):
    """Fill price of the rule's order on one day, or None. direction = +1 when
    the rule needs the price to RISE vs the previous close (take-profit on a
    long, entry of a short), -1 when it needs it to FALL (take-profit on a
    short, entry of a long). See the block comment above. entry=True (the
    entry rule): there is no resting limit -- at a bar that triggered (>= 1.5σ
    move) and is not still running (stability test), the entry is made at that
    bar's close only if the price is beyond the 1.6σ level; if it has pulled
    back between 1.5σ and 1.6σ, no entry on this bar and the scan goes on."""
    level = prev_close * (1 + direction * TAKE_PROFIT_LIMIT_SD * sigma)
    last_close = day_bars[0][0]  # the first bar's "previous hour" price is its own open
    for k, (_, _, _, close) in enumerate(day_bars):
        moved = direction * (close / prev_close - 1)
        # One-sided stability: the price must not have run a further
        # TAKE_PROFIT_MAX_STEP_SD sigma IN THE RULE'S DIRECTION over the last hour
        # (a long entry: not -0.5σ vs the previous hour; a short entry: not +0.5σ).
        # Skipped on the day's last bar (15:00 NY = 21:00 Rome, the final hour).
        stable = k == len(day_bars) - 1 or direction * (close / last_close - 1) < TAKE_PROFIT_MAX_STEP_SD * sigma
        last_close = close
        if moved < TAKE_PROFIT_TRIGGER_SD * sigma or not stable:
            continue
        if direction * (close - level) >= 0:
            return close  # already beyond the limit level: passive quote ~ this bar's close
        if entry:
            continue  # pulled back inside 1.6σ: no entry now, look at the next bar
        for o, h, l, _ in day_bars[k + 1:]:  # resting limit: first later bar that reaches it
            reach = h if direction > 0 else l
            if direction * (reach - level) >= 0:
                return o if direction * (o - level) > 0 else level
        return None  # one order per day: unfilled, expires at the close
    return None


def _position_cum(path, sigma, sign, bars=None, with_exit=False, intraday=None):
    """Cumulative signed P&L from entry at each later bar of `path` [(date,
    close), ...] -- one value per bar after the entry bar -- with the take-
    profit limit applied (see TAKE_PROFIT_LIMIT_SD): each day, a limit at
    the previous close x (1 + sign x TAKE_PROFIT_LIMIT_SD x sigma) fills when
    that day's high (long) / low (short) reaches it -- at the open if it
    gapped through. `bars` = {date: (open, high, low)} for this ticker; a
    day without them falls back to its close reaching the limit. After a fill
    the cumulative P&L stays frozen at the fill; otherwise the position is
    carried to the last bar. with_exit=True returns (cum, exit_index) where
    exit_index is the path index of the take-profit fill day (None if held)."""
    c0 = path[0][1]
    cum = []
    exited = None
    exit_i = None
    for i in range(1, len(path)):
        if exited is not None:
            cum.append(exited)
            continue
        day, close = path[i]
        if sigma:
            if intraday and day in intraday:
                fill = _intraday_fill(intraday[day], path[i - 1][1], sigma, sign)
            else:  # no hourly bars for this day: daily-bar approximation
                limit = path[i - 1][1] * (1 + sign * TAKE_PROFIT_LIMIT_SD * sigma)
                o, h, l = (bars or {}).get(day, (None, None, None))
                reach = (h if sign > 0 else l) if (h is not None and l is not None) else close
                fill = None
                if sign * (reach - limit) >= 0:
                    fill = limit
                    if o is not None and sign * (o - limit) > 0:
                        fill = o
            if fill is not None:
                exited = sign * (fill / c0 - 1)
                exit_i = i
                cum.append(exited)
                continue
        cum.append(sign * (close / c0 - 1))
    return (cum, exit_i) if with_exit else cum


def _position_pnl(path, sigma, sign, bars=None, intraday=None):
    """Signed P&L of one position over its whole hold (the last value of
    _position_cum)."""
    return _position_cum(path, sigma, sign, bars, intraday=intraday)[-1]


# ---- Entry rule overlay (the Trading robot's log-only entry rule) ----------
# Explicit instruction (2026-10-03): positions NOT in the weekly portfolio get
# the robot's entry rule applied in the backtest. Candidates are Strong Buy /
# Strong Sell names that (a) were left out of the portfolio only because the
# file-based daily-move gate fired (the robot replaces that gate by the live
# move test below) -- every other gate must pass -- or (b) were in the portfolio
# but already CLOSED by the take-profit limit (no longer held, so re-enterable
# after the exit day). Rules, approximated with daily bars: portfolio beta
# (week-start portfolio, Positions-page definition) must allow the side (buy
# needs beta < +ENTRY_BETA_BAND, sell needs beta > -band); the net sector /
# industry weight of the week-start portfolio (each leg 100% equal-weight)
# must not exceed +/-ENTRY_SECTOR_NET_LIMIT / ENTRY_INDUSTRY_NET_LIMIT against
# the side; the trigger / stability / fill logic runs on HOURLY bars (see
# _intraday_fill: a Strong Buy needs the price at least TAKE_PROFIT_TRIGGER_SD
# sigma BELOW the previous close at an hourly close, stable vs the previous
# hour, and fills at the -TAKE_PROFIT_LIMIT_SD sigma limit or the hour's close
# if already beyond; a Strong Sell mirrors it upward). One entry per ticker per
# week; held to the week's last close (no take-profit on the entered
# position). Sized ENTRY_SIZE_PCT of NAV, added on top of the portfolio.
# NOT modelled: the 3%-of-NAV price cap, integer share rounding, take-profit
# on the entered position, bid/ask spreads.
ENTRY_BETA_BAND = 0.2
ENTRY_SECTOR_NET_LIMIT = 0.10
ENTRY_INDUSTRY_NET_LIMIT = 0.05
ENTRY_SIZE_PCT = 0.02
ENTRY_TRIGGER_GATES = {"daily_move"}  # reasons the entry rule overrides (replaced by the live move test)


def _entry_rule_overlay(current_records, rows_by_ticker, paths, ohlc, sigmas, intra=None, cut=None):
    """{"trades": [...], "count", "buys", "sells", "contribution", "daily":
    {date: increment}} for one week, or None when nothing triggered. See the
    block comment above for the rules. `cut` (None = the 10% portfolio, or
    RESTRICTED_PCT_4/_2) restricts the portfolio AND the entry candidates to
    that percentile cut, so the 5% / 2.5% portfolios get their own entries."""
    from modules.sector_groups import get_sector_group

    def in_cut(r):
        if cut is None:
            return True
        pct = r.get("pct")
        if pct is None:
            return False
        return pct < cut if r["rating"] in ("Strong Buy", "Buy") or r["group"].startswith("long") else pct >= 1 - cut

    current_records = [r for r in current_records if in_cut(r)]
    members = [r for r in current_records if r["group"] in ("long_strong_buy", "short_strong_sell")]
    longs = [r for r in members if r["group"] == "long_strong_buy"]
    shorts = [r for r in members if r["group"] == "short_strong_sell"]
    if not longs or not shorts:
        return None

    def beta_of(t):
        b = _f(rows_by_ticker.get(t, {}).get("beta"))
        return 1.0 if b is None else b

    beta = (sum(beta_of(r["ticker"]) for r in longs) / len(longs) - sum(beta_of(r["ticker"]) for r in shorts) / len(shorts)) / 2
    buy_ok = beta < ENTRY_BETA_BAND
    sell_ok = beta > -ENTRY_BETA_BAND
    sector_net, industry_net = {}, {}
    for leg, sgn in ((longs, 1.0), (shorts, -1.0)):
        for r in leg:
            ind = rows_by_ticker.get(r["ticker"], {}).get("sector") or ""
            w = sgn / len(leg)
            industry_net[ind] = industry_net.get(ind, 0.0) + w
            sg = get_sector_group(ind)
            sector_net[sg] = sector_net.get(sg, 0.0) + w

    trades = []
    for r in current_records:
        if r["rating"] not in ("Strong Buy", "Strong Sell"):
            continue
        is_long = r["rating"] == "Strong Buy"
        path = paths.get(r["ticker"])
        sigma = sigmas.get(r["ticker"])
        if not path or not sigma or len(path) < 2:
            continue
        if r["group"] in ("long_blocked", "short_blocked"):
            if not r["blockedBy"] or not set(r["blockedBy"]) <= ENTRY_TRIGGER_GATES:
                continue
            first_day = 1
        elif r["group"] in ("long_strong_buy", "short_strong_sell") and r.get("exitDate"):
            first_day = next(i for i, (d, _) in enumerate(path) if d == r["exitDate"]) + 1
        else:
            continue
        if (is_long and not buy_ok) or (not is_long and not sell_ok):
            continue
        ind = rows_by_ticker.get(r["ticker"], {}).get("sector") or ""
        sec_w, ind_w = sector_net.get(get_sector_group(ind), 0.0), industry_net.get(ind, 0.0)
        if is_long and (sec_w > ENTRY_SECTOR_NET_LIMIT or ind_w > ENTRY_INDUSTRY_NET_LIMIT):
            continue
        if not is_long and (sec_w < -ENTRY_SECTOR_NET_LIMIT or ind_w < -ENTRY_INDUSTRY_NET_LIMIT):
            continue
        side = 1 if is_long else -1
        for i in range(first_day, len(path)):
            day, close = path[i]
            prev_close = path[i - 1][1]
            day_bars = (intra(r["ticker"]) or {}).get(day) if intra else None
            if day_bars:
                fill = _intraday_fill(day_bars, prev_close, sigma, -side, entry=True)  # an entry needs the OPPOSITE move
                if fill is None:
                    continue
            else:  # no hourly bars that day: daily-bar approximation
                o, h, l = ohlc.get(r["ticker"], {}).get(day, (None, None, None))
                reach = l if is_long else h
                if reach is None:
                    continue
                trigger = prev_close * (1 - side * TAKE_PROFIT_TRIGGER_SD * sigma)
                limit = prev_close * (1 - side * TAKE_PROFIT_LIMIT_SD * sigma)
                if side * (trigger - reach) < 0 or side * (limit - reach) < 0:
                    continue  # never got to the trigger / the limit
                fill = o if (o is not None and side * (limit - o) >= 0) else limit
            pnl = side * (path[-1][1] / fill - 1)
            increments = {day: ENTRY_SIZE_PCT * side * (close / fill - 1)}
            for j in range(i + 1, len(path)):
                increments[path[j][0]] = ENTRY_SIZE_PCT * side * (path[j][1] / path[j - 1][1] - 1)
            trades.append({"ticker": r["ticker"], "side": "BUY" if is_long else "SELL", "date": day,
                           "fill": round(fill, 4), "pnl": round(pnl, 6), "increments": increments,
                           "source": "daily-move gate" if r["group"].endswith("blocked") else "after take-profit exit"})
            break
    if not trades:
        return None
    daily = {}
    for t in trades:
        for d, v in t["increments"].items():
            daily[d] = daily.get(d, 0.0) + v
    return {
        "trades": [{k: v for k, v in t.items() if k != "increments"} for t in trades],
        "count": len(trades),
        "buys": sum(t["side"] == "BUY" for t in trades),
        "sells": sum(t["side"] == "SELL" for t in trades),
        "contribution": round(sum(ENTRY_SIZE_PCT * t["pnl"] for t in trades), 6),
        "portfolioBeta": round(beta, 3),
        "daily": {d: round(v, 6) for d, v in daily.items()},
    }


def _add_entry_overlay_to_daily(daily, overlay, key="portfolio"):
    """Replaces series[key] (portfolio / portfolioRestricted4 / ...) with its
    daily returns plus the entry-rule trades' P&L on top (same rebase-to-
    previous-day as the other series). `daily` = this week's {"dates",
    "series"} (mutated)."""
    base = daily["series"].get(key)
    if not base:
        return
    increments, cum = [], 0.0
    for r in base:  # recover the additive increments from the rebased returns
        d = r * (1.0 + cum)
        increments.append(d)
        cum += d
    out, cum = [], 0.0
    for date_, d in zip(daily["dates"], increments):
        d += overlay["daily"].get(date_, 0.0)
        out.append(round(d / (1.0 + cum), 6))
        cum += d
    daily["series"][key] = out


def _build_week(week_iso, csv_path, closes, daily_history=None, hourly_history=None):
    screen_date = date.fromisoformat(week_iso)
    entry_cutoff = week_iso
    # Generous calendar buffer (holidays + weekends can eat up to ~4 extra
    # calendar days out of 5 trading days) -- _window_series just needs an
    # outer bound to stop scanning at; the actual holding length is
    # enforced exactly below by trimming to HOLDING_TRADING_DAYS bars,
    # not by this cutoff. Explicit instruction: hold exactly 5 TRADING
    # days (not the old "7 calendar days," which drifted between 3 and 5
    # trading days depending on where the weekend/a holiday fell).
    exit_cutoff = (screen_date + timedelta(days=12)).isoformat()

    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))

    if daily_history is not None and hourly_history is not None:
        _recompute_momentum_asof(rows, week_iso, daily_history, hourly_history)

    # Each ticker's price path is computed ONCE and shared by both models
    # below -- it depends only on the ticker/dates, not on which rating
    # classifies it (long vs short, which flips the pnl sign, is applied
    # per-model in _records_for).
    paths = {}
    sigmas = {}
    hourly_cache = {}

    def intra(ticker):
        """{day: hourly bars} for a ticker (built once), or None without hourly data."""
        if hourly_history is None:
            return None
        if ticker not in hourly_cache:
            hourly_cache[ticker] = _hourly_by_day(hourly_history.get(ticker))
        return hourly_cache[ticker] or None
    # {ticker: {date: (open, high, low)}} for the take-profit limit fill check
    ohlc = {}
    if daily_history:
        for t, bars in daily_history.items():
            ohlc[t] = {
                (b.get("date") or "")[:10]: (_f(b.get("open")), _f(b.get("high")), _f(b.get("low")))
                for b in bars or []
            }
    for row in rows:
        ticker = row.get("ticker")
        if not ticker:
            continue
        path = _window_series(closes.get(ticker), entry_cutoff, exit_cutoff)
        if path:
            # Entry bar + up to HOLDING_TRADING_DAYS bars after it -- if
            # fewer are actually available (short data at the edge of the
            # cache), use what's there rather than dropping the ticker.
            paths[ticker] = path[: HOLDING_TRADING_DAYS + 1]
            sigmas[ticker] = _entry_sigma(closes.get(ticker), path[0][0])

    def _records_for(rating_of, pct_of):
        records = []
        for row in rows:
            path = paths.get(row.get("ticker"))
            if not path:
                continue
            group, reasons = _group_for(rating_of(row), row, entry_cutoff, exit_cutoff)
            if group is None:
                continue
            sign = -1.0 if group.startswith("short") else 1.0  # long_* and hold both held long
            cum, exit_i = _position_cum(path, sigmas.get(row["ticker"]), sign, ohlc.get(row["ticker"]), with_exit=True,
                                        intraday=intra(row["ticker"]))
            records.append({
                "exitDate": path[exit_i][0] if exit_i is not None else None,
                "ticker": row["ticker"],
                "rating": rating_of(row),
                "group": group,
                "blockedBy": reasons,
                "sector": row.get("sector") or None,
                "pnl": cum[-1],
                "cumByDate": {d: v for (d, _), v in zip(path[1:], cum)},
                "pct": pct_of(row),
            })
        return records

    # Actual model's percentile isn't archived, but is reconstructable
    # from the archived `score` column directly -- no rescoring needed,
    # unlike the rating itself.
    actual_pct = _percentile_ranks(
        [(r["ticker"], _f(r.get("score"))) for r in rows if r.get("ticker") and _f(r.get("score")) is not None]
    )
    actual_records = _records_for(lambda row: row.get("rating"), lambda row: actual_pct.get(row.get("ticker")))
    rescored, current_pct = _rescore_current_model(rows)
    current_records = _records_for(lambda row: rescored.get(row.get("ticker")), lambda row: current_pct.get(row.get("ticker")))

    result = {
        "week": week_iso,
        "entryDate": min((p[0][0] for p in paths.values()), default=None),
        "exitDate": max((p[-1][0] for p in paths.values()), default=None),
    }
    result.update(_summarize(actual_records))
    result["currentModel"] = _summarize(current_records)
    by_ticker = {r["ticker"]: r for r in rows if r.get("ticker")}
    for key, cut, field in (("portfolio", None, "entryRule"), ("portfolioRestricted4", RESTRICTED_PCT_4, "entryRuleRestricted4"),
                            ("portfolioRestricted2", RESTRICTED_PCT_2, "entryRuleRestricted2")):
        overlay = _entry_rule_overlay(current_records, by_ticker, paths, ohlc, sigmas, intra, cut)
        if overlay:
            result["currentModel"][field] = overlay
            if result["currentModel"].get("daily"):
                _add_entry_overlay_to_daily(result["currentModel"]["daily"], overlay, key)
            # The Weekly tab shows the same portfolio WITH its entries: the week's
            # return = the compounded daily series just rebuilt (equal to the old
            # figure when no entry trades fired).
            series = (result["currentModel"].get("daily") or {}).get("series", {}).get(key)
            if series and result["currentModel"].get(key):
                total = 1.0
                for x in series:
                    total *= 1.0 + x
                result["currentModel"][key]["returnWithoutEntries"] = result["currentModel"][key]["return"]
                result["currentModel"][key]["return"] = round(total - 1.0, 6)
    return result


def _load_json_or_empty(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def build_backtest(history_dir, daily_file, hourly_file=None):
    """See module docstring. Returns the JSON-ready result dict. Weeks with
    no price coverage yet -- the newest snapshot whose forward week hasn't
    finished, or any week older than IB's ~3-month daily history -- are
    dropped rather than rendered as an all-blank column.

    `hourly_file` (HOURLY_HISTORY_FILE), when given, additionally
    recomputes momentum/meanReversion for EVERY week as of that week's
    own date (see _recompute_momentum_asof) instead of trusting whatever
    was archived in that week's own CSV snapshot -- the raw daily/hourly
    dumps are each loaded ONCE here and truncated per-week inside
    _build_week, not reloaded per week. Omit (None, the default) to fall
    back to the old archived-value behavior for callers that don't have
    these files handy."""
    closes = _load_daily_closes(daily_file)
    # Always loaded now: the take-profit limit needs each day's open/high/low
    # (_position_pnl), not just closes. Momentum is still only recomputed
    # as-of each week when hourly_history is also given (see _build_week).
    daily_history = _load_json_or_empty(daily_file)
    hourly_history = _load_json_or_empty(hourly_file) if hourly_file is not None else None
    weeks = [
        w
        for week_iso, path in _history_files(history_dir)
        if (w := _build_week(week_iso, path, closes, daily_history, hourly_history))["tickers"]
    ]
    return {
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "weeks": weeks,
    }
