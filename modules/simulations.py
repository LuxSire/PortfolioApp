"""simulations.py — EPS-driven Monte Carlo price simulation prototype.

Answers "given what we already know about a company's earnings, what's a
plausible RANGE of fair values today, priced at the industry's own median
multiple, and how likely is the current price to already sit below that
range?" -- entirely from fields already in screen_data.csv (zero network
calls, same "just read what's on disk" contract as main.py's own
rescore()). Feeds both `python main.py simulations`'s own summary
printout and the Simulations tab (see web/src/pages/SimulationsView.tsx).

THE FORMULA
-----------
For a ticker currently trading at price P0:

1. mu_eps is a 5-YEAR AVERAGE of a DISCOUNTED EPS path anchored at
   anchorEps -- the EPS today's price already implies at the peer group's
   own median multiple (P0 / industryPe, see step 2), not the single-year
   forwardEps snapshot. Falls back, only when no industry peer group is
   available at all, to a 50/50 blend of epsCurrentYear (current-fiscal-
   year consensus EPS) and forwardEps, then to a 50/50 blend of
   trailingEps (P0 / trailingPE) and forwardEps, then to forwardEps
   alone -- blending rather than trusting either fallback estimate on its
   own, since each is a single (potentially noisy) data source. Anchoring
   at the industry multiple first (rather than a consensus-EPS blend)
   ties anchorEps to currentPrice by construction, so the model's signal
   comes from the PROJECTED GROWTH TRAJECTORY relative to peers, not also
   a static "this ticker's own multiple differs from its peers'" gap --
   a different, much weaker signal on its own. forwardEps only enters
   as a drift signal on year 1 (independent of whether it also
   contributed to anchorEps above). The growth rate
   reverts from ownGrowthRate to industryGrowthRate via a concave (sqrt)
   schedule:

     ownGrowthRate      = avg(epsTrend, marginAdjustedRevenueGrowth,        -- THIS ticker's own,
                              eulerRevGrowth1y)                            1/3 each when all
                                                                            three present (see
                                                                            _combine_growth)
     industryGrowthRate = avg(industryEpsTrend,                            -- peer MEDIAN of
                              industryMarginAdjRevGrowth,                     each leg, same
                              industryEulerRevGrowth)                         3-way blend as
                                                                               ownGrowthRate
     N = EPS_PROJECTION_YEARS - 1   (= 4 growth steps)
     w_t = sqrt((N - t) / N)        -- concave weight; w_1 ≈ 0.87, w_4 = 0
     g_t = w_t * ownGrowthRate + (1 - w_t) * industryGrowthRate

     Year 1 drift: g_fwd = forwardEps / anchorEps - 1
                   g_1* = Y1_SCHEDULE_WEIGHT * g_1 + (1 - Y1_SCHEDULE_WEIGHT) * g_fwd
                          (schedule 0.6 / g_fwd 0.4)

     epsPath = [anchorEps,
                anchorEps  * (1 + g_1*),            -- year 0 -> year 1
                epsPath[1] * (1 + g_2),              -- year 1 -> year 2
                epsPath[2] * (1 + g_3),              -- year 2 -> year 3
                epsPath[3] * (1 + g_4)]              -- year 3 -> year 4
     mu_eps = weighted_mean(discountedEpsPath, weights=discountWeights)  -- see below

         marginAdjustedRevenueGrowth = revenueGrowth * growth_margin --
     revenue growth converted to its earnings-equivalent (a raw
     revenue-growth % overstates earnings growth for a business that only
     converts a fraction of each new revenue dollar to profit).
     growth_margin is the ticker's own operatingMargin (floored at 0) when
     it's profitable; when it's LOSS-MAKING, the operating margin it could
     credibly reach at scale instead -- its own gross margin less the
     industry-typical gross-to-operating opex load (industryMedianGross -
     industryMedianOp), floored at 0.02 (LOSS_MAKER_MARGIN_FLOOR). So a
     currently loss-making name still gets EPS-growth credit for revenue
     growth, scaled by its unit economics relative to peers, rather than
     zeroed out for being pre-profit. The industry-level version uses the
     peer group's own median revenueGrowth and positive-margin floor.
     epsTrend is the same 30-day
   consensus estimate revision screenerFactors.js's own "EPS Trend"
   column uses (avg of epsRevision0y/1y, whichever present); industryEpsTrend
   is its peer-median equivalent, from the SAME industry/sector peer
   group step 2's industryPe uses (_peer_median, MIN_INDUSTRY_PEERS
   preferred, MIN_PEERS-sector fallback otherwise).

   The concave sqrt schedule means own-rate influence decays fast early
   and flattens as it approaches zero -- year 1 ≈87% own, year 2 ≈71%,
   year 3 ≈50%, year 4 = 0% (pure industry). This captures that the
   company's own near-term signals are most informative for year 1 but
   unreliable to compound over 4 straight years. forwardEps enters only
   as a (1 - Y1_SCHEDULE_WEIGHT) = 40% drift on year 1's growth rate
   (g_fwd = forwardEps/anchorEps - 1): it nudges the first step toward the
   consensus estimate without dominating it. All growth rates are clamped to
   [GROWTH_FLOOR (-99%), GROWTH_CAP (+100%)]. Missing
   epsTrend/marginAdjustedRevenueGrowth falls back to whichever is
   present, or 0% (no signal isn't treated as bad news).

   mu_eps averages the DISCOUNTED path, not the raw nominal one (discount
   everything at a rate, base DISCOUNT_RATE (5%) scaled by the ticker's
   own beta):

     effectiveDiscountRate = DISCOUNT_RATE * clamp(beta, BETA_FLOOR, BETA_CAP)     (beta defaults to 1.0 when missing)
     discountWeights[i]    = 1 / (1 + effectiveDiscountRate) ** i         (i = 0..4)
     discountedEpsPath[i]  = epsPath[i] * discountWeights[i]
     mu_eps = sum(discountedEpsPath) / sum(discountWeights)

   A WEIGHTED mean by discountWeights, NOT a plain mean of the
   already-discounted values -- dividing by the raw year-count (5)
   instead of by the weights' own sum would systematically understate
   mu_eps whenever effectiveDiscountRate > 0, even for a perfectly FLAT
   (zero-growth) epsPath: mean(discountWeights) < 1 for any r > 0, so a
   plain mean would put mu_eps below anchorEps purely from discounting
   mechanics, with no earnings signal behind it at all -- confirmed live
   before this was caught: across 465 near-zero-growth tickers,
   forecastReturn dropped monotonically from +0.7% (beta < 0.75) to
   -12.3% (beta 2-3) despite an unchanged, flat earnings picture in every
   case. The weighted mean is the correct annuity-equivalent average:
   for a flat epsPath it reproduces anchorEps EXACTLY regardless of beta
   (sum(A * discountWeights[i]) / sum(discountWeights) = A, trivially),
   while still weighting near-term years more than distant ones -- the
   discount weights themselves still decay with i, so year 0/1 still
   dominate the average over year 4, exactly as intended.

   industryPe (step 2) is a CURRENT multiple, meant to price a near-term
   EPS figure -- averaging 5 years of nominal future earnings with no
   discounting would implicitly treat a dollar of year-5 EPS as worth
   exactly as much as a dollar next year, and hand a rising epsPath full,
   undiscounted credit for its later, more speculative years. Discounting
   first keeps mu_eps on a basis actually consistent with what a current
   multiple should be applied to. Scaling the rate by beta is the same
   CAPM-style intuition a real cost-of-equity estimate uses: a dollar of a
   high-beta (more systematically risky) ticker's future earnings is worth
   less today than a dollar of a low-beta ticker's, rather than
   discounting every ticker at an identical flat rate.

   Next-year EPS is then modeled as a LOGNORMAL around mu_eps -- EPS is a
   multiplicative quantity (moves in percent terms, can't cross zero), so
   a Normal draw put 10-25% of the mass below zero for a volatile-
   earnings name and the old max(eps_i, 0) then piled it onto a spike at
   exactly 0, biasing every mean/percentile/probability off the array:

     sigma_log = sqrt(ln(1 + combinedVol ** 2))       # same CV as combinedVol
     eps_i     = mu_eps * exp(sigma_log * Z)          # Z ~ N(0, 1), median-preserving

   Same coefficient of variation (combinedVol) and same median (mu_eps)
   as the old Normal, so forecastPrice -- which keys off the median, not
   the mean -- is essentially unchanged; only the sub-zero spike is gone
   and the low tail is a smooth right-skew. (sigma_eps = combinedVol *
   abs(mu_eps), the old Normal's stdev, is still reported in inputs as a
   linear-space spread descriptor.) mu_eps <= 0 falls back to a
   degenerate all-mu_eps array.

   combinedVol combines two INDEPENDENT uncertainty sources via
   root-sum-square (RSS):

     combinedVol = sqrt(epsVolatility ** 2 + analystDispersion ** 2)

   epsVolatility (see IBApp._eps_volatility) is stdev/mean(|EPS|) of the
   company's own trailing up to 5 years of annual Diluted EPS -- so this
   scales the estimate's spread by how historically unpredictable THIS
   company's own earnings actually are, not a flat guess. Falls back to
   the peer/sector-MEDIAN epsVolatility (see METRIC_KEYS/_build_peer_pools)
   when epsVolatility itself isn't on file at all (needs >=3 years of
   annual EPS history -- see that function; ALSO true of any Form 20-F
   foreign private issuer with no SEC XBRL data whatsoever -- confirmed
   live, TSM's own company_facts.json entry is an empty {}), and only
   drops to the flat FALLBACK_EPS_REL_STDEV (20%) floor when even the
   broad sector has nothing to borrow from -- a flat floor for every
   undocumented ticker regardless of peer volatility understated TSM's
   real uncertainty as if a data gap were a confirmed low-risk reading.
   Still floored at FALLBACK_EPS_REL_STDEV either way (industry-median or
   own) when the resulting reading is implausibly low -- an unusually
   quiet historical window (own OR peer) shouldn't collapse the
   distribution to a near-delta-function.
   analystDispersion = (targetHighPrice - targetLowPrice) /
   (2 * targetMeanPrice) is how much sell-side analysts disagree with
   EACH OTHER about where the stock is headed, already in the same
   relative-% space as epsVolatility; combinedVol falls back to
   epsVolatility alone when analyst targets aren't on file. Below
   MIN_CREDIBLE_ANALYSTS (3) contributing analysts, THIS ticker's own
   analystDispersion is substituted with the peer-typical figure instead
   (see MIN_CREDIBLE_ANALYSTS's own comment) -- a 1-2-analyst range
   reflects sample size, not genuine agreement (confirmed live: BKKT, 1
   analyst, reads analystDispersion = 0.0, the tightest possible value,
   purely because there's no second opinion to differ from it).

2. The SAME N simulated eps_i draws are priced ONE way, against a single
   FIXED multiple (no multiple-level distribution/spread; all of the
   price distribution's shape comes from the EPS side alone). An earlier
   version of this design also priced a second, ownPe-scaled scenario
   alongside the industry one ("at today's own multiple" vs. "at the
   industry median") -- retired once mu_eps itself moved to anchoring off
   industryPe (step 1 above): pricing that same industry-anchored EPS at
   the ticker's OWN multiple no longer isolates an independent signal, so
   only the industry scenario remains:

     price_i = max(max(eps_i, 0) * industryPe, bookValueFloor)

   bookValueFloor = min(BOOK_VALUE_FLOOR_MULTIPLE (0.75) * bookValue,
                        MAX_FLOOR_VS_PRICE_MULTIPLE (1.3) * currentPrice)
   -- a pure balance-sheet floor, deliberately NOT the earlier book-value +
   cumulative-earnings floor this module used to have (see CAVEATS: that
   one was built from this SAME module's own projected epsPath, so it
   inherited that projection's own uncertainty and was binding -- silently
   overriding the model's own confidence-weighted view -- for over a
   quarter of the universe in practice). At 3/4 of book value, this floor
   should almost never bind for a solvent company -- it exists only to
   catch a pathological simulated price compounding down toward zero, not
   to express a fair-value opinion the way the old floor effectively did.
   The MAX_FLOOR_VS_PRICE_MULTIPLE cap exists because bookValue alone
   recreated that exact failure mode for a stock already trading far
   below book (confirmed live: NAVI at 37% of book -- 0.75x its book value
   was MORE THAN DOUBLE its current price, and the floor swallowed the
   ENTIRE simulated distribution, p20 through p80, to one exact value).
   "Below book" is often a real, market-priced signal (impairment/wind-
   down risk) for exactly this kind of name, not model noise to override
   -- the price-based cap keeps the floor doing its actual job (catching a
   pathological near-zero price) without manufacturing a guaranteed 100%+
   return out of the book-value gap alone. None (no floor applied) when
   bookValue is missing or non-positive.

   industryPe is the peer group's MEDIAN forwardPE -- the ticker's own
   granular industry when that industry has at least MIN_INDUSTRY_PEERS
   (10) other tickers with a usable positive forwardPE, below that
   widened to every ticker in the same broad GICS-style sector instead
   (modules/sector_groups.py -- a too-small industry peer set isn't a
   reliable comp group). The whole simulation reports no
   forecastPrice/forecastReturn/priceAtIndustryMultiple for a ticker when
   even the broad sector doesn't clear MIN_PEERS (5) -- no industryPe to
   price against at all.

   Median rather than mean for industryPe specifically -- a single
   extreme peer multiple (a richly-valued outlier, or a distressed
   near-zero one) would otherwise pull the whole benchmark toward it; the
   median stays representative of where most peers actually sit.

   Floored at 0 rather than left negative -- a below-zero simulated EPS
   draw isn't sellable through this model, so it's treated as "worth
   nothing" rather than producing a nonsensical negative price.

3. Report the price_i distribution's mean/median/stdev/percentiles and
   P(price > P0) as priceAtIndustryMultiple.

4. Pull a "fair value today" toward currentPrice by a `confidence` score,
   so a high projected upside built on a historically unstable earner (or
   one analysts strongly disagree about) moves the forecast less than an
   equal-sized upside from a predictable, consensus-agreed one:

     confidence     = 1 / (1 + combinedVol)
     fairValueToday = currentPrice + confidence * (priceAtIndustryMultiple.median - currentPrice)

   confidence is ONLY combinedVol (epsVolatility + analystDispersion) --
   epsTrend and revenueGrowth already shape mu_eps directly in step 1
   above, so discounting the diff by them again here would double-count
   the same two signals.

   Adding (not dividing by a risk term, i.e. not a Sharpe-style ratio)
   keeps fairValueToday in the same $ units as currentPrice -- still
   directly comparable across tickers, just pulled toward "no move" in
   proportion to how little the estimate should be trusted.

5. forecastPrice IS the confidence-weighted fair value today -- no
   further adjustment needed to call it that:

     forecastPrice  = max(0, currentPrice + confidence * (priceAtIndustryMultiple.median - currentPrice))
     forecastReturn = forecastPrice / currentPrice - 1

   mu_eps (step 1) is already a genuine present value: it's the mean of
   the DISCOUNTED 5-year EPS path (each year's EPS divided by
   (1+effectiveDiscountRate)^i), so priceAtIndustryMultiple -- and
   forecastPrice, confidence-pulled toward currentPrice from it -- is
   already "what this stock should be worth TODAY," not a nominal
   figure that still needs discounting. An earlier version additionally
   multiplied forecastPrice by (1 + effectiveDiscountRate), reasoning
   that a fairly-valued asset's price should also mechanically drift up
   by its cost of equity over the next year (a second, separate DCF
   claim on top of step 1's own discounting). Dropped: for a high-beta
   ticker that second multiplication let a beta-sized markup dominate
   forecastReturn regardless of the earnings view (e.g. a ticker with a
   dead-neutral raw median -- priceAtIndustryMultiple.median ≈
   currentPrice, i.e. probAboveCurrentPrice ≈ 50% -- could still show a
   double-digit forecastReturn, almost entirely
   beta * DISCOUNT_RATE and unrelated to step 1's earnings projection),
   and it put forecastPrice on a different horizon (12 months out) than
   priceAtIndustryMultiple's own probAboveCurrentPrice (today), which
   never got that same shift -- the two numbers could disagree about
   which side of even a ticker was on for no reason a viewer could see.

   forecastPriceP20/P80 apply that SAME confidence-weighted transform to
   priceAtIndustryMultiple's own p20/p80 instead of its median -- an
   "adjusted" 20/50/80 band around forecastPrice, all on its own scale,
   for charting a bear/median/bull case that isn't three different
   derivations bolted together. Only the central 60% band is reported --
   the lognormal eps_i draw (see below) has a fat upper tail whose p95
   "bull case" isn't wanted. This is NOT the same as the raw
   priceAtIndustryMultiple p20/p80: those are unadjusted (no confidence
   pull toward currentPrice) and can be dramatically wider, since they're
   straight percentiles of the lognormal eps_i draws priced at industryPe.
   There is no longer a separate analyst-target-derived floor/cap PRICE
   (an earlier version had one, built from targetLowPrice/targetHighPrice
   converted to EPS -- retired now that forecastPriceP20/P80 give a
   bear/bull case on forecastPrice's own consistent scale instead).

   eps_i draws are lognormal (step 1) so strictly positive -- the max(.,0)
   that follows is a no-op kept only for the mu_eps <= 0 degenerate
   fallback. No analyst-target-derived floor or cap on either side (see
   CAVEATS): an earlier version
   had both -- eps_floor = min(targetLowPrice/industryPe, abs(forwardEps))
   * (mu_eps/forwardEps), eps_cap the mirror-image on targetHighPrice --
   but whenever the min()/max() picked the abs(forwardEps) branch (common:
   confirmed live for 83% of the universe on the floor side alone), the
   rescaling by mu_eps/forwardEps collapsed the bound to EXACTLY mu_eps,
   silently clipping half the distribution to a single point (several
   tickers had epsFloor == muEps to the last decimal, corrupting P5, P25,
   and for the worst cases the median itself). Removed rather than
   patched on both sides now that the model doesn't need either
   guardrail here.

SIMULATED-PATH FORMULA (SimPrice / simSharpe)
----------------------------------------------
Steps 1-5 above price a single terminal EPS draw per path (eps_i,
Normal(mu_eps, sigma_eps)) against a fixed multiple. SimPrice instead
simulates a FULL 5-year EPS trajectory per path -- the same concave
reversion structure as step 1, run N_SIMULATIONS (20,000) times with
three of its inputs randomized per path, all still priced at the SAME
fixed industryPe used above. This is a SEPARATE, ADDITIONAL output;
forecastPrice/forecastReturn (steps 1-5) are unchanged by any of it.

One shared shock drives all three randomized inputs, so they move
together within a path rather than independently:

  z = clip(Normal(0, 1), -SHOCK_CLIP_SD, +SHOCK_CLIP_SD)   (SHOCK_CLIP_SD = 2.0)

  combined_vol = sqrt(epsVolatility**2 + analystDispersion**2) -- the SAME
               EPS-uncertainty measure forecastPrice's own confidence uses
               (epsVolatility = historical annual-EPS relative swing,
               floored at FALLBACK_EPS_REL_STDEV; analystDispersion =
               (targetHigh - targetLow) / (2*targetMean)). This is the
               ownGrowthRate noise SCALE, so a volatile-earnings name
               (memory, autos, other deep cyclicals) widens its simulated
               distribution instead of only inheriting the peer-valuation
               spread.

  peer_pe_cv = coefficient of variation (stdev / median) of peer
               TRAILING P/E within the SAME industry/sector peer group
               industryPe itself uses, trimmed to [P5, P95] first (see
               _peer_pe_pool_and_cv -- raw stdev/median is not robust to
               real peer-pool outliers, e.g. a 994.9 trailing P/E in
               Semiconductors). This is the reversion-SPEED spread only
               (input 2 below): a name in a tightly-clustered-multiple
               industry gets less reversion-exponent spread than one where
               peers disagree widely.

1. ownGrowthRate is redrawn per path:

     own_growth_sigma = combined_vol * max(abs(ownGrowthRate), GROWTH_NOISE_FLOOR)   (floor = 5%)
     own_growth_i      = ownGrowthRate + z * own_growth_sigma

   industryGrowthRate is deliberately left UNSHOCKED (the plain scalar
   from step 1) in every path -- this is what makes paths properly mean-
   REVERT: the concave weight w_t -> 0 as t -> N regardless of the
   reversion exponent p (below), so every path converges toward the same
   industryGrowthRate-driven tail by year 5, not a randomized one.

2. The reversion exponent p replaces the deterministic case's fixed 0.5
   power (w_t = sqrt((N-t)/N) is the p=0.5 special case of
   w_t = ((N-t)/N)**p):

     p_i = clip(0.5 + z * (peer_pe_cv * 0.5), REVERSION_EXPONENT_MIN, REVERSION_EXPONENT_MAX)   ([0.2, 1.5])

3. g_fwd (year-1 drift, step 1's g_fwd = forwardEps/anchorEps - 1) is
   replaced per path by a draw from the real analyst price-target range
   (targetLowPrice/targetMeanPrice/targetHighPrice), tied to the SAME z
   so a path's growth, reversion speed, AND year-1 drift all move
   together rather than being drawn independently:

     eps_growth_sigma = epsVolatility / SHOCK_CLIP_SD
     g_fwd_low  = clip(targetLowPrice  / currentPrice - 1, GROWTH_FLOOR, GROWTH_CAP)
     g_fwd_high = clip(targetHighPrice / currentPrice - 1, GROWTH_FLOOR, GROWTH_CAP)
     sigma_low  = hypot(max(0, (g_fwd - g_fwd_low)  / SHOCK_CLIP_SD), eps_growth_sigma)
     sigma_high = hypot(max(0, (g_fwd_high - g_fwd) / SHOCK_CLIP_SD), eps_growth_sigma)
     g_fwd_i    = g_fwd + z * (sigma_low if z < 0 else sigma_high)

   epsVolatility is RSS'd into both half-widths so year 1 widens for a
   volatile-earnings name even when its analyst target range is tight.

   g_fwd_i is a linear function of the Normal z, so it is itself
   (split-)Normally distributed: z = -SHOCK_CLIP_SD lands exactly on
   g_fwd_low, z = 0 exactly on g_fwd (today's unchanged deterministic
   value), z = +SHOCK_CLIP_SD exactly on g_fwd_high. sigma_low/sigma_high
   differ (analyst ranges are rarely symmetric around the mean), and both
   are clamped at 0 so a data anomaly (e.g. low > mean) can't flip the
   interpolation's sign.

   When THIS ticker has no analyst target range of its own (targetLowPrice/
   targetHighPrice missing), OR has one but from fewer than
   MIN_CREDIBLE_ANALYSTS (3) contributors (a 1-2-analyst range is
   degenerate, not tight -- see that constant's own comment), falls back
   to the industry/sector-median analyst_dispersion instead of trusting
   the too-thin own range -- PEERS' own (targetHighPrice - targetLowPrice)
   / (2*targetMeanPrice), pooled the same industry-then-sector way as
   every other peer metric (MIN_INDUSTRY_PEERS/MIN_PEERS gates), applied
   as a SYMMETRIC spread around g_fwd since there's no real low/high skew
   to draw from for this ticker, just a typical peer WIDTH:

     sigma_industry = hypot(industryMedianAnalystDispersion / SHOCK_CLIP_SD, eps_growth_sigma)
     g_fwd_i        = clip(g_fwd + z * sigma_industry, GROWTH_FLOOR, GROWTH_CAP)

   When even the broad sector has no analyst coverage to borrow a spread
   from, the last-resort branch is g_fwd + z * eps_growth_sigma (still a
   real per-path spread from epsVolatility alone, not a single fixed
   value). This replaced an earlier "single fixed g_fwd" fallback that
   silently zeroed out one of the three shared-shock inputs' worth of
   variance for an uncovered ticker: COKE (Coca-Cola Consolidated, no
   analyst target data on file) had simReturnVol collapse to 2.3% and
   simSharpe blow up to 18.2 as a direct result, purely from missing
   analyst coverage rather than any genuine earnings predictability.

The terminal multiple is NOT randomized -- every path prices its
simulated EPS at the SAME fixed industryPe the deterministic case uses,
not a per-path draw. An earlier version bootstrap-resampled the terminal
P/E from the real peer trailingPE pool; dropped because multiplying by a
right-skewed random multiple pulled SimPrice's MEAN well above its
median purely from the multiple's own shape, regardless of how
well-behaved the EPS side was (confirmed live: NVDA's peer trailing-P/E
pool alone, even trimmed, still runs 18x-459x -- real peer valuations,
but multiplying by a draw from that shape is a different question than
earnings uncertainty). Removing it brought simulated mean/median ratios
from ~1.5-1.6x down to 0.93-1.00x.

SimPrice = mean of the N simulated per-path prices AFTER winsorizing the
array at its own p5/p95 (multiplicative EPS compounding fattens the right
tail -- the raw mean sits above the body of the distribution on volatile
names; the clip leaves p20/p50/p80 untouched), then scaled down by a
RISK-PREMIUM MULTIPLE HAIRCUT:

  excess     = max(combinedVol - RISK_PREMIUM_COMBVOL_BASELINE, 0)
  pe_haircut = max(1 - RISK_PREMIUM_K * excess, RISK_PREMIUM_PE_FLOOR)
  SimPrice   = median(simPathPrices) * pe_haircut
  simPriceDistribution (p20/p50/p80/mean/stdev/probAboveCurrentPrice) is
             the SAME haircut distribution, so the reported band matches
             the headline number.

The concept: a more uncertain earnings stream should be priced at a LOWER
multiple (a higher demanded risk premium), so SimPrice does not just fan
further from centre as combinedVol rises -- it also gets marked down.
combinedVol (epsVolatility RSS analystDispersion) is the same uncertainty
currency forecastPrice's `confidence` and simSharpe already use. Only the
EXCESS over BASELINE (0.35) is charged -- combinedVol is ~0.32 for nearly
every name (epsVolatility floored at 0.20, analystDispersion ~0.25) and
that normal level is already in the industry multiple, so charging it
would just be a flat tax. K = 0.5 -> a name at combinedVol 1.0 loses ~33%
of its multiple; the floor caps the worst case at a 40% haircut. This is
applied to
SimPrice / SimReturn / simPriceDistribution ONLY -- NOT to forecastPrice
(which keeps its own confidence shrink toward currentPrice, a different
mechanism) and NOT to simSharpe's inputs (which stay on the un-haircut
mean/vol, so risk sits in the Sharpe denominator only, never double-
counted against the premium now in the price).

simSharpe uses the Modified (Israelsen 2005) Sharpe Ratio rather than
the plain formula, because a plain excess_return / volatility ratio
ranks negative-excess-return paths BACKWARDS (dividing a negative
number by a smaller volatility makes it MORE negative, so a badly
underperforming, low-vol name would rank ABOVE a modestly
underperforming, higher-vol one -- exactly backwards):

  excess_return = simReturn - SIM_RF          (SIM_RF = 0.035, matches portfolio_optimizer.py's own RF)
  simSharpe     = excess_return / vol            if excess_return >= 0
                = excess_return * vol             if excess_return <  0

Multiplying (not dividing) by vol when excess_return is negative fixes
the ranking: a MORE negative excess return at a GIVEN vol still ranks
worse, and at a GIVEN negative excess return, HIGHER vol now correctly
ranks worse too (more risk for the same bad outcome), rather than better.

CAVEATS -- read before trusting a number out of this
------------------------------------------------------
- Normal is a simplifying assumption. Real EPS distributions are often
  skewed and fat-tailed (single earnings beats/misses) in ways a
  symmetric bell curve understates.
- This is EARNINGS-DRIVEN only. It says nothing about sentiment, macro,
  rate moves, or a growth-narrative re-rating -- usually the bigger driver
  of SHORT-term price action than the earnings print itself.
- The industry multiple is a fixed point, not a prediction of where the
  multiple is headed -- "at the industry's current median multiple" is a
  fixed what-if scenario, not a forecast of whether that's actually where
  the ticker ends up trading.
- Treat the output as a probabilistic sanity-check range, not a price
  target.
- effectiveDiscountRate (DISCOUNT_RATE * clamp(beta, BETA_FLOOR, BETA_CAP))
  is a simplified CAPM-style stand-in for a real cost-of-equity/WACC
  estimate, not the real thing -- it has no risk-free-rate or
  equity-risk-premium term, just a single 5% base scaled by beta, and beta
  itself (yfinance's 5-year monthly figure) is itself a noisy,
  backward-looking risk estimate. Missing beta (a recent IPO with too
  little trading history to regress one yet, e.g. CBRS) now falls back to
  the peer/sector-median beta rather than a flat market-average 1.0,
  which understated true systematic risk for exactly the kind of young,
  volatile name most likely to be missing one. Clamped to [0.5, 3.0]
  either way (own or peer) so one extreme beta reading (e.g. a raw beta
  of 5+) can't over-discount mu_eps's own 5-year EPS path in step 1 --
  mu_eps can still differ substantially across high/low-beta names, just
  not by an unbounded amount.
- Fundamental price floor: min(0.75x bookValue, 1.3x currentPrice) --
  BOOK_VALUE_FLOOR_MULTIPLE and MAX_FLOOR_VS_PRICE_MULTIPLE, applied to
  both prices_industry and sim_prices. A prior version of this floor
  (bookValue + sum(epsPath)) was removed for being built from this SAME
  module's own projected epsPath, inheriting that projection's own
  uncertainty rather than acting as an independent sanity check --
  confirmed live: it was binding (forecastPrice pinned exactly to the
  floor) for over a fifth of the universe, and for 17 tickers it overrode
  a genuinely bearish confidence-weighted signal outright. The bookValue-
  only replacement recreated the SAME failure mode a second way: NAVI
  trades at 37% of book value (Navient, a student-loan run-off book with
  real, market-priced credit risk), so 0.75x its book value came out at
  more than double its current price -- the floor swallowed the ENTIRE
  simulated distribution (p20 through p80 all collapsed to one exact
  value, simReturnVol -> ~0, simSharpe -> null), not just the tail. Capping
  the floor at MAX_FLOOR_VS_PRICE_MULTIPLE x currentPrice fixes this --
  the floor should now bind rarely and only for a genuinely pathological
  simulated price, not as a mechanism that manufactures a large
  guaranteed return purely from a stock already trading far below book.
  Watch for the same failure mode recurring if either multiple is raised
  much further.
- No analyst-target-derived EPS floor OR cap (both removed -- see step
  5). An earlier version had both: eps_i draws capped at
  targetHighPrice's year-1-equivalent and floored at targetLowPrice's.
  Both used the same construction -- min()/max() against abs(forwardEps),
  rescaled by mu_eps/forwardEps into mu_eps's 5-year-average space -- and
  both had the same bug: whenever the min()/max() picked the
  abs(forwardEps) branch, the rescaling collapsed the bound to EXACTLY
  mu_eps, silently clipping half the Normal draw to a single point.
  Confirmed live on the cap side for PGY (priceAtIndustryMultiple's
  upper percentiles collapsed to one repeated value) and, worse, on the
  floor side for 83% of the simulated universe (binding on >25% of draws;
  several tickers, e.g. COST/IEX/SNEX, had epsFloor == muEps exactly,
  and for the worst cases -- SNEX -- even the median collapsed to the
  same clipped value as the lower percentiles). eps_i is now a lognormal
  draw (strictly positive, step 1), with no analyst-target guardrail on
  either side.
"""

