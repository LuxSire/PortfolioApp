"""simulations.py — EPS-driven Monte Carlo price simulation prototype.

Answers "given what we already know about a company's earnings, what's a
plausible RANGE of prices next year, and how likely is it to be above
today's price -- at today's own multiple, and separately, at the
industry's median multiple?" -- entirely from fields already in
forward_pe.csv (zero network calls, same "just read what's on disk"
contract as main.py's own rescore()). Feeds both `python main.py
montecarlo`'s own summary printout and the Simulations tab (see
web/src/pages/SimulationsView.tsx).

THE FORMULA
-----------
For a ticker currently trading at price P0:

1. mu_eps is a 5-YEAR AVERAGE projected forward from forwardEps (explicit
   instruction), not the single-year forwardEps snapshot itself -- and the
   growth rate driving that projection changes source after year 1
   (explicit instruction):

     ownGrowthRate      = avg(epsTrend, marginAdjustedRevenueGrowth)      -- THIS ticker's own
     industryGrowthRate = avg(industryEpsTrend, industryMarginAdjRevGrowth) -- peer MEDIAN
     year2GrowthRate    = avg(ownGrowthRate, industryGrowthRate)          -- explicit instruction
     epsPath = [forwardEps,
                forwardEps      * (1 + year2GrowthRate),      -- year 1 -> year 2
                epsPath[1]      * (1 + industryGrowthRate),   -- year 2 -> year 3
                epsPath[2]      * (1 + industryGrowthRate),   -- year 3 -> year 4
                epsPath[3]      * (1 + industryGrowthRate)]   -- year 4 -> year 5
     mu_eps = mean(epsPath)

   marginAdjustedRevenueGrowth = revenueGrowth * operatingMargin --
   revenue growth converted to its earnings-equivalent (a raw
   revenue-growth % overstates earnings growth for a business that only
   converts a fraction of each new revenue dollar to profit); the
   industry-level version uses the peer group's own median revenueGrowth
   and median operatingMargin the same way. epsTrend is the same 30-day
   consensus estimate revision screenerFactors.js's own "EPS Trend"
   column uses (avg of epsRevision0y/1y, whichever present); industryEpsTrend
   is its peer-median equivalent, from the SAME industry/sector peer
   group step 2's industryPe uses (_peer_median, MIN_INDUSTRY_PEERS
   preferred, MIN_PEERS-sector fallback otherwise).

   Year 1's step blends the ticker's OWN trend/growth with the peer
   group's (explicit instruction: a transition step, not a hard cutover
   from own to industry) -- years 2-4 use the peer group's median outright:
   a single company's own estimate-revision/revenue trend is far too
   noisy (and, for names with an extreme one-off cut or a near-zero-
   baseline % swing, wildly unrepresentative) to extrapolate for 4
   straight years, so it's only ever a HALF-weight influence, fading out
   entirely by year 3. Both growth rates are clamped to [GROWTH_FLOOR
   (-99%), GROWTH_CAP (+100%)] before being blended so a very negative or
   near-zero-baseline input can't flip epsPath's sign or blow up the
   4-year compound. Missing epsTrend/marginAdjustedRevenueGrowth at
   either level falls back to whichever is present, or a flat 0% (no
   signal isn't treated as bad news).

   mu_eps averages the DISCOUNTED path, not the raw nominal one (explicit
   instruction -- discount everything at a rate, base DISCOUNT_RATE (5%)
   scaled by the ticker's own beta, also explicit instruction):

     effectiveDiscountRate = DISCOUNT_RATE * max(beta, BETA_FLOOR)     (beta defaults to 1.0 when missing)
     discountedEpsPath[i]  = epsPath[i] / (1 + effectiveDiscountRate) ** i    (i = 0..4)
     mu_eps = mean(discountedEpsPath)

   ownPe/blendedPe (step 2) are CURRENT multiples, meant to price a
   near-term EPS figure -- averaging 5 years of nominal future earnings
   with no discounting would implicitly treat a dollar of year-5 EPS as
   worth exactly as much as a dollar next year, and hand a rising epsPath
   full, undiscounted credit for its later, more speculative years.
   Discounting first keeps mu_eps on a basis actually consistent with
   what a current multiple should be applied to. Scaling the rate by beta
   is the same CAPM-style intuition a real cost-of-equity estimate uses:
   a dollar of a high-beta (more systematically risky) ticker's future
   earnings is worth less today than a dollar of a low-beta ticker's,
   rather than discounting every ticker at an identical flat rate.

   Next-year EPS is then modeled as Normal(mu_eps, sigma_eps):

     sigma_eps = epsVolatility * abs(mu_eps)

   epsVolatility (see IBApp._eps_volatility) is stdev/mean(|EPS|) of the
   company's own trailing up to 5 years of annual Diluted EPS -- so this
   scales the estimate's spread by how historically unpredictable THIS
   company's own earnings actually are, not a flat guess. Falls back to
   FALLBACK_EPS_REL_STDEV * abs(mu_eps) when epsVolatility itself isn't on
   file (needs >=3 years of annual EPS history -- see that function).

2. The SAME N simulated eps_i draws are priced two ways, each against a
   single FIXED multiple (explicit instruction -- no multiple-level
   distribution/spread; all of the price distribution's shape comes from
   the EPS side alone). No fundamental (book-value/cumulative-earnings)
   floor -- an earlier version of this module had one; explicit
   instruction removed it (see CAVEATS: a floor built from this module's
   own projected epsPath just inherited that projection's own
   uncertainty, and was binding -- silently overriding the model's own
   confidence-weighted view -- for over a quarter of the universe in
   practice):

     price_current_i  = max(eps_i, 0) * ownPe
     price_blended_i  = max(eps_i, 0) * blendedPe

   ownPe is today's own forwardPE. blendedPe is the simple average of
   ownPe and industryPe -- no trailingPE term:

     blendedPe = mean(ownPe, industryPe)

   industryPe is the peer group's MEDIAN forwardPE -- the ticker's own
   granular industry when that industry has at least MIN_INDUSTRY_PEERS
   (20) other tickers with a usable positive forwardPE, below that
   widened to every ticker in the same broad GICS-style sector instead
   (modules/sector_groups.py -- explicit instruction: a too-small
   industry peer set isn't a reliable comp group). price_blended_i is
   omitted (blendedPe is None) entirely when even the broad sector
   doesn't clear MIN_PEERS (5) -- no industryPe to blend with at all.

   Median rather than mean for industryPe specifically (explicit
   instruction) -- a single extreme peer multiple (a richly-valued
   outlier, or a distressed near-zero one) would otherwise pull the whole
   benchmark toward it; the median stays representative of where most
   peers actually sit.

   Floored at 0 rather than left negative -- a below-zero simulated EPS
   draw isn't sellable through this model, so it's treated as "worth
   nothing" rather than producing a nonsensical negative price.

   Because both series are the SAME eps_i scaled by two different
   constants, they're proportional to each other draw-for-draw (their
   ratio is always exactly blendedPe / ownPe) -- what's actually
   informative is comparing the two resulting distributions' medians and
   their separate P(price > P0), since that probability depends on where
   P0 falls relative to each scaled distribution. P(price_current_i > P0)
   is NOT guaranteed to sit near 50%: that was only true in an earlier
   version of this module, when mu_eps was defined as exactly forwardEps
   (so P0 / ownPe = mu_eps by construction); now that mu_eps is the
   discounted 5-year projected average from step 1, it can differ from
   forwardEps whenever the growth rates there (or DISCOUNT_RATE itself)
   are nonzero, and the probability moves with it -- the intended effect,
   not a bug.

3. Report both price_current_i and price_blended_i distributions'
   mean/median/stdev/percentiles and P(price > P0), plus a `comparison`
   block: the blended-vs-current median difference (absolute and %) and
   the multiple ratio (blendedPe / ownPe) driving it.

4. Discount that median difference by a `confidence` score so a high
   projected upside built on a historically unstable earner doesn't look
   as attractive as an equal-sized upside from a predictable one:

     confidence = 1 / (1 + epsVolatility)

     discountedMedianDiff    = medianDiff * confidence
     discountedMedianDiffPct = medianDiffPct * confidence

   Confidence is ONLY epsVolatility now -- epsTrend and revenueGrowth
   already shape mu_eps directly in step 1 above, so discounting the diff
   by them again here would double-count the same two signals (this
   module's earlier version did fold them into confidence too, before
   step 1 existed; removed once the projection made that redundant).

   Multiplying (not dividing by a risk term, i.e. not a Sharpe-style
   ratio) keeps the discounted figure in the SAME $/% units as the raw
   medianDiff/medianDiffPct it's derived from -- still directly
   comparable and meaningful across tickers, just pulled toward zero (no
   move) in proportion to how little the estimate should be trusted.

5. Derive a ONE-YEAR price target from the 5-year DCF, not a "fair value
   today":

   The DCF in steps 1-4 prices the stock as if its 5-year discounted EPS
   stream should be reflected in the price TODAY. But the forward-looking
   signal we actually want is WHERE SHOULD THE STOCK BE IN 12 MONTHS --
   matching the horizon of analyst price targets (used as the floor/cap)
   and of real trading decisions.

   From year 1's perspective, the same eps_path is one discount period
   closer. Every discounted_eps_path[i] = eps_path[i] / (1+r)^i
   becomes eps_path[i] / (1+r)^(i-1) = discounted_eps_path[i] * (1+r),
   so the 5-year average just multiplies by (1+r):

     mu_eps_y1 = mu_eps * (1 + effectiveDiscountRate)

   The 1-year price target follows directly:

     forecastPrice = forecastPrice_dcf * (1 + effectiveDiscountRate)
     forecastReturn = forecastPrice / currentPrice - 1

   where forecastPrice_dcf = currentPrice + discountedMedianDiff is the
   "fair value today" from step 4.

   Interpretation: a stock trading exactly at its DCF fair value should
   appreciate by effectiveDiscountRate ≈ DISCOUNT_RATE × beta over the
   next year (its cost of equity -- exactly what DCF theory implies for a
   fairly valued stock). An undervalued stock (discountedMedianDiff > 0)
   earns an additional premium on top; an overvalued one earns less.

   EPS floor/cap (analyst target bounds) are applied to the year-1
   EPS distribution before scaling to 5-year space (see eps_floor_y1 /
   eps_cap_y1), so they constrain year-1 EPS draws only, not the full
   5-year projection.

CAVEATS -- read before trusting a number out of this
------------------------------------------------------
- Normal is a simplifying assumption. Real EPS distributions are often
  skewed and fat-tailed (single earnings beats/misses) in ways a
  symmetric bell curve understates.
- This is EARNINGS-DRIVEN only. It says nothing about sentiment, macro,
  rate moves, or a growth-narrative re-rating -- usually the bigger driver
  of SHORT-term price action than the earnings print itself.
- Both multiples are fixed points, not predictions of where the multiple
  is headed -- "at today's multiple" and "at the industry's current
  median multiple" are two fixed what-if scenarios, not a forecast of
  which one actually happens.
- Treat the output as a probabilistic sanity-check range, not a price
  target.
- effectiveDiscountRate (DISCOUNT_RATE * beta) is a simplified CAPM-style
  stand-in for a real cost-of-equity/WACC estimate, not the real thing --
  it has no risk-free-rate or equity-risk-premium term, just a single 5%
  base scaled by beta, and beta itself (yfinance's 5-year monthly figure)
  is itself a noisy, backward-looking risk estimate.
- No fundamental price floor (removed -- explicit instruction). An
  earlier version added one (bookValue + sum(epsPath)), but it was built
  from this SAME module's own projected epsPath, so it inherited that
  projection's uncertainty rather than acting as an independent sanity
  check -- confirmed live: it was binding (forecastPrice pinned exactly
  to the floor) for over a fifth of the universe, and for 17 tickers it
  overrode a genuinely bearish confidence-weighted signal outright.
"""

