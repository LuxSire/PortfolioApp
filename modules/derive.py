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


def eps_volatility(values):
    """stdev(values) / mean(|values|) over an annual Diluted EPS series
    (scoring.eps_volatility_rank: low is better). Divides by the mean of
    the ABSOLUTE values, not the signed mean -- a plain CV breaks the
    moment annual EPS crosses zero, which happens within 4-5 years even
    for large names. None if < 3 values or the mean absolute value is 0."""
    values = [v for v in (to_float(x) for x in values) if v is not None]
    if len(values) < 3:
        return None
    mean_abs = sum(abs(v) for v in values) / len(values)
    if mean_abs == 0:
        return None
    return statistics.stdev(values) / mean_abs


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
    return eps_volatility(list(_row(stmts, "incomeStmt", "Diluted EPS").values()))


def eps_revisions_from_statements(stmts):
    et = (stmts or {}).get("epsTrend") or {}
    r0 = eps_revision((et.get("0y") or {}).get("current"), (et.get("0y") or {}).get("30daysAgo"))
    r1 = eps_revision((et.get("+1y") or {}).get("current"), (et.get("+1y") or {}).get("30daysAgo"))
    return r0, r1


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
            sec_g = sec_fy_revenue_growth(xe)
            stmt_g = to_float(row.get("annualRevenueGrowth"))
            if (sec_g is not None and stmt_g is not None
                    and abs(sec_g - stmt_g) <= _REVGROWTH_FILED_AGREE_TOL
                    and abs((sec_g + stmt_g) / 2.0) < _REVGROWTH_FILED_SANE_MAX):
                filed = round((sec_g + stmt_g) / 2.0, 6)
                print(f"reconcile revenueGrowth: {ticker} {info_g:+.1%} -> {filed:+.1%} "
                      f"(Tier A: SEC {sec_g:+.1%}, yf-stmt {stmt_g:+.1%})")
                row["revenueGrowth"] = filed
                row["revenueGrowthSource"] = "reconciled-filed"
                counts["reconciled-filed"] += 1
            continue

        if get_sector_group(row.get("sector")) == "Financial Services":
            continue

        blended = blend_quarterly(info_g, xe.get("revenueQuarterly"), row.get("latestQuarterEnd"))
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
#
# FY = SEC XBRL dilutedEPS FY-YoY, else yfinance income_stmt "Diluted EPS"
# FY-YoY (row["dilutedEpsGrowth"] / dilutedEpsPrior / dilutedEpsAnnual from
# statement_metrics).
_EARNGROWTH_INFO_IMPLAUSIBLE = 2.0   # Tier A: |Q| must exceed +/-200%
_EARNGROWTH_FILED_SANE_MAX = 1.0     # ...and |FY| must be under +/-100%
_EARNGROWTH_BLEND_WEIGHTS = (0.5, 0.5)  # Q, FY

# earningsMarginDelta = clip(margin_new, +/-SANITY) - clip(margin_old, +/-SANITY)
# where margin_new = dilutedEPS_FYn   / revenuePerShare_FYn
#       margin_old = dilutedEPS_FYn-1 / revenuePerShare_FYn-1
# -- the year-over-year change in net margin. It's what earnings_growth_rank
# scores on, and the per-year overlay simulations.py adds to its EPS path.
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


def earnings_margin_delta(entry, row):
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
    """(fy_growth or None, is_turnaround). fy_growth = latest FY diluted
    EPS / prior FY - 1, from SEC company_facts.json (`entry`) first, then
    yfinance income_stmt (fields on `row`). is_turnaround is True when the
    prior FY was a loss (<= 0) and the latest FY a profit (> 0) -- no
    meaningful %, but a real signal."""
    e = (entry or {}).get("dilutedEPS") or []
    if len(e) >= 2:
        prev, latest = e[-2].get("val"), e[-1].get("val")
        if prev is not None and latest is not None:
            if prev > 0:
                return latest / prev - 1.0, False
            return None, latest > 0
    yf_g = to_float(row.get("dilutedEpsGrowth"))
    if yf_g is not None:
        return yf_g, False
    prior, latest = to_float(row.get("dilutedEpsPrior")), to_float(row.get("dilutedEpsAnnual"))
    if prior is not None and latest is not None:
        return None, prior <= 0 < latest
    return None, False


def reconcile_earnings_growth(data, xbrl):
    """Mutates `data` in place: rewrites each row's earningsGrowth to the
    reconciled figure (the value every scoring site reads), preserving the
    raw quarterly figure as earningsGrowthQ and stamping
    earningsGrowthSource. `xbrl` = loaded company_facts.json."""
    counts = {"eg-turnaround": 0, "eg-tier-a": 0, "eg-blend": 0, "eg-q": 0, "eg-fy": 0}
    md_n = 0
    for ticker, row in data.items():
        # earningsMarginDelta -- the value earnings_growth_rank scores on.
        md, md_src = earnings_margin_delta(xbrl.get(ticker), row)
        if md is not None:
            row["earningsMarginDelta"] = round(md, 6)
            row["earningsMarginDeltaSource"] = md_src
            md_n += 1
        else:
            # Abstain -- clear any stale value so a discontinuity (spinoff /
            # unit error) reads as neutral, not as the previous run's number.
            row["earningsMarginDelta"] = None
            row["earningsMarginDeltaSource"] = None

        q = to_float(row.get("earningsGrowth"))
        if q is not None:
            row["earningsGrowthQ"] = q
        fy, turnaround = fy_diluted_eps_growth(xbrl.get(ticker), row)

        if turnaround:
            # No reconciled rate -- a % off a <=0 prior year is meaningless.
            # Both consumers treat missing as "no corroboration cap"; the
            # loss->profit signal flows through earningsMarginDelta instead.
            row["earningsGrowth"] = None
            row["earningsGrowthSource"] = "eg-turnaround"
            counts["eg-turnaround"] += 1
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
              + f"  |  earningsMarginDelta on {md_n}")