import math
import numpy as np

from modules.scoring import clamp_eps_revision, to_float
from modules.sector_groups import get_sector_group

# Sub-industries where Yahoo's operatingMargins reads as a genuine
# accounting-structure artifact, not real profitability -- a bank's or
# insurer's "revenue" in this ratio is net interest income / premiums net
# of claims, a structurally different (and much smaller) denominator than
# a normal company's gross revenue, so operatingMargins isn't comparable
# and shouldn't feed marginAdjustedRevenueGrowth's EPS-growth projection
# (see that variable's own comment, in simulate_ticker, for the mechanism
# and the live numbers that motivated this).
#
# Deliberately narrower than scoring.py's own is_financials_sector/
# is_real_estate_sector (which serve a DIFFERENT distortion -- missing
# debt/liquidity/ev_ebitda/fcf data -- and are broader on purpose): Asset
# Management, Financial Data & Stock Exchanges, Capital Markets, Credit
# Services, Financial Conglomerates, and Mortgage Finance are
# deliberately NOT included here even though is_financials_sector covers
# them for that other purpose -- confirmed live their elevated margins
# reflect genuine fee/subscription/exchange business economics (real
# operating leverage), not a distorted denominator: Financial Data &
# Stock Exchanges' own median operatingMargin, 44.8%, is actually the
# HIGHEST of any Financials sub-industry checked, not a case that needs
# excluding. Non-mortgage Real Estate (equity REITs) is excluded from
# this set for the same reason -- their own elevated margins look like
# real rental-income economics, not an artifact (confirmed live: every
# equity REIT sub-industry sits at 17-42% vs. non-REIT Real Estate
# Services' 4.7%, which reads close to the broad-universe baseline).
# REIT - Mortgage is the one Real Estate sub-industry included: a
# mortgage REIT's business (borrow short, hold mortgage-backed
# securities long) is functionally a bank's, and its own operatingMargin
# (54.0% median) is the highest of any sector checked -- confirmed to
# share the same distortion, not just a coincidence.
#
# Explicit instruction: deliberately narrow to just Banks/Insurance/
# REIT - Mortgage "for now" -- Capital Markets/Credit Services/Financial
# Conglomerates/Mortgage Finance may turn out to belong here too, but
# haven't been individually confirmed the way these three have.
_MARGIN_DISTORTED_SECTORS = {
    "Banks - Diversified",
    "Banks - Regional",
    "Insurance - Diversified",
    "Insurance - Life",
    "Insurance - Property & Casualty",
    "Insurance - Reinsurance",
    "Insurance - Specialty",
    "Insurance Brokers",
    "REIT - Mortgage",
}


def _has_distorted_operating_margin(sector):
    """True for a curated `sector` (really an industry -- see this
    module's own docstring) where Yahoo's operatingMargins doesn't
    reflect real profitability -- see _MARGIN_DISTORTED_SECTORS' own
    comment for which ones and why."""
    return sector in _MARGIN_DISTORTED_SECTORS


MIN_PEERS = 5
# Below this many same-industry peers, widen to the whole broad sector
# instead (explicit instruction) -- see _peer_median. Lowered from 20 to
# 10, per explicit instruction.
MIN_INDUSTRY_PEERS = 10
# Floor only -- the matching ceiling (modules.derive.EPS_VOLATILITY_CAP)
# is applied at the source, in derive.eps_volatility itself, so every
# consumer here already sees a bounded [FALLBACK_EPS_REL_STDEV,
# EPS_VOLATILITY_CAP] value and doesn't need its own top clamp.
FALLBACK_EPS_REL_STDEV = 0.20
N_SIMULATIONS = 20000
PERCENTILES = (20, 25, 50, 75, 80)
# Years in the EPS path -- anchorEps (year 0) plus 8 growth steps.
# Extended from 4 to 8 -- explicit instruction: a 4-year EPS-multiple
# model structurally can't represent a long-duration, pre-profitability
# growth story (GSAT/TSLA-style names trading on a 5-10+ year narrative,
# not a 4-year one) -- no amount of tempering the SHORT path fixes that,
# only pricing further out does. The concave w_t = sqrt((n_steps-t)/
# n_steps) reversion schedule, years-1-2 real-base blend, and every other
# mechanism in this file are already parametrized by EPS_PROJECTION_YEARS/
# n_steps, not hardcoded to 4 -- own-ticker influence still fades to
# exactly zero by the LAST year, just over 8 steps now instead of 4,
# tempering how much of the extra 4 years is spent extrapolating a
# possibly-noisy own_growth_rate versus convergence_growth_rate. See
# simulate_ticker's own comment for the full schedule.
EPS_PROJECTION_YEARS = 9
# Floor on growthRate so repeated compounding can't flip eps_path's sign
# and oscillate -- -99%/year decays toward (but never reaches) zero
# instead.
GROWTH_FLOOR = -0.99
# Cap on growthRate -- without one, a near-zero-baseline epsTrend/
# revenueGrowth artifact (a tiny absolute change reading as a huge %) can
# compound over EPS_PROJECTION_YEARS into an absurd multi-hundred-x
# estimate (confirmed live: BKKT's +779% epsTrend, itself an artifact of
# a ~$0 prior estimate, compounded fwdEps 0.96 -> 574 by year 5 before
# this cap existed). +100%/year is already a generous ceiling for
# sustained growth -- few real businesses compound faster than that for
# multiple years straight, and now that the path runs 8 years instead of
# 4, the concave schedule's own decay (not this cap alone) is doing more
# of the work of keeping the tail years from running away, since
# own_growth_rate's own weight fades to zero well before year 8 for most
# of the extended path.
GROWTH_CAP = 1.0
# No-coverage forwardEps sanity band. For a ticker with NO analyst
# coverage (numberOfAnalystOpinions absent), if Yahoo's forwardPE is more
# than 3x or less than 1/3 of its own trailingPE, its forwardEps is almost
# always stale or split-contaminated data with no analyst estimate behind
# it (confirmed live: COKE, forwardEps 38.94 vs trailingEps ~7.7 -- a 5x
# gap Yahoo never corrected). simulate_ticker then swaps in trailingEps as
# the forward figure (a flat "no growth priced in" assumption) rather than
# compounding the bad number through g_fwd.
FWD_TRAILING_PE_RATIO_MIN = 0.33
FWD_TRAILING_PE_RATIO_MAX = 3.0
# REVISED (explicit instruction, replaces the old Y1/Y2_SCHEDULE_WEIGHT
# drift-rate mechanism): years 1 AND 2 of the EPS path are now built as a
# LEVEL blend, not a growth-rate blend applied to anchor_eps. See
# REAL_BASE_BLEND_WEIGHT's own comment (in simulate_ticker) for the full
# design and the SNDK/GSAT/PGY/CBRS cases that motivated it -- summary:
# compounding ANY growth rate off anchor_eps (the industry-multiple-
# implied hypothetical EPS) let a bad anchor, or an unrelated bad growth-
# rate input, distort the WHOLE path from year 0 onward. The fix blends
# two REAL, anchor-independent numbers at each of years 1-2 instead:
# ownGrowthRate compounded off a real current-year EPS base, and the
# direct analyst-consensus EPS for that year (forwardEps for year 1,
# eulerFwdEps2y/eulerRevGrowth2y for year 2). A single flat, equal weight
# for both years (explicit instruction), not two separately-tuned
# schedule weights the way Y1_SCHEDULE_WEIGHT/Y2_SCHEDULE_WEIGHT used to
# be. Years 3+ are unchanged: still the concave own->industry reversion
# schedule, just compounding from year 2's real level instead of a chain
# seeded at anchor_eps. The SIMULATED path below (eps_1_sim/eps_2_sim)
# uses this exact same design, per-path.
REAL_BASE_BLEND_WEIGHT = 0.5
# OWN_PE_BLEND_CAP_MULTIPLE: the ASYMPTOTIC ceiling (as a multiple of
# industryPe) a validated ownPE's contribution to the pricing multiple
# (mu_pe) can approach -- via _log_compress_multiple's smooth log/tanh
# transform below, not a hard cliff any more. Originally a hard
# min(ownPE, industryPe*3.0) cap -- explicit instruction to replace that
# with a smooth compression instead, since a hard cap treats a 3.01x
# premium identically to a 100x one (both clipped to exactly 3x), losing
# any distinction between "somewhat rich" and "extraordinarily rich."
# RAW_PE_ABSOLUTE_CAP (below) still bounds the INPUT before any of this
# runs. Confirmed live: HOOD (33.2x, a real 2.4x-of-industry premium) and
# TEAM (28.2x, 1.6x-of-industry) are both comfortably real, credible
# premiums this leaves nearly untouched; GSAT (334.9x) and TEM/Tempus
# (14,752x, RAW_PE_ABSOLUTE_CAP-clipped to 100x first) both compress
# smoothly toward this ceiling instead of being either fully trusted or
# fully rejected.
OWN_PE_BLEND_CAP_MULTIPLE = 3.0
# RAW_PE_ABSOLUTE_CAP: ownPE/trailingPE is clipped to this absolute value
# BEFORE _log_compress_multiple runs -- explicit instruction. Even the
# smooth log/tanh compression below still takes ln(ratio) of whatever
# comes in, so an truly absurd raw reading (TEM's 14,752x) would still
# pull the compressed result meaningfully higher than a merely-extreme one
# (GSAT's 335x) without this -- both are almost certainly data artifacts,
# not distinguishable "how rich" signals, so there's no reason to let one
# drag the compressed output further than the other. 100x is already a
# generous ceiling for a REAL company's own multiple (few businesses
# sustain that on a forward basis) -- this only ever bites for the same
# class of near-zero-EPS-denominator artifact OWN_PE_CONSISTENCY_MIN/MAX
# already screens for elsewhere, as a second, absolute-magnitude
# backstop.
RAW_PE_ABSOLUTE_CAP = 100.0
# OWN_PE_CONSISTENCY_MIN/MAX: ownPE (the row's raw forwardPE field) is only
# trusted for the mu_pe blend when it's roughly consistent with what
# price/forwardEps ITSELF implies -- forwardPE is normally just DEFINED as
# that ratio, so a big divergence between the two means one of the two
# fields is bad data, not a real multiple signal. Confirmed live: TPL
# (Texas Pacific Land, a 100% domestic royalty trust -- not an ADR)
# reports ownPE=5.05, but price/forwardEps = 369.1/9.76 = 37.8 -- a 7.5x
# mismatch, with trailingEps ($7.83) agreeing closely with forwardEps
# ($9.76), so it's the forwardPE FIELD that's wrong here, not forwardEps.
# Trusting that broken 5.05 in the mu_pe blend dragged the pricing
# multiple BELOW even industryPe, the opposite of the credit the blend is
# meant to give. This is the same consistency tell used to catch the
# BABA/STNE-style ADR currency mismatches (fwdEps vs. its own implied
# level), just applied here to gate ownPE's OWN trustworthiness instead of
# excluding a ticker outright.
OWN_PE_CONSISTENCY_MIN = 0.4
OWN_PE_CONSISTENCY_MAX = 2.5
# A blend of anchorEps itself toward currentPrice/ownPE was tried and
# REVERTED (see anchor_eps's own comment below, where it used to apply) --
# it empirically widened simReturn's gap from current price for ordinary
# "quality premium" names (HOOD/TEAM/TSLA/AAPL) instead of narrowing it,
# via the Monte Carlo year-1 spread term, not the level. Not reattempted
# here as a dead constant; see that comment for the full mechanism.
# g_fwd itself (forwardEps/anchorEps - 1) is blended toward the analyst
# price target's own implied 1-year return, targetMeanPrice/currentPrice -
# 1 -- explicit instruction, confirmed live on FEIM: anchorEps falls back
# to currentPrice/industryPe when a ticker's own multiple is unusable (see
# anchor_eps below), and for a name trading well above its peer multiple
# that makes g_fwd strongly negative on its OWN terms (FEIM: -76%, forward
# EPS 0.93 vs. an industry-multiple anchor of 3.89) regardless of what
# analysts actually expect the stock to do -- the analyst target range
# already fed the per-path SPREAD (g_fwd_low/g_fwd_high below) but never
# the deterministic CENTER, so a strongly bullish target next to a
# richly-priced anchor could widen the simulated distribution's tail
# without ever moving its middle (FEIM: 0 of 20,000 simulated paths ended
# above the current price, despite a $77-85 analyst range sitting above
# it). Weight scales with analyst coverage -- 5 percentage points per
# analyst, capped -- rather than a flat blend, so a single stale estimate
# can't dominate the center the way it already can't dominate the spread
# (see g_fwd_i's own per-path fallback cascade for missing/thin coverage).
TARGET_BLEND_WEIGHT_PER_ANALYST = 0.05
TARGET_BLEND_WEIGHT_MAX = 0.40
# A second, additive contribution to target_weight from Eulerpool's own
# analyst_consensus_score (modules.scoring -- that ticker's covering
# firms' MOST RECENT grades, each mapped to a -1..1 tier scale and
# averaged; see that function's own docstring) -- explicit instruction,
# "Proposal B": how STRONG the consensus is (its magnitude, not whether
# it agrees with g_target's own direction -- Proposal A, not chosen)
# adds up to this much MORE weight on the target-price blend, same
# additive-and-capped shape as the per-analyst term above rather than a
# multiplier on it. A fully bullish or bearish consensus (|score| = 1.0)
# contributes the full amount; a neutral or missing consensus (no
# Eulerpool coverage) contributes 0, same graceful-fallback convention
# the per-analyst term already uses for missing target-price data.
TARGET_BLEND_WEIGHT_PER_CONSENSUS = 0.10
# REVERTED (explicit instruction): a 50/50 anchorEps blend with the
# ticker's own multiple, plus a cap at the highest real forward estimate
# on file, were tried here to fix PGY (anchor too low vs. its own actual
# consensus) and CBRS (anchor too high vs. any real estimate) -- but
# together they broke a much wider set of tickers worse than either
# original problem:
#   SNDK: the blend pulls the anchor toward the ticker's OWN multiple,
#     which is exactly the wrong thing to do when that ticker ALSO has an
#     unrelated, already-broken input (SNDK's spinoff-driven 371.6%
#     trailing revenueGrowth, never reconciled) -- the blend roughly
#     doubled the starting anchor (post-blend ~176 vs. industry-only
#     ~87), and that already-broken growth rate then compounded on top of
#     a starting point twice as large, producing simPrice ~$5,000 against
#     a ~$1,600 actual price.
#   GSAT: the guardrail (capping the anchor at the best real forward
#     estimate) is only correct for a ticker whose TRUE value is priced
#     within the visible 2-year estimate window. For a longer-duration
#     growth story (GSAT's satellite-deal ramp), the cap chopped a
#     reasonable blended anchor (~3.74) down to eulerFwdEps2y (0.31, a
#     tiny 2-years-out consensus number with nothing to do with where the
#     market actually expects this business to be), collapsing simPrice
#     to a ~97% "loss" with no real basis.
# Net: this fix made the model MORE sensitive to each ticker's own
# (sometimes noisy, sometimes just short-horizon) inputs and LESS
# anchored to the more stable peer-relative view the original design
# specifically chose for that reason (see this function's own comment on
# Option C above). Reverted back to industry-multiple-only. PGY/CBRS
# remain open problems -- worth a narrower, more surgical fix later, not
# a blanket blend/cap applied to every ticker.
# Base annual discount rate (explicit instruction), scaled per ticker by
# its own beta (also explicit instruction) -- see simulate_ticker's own
# comment for the effective_discount_rate formula.
DISCOUNT_RATE = 0.05
# Risk-free rate for simSharpe (see the simulated-path block) -- same
# assumption modules/portfolio_optimizer.py's own RF already uses, kept
# in sync by hand (no shared constants module across the two).
SIM_RF = 0.035
# Floor/cap on the beta used to scale DISCOUNT_RATE -- a raw beta at or
# below BETA_FLOOR would flip or collapse the effective discount rate into
# something meaningless rather than "lower risk than the market," and an
# uncapped beta (e.g. PGY's 5.374) lets a single noisy, backward-looking
# beta reading over-discount mu_eps's own 5-year EPS path in step 1.
BETA_FLOOR = 0.5
BETA_CAP = 3.0

