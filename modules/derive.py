"""derive.py -- pure raw -> derived transforms.

Everything here reads only from the raw provider dumps on disk
(raw_data.json = yfinance .info, raw_yf_statements.json = yfinance
statement DataFrames, company_facts.json = SEC XBRL) and computes the
"worked" values the scorer consumes. NO network, no yfinance/IB/SEC
objects -- so main.recalc() can rebuild every derived field without
touching a provider.

Split out of IBApp.get_forward_pe (which used to fetch AND compute in one
pass) and main._reconcile_revenue_growth so the download step can be a
plain fetch and this step a plain recompute.
"""

import math
import statistics
from datetime import date, timedelta

from modules.scoring import clamp_eps_revision
from modules.sector_groups import get_sector_group


def to_float(x):
    try:
        v = float(x)
        return v if v == v and abs(v) != math.inf else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
#  small numeric transforms (moved verbatim from IBApp)                        #
# --------------------------------------------------------------------------- #
MARGIN_FLOOR = -3.0  # -300%
MARGIN_CAP = 2.0  # +200%; above this is essentially always a tiny-revenue artifact


def clamp_margin(value):
    """operatingMargins / grossMargins clamped to [MARGIN_FLOOR, MARGIN_CAP]:
    a near-zero-revenue name's ratio can explode to a mathematically-correct
    but meaningless magnitude, and both margins are the same revenue-
    denominated shape of ratio. None passes through untouched."""
    v = to_float(value)
    if v is None:
        return None
    return max(MARGIN_FLOOR, min(MARGIN_CAP, v))


def eps_revision(current, baseline):
    """Capped (current - baseline) / abs(baseline) -- how much a consensus
    EPS estimate moved vs an earlier snapshot of itself (yfinance
    get_eps_trend()'s "current" vs "30daysAgo" for a period). Positive =
    analysts raising the estimate. None for a missing/NaN input or a zero
    baseline. The cap (clamp_eps_revision) stops near-zero priors becoming
    huge percentage artifacts."""
    current, baseline = to_float(current), to_float(baseline)
    if current is None or baseline is None or baseline == 0:
        return None
    return clamp_eps_revision((current - baseline) / abs(baseline))


# Ceiling on eps_volatility's own ratio -- explicit instruction ("the
# penalty looks too harsh... a way to soften the impact of epsVolatility").
# Every comparable ratio in this file caps the top as well as the bottom
# (GROWTH_CAP=1.0, EARN_MARGIN_DELTA_CAP=0.9); epsVolatility had a FLOOR
# (FALLBACK_EPS_REL_STDEV, simulations.py -- protects the too-LOW side)
# but no matching ceiling, so a name with a genuinely lumpy history (or a
# small mean(|EPS|) denominator) could push the ratio arbitrarily high --
# confirmed live on FEIM: even merged onto its full SEC history (11 years,
# not just yfinance's 4), epsVolatility still read 1.26 (126% relative
# swing), which alone was enough to pin simulations.py's risk-premium
# haircut (RISK_PREMIUM_K) at its own floor -- the maximum discount the
# model can apply to any stock, floor-bound for anything >= ~1.15. Capped
# here at the source, same place MARGIN_FLOOR/MARGIN_CAP and
# EARN_MARGIN_DELTA_CAP already clamp their own ratios, so every consumer
# (eps_volatility_rank, simulations.py's combinedVol/risk-premium haircut)
# sees the same bounded number without each having to re-clamp it.
EPS_VOLATILITY_CAP = 1.0


def eps_volatility(values):
    """stdev of YEAR-OVER-YEAR EPS growth rates (values must already be in
    CHRONOLOGICAL, oldest-first order), capped at EPS_VOLATILITY_CAP (low
    is better -- scoring.eps_volatility_rank). Detrended -- NOT
    stdev(levels)/mean(|levels|), the old formula, which conflated a
    steady long-term growth TREND with genuine volatility: confirmed live,
    MSFT's own 19-year merged SEC history (a smooth ~10x compounding
    trend, $1.87 -> $17.95, with only two well-known one-time dips -- the
    FY2015 Nokia writedown, the FY2018 TCJA one-time tax charge) read 86%
    "volatility" under the old formula purely from the LEVEL span, not
    from any real year-to-year unpredictability -- it couldn't tell "grew
    smoothly and enormously" apart from "swings unpredictably." stdev of
    YoY growth RATES instead measures dispersion around whatever the
    average growth rate actually is, so a smooth compounder reads as
    low-risk regardless of how much it grew in total; only genuine
    non-monotonic swings inflate it. Each YoY rate is clamped via
    clamp_eps_revision (the same [-1, +1] cap eps_revision itself uses)
    before the stdev, guarding a near-zero-or-negative prior EPS from
    producing one extreme growth-rate outlier that would dominate the
    whole series -- the same near-zero-denominator risk this project has
    repeatedly had to guard elsewhere. None if fewer than 3 usable YoY
    growth rates (4 EPS points) can be formed."""
    values = [v for v in (to_float(x) for x in values) if v is not None]
    if len(values) < 4:
        return None
    growths = [
        clamp_eps_revision((cur - prev) / abs(prev))
        for prev, cur in zip(values, values[1:])
        if prev != 0
    ]
    if len(growths) < 3:
        return None
    return min(statistics.stdev(growths), EPS_VOLATILITY_CAP)


# --------------------------------------------------------------------------- #
#  yfinance statement-DataFrame serialisation + parsing                        #
# --------------------------------------------------------------------------- #
def df_to_dict(df):
    """A yfinance statement DataFrame -> {rowLabel: {colKey: float|None}}.
    Column keys are ISO date strings when the columns are Timestamps
    (income_stmt / quarterly_income_stmt), plain strings otherwise
    (get_eps_trend / get_earnings_estimate). Returns {} for None/empty."""
    if df is None or getattr(df, "empty", True):
        return {}

    def col_key(c):
        try:
            return str(c.date())
        except AttributeError:
            return str(c)

    out = {}
    for label in df.index:
        row = {}
        for c in df.columns:
            row[col_key(c)] = to_float(df.loc[label, c])
        out[str(label)] = row
    return out


def _row(stmts, which, label):
    return ((stmts or {}).get(which) or {}).get(label) or {}


def _dates_desc(row):
    """ISO-date keys of a serialised statement row, newest first."""
    out = []
    for k in row:
        try:
            out.append((date.fromisoformat(k), k))
        except (TypeError, ValueError):
            continue
    out.sort(reverse=True)
    return [k for _, k in out]


def _yoy(new, old):
    # % change only where the base is positive -- a <=0 prior makes it meaningless
    if new is None or old is None or old <= 0:
        return None
    return new / old - 1.0


def eps_volatility_from_statements(stmts):
    # sorted(...) on ISO 'YYYY-MM-DD' keys is chronological, oldest-first --
    # eps_volatility's YoY-growth computation needs that order (see its own
    # docstring); dict.values() alone doesn't guarantee it.
    row = _row(stmts, "incomeStmt", "Diluted EPS")
    return eps_volatility([v for _, v in sorted(row.items())])


def eps_revisions_from_statements(stmts):
    et = (stmts or {}).get("epsTrend") or {}
    r0 = eps_revision((et.get("0y") or {}).get("current"), (et.get("0y") or {}).get("30daysAgo"))
    r1 = eps_revision((et.get("+1y") or {}).get("current"), (et.get("+1y") or {}).get("30daysAgo"))
    return r0, r1


# Actually-reported quarters averaged into earningsSurpriseAvg -- enough
# to smooth a single freak quarter without reaching back so far the
# result reflects who the company used to be, same reasoning
# SEC_OUTLIER_REFERENCE_YEARS/ANALYST_GRADE_LOOKBACK_DAYS elsewhere in
# this file trade off recency vs. noise.
EARNINGS_SURPRISE_LOOKBACK_QUARTERS = 8
# Confirmed live (AAPL 2021 Q2: +42.16%, Q3: +28.18%) that a single
# blowout quarter -- often a one-off (post-COVID reopening demand, a
# product supercycle) -- can dominate a plain average; capped the same
# way GROWTH_CAP/MARGIN_CAP bound other percentage-shaped factors here.
EARNINGS_SURPRISE_CAP = 0.20


def earnings_surprise_from_statements(stmts):
    """Average reported-vs-estimate EPS surprise % over the trailing
    EARNINGS_SURPRISE_LOOKBACK_QUARTERS actually-reported quarters (see
    IBApp.get_yf_statements' own earningsDates fetch), each clamped to
    +/-EARNINGS_SURPRISE_CAP before averaging. A DIFFERENT signal from
    epsRevision0y/1y (analyst ESTIMATES moving before the print, not
    company behavior) and from epsVolatility (dispersion of REPORTED EPS
    itself, not how it compares to what was expected each quarter) --
    this is the historical beat/miss TRACK RECORD, the input behind two
    well-documented anomalies (surprise persistence, post-earnings-
    announcement drift) neither of those two already captures.

    get_earnings_dates rows are newest-first and include the next
    (unreported) print with a None/NaN Surprise(%) -- skipped here, same
    as any other quarter missing a value, not specially cased: this
    function only ever sees actually-reported quarters either way. None
    when there are no reported quarters with a surprise value at all
    (thin/no earnings-date coverage) -- earnings_surprise_rank ranks a
    missing value NEUTRAL (0.5), same treatment as earnings_growth_rank/
    eps_volatility_rank (no track record on file isn't itself bearish)."""
    dates = (stmts or {}).get("earningsDates") or {}
    surprises = []
    for row in dates.values():
        pct = row.get("Surprise(%)")
        if pct is None:
            continue
        surprises.append(max(-EARNINGS_SURPRISE_CAP, min(EARNINGS_SURPRISE_CAP, pct / 100.0)))
        if len(surprises) >= EARNINGS_SURPRISE_LOOKBACK_QUARTERS:
            break
    return statistics.fmean(surprises) if surprises else None


# How long a surprise keeps mattering for the PEAD (post-earnings-
# announcement-drift) read below -- calendar days, not trading days (no
# trading calendar handy in this file; ~56 calendar days is roughly 8
# trading weeks, comfortably inside the ~60-trading-day window the PEAD
# literature typically studies). Linear decay to 0 over this window,
# rather than a hard cutoff, so the signal fades smoothly rather than
# vanishing the day after some arbitrary deadline.
PEAD_DECAY_DAYS = 56