import numpy as np

from modules.scoring import to_float
from modules.sector_groups import get_sector_group

MIN_PEERS = 5
# Below this many same-industry peers, widen to the whole broad sector
# instead (explicit instruction) -- see _peer_median.
MIN_INDUSTRY_PEERS = 20
FALLBACK_EPS_REL_STDEV = 0.20
N_SIMULATIONS = 20000
PERCENTILES = (5, 25, 50, 75, 95)
# Years averaged into mu_eps -- forwardEps itself (year 1) plus 4 more
# compounded forward at growthRate. See simulate_ticker's own comment.
EPS_PROJECTION_YEARS = 5
# Floor on growthRate so 4 years of compounding can't flip eps_path's
# sign and oscillate -- -99%/year decays toward (but never reaches) zero
# instead.
GROWTH_FLOOR = -0.99
# Cap on growthRate -- without one, a near-zero-baseline epsTrend/
# revenueGrowth artifact (a tiny absolute change reading as a huge %) can
# compound over EPS_PROJECTION_YEARS into an absurd multi-hundred-x
# estimate (confirmed live: BKKT's +779% epsTrend, itself an artifact of
# a ~$0 prior estimate, compounded fwdEps 0.96 -> 574 by year 5 before
# this cap existed). +100%/year is already a generous ceiling for
# sustained growth -- few real businesses compound faster than that for
# 4 years straight.
GROWTH_CAP = 1.0
# Base annual discount rate (explicit instruction), scaled per ticker by
# its own beta (also explicit instruction) -- see simulate_ticker's own
# comment for the effective_discount_rate formula.
DISCOUNT_RATE = 0.05
# Floor on the beta used to scale DISCOUNT_RATE -- a raw beta at or below
# this would flip or collapse the effective discount rate into something
# meaningless rather than "lower risk than the market."
BETA_FLOOR = 0.1


