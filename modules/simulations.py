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

     ownGrowthRate      = avg(epsTrend, marginAdjustedRevenueGrowth)        -- THIS ticker's own
     industryGrowthRate = avg(industryEpsTrend, industryMarginAdjRevGrowth) -- peer MEDIAN
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
   (and is floored at) FALLBACK_EPS_REL_STDEV (20%) when epsVolatility
   itself isn't on file (needs >=3 years of annual EPS history -- see
   that function) or is implausibly low (an unusually quiet historical
   window shouldn't collapse the distribution to a near-delta-function).
   analystDispersion = (targetHighPrice - targetLowPrice) /
   (2 * targetMeanPrice) is how much sell-side analysts disagree with
   EACH OTHER about where the stock is headed, already in the same
   relative-% space as epsVolatility; combinedVol falls back to
   epsVolatility alone when analyst targets aren't on file.

2. The SAME N simulated eps_i draws are priced ONE way, against a single
   FIXED multiple (no multiple-level distribution/spread; all of the
   price distribution's shape comes from the EPS side alone). No
   fundamental (book-value/cumulative-earnings) floor -- an earlier
   version of this module had one; removed (see CAVEATS: a floor built
   from this module's own projected epsPath just inherited that
   projection's own uncertainty, and was binding -- silently overriding
   the model's own confidence-weighted view -- for over a quarter of the
   universe in practice). An earlier version of this design also priced a
   second, ownPe-scaled scenario alongside the industry one ("at today's
   own multiple" vs. "at the industry median") -- retired once mu_eps
   itself moved to anchoring off industryPe (step 1 above): pricing that
   same industry-anchored EPS at the ticker's OWN multiple no longer
   isolates an independent signal, so only the industry scenario remains:

     price_i = max(eps_i, 0) * industryPe

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
   targetHighPrice missing), falls back to the industry/sector-median
   analyst_dispersion instead of a single fixed g_fwd for every path --
   PEERS' own (targetHighPrice - targetLowPrice) / (2*targetMeanPrice),
   pooled the same industry-then-sector way as every other peer metric
   (MIN_INDUSTRY_PEERS/MIN_PEERS gates), applied as a SYMMETRIC spread
   around g_fwd since there's no real low/high skew to draw from for this
   ticker, just a typical peer WIDTH:

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
  backward-looking risk estimate. Clamped to [0.5, 3.0] so one extreme
  beta reading (e.g. a raw beta of 5+) can't over-discount mu_eps's own
  5-year EPS path in step 1 -- mu_eps can still differ substantially
  across high/low-beta names, just not by an unbounded amount.
- No fundamental price floor (removed). An earlier version added one
  (bookValue + sum(epsPath)), but it was built from this SAME module's
  own projected epsPath, so it inherited that projection's uncertainty
  rather than acting as an independent sanity check -- confirmed live: it
  was binding (forecastPrice pinned exactly to the floor) for over a
  fifth of the universe, and for 17 tickers it overrode a genuinely
  bearish confidence-weighted signal outright.
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
FALLBACK_EPS_REL_STDEV = 0.20
N_SIMULATIONS = 20000
PERCENTILES = (20, 50, 80)
# Years in the EPS path -- anchorEps (year 0) plus 4 growth steps.
# See simulate_ticker's own comment.
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
# Year-1 EPS growth is a blend of the concave own->industry schedule rate
# and g_fwd (the drift implied by forwardEps vs. the price-anchored
# anchorEps). Y1_SCHEDULE_WEIGHT is the schedule side's share; g_fwd gets
# (1 - Y1_SCHEDULE_WEIGHT). Raised from 0.5 to 0.6 -- for a name trading
# well above its peer multiple, anchorEps >> forwardEps makes g_fwd
# strongly negative (~-0.85), and at a full 50% weight that single
# year-1 collapse dominated forecastReturn and compressed every richly-
# valued name in a peer group to roughly the same number regardless of
# its own projected growth. Shifting 10 points onto the schedule rate
# lets own_growth_rate carry a bit more of year 1 without abandoning the
# price-anchored design.
Y1_SCHEDULE_WEIGHT = 0.6
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


METRIC_KEYS = ("forwardPE", "trailingPE", "epsTrend", "revenueGrowth", "earningsGrowth", "earningsMarginDelta", "operatingMargin", "grossMargin", "analystDispersion")


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
        return {"ticker": ticker, "error": "not found in screen_data.csv"}

    fwd_eps = to_float(row.get("forwardEps"))
    current_price = to_float(row.get("price"))
    own_pe = to_float(row.get("forwardPE"))
    if fwd_eps is None or current_price is None or own_pe is None or own_pe <= 0:
        return {"ticker": ticker, "error": "missing forwardEps, price, or forwardPE"}

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
    ind_revenue_growth, _, _ = _peer_median(ticker, industry, *peer_pools["revenueGrowth"])
    ind_earnings_growth, _, _ = _peer_median(ticker, industry, *peer_pools["earningsGrowth"])
    ind_operating_margin, _, _ = _peer_median(ticker, industry, *peer_pools["operatingMargin"])
    ind_gross_margin, _, _ = _peer_median(ticker, industry, *peer_pools["grossMargin"])
    earnings_growth = to_float(row.get("earningsGrowth"))

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
    # dollar overlay on the EPS path (own_delta_anchor / ind_delta_anchor,
    # computed below) that fades own -> industry, so a name whose growth
    # isn't reaching the bottom line gets a negative overlay instead of a
    # rate cap.
    own_growth_rate = _combine_growth(eps_trend, margin_adjusted_revenue_growth)

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
    industry_growth_rate = _combine_growth(ind_eps_trend, ind_margin_adjusted_revenue_growth)

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
    # No-coverage forwardEps sanity swap (see FWD_TRAILING_PE_RATIO_* above).
    # Uncovered name + forwardPE wildly out of line with its own trailingPE
    # => treat forwardEps as bad data and use trailingEps as the forward
    # figure instead (flat, no growth priced in). own_pe follows so ownPe
    # in the output reflects the value actually used.
    fwd_eps_source = "forwardEps"
    if (not to_float(row.get("numberOfAnalystOpinions"))
            and trailing_eps is not None and trailing_eps > 0
            and trailing_pe is not None and trailing_pe > 0):
        _pe_ratio = own_pe / trailing_pe
        if not (FWD_TRAILING_PE_RATIO_MIN <= _pe_ratio <= FWD_TRAILING_PE_RATIO_MAX):
            fwd_eps = trailing_eps
            own_pe = trailing_pe
            fwd_eps_source = "trailingEps (no coverage, forwardPE/trailingPE out of band)"
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
    # EPS path's own basis by multiplying by anchor_eps -- ownDeltaAnchor is
    # then bounded to +/-EARN_MARGIN_DELTA_CAP * anchor_eps and never
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
    own_delta_anchor = own_margin_delta * anchor_eps if own_margin_delta is not None else 0.0
    ind_delta_anchor = ind_margin_delta * anchor_eps if ind_margin_delta is not None else 0.0

    # Concave reversion: w_t = sqrt((N-t)/N), own->industry over N steps.
    # Year 1 blends the schedule rate with g_fwd (analyst implied return),
    # Y1_SCHEDULE_WEIGHT on the schedule side.
    n_steps = EPS_PROJECTION_YEARS - 1
    eps_path = [anchor_eps]
    growth_path = anchor_eps  # pure multiplicative growth, no overlay
    for t in range(1, EPS_PROJECTION_YEARS):
        w = math.sqrt((n_steps - t) / n_steps) if t < n_steps else 0.0
        g_t = w * own_growth_rate + (1.0 - w) * industry_growth_rate
        if t == 1:
            g_fwd = (fwd_eps / anchor_eps - 1.0) if abs(anchor_eps) > 1e-9 else 0.0
            g_fwd = max(GROWTH_FLOOR, min(GROWTH_CAP, g_fwd))
            g_t = Y1_SCHEDULE_WEIGHT * g_t + (1.0 - Y1_SCHEDULE_WEIGHT) * g_fwd
        g_t = max(GROWTH_FLOOR, min(GROWTH_CAP, g_t))
        growth_path = growth_path * (1.0 + g_t)
        # Fading additive margin-trend overlay: NOT cumulative (a single
        # term per year), bounded by own_delta_anchor, -> ind_delta_anchor
        # by year N (w = 0).
        md_t = w * own_delta_anchor + (1.0 - w) * ind_delta_anchor
        eps_path.append(growth_path + md_t)

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
    # 1.0 (market-average risk, i.e. the flat DISCOUNT_RATE unscaled).
    # Clamped to [BETA_FLOOR, BETA_CAP] rather than left at its raw value
    # -- a negative or near-zero beta would flip or collapse the discount
    # rate itself, which isn't a meaningful "less risky than the market"
    # reading so much as a low/negative-correlation artifact; an extreme
    # high beta (e.g. PGY's 5.374) would otherwise let a single noisy
    # input swamp mu_eps's own 5-year discounted EPS path.
    beta = to_float(row.get("beta"))
    effective_discount_rate = DISCOUNT_RATE * (max(BETA_FLOOR, min(beta, BETA_CAP)) if beta is not None else 1.0)
    discount_weights = [1.0 / ((1.0 + effective_discount_rate) ** i) for i in range(len(eps_path))]
    discounted_eps_path = [eps_path[i] * discount_weights[i] for i in range(len(eps_path))]
    # Weighted mean by the SAME discount weights, not a plain mean of
    # already-discounted values -- dividing by raw year-count (5) instead
    # of by the weights' own sum systematically understates mu_eps
    # whenever effectiveDiscountRate > 0, even for a FLAT (zero-growth)
    # epsPath: mean(w_i) < 1 for any r > 0, so mu_eps < anchorEps purely
    # from discounting mechanics, with no earnings signal behind it at
    # all -- confirmed live across 465 near-zero-growth tickers, where
    # forecastReturn dropped monotonically from +0.7% (beta < 0.75) to
    # -12.3% (beta 2-3) despite a flat earnings picture in every case.
    # The weighted mean is the correct annuity-equivalent average: for a
    # flat epsPath it reproduces anchorEps EXACTLY regardless of beta,
    # while still weighting near-term years more than distant ones (the
    # discount weights themselves still decay with i).
    mu_eps = sum(discounted_eps_path) / sum(discount_weights)

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

        # epsVolatility's own contribution to the year-1 drift spread, in the
        # per-SD units g_fwd's sigma_low/sigma_high/sigma_industry are in --
        # RSS'd into each of them below so the year-1 draw widens for a
        # volatile-earnings name even when its analyst target range is tight
        # (or, in the final branch, absent).
        eps_growth_sigma = eps_vol / SHOCK_CLIP_SD

        # Per-path g_fwd draw -- explicit instruction: replaces the single
        # fixed g_fwd (computed below, still used as-is by the
        # deterministic base case) with a real analyst-implied range, tied
        # to the SAME shared shock z. g_fwd_i is a linear function of a
        # Normal variable (z), so it's itself Normally distributed -- a
        # split-normal, since sigma_low/sigma_high are generally different
        # (analyst target ranges are rarely symmetric around the mean):
        # z == -SHOCK_CLIP_SD lands exactly on g_fwd_low, z == 0 exactly
        # on g_fwd (today's existing, unchanged deterministic value), and
        # z == +SHOCK_CLIP_SD exactly on g_fwd_high, continuous in
        # between.
        #
        # When THIS ticker has no analyst target range of its own, falls
        # back on the industry/sector-median analyst_dispersion (relative
        # half-width of PEERS' own target ranges, see
        # industry_analyst_dispersion above) as a symmetric spread around
        # g_fwd -- still tied to the same shared shock z, not a single
        # fixed value for every path -- and only drops to the single
        # fixed g_fwd (no per-path spread at all) when even the broad
        # sector has no analyst coverage to borrow a typical spread from.
        if target_low_price is not None and target_high_price is not None and current_price > 0:
            g_fwd_low = max(GROWTH_FLOOR, min(GROWTH_CAP, target_low_price / current_price - 1.0))
            g_fwd_high = max(GROWTH_FLOOR, min(GROWTH_CAP, target_high_price / current_price - 1.0))
            # Clamped at 0 -- a data anomaly (e.g. low > mean) should mean
            # "no extra spread on this side," never a sign flip that would
            # make the interpolation run backwards.
            sigma_low = math.hypot(max(0.0, (g_fwd - g_fwd_low) / SHOCK_CLIP_SD), eps_growth_sigma)
            sigma_high = math.hypot(max(0.0, (g_fwd_high - g_fwd) / SHOCK_CLIP_SD), eps_growth_sigma)
            g_fwd_draws = np.where(z < 0, g_fwd + z * sigma_low, g_fwd + z * sigma_high)
        elif industry_analyst_dispersion is not None:
            # No real low/high skew to draw from for THIS ticker, just a
            # typical peer WIDTH -- so, unlike the per-ticker case above,
            # the fallback spread is symmetric around g_fwd.
            sigma_industry = math.hypot(industry_analyst_dispersion / SHOCK_CLIP_SD, eps_growth_sigma)
            g_fwd_draws = np.clip(g_fwd + z * sigma_industry, GROWTH_FLOOR, GROWTH_CAP)
        else:
            # No analyst target range for this ticker AND no peer spread to
            # borrow -- epsVolatility alone still gives year 1 a real
            # per-path spread instead of collapsing to a single fixed g_fwd.
            g_fwd_draws = np.clip(g_fwd + z * eps_growth_sigma, GROWTH_FLOOR, GROWTH_CAP)
        p_draws = np.clip(0.5 + z * (peer_pe_cv * 0.5), REVERSION_EXPONENT_MIN, REVERSION_EXPONENT_MAX)

        eps_path_sim = np.full(n, anchor_eps)
        growth_path_sim = np.full(n, anchor_eps)  # pure growth, no overlay
        discounted_sum_sim = eps_path_sim * discount_weights[0]
        weight_sum_sim = discount_weights[0]
        for t in range(1, EPS_PROJECTION_YEARS):
            # (n_steps - t)/n_steps is exactly 0 at t == n_steps (any
            # positive power of 0 is still 0, p_draws is always positive
            # via the REVERSION_EXPONENT_MIN/MAX clip), so this reproduces
            # the deterministic loop's explicit "w = 0 at t == n_steps"
            # branch without needing a separate guard. industry_growth_rate
            # is the plain unshocked scalar (see above), so as w_sim -> 0
            # every path converges to the EXACT SAME g_t regardless of its
            # own shock -- the mean-reversion property.
            w_sim = ((n_steps - t) / n_steps) ** p_draws
            g_t_sim = w_sim * own_growth_draws + (1.0 - w_sim) * industry_growth_rate
            if t == 1:
                # g_fwd_draws: per-path draw from the analyst target range
                # (see this block's own comment above), not the fixed
                # deterministic g_fwd -- this is what gives year 1 its own
                # genuine spread instead of being anchored to a constant.
                # Same Y1_SCHEDULE_WEIGHT split as the deterministic path.
                g_t_sim = Y1_SCHEDULE_WEIGHT * g_t_sim + (1.0 - Y1_SCHEDULE_WEIGHT) * g_fwd_draws
            g_t_sim = np.clip(g_t_sim, GROWTH_FLOOR, GROWTH_CAP)
            growth_path_sim = growth_path_sim * (1.0 + g_t_sim)
            # Same fading additive margin-trend overlay as the deterministic
            # path; w_sim is the per-path concave weight so the overlay
            # converges own -> industry alongside the growth rate.
            md_t_sim = w_sim * own_delta_anchor + (1.0 - w_sim) * ind_delta_anchor
            eps_path_sim = growth_path_sim + md_t_sim
            discounted_sum_sim = discounted_sum_sim + eps_path_sim * discount_weights[t]
            weight_sum_sim += discount_weights[t]
        mu_eps_sim = discounted_sum_sim / weight_sum_sim
        # Safety floor only -- a path can't actually reach here <= 0 while
        # anchor_eps > 0 (every (1 + g_t) >= 0.01 via the GROWTH_FLOOR clip),
        # which holds for every ticker whose anchor is price / industryPE or
        # a >0-guarded EPS fallback. Kept as a guard for the lone
        # anchor_eps = fwd_eps branch (negative forwardEps).
        mu_eps_sim_floored = np.maximum(mu_eps_sim, 0.0)
        # Fixed multiple, same as industry_pe below -- see this block's
        # own opening comment on why the multiple itself isn't randomized.
        sim_prices = mu_eps_sim_floored * industry_pe
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
        stats_sim = _price_stats(sim_prices_hc, current_price)
        # SimPrice = mean of the p5/p95-winsorized haircut distribution,
        # deliberately NOT confidence-pulled toward current_price the way
        # forecastPrice is.
        sim_price = stats_sim["mean"]
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
        prices_industry = eps_draws_floored * industry_pe
        stats_industry = _price_stats(prices_industry, current_price)
        forecast_price = _forecast(stats_industry["median"])
        forecast_return = forecast_price / current_price - 1
        forecast_price_p20 = _forecast(stats_industry["p20"])
        forecast_price_p80 = _forecast(stats_industry["p80"])

    return {
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
            "earningsGrowth": earnings_growth,
            "earningsGrowthSource": row.get("earningsGrowthSource") or None,
            "earningsGrowthQ": to_float(row.get("earningsGrowthQ")),
            # Margin-trend overlay: earningsMarginDelta (YoY net-margin
            # change / share) put on anchorEps's basis; added per year in
            # the EPS path, fading own -> industry. ownGrowthRate no longer
            # carries an earningsGrowth-rate cap -- this replaces it.
            "earningsMarginDelta": own_margin_delta,
            "industryMarginDelta": ind_margin_delta,
            "ownDeltaAnchor": own_delta_anchor,
            "industryDeltaAnchor": ind_delta_anchor,
            "operatingMargin": operating_margin,
            "grossMargin": gross_margin,
            "growthMargin": growth_margin,
            "marginAdjustedRevenueGrowth": margin_adjusted_revenue_growth,
            "ownGrowthRate": own_growth_rate,
            "industryEpsTrend": ind_eps_trend,
            "industryRevenueGrowth": ind_revenue_growth,
            "industryEarningsGrowth": ind_earnings_growth,
            "industryOperatingMargin": ind_operating_margin,
            "industryGrossMargin": ind_gross_margin,
            "industryGrowthRate": industry_growth_rate,
            "epsPath": eps_path,
            "discountedEpsPath": discounted_eps_path,
            "beta": beta,
            "effectiveDiscountRate": effective_discount_rate,
            "muEps": mu_eps,
            "sigmaEps": sigma_eps,
            "sigmaEpsLog": sigma_log,
            "epsVolatilitySource": eps_vol_source,
            "analystDispersion": analyst_dispersion,
            "combinedVol": combined_vol,
            "confidence": confidence,
            # Risk-premium multiple haircut actually applied to SimPrice /
            # SimReturn / simPriceDistribution (1.0 = none; floored at
            # RISK_PREMIUM_PE_FLOOR). None when the simulated path didn't run.
            "simPeHaircut": pe_haircut if stats_sim is not None else None,
            "ownPe": own_pe,
            "industryMedianPe": industry_pe,
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