def earnings_pead_from_statements(stmts, today=None):
    """Recency-weighted read on the SAME earningsDates data
    earnings_surprise_from_statements averages -- but built for a
    DIFFERENT anomaly. That function answers "has this company been a
    reliable beater" (a slow-moving quality tilt, all 8 quarters weighted
    equally); this one answers "did it just surprise the market, and are
    we still inside the window where the stock's own price tends to keep
    drifting in that direction" (a fast-moving, event-driven read on the
    MOST RECENT print alone, decayed to 0 by PEAD_DECAY_DAYS after it).

    Finds the most recent REPORTED quarter (skips the next unreported
    print the same way earnings_surprise_from_statements does), clamps
    its surprise % to +/-EARNINGS_SURPRISE_CAP same as that function, then
    scales it by max(0, 1 - days_since_report / PEAD_DECAY_DAYS) -- full
    weight the day after the print, linearly fading to exactly 0 (not a
    small residual) once PEAD_DECAY_DAYS have passed. `today` defaults to
    date.today() but takes an explicit value for testability, same
    convention modules.eulerpool.get_forward_estimates uses.

    None when there's no reported quarter to read at all (thin coverage)
    OR the most recent one has already fully decayed (> PEAD_DECAY_DAYS
    old) -- both cases mean "no live drift signal right now," not
    "bearish": earnings_pead_rank ranks either NEUTRAL (0.5), same
    convention as earnings_surprise_from_statements above. Deliberately
    NOT the same missing-data case as that function even though they
    share a return value here -- a fully-decayed recent beat is a
    genuinely different state (there WAS a real surprise, it just no
    longer counts) from never having reported at all, but both resolve to
    "nothing to act on" for this factor either way."""
    today = today or date.today()
    dates = (stmts or {}).get("earningsDates") or {}
    for key, row in dates.items():
        pct = row.get("Surprise(%)")
        if pct is None:
            continue
        try:
            reported = date.fromisoformat(key[:10])
        except ValueError:
            continue
        days_since = (today - reported).days
        if days_since < 0:
            continue  # defensive -- shouldn't happen, a "reported" row with a future date
        weight = max(0.0, 1.0 - days_since / PEAD_DECAY_DAYS)
        if weight <= 0:
            return None
        clamped = max(-EARNINGS_SURPRISE_CAP, min(EARNINGS_SURPRISE_CAP, pct / 100.0))
        return clamped * weight
    return None


def statement_metrics(stmts):
    """The cross-check figures the revenueGrowth reconcile and the
    Simulations forward-EPS anchor use, all from yfinance's own statement
    objects rather than the (breakable) info[] ratio fields. Was
    IBApp.get_statement_check."""
    out = {}

    rev = _row(stmts, "incomeStmt", "Total Revenue")
    d = _dates_desc(rev)
    if len(d) >= 2:
        out["annualRevenue"] = rev[d[0]]
        out["annualRevenuePrior"] = rev[d[1]]
        out["annualRevenueGrowth"] = _yoy(rev[d[0]], rev[d[1]])
    eps = _row(stmts, "incomeStmt", "Diluted EPS")
    de = _dates_desc(eps)
    if len(de) >= 2:
        out["dilutedEpsAnnual"] = eps[de[0]]
        out["dilutedEpsPrior"] = eps[de[1]]
        out["dilutedEpsGrowth"] = _yoy(eps[de[0]], eps[de[1]])
    sh = _row(stmts, "incomeStmt", "Diluted Average Shares") or _row(stmts, "incomeStmt", "Basic Average Shares")
    sd = _dates_desc(sh)
    if len(sd) >= 2:
        out["dilutedSharesAnnual"] = sh[sd[0]]
        out["dilutedSharesPrior"] = sh[sd[1]]

    qrev = _row(stmts, "quarterlyIncomeStmt", "Total Revenue")
    qd = [k for k in _dates_desc(qrev) if qrev[k] is not None]
    if qd:
        out["latestQuarterEnd"] = qd[0]
        if len(qd) >= 8:
            ttm = sum(qrev[k] for k in qd[:4])
            prior = sum(qrev[k] for k in qd[4:8])
            out["ttmRevenue"] = ttm
            out["ttmRevenueGrowth"] = _yoy(ttm, prior)

    est = (stmts or {}).get("earningsEstimate") or {}
    for period, key in (("0y", "fwdEps0y"), ("+1y", "fwdEps1y")):
        v = to_float((est.get(period) or {}).get("avg"))
        if v is not None:
            out[key] = v
    if "+1y" in est:
        out["estimateGrowth1y"] = to_float(est["+1y"].get("growth"))
        n = to_float(est["+1y"].get("numberOfAnalysts"))
        out["estimateAnalysts"] = int(n) if n is not None else None
    return out


# --------------------------------------------------------------------------- #
#  build one screen_data row from raw provider data                            #
# --------------------------------------------------------------------------- #
# Which .info fields pass straight through, keyed by the screen_data column.
_INFO_PASSTHROUGH = {
    "name": "shortName",
    "forwardPE": "forwardPE",
    "forwardEps": "forwardEps",
    # Per-share book value (yfinance's own .info field) -- feeds
    # modules.simulations' own fundamental price floor
    # (BOOK_VALUE_FLOOR_MULTIPLE), a pure balance-sheet number independent
    # of that module's own projected epsPath (unlike the earlier, removed
    # bookValue+sum(epsPath) floor -- see that module's own CAVEATS
    # section for why that one was pulled).
    "bookValue": "bookValue",
    "epsCurrentYear": "epsCurrentYear",
    "trailingPS": "priceToSalesTrailing12Months",
    "pegRatio": "pegRatio",
    "enterpriseValue": "enterpriseValue",
    "sharesOutstanding": "sharesOutstanding",
    "impliedSharesOutstanding": "impliedSharesOutstanding",
    "debtToEquity": "debtToEquity",
    "revenuePerShare": "revenuePerShare",
    "quickRatio": "quickRatio",
    "currentRatio": "currentRatio",
    "shortRatio": "shortRatio",
    "shortPercentOfFloat": "shortPercentOfFloat",
    "country": "country",
    "targetMeanPrice": "targetMeanPrice",
    "targetHighPrice": "targetHighPrice",
    "targetLowPrice": "targetLowPrice",
    "numberOfAnalystOpinions": "numberOfAnalystOpinions",
    "revenueGrowth": "revenueGrowth",
    "earningsGrowth": "earningsGrowth",
    "returnOnEquity": "returnOnEquity",
    "profitMargins": "profitMargins",
    "enterpriseToEbitda": "enterpriseToEbitda",
    "beta": "beta",
    "recommendationKey": "recommendationKey",
    "recommendationMean": "recommendationMean",
    "earningsTimestampStart": "earningsTimestampStart",
    "heldPercentInsiders": "heldPercentInsiders",
}


def build_screen_row(info, stmts):
    """The curated screen_data.csv row for one ticker, from its raw
    yfinance .info dict + raw statement dict. Pure computation -- this is
    exactly what IBApp.get_forward_pe used to build inline during the
    fetch. `lastDownload` is copied from info (written there by the fetch).
    """
    info = info or {}
    row = {col: info.get(src) for col, src in _INFO_PASSTHROUGH.items()}

    price = info.get("currentPrice") or info.get("regularMarketPrice")
    row["price"] = price

    market_cap, fcf = info.get("marketCap"), info.get("freeCashflow")
    row["priceToFCF"] = market_cap / fcf if market_cap and fcf else None

    trailing_pe = info.get("trailingPE")
    trailing_eps = info.get("trailingEps")
    if trailing_pe is None and price and trailing_eps:
        # Yahoo suppresses trailingPE when trailing EPS is negative;
        # compute it so negative earnings stay visible.
        trailing_pe = price / trailing_eps
    row["trailingPE"] = trailing_pe

    row["operatingMargins"] = clamp_margin(info.get("operatingMargins"))
    row["grossMargins"] = clamp_margin(info.get("grossMargins"))

    r0, r1 = eps_revisions_from_statements(stmts)
    row["epsRevision0y"], row["epsRevision1y"] = r0, r1
    row["epsVolatility"] = eps_volatility_from_statements(stmts)
    row["earningsSurpriseAvg"] = earnings_surprise_from_statements(stmts)
    row["earningsPead"] = earnings_pead_from_statements(stmts)

    row.update(statement_metrics(stmts))

    # "industry" (e.g. "Semiconductors") not the coarse "sector"; symbols.json
    # overrides are layered on by main.apply_sector_overrides afterwards.
    row["sector"] = info.get("industry")
    row["yearReturn"] = info.get("52WeekChange")
    row["lastDownload"] = info.get("lastDownload")
    return row


# --------------------------------------------------------------------------- #
#  revenueGrowth reconcile (moved from main)                                   #
# --------------------------------------------------------------------------- #
# yfinance info.revenueGrowth is a SINGLE most-recent-quarter YoY figure
# (87% of a 200-name sample match MRQ-YoY within 5pp; 0% match TTM) --
# noisy for cyclicals (one hot quarter vs a soft comp reads +50% while the
# trailing year is flat) and, on merger/spin/reverse-merger names,
# sometimes differenced against a broken base (COF +1,111%). Per ticker:
#
#   Tier A -- corruption override. info implies > +200% AND both filed
#     annual sources (SEC XBRL revenue + yfinance income_stmt) agree with
#     each other within 15pp and are themselves under +60% -> filed mean.
#     Every sector.
#   Blend (non-financials) -- 0.5*y0 + 0.25*y1 + 0.25*y2, y0 =
#     info.revenueGrowth doubled, y1/y2 = the next two quarters' YoY from
#     the SEC quarterly revenue series aligned to yfinance's latest quarter.
#   blend-annual fallback -- 0.5*y0 + 0.5*(yfinance annual YoY).
#   info-mrq -- raw info unchanged (financials, Tier-A misses, no data).
_REVGROWTH_INFO_IMPLAUSIBLE = 2.0
_REVGROWTH_FILED_AGREE_TOL = 0.15
_REVGROWTH_FILED_SANE_MAX = 0.60
_REVGROWTH_BLEND_WEIGHTS = (0.5, 0.25, 0.25)
_REVGROWTH_QUARTER_MATCH_DAYS = 25


def sec_fy_revenue_growth(entry):
    """latest FY revenue / prior FY - 1 from one company_facts.json entry's
    `revenue` array (oldest->newest). None unless both years present and
    the prior year positive."""
    rev = (entry or {}).get("revenue") or []
    if len(rev) < 2:
        return None
    prev, latest = rev[-2].get("val"), rev[-1].get("val")
    if prev is None or latest is None or prev <= 0:
        return None
    return latest / prev - 1.0