METRIC_KEYS = ("forwardPE", "epsTrend", "revenueGrowth", "operatingMargin")


def _build_peer_pools(data):
    """Precomputes, once for the whole `data` set, per-metric
    {industry: [(ticker, value), ...]} / {sector_group: [(ticker, value),
    ...]} pools for each of METRIC_KEYS -- so every _peer_median lookup
    (the P/E peer-median in step 2, and the industry/sector-median
    epsTrend/revenueGrowth/operatingMargin feeding years 3-5 of the EPS
    projection in step 1) is O(peer group size) per ticker instead of
    O(len(data)) -- matters once a full-universe `--all` run calls it for
    every single ticker (that would otherwise be an O(n^2) rescan).
    Returns {metric_key: (by_industry, by_group)}. forwardPE excludes
    non-positive values (a negative/zero forward P/E carries no
    "multiple" meaning); the other three keep whatever sign they have,
    including negative (a negative epsTrend/revenueGrowth/operatingMargin
    is itself real, informative peer signal, not noise to drop)."""
    pools: dict[str, tuple[dict[str, list[tuple[str, float]]], dict[str, list[tuple[str, float]]]]] = {
        key: ({}, {}) for key in METRIC_KEYS
    }
    for t, d in data.items():
        industry = d.get("sector")
        if not industry:
            continue
        group = get_sector_group(industry)

        pe = to_float(d.get("forwardPE"))
        r0 = to_float(d.get("epsRevision0y"))
        r1 = to_float(d.get("epsRevision1y"))
        trend_parts = [v for v in (r0, r1) if v is not None]
        values = {
            "forwardPE": pe if pe is not None and pe > 0 else None,
            "epsTrend": sum(trend_parts) / len(trend_parts) if trend_parts else None,
            "revenueGrowth": to_float(d.get("revenueGrowth")),
            "operatingMargin": to_float(d.get("operatingMargins")),
        }
        for key, value in values.items():
            if value is None:
                continue
            by_industry, by_group = pools[key]
            by_industry.setdefault(industry, []).append((t, value))
            by_group.setdefault(group, []).append((t, value))
    return pools