# ── simulated-path Monte Carlo (SimPrice) ───────────────────────────────────
# Explicit instruction: alongside the deterministic base case above
# (unchanged, still forecastPrice/forecastReturn), simulate a full EPS path
# per Monte Carlo draw instead of a single terminal EPS draw -- see
# simulate_ticker's own comment on the simulated-path block for the full
# design.
#
# The multiple itself is NOT randomized -- explicit correction. Every
# simulated path prices at the SAME fixed industry_pe the deterministic
# case uses; only the EPS side (ownGrowthRate, reversion speed) is random.
# An earlier version also randomized the terminal P/E (bootstrap-resampled
# from the peer pool, later rank-matched to a shared shock) -- dropped
# because peer trailing P/E is itself genuinely right-skewed (confirmed
# live: NVDA's Semiconductors peer pool, even trimmed, still runs
# 18x-459x), and multiplying by a draw from a right-skewed distribution
# pulls SimPrice's MEAN well above its median purely from the multiple's
# own shape -- a different question from EPS/earnings uncertainty, which
# is what this feature is meant to capture.
#
# ONE shared shock per path (z, standard Normal, clipped to
# +/-SHOCK_CLIP_SD) drives BOTH remaining random inputs (ownGrowthRate,
# reversion speed) together, rather than drawing each independently --
# ties them together economically (a path that's optimistic on growth is
# also more likely to revert more slowly, not independently roll each)
# and, clipped, makes a runaway-extreme path structurally impossible
# rather than just unlikely.
#
# Scaled by peer_pe_cv -- the coefficient of variation of peer trailing
# P/E within the SAME industry-then-sector peer group industryPe/
# industryGrowthRate already use (see _peer_pe_pool_and_cv) -- a name in a
# tightly-clustered-multiple industry gets less randomized EPS spread than
# one where peers disagree widely on valuation, even though the multiple
# itself is fixed either way.
#
# industryGrowthRate is deliberately left UNSHOCKED (the plain
# deterministic point estimate, not a per-path array) -- only
# ownGrowthRate carries the shock. This is what makes the simulated paths
# properly mean-reverting, per explicit instruction: the existing concave
# schedule (w_t, own's own weight) already decays to 0 by year N
# regardless of shock, so g_t = w_t*(ownGrowthRate + shock) +
# (1-w_t)*industryGrowthRate converges EXACTLY to the same unshocked
# industryGrowthRate for every path by year N -- an early lucky/unlucky
# path doesn't just grow more slowly from a permanently inflated base, it
# actually reverts, because nothing shock-derived survives past year N.
SHOCK_CLIP_SD = 2.0
# GROWTH_NOISE_FLOOR: ownGrowthRate noise is
# peer_pe_cv * max(abs(rate), GROWTH_NOISE_FLOOR) -- a floor, not a bare
# peer_pe_cv * abs(rate), so a ticker whose CURRENT point-estimate growth
# happens to sit near 0% (common when epsTrend/revenueGrowth data is
# thin) doesn't collapse to near-zero-variance noise; "true" growth could
# plausibly be positive or negative even when today's point estimate
# reads flat, and the simulated distribution should reflect that.
GROWTH_NOISE_FLOOR = 0.05
# Below this many analysts, a ticker's OWN targetLow/targetMean/targetHigh
# range isn't treated as a real measurement of disagreement -- with 1-2
# contributors, a tight (or exactly zero, at n=1) range reflects sample
# size, not genuine consensus. Confirmed live: BKKT (1 analyst) reads
# analystDispersion = 0.0 -- the single most "certain" reading a wider-
# covered peer with real disagreement could never produce -- purely an
# artifact of n=1, not evidence this name is unusually predictable.
# Below MIN_CREDIBLE_ANALYSTS, both combined_vol's own analyst_dispersion
# term and the per-path g_fwd_i draw route through the SAME peer-typical
# fallback this module already uses for a ticker with NO target range at
# all (see industry_analyst_dispersion below) -- treating "too thin to
# trust" the same as "missing," rather than trusting a too-small sample's
# own degenerate spread.
MIN_CREDIBLE_ANALYSTS = 3
# ANALYST_TARGET_FLOOR_MULTIPLE/CAP_MULTIPLE: simPrice is clipped to
# [targetLowPrice x 0.5, targetHighPrice x 2.0] when analyst coverage is
# credible (not thin_coverage) -- explicit instruction. A revenue-multiple
# floor was tried and reverted (see mu_eps's own comment on why) after it
# broke badly for genuinely low-margin businesses; real analyst price
# targets are a far more robust sanity bound for the SAME underlying
# problem (an EPS/revenue-multiple model structurally missing something a
# name's price reflects) -- they already incorporate leverage, margin
# structure, competitive position, and qualitative judgment this model
# can't reconstruct from raw ratios. Confirmed live: GSAT's 3 analysts
# unanimously target $90 (targetLow == targetMean == targetHigh, ABOVE
# its own $82.48 price) while this model's own simPrice sat at $2.90 --
# CIFR/TEM/TXG/BBIO all show the same pattern, real analyst targetLow
# prices sitting far above what this model's EPS path alone produces. The
# 0.5x/2.0x multiples are a wide, deliberately generous band -- this is a
# sanity backstop against the model being drastically wrong, not a
# replacement for its own EPS-driven estimate, so it should rarely bind
# for a name whose own valuation is even roughly plausible.
ANALYST_TARGET_FLOOR_MULTIPLE = 0.5
ANALYST_TARGET_CAP_MULTIPLE = 2.0
# Reversion-speed exponent: the deterministic schedule's
# w_t = sqrt((N-t)/N) is the p=0.5 case of w_t = ((N-t)/N)**p; each
# simulated path's p moves with the SAME shared shock around 0.5, clipped
# to this range so a path never gets a degenerate near-flat (large p) or
# near-instant (small p) reversion shape.
REVERSION_EXPONENT_MIN = 0.2
REVERSION_EXPONENT_MAX = 1.5

# ── SimPrice risk-premium multiple haircut ──────────────────────────────────
# A more uncertain earnings stream is priced at a LOWER multiple. SimPrice
# (the simulated-path price) is scaled by
#   excess     = max(combinedVol - RISK_PREMIUM_COMBVOL_BASELINE, 0)
#   pe_haircut = max(1 - RISK_PREMIUM_K * excess, RISK_PREMIUM_PE_FLOOR)
# combinedVol = sqrt(epsVolatility**2 + analystDispersion**2) is the same
# uncertainty currency `confidence` and simSharpe already use. Only
# uncertainty ABOVE the baseline is penalised -- epsVolatility is floored
# at 0.20 and analystDispersion runs ~0.25, so combinedVol is ~0.32 for
# essentially every name and that "normal" level is already priced into the
# industry multiple itself; without the baseline the haircut is a near-
# uniform ~10-15% tax instead of a differentiator. BASELINE 0.35 -> a
# typical name gets no haircut; K = 0.5 -> a name at combinedVol 1.0 loses
# ~33% of its multiple; the floor caps the worst case at a 40% haircut.
# Applied to SimPrice / SimReturn / simPriceDistribution ONLY -- forecastPrice
# keeps its own confidence shrink, and simSharpe stays on the un-haircut
# mean/vol so the premium isn't double-counted against the Sharpe denominator.
RISK_PREMIUM_COMBVOL_BASELINE = 0.35
RISK_PREMIUM_K = 0.5
RISK_PREMIUM_PE_FLOOR = 0.6

# ── Fundamental price floor ──────────────────────────────────────────────────
# Explicit instruction: reintroduce a fundamental floor, but a MUCH more
# conservative one than the earlier version this module's own CAVEATS
# section describes removing. That one was bookValue + sum(epsPath) -- built
# from this SAME module's own projected earnings, so it inherited that
# projection's own uncertainty rather than acting as an independent sanity
# check, and was binding (silently pinning the output) for over a fifth of
# the universe. This floor uses ONLY a pure balance-sheet number (yfinance's
# own per-share bookValue, no epsPath involved at all) at a fraction deep
# enough that it should almost never bind for a solvent company -- it exists
# to catch pathological simulation outputs (a deeply cyclical or loss-making
# name's price compounding down toward zero across enough bad-growth paths),
# not to express a view on fair value the way the old floor effectively did.
# Missing/non-positive bookValue -> no floor applied (graceful degrade, same
# as every other optional input in this file) -- a stock this doesn't cover
# is simply left on the unfloored distribution, same as before this existed.
# Raised from 0.5 to 0.75 (explicit instruction) -- verified live first at
# 0.5 that the floor binds rarely (1 of 1652 tickers with a usable
# bookValueFloor, ERIC), so there's room to raise it without recreating the
# old floor's failure mode of binding broadly across the universe.
BOOK_VALUE_FLOOR_MULTIPLE = 0.75
# The book-value floor alone can still recreate the OLD floor's exact
# failure mode for a stock the market has already marked down hard vs.
# book -- confirmed live: NAVI trades at 37% of book value (Navient, a
# student-loan run-off book with real, market-priced credit/impairment
# risk), so 0.75x its book value (19.18) came out at MORE THAN DOUBLE the
# current price (9.42) -- before any earnings analysis at all. The floor
# swallowed the ENTIRE simulated distribution (p20 through p80 all
# collapsed to the exact floor value, simReturnVol -> ~0, simSharpe ->
# null), the same "silently pinning the output, corrupting the median"
# failure this floor was specifically designed to avoid. "Below book" is
# often a real, informationally-loaded signal for exactly this kind of
# name (impairment risk, wind-down risk), not model noise to override.
#
# Fix: cap the floor by CURRENT PRICE too, not book value alone -- the
# floor's actual job is catching a pathological near-zero simulated price
# for an otherwise normally-priced stock, not manufacturing a guaranteed
# 100%+ return for any name already trading far below book.
MAX_FLOOR_VS_PRICE_MULTIPLE = 1.3


METRIC_KEYS = (
    "forwardPE", "trailingPE", "epsTrend", "revenueGrowth", "earningsGrowth",
    "earningsMarginDelta", "operatingMargin", "grossMargin", "analystDispersion",
    "eulerRevGrowth1y", "eulerRevGrowth2y", "epsVolatility", "beta",
)