def blend_quarterly(y0, sec_quarterly, latest_quarter_end):
    """0.5*y0 + 0.25*y1 + 0.25*y2. y0 = yfinance's fresh MRQ-YoY (passed
    in); y1/y2 = the two prior quarters' YoY from `sec_quarterly`
    (company_facts.json revenueQuarterly, [{end, val}, ...]) aligned by
    date to `latest_quarter_end`. None if the SEC series lacks any of the
    four quarters y1/y2 need."""
    if not sec_quarterly or not latest_quarter_end:
        return None
    try:
        d0 = date.fromisoformat(latest_quarter_end)
    except (TypeError, ValueError):
        return None
    by_end = {}
    for q in sec_quarterly:
        try:
            by_end[date.fromisoformat(q["end"])] = q["val"]
        except (TypeError, ValueError, KeyError):
            continue

    def near(days_back):
        target = d0 - timedelta(days=days_back)
        hits = [(abs((d - target).days), v) for d, v in by_end.items()]
        hits = [(g, v) for g, v in hits if g <= _REVGROWTH_QUARTER_MATCH_DAYS]
        return min(hits)[1] if hits else None

    q1, q1_prior = near(91), near(456)
    q2, q2_prior = near(182), near(547)
    if None in (q1, q1_prior, q2, q2_prior) or q1_prior <= 0 or q2_prior <= 0:
        return None
    y1, y2 = q1 / q1_prior - 1.0, q2 / q2_prior - 1.0
    w0, w1, w2 = _REVGROWTH_BLEND_WEIGHTS
    return w0 * y0 + w1 * y1 + w2 * y2


def _filed_revenue_growth(xe, row):
    """(mean, sec_g, stmt_g) or None -- the mean of the two FILED annual
    revenue-growth figures (SEC XBRL FY-YoY from sec_fy_revenue_growth and
    yfinance income_stmt FY-YoY from row['annualRevenueGrowth']) when they
    agree within _REVGROWTH_FILED_AGREE_TOL and are each under
    _REVGROWTH_FILED_SANE_MAX. The trustworthy fallback for BOTH a corrupt
    info.revenueGrowth (Tier A) and a corrupt quarterly blend (below --
    SEC revenueQuarterly can carry a YTD / stub / segment value for a
    single quarter-end, e.g. PRG's $3.7M and $1.8B "quarters" for a
    ~$600M/qtr company, which blend to +829%)."""
    sec_g = sec_fy_revenue_growth(xe)
    stmt_g = to_float(row.get("annualRevenueGrowth"))
    if (sec_g is not None and stmt_g is not None
            and abs(sec_g - stmt_g) <= _REVGROWTH_FILED_AGREE_TOL
            and abs((sec_g + stmt_g) / 2.0) < _REVGROWTH_FILED_SANE_MAX):
        return round((sec_g + stmt_g) / 2.0, 6), sec_g, stmt_g
    return None


def reconcile_revenue_growth(data, xbrl):
    """Mutates `data` in place: rewrites each row's revenueGrowth and
    stamps revenueGrowthSource. `xbrl` = loaded company_facts.json. The
    yfinance statement figures (annualRevenueGrowth / latestQuarterEnd)
    are read off the row itself -- build_screen_row put them there. No-op
    when `xbrl` is empty AND no row carries annualRevenueGrowth."""
    counts = {"reconciled-filed": 0, "blend-q": 0, "blend-annual": 0}
    for ticker, row in data.items():
        info_g = to_float(row.get("revenueGrowth"))
        if info_g is None:
            continue
        xe = xbrl.get(ticker) or {}

        if abs(info_g) > _REVGROWTH_INFO_IMPLAUSIBLE:
            filed = _filed_revenue_growth(xe, row)
            if filed is not None:
                val, sec_g, stmt_g = filed
                print(f"reconcile revenueGrowth: {ticker} {info_g:+.1%} -> {val:+.1%} "
                      f"(Tier A: SEC {sec_g:+.1%}, yf-stmt {stmt_g:+.1%})")
                row["revenueGrowth"] = val
                row["revenueGrowthSource"] = "reconciled-filed"
                counts["reconciled-filed"] += 1
            continue

        if get_sector_group(row.get("sector")) == "Financial Services":
            continue

        blended = blend_quarterly(info_g, xe.get("revenueQuarterly"), row.get("latestQuarterEnd"))

        if blended is not None and abs(blended) > _REVGROWTH_INFO_IMPLAUSIBLE:
            # The quarterly blend itself is corrupt -- same +200% bar and
            # same filed-annual fallback as a corrupt info figure above.
            filed = _filed_revenue_growth(xe, row)
            if filed is not None:
                val, sec_g, stmt_g = filed
                print(f"reconcile revenueGrowth: {ticker} blend {blended:+.1%} -> {val:+.1%} "
                      f"(blend corrupt; Tier A: SEC {sec_g:+.1%}, yf-stmt {stmt_g:+.1%})")
                row["revenueGrowth"] = val
                row["revenueGrowthSource"] = "reconciled-filed"
                counts["reconciled-filed"] += 1
                continue
            blended = None  # fall through to blend-annual

        if blended is not None:
            row["revenueGrowth"] = round(blended, 6)
            row["revenueGrowthSource"] = "blend-q"
            counts["blend-q"] += 1
            continue

        ann = to_float(row.get("annualRevenueGrowth"))
        if ann is not None:
            row["revenueGrowth"] = round(0.5 * info_g + 0.5 * ann, 6)
            row["revenueGrowthSource"] = "blend-annual"
            counts["blend-annual"] += 1

    if any(counts.values()):
        print("Reconciled revenueGrowth: "
              + ", ".join(f"{v} {k}" for k, v in counts.items() if v))


# --------------------------------------------------------------------------- #
#  earningsGrowth blend                                                        #
# --------------------------------------------------------------------------- #
# yfinance info.earningsGrowth is the SAME kind of figure as
# info.revenueGrowth -- most-recent-quarter YoY EPS growth (confirmed: it
# tracks info.earningsQuarterlyGrowth). vs. filed fiscal-year diluted-EPS
# growth it has a median 41-47pp gap: cyclicals (CVX +322% Q / -32% FY),
# merger/split contamination (COKE +266% Q / -3% FY), near-zero bases (MU
# +1368%). The SCORED value everywhere earningsGrowth is read (growth_rank's
# revenue cap, earnings_growth_rank, simulations' own + peer caps) is
# reconciled:
#
#   eg-turnaround -- prior filed FY diluted EPS <= 0 and the latest > 0.
#     A % off a negative base is meaningless AND is the loss-to-profit case
#     that produces the wildest raw readings (MH +9,860%, PARR +699%).
#     earningsGrowth is left BLANK -- no reconciled rate. Both consumers
#     (growth_rank's revenue-corroboration cap, simulations' own/peer caps)
#     already treat a missing value as "no cap", which is the correct
#     semantics here: "did earnings keep pace with revenue" is undefined
#     when the prior year was a loss. The loss->profit signal itself flows
#     through earningsMarginDelta, which differences two net margins and so
#     handles a negative prior year natively -- no sentinel needed.
#   eg-tier-a -- |Q| > +/-200% AND a sane filed FY (|FY| < +/-100%) -> FY.
#   eg-blend  -- 0.5*Q + 0.5*FY when both present.
#   eg-q / eg-fy -- only one of the two available.
#   eg-est -- analyst +1y estimate growth fallback when neither actual-Q nor
#     filed-FY EPS growth is available.
#
# FY = SEC XBRL dilutedEPS FY-YoY, else yfinance income_stmt "Diluted EPS"
# FY-YoY (row["dilutedEpsGrowth"] / dilutedEpsPrior / dilutedEpsAnnual from
# statement_metrics).
_EARNGROWTH_INFO_IMPLAUSIBLE = 2.0   # Tier A: |Q| must exceed +/-200%
_EARNGROWTH_FILED_SANE_MAX = 1.0     # ...and |FY| must be under +/-100%
_EARNGROWTH_BLEND_WEIGHTS = (0.5, 0.5)  # Q, FY
# Floor on any earnings-growth RATE. A profit -> loss year (prev EPS > 0,
# latest < 0) gives latest/prev - 1 < -100% -- HPE (small profit -> -$0.04)
# read -102%, HP ($3.43 -> -$1.66) read -148%. You can't lose more than all
# of last year's earnings, so the sub -100% tail is meaningless as a rate;
# it also drags the peer-median earningsGrowth. Floored at -1.0 it still
# trips growth_rank's revenue-corroboration cap (max(rate, 0) = 0) exactly
# the same way, just without the nonsense magnitude.
_EARNGROWTH_RATE_FLOOR = -1.0
# Mirror of _EARNGROWTH_RATE_FLOOR on the UP side. A near-zero-but-positive
# prior FY EPS (a one-time-charge year, a cycle trough) makes latest/prev-1
# explode -- INCY ($0.15 charge year -> $6.41) read +4173%, MU ($0.70
# trough -> $7.59) +984%. There's no natural ceiling on real EPS growth, so
# instead of a flat cap the PRIOR-year denominator is floored at this
# fraction of the latest year (same trick earnings_margin_delta uses for
# revps_old): prev can't count as less than a quarter of latest, bounding
# the ratio at 1/FRAC - 1 = +300% when prev is an artifact, and only
# biting once growth exceeds ~4x. Every consumer reads earningsGrowth as
# max(rate, 0) in a one-way cap, so +300% vs +984% is invisible to scoring
# -- this just stops the nonsense magnitude polluting the peer median / UI.
_EARNGROWTH_PRIOR_FLOOR_FRAC = 0.25

# earningsMarginDelta = avg(net_margin_delta, operating_margin_delta) when
# both are available, else whichever one is (see earnings_margin_delta --
# Option-B blend). Each leg is:
#   net_margin_delta       = clip(EPS_FYn/revps_FYn) - clip(EPS_FYn-1/revps_FYn-1)
#   operating_margin_delta = clip(opInc_FYn/rev_FYn) - clip(opInc_FYn-1/rev_FYn-1)
# both clipped per-year to +/-SANITY and clamped to +/-DELTA_CAP, so they
# average cleanly. The operating leg strips one-time items / tax / non-
# operating marks; the net leg covers the ~20% of names with no clean
# OperatingIncomeLoss tag and ties back to the EPS-based factors. It's what
# earnings_growth_rank scores on, and the per-year overlay simulations.py
# adds to its EPS path.
# Differencing TWO net margins (each with its OWN year's revenue-per-share
# denominator) rather than dividing the EPS change by a single denominator
# means: (a) a loss -> profit turnaround is just (positive) - (negative),
# no sentinel; (b) a fast grower's prior margin is measured against its own
# (smaller) revenue base, not today's, so the swing isn't mechanically
# inflated by revenue growth -- MU $0.70 -> $7.59 reads ~+0.30, not +984%.
# The DENOMINATOR is TOTAL revenue per share: a date-matched SEC XBRL
# revenue/dilutedShares pair first (it carries both years), then yfinance
# (.info revenuePerShare for the new year, income_stmt prior-FY figures for
# the old, else the new-year value for both). Each single-year margin is
# clipped to +/-EARN_MARGIN_SANITY before differencing (a full-year net
# margin outside that band is a one-time item, not operating profit), and
# the difference is clamped to +/-EARN_MARGIN_DELTA_CAP -- which also bounds
# the simulations.py overlay to +/-EARN_MARGIN_DELTA_CAP * anchorEps/year.
EARN_MARGIN_DELTA_CAP = 0.9
# A genuine full-year net margin lives well inside +/-50%; a single year
# reading outside this is almost always a one-time item (asset-sale gain,
# litigation, spinoff remeasurement -- SNDK's post-spinoff $73.76 diluted
# EPS on ~$138 revenue/share = 53% "margin"), not profitability that should
# drive an earnings-trend signal. Each year's margin is clipped here first.
EARN_MARGIN_SANITY = 0.5
# The prior-year revenue-per-share denominator is floored at this fraction
# of the current year's -- a real many-fold commercial ramp would otherwise
# have its prior margin measured against a near-zero base.
EARN_MARGIN_REVPS_FLOOR_FRAC = 0.25
# If revenue per share (or, for the SEC pair, the diluted-share count)
# moves more than this factor YoY, the two fiscal years are not the same
# company -- a spinoff, a transformational acquisition, or a filing
# unit-of-measure error. earningsMarginDelta is then left BLANK (neutral
# rank) rather than fabricated. SNDK (SanDisk / WDC spinoff: SEC revenue
# $7.4B -> $20.2B) and CRMD (SEC diluted shares off ~1000x in FY24-25)
# both land here.
EARN_MARGIN_DISCONTINUITY = 2.5


