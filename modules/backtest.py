"""backtest.py -- forward weekly performance of each historical screen's
RECOMMENDATION groups.

For every ``data/output/history/sorted_screen <YYYYMMDD>.csv`` snapshot,
every rated candidate (Strong Buy / Buy / Sell / Strong Sell -- the same
set the Recommendations page draws from) is put into one of six groups
by the same entry gates RecommendationsView.tsx applies:

  long_strong_buy   Strong Buy that clears the long gates
  long_buy          Buy that clears the long gates
  long_blocked      Buy/Strong Buy that fails one (weak momentum, a
                    simulation saying it should fall, earnings within
                    the week)
  short_strong_sell Strong Sell that clears the short gates
  short_sell        Sell that clears the short gates
  short_blocked     Sell/Strong Sell that fails one (strong momentum, a
                    simulation saying it should rise, strong revenue
                    growth, earnings within the week)

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
# MSI is now a pure two-threshold continuation gate: long blocked at/below
# NO_BUY, short blocked at/above NO_SELL, nothing else. The old
# mean-reversion carve-outs at the far extremes (buy-the-dip below
# _MOMENTUM_OVERSOLD, short-the-top above _MOMENTUM_OVERBOUGHT, and the
# asymmetric _MOMENTUM_SHORT_OVERSOLD=15 floor) were all dropped: hourly
# entry-timing analysis showed every counter-trend entry (buy oversold /
# falling knife, short overbought / strong uptrend) lost ~1.5-3.4% over
# the next 3 days with 20-40% hit rates, while every continuation entry
# (buy uptrend / overbought, short falling knife / oversold) made
# ~1.4-3.4% at 60-78% hit. OVERSOLD/OVERBOUGHT survive only as zone-label
# text in RecommendationsView.tsx, not as gate thresholds.
_MOMENTUM_NO_BUY = 35
_MOMENTUM_NO_SELL = 65
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
_SHORT_GROWTH_CEILING = 0.10  # RecommendationsView.tsx SHORT_GROWTH_CEILING


def _growth_blocks_short(row):
    trailing = _f(row.get("revenueGrowth"))
    expected = _f(row.get("eulerRevGrowth1y"))
    return (trailing is not None and trailing > _SHORT_GROWTH_CEILING) or (
        expected is not None and expected > _SHORT_GROWTH_CEILING
    )

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

    Momentum (MSI) blocks a long at or below NO_BUY -- the whole
    weak-momentum half, oversold and falling knife alike (continuation:
    buying weakness kept losing over the next few days). Everything above
    NO_BUY, overbought included, is fine. Mirrors
    RecommendationsView.tsx's momentumBlocks('Long'). No EPS-trend check
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
    momentum = _f(row.get("momentum"))
    if momentum is None or momentum <= _MOMENTUM_NO_BUY:
        reasons.append("momentum")
    sim_return = _f(row.get("simReturn"))
    if sim_return is not None and sim_return < 0:
        reasons.append("sim_return")
    if _earnings_blocks(row, entry_cutoff, exit_cutoff):
        reasons.append("earnings")
    return reasons


def _short_gate_reasons(row, entry_cutoff, exit_cutoff):
    """Short-side twin of _long_gate_reasons -- RecommendationsView.tsx's
    eligibleToSell, each named independently. No EPS-trend check (removed
    from the live gate, see _long_gate_reasons' own comment).

    Momentum (MSI) blocks a short at or above NO_SELL -- the whole
    strong-momentum half, strong uptrend and overbought alike
    (continuation: shorting strength kept losing). Everything below
    NO_SELL, oversold included, is fine. Mirrors
    RecommendationsView.tsx's momentumBlocks('Short').

    No entry_timing check -- same removal as _long_gate_reasons' own.

    growth: added after confirming live that this module had drifted out
    of sync with RecommendationsView.tsx's own growthBlocksShortEntry
    (S, PANW both showed up as clean short_strong_sell weeks with an ugly
    loss when the live app would already refuse to short either today)
    -- see _growth_blocks_short's own comment. No short_interest check --
    that gate is retired, see this module's own top-of-file comment.

    sim_return: mirrors RecommendationsView.tsx's simReturnOkForShort --
    blocked when the Monte Carlo simulation says the price should RISE
    (simReturn > 0). Same forward-only caveat as _long_gate_reasons' own
    sim_return paragraph -- fails open for any week archived before
    simReturn started being written to sorted_screen <date>.csv."""
    reasons = []
    momentum = _f(row.get("momentum"))
    if momentum is None or momentum >= _MOMENTUM_NO_SELL:
        reasons.append("momentum")
    sim_return = _f(row.get("simReturn"))
    if sim_return is not None and sim_return > 0:
        reasons.append("sim_return")
    if _growth_blocks_short(row):
        reasons.append("growth")
    if _earnings_blocks(row, entry_cutoff, exit_cutoff):
        reasons.append("earnings")
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


_GATE_REASONS = ("momentum", "sim_return", "growth", "earnings")


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
                "sector": r.get("sector"),
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
    across every week's own cutoff. hourly_trunc feeds reconcile_momentum
    too now (the trend_health gate) -- meanReversion/reconcile_mean_reversion
    is gone entirely (see derive.py's own retirement comment); trend_health
    replaces its role, folded directly into momentum instead of a
    standalone field.

    Coverage caveat, confirmed live: HOURLY_HISTORY_FILE only carries
    ~3-4 weeks of history at any given time, so a week_iso older than
    that has almost no hourly bars to truncate down to (confirmed live:
    2026-08-22 had usable hourly data for only 8 of 1,770 tickers) --
    reconcile_entry_timing's own graceful-degrade (keep whatever was
    already on the row -- entryTiming simply stays unset if there's
    nothing to fall back to, since it's a new field, not a stale-value
    carryover) means entryTiming silently misses out for a week that old,
    and the trend_health gate inside reconcile_momentum contributes zero
    for the same tickers that week, while r10/r5/vol_accel (backed by the
    3-month-deep daily file) are reliably recomputed much further back.

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
    derive.reconcile_entry_timing(data, hourly_trunc)


HOLDING_TRADING_DAYS = 5

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

    def _records_for(rating_of):
        records = []
        for row in rows:
            path = paths.get(row.get("ticker"))
            if not path:
                continue
            group, reasons = _group_for(rating_of(row), row, entry_cutoff, exit_cutoff)
            if group is None:
                continue
            sign = 1.0 if group.startswith("long") else -1.0
            records.append({
                "ticker": row["ticker"],
                "rating": rating_of(row),
                "group": group,
                "blockedBy": reasons,
                "sector": row.get("sector") or None,
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
    daily_history = _load_json_or_empty(daily_file) if hourly_file is not None else None
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