def _build_peer_pools(data):
    """Precomputes, once for the whole `data` set, per-metric
    {industry: [(ticker, value), ...]} / {sector_group: [(ticker, value),
    ...]} pools for each of METRIC_KEYS -- so every _peer_median lookup
    (the P/E peer-median in step 2, and the industry/sector-median
    epsTrend/revenueGrowth/operatingMargin/eulerRevGrowth1y feeding years
    3-5 of the EPS projection in step 1) is O(peer group size) per ticker
    instead of
    O(len(data)) -- matters once a full-universe `--all` run calls it for
    every single ticker (that would otherwise be an O(n^2) rescan).
    Returns {metric_key: (by_industry, by_group)}. forwardPE excludes
    non-positive values (a negative/zero forward P/E carries no
    "multiple" meaning); epsTrend/revenueGrowth/operatingMargin/
    grossMargin keep whatever sign they have, including negative (a
    negative epsTrend/revenueGrowth/operatingMargin is itself real,
    informative peer signal, not noise to drop -- grossMargin realistically
    never goes negative, but it's pooled the same way for the
    industry-median gross margin the loss-maker growth_margin path uses).
    analystDispersion (relative width of
    a peer's OWN targetLow/targetHigh range, same formula as
    simulate_ticker's own analyst_dispersion) excludes non-positive/
    missing the same way forwardPE does -- it's the industry-median
    fallback g_fwd_draws uses for a ticker with no analyst target range
    of its own (see that block's own comment)."""
    pools: dict[str, tuple[dict[str, list[tuple[str, float]]], dict[str, list[tuple[str, float]]]]] = {
        key: ({}, {}) for key in METRIC_KEYS
    }
    for t, d in data.items():
        industry = d.get("sector")
        if not industry:
            continue
        group = get_sector_group(industry)

        pe = to_float(d.get("forwardPE"))
        tpe = to_float(d.get("trailingPE"))
        r0 = clamp_eps_revision(d.get("epsRevision0y"))
        r1 = clamp_eps_revision(d.get("epsRevision1y"))
        trend_parts = [v for v in (r0, r1) if v is not None]
        # Same formula as simulate_ticker's own analyst_dispersion --
        # relative half-width of THIS peer's targetLow/targetHigh range
        # around its targetMean. Pooled so a ticker with no target range
        # of its own has an industry-typical spread to fall back on (see
        # METRIC_KEYS's own docstring note above).
        t_low = to_float(d.get("targetLowPrice"))
        t_mean = to_float(d.get("targetMeanPrice"))
        t_high = to_float(d.get("targetHighPrice"))
        disp = None
        if t_low is not None and t_mean is not None and t_mean > 0 and t_high is not None:
            disp = max(0.0, (t_high - t_low) / (2.0 * t_mean))
        values = {
            "forwardPE": pe if pe is not None and pe > 0 else None,
            # Same "no meaningful multiple" exclusion as forwardPE --
            # pooled for _peer_pe_pool_and_cv's simulated-path noise scale
            # (own-growth-rate/reversion-speed/g_fwd perturbation width),
            # a separate use from forwardPE's own industryPe/anchorEps
            # role. The terminal multiple itself is never randomized --
            # see simulate_ticker's own comment on why.
            "trailingPE": tpe if tpe is not None and tpe > 0 else None,
            "epsTrend": sum(trend_parts) / len(trend_parts) if trend_parts else None,
            "revenueGrowth": to_float(d.get("revenueGrowth")),
            # Trailing YoY earnings growth -- the peer median caps
            # ind_margin_adjusted_revenue_growth the same way a ticker's own
            # earningsGrowth caps its own (see simulate_ticker). Keeps its
            # sign, like revenueGrowth/operatingMargin.
            "earningsGrowth": to_float(d.get("earningsGrowth")),
            # YoY net-margin change per share (modules.derive) -- the peer
            # median is the industry target the per-year margin-trend
            # overlay fades toward in simulate_ticker.
            "earningsMarginDelta": to_float(d.get("earningsMarginDelta")),
            "operatingMargin": to_float(d.get("operatingMargins")),
            # Non-positive excluded like forwardPE: a real operating company
            # doesn't run a <=0% gross margin -- an exact 0.0 is Yahoo's
            # "no product revenue / not reported" sentinel (common across
            # pre-revenue Biotechnology), and leaving those in drags the
            # peer-median gross margin to 0 and blows up the loss-maker
            # growth_margin (ownGrossMargin - (indGross - indOp)).
            "grossMargin": gm if (gm := to_float(d.get("grossMargins"))) is not None and gm > 0 else None,
            "analystDispersion": disp if disp is not None and disp > 0 else None,
            # Eulerpool's own forward revenue-growth consensus (see
            # modules.derive.reconcile_forward_eps) -- pooled here so
            # industry_growth_rate gets the SAME forward-looking Eulerpool
            # leg ownGrowthRate already has (explicit instruction: the
            # asymmetry where only the near-term own-rate saw this signal
            # was worth closing). Keeps its sign, like revenueGrowth.
            "eulerRevGrowth1y": to_float(d.get("eulerRevGrowth1y")),
            # Same pooling, for the year-2-out estimate -- explicit
            # instruction: gate BOTH eulerRevGrowth1y and eulerRevGrowth2y
            # on MIN_CREDIBLE_ANALYSTS (see simulate_ticker's own thin-
            # coverage gate for why), not just the 1-year one.
            "eulerRevGrowth2y": to_float(d.get("eulerRevGrowth2y")),
            # Historical annual-EPS relative swing (IBApp._eps_volatility,
            # SEC-XBRL-plus-yfinance merged where available -- see
            # modules.derive.eps_volatility_merged) -- pooled so a ticker
            # missing this entirely (e.g. TSM: a Form 20-F foreign private
            # issuer with NO SEC XBRL data at all, confirmed live: an
            # empty {} entry in company_facts.json) gets a peer-typical
            # reading instead of the same flat FALLBACK_EPS_REL_STDEV
            # every other undocumented ticker gets regardless of how
            # volatile its actual peer group runs. Excludes non-positive
            # like grossMargin/analystDispersion -- an exact 0.0 is a
            # miscomputed/degenerate value, not a real "zero volatility"
            # reading, and would drag the peer median toward it.
            "epsVolatility": ev if (ev := to_float(d.get("epsVolatility"))) is not None and ev > 0 else None,
            # yfinance's own 5-year monthly beta -- pooled so a ticker with
            # none on file (a recent IPO with too little trading history to
            # regress one yet, e.g. CBRS) gets a peer-typical systematic-
            # risk reading instead of falling all the way to a flat
            # market-average 1.0 that understates a genuinely volatile
            # young name's true risk. Unfiltered (unlike forwardPE/
            # grossMargin) -- a negative or near-zero beta is itself a
            # real, if unusual, reading worth including in the peer
            # median, and the eventual use already clamps to
            # [BETA_FLOOR, BETA_CAP] regardless of source.
            "beta": to_float(d.get("beta")),
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


def _peer_pe_pool_and_cv(ticker, industry, by_industry, by_group):
    """Trailing-P/E twin of _peer_median, same industry-then-sector
    fallback (same MIN_INDUSTRY_PEERS/MIN_PEERS gates) -- but returns the
    peer VALUES list (winsorized, see below), not just their median, plus
    that list's coefficient of variation (stdev/median). Both feed
    simulate_ticker's simulated-path Monte Carlo: the peer count/level
    feed the `peerPeCount`/`peerPeLevel` diagnostics in the output, and
    the CV (`peer_pe_cv`) is the shared per-industry noise scale for the
    own-growth-rate, reversion-speed, and g_fwd perturbations (see
    GROWTH_NOISE_FLOOR/REVERSION_EXPONENT_MIN/MAX's own comments). The
    terminal multiple itself is priced at the SAME fixed industry_pe as
    the deterministic case in every path -- it is never randomized or
    resampled from this pool; see simulate_ticker's own comment on why.
    Returns (None, None, None) when even the broad sector doesn't clear
    MIN_PEERS, same as _peer_median.

    Trimmed to the [P5, P95] range before either statistic is computed --
    confirmed live this matters, not just tidiness: Semiconductors' own
    25-name peer pool ranges up to a trailing P/E of 994.9 (a near-zero-
    trailing-EPS artifact, the SAME class of problem GROWTH_CAP already
    exists to guard against for growth rates, just showing up in a
    multiple instead of a rate here) -- left untrimmed, raw stdev/median
    read 3.69 (a stdev nearly 4x the median, dominated by a handful of
    outliers) and bootstrap-resampling those same outliers pulled
    simulate_ticker's SimPrice to ~7x forecastPrice for NVDA. Trimmed,
    the same pool's cv drops to a still-wide-but-sane 1.7."""
    if not industry:
        return None, None, None
    industry_vals = [v for t, v in by_industry.get(industry, []) if t != ticker]
    if len(industry_vals) >= MIN_INDUSTRY_PEERS:
        vals, level = industry_vals, "industry"
    else:
        group = get_sector_group(industry)
        group_vals = [v for t, v in by_group.get(group, []) if t != ticker]
        if len(group_vals) >= MIN_PEERS:
            vals, level = group_vals, "sector"
        else:
            return None, None, None
    arr = np.asarray(vals, dtype=float)
    p5, p95 = np.percentile(arr, [5, 95])
    trimmed = arr[(arr >= p5) & (arr <= p95)]
    if len(trimmed) < 2:
        trimmed = arr
    vals = trimmed.tolist()
    median = float(np.median(trimmed))
    cv = float(np.std(trimmed, ddof=1) / median) if median > 0 and len(trimmed) > 1 else 0.0
    return vals, cv, level


def _log_compress_multiple(raw_multiple, industry_multiple, max_ratio):
    """raw_multiple, softened toward industry_multiple when it sits ABOVE
    it -- explicit instruction, replacing the old hard min(raw_multiple,
    industry_multiple*max_ratio) cliff with a smooth transform. First
    clips raw_multiple to RAW_PE_ABSOLUTE_CAP (a truly absurd raw reading,
    e.g. a near-zero-EPS-denominator artifact, shouldn't pull the
    compressed result any further than a merely-extreme one -- both are
    equally untrustworthy as a MAGNITUDE, only their ratio to industry
    differs). Then works in LOG space, since a P/E ratio is inherently
    multiplicative: log_ratio = ln(raw/industry), squashed via tanh scaled
    so it asymptotically approaches ln(max_ratio) (never exceeds it,
    unlike the old hard cap which clipped AT it) as log_ratio grows
    without bound. A modest premium (log_ratio near 0) passes through
    nearly unchanged (tanh(x) ≈ x for small x); an extreme one compresses
    smoothly toward the ceiling instead of being clipped outright at a
    single fixed multiple regardless of how far beyond it the raw value
    sits. Values AT OR BELOW industry_multiple are left untouched -- this
    only ever softens a PREMIUM, the same one-sided treatment the old hard
    cap had (a real discount is its own informative signal, not an
    artifact this function is trying to catch)."""
    if raw_multiple is None or industry_multiple is None or industry_multiple <= 0:
        return raw_multiple
    capped = min(raw_multiple, RAW_PE_ABSOLUTE_CAP)
    if capped <= industry_multiple:
        return capped
    max_log_ratio = math.log(max_ratio)
    log_ratio = math.log(capped / industry_multiple)
    compressed_log_ratio = max_log_ratio * math.tanh(log_ratio / max_log_ratio)
    return industry_multiple * math.exp(compressed_log_ratio)


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


def _combine_growth(eps_trend, margin_adjusted_revenue_growth, euler_rev_growth=None):
    """avg(eps_trend, margin_adjusted_revenue_growth, euler_rev_growth),
    whichever are present, clamped to [GROWTH_FLOOR, GROWTH_CAP]; 0.0
    (flat) when all are missing -- shared by both the ticker's own year-1
    growth rate and the industry/sector-median rate used for years 2+ (see
    simulate_ticker's own comment on why they differ). euler_rev_growth is
    THIS ticker's own eulerRevGrowth1y at the own-rate call site, and the
    peer-pooled MEDIAN eulerRevGrowth1y (see METRIC_KEYS/_build_peer_pools)
    at the industry-median call site -- explicit instruction: closing the
    asymmetry where only the near-term own-rate saw Eulerpool's forward
    consensus and the years-2+ industry reversion target didn't. Equal 1/3
    weight alongside eps_trend and margin_adjusted_revenue_growth when all
    three are present, not a 50/50 blend folded into either of the other
    two, at BOTH call sites."""
    parts = [v for v in (eps_trend, margin_adjusted_revenue_growth, euler_rev_growth) if v is not None]
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
        return {"ticker": ticker, "error": "not found in screen_data.csv"}

    fwd_eps = to_float(row.get("forwardEps"))
    current_price = to_float(row.get("price"))
    own_pe = to_float(row.get("forwardPE"))
    if fwd_eps is None or current_price is None or own_pe is None or own_pe <= 0:
        return {"ticker": ticker, "error": "missing forwardEps, price, or forwardPE"}

    # Fundamental price floor (see BOOK_VALUE_FLOOR_MULTIPLE's own
    # comment) -- applied to both the deterministic priceAtIndustryMultiple
    # array and the Monte Carlo sim_prices array below, right where each is
    # first computed. None (no floor applied) when bookValue is missing or
    # non-positive. Also capped at MAX_FLOOR_VS_PRICE_MULTIPLE x
    # current_price (see that constant's own comment on NAVI) -- a stock
    # already trading far below book gets the price-based cap instead of
    # the raw book-value figure, so the floor can nudge a pathological
    # simulated price back toward sanity without single-handedly
    # manufacturing a 100%+ "return" out of the book-value gap alone.
    book_value = to_float(row.get("bookValue"))
    book_value_floor = (
        min(book_value * BOOK_VALUE_FLOOR_MULTIPLE, current_price * MAX_FLOOR_VS_PRICE_MULTIPLE)
        if book_value is not None and book_value > 0 else None
    )

    industry = row.get("sector")
    peer_pools = peer_pools if peer_pools is not None else _build_peer_pools(data)

    # EPS trend -- same definition screenerFactors.js's own epsTrendParts
    # uses for the Screener's "EPS Trend" column: the average of the
    # capped current- and next-fiscal-year 30-day consensus estimate
    # revisions (epsRevision0y/1y), whichever are present.
    eps_revision_0y = clamp_eps_revision(row.get("epsRevision0y"))
    eps_revision_1y = clamp_eps_revision(row.get("epsRevision1y"))
    eps_trend_parts = [v for v in (eps_revision_0y, eps_revision_1y) if v is not None]
    eps_trend = sum(eps_trend_parts) / len(eps_trend_parts) if eps_trend_parts else None

    revenue_growth = to_float(row.get("revenueGrowth"))
    # Eulerpool's own forward revenue-growth consensus for THIS ticker
    # (fwdRevenue1y/fwdRevenue0y - 1, see modules.derive.
    # reconcile_forward_eps) -- a genuinely new forward-looking growth
    # signal, not a blend of two measurements of the same thing the way
    # forwardEps is: yfinance's own revenueGrowth above is TRAILING, this
    # is analyst consensus for the year ahead. Folded into own_growth_rate
    # below as an equal third leg, not into margin_adjusted_revenue_growth
    # -- it's already a growth RATE, not a revenue level, so running it
    # through growth_margin (a trailing-revenue-to-EPS conversion) would
    # apply a transform it doesn't need.
    euler_rev_growth = to_float(row.get("eulerRevGrowth1y"))
    # Eulerpool's own year-after-next EPS and revenue consensus (see
    # modules.derive.reconcile_forward_eps) -- no yfinance counterpart for
    # either, both used as year 2's direct real-data leg below (see
    # REAL_BASE_BLEND_WEIGHT's own comment): eulerFwdEps2y directly when
    # available, else eulerRevGrowth2y applied to forwardEps.
    euler_fwd_eps2y = to_float(row.get("eulerFwdEps2y"))
    euler_rev_growth2y = to_float(row.get("eulerRevGrowth2y"))
    operating_margin = to_float(row.get("operatingMargins"))
    gross_margin = to_float(row.get("grossMargins"))
    # Revenue growth converted to its EPS-equivalent via the operating
    # positive margin -- a raw revenue-growth % overstates earnings growth
    # for a business that only converts a fraction of each new revenue
    # dollar to profit: margin_adjusted_revenue_growth = revenueGrowth *
    # growth_margin, where growth_margin is max(operatingMargin, 0) for a
    # profitable name and the industry-convergence floor below for a
    # loss-making one. Loss-making growth should not get full EPS credit,
    # but it also should not become a bearish growth signal purely because
    # the company is currently investing ahead of profitability.
    # None (not 0%) when operatingMargins itself is missing, so
    # growth_parts below falls back to epsTrend alone rather than silently
    # treating "no margin data" as "no growth" -- same treatment applied
    # here, deliberately, for a
    # ticker in _MARGIN_DISTORTED_SECTORS regardless of whether
    # operatingMargins is populated: see that set's own comment for which
    # sub-industries and why (Banks/Insurance/REIT - Mortgage's
    # operatingMargins reads structurally inflated -- a bank's or
    # insurer's "revenue" in that ratio is net interest income / premiums
    # net of claims, a much smaller denominator than a normal company's
    # gross revenue, not a real profitability edge -- confirmed live:
    # 37.5% median for all of Financials vs. 12.8% market-wide, with
    # Banks - Regional/Financial Data & Stock Exchanges both over 44%).
    # scoring.py's own FACTOR_WEIGHTS already zeroes the direct margin-
    # quality factor for the broader Financials group for exactly this
    # reason; this is the SAME distorted value feeding in here too,
    # uncorrected until now -- multiplying straight through into a ~4x
    # inflated margin_adjusted_revenue_growth (confirmed live: 5.0%
    # median for all of Financials vs. 1.3% market-wide) and, via
    # own_growth_rate below, into every affected ticker's projected EPS
    # path and forecastReturn, not just the genuinely fast-growing ones.
    margin_distorted = _has_distorted_operating_margin(industry)

    ind_eps_trend, _, _ = _peer_median(ticker, industry, *peer_pools["epsTrend"])
    ind_euler_rev_growth, _, _ = _peer_median(ticker, industry, *peer_pools["eulerRevGrowth1y"])
    ind_euler_rev_growth2y, _, _ = _peer_median(ticker, industry, *peer_pools["eulerRevGrowth2y"])
    ind_revenue_growth, _, _ = _peer_median(ticker, industry, *peer_pools["revenueGrowth"])
    ind_earnings_growth, _, _ = _peer_median(ticker, industry, *peer_pools["earningsGrowth"])
    ind_operating_margin, _, _ = _peer_median(ticker, industry, *peer_pools["operatingMargin"])
    ind_gross_margin, _, _ = _peer_median(ticker, industry, *peer_pools["grossMargin"])
    earnings_growth = to_float(row.get("earningsGrowth"))

    # Thin-coverage gate on eulerRevGrowth1y/2y -- explicit instruction
    # (proposal part A): a ticker with fewer than MIN_CREDIBLE_ANALYSTS
    # (3) contributing analysts gets its OWN Eulerpool revenue-growth
    # estimate replaced by the peer/sector median, the SAME "too few
    # analysts to trust as a real reading" treatment analystDispersion and
    # the forwardPE/trailingPE swap already give thin coverage elsewhere
    # in this file. Confirmed live: BKKT (1 analyst) reads
    # eulerRevGrowth1y=210%, BBUC reads 890% -- both essentially
    # uncorroborated single-source estimates that then dominate
    # ownGrowthRate (1/3 weight) with no independent check. Applied here,
    # before euler_rev_growth/euler_rev_growth2y are used anywhere below
    # (own_growth_rate, direct_eps_2's growth rate).
    n_analysts_for_euler = to_float(row.get("numberOfAnalystOpinions"))
    if n_analysts_for_euler is None or n_analysts_for_euler < MIN_CREDIBLE_ANALYSTS:
        if ind_euler_rev_growth is not None:
            euler_rev_growth = ind_euler_rev_growth
        if ind_euler_rev_growth2y is not None:
            euler_rev_growth2y = ind_euler_rev_growth2y

    # growth_margin is the cents-of-EPS per incremental revenue dollar that
    # revenueGrowth is scaled by to get an EPS-growth equivalent. For a
    # profitable name it's just its own operatingMargin (floored at 0).
    #
    # For a LOSS-MAKING name, its current operatingMargin understates what
    # the business earns per revenue dollar once it stops spending ahead of
    # profitability, so instead credit revenue growth at the operating
    # margin it could CREDIBLY reach at scale: its own gross margin, less
    # the opex load a typical peer carries between gross and operating --
    #     industryOpexLoad = industryMedianGrossMargin - industryMedianOpMargin
    #     growth_margin     = max(ownGrossMargin - industryOpexLoad, 0.02)
    # So a company with peer-typical gross margin lands on roughly the peer
    # operating margin; one with a better gross margin than peers (stronger
    # unit economics) gets more credit; one with a worse gross margin gets
    # less, down to the 0.02 floor. Fully peer-derived -- no fixed
    # flow-through constant. Falls back to the older
    # "industryMedianOpMargin + ownOpMargin" convergence term when gross
    # margin (own or industry) isn't available. Applied for either sign of
    # revenueGrowth: a growing loss-maker gets a partial positive growth
    # signal, a shrinking one a (now margin-scaled) bearish signal.
    LOSS_MAKER_MARGIN_FLOOR = 0.02
    if operating_margin is None:
        growth_margin = None
    elif (
        operating_margin < 0
        and revenue_growth is not None
        and ind_operating_margin is not None
    ):
        industry_opex_load = (
            ind_gross_margin - ind_operating_margin
            if ind_gross_margin is not None and ind_operating_margin is not None
            else None
        )
        if gross_margin is not None and gross_margin > 0 and industry_opex_load is not None and industry_opex_load > 0:
            target_operating_margin = gross_margin - industry_opex_load
        else:
            # No usable industry gross-to-operating gap (thin peer group, or
            # a peer set with no real gross-margin coverage) -- fall back to
            # the older convergence term.
            target_operating_margin = ind_operating_margin + operating_margin
        growth_margin = max(target_operating_margin, LOSS_MAKER_MARGIN_FLOOR)
    else:
        growth_margin = max(operating_margin, 0.0)
    margin_adjusted_revenue_growth = revenue_growth * growth_margin if (
        revenue_growth is not None and growth_margin is not None and not margin_distorted
    ) else None
    # The old max(earningsGrowth, 0) cap on margin_adjusted_revenue_growth
    # is gone -- it worked in growth-RATE space off a possibly tiny/negative
    # prior-year EPS. The earnings-quality correction is now a per-year
    # dollar overlay on the EPS path (own_delta_eps2 / ind_delta_eps2,
    # computed below) that fades own -> industry, so a name whose growth
    # isn't reaching the bottom line gets a negative overlay instead of a
    # rate cap.
    own_growth_rate = _combine_growth(eps_trend, margin_adjusted_revenue_growth, euler_rev_growth)

    # Same exclusion as margin_adjusted_revenue_growth above --
    # ind_operating_margin is a peer MEDIAN of the same structurally
    # inflated ratio, not a company-specific quirk, so it's equally
    # distorted and needs the same treatment.
    ind_growth_margin = max(ind_operating_margin, 0.0) if ind_operating_margin is not None else None
    ind_margin_adjusted_revenue_growth = ind_revenue_growth * ind_growth_margin if (
        ind_revenue_growth is not None and ind_operating_margin is not None and not margin_distorted
    ) else None
    # Same earningsGrowth cap as the own-ticker term above, on the peer
    # median (ind_earnings_growth = _peer_median of earningsGrowth).
    if (
        ind_margin_adjusted_revenue_growth is not None
        and ind_earnings_growth is not None
        and ind_earnings_growth < ind_margin_adjusted_revenue_growth
    ):
        ind_margin_adjusted_revenue_growth = min(
            ind_margin_adjusted_revenue_growth, max(ind_earnings_growth, 0.0)
        )
    industry_growth_rate = _combine_growth(ind_eps_trend, ind_margin_adjusted_revenue_growth, ind_euler_rev_growth)
    # convergence_growth_rate: the actual reversion TARGET the growth
    # schedule fades toward, everywhere industry_growth_rate used to play
    # that role -- explicit instruction. 70% industryGrowthRate (the peer
    # group's own current state), 30% earningsGrowth (this ticker's own
    # REALIZED, backward-looking FY EPS growth -- derive.
    # fy_diluted_eps_growth, real trailing data with its own floors/
    # fallbacks already built in). earningsGrowth was already being read
    # into this function (as earnings_growth, above) but had gone unused
    # for anything beyond display since the old max(earningsGrowth, 0) cap
    # on margin_adjusted_revenue_growth was removed earlier this session --
    # this gives it a real role again, adding a company-specific anchor to
    # what "fully converged" means instead of that always being 100% pure
    # peer-group. industry_growth_rate ITSELF, and its own use as a cap on
    # the industry-side revenue-growth leg above, are UNCHANGED -- only ITS
    # role as the schedule's reversion target is replaced by this blend.
    # earningsGrowth, unlike ownGrowthRate/industryGrowthRate, is NOT
    # already bounded before it reaches here (fy_diluted_eps_growth floors
    # a collapse-to-loss case but never caps a turnaround-from-near-zero
    # one) -- confirmed live, MU's real 2023 memory-downcycle trough left
    # earningsGrowth=834%, and KMT a real trailing turnaround left it at
    # 598%. Weighting it down (90/10, not 70/30) wasn't enough by itself --
    # 10% of an 800%+ reading is still 60-80 points on its own, and (since
    # this schedule gives convergence_growth_rate MORE weight as t -> 4,
    # not less) that pushed the "most converged," supposed-to-be-most-
    # conservative year to the HIGHEST growth rate in the whole path --
    # backwards from the schedule's whole point. Explicit instruction:
    # clamp earningsGrowth itself to GROWTH_CAP (100%) before the blend,
    # same discipline every other growth input in this file already has,
    # so it can influence the direction of convergence_growth_rate without
    # being able to single-handedly dominate it.
    earnings_growth_for_convergence = (
        max(GROWTH_FLOOR, min(GROWTH_CAP, earnings_growth)) if earnings_growth is not None else None
    )
    # Corroborated against revenueGrowth the same way growth_rank corroborates
    # revenue growth against earnings (scoring.growth_rank), just inverted --
    # explicit instruction. GROWTH_CAP alone (100%) still let a real but
    # non-structural margin/base-effect recovery dominate this LONG-RUN
    # steady-state assumption: confirmed live, ENR earningsGrowth=113.6% vs.
    # revenueGrowth=1.5% -- a massive decoupling that's a margin snapback off
    # a depressed prior year, not the business actually growing 113%/year
    # forever. When earningsGrowth outruns revenueGrowth, it's capped at
    # revenueGrowth (floored at GROWTH_FLOOR, never dragged below it) before
    # entering the blend -- a long-run growth rate should track what the
    # business actually sells more of, not a margin swing untethered from
    # it. Only caps the DOWN side of this specific mismatch (earnings ahead
    # of revenue); earnings lagging or matching revenue is left alone, same
    # one-directional shape growth_rank's own corroboration cap uses.
    if (
        earnings_growth_for_convergence is not None and revenue_growth is not None
        and earnings_growth_for_convergence > revenue_growth
    ):
        earnings_growth_for_convergence = max(revenue_growth, GROWTH_FLOOR)
    convergence_growth_rate = (
        max(GROWTH_FLOOR, min(GROWTH_CAP, 0.9 * industry_growth_rate + 0.1 * earnings_growth_for_convergence))
        if earnings_growth_for_convergence is not None else industry_growth_rate
    )

    # Option C anchor: price / industryPE — the EPS that would justify today's
    # price at the peer median multiple. Ties anchorEps to currentPrice by
    # construction (no growth == no signal, since anchorEps*industryPE ==
    # currentPrice exactly), so forecastReturn is driven by the PROJECTED
    # GROWTH TRAJECTORY (this ticker's own growth vs. the peer group's,
    # discounted) rather than also conflating in a static "this ticker's own
    # multiple differs from its peers' " gap -- a genuinely different (and
    # much weaker/value-trap-prone on its own) signal that isn't what the
    # growth-rate machinery below is built to judge. g_fwd = forwardEps/
    # anchor-1 becomes forwardEps*industryPE/price-1: the analyst consensus
    # implied return at the industry multiple.
    #
    # Falls back, only when no industry peer group is available at all, to
    # a 50/50 blend of current-year consensus EPS (epsCurrentYear) and
    # forwardEps, then a 50/50 blend of trailingEps and forwardEps, then
    # forwardEps alone -- blending rather than trusting either fallback
    # estimate in isolation, since each is a single (potentially noisy)
    # data source on its own. fwd_eps is always available by this point
    # (required at function entry), so there's always something to blend
    # with.
    industry_pe, peer_n, pe_level = _peer_median(ticker, industry, *peer_pools["forwardPE"])
    current_year_eps = to_float(row.get("epsCurrentYear"))
    trailing_pe = to_float(row.get("trailingPE"))
    trailing_eps = (
        current_price / trailing_pe
        if trailing_pe is not None and trailing_pe > 0 else None
    )
    # Thin-coverage forwardEps sanity swap (see FWD_TRAILING_PE_RATIO_*
    # above). Thinly-covered name + forwardPE wildly out of line with its
    # own trailingPE => treat forwardEps as bad data and use trailingEps as
    # the forward figure instead (flat, no growth priced in). own_pe
    # follows so ownPe in the output reflects the value actually used.
    # Gate widened from "zero coverage" to "< MIN_CREDIBLE_ANALYSTS" --
    # same thin-coverage threshold used everywhere else in this file --
    # confirmed live: TPL (Texas Pacific Land, a 100% domestic royalty
    # trust) has 2 analysts (nonzero, so the old zero-only gate never
    # triggered) yet forwardPE=5.05 vs. trailingPE=47.14, a 9.3x mismatch,
    # with trailingEps agreeing closely with forwardEps -- exactly the
    # class of broken forwardPE this swap exists to catch, just at a
    # coverage level the old gate didn't reach.
    fwd_eps_source = "forwardEps"
    n_analysts_for_swap = to_float(row.get("numberOfAnalystOpinions"))
    if (
        (n_analysts_for_swap is None or n_analysts_for_swap < MIN_CREDIBLE_ANALYSTS)
        and trailing_eps is not None and trailing_eps > 0
        and trailing_pe is not None and trailing_pe > 0
    ):
        _pe_ratio = own_pe / trailing_pe
        if not (FWD_TRAILING_PE_RATIO_MIN <= _pe_ratio <= FWD_TRAILING_PE_RATIO_MAX):
            fwd_eps = trailing_eps
            own_pe = trailing_pe
            fwd_eps_source = "trailingEps (thin coverage, forwardPE/trailingPE out of band)"

    # own_pe capped at RAW_PE_ABSOLUTE_CAP here, at its own point of final
    # resolution -- explicit instruction: "cap the PE used at the
    # beginning to 100," not just inside _log_compress_multiple's own
    # narrower use (own_pe_for_blend/trailing_pe_for_blend). Every OTHER
    # downstream use of own_pe (direct_eps_1's target_implied_eps_1 =
    # targetMeanPrice/ownPE blend, the reported "ownPe" output field, etc.)
    # now inherits this same bound too, not just the mu_pe blend.
    if own_pe is not None and own_pe > RAW_PE_ABSOLUTE_CAP:
        own_pe = RAW_PE_ABSOLUTE_CAP

    # own_pe_consistent/own_pe_for_blend: computed here (moved up from the
    # mu_pe block below, which reuses these same two names) purely to keep
    # both consumers in one place. A blend of anchorEps itself toward
    # current_price/own_pe_for_blend was tried and REVERTED here (explicit
    # instruction to check, then explicit instruction to revert once
    # checked) -- confirmed live via an isolated A/B (holding the RNG seed
    # and every other input fixed): for any ticker whose ownPE sits ABOVE
    # industryPe (HOOD, TEAM, TSLA, AAPL -- ordinary "quality premium"
    # cases, not just the broken-data ones), blending anchorEps toward
    # price/ownPE actually SHRINKS anchorEps (dividing by a bigger
    # multiple gives a smaller number), which narrows the Monte Carlo's
    # year-1 dollar spread around forwardEps (direct_eps_1_sim =
    # anchor_eps*(1+g_fwd_draws) = forwardEps + anchor_eps*z*sigma at the
    # per-path level -- the spread term scales with anchor_eps) in a way
    # that empirically skewed simReturn MORE negative for all four names
    # tested, not less -- the opposite of the intended effect. forecastPrice/
    # forecastReturn were completely unaffected either way (confirmed:
    # anchorEps's only remaining live role is that Monte Carlo spread, not
    # the deterministic path), so this never touched forecastPrice, and
    # is not worth re-attempting without a different mechanism than a
    # blend on anchorEps itself.
    own_pe_implied = current_price / fwd_eps if current_price is not None and fwd_eps is not None and fwd_eps > 0 else None
    own_pe_consistent = (
        own_pe_implied is not None and own_pe is not None and own_pe > 0
        and OWN_PE_CONSISTENCY_MIN <= own_pe / own_pe_implied <= OWN_PE_CONSISTENCY_MAX
    )
    own_pe_for_blend = (
        _log_compress_multiple(own_pe, industry_pe, OWN_PE_BLEND_CAP_MULTIPLE)
        if own_pe_consistent and industry_pe is not None and industry_pe > 0 else None
    )

    if industry_pe is not None and industry_pe > 0:
        anchor_eps = current_price / industry_pe
    elif current_year_eps is not None and current_year_eps > 0:
        anchor_eps = 0.5 * current_year_eps + 0.5 * fwd_eps
    elif trailing_eps is not None and trailing_eps > 0:
        anchor_eps = 0.5 * trailing_eps + 0.5 * fwd_eps
    else:
        anchor_eps = fwd_eps

    # --- margin-trend EPS overlay ------------------------------------------
    # earningsMarginDelta (modules.derive) = YoY change in net margin, as a
    # fraction of revenue per share ((dilutedEPS_FYn - dilutedEPS_FYn-1) /
    # revenuePerShare), clamped at source to +/-EARN_MARGIN_DELTA_CAP
    # (modules.derive, currently 0.9). Scale it to a $/share figure on the
    # EPS path's own basis by multiplying by eps_2 -- ownDeltaEps2 is
    # then bounded to +/-EARN_MARGIN_DELTA_CAP * eps_2 and never
    # depends on the ticker's own (possibly negative or
    # near-zero) net margin. An earlier version divided by net_margin_now
    # to reconstruct the ticker's "true" revenue per share; that was exact
    # for a healthy positive-margin name but undefined for a loss-maker
    # (net margin <= 0 floored to 0.03 -> a fixed ~33x amplifier), which
    # blew up the overlay for essentially the whole Biotechnology sector.
    # Applied as a per-year additive overlay in the EPS path below that
    # fades own -> industry via the same concave weight w_t the growth rate
    # uses: year 1 = this company's own margin trend, year N = the
    # peer-median margin trend (industryMarginDelta).
    own_margin_delta = to_float(row.get("earningsMarginDelta"))
    ind_margin_delta, _, _ = _peer_median(ticker, industry, *peer_pools["earningsMarginDelta"])
    # Both the deterministic and simulated paths now compound years 3+ off
    # a real eps_2 level (see REAL_BASE_BLEND_WEIGHT's own comment), so the
    # margin-trend overlay is scaled consistently by eps_2/eps_2_sim
    # (own_delta_eps2/ind_delta_eps2, computed after eps_2 exists, further
    # below) instead of the old anchor_eps-scaled pair this used to be.

    # Concave reversion: w_t = sqrt((N-t)/N), own->industry over N steps --
    # applies to years 3+ ONLY now (see REAL_BASE_BLEND_WEIGHT's own
    # comment). Years 1-2 are built first, as a LEVEL blend of two REAL,
    # anchor-independent numbers -- explicit instruction (SNDK/GSAT/PGY/
    # CBRS all traced back to a bad growth-RATE compounding off the
    # artificial anchor_eps, at up to 60-80% weight):
    #
    #   ownGrowthLevel_t = realBase * (1 + ownGrowthRate) ** t
    #     realBase = currentYearEps, else trailingEps (same fallback order
    #     anchor_eps's own no-peer-group branch already uses) -- a REAL
    #     current EPS, not a price/multiple construct. ownGrowthRate is
    #     UNCHANGED (still the 3-way epsTrend / marginAdjustedRevenueGrowth
    #     (yfinance, margin-ratio-converted) / eulerRevGrowth1y (Eulerpool)
    #     blend) -- both revenue-growth sources keep exactly the role they
    #     already had, just compounded off a real base instead of the
    #     anchor.
    #   directEps_1 = forwardEps, optionally blended toward the analyst
    #     target price's own IMPLIED EPS (targetMeanPrice / ownPE -- ownPE
    #     is a REAL, currently-observed ratio, unlike the industry-derived
    #     anchor_eps, so reusing the target-price signal this way doesn't
    #     reintroduce the anchor-contamination problem the old rate-space
    #     version of this same blend had).
    #   directEps_2 = eulerFwdEps2y, or (missing that) forwardEps grown by
    #     eulerRevGrowth2y (used as-is, no margin conversion -- same
    #     precedent as eulerRevGrowth1y in ownGrowthRate).
    #
    # REAL_BASE_BLEND_WEIGHT (a single flat, EQUALLY-weighted constant for
    # both years, explicit instruction) blends ownGrowthLevel against the
    # direct estimate at each year; whichever side is available when only
    # one is (real_base missing, or no Eulerpool/target data at all).
    n_steps = EPS_PROJECTION_YEARS - 1
    eps_path = [anchor_eps]

    # revenue_based_eps: a synthetic, revenue-grounded EPS fallback for
    # when a ticker has no positive real EPS anywhere (currentYearEps and
    # trailingEps both missing/<=0 for real_base; forwardEps also <=0 for
    # directEps_1 -- see that computation's own comment) -- explicit
    # instruction. revenuePerShare (a real, current figure -- almost
    # always positive even for a pre-profit company) at growth_margin (the
    # SAME "credible eventual operating margin this business could reach
    # at scale" already computed above for marginAdjustedRevenueGrowth,
    # not the company's own CURRENT possibly-negative margin) answers
    # "what would this company's per-share earnings look like if it
    # already operated at a normal, credible margin for its business at
    # today's revenue" -- a real, positive foundation for years 1-2 to
    # compound from, instead of nothing at all for a structurally
    # unprofitable but real, scaling business (GSAT, RKLB, RARE-style).
    #
    # leverage_penalty scales that down for a company whose unprofitability
    # is actually a LEVERAGE/debt-service story rather than a genuine
    # "hasn't scaled to a credible margin yet" one -- growth_margin is an
    # OPERATING margin, blind to what happens BELOW that line (interest
    # expense), so it overstates true bottom-line earning power for a
    # heavily-indebted name. Confirmed live: FUN (Six Flags/Cedar Fair, a
    # heavily-levered theme-park operator) computed revenueBasedEps=$3.92
    # against a $13.14 price -- a 3.4x implied P/E -- when its real problem
    # is debt service, not immature unit economics.
    #
    # debtToEquity was considered and REJECTED as the penalty's own input:
    # it's corrupted by the same near-zero/negative-equity artifact this
    # project has repeatedly guarded against elsewhere -- GSAT (143x) and
    # TEM (324x), both genuine growth stories already benefiting from this
    # fallback, show debtToEquity just as extreme as FUN (1455x) for a
    # COMPLETELY different reason (small/negative equity from accumulated
    # growth-stage losses, not a large absolute debt load). Net debt
    # (enterpriseValue - marketCap, immune to the equity-denominator
    # problem since it never divides by equity at all) relative to
    # REVENUE cleanly separates them instead: GSAT (-0.20x, net CASH) and
    # RKLB (-3.51x, net cash, well-funded) correctly read as unleveraged;
    # FUN (1.72x), BBIO (3.72x), RARE (1.31x) correctly read as real debt
    # burdens.
    revenue_per_share = to_float(row.get("revenuePerShare"))
    enterprise_value = to_float(row.get("enterpriseValue"))
    shares_outstanding = to_float(row.get("sharesOutstanding"))
    net_debt_to_revenue = None
    if (
        enterprise_value is not None and shares_outstanding is not None and shares_outstanding > 0
        and current_price is not None and revenue_per_share is not None and revenue_per_share > 0
    ):
        net_debt_per_share = enterprise_value / shares_outstanding - current_price
        net_debt_to_revenue = net_debt_per_share / revenue_per_share
    leverage_penalty = (
        1.0 / (1.0 + max(net_debt_to_revenue, 0.0)) if net_debt_to_revenue is not None else 1.0
    )
    # Applied to the credit ABOVE LOSS_MAKER_MARGIN_FLOOR only, not to
    # growth_margin as a whole -- explicit instruction, the general form
    # of "avoid a quasi-zero starting point" (not just CIFR specifically).
    # Multiplying leverage_penalty straight through growth_margin let TWO
    # independent worst-case floors compound into something far more
    # extreme than either alone represents -- confirmed live, CIFR (real
    # $4.4B net debt vs. ~$198M revenue, netDebtToRevenue=22.3x, a
    # genuine, not artifactual, leverage reading) already had growth_margin
    # pinned at its own floor (2%, LOSS_MAKER_MARGIN_FLOOR), and multiplying
    # THAT by leverage_penalty=0.043 crushed the result to $0.0004 --
    # effectively zero, even though LOSS_MAKER_MARGIN_FLOOR's whole reason
    # to exist is already being the worst-case answer for exactly this
    # situation. LOSS_MAKER_MARGIN_FLOOR itself is now NEVER discounted
    # further by leverage; only the (usually small) EXCESS above it is,
    # so revenue_based_eps can never fall below what every other
    # under-margin company already gets as its own baseline, regardless of
    # how extreme net_debt_to_revenue is.
    # Guarded on growth_margin >= LOSS_MAKER_MARGIN_FLOOR: a genuinely
    # thin-but-real PROFITABLE margin below that floor (the profitable
    # branch above never floors at LOSS_MAKER_MARGIN_FLOOR, only the
    # loss-maker branch does) gets the plain leverage discount instead --
    # anchoring to a floor that was never claimed as ITS OWN worst-case
    # baseline would incorrectly inflate a real, already-low margin.
    if growth_margin is not None and growth_margin >= LOSS_MAKER_MARGIN_FLOOR:
        effective_margin = LOSS_MAKER_MARGIN_FLOOR + (growth_margin - LOSS_MAKER_MARGIN_FLOOR) * leverage_penalty
    elif growth_margin is not None:
        effective_margin = growth_margin * leverage_penalty
    else:
        effective_margin = None
    revenue_based_eps = (
        revenue_per_share * effective_margin
        if revenue_per_share is not None and revenue_per_share > 0
        and effective_margin is not None and effective_margin > 0
        else None
    )
    # current_year_eps, when trusted at face value, can be a genuine but
    # non-representative outlier -- a one-off mark-to-market swing (BKKT:
    # epsCurrentYear=$2.26 on volatile crypto-custody quarterly EPS of
    # +1.13/-2.16/-1.15/-0.41/+1.94). Tempered against trailingEps (when
    # available) at the same REAL_BASE_BLEND_WEIGHT used for the
    # own-growth/direct-eps blend below, rather than compounding an
    # outlier untouched for 8 years -- NOT against forwardEps: fwd_eps is
    # deliberately kept OUT of real_base entirely now (see direct_eps_1
    # below), so it only ever enters the EPS path once. Confirmed live,
    # SNDK: real_base blended with forwardEps ($264.18) made forwardEps's
    # effective weight in eps_1 scale with own_growth_rate_1 instead of
    # staying flat -- at own_growth_rate_1=100% (GROWTH_CAP, pinned there
    # by a corrupted revenueGrowth=371.6%), forwardEps ended up
    # contributing to eps_1 at ~2x its intended weight (once flat via
    # direct_eps_1, once AGAIN inside real_base before being grown by a
    # full extra year of capped growth), landing eps_1~$371 against a
    # forwardEps of just $264. Algebra: eps_1 = fwdEps*(0.75 +
    # 0.25*own_growth_rate_1) + currentYearEps*(0.25 + 0.25*
    # own_growth_rate_1) when real_base blended fwdEps in -- fwdEps's
    # coefficient rising with own_growth_rate_1 rather than staying at a
    # flat 0.5 is exactly the double-count.
    # trailing_eps only trusted as real_base's blend partner when it's
    # self-consistent with current_year_eps -- same FWD_TRAILING_PE_RATIO_*
    # band already used for the forwardPE/trailingPE swap above, reused
    # here for the same reason: a TTM figure can still be a genuine
    # outlier the reconcile_trailing_eps 6-sigma guard didn't (or
    # couldn't, with too little quarterly history) catch. Confirmed live,
    # VISN: even after that guard's fix, trailingEps ($4.42) still sat
    # ~5.2x above current_year_eps ($0.85) and forwardEps ($0.70) --
    # both independently agreeing with each other -- blending it in
    # anyway would still inflate real_base well past what two agreeing
    # signals suggest is the real run-rate. Falls back to plain
    # current_year_eps alone when the check fails, same as when
    # trailing_eps is missing entirely.
    if current_year_eps is not None and current_year_eps > 0:
        trailing_eps_consistent = (
            trailing_eps is not None and trailing_eps > 0
            and FWD_TRAILING_PE_RATIO_MIN <= trailing_eps / current_year_eps <= FWD_TRAILING_PE_RATIO_MAX
        )
        real_base = (
            REAL_BASE_BLEND_WEIGHT * current_year_eps + (1.0 - REAL_BASE_BLEND_WEIGHT) * trailing_eps
            if trailing_eps_consistent else current_year_eps
        )
    elif trailing_eps is not None and trailing_eps > 0:
        real_base = trailing_eps
    # forwardEps inserted ahead of revenue_based_eps -- explicit fix,
    # FWRD: current_year_eps negative, trailing_eps missing, so real_base
    # fell all the way to revenue_based_eps ($3.60, a synthetic
    # revenue-times-margin figure) even though a perfectly good, credible
    # forwardEps ($0.333) existed. revenue_based_eps is now a true last
    # resort -- no positive EPS signal anywhere, not even forward.
    elif fwd_eps is not None and fwd_eps > 0:
        real_base = fwd_eps
    else:
        real_base = revenue_based_eps
    # Years 1-2's own-growth leg now converges toward industryGrowthRate on
    # the SAME concave w_t schedule years 3-4 already use -- explicit
    # instruction: "year 1 and year 2 must always show convergence to
    # industryGrowthRate, that is the philosophy behind, valid also for
    # fallbacks." Previously ONLY year 2 got any industry pull
    # (own_growth_rate_y2 below); year 1 compounded the raw, untempered
    # ownGrowthRate outright. w1 is the same formula one step earlier than
    # w2's own (already-existing) sqrt((n_steps-2)/n_steps).
    w1 = math.sqrt((n_steps - 1) / n_steps)
    own_growth_rate_1 = w1 * own_growth_rate + (1.0 - w1) * convergence_growth_rate
    own_growth_level_1 = real_base * (1.0 + own_growth_rate_1) if real_base is not None else None

    # direct_eps_1 falls back to revenue_based_eps too when forwardEps
    # itself is non-positive -- explicit instruction, same "no positive
    # real EPS anywhere" gap real_base's own revenue fallback addresses.
    # A negative forwardEps for a real, revenue-scaling growth company
    # (GSAT/RKLB/RARE-style) still isn't a good near-term anchor even
    # though it's genuine analyst consensus -- it just means analysts
    # expect a loss, which revenue_based_eps's own "credible eventual
    # margin at today's revenue" framing already answers more usefully
    # than compounding a negative number would. Only engages when
    # forwardEps itself can't do the job; a positive forwardEps (even a
    # small one) is left untouched, still the real analyst figure.
    direct_eps_1 = fwd_eps if fwd_eps is not None and fwd_eps > 0 else (
        revenue_based_eps if revenue_based_eps is not None else fwd_eps
    )
    target_mean_price = to_float(row.get("targetMeanPrice"))
    n_analysts = to_float(row.get("numberOfAnalystOpinions"))
    # Gated on own_pe_consistent (not just "own_pe is truthy") -- same
    # consistency check mu_pe's own ownPE usage already requires. Confirmed
    # live: MSTR's ownPe=2.65 disagrees with what price/forwardEps itself
    # implies (9.18, a real mismatch -- ownPe likely reflects a different,
    # stale EPS basis given MSTR's huge bitcoin-driven earnings swings),
    # and mu_pe already distrusts it correctly (falls back to pure
    # industryPe) -- but this target-price blend used the SAME broken
    # ownPe unguarded, dividing targetMeanPrice by 2.65 inflated
    # target_implied_eps_1 enough to drag direct_eps_1 up to $42.75 against
    # a real forwardEps of just $14.27.
    if target_mean_price and own_pe_consistent and n_analysts:
        target_implied_eps_1 = target_mean_price / own_pe
        consensus_score = to_float(row.get("analystConsensus"))
        consensus_weight = TARGET_BLEND_WEIGHT_PER_CONSENSUS * abs(consensus_score) if consensus_score is not None else 0.0
        target_weight = min(
            TARGET_BLEND_WEIGHT_PER_ANALYST * n_analysts + consensus_weight, TARGET_BLEND_WEIGHT_MAX
        )
        direct_eps_1 = (1.0 - target_weight) * direct_eps_1 + target_weight * target_implied_eps_1

    if own_growth_level_1 is not None:
        eps_1 = REAL_BASE_BLEND_WEIGHT * own_growth_level_1 + (1.0 - REAL_BASE_BLEND_WEIGHT) * direct_eps_1
    else:
        eps_1 = direct_eps_1
    eps_path.append(max(eps_1, 0.0))

    # Year 2's own-growth leg: fix for a bug where an extreme (possibly
    # bad-data-driven -- e.g. SNDK's corrupted revenueGrowth pinning
    # own_growth_rate at GROWTH_CAP) rate got applied TWICE in a row at
    # full, undiscounted strength (own_growth_level_1 -> own_growth_level_2,
    # squaring the distortion) before years 3+'s own concave reversion
    # schedule ever got a chance to fade it. Confirmed live: SNDK's
    # own_growth_rate=100% compounded twice off a real epsCurrentYear=214
    # gave own_growth_level_2=856 -- more than 3x its own real
    # eulerFwdEps2y=276 -- dragging a perfectly sane direct_eps_2 up with
    # it at a flat 50/50 blend. Two combined fixes (explicit instruction,
    # weighted toward the first, sharing ONE decay factor rather than
    # compounding two independently-tuned ones):
    #   1. (primary) Reuse the SAME concave w_t = sqrt((n_steps-t)/n_steps)
    #      schedule years 3+ already use, one step early (t=2), to fade
    #      own_growth_rate toward convergence_growth_rate BEFORE the second
    #      compounding step -- own_growth_rate stays full-strength for
    #      year 1, but only partially trusted for year 2's OWN growth
    #      application, matching the "own signal decays fast" philosophy
    #      already used everywhere else in this path.
    #   2. (secondary, softer) REAL_BASE_BLEND_WEIGHT itself also decays
    #      for year 2's blend, reusing the SAME w2 (not a second schedule)
    #      but only HALFWAY (averaged with 1.0), so it leans much less on
    #      the fix than #1 does.
    w2 = math.sqrt((n_steps - 2) / n_steps)
    own_growth_rate_y2 = w2 * own_growth_rate + (1.0 - w2) * convergence_growth_rate
    own_growth_level_2 = own_growth_level_1 * (1.0 + own_growth_rate_y2) if own_growth_level_1 is not None else None
    blend_weight_y2 = REAL_BASE_BLEND_WEIGHT * (0.5 + 0.5 * w2)

    # direct_eps_2: rebuilt on the SAME shape as direct_eps_1 (a real base
    # compounded by a growth RATE) instead of trusting Eulerpool's absolute
    # eulerFwdEps2y point estimate outright -- explicit instruction. Before
    # this, year 2's entire "direct" leg was single-sourced from one
    # Eulerpool EPS-level figure with no independent corroboration (unlike
    # year 1, which blends forwardEps toward the analyst target price, a
    # SEPARATE source). Now it's built from eulerRevGrowth2y (Eulerpool's
    # own 2-year-out REVENUE growth), margin-converted the same way
    # marginAdjustedRevenueGrowth is (growth_margin), and -- explicit
    # instruction -- already blended toward industryGrowthRate at year 2
    # via the SAME w2 concave weight used above, rather than waiting until
    # year 3 to start any industry convergence at all.
    #
    # When eulerRevGrowth2y is missing (no Eulerpool coverage at all),
    # directEps_2 now falls back to directEps_1 grown at industryGrowthRate
    # -- explicit instruction, the SAME "years 1-2 must always show
    # convergence, valid also for fallbacks" rule above -- instead of
    # leaving directEps_2 as None, which used to drop year 2 to ZERO
    # tempering (100% raw ownGrowthLevel_2, no direct-estimate counterweight
    # at all). Confirmed live: NAT (no Eulerpool coverage, ownGrowthRate
    # pinned near 80% from a real tanker-rate upcycle) jumped +154.5% from
    # year 1 to year 2 under the old untempered fallback, then decelerated
    # HARD the following year -- a spike-then-taper shape, not the smooth
    # taper the schedule is supposed to produce. euler_growth_rate_2y is
    # set to industryGrowthRate in this branch too, so
    # own_growth_rate_realized2 below needs no separate no-Eulerpool case
    # any more -- directEps_2's own implied rate is always defined now.
    if euler_rev_growth2y is not None:
        euler_rev_growth2y_adj = euler_rev_growth2y * growth_margin if growth_margin is not None else euler_rev_growth2y
        euler_growth_rate_2y = max(
            GROWTH_FLOOR, min(GROWTH_CAP, w2 * euler_rev_growth2y_adj + (1.0 - w2) * convergence_growth_rate)
        )
    else:
        euler_growth_rate_2y = convergence_growth_rate
    direct_eps_2 = direct_eps_1 * (1.0 + euler_growth_rate_2y)

    # own_growth_rate_realized2: the rate years 3+ continue from, derived
    # from the two COMPONENT rates (own_growth_rate_y2, euler_growth_rate_2y)
    # weighted the SAME way eps_2 itself is blended -- NOT from
    # eps_2/eps_1 - 1 (a ratio of blended LEVELS). The level-ratio version
    # conflates two different effects: genuine growth AND the composition
    # shift from blend_weight_y2 != REAL_BASE_BLEND_WEIGHT (year 2 leans
    # slightly less on the own leg than year 1 did). Confirmed live: GSAT's
    # own leg grew +3.3% and its direct leg grew +1.0% -- BOTH legs grew --
    # but because the own leg sits ~5x above the tiny direct leg and year
    # 2's blend shifted weight (0.5 -> 0.427) away from the own leg toward
    # the direct leg, the BLENDED eps_2 came out 6.2% BELOW eps_1 even
    # though nothing in the underlying economics declined -- a level-ratio
    # "realized rate" would have fed that artificial -6.2% into years 3-4.
    if own_growth_level_2 is not None and direct_eps_2 is not None:
        eps_2 = blend_weight_y2 * own_growth_level_2 + (1.0 - blend_weight_y2) * direct_eps_2
        own_growth_rate_realized2 = blend_weight_y2 * own_growth_rate_y2 + (1.0 - blend_weight_y2) * euler_growth_rate_2y
    elif direct_eps_2 is not None:
        eps_2 = direct_eps_2
        own_growth_rate_realized2 = euler_growth_rate_2y
    elif own_growth_level_2 is not None:
        eps_2 = own_growth_level_2
        own_growth_rate_realized2 = own_growth_rate_y2
    else:
        # No real base AND no Eulerpool 2-year data at all -- last resort,
        # same "no signal isn't bad news" spirit as _combine_growth's own
        # missing-data fallback.
        eps_2 = eps_1 * (1.0 + convergence_growth_rate)
        own_growth_rate_realized2 = convergence_growth_rate
    # eps_2 can never fall below eps_1 -- explicit instruction (confirmed
    # live via GSAT/HOOD-style cases): a genuine YoY EPS decline is a real
    # possibility for a single ticker, but years 1-2 here are both
    # SHORT-TERM, ANALYST-ANCHORED estimates (forwardEps-based, blended
    # against a real growth extrapolation) -- the blend-weight composition
    # shift between year 1 and year 2 (blend_weight_y2 !=
    # REAL_BASE_BLEND_WEIGHT) can still produce a small apparent decline
    # even when own_growth_rate_realized2 above is itself computed
    # correctly (immune to the WORST of that shift, but not perfectly
    # zero), so this is a hard floor, not just a best-effort fix. When it
    # binds, the realized rate is floored at 0% too, so years 3+ continue
    # from "flat", not from whatever (now-overridden) rate the blend
    # implied.
    if eps_2 < eps_1:
        eps_2 = eps_1
        own_growth_rate_realized2 = max(own_growth_rate_realized2, 0.0)
    eps_path.append(max(eps_2, 0.0))

    # Years 3+: unchanged concave own->industry reversion, now compounding
    # from eps_2 (a real number) instead of a chain seeded at anchor_eps.
    # No margin-trend overlay on years 1-2 above -- a real analyst-
    # consensus EPS already reflects analysts' own margin view, so adding
    # this project's own overlay on top would double-count it; years 3+
    # keep the overlay since they're genuinely this model's own
    # extrapolation, same as before.
    #
    # own_delta_eps2/ind_delta_eps2, scaled by eps_2 (explicit instruction)
    # instead of anchor_eps, for this DETERMINISTIC path. Years 3+ now
    # compound from eps_2, so the overlay's own dollar magnitude needs to
    # match the trajectory it's actually added to -- confirmed live, TEAM's
    # anchor_eps (11.09) sat 48% above its real eps_2 (7.47), so scaling
    # the overlay by anchor_eps made it disproportionately large relative
    # to the path it was being added onto, the same class of "anchor no
    # longer matches the real trajectory" issue mu_eps's own fix addressed.
    # eps_2 is ALWAYS a real number by this point (see its own fallback
    # chain -- worst case it's derived from eps_1, which itself always
    # resolves at least to forwardEps), so no extra guard is needed here.
    # The SIMULATED path below uses its own analogous eps_2_sim-scaled
    # pair (own_delta_eps2_sim/ind_delta_eps2_sim), computed there once
    # eps_2_sim exists.
    own_delta_eps2 = own_margin_delta * eps_2 if own_margin_delta is not None else 0.0
    ind_delta_eps2 = ind_margin_delta * eps_2 if ind_margin_delta is not None else 0.0
    # Years 3+'s own-rate leg: use the REALIZED growth the years-1-2 level
    # blend actually produced (own_growth_rate_realized2, computed above
    # from the two COMPONENT rates, not from eps_2/eps_1 - 1 -- see that
    # computation's own comment for why the level-ratio version is broken),
    # not the raw own_growth_rate scalar. Years 1-2 get moderated by
    # blending against real external anchors (forwardEps/eulerFwdEps2y),
    # which tempers a bad/extreme own_growth_rate reading; reverting to the
    # RAW scalar here would throw that moderation away and let the
    # schedule's own-rate component snap back to full strength -- confirmed
    # live: SNDK's own_growth_rate=100% (pinned by its own corrupted
    # revenueGrowth) got tempered down to a 31.6% REALIZED year1->year2
    # growth by the blend, but the old code still fed the raw 100% into
    # year 3's schedule, making growth ACCELERATE (56.7%) year-over-year
    # instead of continuing to taper toward industryGrowthRate the way the
    # schedule is supposed to.
    own_growth_rate_realized = max(GROWTH_FLOOR, min(GROWTH_CAP, own_growth_rate_realized2))
    growth_path = eps_2
    for t in range(3, EPS_PROJECTION_YEARS):
        w = math.sqrt((n_steps - t) / n_steps) if t < n_steps else 0.0
        g_t = max(GROWTH_FLOOR, min(GROWTH_CAP, w * own_growth_rate_realized + (1.0 - w) * convergence_growth_rate))
        growth_path = growth_path * (1.0 + g_t)
        # Fading additive margin-trend overlay: NOT cumulative (a single
        # term per year), bounded by own_delta_eps2, -> ind_delta_eps2
        # by year N (w = 0).
        md_t = w * own_delta_eps2 + (1.0 - w) * ind_delta_eps2
        # Same zero floor years 1-2 already have (max(eps_1, 0.0) /
        # max(eps_2, 0.0)) -- explicit instruction. growth_path itself
        # (the pure-growth compounding, no overlay) can't go negative on
        # its own (GROWTH_FLOOR clips every (1+g_t) to >= 0.01), so this
        # only ever catches the ADDITIVE margin overlay pushing a
        # near-zero growth_path below zero -- confirmed live: S
        # (SentinelOne, both forwardEps and currentYearEps negative, so
        # eps_1/eps_2 floor to exactly 0) drifted to -$0.60/-$0.64 in
        # years 3-4 purely from a negative margin-trend overlay term, with
        # nothing stopping it the way years 1-2 already are protected.
        eps_path.append(max(growth_path + md_t, 0.0))

    # mu_eps averages the DISCOUNTED path, not the raw nominal one
    # (explicit instruction: discount everything at a 5% rate) --
    # industryPe is a CURRENT multiple, meant to price a near-term EPS
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
    # DISCOUNT_RATE would otherwise have. beta is screen_data.csv's own
    # column (yfinance's 5-year monthly beta); missing beta falls back to
    # the peer/sector-median beta (see METRIC_KEYS/_build_peer_pools)
    # rather than a flat market-average 1.0 -- confirmed live, CBRS (a
    # recent IPO with too little trading history for yfinance to regress
    # a beta yet) was defaulting to 1.0 regardless of how volatile its
    # actual semiconductor peers run, understating its true systematic
    # risk. Only drops to the flat 1.0 when even the broad sector has no
    # beta to borrow from. Clamped to [BETA_FLOOR, BETA_CAP] either way
    # (own or peer) rather than left at its raw value -- a negative or
    # near-zero beta would flip or collapse the discount rate itself,
    # which isn't a meaningful "less risky than the market" reading so
    # much as a low/negative-correlation artifact; an extreme high beta
    # (e.g. PGY's 5.374) would otherwise let a single noisy input swamp
    # mu_eps's own 5-year discounted EPS path.
    beta = to_float(row.get("beta"))
    industry_beta, _, _ = _peer_median(ticker, industry, *peer_pools["beta"])
    beta_for_discount = beta if beta is not None else (industry_beta if industry_beta is not None else 1.0)
    effective_discount_rate = DISCOUNT_RATE * max(BETA_FLOOR, min(beta_for_discount, BETA_CAP))
    discount_weights = [1.0 / ((1.0 + effective_discount_rate) ** i) for i in range(len(eps_path))]
    discounted_eps_path = [eps_path[i] * discount_weights[i] for i in range(len(eps_path))]
    # Weighted mean by the SAME discount weights, not a plain mean of
    # already-discounted values -- dividing by raw year-count instead of
    # by the weights' own sum systematically understates mu_eps whenever
    # effectiveDiscountRate > 0, even for a FLAT (zero-growth) epsPath:
    # mean(w_i) < 1 for any r > 0, so mu_eps would drop below the real
    # trajectory purely from discounting mechanics, with no earnings
    # signal behind it at all -- confirmed live across 465 near-zero-
    # growth tickers, where forecastReturn dropped monotonically from
    # +0.7% (beta < 0.75) to -12.3% (beta 2-3) despite a flat earnings
    # picture in every case. The weighted mean is the correct annuity-
    # equivalent average: for a flat path it reproduces that flat value
    # EXACTLY regardless of beta, while still weighting near-term years
    # more than distant ones (the discount weights themselves still decay
    # with i).
    #
    # eps_path[0] (anchorEps) is EXCLUDED from this average (explicit
    # instruction) -- it's the one entry that's still an artificial
    # industry-multiple construct (years 1-4 are now real numbers, see
    # REAL_BASE_BLEND_WEIGHT's own comment), and since it carries the
    # LARGEST weight (i=0, undiscounted), including it let a ticker's
    # own-vs-industry multiple gap re-contaminate mu_eps even after years
    # 1-4 stopped depending on that gap. Confirmed live: TEAM (own P/E
    # 28.2x vs. a 17.2x industry median) had anchorEps=11.09 sitting well
    # above every real year of its own path (6.3-8.5), pulling mu_eps up
    # to 8.35 from what it'd be (~7.3) averaging only the real years.
    # anchorEps itself is otherwise unaffected by this exclusion -- still
    # eps_path[0] in the reported array, still the terminal-pricing/
    # discounting-baseline value everywhere else in this function -- only
    # mu_eps's own average drops it.
    mu_eps = sum(discounted_eps_path[1:]) / sum(discount_weights[1:])

    # A revenue-multiple floor (peer-group trailingPS x a grown revenue
    # projection) was tried here and REVERTED -- explicit instruction. It
    # correctly helped GSAT/CIFR/TEM-style asset-light names, but broke
    # badly for genuinely low-margin, high-revenue businesses (WKC, XRX,
    # SNEX, BBUC, SMCI, and several more) whose own P/S sits far BELOW
    # industry for a REAL structural reason (a different margin profile
    # than their peer group, not an undervaluation) -- applying the peer
    # group's richer, higher-margin-implied P/S to their own large revenue
    # produced absurd floors (+4,800% to +17,000% "returns"). A ratio gate
    # (only trust the floor when ownPS isn't far below industryPS) would
    # have fixed that specific failure mode, but the analyst-target floor/
    # cap below is a cleaner, more robust fix for the same underlying
    # problem -- see that computation's own comment.

    # mu_pe: the pricing MULTIPLE now converges gradually from ownPE toward
    # industryPe on the SAME concave w_t schedule the growth rate uses,
    # instead of snapping every year's EPS straight to industryPe from year
    # 1 onward -- explicit instruction. Confirmed live: HOOD trades at
    # ownPE=33.2x vs. a 13.86x industryPe; pricing its year-4 EPS ($3.78)
    # at its OWN multiple gives ~$125, roughly matching its actual $112.57
    # price -- i.e. HOOD's real growth trajectory plausibly DOES justify
    # something close to its own premium multiple, but the old flat-
    # industryPe pricing never gave it any credit for that, immediately
    # and permanently marking every year down to the peer multiple
    # regardless of how much real earnings growth the path shows.
    # own_pe_consistent/own_pe_for_blend already computed above, alongside
    # anchorEps's own (now also ownPE-aware) blend -- reused here as-is.
    #
    # convergence_multiple: same "the reversion target isn't 100% pure
    # peer-group" redefinition as convergence_growth_rate above -- explicit
    # instruction. 90% industryPe, 10% trailingPe (this ticker's own,
    # REAL, currently-observed trailing multiple -- trailing_pe is already
    # a variable in scope, used earlier by the thin-coverage forwardPE/
    # trailingPE swap). Replaces industry_pe ONLY in mu_pe's own blend/
    # fallback below -- NOT anchor_eps (price/industry_pe, a separate
    # concept) and NOT the OWN_PE_BLEND_CAP_MULTIPLE sanity cap above
    # (still anchored to the raw peer-group figure as its sanity
    # reference). trailingPE's own contribution is capped at
    # OWN_PE_BLEND_CAP_MULTIPLE x industryPe too -- same near-zero-EPS-
    # artifact risk as ownPE (see that constant's own comment): confirmed
    # live, TSLA's trailingPE=335.3x (a temporarily near-zero trailing EPS
    # base, not a genuine sustained multiple) would otherwise have dragged
    # convergence_multiple up to 110+ unchecked.
    trailing_pe_for_blend = (
        _log_compress_multiple(trailing_pe, industry_pe, OWN_PE_BLEND_CAP_MULTIPLE)
        if trailing_pe is not None and trailing_pe > 0 and industry_pe is not None and industry_pe > 0 else None
    )
    convergence_multiple = (
        0.9 * industry_pe + 0.1 * trailing_pe_for_blend if trailing_pe_for_blend is not None else industry_pe
    )
    if own_pe_for_blend is not None:
        multiple_sum = 0.0
        for t in range(1, EPS_PROJECTION_YEARS):
            w_pe = math.sqrt((n_steps - t) / n_steps) if t < n_steps else 0.0
            multiple_t = w_pe * own_pe_for_blend + (1.0 - w_pe) * convergence_multiple
            multiple_sum += multiple_t * discount_weights[t]
        mu_pe = multiple_sum / sum(discount_weights[1:])
    else:
        mu_pe = convergence_multiple

    # Simulated-path Monte Carlo (SimPrice) -- explicit instruction:
    # alongside the deterministic base case above (unchanged, still
    # forecastPrice/forecastReturn below), simulate a FULL EPS path per
    # Monte Carlo draw instead of resampling a single terminal EPS around
    # mu_eps the way eps_draws further below still does for
    # priceAtIndustryMultiple/forecastPrice. Every draw uses the EXACT
    # SAME structure as the deterministic loop just above -- same concave
    # reversion toward industry growth over the same EPS_PROJECTION_YEARS
    # horizon, same industry-then-sector peer selection (via
    # _peer_pe_pool_and_cv, the same MIN_INDUSTRY_PEERS/MIN_PEERS gate
    # industryPe/industryGrowthRate already use). Only THREE inputs to
    # that structure are randomized per path -- ownGrowthRate, the
    # reversion exponent p (0.5 in the deterministic case, i.e.
    # w_t = ((N-t)/N)**p reduces to the deterministic sqrt schedule), and
    # g_fwd (year-1 analyst-implied drift) -- all three tied to one shared
    # shock z, below. industryGrowthRate and the terminal multiple are
    # left UNCHANGED from the deterministic case in every path; see each
    # one's own comment just below for why. The deterministic case is
    # this SAME machinery's p=0.5, no-perturbation special case, not a
    # different formula.
    #
    # peer_pe_cv (coefficient of variation of peer trailing P/E within the
    # SAME peer group, trimmed to [P5, P95] -- see _peer_pe_pool_and_cv)
    # is the shared noise scale -- a name in a tightly-clustered-multiple
    # industry gets less randomized spread than one where peers disagree
    # widely on valuation (see GROWTH_NOISE_FLOOR/
    # REVERSION_EXPONENT_MIN/MAX's own comments for the exact scaling).
    #
    # Vectorized across all n draws at once (shape (n,) throughout) rather
    # than a Python loop per draw -- the same array-broadcast pattern
    # eps_draws below already uses for its own single terminal draw, just
    # extended across EPS_PROJECTION_YEARS steps instead of one.
    peer_pe_pool, peer_pe_cv, peer_pe_level = _peer_pe_pool_and_cv(ticker, industry, *peer_pools["trailingPE"])
    # Read once here (needed for the simulated path's per-path g_fwd draw
    # below) and reused again, unchanged, by analyst_dispersion further
    # down -- same three raw fields, no need to read them twice.
    target_low_price = to_float(row.get("targetLowPrice"))
    target_mean_price = to_float(row.get("targetMeanPrice"))
    target_high_price = to_float(row.get("targetHighPrice"))
    # Read once here too (needed by both the combined_vol override below
    # and the per-path g_fwd draw's branch selection further down) -- see
    # MIN_CREDIBLE_ANALYSTS's own comment on why a thin-coverage ticker's
    # OWN range/dispersion isn't trusted at either use site.
    n_analysts_for_dispersion = to_float(row.get("numberOfAnalystOpinions"))
    thin_coverage = n_analysts_for_dispersion is None or n_analysts_for_dispersion < MIN_CREDIBLE_ANALYSTS
    # Industry/sector-median analyst_dispersion (see METRIC_KEYS/
    # _build_peer_pools's own comments) -- the fallback g_fwd_draws below
    # uses when THIS ticker has no analyst target range of its own, so
    # its per-path g_fwd still gets a realistic (peer-typical) spread
    # instead of collapsing to a single fixed value for every path (that
    # fixed-value fallback silently dropped one of the three shared-shock
    # inputs' worth of variance -- confirmed live: COKE, with no analyst
    # coverage, had simReturnVol collapse to 2.3% and simSharpe blow up
    # to 18.2 as a direct result).
    industry_analyst_dispersion, _, _ = _peer_median(ticker, industry, *peer_pools["analystDispersion"])
    # Industry/sector-median epsVolatility -- the fallback eps_vol below
    # uses in place of the flat FALLBACK_EPS_REL_STDEV when THIS ticker
    # has no epsVolatility of its own on file (see that assignment's own
    # comment on why a flat floor understates a genuinely volatile
    # foreign-issuer name like TSM, which has no SEC XBRL data at all to
    # compute its own reading from).
    industry_eps_vol, _, _ = _peer_median(ticker, industry, *peer_pools["epsVolatility"])

    # --- EPS-uncertainty scale (epsVolatility (+) analyst price-target
    # dispersion, RSS) -------------------------------------------------------
    # Computed HERE, before the simulated-path block, because that block now
    # drives its per-path EPS-side noise from combined_vol too -- not just
    # analyst dispersion + peer_pe_cv. Keeps the Monte Carlo's spread
    # consistent with the single number forecastPrice's own confidence uses,
    # so a volatile-earnings name (memory, autos, other deep cyclicals)
    # widens its simulated distribution instead of looking deceptively
    # tight and handing simSharpe an overstated risk-adjusted rank.
    eps_vol = to_float(row.get("epsVolatility"))
    if eps_vol is None:
        # Peer-typical reading preferred over the flat floor when THIS
        # ticker has no epsVolatility of its own -- confirmed live: TSM
        # (a Form 20-F filer, no SEC XBRL data at all) was defaulting to
        # the same 20% floor as any undocumented ticker regardless of
        # actual peer volatility, reading as artificially "safe" purely
        # from a data gap rather than a measured low-risk reading. Still
        # floored at FALLBACK_EPS_REL_STDEV as the last resort when even
        # the broad sector has no epsVolatility to borrow from.
        if industry_eps_vol is not None:
            eps_vol = max(industry_eps_vol, FALLBACK_EPS_REL_STDEV)
            eps_vol_source = "industry median (no epsVolatility on file)"
        else:
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

    # Combine historical EPS volatility with analyst price-target dispersion
    # as independent uncertainty sources (RSS): dispersion = half-range / mean
    # of analyst price targets, already in the same relative-% space as eps_vol.
    # target_mean_price/target_high_price/target_low_price were already read
    # just above.
    if (target_mean_price is not None and target_mean_price > 0
            and target_high_price is not None and target_low_price is not None):
        analyst_dispersion = (target_high_price - target_low_price) / (2.0 * target_mean_price)
        analyst_dispersion = max(0.0, analyst_dispersion)
    else:
        analyst_dispersion = None
    # Thin coverage (< MIN_CREDIBLE_ANALYSTS) -- substitute the peer-
    # typical dispersion for THIS ticker's own (own value is either
    # missing, or too small a sample to trust as real measured
    # disagreement) rather than letting a falsely-tight own value pull
    # combined_vol down. Only overrides when a peer figure is actually
    # available; a thin-AND-thinly-covered-sector ticker keeps whatever
    # own value (or None) it already had.
    if thin_coverage and industry_analyst_dispersion is not None:
        analyst_dispersion = industry_analyst_dispersion
    combined_vol = (math.sqrt(eps_vol ** 2 + analyst_dispersion ** 2)
                    if analyst_dispersion is not None else eps_vol)
    sigma_eps = combined_vol * abs(mu_eps)
    confidence = 1.0 / (1.0 + combined_vol)

    sim_price = None
    sim_return = None
    sim_sharpe = None
    sim_return_vol = None
    stats_sim = None
    # Explicit correction: the multiple does NOT get randomized, only the
    # EPS side does -- every simulated path prices at the SAME fixed
    # industry_pe the deterministic case uses (industryMedianPe), not a
    # per-path draw. An earlier version randomized the terminal P/E too
    # (bootstrap, then rank-matched to the shared shock below) -- dropped
    # because multiplying by a right-skewed random multiple pulls
    # SimPrice's MEAN well above its median purely from the multiple's own
    # shape, regardless of how well-behaved the EPS side is (confirmed
    # live: NVDA's peer trailing-P/E pool alone, even trimmed, still runs
    # 18x-459x -- real peer valuations, but multiplying by a draw from
    # that shape is a different question than earnings uncertainty).
    # peer_pe_cv (still computed from that SAME peer trailing-P/E pool) is
    # kept as the noise-SCALE for the EPS-side randomization below, just
    # no longer used to draw the multiple itself.
    if peer_pe_cv is not None and industry_pe is not None and industry_pe > 0:
        # One shared shock per path -- standard Normal, clipped to
        # +/-SHOCK_CLIP_SD -- ties ownGrowthRate and reversion-speed
        # together for every path instead of drawing each independently
        # (see this module's own SHOCK_CLIP_SD comment for why).
        # industryGrowthRate stays the plain unshocked scalar --
        # deliberately not perturbed by z at all, which is what makes the
        # paths properly mean-reverting (see that same comment).
        z = np.clip(rng.normal(0.0, 1.0, n), -SHOCK_CLIP_SD, SHOCK_CLIP_SD)

        # ownGrowthRate spread is scaled by combined_vol (epsVolatility RSS
        # analyst price-target dispersion, computed above) -- the SAME
        # uncertainty measure forecastPrice's confidence uses -- rather than
        # peer_pe_cv (kept below purely as the reversion-SPEED spread). This
        # is what lets a volatile-earnings name widen its simulated
        # distribution instead of inheriting only the peer-valuation spread.
        own_growth_sigma = combined_vol * max(abs(own_growth_rate), GROWTH_NOISE_FLOOR)
        own_growth_draws = own_growth_rate + z * own_growth_sigma
        # Clamped copy for direct use as a per-path growth RATE (years 1-2's
        # real-base level blend below, which multiplies this in TWICE --
        # once for eps_1_sim, again for eps_2_sim). own_growth_draws itself
        # stays unclamped for the years-3+ blend below, which clips the
        # BLENDED rate (g_t_sim) instead -- same as before this change.
        # Without this, an extreme tail path (high own_growth_sigma names
        # like BABA/STNE/PDD) could compound two uncapped, unrelated draws
        # back-to-back and blow up simPrice by 10x+ -- confirmed live.
        own_growth_draws_clamped = np.clip(own_growth_draws, GROWTH_FLOOR, GROWTH_CAP)

        # epsVolatility's own contribution to the year-1 drift spread, in the
        # per-SD units g_fwd's sigma_low/sigma_high/sigma_industry are in --
        # RSS'd into each of them below so the year-1 draw widens for a
        # volatile-earnings name even when its analyst target range is tight
        # (or, in the final branch, absent).
        eps_growth_sigma = eps_vol / SHOCK_CLIP_SD

        # Per-path direct-estimate draw for year 1 -- centered and scaled
        # entirely on forwardEps, NOT anchorEps. The OLD version
        # multiplied by anchor_eps (direct_eps_1_sim = anchor_eps*(1+
        # g_fwd_draws)) -- at z=0 this happened to equal forwardEps
        # exactly (an algebraic identity: the old g_fwd was DEFINED as
        # forwardEps/anchorEps - 1), but for z != 0 the per-path DOLLAR
        # spread scaled with anchor_eps's own magnitude, not forwardEps's
        # -- confirmed live, MU's anchorEps=$34 vs. a real forwardEps of
        # $140-180 meant this understated year 1's uncertainty band by
        # ~4-5x purely from using the wrong scale. It also silently mixed
        # bases: the old g_fwd_low/g_fwd_high were PRICE-return rates
        # (targetPrice/currentPrice - 1) while the old g_fwd center was an
        # ANCHOR-relative EPS rate (forwardEps/anchorEps - 1) -- treated
        # as the same kind of quantity when they aren't. Fixed by
        # converting the analyst target prices to EPS space via ownPE (the
        # ticker's own REAL, validated multiple -- the SAME source the
        # deterministic path's own directEps_1 target blend already uses,
        # gated on own_pe_consistent so a broken ownPE can't reintroduce a
        # different distortion), then expressing the spread relative to
        # forwardEps throughout, centered at 0 (forwardEps IS the center;
        # no separate drift term needed, unlike the old g_fwd-centered
        # version). g_fwd_i is a linear function of a Normal variable (z),
        # so it's itself Normally distributed -- a split-normal, since
        # sigma_low/sigma_high are generally different (analyst target
        # ranges are rarely symmetric): z == -SHOCK_CLIP_SD lands exactly
        # on the low target's implied EPS, z == 0 exactly on forwardEps,
        # z == +SHOCK_CLIP_SD exactly on the high target's, continuous in
        # between.
        #
        # When THIS ticker has no analyst target range of its own, OR too
        # few analysts to trust the one it has as real disagreement (see
        # MIN_CREDIBLE_ANALYSTS's own comment -- a 1-analyst "range" is
        # degenerate, not tight), OR ownPE itself isn't trustworthy enough
        # to convert price targets to EPS space, falls back on the
        # industry/sector-median analyst_dispersion (relative half-width
        # of PEERS' own target ranges, see industry_analyst_dispersion
        # above) as a symmetric spread around 0 -- still tied to the same
        # shared shock z, not a single fixed value for every path -- and
        # only drops to zero spread (a single fixed forwardEps, no
        # per-path variation) when even the broad sector has no analyst
        # coverage to borrow a typical spread from.
        if (
            target_low_price is not None and target_high_price is not None
            and not thin_coverage and own_pe_consistent and fwd_eps is not None and fwd_eps > 0
        ):
            target_low_eps = target_low_price / own_pe
            target_high_eps = target_high_price / own_pe
            g_fwd_low = max(GROWTH_FLOOR, min(GROWTH_CAP, target_low_eps / fwd_eps - 1.0))
            g_fwd_high = max(GROWTH_FLOOR, min(GROWTH_CAP, target_high_eps / fwd_eps - 1.0))
            # Clamped at 0 -- a data anomaly (e.g. the low target implies
            # MORE EPS than forwardEps itself) should mean "no extra
            # spread on this side," never a sign flip that would run the
            # interpolation backwards.
            sigma_low = math.hypot(max(0.0, -g_fwd_low) / SHOCK_CLIP_SD, eps_growth_sigma)
            sigma_high = math.hypot(max(0.0, g_fwd_high) / SHOCK_CLIP_SD, eps_growth_sigma)
            g_fwd_draws = np.clip(np.where(z < 0, z * sigma_low, z * sigma_high), GROWTH_FLOOR, GROWTH_CAP)
        elif industry_analyst_dispersion is not None:
            # No real low/high skew to draw from for THIS ticker (or ownPE
            # isn't trustworthy enough to convert price targets to EPS
            # space), just a typical peer WIDTH -- symmetric around 0
            # (forwardEps), same reasoning as above.
            sigma_industry = math.hypot(industry_analyst_dispersion / SHOCK_CLIP_SD, eps_growth_sigma)
            g_fwd_draws = np.clip(z * sigma_industry, GROWTH_FLOOR, GROWTH_CAP)
        else:
            # No analyst target range for this ticker AND no peer spread to
            # borrow -- epsVolatility alone still gives year 1 a real
            # per-path spread instead of collapsing to a single fixed
            # forwardEps.
            g_fwd_draws = np.clip(z * eps_growth_sigma, GROWTH_FLOOR, GROWTH_CAP)
        p_draws = np.clip(0.5 + z * (peer_pe_cv * 0.5), REVERSION_EXPONENT_MIN, REVERSION_EXPONENT_MAX)

        # Years 1-2: real-base LEVEL blend per path, mirroring the
        # deterministic eps_1/eps_2 above (see REAL_BASE_BLEND_WEIGHT's own
        # comment) instead of compounding growth off anchor_eps.
        # direct_eps_1_sim is centered and scaled entirely on forwardEps
        # (see g_fwd_draws' own comment above) for a POSITIVE forwardEps --
        # no anchor_eps involvement in that case. When forwardEps itself is
        # non-positive, falls back to revenue_based_eps (ALWAYS positive
        # when available -- see that computation's own comment) as the
        # multiplicative base instead -- same "no positive real EPS
        # anywhere" gap direct_eps_1's own revenue fallback addresses,
        # mirrored here for the simulated path. g_fwd_draws itself is
        # already a plain-zero-centered, sign-independent draw in this
        # branch (the target-range branch above requires forwardEps > 0
        # to even engage), so reusing it here is safe regardless of which
        # base is used. Only when NEITHER forwardEps nor revenue_based_eps
        # is usable does this fall back to a DOLLAR (additive) perturbation
        # around forwardEps, scaled by anchor_eps purely as a reference
        # magnitude -- a percentage growth rate isn't meaningful off a
        # negative/zero base (same reasoning every other "prev <= 0" guard
        # in this project already applies -- e.g. modules.derive._yoy).
        # Confirmed live: RARE (forwardEps=-$0.25) collapsed its ENTIRE
        # simulated distribution to exactly $0 (simReturn=-100%) under the
        # plain multiplicative formula before revenue_based_eps existed --
        # multiplying a negative base by (1+shock) inverts the sign
        # relationship, and every path that went more negative got floored
        # to 0 by the eps>=0 guard further down, including the z=0 center
        # itself.
        if fwd_eps is not None and fwd_eps > 0:
            direct_eps_1_sim = fwd_eps * (1.0 + g_fwd_draws)
        elif revenue_based_eps is not None:
            direct_eps_1_sim = revenue_based_eps * (1.0 + g_fwd_draws)
        else:
            direct_eps_1_sim = fwd_eps + anchor_eps * g_fwd_draws
        # own_growth_draws_y1: same w1 tempering as the deterministic
        # own_growth_rate_1 above -- explicit instruction, years 1-2 must
        # always show convergence toward industryGrowthRate, per path here
        # too.
        own_growth_draws_y1 = w1 * own_growth_draws_clamped + (1.0 - w1) * convergence_growth_rate
        if real_base is not None:
            own_growth_level_1_sim = real_base * (1.0 + own_growth_draws_y1)
            eps_1_sim = (
                REAL_BASE_BLEND_WEIGHT * own_growth_level_1_sim
                + (1.0 - REAL_BASE_BLEND_WEIGHT) * direct_eps_1_sim
            )
        else:
            own_growth_level_1_sim = None
            eps_1_sim = direct_eps_1_sim

        # Year 2: same shape. Eulerpool doesn't publish a high/low range for
        # the 2-year-out estimate the way analyst price targets do for year
        # 1, so g_fwd2_draws reuses the SAME shared shock z and
        # eps_growth_sigma (epsVolatility-based spread) g_fwd_draws' own
        # no-target-range fallback branch already uses -- centered on
        # g_fwd2_center so fwd_eps*(1+g_fwd2_draws) lands exactly on
        # direct_eps_2 (the deterministic path's own year-2 direct
        # estimate, ALWAYS populated now -- see that computation's own
        # comment on the industryGrowthRate fallback when Eulerpool has no
        # 2-year data) at the shock's center.
        if direct_eps_2 is not None:
            g_fwd2_center = (direct_eps_2 / fwd_eps - 1.0) if abs(fwd_eps) > 1e-9 else 0.0
            g_fwd2_draws = np.clip(g_fwd2_center + z * eps_growth_sigma, GROWTH_FLOOR, GROWTH_CAP)
            direct_eps_2_sim = fwd_eps * (1.0 + g_fwd2_draws)
        else:
            direct_eps_2_sim = None

        # Same year-2 own-rate decay + softened blend-weight decay as the
        # deterministic eps_2 above (w2/own_growth_rate_y2/blend_weight_y2
        # are shared scalars, already computed there) -- guards this
        # per-path chain against the same squared-extreme-rate bug.
        own_growth_draws_y2 = w2 * own_growth_draws_clamped + (1.0 - w2) * convergence_growth_rate
        own_growth_level_2_sim = (
            own_growth_level_1_sim * (1.0 + own_growth_draws_y2) if own_growth_level_1_sim is not None else None
        )

        if own_growth_level_2_sim is not None and direct_eps_2_sim is not None:
            eps_2_sim = (
                blend_weight_y2 * own_growth_level_2_sim
                + (1.0 - blend_weight_y2) * direct_eps_2_sim
            )
        elif direct_eps_2_sim is not None:
            eps_2_sim = direct_eps_2_sim
        elif own_growth_level_2_sim is not None:
            eps_2_sim = own_growth_level_2_sim
        else:
            eps_2_sim = eps_1_sim * (1.0 + convergence_growth_rate)

        eps_1_sim = np.maximum(eps_1_sim, 0.0)
        eps_2_sim = np.maximum(eps_2_sim, 0.0)
        # eps_2_sim can never fall below eps_1_sim -- same hard floor as the
        # deterministic eps_2 above, per path (see that computation's own
        # comment). eps_2_floored_mask feeds into own_growth_rate_realized_
        # sim below, so a floored path continues years 3+ from "flat", not
        # from whatever (now-overridden) rate the blend implied.
        eps_2_floored_mask = eps_2_sim < eps_1_sim
        eps_2_sim = np.maximum(eps_2_sim, eps_1_sim)

        # mu_eps_sim's own average EXCLUDES year 0 (anchor_eps) -- same fix
        # as the deterministic mu_eps above -- and now starts directly from
        # the real-base years 1-2 computed above.
        discounted_sum_sim = eps_1_sim * discount_weights[1] + eps_2_sim * discount_weights[2]
        weight_sum_sim = discount_weights[1] + discount_weights[2]

        # Years 3+: same concave own->industry growth-rate reversion as
        # before, now compounding from eps_2_sim (real per-path levels)
        # instead of a chain seeded at anchor_eps. Margin-trend overlay is
        # scaled by eps_2_sim (per-path array), not anchor_eps, mirroring
        # the deterministic own_delta_eps2/ind_delta_eps2 fix -- see that
        # computation's own comment.
        # Same fix as the deterministic own_growth_rate_realized above: use
        # the REALIZED per-path growth the years-1-2 level blend actually
        # produced, not the raw own_growth_draws array, so years 3+ don't
        # snap back to full-strength trust in a possibly-extreme raw draw
        # right after the level blend just tempered it down. Derived from
        # the two COMPONENT rates (own_growth_draws_y2, g_fwd2_draws),
        # weighted the SAME way eps_2_sim itself was blended -- NOT from
        # eps_2_sim/eps_1_sim - 1 (a ratio of blended LEVELS), which
        # conflates genuine growth with the composition shift from
        # blend_weight_y2 != REAL_BASE_BLEND_WEIGHT -- see the
        # deterministic own_growth_rate_realized2's own comment (GSAT: both
        # legs individually grew, but the level-ratio version read an
        # artificial decline purely from the weight shift).
        if own_growth_level_2_sim is not None and direct_eps_2_sim is not None:
            own_growth_rate_realized_sim = (
                blend_weight_y2 * own_growth_draws_y2 + (1.0 - blend_weight_y2) * g_fwd2_draws
            )
        elif direct_eps_2_sim is not None:
            own_growth_rate_realized_sim = g_fwd2_draws
        elif own_growth_level_2_sim is not None:
            own_growth_rate_realized_sim = own_growth_draws_y2
        else:
            own_growth_rate_realized_sim = np.full(n, convergence_growth_rate)
        own_growth_rate_realized_sim = np.where(
            eps_2_floored_mask, np.maximum(own_growth_rate_realized_sim, 0.0), own_growth_rate_realized_sim
        )
        own_growth_rate_realized_sim = np.clip(own_growth_rate_realized_sim, GROWTH_FLOOR, GROWTH_CAP)
        growth_path_sim = eps_2_sim
        own_delta_eps2_sim = own_margin_delta * eps_2_sim if own_margin_delta is not None else np.zeros(n)
        ind_delta_eps2_sim = ind_margin_delta * eps_2_sim if ind_margin_delta is not None else np.zeros(n)
        for t in range(3, EPS_PROJECTION_YEARS):
            # (n_steps - t)/n_steps is exactly 0 at t == n_steps (any
            # positive power of 0 is still 0, p_draws is always positive
            # via the REVERSION_EXPONENT_MIN/MAX clip), so this reproduces
            # the deterministic loop's explicit "w = 0 at t == n_steps"
            # branch without needing a separate guard. convergence_growth_rate
            # is the plain unshocked scalar (see above), so as w_sim -> 0
            # every path converges to the EXACT SAME g_t regardless of its
            # own shock -- the mean-reversion property.
            w_sim = ((n_steps - t) / n_steps) ** p_draws
            g_t_sim = np.clip(
                w_sim * own_growth_rate_realized_sim + (1.0 - w_sim) * convergence_growth_rate, GROWTH_FLOOR, GROWTH_CAP
            )
            growth_path_sim = growth_path_sim * (1.0 + g_t_sim)
            md_t_sim = w_sim * own_delta_eps2_sim + (1.0 - w_sim) * ind_delta_eps2_sim
            # Same zero floor as the deterministic years 3+ above, per path
            # -- see that computation's own comment.
            eps_path_sim = np.maximum(growth_path_sim + md_t_sim, 0.0)
            discounted_sum_sim = discounted_sum_sim + eps_path_sim * discount_weights[t]
            weight_sum_sim += discount_weights[t]
        mu_eps_sim = discounted_sum_sim / weight_sum_sim
        # Safety floor only -- a path can't actually reach here <= 0 while
        # anchor_eps > 0 (every (1 + g_t) >= 0.01 via the GROWTH_FLOOR clip),
        # which holds for every ticker whose anchor is price / industryPE or
        # a >0-guarded EPS fallback. Kept as a guard for the lone
        # anchor_eps = fwd_eps branch (negative forwardEps).
        mu_eps_sim_floored = np.maximum(mu_eps_sim, 0.0)
        # mu_pe (the same ownPE->industryPe converging multiple the
        # deterministic mu_eps above now uses, see that computation's own
        # comment) -- a plain scalar, shared across every path, same as
        # industry_pe used to be: the multiple itself still isn't
        # randomized, only now it converges by year instead of snapping to
        # industryPe immediately.
        sim_prices = mu_eps_sim_floored * mu_pe
        if book_value_floor is not None:
            sim_prices = np.maximum(sim_prices, book_value_floor)
        # Winsorize at the 5th/95th percentiles before taking ANY moment of
        # the distribution. Multiplicative EPS compounding (eps_path =
        # anchor_eps * prod(1 + g_t)) makes sim_prices lognormal-ish with a
        # fat right tail -- GROWTH_CAP (+100%/yr) still lets EPS run up
        # ~16x over the reverting path while the downside is bounded near 0
        # -- so the raw mean and stdev sit above the body of the
        # distribution, worst on exactly the volatile-earnings names change
        # #1 widened. (This is NOT the eps_draws-style chop at 0 -- see
        # that block below; the PATH prices never cross zero.) Clipping the
        # two tails to the p5/p95 values pulls SimPrice and simReturnVol
        # back onto the typical outcome WITHOUT confidence-shrinking toward
        # current_price the way forecastPrice does. A clip at p5/p95 leaves
        # every percentile from p5 to p95 (the median included) untouched
        # -- only mean/stdev, and the returns derived from this array
        # below, move.
        p5_price, p95_price = np.percentile(sim_prices, [5, 95])
        sim_prices = np.clip(sim_prices, p5_price, p95_price)

        # --- risk-premium multiple haircut (SimPrice / SimReturn /
        # simPriceDistribution ONLY) --------------------------------------
        # A more uncertain earnings stream is priced at a lower multiple.
        # pe_haircut scales the whole simulated price distribution down by
        # RISK_PREMIUM_K * combinedVol (floored) -- so higher uncertainty
        # doesn't just fan SimPrice further from centre, it also marks it
        # down. NOT applied to forecastPrice (its own confidence shrink) or
        # to simSharpe's mean/vol below (which stay on the un-haircut array,
        # keeping risk in the Sharpe denominator only). See RISK_PREMIUM_K.
        _rp_excess = max(combined_vol - RISK_PREMIUM_COMBVOL_BASELINE, 0.0)
        pe_haircut = max(1.0 - RISK_PREMIUM_K * _rp_excess, RISK_PREMIUM_PE_FLOOR)
        sim_prices_hc = sim_prices * pe_haircut
        # book_value_floor re-applied HERE, after the haircut -- a floor is
        # meant to be an ABSOLUTE minimum, not something the risk-premium
        # discount should be allowed to erode further. Confirmed live:
        # GSAT's floor was computed correctly and applied to sim_prices
        # above, but the haircut (~0.82 for GSAT's combinedVol) still
        # dragged the FINAL sim_prices_hc below it, since the floor was
        # only ever enforced on the PRE-haircut array.
        if book_value_floor is not None:
            sim_prices_hc = np.maximum(sim_prices_hc, book_value_floor)
        # Analyst-target floor/cap -- see ANALYST_TARGET_FLOOR_MULTIPLE's
        # own comment. Applied last, after every other adjustment
        # (haircut, book-value floor), as the final sanity bound on the
        # whole distribution.
        if not thin_coverage:
            if target_low_price is not None and target_low_price > 0:
                sim_prices_hc = np.maximum(sim_prices_hc, target_low_price * ANALYST_TARGET_FLOOR_MULTIPLE)
            if target_high_price is not None and target_high_price > 0:
                sim_prices_hc = np.minimum(sim_prices_hc, target_high_price * ANALYST_TARGET_CAP_MULTIPLE)
        stats_sim = _price_stats(sim_prices_hc, current_price)
        # SimPrice -- explicit instruction: instead of the plain mean of
        # the p5/p95-winsorized haircut distribution (which used to be
        # reported with NO pull toward current_price at all, unlike
        # forecastPrice's own confidence blend -- confirmed live, GSAT read
        # forecastReturn=-53% but simReturn=-97% off the SAME underlying
        # EPS estimate, purely from this asymmetry), use the DISTRIBUTION'S
        # OWN spread to self-moderate: the lowest quartile (p25) when the
        # raw mean call is BULLISH (pulls an optimistic call down toward a
        # more conservative reading), the highest quartile (p75) when the
        # raw mean call is BEARISH (pulls a pessimistic call up toward a
        # less extreme reading). A wide, uncertain distribution gets pulled
        # further from its own mean this way (large gap between mean and
        # p25/p75); a narrow, confident one barely moves -- a similar
        # uncertainty-scaled tempering to forecastPrice's confidence blend,
        # but driven by the simulated distribution's own shape rather than
        # a separate confidence formula.
        sim_price = (
            stats_sim["p25"] if stats_sim["mean"] > current_price
            else stats_sim["p75"] if stats_sim["mean"] < current_price
            else stats_sim["mean"]
        )
        sim_return = sim_price / current_price - 1
        # simSharpe -- explicit instruction: a risk-adjusted return built
        # directly from the simulated-path distribution, not the analyst-
        # dispersion/epsVolatility combinedVol forecastPrice's own
        # confidence already uses. sim_returns is the per-path return
        # implied by each simulated (discounted-average-EPS) price against
        # today's price -- the SAME n paths sim_prices/stats_sim already
        # holds, just rescaled from price-level to return-level. Its
        # stdev is this ticker's own simulation-implied volatility;
        # sim_return (mean of the same array) is the numerator, net of
        # SIM_RF -- the standard Sharpe construction, just built from this
        # module's own Monte Carlo output instead of a historical-returns
        # series (which simulate_ticker has no access to for many
        # thinly-traded/newly-listed names anyway).
        sim_returns = sim_prices / current_price - 1.0
        sim_return_vol = float(np.std(sim_returns, ddof=1))
        # simSharpe's numerator is the RAW (un-haircut) winsorized-mean
        # return -- the risk-premium haircut belongs in SimPrice, not here,
        # or it would be counted twice against sim_return_vol below.
        sim_mean_return = float(sim_returns.mean())
        # Modified Sharpe (Israelsen 2005) -- explicit instruction: the
        # plain excess_return/vol formula ranks BACKWARDS once excess
        # return goes negative (dividing by a SMALLER vol makes it MORE
        # negative, i.e. "worse," when a confidently-bad outcome, low vol,
        # is actually less bad than an uncertain one, high vol, with the
        # SAME expected loss). Flips to multiplying by vol in that case
        # instead, restoring the correct direction: for a negative excess
        # return, higher vol now makes the score MORE negative (correctly
        # worse), lower vol LESS negative (correctly less bad).
        excess_return = sim_mean_return - SIM_RF
        if sim_return_vol > 1e-9:
            sim_sharpe = excess_return / sim_return_vol if excess_return >= 0 else excess_return * sim_return_vol
        else:
            sim_sharpe = None

    # No analyst-target-derived floor OR cap. An earlier version capped
    # eps_draws at targetHighPrice's year-1-equivalent, but whenever that
    # analyst-implied bound sat below abs(fwd_eps) (common for a ticker
    # priced far cheaper than its industry peers, e.g. PGY: ownPe 5.3 vs.
    # industryPe 21.3), the max() in eps_cap_y1 fell back to abs(fwd_eps),
    # which scales to EXACTLY mu_eps -- silently clipping the entire upper
    # half of the Normal draw at its own mean. The floor had the SAME
    # construction, on the other side: eps_floor_y1 = min(targetLowPrice/
    # industryPe, abs(fwd_eps)), rescaled by mu_eps/fwd_eps, collapses to
    # EXACTLY mu_eps whenever the min() picks abs(fwd_eps) -- confirmed
    # live: that branch fires for the vast majority of the universe (83%
    # of simulated tickers had it binding on >25% of draws; several,
    # e.g. COST/IEX/SNEX, had epsFloor == muEps to the last decimal,
    # clipping the ENTIRE lower half of the distribution -- for SNEX even
    # the median collapsed to the same clipped value as P5/P25). Removed
    # rather than patched, same reasoning as the cap -- no analyst-target
    # guardrail on either side.
    #
    # The terminal EPS draw is LOGNORMAL (was Normal + a floor at 0). EPS
    # is a multiplicative quantity -- it moves in percent terms and can't
    # cross zero -- so a Normal(mu_eps, combined_vol*|mu_eps|) draw put
    # 10-25% of its mass below zero for a volatile-earnings name
    # (combined_vol 0.8-1.6: SNDK 10%, MU 22%, WDC 26%) and the old
    # max(.,0) then flattened all of it onto a spike at exactly 0, biasing
    # every mean / percentile / probability taken off the array. The
    # lognormal keeps the SAME coefficient of variation (combined_vol) by
    # construction -- sigma_log = sqrt(ln(1 + combined_vol**2)) -- and the
    # SAME median (mu_eps), so forecastPrice (which keys off the median
    # below, not the mean) is essentially unchanged; the sub-zero spike is
    # simply gone and the low tail is a smooth right-skew instead of a
    # clip. mu_eps <= 0 (only reachable via the anchor_eps = fwd_eps
    # branch with a negative forwardEps) has no lognormal form -- fall
    # back to the degenerate all-mu_eps array, which the max(., 0) below
    # then handles exactly as the old code did. To make the draw
    # MEAN-preserving instead of median-preserving (E[eps_draws] = mu_eps,
    # which pulls the median -- and forecastPrice -- DOWN by
    # 1/sqrt(1+combined_vol**2) for a high-vol name), subtract
    # sigma_log**2 / 2 inside the exp.
    if combined_vol > 0 and mu_eps > 0:
        sigma_log = math.sqrt(math.log(1.0 + combined_vol ** 2))
        eps_draws = mu_eps * np.exp(sigma_log * rng.standard_normal(n))
    else:
        sigma_log = 0.0
        eps_draws = np.full(n, mu_eps)
    # No-op now for the lognormal branch (strictly positive); still guards
    # the mu_eps <= 0 degenerate fallback above.
    eps_draws_floored = np.maximum(eps_draws, 0.0)

    # Single pricing scenario: industry median forwardPE only.
    # Confidence pulls the fair-value-today toward current_price -- this
    # IS forecastPrice, full stop. No further one-year-forward shift: an
    # earlier version multiplied by (1+effectiveDiscountRate) here too
    # (on top of already discounting the EPS path itself in step 1),
    # reasoning that a fairly-valued asset's price mechanically drifts up
    # by its cost of equity over the next year. Dropped -- for a
    # high-beta ticker that second application let a beta-sized markup
    # dominate forecastReturn regardless of the earnings view (e.g. MSTR:
    # raw median ~= current_price, i.e. a dead-neutral earnings signal,
    # yet the old forecastReturn was +14.9%, almost entirely
    # beta * DISCOUNT_RATE), and it made forecastPrice describe a
    # DIFFERENT horizon (12 months out) than priceAtIndustryMultiple's own
    # probAboveCurrentPrice (today), which never got that same shift.
    # forecast_price_p5/p25/p75/p95 apply the SAME transform to
    # priceAtIndustryMultiple's own percentiles -- an "adjusted" band
    # around forecastPrice, on forecastPrice's own confidence-weighted
    # scale, rather than the raw simulated distribution's unadjusted
    # (much wider, since it isn't pulled toward current_price at all)
    # percentiles. p5/p95 double as this model's bear/bull case: no
    # separate analyst-target-derived floor/cap price (see CAVEATS) --
    # eps_draws is only floored at 0 now (no negative EPS), no analyst-
    # target guardrail on either side.
    def _forecast(x):
        return max(0.0, current_price + confidence * (x - current_price))

    stats_industry = None
    forecast_price = None
    forecast_return = None
    forecast_price_p20 = None
    forecast_price_p80 = None
    if industry_pe is not None and industry_pe > 0:
        prices_industry = eps_draws_floored * mu_pe
        if book_value_floor is not None:
            prices_industry = np.maximum(prices_industry, book_value_floor)
        stats_industry = _price_stats(prices_industry, current_price)
        forecast_price = _forecast(stats_industry["median"])
        forecast_return = forecast_price / current_price - 1
        forecast_price_p20 = _forecast(stats_industry["p20"])
        forecast_price_p80 = _forecast(stats_industry["p80"])

    result = {
        "ticker": ticker,
        "name": row.get("name") or None,
        "sector": row.get("sector") or None,
        "forecastPrice": forecast_price,
        "forecastReturn": forecast_return,
        "forecastPriceP20": forecast_price_p20,
        "forecastPriceP80": forecast_price_p80,
        # SimPrice: p5/p95-winsorized mean of the simulated-path price
        # distribution, scaled down by the risk-premium multiple haircut
        # (pe_haircut, see RISK_PREMIUM_K) -- so a more uncertain earnings
        # stream is priced at a lower multiple.
        # See the simulated-path block in this module's docstring. NOT
        # confidence-pulled toward currentPrice (that is forecastPrice's
        # separate mechanism); simSharpe is built from the un-haircut
        # mean/vol so the premium isn't double-counted.
        "simPrice": sim_price,
        "simReturn": sim_return,
        "simSharpe": sim_sharpe,
        "currentPrice": current_price,
        "inputs": {
            "fwdEps": fwd_eps,
            "fwdEpsSource": fwd_eps_source,
            "currentYearEps": current_year_eps,
            "trailingEps": trailing_eps,
            "anchorEps": anchor_eps,
            "yearReturn": to_float(row.get("yearReturn")),
            "epsTrend": eps_trend,
            # revenueGrowth / earningsGrowth here are the RECONCILED blends
            # from screen_data.csv (see modules.derive) -- a recency-weighted
            # trailing-quarter blend for revenue, 0.5 Q + 0.5 filed-FY for
            # earnings, each with a Tier-A corruption override -- NOT
            # yfinance's raw single-quarter ratios. *Source names which path
            # produced it; earningsGrowthQ is the raw quarterly figure.
            "revenueGrowth": revenue_growth,
            "revenueGrowthSource": row.get("revenueGrowthSource") or None,
            "revenueBasedEps": revenue_based_eps,
            "netDebtToRevenue": net_debt_to_revenue,
            "leveragePenalty": leverage_penalty,
            "earningsGrowth": earnings_growth,
            "earningsGrowthSource": row.get("earningsGrowthSource") or None,
            "earningsGrowthQ": to_float(row.get("earningsGrowthQ")),
            # Margin-trend overlay: earningsMarginDelta (YoY net-margin
            # change / share) put on eps_2's basis; added per year in
            # the EPS path, fading own -> industry. ownGrowthRate no longer
            # carries an earningsGrowth-rate cap -- this replaces it.
            "earningsMarginDelta": own_margin_delta,
            "industryMarginDelta": ind_margin_delta,
            "ownDeltaEps2": own_delta_eps2,
            "industryDeltaEps2": ind_delta_eps2,
            "operatingMargin": operating_margin,
            "grossMargin": gross_margin,
            "growthMargin": growth_margin,
            "marginAdjustedRevenueGrowth": margin_adjusted_revenue_growth,
            "ownGrowthRate": own_growth_rate,
            "eulerRevGrowth1y": euler_rev_growth,
            "eulerFwdEps2y": euler_fwd_eps2y,
            "eulerRevGrowth2y": euler_rev_growth2y,
            "industryEpsTrend": ind_eps_trend,
            "industryEulerRevGrowth": ind_euler_rev_growth,
            "industryRevenueGrowth": ind_revenue_growth,
            "industryEarningsGrowth": ind_earnings_growth,
            "industryOperatingMargin": ind_operating_margin,
            "industryGrossMargin": ind_gross_margin,
            "industryGrowthRate": industry_growth_rate,
            "convergenceGrowthRate": convergence_growth_rate,
            "epsPath": eps_path,
            "discountedEpsPath": discounted_eps_path,
            "beta": beta,
            "industryBeta": industry_beta,
            "betaForDiscount": beta_for_discount,
            "bookValue": book_value,
            "bookValueFloor": book_value_floor,
            "effectiveDiscountRate": effective_discount_rate,
            "muEps": mu_eps,
            "sigmaEps": sigma_eps,
            "sigmaEpsLog": sigma_log,
            "epsVolatilitySource": eps_vol_source,
            "industryEpsVolatility": industry_eps_vol,
            "analystDispersion": analyst_dispersion,
            "thinCoverage": thin_coverage,
            "combinedVol": combined_vol,
            "confidence": confidence,
            # Risk-premium multiple haircut actually applied to SimPrice /
            # SimReturn / simPriceDistribution (1.0 = none; floored at
            # RISK_PREMIUM_PE_FLOOR). None when the simulated path didn't run.
            "simPeHaircut": pe_haircut if stats_sim is not None else None,
            "ownPe": own_pe,
            "industryMedianPe": industry_pe,
            "muPe": mu_pe,
            "convergenceMultiple": convergence_multiple,
            "peerCount": peer_n,
            "peLevel": pe_level,
            "peerPeCv": peer_pe_cv,
            "peerPeLevel": peer_pe_level,
            "peerPeCount": len(peer_pe_pool) if peer_pe_pool else 0,
            "simReturnVol": sim_return_vol,
        },
        "priceAtIndustryMultiple": stats_industry,
        "simPriceDistribution": stats_sim,
        # Not part of the model itself -- a cheap independent cross-check
        # against what sell-side analysts are already projecting.
        "analystTargets": {
            "mean": target_mean_price,
            "low": target_low_price,
            "high": target_high_price,
        },
    }
    # Computed for EVERY simulated ticker, not just Strong Buy/Strong Sell
    # -- the Simulations page (web/src/pages/SimulationsView.tsx) reads
    # simulations.json directly and covers the full active universe, so a
    # click-to-open note needs to exist for whichever row the user picks.
    # modules/recommendations.py's build_recommendations reuses this SAME
    # field (rather than recomputing it) for its own narrower Strong
    # Buy/Strong Sell-only `notes` column -- one source of truth, cost is
    # already trivial (rule-based, no model/API call) so computing it for
    # every ticker here isn't wasted work even for rows that never surface
    # it in the recommendations table.
    result["notes"] = generate_note(result)
    return result


def _fmt_money(value):
    return f"${value:,.2f}" if value is not None else "n/a"


def _fmt_pct(value):
    return f"{value * 100:+.0f}%" if value is not None else "n/a"


def _real_base_summary(inputs):
    """Replicates simulate_ticker's own real_base priority chain (see that
    function's own comment above `real_base = (...)`) purely from output
    fields, to name which source actually anchored the near-term EPS path
    and its resulting value -- the single most significant input behind
    any simReturn, so it leads the note."""
    fwd_eps = inputs.get("fwdEps")
    current_year_eps = inputs.get("currentYearEps")
    trailing_eps = inputs.get("trailingEps")
    revenue_based_eps = inputs.get("revenueBasedEps")

    if current_year_eps is not None and current_year_eps > 0:
        trailing_eps_consistent = (
            trailing_eps is not None and trailing_eps > 0
            and FWD_TRAILING_PE_RATIO_MIN <= trailing_eps / current_year_eps <= FWD_TRAILING_PE_RATIO_MAX
        )
        if trailing_eps_consistent:
            value = REAL_BASE_BLEND_WEIGHT * current_year_eps + (1.0 - REAL_BASE_BLEND_WEIGHT) * trailing_eps
            return value, f"blend of current-year EPS ({_fmt_money(current_year_eps)}) and trailing EPS ({_fmt_money(trailing_eps)})"
        return current_year_eps, f"current-year EPS ({_fmt_money(current_year_eps)})"
    if trailing_eps is not None and trailing_eps > 0:
        return trailing_eps, f"trailing EPS ({_fmt_money(trailing_eps)})"
    if fwd_eps is not None and fwd_eps > 0:
        return fwd_eps, f"forward EPS ({_fmt_money(fwd_eps)})"
    if revenue_based_eps is not None:
        return revenue_based_eps, f"synthetic revenue x assumed-margin EPS ({_fmt_money(revenue_based_eps)}), no positive reported/forward EPS available"
    return None, "no positive EPS signal available"


def generate_note(entry):
    """Rule-based, deterministic explanation of one simulate_ticker result,
    for the recommendations table's `notes` field (Strong Buy/Strong Sell
    tickers only -- see modules/recommendations.py) and the Simulations
    page's click-to-open popup (every ticker). Returns a LIST of short
    bullet strings -- the most significant inputs behind that ticker's
    simReturn -- not a paragraph: explicit instruction, the headline
    "simulation implies +X% vs current price" sentence was dropped
    because simPrice/simReturn are already the table's own columns, and
    what's actually useful in a note is what's NOT already visible: which
    EPS figure anchored the path, the growth/multiple assumptions applied
    to it, and any data-quality flags. Ordered by how directly each
    drives the output -- real-base EPS anchor first (the single biggest
    lever), then growth rate, then multiple, then narrower flags
    (forwardPE/trailingPE swap, EPS mismatch, synthetic fallback), then
    coverage/confidence caveats, then guardrail binding, with the analyst
    target last as outside context rather than a model input. WITHOUT any
    model/API call: everything here already exists in
    `entry["inputs"]`/`entry["analystTargets"]`. Deliberately mechanical,
    not judgment-based -- it flags the same facts a human reviewer would
    start from (e.g. "forward EPS is 3x trailing EPS"), not a verdict on
    whether that's benign (real turnaround) or not (see this session's
    FWRD/BKKT/TRS/ENR/HLMN reviews for what that extra judgment looks
    like). Returns None for an errored simulation or one with nothing
    notable to say."""
    if entry.get("error"):
        return None
    inputs = entry.get("inputs") or {}
    sim_price = entry.get("simPrice")
    targets = entry.get("analystTargets") or {}
    target_mean = targets.get("mean")
    target_low = targets.get("low")
    target_high = targets.get("high")
    current_price = entry.get("currentPrice")

    parts = []

    _, real_base_desc = _real_base_summary(inputs)
    parts.append(f"Near-term EPS anchored to {real_base_desc}.")

    own_growth_rate = inputs.get("ownGrowthRate")
    convergence_growth_rate = inputs.get("convergenceGrowthRate")
    if own_growth_rate is not None:
        parts.append(
            f"Own growth rate {_fmt_pct(own_growth_rate)}, reverting toward a "
            f"{_fmt_pct(convergence_growth_rate)} peer/consensus-blended rate over the projection."
        )

    own_pe = inputs.get("ownPe")
    industry_pe = inputs.get("industryMedianPe")
    mu_pe = inputs.get("muPe")
    if own_pe is not None and industry_pe is not None and industry_pe > 0:
        ratio = own_pe / industry_pe
        parts.append(
            f"Priced at {own_pe:.1f}x forward earnings vs a {industry_pe:.1f}x peer-group median "
            f"({ratio:.1f}x peers); multiple modeled reverting to {mu_pe:.1f}x blended."
        )

    fwd_eps = inputs.get("fwdEps")
    fwd_source = inputs.get("fwdEpsSource") or ""
    current_year_eps = inputs.get("currentYearEps")
    trailing_eps = inputs.get("trailingEps")

    if "thin coverage" in fwd_source:
        parts.append(
            f"forwardEps was swapped for trailingEps ({_fmt_money(fwd_eps)}) -- thin analyst "
            "coverage plus an out-of-band forwardPE/trailingPE ratio; worth a manual check "
            "against reported earnings."
        )
    if (
        current_year_eps is not None and current_year_eps > 0
        and trailing_eps is not None and trailing_eps > 0
        and (current_year_eps > trailing_eps * 3 or trailing_eps > current_year_eps * 3)
    ):
        parts.append(
            f"Current-year EPS ({_fmt_money(current_year_eps)}) and trailing EPS "
            f"({_fmt_money(trailing_eps)}) disagree by {max(current_year_eps, trailing_eps) / max(min(current_year_eps, trailing_eps), 0.01):.1f}x -- "
            "a large recent earnings swing; the model blends the two rather than trusting either alone."
        )

    thin_coverage = inputs.get("thinCoverage")
    confidence = inputs.get("confidence")
    if thin_coverage:
        parts.append(
            "Thin analyst coverage (fewer than 3 estimates) -- forward figures and the "
            "analyst-target floor/cap are less reliable here."
        )
    if confidence is not None and confidence < 0.5:
        parts.append(
            f"Low model confidence ({confidence:.0%}) from combined earnings/multiple "
            "volatility -- simReturn is already discounted for this, treat the point estimate as fuzzy."
        )

    if target_low and target_high and sim_price is not None and not thin_coverage:
        floor_price = target_low * ANALYST_TARGET_FLOOR_MULTIPLE
        cap_price = target_high * ANALYST_TARGET_CAP_MULTIPLE
        if sim_price <= floor_price * 1.02:
            parts.append(
                f"Simulated price sits at the analyst-target floor ({_fmt_money(floor_price)}, "
                f"{ANALYST_TARGET_FLOOR_MULTIPLE}x the low target) -- the EPS path alone would "
                "otherwise imply an even lower price."
            )
        elif sim_price >= cap_price * 0.98:
            parts.append(
                f"Simulated price sits at the analyst-target cap ({_fmt_money(cap_price)}, "
                f"{ANALYST_TARGET_CAP_MULTIPLE}x the high target) -- the EPS path alone would "
                "otherwise imply an even higher price."
            )

    # Outside context, not a model input -- listed last.
    if target_mean and current_price:
        consensus_return = target_mean / current_price - 1.0
        parts.append(
            f"Analyst target: {_fmt_money(target_mean)} mean ({_fmt_pct(consensus_return)}), "
            f"range {_fmt_money(target_low)}-{_fmt_money(target_high)}."
        )

    return parts or None


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