def _sec_facts_by_end(entry, key):
    """{end_date: float value} for one company_facts.json fact list --
    lets revenue / dilutedShares / dilutedEPS be aligned by fiscal-period
    end date rather than by list position (the lists don't always cover
    the same years -- e.g. CRMD's revenue skips FY2023)."""
    out = {}
    for r in (entry or {}).get(key) or []:
        v = to_float(r.get("val"))
        end = r.get("end")
        if v is not None and end:
            out[end] = v
    return out


def _quarter_key(date_str):
    """ISO 'YYYY-MM-DD' -> (year, quarter) for grouping fiscal-period-end
    dates that land within a few days of each other (different sources'
    own idea of the exact end date for the SAME fiscal period) into one
    bucket. None for an unparseable string."""
    try:
        d = date.fromisoformat(date_str)
    except (TypeError, ValueError):
        return None
    return (d.year, (d.month - 1) // 3)


def eps_volatility_merged(entry, stmts, euler_eps=None):
    """eps_volatility (see that function) computed on the UNION of SEC
    company_facts' dilutedEPS (usually a much longer back history, but
    sometimes has a multi-year filing gap), yfinance's own income_stmt row
    (usually only ~4-5 trailing annual columns, but reliably contiguous),
    and Eulerpool's own historical epsEstimate series (modules.eulerpool.
    fetch_eps_estimates, cached to EPS_ESTIMATES_FILE) -- all three keyed
    by fiscal-period end date so they line up naturally (ISO 'YYYY-MM-DD',
    see _sec_facts_by_end / df_to_dict's own col_key / fetch_eps_
    estimates' own docstring).

    Precedence: Eulerpool wins whenever it covers a date (see below for
    why), else SEC's own filed figure (the same preference earnings_
    margin_delta/fy_diluted_eps_growth already give it over yfinance),
    else yfinance fills in anything neither of the other two has. euler_eps
    defaults to {} (a ticker with no Eulerpool coverage falls back to the
    old SEC/yfinance-only merge exactly as before).

    Why Eulerpool wins: it reads closer to a "Street"/adjusted EPS than
    raw GAAP -- confirmed live, spot-checked against SEC's own dilutedEPS
    for MSFT (FY2023: $8.70 Eulerpool vs. $9.68 GAAP) and AMZN (FY2022:
    -$0.12 Eulerpool vs. -$0.27 GAAP, the year of AMZN's huge Rivian
    stock mark-to-market loss) -- i.e. it excludes at least some one-off
    items GAAP includes, which is exactly what a genuine-volatility
    measure wants to exclude. This is ADDITIONAL to (not a replacement
    for) the eps_volatility fix below that computes YoY growth-rate
    dispersion instead of stdev(levels)/mean(|levels|) -- that fix
    detrends a smooth compounding trend, this one removes one-off noise
    at the source; MSFT's own case benefits from both (see eps_volatility's
    own docstring for the trend-conflation half of that story).

    Why the SEC/yfinance merge still matters underneath: yfinance's
    income_stmt caps out around 4-5 annual columns, so a single unusual
    year (a one-time gain/charge) can dominate a stdev computed on that
    alone -- confirmed live on FEIM, whose yfinance Diluted EPS series is
    just [-0.59, 0.59, 2.48, -0.09]: that lone +2.48 (likely a one-time
    item, not repeatable earnings power) alone pushed epsVolatility to
    1.44 (under the OLD levels-based formula), the most extreme tier in
    the universe, which in turn pinned simulations.py's risk-premium
    haircut at its own floor -- the largest discount the model can apply
    to any stock (see RISK_PREMIUM_K there). FEIM's SEC record goes back
    to 2011; merged, that's 10 fiscal years instead of 4, sharply diluting
    that one year's leverage over the ratio. None when the merged series
    still has fewer than 4 points (same floor eps_volatility itself
    enforces)."""
    sec_eps = _sec_facts_by_end(entry, "dilutedEPS")
    yf_eps = _row(stmts, "incomeStmt", "Diluted EPS")
    euler_eps = euler_eps or {}
    # Grouped by (year, quarter) rather than exact date -- explicit
    # instruction: the same fiscal year end gets reported a day or two
    # apart by different sources (confirmed live, MU: SEC's 2018-08-30 vs.
    # Eulerpool's 2018-08-31, both the SAME FY2018), so merging by exact
    # date string treated them as two separate observations instead of one
    # -- diluting genuine volatility with near-zero "phantom" YoY
    # transitions between two readings of the same year. One observation
    # per quarter, same source precedence as before (Eulerpool wins, then
    # SEC, then yfinance) via dict overwrite order.
    merged_by_quarter = {}
    for source in (yf_eps, sec_eps, euler_eps):
        for date_str, v in source.items():
            key = _quarter_key(date_str)
            if key is not None and v is not None:
                merged_by_quarter[key] = v
    # sorted(...) on (year, quarter) tuples is chronological, oldest-first
    # -- required by eps_volatility's YoY-growth computation (see that
    # function's own docstring).
    return eps_volatility([v for _, v in sorted(merged_by_quarter.items())])


def reconcile_forward_eps(data, eulerpool_forward_eps):
    """Mutates `data` in place: blends this project's yfinance-sourced
    fwdEps0y/fwdEps1y (set earlier at statement_metrics time, from
    yfinance's own earningsEstimate statement) 50/50 with Eulerpool's own
    consensus estimates for the same two fiscal-year slots (modules.
    eulerpool.get_forward_eps, cached to FORWARD_EPS_FILE by
    fetch_forward_eps) into "our own" forward EPS -- one number per slot,
    used everywhere from here on, not two competing ones.

    `eulerpool_forward_eps` = loaded FORWARD_EPS_FILE, i.e.
    {ticker: {"fwdEps0y": ..., "fwdEps1y": ..., "fwdRevenue0y": ...,
    "fwdRevenue1y": ...}}.

    Per EPS slot: average of the two sources when both are present;
    whichever one is present when only one is (graceful degrade, same as
    every other reconcile_* in this file) rather than leaving the field
    untouched -- a ticker Eulerpool doesn't cover keeps its yfinance-only
    number, a ticker with only an Eulerpool figure (e.g. yfinance's
    estimate statement was empty) still gets one. Nothing changes for a
    ticker with neither.

    Also overwrites forwardEps with the blended fwdEps1y: forwardEps is
    the field modules.simulations actually reads (as fwd_eps, the anchor
    for its EPS-path g_fwd), and it was confirmed empirically
    (AAPL/TSLA/MSFT) to already equal fwdEps1y from yfinance alone -- so
    this is the one line that makes simulations.py inherit the blend
    without any changes of its own.

    Additionally stamps eulerRevGrowth1y = fwdRevenue1y/fwdRevenue0y - 1
    when both Eulerpool revenue figures are present (None otherwise) --
    NOT a blend, since yfinance never gave this project a forward revenue
    estimate to blend against (only revenueGrowth, a TRAILING figure --
    see reconcile_revenue_growth). This is a genuinely new forward-looking
    growth signal, consumed by modules.simulations' own ownGrowthRate/
    industryGrowthRate as a third, equally-weighted leg alongside
    epsTrend and marginAdjustedRevenueGrowth (explicit instruction: 1/3
    each).

    Also passes eulerFwdEps2y straight through from Eulerpool's own
    fwdEps2y (no blend -- yfinance's own earningsEstimate statement only
    ever carries '0y'/'+1y', nothing two years out, so there's no second
    source to average against), and stamps eulerRevGrowth2y =
    fwdRevenue2y/fwdRevenue1y - 1 the same way eulerRevGrowth1y comes from
    the 0y/1y pair. modules.simulations averages an EPS-implied
    (eulerFwdEps2y) and a revenue-implied (eulerRevGrowth2y) reading into
    its year-2 consensus drift nudge -- the same role forwardEps/
    anchorEps - 1 plays for year 1, one year further out and from two
    Eulerpool-only sources instead of one blended one. Years 3+ get no
    equivalent drift at all -- see that module's own comment on
    Y2_SCHEDULE_WEIGHT for why the drift stops at year 2 and years 3+ are
    left on the plain concave reversion-to-peer-median schedule."""
    for ticker, row in data.items():
        eu = eulerpool_forward_eps.get(ticker) or {}
        for slot in ("fwdEps0y", "fwdEps1y"):
            yf_val = to_float(row.get(slot))
            eu_val = to_float(eu.get(slot))
            if yf_val is not None and eu_val is not None:
                row[slot] = round((yf_val + eu_val) / 2, 6)
            elif eu_val is not None:
                row[slot] = eu_val
            # else: yf_val only, or neither -- leave row[slot] as is

        if row.get("fwdEps1y") is not None:
            row["forwardEps"] = row["fwdEps1y"]
        # epsCurrentYear gets the SAME treatment as forwardEps above, via
        # the blended fwdEps0y instead of fwdEps1y -- explicit bug fix:
        # epsCurrentYear used to stay on yfinance's raw, unreconciled value
        # forever (fwdEps0y was computed and stored but never copied back
        # to it), so a ticker whose raw yfinance epsCurrentYear happened to
        # be corrupted (same class of data-basis issue as the ADR
        # forwardEps mismatches -- confirmed live, JD: raw epsCurrentYear
        # =$22.76 vs. a properly-blended fwdEps0y of $3.31, a 6.9x gap for
        # the SAME company's current fiscal year) fed that bad number
        # straight into modules.simulations' real_base (the years-1-2
        # level-blend's own EPS anchor), inflating eps_1/eps_2 the same way
        # a bad forwardEps used to before THAT field got this exact fix.
        if row.get("fwdEps0y") is not None:
            row["epsCurrentYear"] = row["fwdEps0y"]

        eps2y = to_float(eu.get("fwdEps2y"))
        row["eulerFwdEps2y"] = round(eps2y, 6) if eps2y is not None else None

        fwd_rev0y = to_float(eu.get("fwdRevenue0y"))
        fwd_rev1y = to_float(eu.get("fwdRevenue1y"))
        fwd_rev2y = to_float(eu.get("fwdRevenue2y"))
        if fwd_rev0y is not None and fwd_rev1y is not None and fwd_rev0y != 0:
            row["eulerRevGrowth1y"] = round(fwd_rev1y / fwd_rev0y - 1, 6)
        else:
            row["eulerRevGrowth1y"] = None
        if fwd_rev1y is not None and fwd_rev2y is not None and fwd_rev1y != 0:
            row["eulerRevGrowth2y"] = round(fwd_rev2y / fwd_rev1y - 1, 6)
        else:
            row["eulerRevGrowth2y"] = None


def reconcile_peg_ratio(data):
    """Mutates `data` in place: overwrites pegRatio with a self-computed
    (price/forwardEps) / (earningsGrowth*100), using this project's OWN
    reconciled forwardEps (reconcile_forward_eps' Eulerpool/yfinance
    blend, already overwriting epsCurrentYear/forwardEps by the time this
    runs) and earningsGrowth (reconcile_earnings_growth's SEC-XBRL-aware
    figure) -- rather than trusting yfinance's raw, un-reconciled pegRatio
    field outright.

    Why: every other growth/EPS input this project scores on has been
    hardened with some kind of cross-check or reconciliation this session
    (forwardEps/trailingEps outlier guards and consistency gates,
    earningsGrowth's SEC blend, revenueGrowth's own reconciliation) --
    pegRatio was the one major valuation-growth factor still a straight,
    unvalidated pass-through of Yahoo's own (not always transparent --
    sometimes trailing PE, sometimes forward, various growth-estimate
    vintages depending on the ticker) methodology. Recomputing it from
    inputs this project has already vetted keeps it internally consistent
    with what scoring.peg_rank/simulations.py trust elsewhere, instead of
    silently mixing a third, independently-sourced growth/multiple basis
    into the composite score.

    Also uses price/forwardEps rather than the raw forwardPE field for
    the multiple half -- forwardPE is ITSELF a yfinance pass-through (see
    the field-mapping table above) that was never guaranteed to reflect
    the post-reconciliation forwardEps value, the same "two raw fields
    that can drift apart" issue modules.simulations' own forwardPE/
    trailingPE consistency swap exists to catch.

    Non-positive earningsGrowth produces a negative pegRatio (division by
    a negative number) rather than a null one -- explicit continuity with
    scoring.peg_rank's own PEG_NONPOSITIVE_SENTINEL handling, which
    already treats a non-positive PEG as a real "not actually cheap"
    signal ranked worst, not missing data. Only the earningsGrowth==0
    edge case (genuine division-by-zero, rather than a real negative
    figure) is special-cased to an arbitrary but reliably-negative -1.0.

    Graceful degrade, same as every other reconcile_* here: a ticker
    missing price, forwardEps (non-positive included), or earningsGrowth
    keeps whatever pegRatio build_screen_row already set from yfinance's
    raw field, unchanged -- this only overrides pegRatio when this
    project's own inputs are actually available to compute a better one.

    Final pass clamps EVERY negative pegRatio (self-computed above, or
    yfinance's raw fallback for a ticker this function couldn't
    reconcile) to -0.0001 -- explicit instruction. peg_rank's own ranking
    already treats any peg<=0 identically regardless of magnitude (see
    PEG_NONPOSITIVE_SENTINEL), so this changes nothing there -- it's for
    every OTHER place pegRatio gets summed/averaged directly across many
    positions (Positions' value-weighted Portfolio Factors table,
    Sectors' factor tree): a single large-magnitude outlier like ST's
    -4.64 (confirmed live) would otherwise drag a mostly-positive-PEG
    average down far more than one "not attractive" data point should.
    -0.0001 keeps the sign (still recognizably non-positive) while being
    negligible in any sum/average it's folded into."""
    for row in data.values():
        price = to_float(row.get("price"))
        forward_eps = to_float(row.get("forwardEps"))
        earnings_growth = to_float(row.get("earningsGrowth"))
        if (
            price is None or forward_eps is None or forward_eps <= 0
            or earnings_growth is None
        ):
            continue
        own_forward_pe = price / forward_eps
        if earnings_growth == 0:
            row["pegRatio"] = -1.0
        else:
            row["pegRatio"] = round(own_forward_pe / (earnings_growth * 100.0), 6)

    for row in data.values():
        peg = to_float(row.get("pegRatio"))
        if peg is not None and peg < 0:
            row["pegRatio"] = -0.0001


EPS_OUTLIER_SIGMA = 6.0
EPS_OUTLIER_MAX_PASSES = 3
SEC_OUTLIER_REFERENCE_YEARS = 5


def reconcile_trailing_eps(data, raw_stmts, xbrl=None):
    """Mutates `data` in place: recomputes trailingEps (and trailingPE,
    price/trailingEps) as the sum of the 4 MOST RECENT quarterly Diluted
    EPS values from yfinance's own quarterlyIncomeStmt, when at least 4
    quarters are available -- explicit instruction.

    Why: yfinance's own `.info` trailingEps/trailingPE fields can lag
    their OWN quarterlyIncomeStmt endpoint by a full quarter -- confirmed
    live, FEIM's trailingEps stayed at exactly $0.25 (byte-for-byte
    identical) even immediately after a fresh download picked up its new
    2026-07-31 quarter in quarterlyIncomeStmt, because `.info` is a
    separate, independently-cached summary payload that doesn't always
    recompute the instant new quarterly data lands. Recomputing directly
    from the raw quarters here means REPORTED (already-happened) earnings
    are reflected as soon as the underlying quarterly statement data is
    refreshed (a `download()` call) and this function next runs (zero
    network cost, so every `recalc()` -- not just an explicit refetch --
    picks up whatever's freshest), rather than waiting on yfinance's own
    summary cache to catch up on its own schedule.

    This is about REPORTED earnings specifically, not forward estimates --
    forwardEps/eulerFwdEps2y etc. are untouched; this only overrides
    trailingEps/trailingPE, and only when 4 real quarters are present (a
    ticker with sparser quarterly coverage keeps whatever build_screen_row
    already set from yfinance's own trailingEps field, unchanged).

    `xbrl` (SEC XBRL_FACTS_FILE, optional) extends the outlier guard's own
    reference pool below with up to SEC_OUTLIER_REFERENCE_YEARS of SEC's
    ANNUAL dilutedEPS history (/4, to approximate a quarterly-equivalent
    scale) -- explicit instruction, "use at least 5 years to calculate
    the volatility of earnings". yfinance's own quarterlyIncomeStmt caps
    out at 5 quarters for ~99% of the universe (confirmed live), leaving
    only ~1 extra reference point beyond the 4-quarter TTM window itself
    -- nowhere near enough to reliably catch a second, smaller outlier
    once a first, larger one has already been found (see the outlier
    guard's own comment on VISN's masking failure mode). SEC's
    company_facts.json, already ingested for eps_volatility_merged/
    reconcile_revenue_growth, routinely goes back a decade or more
    (confirmed live, AAPL: FY2007 onward) -- annual, not quarterly, hence
    the /4 approximation, imprecise but adequate for a coarse sigma test,
    not a replacement for eps_volatility's own more careful per-fact
    handling."""
    xbrl = xbrl or {}
    for ticker, row in data.items():
        stmts = raw_stmts.get(ticker) or {}
        q_eps = (stmts.get("quarterlyIncomeStmt") or {}).get("Diluted EPS") or {}
        all_quarters = sorted(
            ((d, v) for d, v in q_eps.items() if to_float(v) is not None),
            key=lambda dv: dv[0], reverse=True,
        )
        if len(all_quarters) < 4:
            continue
        # TTM is still, and only ever, the 4 MOST RECENT quarters -- older
        # history (yfinance's own leftover quarters, plus SEC's annual
        # history below) is pulled in ONLY as extra reference data for the
        # outlier guard, never summed into trailingEps itself.
        recent_values = [to_float(v) for _, v in all_quarters[:4]]
        older_values = [to_float(v) for _, v in all_quarters[4:]]
        sec_annual = sorted(
            _sec_facts_by_end(xbrl.get(ticker), "dilutedEPS").items(),
            key=lambda dv: dv[0], reverse=True,
        )[:SEC_OUTLIER_REFERENCE_YEARS]
        older_values.extend(v / 4.0 for _, v in sec_annual)

        # 6-sigma outlier guard: a single freak quarter (a one-off
        # gain/impairment) can dominate the TTM sum even though it's
        # genuinely reported -- confirmed live, TRS's 2026-03-31 quarter
        # (+$21.40) vs. its other three quarters (+0.41/+0.23/+0.37,
        # mean~$0.34, stdev~$0.08) inflated trailingEps to $24.03 (implied
        # P/E 1.6x) against a forwardEps/epsCurrentYear/revenueBasedEps
        # all clustered around $1.3-2.1 -- clearly not the ongoing
        # earnings level. EPS_OUTLIER_SIGMA=6.0 is a strict enough bar
        # that ordinary earnings volatility (even a volatile name) won't
        # trip it -- only a genuine order-of-magnitude freak value will.
        #
        # Tested against ALL available quarters (the other 3 in the
        # summed window PLUS any older history beyond it), not just
        # leave-one-out within the 4-quarter window alone, and iterated
        # up to EPS_OUTLIER_MAX_PASSES times -- explicit fix for a
        # MASKING failure mode confirmed live on VISN: quarters were
        # $1.21/$23.15/$6.05/$0.39, and a single leave-one-out pass over
        # just those 4 correctly replaced the freak $23.15 quarter but
        # left $6.05 untouched, because $6.05's own reference set
        # (`{1.21, 23.15, 0.39}`) still contained the unreplaced $23.15,
        # inflating that reference's stdev enough to hide $6.05 behind
        # it -- one extreme outlier masking a second, smaller one. Most
        # tickers only have ~5-8 quarters of yfinance history total, so
        # `older_values` is often thin (sometimes just 1-2 points) rather
        # than a large independent sample; re-testing against the
        # PARTIALLY CLEANED set on each pass (not a fixed original
        # snapshot) is what actually resolves the masking, not the extra
        # history alone -- confirmed by hand for VISN: pass 1 replaces
        # $23.15 with $1.93 (mean of {1.21, 6.05, 0.39, 0.06}), and pass 2
        # then correctly catches $6.05 against the now-cleaned
        # {1.21, 1.93, 0.39, 0.06}.
        cleaned = list(recent_values)
        for _ in range(EPS_OUTLIER_MAX_PASSES):
            changed = False
            for i, v in enumerate(cleaned):
                others = cleaned[:i] + cleaned[i + 1:] + older_values
                if len(others) < 3:
                    continue
                mean_others = statistics.mean(others)
                stdev_others = statistics.pstdev(others)
                if stdev_others > 0 and abs(v - mean_others) > EPS_OUTLIER_SIGMA * stdev_others:
                    cleaned[i] = mean_others
                    changed = True
            if not changed:
                break
        ttm_eps = sum(cleaned)
        row["trailingEps"] = round(ttm_eps, 6)
        price = to_float(row.get("price"))
        if price is not None and ttm_eps > 0:
            row["trailingPE"] = round(price / ttm_eps, 6)
        elif ttm_eps <= 0:
            # A <= 0 TTM EPS makes trailingPE meaningless (same convention
            # as every other "prev <= 0" guard in this file) -- leave
            # whatever trailingPE was already set to rather than compute a
            # nonsensical negative or infinite multiple.
            pass


def reconcile_eps_volatility(data, xbrl, raw_stmts, euler_eps_estimates=None):
    """Mutates `data` in place: recomputes each row's epsVolatility (set
    at build_screen_row time from yfinance's own income_stmt alone, before
    xbrl is even loaded -- see build_screen_row) using eps_volatility_
    merged's longer, three-source (Eulerpool + SEC + yfinance) series, for
    any ticker at least ONE of SEC or Eulerpool has data for. Left
    untouched (whatever build_screen_row already computed from yfinance
    alone) for a ticker neither SEC nor Eulerpool covers at all, or where
    the merge still doesn't reach eps_volatility's own 4-point floor.

    `euler_eps_estimates` = loaded EPS_ESTIMATES_FILE (modules.eulerpool.
    fetch_eps_estimates), i.e. {ticker: {period: epsEstimate, ...}};
    defaults to {} (an ticker Eulerpool doesn't cover, or when the file
    hasn't been fetched at all yet, falls back to the old SEC/yfinance-only
    merge exactly as before -- Eulerpool coverage widens which tickers get
    the merge treatment at all, e.g. a Form 20-F foreign issuer like TSM
    with no SEC XBRL dilutedEPS facts can still benefit if Eulerpool
    covers it, which SEC-only gating never allowed)."""
    euler_eps_estimates = euler_eps_estimates or {}
    for ticker, row in data.items():
        entry = xbrl.get(ticker)
        euler_eps = euler_eps_estimates.get(ticker)
        if (not entry or not entry.get("dilutedEPS")) and not euler_eps:
            continue
        merged = eps_volatility_merged(entry, raw_stmts.get(ticker), euler_eps)
        if merged is not None:
            row["epsVolatility"] = round(merged, 6)


def _clamp_margin_delta(md):
    return max(-EARN_MARGIN_DELTA_CAP, min(EARN_MARGIN_DELTA_CAP, md))


def _operating_margin_delta(entry):
    """YoY change in OPERATING margin (SEC OperatingIncomeLoss / revenue,
    date-matched) or None. Same SANITY per-year clip / DELTA_CAP clamp /
    DISCONTINUITY continuity guard as _net_margin_delta, so the two figures
    are on one scale and can be averaged. Operating income is a total-
    company dollar figure, so this is a plain ratio -- no per-share step,
    no share-count continuity check (the revenue move covers it)."""
    oi = _sec_facts_by_end(entry, "operatingIncome")
    rev = _sec_facts_by_end(entry, "revenue")
    common = sorted(set(oi) & set(rev))
    if len(common) < 2:
        return None
    d_new, d_old = common[-1], common[-2]
    rev_new, rev_old = rev[d_new], rev[d_old]
    if rev_new <= 0 or rev_old <= 0:
        return None
    if max(rev_new, rev_old) / min(rev_new, rev_old) > EARN_MARGIN_DISCONTINUITY:
        return None
    clip = lambda m: max(-EARN_MARGIN_SANITY, min(EARN_MARGIN_SANITY, m))
    return _clamp_margin_delta(clip(oi[d_new] / rev_new) - clip(oi[d_old] / rev_old))


def earnings_margin_delta(entry, row):
    """(value, source). Option-B blend: the average of the net-margin delta
    (_net_margin_delta -- dilutedEPS / revenue-per-share YoY) and the
    operating-margin delta (_operating_margin_delta -- OperatingIncomeLoss
    / revenue YoY) when BOTH are available, else whichever one is. Operating
    margin strips one-time items / tax / non-operating marks -- a cleaner
    read on core-business profitability trend -- but ~20% of names have no
    clean OperatingIncomeLoss tag (and banks/insurers/REITs structurally
    don't), so net margin carries those and keeps consistency with the
    EPS-based factors. Source: "<net_src>+op" when blended, else the net
    source ("sec"/"yf") alone, or "op" when only operating is available.
    (None, None) when neither is."""
    net_md, net_src = _net_margin_delta(entry, row)
    op_md = _operating_margin_delta(entry)
    if net_md is not None and op_md is not None:
        return _clamp_margin_delta((net_md + op_md) / 2), f"{net_src}+op"
    if net_md is not None:
        return net_md, net_src
    if op_md is not None:
        return op_md, "op"
    return None, None


def _net_margin_delta(entry, row):
    """(value, source) -- see EARN_MARGIN_DELTA_CAP / EARN_MARGIN_SANITY /
    EARN_MARGIN_DISCONTINUITY above. `entry` = one company_facts.json ticker
    entry, `row` = the screen row. Returns (None, None) when a clean
    two-year (EPS, revenue-per-share) pair isn't available, or when the two
    years fail the continuity check (spinoff / transformational M&A / filing
    unit change -- the margin trend is undefined, not zero)."""
    row_eps = (to_float(row.get("dilutedEpsAnnual")), to_float(row.get("dilutedEpsPrior")))

    # Preferred: a date-matched SEC pair -- carries BOTH years' revenue and
    # share count, so each year's margin gets its own denominator.
    rev = _sec_facts_by_end(entry, "revenue")
    shs = _sec_facts_by_end(entry, "dilutedShares")
    eps = _sec_facts_by_end(entry, "dilutedEPS")
    common = sorted(set(rev) & set(shs) & set(eps))
    src = None
    revps_new = revps_old = eps_new = eps_old = None
    sh_new = sh_old = None
    if len(common) >= 2:
        d_new, d_old = common[-1], common[-2]
        sh_new, sh_old = shs[d_new], shs[d_old]
        if sh_new > 0 and sh_old > 0:
            eps_new, eps_old = eps[d_new], eps[d_old]
            revps_new, revps_old = rev[d_new] / sh_new, rev[d_old] / sh_old
            src = "sec"

    # Fallback: yfinance. Prior-year revenue-per-share is rarely populated,
    # so the current-year (TTM) figure stands in for both -- this measures
    # the per-share earnings swing against today's revenue base
    # (understates a fast grower, but never explodes).
    if revps_new is None or revps_new <= 0:
        eps_new, eps_old = row_eps
        if eps_new is None or eps_old is None:
            e = (entry or {}).get("dilutedEPS") or []
            if len(e) >= 2:
                eps_new, eps_old = to_float(e[-1].get("val")), to_float(e[-2].get("val"))
        rps_ttm = to_float(row.get("revenuePerShare"))
        if rps_ttm is None or rps_ttm <= 0:
            return None, None
        rev_prior = to_float(row.get("annualRevenuePrior"))
        sh_prior = to_float(row.get("dilutedSharesPrior"))
        revps_new = rps_ttm
        revps_old = (rev_prior / sh_prior) if (rev_prior and sh_prior and sh_prior > 0) else rps_ttm
        src = "yf"

    if eps_new is None or eps_old is None or revps_new <= 0 or revps_old <= 0:
        return None, None

    # Continuity: a > EARN_MARGIN_DISCONTINUITY-fold YoY move in revenue per
    # share (or, for the SEC pair, in the raw share count) means the two
    # years aren't the same company.
    if max(revps_new, revps_old) / min(revps_new, revps_old) > EARN_MARGIN_DISCONTINUITY:
        return None, None
    if src == "sec" and max(sh_new, sh_old) / min(sh_new, sh_old) > EARN_MARGIN_DISCONTINUITY:
        return None, None

    revps_old = max(revps_old, EARN_MARGIN_REVPS_FLOOR_FRAC * revps_new)
    clip = lambda m: max(-EARN_MARGIN_SANITY, min(EARN_MARGIN_SANITY, m))
    md = clip(eps_new / revps_new) - clip(eps_old / revps_old)
    return max(-EARN_MARGIN_DELTA_CAP, min(EARN_MARGIN_DELTA_CAP, md)), src


def fy_diluted_eps_growth(entry, row):
    """(fy_growth or None, is_turnaround, source). fy_growth = latest FY
    diluted EPS / prior FY - 1, from SEC company_facts.json (`entry`) first,
    then yfinance income_stmt (fields on `row`). For the SEC path the
    PRIOR-year denominator is floored at _EARNGROWTH_PRIOR_FLOOR_FRAC *
    latest so a near-zero artifact prior year can't blow the ratio up.

    A trailing rate that would hit _EARNGROWTH_RATE_FLOOR (earnings
    collapsed to a loss -- a > 100% drop, uninformative as a rate) is
    REPLACED by the forward analyst estimate estimateGrowth1y when
    available, clamped to +/-1.0 (explicit instruction: a forward number
    beats reporting -100%; the estimate itself is often off a small or
    negative base, hence the clamp). `source` is "fy" for a real trailing
    rate, "est" when the forward estimate stood in, None when nothing was
    available. is_turnaround is True when the prior FY was a loss (<= 0)
    and the latest FY a profit (> 0) -- no meaningful %, but a real
    signal.

    estimateGrowth1y itself is a THIRD-PARTY ratio (Yahoo's own +1y
    consensus EPS / current-year consensus EPS - 1) with no cross-zero
    protection of its own -- confirmed live, WYFI: epsCurrentYear=-$0.80
    (a loss), forwardEps=+$0.23 (expected profit), the exact same
    loss-to-profit pathology eg-turnaround exists to catch, just shifted
    one year forward. estimateGrowth1y=1.2391 (clamped to the +1.0
    ceiling) fed straight through as "eg-est", then simulations.py's
    convergenceGrowthRate (0.9*industryGrowthRate + 0.1*earningsGrowth)
    treated that meaningless clamped ratio as a real long-run growth
    signal. Guarded the same way as the trailing case: when
    epsCurrentYear and forwardEps sit on OPPOSITE sides of zero, treat
    ANY est_clamped use below as a turnaround (blank rate, real signal
    left to earningsMarginDelta) instead of trusting the ratio."""
    est_g = to_float(row.get("estimateGrowth1y"))
    est_clamped = max(-1.0, min(1.0, est_g)) if est_g is not None else None
    cur_year_eps = to_float(row.get("epsCurrentYear"))
    fwd_eps = to_float(row.get("forwardEps"))
    est_crosses_zero = (
        cur_year_eps is not None and fwd_eps is not None
        and (cur_year_eps <= 0) != (fwd_eps <= 0)
    )

    def _floor(raw):
        # raw > -1.0: a real decline, keep it. raw <= -1.0 (profit -> loss):
        # prefer the clamped forward estimate, else the -1.0 floor.
        if raw > _EARNGROWTH_RATE_FLOOR:
            return raw, False, "fy"
        if est_clamped is not None and not est_crosses_zero:
            return est_clamped, False, "est"
        if est_crosses_zero:
            return None, True, None
        return _EARNGROWTH_RATE_FLOOR, False, "fy"

    e = (entry or {}).get("dilutedEPS") or []
    if len(e) >= 2:
        prev, latest = e[-2].get("val"), e[-1].get("val")
        if prev is not None and latest is not None:
            if prev > 0:
                prev_eff = max(prev, _EARNGROWTH_PRIOR_FLOOR_FRAC * latest) if latest > 0 else prev
                return _floor(latest / prev_eff - 1.0)
            return None, latest > 0, None
    yf_g = to_float(row.get("dilutedEpsGrowth"))
    if yf_g is not None:
        return _floor(yf_g)
    prior, latest = to_float(row.get("dilutedEpsPrior")), to_float(row.get("dilutedEpsAnnual"))
    if prior is not None and latest is not None:
        return None, prior <= 0 < latest, None
    if est_clamped is not None:
        if est_crosses_zero:
            return None, True, None
        return est_clamped, False, "est"
    return None, False, None


def reconcile_earnings_growth(data, xbrl):
    """Mutates `data` in place: rewrites each row's earningsGrowth to the
    reconciled figure (the value every scoring site reads), preserving the
    raw quarterly figure as earningsGrowthQ and stamping
    earningsGrowthSource. `xbrl` = loaded company_facts.json."""
    counts = {"eg-turnaround": 0, "eg-tier-a": 0, "eg-blend": 0, "eg-q": 0, "eg-fy": 0, "eg-est": 0}
    md_n = 0
    md_op_n = 0  # earningsMarginDelta rows where the operating leg contributed
    for ticker, row in data.items():
        # earningsMarginDelta -- the value earnings_growth_rank scores on.
        md, md_src = earnings_margin_delta(xbrl.get(ticker), row)
        if md is not None:
            row["earningsMarginDelta"] = round(md, 6)
            row["earningsMarginDeltaSource"] = md_src
            md_n += 1
            if md_src and md_src.endswith("op"):
                md_op_n += 1
        else:
            # Abstain -- clear any stale value so a discontinuity (spinoff /
            # unit error) reads as neutral, not as the previous run's number.
            row["earningsMarginDelta"] = None
            row["earningsMarginDeltaSource"] = None

        q = to_float(row.get("earningsGrowth"))
        if q is not None:
            row["earningsGrowthQ"] = q          # raw MRQ figure, unfloored
            q = max(q, _EARNGROWTH_RATE_FLOOR)  # a profit -> loss quarter can also print < -100%
        fy, turnaround, fy_src = fy_diluted_eps_growth(xbrl.get(ticker), row)

        if turnaround:
            # No reconciled rate -- a % off a <=0 prior year is meaningless.
            # Both consumers treat missing as "no corroboration cap"; the
            # loss->profit signal flows through earningsMarginDelta instead.
            row["earningsGrowth"] = None
            row["earningsGrowthSource"] = "eg-turnaround"
            counts["eg-turnaround"] += 1
            continue
        if fy_src == "est":
            # Trailing FY collapsed to a loss (or no trailing data at all);
            # the clamped forward analyst estimate stands in. It IS the
            # reconciled view for such a name -- don't blend it with the
            # (equally broken) quarterly figure.
            row["earningsGrowth"] = round(fy, 6)
            row["earningsGrowthSource"] = "eg-est"
            counts["eg-est"] += 1
            continue
        if q is None:
            if fy is not None:
                row["earningsGrowth"] = round(fy, 6)
                row["earningsGrowthSource"] = "eg-fy"
                counts["eg-fy"] += 1
            continue
        if fy is None:
            row["earningsGrowthSource"] = "eg-q"
            counts["eg-q"] += 1
            continue

        if abs(q) > _EARNGROWTH_INFO_IMPLAUSIBLE and abs(fy) < _EARNGROWTH_FILED_SANE_MAX:
            row["earningsGrowth"] = round(fy, 6)
            row["earningsGrowthSource"] = "eg-tier-a"
            counts["eg-tier-a"] += 1
            print(f"reconcile earningsGrowth: {ticker} {q:+.1%} -> {fy:+.1%} (Tier A, filed FY)")
            continue

        w_q, w_fy = _EARNGROWTH_BLEND_WEIGHTS
        row["earningsGrowth"] = round(w_q * q + w_fy * fy, 6)
        row["earningsGrowthSource"] = "eg-blend"
        counts["eg-blend"] += 1

    if any(counts.values()):
        print("Reconciled earningsGrowth: "
              + ", ".join(f"{v} {k}" for k, v in counts.items() if v)
              + f"  |  earningsMarginDelta on {md_n} ({md_op_n} incl. operating leg)")


# --------------------------------------------------------------------------- #
#  Momentum -- Trend Score, replacing the old Money Flow Index               #
# --------------------------------------------------------------------------- #
# Explicit instruction, after a formation/holding-period cross-section on
# 3 months of daily IB Gateway bars (1,997 tickers) showed:
#   - 5-15 trading-day lookbacks predict NEGATIVE forward returns (short-
#     term reversal -- a stock up sharply over 1-3 weeks tends to give
#     some back, not keep running).
#   - ~20-30 trading-day lookbacks predict POSITIVE forward returns
#     (genuine continuation) -- best-supported pair at the time: 20-day
#     formation, 20-day holding (top-decile-minus-bottom-decile spread
#     +4.58%, Spearman +0.089, n=3,994 non-overlapping windows).
# MSI (Money Flow Index/RSI, see IBApp.get_momentum) answers a
# DIFFERENT, shorter-horizon question and was never wrong on its own
# terms -- it's built around exactly the 1-3 week window this
# cross-section shows is reversal-prone, which is why a genuine
# multi-week trend (confirmed live: DINO, Hold-rated, +76% over 3
# months) reads as "overbought" under MSI instead of "trending."
#
# UPDATE, re-run months later after DINO itself reversed hard: rescanning
# formation windows 5-35 days on BOTH the (by-then-shifted) 3-month IB
# window and an independent 8-month yfinance sample found 20 days had
# become one of the WORST lookbacks on both (rho -0.079 and -0.138
# respectively), while 10-12 days was consistently the best-supported
# pair on both (rho +0.021/+0.112, +0.108-ish neighbor at 12) -- not a
# marginal re-tune, a genuine regime change (momentum-crash-shaped: this
# stretch had more DINO-style reversals than the period 20d was first
# tuned on). MOMENTUM_FORMATION_DAYS moved to 10 on that basis. r5/
# vol_accel's own weights were NOT re-tuned at the new window -- both
# came back sign-inconsistent across the two datasets when re-checked
# (r5: +0.036 vs -0.010; vol_accel: sign flipped from the original
# finding entirely) -- kept at their legacy weights rather than fit a
# new number to data too noisy to trust one from.
MOMENTUM_FORMATION_DAYS = 10
MOMENTUM_REVERSAL_DAYS = 5
# Weights on the z-scored legs before percentile-ranking to 0-100 -- r10
# at full weight (the primary, best-validated signal), vol_accel/r5 at
# their original partial weights (see UPDATE above -- not re-validated at
# the new window, kept rather than re-fit on noise), trend_health at a
# weight comparable to vol_accel since it's the strongest single
# conditional effect found this round (see its own section below).
MOMENTUM_VOL_WEIGHT = 0.5
MOMENTUM_REVERSAL_WEIGHT = 0.3
MOMENTUM_HEALTH_WEIGHT = 0.5
# Caps the trend_health gate's multiplier (see its own use-site comment,
# below, for the META bug this fixes) -- an above-average 20-day trend
# counts fully toward turning trend_health on; further extremity past
# this doesn't amplify it further.
MOMENTUM_HEALTH_GATE_CAP = 1.0
# Bars needed: MOMENTUM_FORMATION_DAYS (r10) + 1 (the day before it, as
# the r10 base) + a little slack. Separately, TREND_HEALTH_GATE_DAYS (see
# below) still needs a real 20-trading-day daily history regardless of
# the shorter primary window, so this stays generous rather than
# shrinking in step with MOMENTUM_FORMATION_DAYS.
MOMENTUM_MIN_BARS = 25
# The daily lookback trend_health is GATED on -- deliberately kept at the
# OLD 20-day window rather than following MOMENTUM_FORMATION_DAYS down to
# 10. Explicit finding: trend_health's strong conditional effect
# (Spearman +0.268 within the top quintile of a GENUINE 20-day trend, IB
# 3-month data) collapsed to +0.080 when re-conditioned on the new
# 10-day primary window instead -- the "topping" story it detects
# (DINO-shaped: a trend that's been running for WEEKS starting to fade
# on the hourly tape) specifically needs a multi-week trend to define
# "trending" against, not a 10-day one. So r20 stays in the formula
# purely as this gate, decoupled from being the primary continuation
# signal it used to be.
TREND_HEALTH_GATE_DAYS = 20
# The hourly window trend_health itself is measured over -- 14 hours
# recent pace vs. the preceding 21 hours (35h total, ~5 trading days),
# NON-overlapping segments so this is a genuine acceleration/deceleration
# read, not two overlapping windows double-counting the same hours. See
# derive.py session notes: recent_pace - earlier_pace, both hourly rates
# (return / hours), validated via a formation/holding cross-section on 3
# months of IB hourly bars restricted to stocks already in a genuine
# TREND_HEALTH_GATE_DAYS-day uptrend.
TREND_HEALTH_RECENT_HOURS = 14
TREND_HEALTH_EARLIER_HOURS = 21
TREND_HEALTH_MIN_HOURLY_BARS = TREND_HEALTH_RECENT_HOURS + TREND_HEALTH_EARLIER_HOURS


def _trend_health(hourly_series):
    """recent_pace - earlier_pace (both hourly rates: return / hours) from
    a ticker's hourly closes, or None if there aren't
    TREND_HEALTH_MIN_HOURLY_BARS of them. Positive = accelerating (the
    most recent TREND_HEALTH_RECENT_HOURS hours moved faster, per hour,
    than the TREND_HEALTH_EARLIER_HOURS before that) -- a fresh breakout
    within an existing trend. Negative = decelerating -- the "topping"
    shape: strong over the whole window, but visibly losing steam right
    now."""
    if not hourly_series or len(hourly_series) < TREND_HEALTH_MIN_HOURLY_BARS:
        return None
    closes = [to_float(b.get("close")) for b in hourly_series[-TREND_HEALTH_MIN_HOURLY_BARS:]]
    if any(c is None for c in closes):
        return None
    recent_base = closes[-1 - TREND_HEALTH_RECENT_HOURS]
    earlier_base = closes[0]
    if not recent_base or recent_base <= 0 or not earlier_base or earlier_base <= 0 or closes[-1] <= 0:
        return None
    recent_pace = (closes[-1] / recent_base - 1.0) / TREND_HEALTH_RECENT_HOURS
    earlier_pace = (recent_base / earlier_base - 1.0) / TREND_HEALTH_EARLIER_HOURS
    return recent_pace - earlier_pace


def reconcile_momentum(data, daily_history, hourly_history=None):
    """Mutates `data` in place: overwrites each row's momentum with the
    Trend Score, a cross-sectional percentile rank 0-100 (0=most bearish,
    100=most bullish) -- SAME scale and SAME semantics the old MSI-based
    momentum already used, so RecommendationsView.tsx's existing gate
    thresholds (MOMENTUM_NO_BUY=35, NO_SELL=65, OVERSOLD=20,
    OVERBOUGHT=80) still mean exactly what they always meant ("bottom
    35th percentile," etc.) without needing to be recalibrated -- a
    percentile rank carries that meaning regardless of the underlying
    score's own distribution shape, unlike a raw z-score or return
    figure would.

    `daily_history` = loaded DAILY_3MO_HISTORY_FILE. A ticker with fewer
    than MOMENTUM_MIN_BARS daily bars keeps whatever momentum
    build_screen_row/add_momentum already set (graceful degrade, same
    convention every other reconcile_* here uses) -- this only replaces
    momentum where the new computation actually has enough data to
    trust.

    `hourly_history` (optional, defaults to None/skipped) = loaded
    HOURLY_HISTORY_FILE -- feeds the trend_health gate (see
    TREND_HEALTH_GATE_DAYS' own comment). This REPLACES what
    reconcile_mean_reversion/meanReversion used to do with the hourly
    series (retired -- backwards on both books, see that function's own
    removal), but the mechanism is deliberately different: gated by r20
    and folded directly into momentum, not a standalone rank a human
    reads as "oversold/overbought." A ticker missing hourly coverage (not
    in CANDLESTICK_TOP_N, or too few bars) just gets zero contribution
    from this term -- same graceful-degrade spirit as everything else
    here, not a reason to skip the rest of the Trend Score."""
    raw = {}
    for ticker, row in data.items():
        series = daily_history.get(ticker)
        if not series or len(series) < MOMENTUM_MIN_BARS:
            continue
        closes = [to_float(b.get("close")) for b in series[-MOMENTUM_MIN_BARS:]]
        vols = [to_float(b.get("volume")) or 0.0 for b in series[-MOMENTUM_MIN_BARS:]]
        if any(c is None for c in closes):
            continue
        base10 = closes[-1 - MOMENTUM_FORMATION_DAYS]
        base5 = closes[-1 - MOMENTUM_REVERSAL_DAYS]
        base20 = closes[-1 - TREND_HEALTH_GATE_DAYS]
        if base10 is None or base10 <= 0 or base5 is None or base5 <= 0 or closes[-1] <= 0:
            continue
        r10 = closes[-1] / base10 - 1.0
        r5 = closes[-1] / base5 - 1.0
        r20 = closes[-1] / base20 - 1.0 if base20 and base20 > 0 else None
        formation_vols = vols[-MOMENTUM_FORMATION_DAYS:]
        half = MOMENTUM_FORMATION_DAYS // 2
        v_first = statistics.mean(formation_vols[:half])
        v_second = statistics.mean(formation_vols[half:])
        if v_first <= 0:
            continue
        vol_accel = v_second / v_first - 1.0
        trend_health = _trend_health((hourly_history or {}).get(ticker))
        raw[ticker] = {"r10": r10, "r5": r5, "vol_accel": vol_accel, "r20": r20, "trend_health": trend_health}

    if len(raw) < 10:
        return

    def zscores(key):
        vals = [v[key] for v in raw.values() if v[key] is not None]
        if len(vals) < 10:
            return {t: 0.0 for t in raw}
        mean = statistics.mean(vals)
        stdev = statistics.pstdev(vals)
        return {
            t: ((v[key] - mean) / stdev if stdev > 0 else 0.0) if v[key] is not None else 0.0
            for t, v in raw.items()
        }

    z_r10 = zscores("r10")
    z_r5 = zscores("r5")
    z_vol = zscores("vol_accel")
    z_r20 = zscores("r20")
    z_health = zscores("trend_health")

    # Gate: trend_health only counts for a ticker already ABOVE-average on
    # the 20-day (genuine multi-week trend) measure -- min(max(0, z_r20),
    # MOMENTUM_HEALTH_GATE_CAP), not the raw z_r20 value. A below-average/
    # negative-trend ticker gets exactly zero contribution (max(0, ...)),
    # same as originally intended -- but CAPPING the top end matters too:
    # confirmed live (META, z_r20=+3.37 during a genuine ~15% rally) that
    # an uncapped gate lets an extreme r20 outlier amplify trend_health's
    # contribution far past what was ever validated -- the gate was only
    # ever tested as "does trend_health matter AT ALL for a top-quintile
    # stock" (yes/no), never as "multiply by how extreme the 20-day
    # return is." Uncapped, META's -1.30 (modest, not dramatic)
    # trend_health z-score got multiplied into a -2.19 penalty bigger
    # than its entire +2.04 primary r10 signal, mis-scoring a strongly
    # uptrending stock as a "strong downtrend." Capped at 1.0 (an
    # above-average trend counts fully; further extremity past that
    # doesn't buy additional amplification).
    scores = {
        t: z_r10[t]
        - MOMENTUM_VOL_WEIGHT * z_vol[t]
        - MOMENTUM_REVERSAL_WEIGHT * z_r5[t]
        + MOMENTUM_HEALTH_WEIGHT * min(max(0.0, z_r20[t]), MOMENTUM_HEALTH_GATE_CAP) * z_health[t]
        for t in raw
    }
    ordered = sorted(scores, key=lambda t: scores[t])
    n = len(ordered)
    for i, ticker in enumerate(ordered):
        pct = (i / (n - 1) * 100.0) if n > 1 else 50.0
        data[ticker]["momentum"] = round(pct, 2)


# --------------------------------------------------------------------------- #
#  Mean reversion -- RETIRED (see reconcile_momentum's trend_health term)     #
# --------------------------------------------------------------------------- #
# reconcile_mean_reversion/meanReversion (the hourly Reversal Score) is
# gone -- explicit instruction. It was backwards on both books (long
# rho=+0.071, short rho=-0.106, both the wrong sign for its own
# "oversold=good entry, overbought=bad" premise) despite being a
# recalibration of a real, validated hourly-timeframe mean-reversion
# effect (14h formation predicting the next 21h, rho=-0.168 in the
# dedicated grid study) -- the standalone-rank, human-facing framing
# ("oversold/overbought") never actually paid off live. Its role is
# replaced by reconcile_momentum's trend_health term above: same 14h-
# adjacent hourly measurement, but folded directly into the Trend Score,
# gated on an existing 20-day trend, rather than surfaced as its own
# independent oversold/overbought read.
#
# 35 hours (~5 trading days at 7 bars/day) -- NOT the same window as
# meanReversion's old 14h. Found via a dedicated formation/holding scan fixed
# at a 1-TRADING-DAY holding period (7 hourly bars): rho against next-day
# return is negative (mean-reverting) at every formation length tested,
# but forms a clean, isolated peak at 35h (rho=-0.104, n~29,976) --
# 14h sits near the WEAKEST point of that curve (rho=-0.025), which is
# why the old meanReversion-as-a-scored-factor came out backwards. This
# is a deliberately different, narrower thing: a 1-day-ahead timing read,
# not a replacement for meanReversion and not fed into FACTOR_WEIGHTS at
# all -- explicit instruction, after confirming this signal's own edge
# decays past a ~1-2 day horizon (rho fades to -0.04 by 49h/7 days), so
# it has no business influencing which stocks get picked for a 5-trading-
# day hold. It answers a narrower question instead: for a candidate
# ALREADY selected, is TODAY specifically a good day to place that entry.
ENTRY_TIMING_FORMATION_HOURS = 35
ENTRY_TIMING_MIN_BARS = 40  # formation window + a little slack


def reconcile_entry_timing(data, hourly_history):
    """Mutates `data` in place: writes each row's entryTiming, a
    cross-sectional percentile rank 0-100 on the trailing
    ENTRY_TIMING_FORMATION_HOURS-hour return -- same "low=recently
    fallen=a good day to buy, high=recently run up=a good day to short"
    convention meanReversion uses, same graceful-degrade (a ticker
    outside CANDLESTICK_TOP_N hourly coverage, or with fewer than
    ENTRY_TIMING_MIN_BARS hourly bars, is simply left without a value --
    there's no stale prior value to fall back to since this is a new
    field, unlike meanReversion/momentum's reconcile_* functions).

    Deliberately NOT scored (no FACTOR_WEIGHTS entry) and NOT a gate --
    see this section's own comment above for why. RecommendationsView.tsx
    surfaces it as a plain informational "good day to act" line on cards
    for candidates already selected, nothing else reads it."""
    raw = {}
    for ticker, row in data.items():
        series = hourly_history.get(ticker)
        if not series or len(series) < ENTRY_TIMING_MIN_BARS:
            continue
        closes = [to_float(b.get("close")) for b in series[-ENTRY_TIMING_MIN_BARS:]]
        if any(c is None for c in closes):
            continue
        base = closes[-1 - ENTRY_TIMING_FORMATION_HOURS]
        if base is None or base <= 0 or closes[-1] <= 0:
            continue
        raw[ticker] = closes[-1] / base - 1.0

    if len(raw) < 10:
        return

    mean = statistics.mean(raw.values())
    stdev = statistics.pstdev(raw.values())
    scores = {t: ((v - mean) / stdev if stdev > 0 else 0.0) for t, v in raw.items()}

    ordered = sorted(scores, key=lambda t: scores[t])
    n = len(ordered)
    for i, ticker in enumerate(ordered):
        pct = (i / (n - 1) * 100.0) if n > 1 else 50.0
        data[ticker]["entryTiming"] = round(pct, 2)