def _peer_median(ticker, industry, by_industry, by_group):
    """Median of one metric across peers (excluding the ticker itself),
    preferring the granular industry but widening to the broad sector when
    the industry has fewer than MIN_INDUSTRY_PEERS candidates -- see this
    module's own docstring. Returns (value, peer_count, level); level is
    "industry" or "sector", and value/level are None when even the broad
    sector doesn't clear MIN_PEERS."""
    if not industry:
        return None, 0, None
    industry_vals = [v for t, v in by_industry.get(industry, []) if t != ticker]
    if len(industry_vals) >= MIN_INDUSTRY_PEERS:
        return float(np.median(industry_vals)), len(industry_vals), "industry"
    group = get_sector_group(industry)
    group_vals = [v for t, v in by_group.get(group, []) if t != ticker]
    if len(group_vals) >= MIN_PEERS:
        return float(np.median(group_vals)), len(group_vals), "sector"
    return None, len(industry_vals), None


def _price_stats(prices, current_price):
    """mean/median/stdev/percentiles + P(price > current_price) for one
    already-simulated numpy array of prices."""
    percentiles = {f"p{p}": float(np.percentile(prices, p)) for p in PERCENTILES}
    return {
        "mean": float(prices.mean()),
        "median": percentiles["p50"],
        "stdev": float(prices.std(ddof=1)),
        **percentiles,
        "probAboveCurrentPrice": float((prices > current_price).mean()),
    }


def _combine_growth(eps_trend, margin_adjusted_revenue_growth):
    """avg(eps_trend, margin_adjusted_revenue_growth), whichever are
    present, clamped to [GROWTH_FLOOR, GROWTH_CAP]; 0.0 (flat) when both
    are missing -- shared by both the ticker's own year-1 growth rate and
    the industry/sector-median rate used for years 2+ (see
    simulate_ticker's own comment on why they differ)."""
    parts = [v for v in (eps_trend, margin_adjusted_revenue_growth) if v is not None]
    if not parts:
        return 0.0
    return min(max(sum(parts) / len(parts), GROWTH_FLOOR), GROWTH_CAP)


def simulate_ticker(ticker, data, n=N_SIMULATIONS, rng=None, peer_pools=None):
    """Runs the EPS-driven Monte Carlo price simulation for one ticker (see
    this module's own docstring for the formula). `data` is a
    {ticker: {field: str}} dict shaped like main.load_pe_data(OUTPUT_CSV)'s
    return value -- every field is still a raw CSV string, hence the
    to_float() calls throughout. `peer_pools` is the {metric_key:
    (by_industry, by_group)} dict _build_peer_pools(data) returns -- built
    once by run_iter/run and passed through, or built here on the fly for
    a one-off standalone call. Returns an {"error": ...} entry instead of
    crashing when a required field (forwardEps, price, forwardPE) is
    missing for this ticker -- nothing to simulate from."""
    rng = rng or np.random.default_rng()
    row = data.get(ticker)
    if not row:
        return {"ticker": ticker, "error": "not found in forward_pe.csv"}

    fwd_eps = to_float(row.get("forwardEps"))
    current_price = to_float(row.get("price"))
    own_pe = to_float(row.get("forwardPE"))
    if fwd_eps is None or current_price is None or own_pe is None or own_pe <= 0:
        return {"ticker": ticker, "error": "missing forwardEps, price, or forwardPE"}

    industry = row.get("sector")
    peer_pools = peer_pools if peer_pools is not None else _build_peer_pools(data)

    # EPS trend -- same definition screenerFactors.js's own epsTrendParts
    # uses for the Screener's "EPS Trend" column: the average of the
    # current- and next-fiscal-year 30-day consensus estimate revisions
    # (epsRevision0y/1y), whichever are present.
    eps_revision_0y = to_float(row.get("epsRevision0y"))
    eps_revision_1y = to_float(row.get("epsRevision1y"))
    eps_trend_parts = [v for v in (eps_revision_0y, eps_revision_1y) if v is not None]
    eps_trend = sum(eps_trend_parts) / len(eps_trend_parts) if eps_trend_parts else None

    revenue_growth = to_float(row.get("revenueGrowth"))
    operating_margin = to_float(row.get("operatingMargins"))
    # Revenue growth converted to its EPS-equivalent via the operating
    # margin (explicit instruction) -- a raw revenue-growth % overstates
    # earnings growth for a business that only converts a fraction of
    # each new revenue dollar to profit: margin_adjusted_revenue_growth =
    # revenueGrowth * operatingMargin. None (not 0%) when operatingMargins
    # itself is missing, so growth_parts below falls back to epsTrend
    # alone rather than silently treating "no margin data" as "no
    # growth."
    margin_adjusted_revenue_growth = revenue_growth * operating_margin if (
        revenue_growth is not None and operating_margin is not None
    ) else None

    # mu_eps is a 5-YEAR AVERAGE, not the single-year forwardEps snapshot
    # (explicit instruction). Year 1 is forwardEps itself. The year1->year2
    # step blends this TICKER'S OWN epsTrend/revenueGrowth (own_growth_rate
    # -- the most reliable signal available for what happens next) with the
    # INDUSTRY median rate (explicit instruction: avg of the two, not pure
    # own_growth_rate -- a transition step rather than a hard cutover).
    # Years 2->3, 3->4, 4->5 use the INDUSTRY (or, if too few peers,
    # sector) MEDIAN epsTrend/revenueGrowth outright: a single company's
    # own estimate-revision/revenue trend is far too noisy (and, for names
    # like MSTR/BKKT, too extreme) to extrapolate for 4 straight years --
    # fading toward the peer group's typical trajectory is the same
    # "revert toward the comp set" logic MIN_INDUSTRY_PEERS/_peer_median
    # already use for the P/E multiple in step 2.
    own_growth_rate = _combine_growth(eps_trend, margin_adjusted_revenue_growth)

    ind_eps_trend, _, _ = _peer_median(ticker, industry, *peer_pools["epsTrend"])
    ind_revenue_growth, _, _ = _peer_median(ticker, industry, *peer_pools["revenueGrowth"])
    ind_operating_margin, _, _ = _peer_median(ticker, industry, *peer_pools["operatingMargin"])
    ind_margin_adjusted_revenue_growth = ind_revenue_growth * ind_operating_margin if (
        ind_revenue_growth is not None and ind_operating_margin is not None
    ) else None
    industry_growth_rate = _combine_growth(ind_eps_trend, ind_margin_adjusted_revenue_growth)
    year2_growth_rate = (own_growth_rate + industry_growth_rate) / 2.0

    eps_path = [fwd_eps, fwd_eps * (1.0 + year2_growth_rate)]
    for _ in range(EPS_PROJECTION_YEARS - 2):
        eps_path.append(eps_path[-1] * (1.0 + industry_growth_rate))

    # mu_eps averages the DISCOUNTED path, not the raw nominal one
    # (explicit instruction: discount everything at a 5% rate) -- ownPe/
    # blendedPe are CURRENT multiples, meant to price a near-term EPS
    # figure, not 5 years of nominal future earnings treated as if a
    # dollar in year 5 were worth exactly as much as a dollar next year.
    # Discounting each year back to present value before averaging keeps
    # mu_eps on a basis actually consistent with what a current multiple
    # should be applied to, and stops a rising epsPath from getting full,
    # undiscounted credit for its later, more speculative years.
    #
    # The rate itself is DISCOUNT_RATE scaled by the ticker's own beta
    # (explicit instruction) -- same CAPM-style intuition as a proper
    # cost-of-equity estimate (higher systematic risk -> a dollar of this
    # ticker's future earnings is worth less today than a dollar of a
    # low-beta ticker's), addressing the flat-single-rate caveat a plain
    # DISCOUNT_RATE would otherwise have. beta is forward_pe.csv's own
    # column (yfinance's 5-year monthly beta); missing beta falls back to
    # 1.0 (market-average risk, i.e. the flat DISCOUNT_RATE unscaled).
    # Floored at BETA_FLOOR rather than left at its raw value -- a
    # negative or near-zero beta would flip or collapse the discount rate
    # itself, which isn't a meaningful "less risky than the market"
    # reading so much as a low/negative-correlation artifact.
    beta = to_float(row.get("beta"))
    effective_discount_rate = DISCOUNT_RATE * (max(beta, BETA_FLOOR) if beta is not None else 1.0)
    discounted_eps_path = [eps_path[i] / ((1.0 + effective_discount_rate) ** i) for i in range(len(eps_path))]
    mu_eps = sum(discounted_eps_path) / len(discounted_eps_path)

    eps_vol = to_float(row.get("epsVolatility"))
    if eps_vol is None:
        eps_vol = FALLBACK_EPS_REL_STDEV
        eps_vol_source = "fallback (no epsVolatility on file)"
    elif eps_vol < FALLBACK_EPS_REL_STDEV:
        # Floor epsVolatility at FALLBACK_EPS_REL_STDEV even when on file --
        # a historical window that was unusually quiet can produce implausibly
        # tight values (e.g. ELV 1.4%, GLPI 3.9%) that collapse the EPS
        # distribution to a near-delta-function, making P(price > current) hit
        # exactly 1.0 even for a modest blended-vs-own PE gap. Forward EPS
        # estimates carry irreducible analyst-consensus uncertainty of ~15-20%
        # regardless of how stable recent history was; FALLBACK_EPS_REL_STDEV
        # (20%) already encodes that lower bound for the no-data case and is
        # the right floor here too.
        eps_vol = FALLBACK_EPS_REL_STDEV
        eps_vol_source = "fallback (epsVolatility below minimum)"
    else:
        eps_vol_source = "epsVolatility"
    sigma_eps = eps_vol * abs(mu_eps)

    # Confidence now reflects ONLY earnings-history unpredictability --
    # epsTrend/revenueGrowth already shape mu_eps directly above, so
    # discounting the diff by them again here would double-count the same
    # two signals.
    confidence = 1.0 / (1.0 + eps_vol)

    industry_pe, peer_n, pe_level = _peer_median(ticker, industry, *peer_pools["forwardPE"])
    if industry_pe is not None:
        raw_blended_pe = (own_pe + industry_pe) / 2.0
        # Cap the blended PE within ±50% of the ticker's own PE so an extreme
        # industry median (e.g. a high-growth sector dragging a value name far
        # above its own multiple, or vice-versa) can't produce a wildly
        # unrealistic price scenario. The blended multiple is meant to be a
        # gentle mean-reversion nudge, not a full rerate to the sector.
        blended_pe = min(max(raw_blended_pe, own_pe * 0.5), own_pe * 1.5)
    else:
        raw_blended_pe = None
        blended_pe = None

    # EPS floor/cap: implied by the lowest / highest analyst price target
    # divided by the current forward PE. Analyst targets are 12-MONTH
    # forecasts, so the floor/cap are year-1 EPS bounds -- they must NOT
    # be applied directly to the 5-year-average EPS space (eps_draws).
    # Applying a year-1 cap to a 5-year average would artificially
    # truncate the long-run upside of growing companies (whose mu_eps
    # already exceeds fwd_eps) and make a shrinking company's downside
    # too tight. Instead, scale the bounds by (mu_eps / fwd_eps) so they
    # constrain the YEAR-1-EQUIVALENT of each draw, not the full 5-year
    # projection.
    #   floor is capped at |fwd_eps| (year-1 mean) -- a floor above the
    #     year-1 mean would eliminate all year-1 downside, which is wrong.
    #   cap is floored at |fwd_eps| -- a cap below the year-1 mean would
    #     eliminate all year-1 upside, which is equally wrong.
    # y1_to_avg converts the year-1 EPS bound into the 5-year-average
    # EPS space where the draws live (ratio of discounted 5yr avg to y1).
    y1_to_avg = mu_eps / fwd_eps if abs(fwd_eps) > 1e-9 else 1.0

    target_low_price = to_float(row.get("targetLowPrice"))
    if target_low_price is not None and target_low_price > 0 and own_pe > 0:
        eps_floor_y1 = min(target_low_price / own_pe, abs(fwd_eps))
        eps_floor = eps_floor_y1 * y1_to_avg
        eps_floor_source = "targetLowPrice / ownPe (year-1 scaled)"
    else:
        eps_floor = 0.0
        eps_floor_source = "fallback (no targetLowPrice)"

    target_high_price = to_float(row.get("targetHighPrice"))
    if target_high_price is not None and target_high_price > 0 and own_pe > 0:
        eps_cap_y1 = max(target_high_price / own_pe, abs(fwd_eps))
        eps_cap = eps_cap_y1 * y1_to_avg
        eps_cap_source = "targetHighPrice / ownPe (year-1 scaled)"
    else:
        eps_cap = None
        eps_cap_source = "fallback (no targetHighPrice)"

    eps_draws = rng.normal(mu_eps, sigma_eps, n) if sigma_eps else np.full(n, mu_eps)
    eps_draws_floored = np.maximum(eps_draws, eps_floor)
    if eps_cap is not None:
        eps_draws_floored = np.minimum(eps_draws_floored, eps_cap)

    prices_current = eps_draws_floored * own_pe
    stats_current = _price_stats(prices_current, current_price)

    stats_industry = None
    comparison = None
    if blended_pe is not None:
        prices_industry = eps_draws_floored * blended_pe
        stats_industry = _price_stats(prices_industry, current_price)
        median_diff = stats_industry["median"] - stats_current["median"]
        median_diff_pct = (stats_industry["median"] / stats_current["median"] - 1) if stats_current["median"] else None
        # Discounted, not divided: explicit instruction -- a Sharpe-style
        # ratio (return / risk) isn't directly comparable across tickers
        # as a price-like number any more, whereas multiplying the raw
        # diff by `confidence` keeps it in the same $/% units, just pulled
        # toward zero (no move) the less trustworthy a high eps_vol makes
        # the estimate. A big projected upside built on wildly unstable
        # earnings ends up looking smaller here than an equal-sized upside
        # from a predictable earner, without changing what unit it's in.
        comparison = {
            "medianDiff": median_diff,
            "medianDiffPct": median_diff_pct,
            "peMultipleRatio": blended_pe / own_pe,
            "confidence": confidence,
            "discountedMedianDiff": median_diff * confidence,
            "discountedMedianDiffPct": median_diff_pct * confidence if median_diff_pct is not None else None,
        }

    # The single actionable number for "what does this model forecast" --
    # today's 5-year DCF fair value, shifted one year forward.
    #
    # The DCF gives a "fair value TODAY" = current_price + confidence-
    # discounted gap toward the blended-PE scenario. But analyst targets
    # are 12-month forecasts, and forecastReturn is compared against real
    # trading decisions made over a 1-year horizon, so we want
    # "fair value IN ONE YEAR", not "fair value today".
    #
    # From year-1's perspective the same 5-year EPS path is one discount
    # period closer, so every discounted_eps_path[i] grows by (1+r):
    #   mu_eps_y1 = mean(eps[i] / (1+r)^(i-1))
    #             = mu_eps * (1 + effective_discount_rate)
    # The 1-year price target is therefore the 5-year DCF fair value
    # multiplied by one year's cost of equity. A stock at fair value
    # should appreciate by exactly r over the year; an undervalued stock
    # returns more; an overvalued one returns less.
    #
    # None when there's no industry benchmark to shift toward at all.
    # Floored at 0 (a price can't be negative).
    _forecast_price_dcf = max(0.0, current_price + comparison["discountedMedianDiff"]) if comparison else None
    forecast_price = (
        _forecast_price_dcf * (1.0 + effective_discount_rate)
        if _forecast_price_dcf is not None else None
    )
    # THE ranking signal (explicit instruction): 1-year expected return
    # derived from the 5-year DCF price target above.
    forecast_return = forecast_price / current_price - 1 if forecast_price is not None else None

    # Applied floor/cap in year-1 price units: the analyst-target year-1
    # EPS bounds (eps_floor/cap_y1) × own_pe, scaled forward by (1+r) to
    # match the 1-year price horizon of forecastPrice.
    price_floor = eps_floor * own_pe * (1.0 + effective_discount_rate)
    price_cap = eps_cap * own_pe * (1.0 + effective_discount_rate) if eps_cap is not None else None

    return {
        "ticker": ticker,
        "name": row.get("name") or None,
        "sector": row.get("sector") or None,
        "forecastPrice": forecast_price,
        "forecastReturn": forecast_return,
        "priceFloor": price_floor,
        "priceCap": price_cap,
        "currentPrice": current_price,
        "inputs": {
            "fwdEps": fwd_eps,
            "epsTrend": eps_trend,
            "revenueGrowth": revenue_growth,
            "operatingMargin": operating_margin,
            "marginAdjustedRevenueGrowth": margin_adjusted_revenue_growth,
            "ownGrowthRate": own_growth_rate,
            "year2GrowthRate": year2_growth_rate,
            "industryEpsTrend": ind_eps_trend,
            "industryRevenueGrowth": ind_revenue_growth,
            "industryOperatingMargin": ind_operating_margin,
            "industryGrowthRate": industry_growth_rate,
            "epsPath": eps_path,
            "discountedEpsPath": discounted_eps_path,
            "beta": beta,
            "effectiveDiscountRate": effective_discount_rate,
            "muEps": mu_eps,
            "sigmaEps": sigma_eps,
            "epsVolatilitySource": eps_vol_source,
            "epsFloor": eps_floor,
            "epsFloorSource": eps_floor_source,
            "epsCap": eps_cap,
            "epsCapSource": eps_cap_source,
            "confidence": confidence,
            "ownPe": own_pe,
            "industryMedianPe": industry_pe,
            "rawBlendedPe": raw_blended_pe,
            "blendedPe": blended_pe,
            "peerCount": peer_n,
            "peLevel": pe_level,
        },
        "priceAtCurrentMultiple": stats_current,
        "priceAtBlendedMultiple": stats_industry,
        "comparison": comparison,
        # Not part of the model itself -- a cheap independent cross-check
        # against what sell-side analysts are already projecting.
        "analystTargets": {
            "mean": to_float(row.get("targetMeanPrice")),
            "low": to_float(row.get("targetLowPrice")),
            "high": to_float(row.get("targetHighPrice")),
        },
    }


def run_iter(tickers, data, n=N_SIMULATIONS, seed=None):
    """Same as run() below, but yields each ticker's result as it's
    computed instead of returning the whole list at once -- lets a caller
    run_eps_simulations_iter, not the whole-list-at-once run()), so a
    in real time rather than only after the entire run (which, for `--all`
    across the whole universe, would otherwise look like nothing is
    happening for its full duration)."""
    rng = np.random.default_rng(seed)
    peer_pools = _build_peer_pools(data)
    for t in tickers:
        yield simulate_ticker(t, data, n=n, rng=rng, peer_pools=peer_pools)


def run(tickers, data, n=N_SIMULATIONS, seed=None):
    """Runs simulate_ticker for every ticker in `tickers`, sharing one
    seeded RNG across all of them so a full run is reproducible end to end
    (same seed -> identical output, useful for prototype comparisons)."""
    return list(run_iter(tickers, data, n=n, seed=seed))
