"""scoring.py — every function that goes into sorted_screen.csv's composite
score: the shared ranking primitives, the enrichment steps that compute a
couple of indicators in the first place (targetUpside, LiqRatio), the
individual indicator/factor calculations, and score_rows itself, which
combines all of them with the weights below into one number per ticker.
Pulled out of main.py (which still owns the download pipeline and file I/O)
so the scoring logic has one home instead of being buried in a 230-line
function.

Weights live in FACTOR_WEIGHTS below (each of STANDARD_WEIGHTS/
FINANCIALS_WEIGHTS must sum to 1.00 -- score_rows's own weighted sum is
the only place that's enforced, there's no runtime assertion) -- also
the source GET /api/scoring-formula (ib_server.py) reads for the web
app's Scoring tab, so the two formulas shown there can never drift from
what score_rows actually computes. See each factor function's own
docstring for what "better" means for it, is_financials_sector's own
docstring for why Financials gets a second formula, and score_rows's
docstring for the full picture.
"""

import json
import math
import statistics
from datetime import date, timedelta


# ---------------------------------------------------------------------- #
#  Shared date/freshness utility -- not a ranking primitive, just kept   #
#  here since this is the one dependency-light module both main.py and  #
#  ib_server.py already import from without risking a circular    #
#  import (ib_server.py imports FROM main.py, so main.py can't    #
#  import back from it).                                                #
# ---------------------------------------------------------------------- #
def most_recent_completed_trading_day():
    """Yesterday, rolled back over the weekend -- a plain weekday
    heuristic, not a real market-holiday calendar (same "good enough,
    don't over-engineer the calendar" spirit finra.py's own settlement-
    date probing already uses). Whatever this resolves to should have a
    settled close available from any real data source by now -- used by
    ib_server.py's own dataset-staleness check (is the price-
    history data actually current, not just recently-written) and by
    main.py's download_ib_daily_history (is a ticker's existing IB daily
    bar recent enough to skip re-fetching it)."""
    d = date.today() - timedelta(days=1)
    while d.weekday() >= 5:  # Saturday=5, Sunday=6
        d -= timedelta(days=1)
    return d.isoformat()


# ---------------------------------------------------------------------- #
#  Shared ranking primitives                                              #
# ---------------------------------------------------------------------- #
def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def rank_ascending(rows, value_fn, missing=1.0):
    """Percentile rank (0 = best/lowest, 1 = worst) of rows by value_fn(d);
    rows where value_fn returns None get `missing` (default the worst
    rank, 1.0 -- the right default almost everywhere else in this file,
    where "no data" plausibly correlates with something real about the
    company). mean_reversion_rank passes missing=0.5 (neutral) instead
    -- see that function's own docstring for why "missing" means
    something structurally different there: IB Gateway only ever fetches
    hourly bars for ~40% of the universe (CANDLESTICK_TOP_N ranked/
    RATED_FOR_EXTRAS/held), so most of a "missing" reading here is a
    coverage-scope artifact, not a signal about the ticker. Confirmed
    live with the worst-default: mean_reversion_rank's own average
    percentile was 0.7-0.8 in EVERY sector group checked, none anywhere
    near neutral -- not sector bias, a universal one."""
    valid = [(symbol, value_fn(d)) for symbol, d in rows if value_fn(d) is not None]
    valid.sort(key=lambda item: item[1])
    n = len(valid)
    ranks = {symbol: i / (n - 1) if n > 1 else 0 for i, (symbol, _) in enumerate(valid)}
    return {symbol: ranks.get(symbol, missing) for symbol, _ in rows}


def neg_perf(field):
    def key(d):
        perf = to_float(d.get(field))
        return -perf if perf is not None else None
    return key


def neg_if_positive(field):
    """Like neg_perf, but zero/negative values are excluded from the
    normal magnitude-based ranking and fall back to the worst rank
    instead — the same treatment peg_rank gives negative pegRatio.
    Used for factors where "negative" is a qualitatively different,
    much worse signal than "low positive", not just more of the same."""
    def key(d):
        value = to_float(d.get(field))
        return -value if value is not None and value > 0 else None
    return key


def high_is_better_key(field):
    """Simple negation, for fields that are never meaningfully negative
    (a ratio, a percentage of float) where "high is better" and
    missing just means missing -- unlike neg_if_positive, there's no
    qualitatively-worse-than-missing negative case to special-case."""
    def key(d):
        value = to_float(d.get(field))
        return -value if value is not None else None
    return key


# ---------------------------------------------------------------------- #
#  Enrichment -- computes a raw field that a factor below then ranks     #
# ---------------------------------------------------------------------- #
def add_target_upside(data):
    """Adds targetUpside (sell-side mean target price vs. current price) to
    each entry in data, in place. Feeds analyst_conviction_rank."""
    for d in data.values():
        price = to_float(d.get("price"))
        target = to_float(d.get("targetMeanPrice"))
        d["targetUpside"] = target / price - 1 if price and target else None


def add_avg_liquidity_ratio(data):
    """Adds LiqRatio (mean of quickRatio and currentRatio) to each entry in
    data, in place. Left blank when either ratio is missing, rather than
    averaging just the one present value. Feeds liquidity_rank."""
    for d in data.values():
        quick = to_float(d.get("quickRatio"))
        current = to_float(d.get("currentRatio"))
        d["LiqRatio"] = (quick + current) / 2 if quick is not None and current is not None else None


# ---------------------------------------------------------------------- #
#  Sentiment -- loading + blending StockTwits/FinBERT scores              #
# ---------------------------------------------------------------------- #
# A single-quarter swing this large in aggregate institutional shares held
# is already an extreme, rare signal (see fetch_13f_holdings' own real
# numbers for context: mega-caps like AAPL/MSFT/NVDA move a few percent a
# quarter, not tens) -- beyond this shouldn't count for proportionally
# more in the sentiment blend below, same reasoning growth_rank's own
# GROWTH_CAP and IBApp.py's MARGIN_CAP already use for revenueGrowth/
# operatingMargins.
INST_CHANGE_CLIP = 0.5


def load_sentiment_scores(social_sentiment_file, news_sentiment_file, institutional_holdings_file=None):
    """{ticker: score in [-1, 1]}, blending StockTwits social sentiment
    (social_sentiment_file, already -1..1 -- see social_sentiment.py),
    FinBERT news sentiment (news_sentiment_file, {ticker: {articleId:
    score}} with each score 1 (very bearish) - 5 (very bullish) --
    written by ib_server.py's news_loop), and institutional
    quarter-over-quarter share-count change (institutional_holdings_file,
    {ticker: {pctShareChangeQoQ, ...}} -- see
    sec_edgar.fetch_13f_holdings; institutions net-buying vs. net-selling)
    into the single sentiment factor sentiment_rank uses. Neutral (score
    3) articles are dropped before averaging -- a ticker whose headlines
    are all routine filings/dividend notices shouldn't average out to
    "neutral" in a way that's indistinguishable from "no signal", it
    should just contribute no news opinion at all (same as having no
    news). Remaining news scores are averaged per ticker then
    recentered/rescaled to -1..1 as (avg - 3) / 2, so all three sources
    share the same scale before blending -- pctShareChangeQoQ is clipped
    to +/-INST_CHANGE_CLIP then divided by it. A ticker with only some of
    the three sources uses whichever it has; simple average of whatever's
    present. A missing file (e.g. that background process/script has
    never run), a ticker whose news was entirely neutral, or a ticker
    with no institutional-holdings match, just means that ticker falls
    back to whatever subset of sources it has, or (if none apply) an
    empty map -- sentiment_rank already ranks a missing score worst, same
    treatment as every other factor's missing data, so this never blocks
    scoring. Takes all file paths as arguments (rather than importing
    them from main.py) so main.py can import from this module without a
    circular import back the other way. institutional_holdings_file
    defaults to None (skipped entirely, same as if it were missing) so
    existing callers that only pass the first two files keep working
    unchanged."""
    try:
        with open(social_sentiment_file) as f:
            social = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        social = {}
    try:
        with open(news_sentiment_file) as f:
            news = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        news = {}
    institutional = {}
    if institutional_holdings_file:
        try:
            with open(institutional_holdings_file) as f:
                institutional = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            institutional = {}

    scores = {}
    for ticker in set(social) | set(news) | set(institutional):
        parts = []
        social_score = social.get(ticker, {}).get("score")
        if social_score is not None:
            parts.append(social_score)
        non_neutral = [s for s in news.get(ticker, {}).values() if s != 3]
        if non_neutral:
            parts.append((sum(non_neutral) / len(non_neutral) - 3) / 2)
        inst_change = institutional.get(ticker, {}).get("pctShareChangeQoQ")
        if inst_change is not None:
            clipped = max(-INST_CHANGE_CLIP, min(INST_CHANGE_CLIP, inst_change))
            parts.append(clipped / INST_CHANGE_CLIP)
        if parts:
            scores[ticker] = sum(parts) / len(parts)
    return scores


def sentiment_rank(rows, sentiment_scores):
    """High combined news+social sentiment (see load_sentiment_scores)
    ranks better; missing ranked worst. rank_ascending's value_fn only
    ever sees a row's own dict, not its symbol, so sentiment_scores (keyed
    by symbol) is injected into a copy of each row's dict first rather
    than looked up by symbol inside the key function."""
    augmented = [(symbol, {**d, "_sentimentScore": sentiment_scores.get(symbol)}) for symbol, d in rows]
    return rank_ascending(augmented, lambda d: -d["_sentimentScore"] if d.get("_sentimentScore") is not None else None)


# ---------------------------------------------------------------------- #
#  Insiders -- SEC Form 4 open-market buy/sell activity                   #
# ---------------------------------------------------------------------- #
def load_insider_scores(form4_file):
    """{ticker: score in [-1, 1]} from SEC EDGAR Form 4 filings (see
    sec_edgar.py's fetch_form4) -- (buys - sells) / (buys + sells), counting
    only open-market transactions (transactionCode 'P' = purchase, 'S' =
    sale). Every other code (M = option exercise, F = tax withholding, A =
    grant/award, G = gift, ...) is routine compensation mechanics, not a
    discretionary bet, and is excluded the same way load_sentiment_scores
    drops neutral news headlines above -- it shouldn't pull the score
    toward anything, it just shouldn't count. A ticker with no P/S
    transactions in the file (no Form 4 coverage at all, or only
    non-open-market activity) is left out of the returned map entirely;
    insiders_rank already ranks a missing score worst, same treatment as
    every other factor's missing data. Takes the file path as an argument
    rather than importing it from main.py, same reasoning as
    load_sentiment_scores."""
    try:
        with open(form4_file) as f:
            filings_by_ticker = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

    scores = {}
    for ticker, filings in filings_by_ticker.items():
        buys = sells = 0
        for filing in filings:
            for tx in filing.get("transactions", []):
                code = tx.get("code")
                if code == "P":
                    buys += 1
                elif code == "S":
                    sells += 1
        total = buys + sells
        if total:
            scores[ticker] = (buys - sells) / total
    return scores


def insiders_rank(rows, insider_scores):
    """Equally-weighted average of two DIFFERENT insider signals: recent
    open-market TRANSACTION activity (buys vs. sells) and current
    OWNERSHIP level (heldPercentInsiders, what fraction of shares
    insiders hold right now) -- one is "what have insiders been doing
    lately," the other is "how much skin do they have in the game,"
    distinct enough that a name can score well on one and poorly on the
    other (e.g. a founder-heavy small-cap with high ownership but no
    recent trading either way).

    Transaction-activity component: [0, 1] score (0=best/all-buys,
    1=worst/all-sells) directly from each ticker's raw open-market
    buy/sell ratio (see load_insider_scores -- (buys-sells)/(buys+sells),
    already bounded [-1, 1]), linearly rescaled via (1 - score) / 2;
    missing ranked worst. Deliberately NOT a population-relative
    percentile rank the way every other *_rank function here uses
    rank_ascending: insider open-market PURCHASES are rare industry-wide
    (confirmed on this universe's own Form4 data: ~78% of tickers with
    any open-market P/S activity have a raw ratio of exactly -1.0, i.e.
    literally zero buys), so a percentile rank ties that entire zero-buy
    majority to the worst rank and then lets a SINGLE stray buy against
    dozens of sells vault a ticker dramatically up the scale just for not
    being tied to that block -- confirmed live: LQDA (1 buy, 79 sells,
    raw ratio -0.976) ranked at the 78th percentile under the old
    rank_ascending treatment, reading as strongly bullish despite being
    98.8% sells. The raw ratio is already a bounded, comparable value --
    unlike momentum or the other unbounded factors rank_ascending exists
    to protect against one outlier crushing, it needs no percentile
    treatment at all, just a direct linear rescale into the same [0, 1]
    rank-space every other factor here reports.

    Ownership component: high heldPercentInsiders ranks better via the
    ordinary rank_ascending/high_is_better_key treatment every other
    percentage-shaped factor here uses -- unlike the transaction ratio,
    ownership levels ARE smoothly distributed across the universe (no
    single dominant value most tickers pile onto), so the population-
    relative percentile this file's other factors already lean on is the
    right tool here, not the transaction score's own special-cased linear
    rescale."""
    transaction_ranks = {symbol: (1 - insider_scores[symbol]) / 2 if symbol in insider_scores else 1.0 for symbol, _ in rows}
    ownership_ranks = rank_ascending(rows, high_is_better_key("heldPercentInsiders"))
    return {symbol: (transaction_ranks[symbol] + ownership_ranks[symbol]) / 2 for symbol, _ in rows}


# ---------------------------------------------------------------------- #
#  Valuation                                                              #
# ---------------------------------------------------------------------- #
def pe_rank(rows):
    """Low forwardPE ranks better; missing ranked worst."""
    return rank_ascending(rows, lambda d: to_float(d.get("forwardPE")))


def _sector_avg_forward_pe(rows):
    """MEDIAN forwardPE per sector, across rows that have both a sector and
    a forwardPE -- an arithmetic mean here is badly distorted by outliers
    in a thin, right-skewed sector: confirmed live for Biotechnology
    (only 41 of 204 tickers even reach scoring, the rest excluded as
    unranked for non-positive forwardPE -- see screen_rows), where three
    barely-profitable names with forwardPE in the hundreds/thousands
    pulled the mean to 99.8 while the median was 17.4 -- every other
    name in the sector then compared against that inflated mean and
    looked artificially cheap. Median is far more robust to exactly this
    shape of outlier and isn't specific to Biotechnology -- any sector
    this granular (Yahoo's ~130 industry labels, not broad GICS sectors)
    can have thin membership and a skewed tail, so this applies
    universally rather than as a per-sector carve-out."""
    values = {}
    for _, d in rows:
        sector = d.get("sector")
        fwd_pe = to_float(d.get("forwardPE"))
        if sector and fwd_pe is not None:
            values.setdefault(sector, []).append(fwd_pe)
    return {sector: statistics.median(vals) for sector, vals in values.items()}


def sector_relative_pe_rank(rows):
    """Low forwardPE relative to its sector's average forwardPE ranks
    better; missing sector or forwardPE (or a sector with no average yet)
    ranked worst."""
    sector_avg = _sector_avg_forward_pe(rows)

    def key(d):
        sector = d.get("sector")
        fwd_pe = to_float(d.get("forwardPE"))
        avg = sector_avg.get(sector)
        return fwd_pe / avg if sector and fwd_pe is not None and avg else None

    return rank_ascending(rows, key)


def fcf_rank(rows):
    """Low priceToFCF ranks better. Negative or missing priceToFCF
    (negative or unavailable free cash flow) carries no real cash-flow
    signal; treated as a fixed 200 instead of excluded, the same
    treatment pe_vs_trailing_rank gives negative/infinite trailingPE.
    Its own independent factor, alongside ev_ebitda_rank below -- both
    measure valuation relative to cash-generating ability, but from
    different angles (priceToFCF's market-cap numerator ignores debt,
    ev_ebitda_rank's enterprise-value numerator folds it in), so kept as
    two separately-weighted factors rather than averaged into one."""
    def effective_price_to_fcf(d):
        fcf = to_float(d.get("priceToFCF"))
        return 200 if fcf is None or fcf < 0 else fcf

    return rank_ascending(rows, effective_price_to_fcf)


def ev_ebitda_rank(rows):
    """Low enterpriseToEbitda ranks better; negative EBITDA ranked worst
    (see neg_if_positive) rather than given fcf_rank's fixed-200
    fallback -- there's no "priced off the mean, still comparable"
    convention for it the way priceToFCF has one, so it gets the same
    qualitatively-worse-than-low-positive treatment margin_rank/
    roe_rank/growth_rank give their own negative inputs. A heavily-
    levered name that looks cheap on priceToFCF alone (fcf_rank) can
    look expensive here, since enterprise value folds in debt that
    market cap ignores -- see that function's docstring."""
    return rank_ascending(rows, neg_if_positive("enterpriseToEbitda"))


def pe_vs_trailing_rank(rows):
    """More negative forwardPE - trailingPE ranks better (forward earnings
    cheap relative to trailing). Infinite or negative trailingPE means the
    company lost money over the trailing twelve months and carries no real
    earnings signal; ranked worst instead of substituted with a
    placeholder, since fwd_pe - placeholder would otherwise look like the
    best possible diff for cheap stocks (the opposite of what negative
    earnings should signal)."""
    def key(d):
        trailing_pe = to_float(d.get("trailingPE"))
        fwd_pe = to_float(d.get("forwardPE"))
        if trailing_pe is None or fwd_pe is None or not math.isfinite(trailing_pe) or trailing_pe < 0:
            return None
        return fwd_pe - trailing_pe

    return rank_ascending(rows, key)


def trailing_ps_rank(rows):
    """Low trailingPS (price / trailing-twelve-month revenue) ranks
    better; missing ranked worst. A separate valuation lens from
    pe_rank/fcf_rank/ev_ebitda_rank, not blended with any of them (same
    "independent factors, not averaged" call this project already made
    for fcf_rank vs. ev_ebitda_rank) -- revenue, unlike earnings, free
    cash flow, or EBITDA, is essentially never negative, so this stays
    meaningful for exactly the unprofitable/negative-FCF names where
    those other multiples break down and get ranked worst by
    substitution or exclusion. No special negative-value handling needed
    here the way fcf_rank/ev_ebitda_rank need -- there's no realistic
    negative-revenue case to guard against, and screen_rows already
    pre-filters to positive-forwardPE tickers before scoring starts
    anyway, same as pe_rank."""
    return rank_ascending(rows, lambda d: to_float(d.get("trailingPS")))


def peg_rank(rows):
    """Low pegRatio ranks better; negative or zero PEG ranked worst, not
    treated as "cheap"."""
    return rank_ascending(
        rows,
        lambda d: to_float(d.get("pegRatio")) if (to_float(d.get("pegRatio")) or 0) > 0 else None,
    )


# ---------------------------------------------------------------------- #
#  Quality                                                                 #
# ---------------------------------------------------------------------- #
def _sector_avg_debt_to_equity(rows):
    """MEDIAN non-negative debtToEquity per sector, across rows that have
    both a sector and a non-negative debtToEquity (negative debtToEquity,
    i.e. negative shareholder equity, is excluded so one distressed company
    doesn't skew its sector's baseline). Median, not mean -- same
    outlier-robustness reasoning as _sector_avg_forward_pe above: a single
    heavily-levered name in a thin sector can pull a mean far higher than
    what's typical, the same way it does for forwardPE."""
    values = {}
    for _, d in rows:
        sector = d.get("sector")
        de = to_float(d.get("debtToEquity"))
        if sector and de is not None and de >= 0:
            values.setdefault(sector, []).append(de)
    return {sector: statistics.median(vals) for sector, vals in values.items()}


def debt_rank(rows):
    """Low debtToEquity relative to its sector's average ranks better.
    Negative debtToEquity comes from negative shareholder equity
    (financial distress), not "low debt"; ranked worst, same treatment as
    negative pegRatio."""
    debt_sector_avg = _sector_avg_debt_to_equity(rows)

    def key(d):
        sector = d.get("sector")
        de = to_float(d.get("debtToEquity"))
        avg = debt_sector_avg.get(sector)
        if de is None or de < 0 or not sector or avg is None:
            return None
        return de - avg

    return rank_ascending(rows, key)


def liquidity_rank(rows):
    """Average of high quickRatio and high currentRatio ranks; missing
    ranked worst."""
    quick_ranks = rank_ascending(rows, high_is_better_key("quickRatio"))
    current_ranks = rank_ascending(rows, high_is_better_key("currentRatio"))
    return {symbol: (quick_ranks[symbol] + current_ranks[symbol]) / 2 for symbol, _ in rows}


def roe_rank(rows):
    """High returnOnEquity ranks better. Negative ROE means negative net
    income relative to equity — a qualitatively worse signal than "low
    positive" ROE, same treatment growth_rank gives negative
    revenueGrowth."""
    return rank_ascending(rows, neg_if_positive("returnOnEquity"))


def margin_rank(rows):
    """Equally-weighted average of high profitMargins, high
    operatingMargins, and high grossMargins ranks. Negative margins mean
    the company is losing money on an operating or net basis -- a
    qualitatively worse signal than "low positive" margins, same
    neg_if_positive treatment as revenueGrowth/ROE. profitMargins is the
    all-costs-in bottom line; operatingMargins isolates the core business
    from financing/tax noise; grossMargins isolates it further still,
    before operating expenses (SG&A, R&D) -- three different points along
    the income statement, so a name can look fine on one and weak on
    another (e.g. strong gross margin eaten up by heavy opex spend shows
    up as a gap between grossMargins and operatingMargins specifically)."""
    profit_margin_ranks = rank_ascending(rows, neg_if_positive("profitMargins"))
    operating_margin_ranks = rank_ascending(rows, neg_if_positive("operatingMargins"))
    gross_margin_ranks = rank_ascending(rows, neg_if_positive("grossMargins"))
    return {
        symbol: (profit_margin_ranks[symbol] + operating_margin_ranks[symbol] + gross_margin_ranks[symbol]) / 3
        for symbol, _ in rows
    }


def eps_volatility_rank(rows):
    """Low epsVolatility ranks better; missing ranked worst. epsVolatility
    (see IBApp._eps_volatility) is stdev/mean(|value|) of the last (up
    to) 5 years' annual Diluted EPS -- always >= 0, so no negative-value
    special case is needed the way margin_rank/roe_rank/growth_rank each
    need one; same plain "low is better, never negative" shape as
    trailing_ps_rank. A distinct quality/predictability signal from
    eps_trend_rank (which reads consensus ESTIMATE revisions, forward-
    looking) -- this instead reads how much a company's own REPORTED
    earnings have actually swung year to year, backward-looking."""
    return rank_ascending(rows, lambda d: to_float(d.get("epsVolatility")))


# ---------------------------------------------------------------------- #
#  Growth & momentum                                                      #
# ---------------------------------------------------------------------- #
# +300%; triple-digit YoY growth is already exceptional -- see IBApp.py's
# comment above MARGIN_FLOOR/MARGIN_CAP for why a near-zero-revenue name's
# growth ratio needs capping at all (same base-effect problem, JOBY-style).
# Clamped here, for ranking only -- IBApp.py stores revenueGrowth raw and
# uncapped, so the screener still shows the actual number; only the value
# rank_ascending sees below is capped.
GROWTH_CAP = 3.0


def growth_rank(rows):
    """High revenueGrowth ranks better; negative growth ranked worst, not
    just low. Capped at GROWTH_CAP before ranking so a near-zero-revenue
    base-effect artifact (a ratio that reads in the thousands of percent)
    can't claim the single best rank ahead of a company with a real,
    still-exceptional growth number -- see GROWTH_CAP's own comment.
    revenueGrowth itself is dilution-adjusted at the source (see
    IBApp._revenue_per_share_growth) rather than Yahoo's raw total-company
    ratio -- restated per-share so growth bought with newly issued shares
    (an all-stock acquisition) doesn't count the same as organic growth.
    That adjusted-but-uncapped value is untouched everywhere else (the row
    dict, forward_pe.csv, sorted_screen.csv, the screener UI); only the
    number fed into rank_ascending here is clamped."""
    def key(d):
        value = to_float(d.get("revenueGrowth"))
        return -min(value, GROWTH_CAP) if value is not None and value > 0 else None
    return rank_ascending(rows, key)


# momentum_rank's "sweet spot" curve for the daily Money Flow Index/RSI
# strength reading (see IBApp._money_flow_index/_relative_strength_index,
# both bounded [0, 100]) -- (value, rank) breakpoints, 0=best/1=worst,
# linearly interpolated between. Deliberately NOT a population-relative
# percentile the way every other *_rank function here uses
# rank_ascending: MFI/RSI is already a fixed, standardized 0-100 scale
# with universally-understood reference points (25 = oversold, 75 =
# overbought -- explicit instruction, tightened from the initial 30/80),
# so re-deriving "good" from THIS universe's current distribution would
# throw that fixed meaning away for no benefit.
# Peaks at 60, not 50 -- gives more room on the strong side before the
# overbought penalty kicks in, deliberately asymmetric, consistent with
# the live backtest earlier confirming momentum is a CONTINUATION signal
# (strong stays strong more often than it reverses) -- while still
# docking real credit for a blow-off-top-style extreme past 75, which is
# the actual behavior change from the old plain "high is better" regression-
# momentum factor this replaces.
_MOMENTUM_SWEET_SPOT = [(0, 0.8), (25, 0.5), (60, 0.0), (75, 0.5), (100, 0.9)]


def _sweet_spot_rank(value, breakpoints):
    """Piecewise-linear interpolation of `value` against a sorted list of
    (x, rank) breakpoints -- clamped to the first/last breakpoint's own
    rank outside that range (only matters for a value sitting exactly at
    the scale's own bounds, since MFI/RSI can't go outside [0, 100])."""
    if value <= breakpoints[0][0]:
        return breakpoints[0][1]
    if value >= breakpoints[-1][0]:
        return breakpoints[-1][1]
    for (x0, r0), (x1, r1) in zip(breakpoints, breakpoints[1:]):
        if x0 <= value <= x1:
            fraction = (value - x0) / (x1 - x0)
            return r0 + fraction * (r1 - r0)
    return breakpoints[-1][1]  # unreachable given the bounds checks above


def momentum_rank(rows):
    """Daily-timeframe Money Flow Index (or RSI on the yfinance-fallback
    tier -- see IBApp.get_momentum) scored via the fixed sweet-spot curve
    above, not a population-relative percentile -- see
    _MOMENTUM_SWEET_SPOT's own comment for why. Missing ranked worst,
    same convention as most other factors here. A pure daily-timeframe
    strength read, independent of mean_reversion_rank's hourly one below
    -- see that function's docstring for why they're two separate
    factors, not one blended number. Ranks the already-computed
    `momentum` field -- computing it in the first place (main.py's
    add_momentum) needs a live IBApp connection and writes
    price_history.json, so that stays in main.py rather than here."""
    result = {}
    for symbol, d in rows:
        value = to_float(d.get("momentum"))
        result[symbol] = 1.0 if value is None else _sweet_spot_rank(value, _MOMENTUM_SWEET_SPOT)
    return result


def mean_reversion_rank(rows):
    """Hourly-timeframe Money Flow Index (see IBApp._money_flow_index),
    bounded [0, 100] -- a direct linear read (rank = value / 100), NOT
    momentum_rank's sweet-spot curve: this factor's job is entry timing,
    not strength, so low (oversold) should always rank best and high
    (overbought) always worst, with no "moderate is best" hump the way
    daily strength has one. A stock already overbought on the hour is one
    you'd be chasing (bad timing for a new long), read against a long
    that's already held, one that may be due for a pullback (worth a
    look) -- see RecommendationsView.tsx's meanReversionOkForLong/
    meanReversionOkForShort and buildCloseReasons for exactly how each
    side reads this. Missing ranked NEUTRAL (0.5), not worst: unlike this
    file's other factors, "missing" here overwhelmingly means "outside IB
    Gateway's ~40%-of-universe hourly-bar coverage scope" (CANDLESTICK_TOP_N
    ranked/held tickers only, no fallback data source the way
    momentum_rank falls back to yfinance daily), not a real signal about
    the ticker -- scoring it as if it were the worst possible reading was
    wrong for roughly 60% of the universe."""
    result = {}
    for symbol, d in rows:
        value = to_float(d.get("meanReversion"))
        result[symbol] = 0.5 if value is None else value / 100
    return result


def eps_trend_rank(rows):
    """Average of high epsRevision0y and high epsRevision1y ranks;
    missing ranked worst. Each is the consensus EPS estimate's 30-day
    revision trend -- (current estimate - the estimate 30 days ago) /
    abs(30-days-ago estimate), for the current ("0y") and next ("+1y")
    fiscal year respectively (see IBApp.get_forward_pe /
    IBApp._eps_revision, from yfinance's get_eps_trend()). Positive means
    analysts have been raising the estimate over the last month (a
    bullish signal distinct from analyst_conviction_rank's point-in-time
    target-price/recommendation snapshot -- this one's a trend), negative
    means cuts. Averages the two periods' ranks, same "independently-
    meaningful sub-metrics on incompatible scales" pattern as
    margin_rank/liquidity_rank/short_interest_rank -- a ticker missing
    just one period still gets dragged toward (not all the way to) worst
    by that period's missing-ranked-worst contribution to the average,
    the same partial penalty every other averaged-rank factor here
    applies."""
    rev_0y_ranks = rank_ascending(rows, neg_perf("epsRevision0y"))
    rev_1y_ranks = rank_ascending(rows, neg_perf("epsRevision1y"))
    return {symbol: (rev_0y_ranks[symbol] + rev_1y_ranks[symbol]) / 2 for symbol, _ in rows}


# ---------------------------------------------------------------------- #
#  Analyst conviction                                                     #
# ---------------------------------------------------------------------- #
def analyst_conviction_rank(rows):
    """Average of high targetUpside, low recommendationMean, and low
    target-price dispersion ranks. targetUpside alone says "analysts
    expect it to rise"; recommendationMean asks whether they're also
    confident enough to call it a buy, since a mean target can look high
    just from a stale or thinly-covered outlier; dispersion --
    (targetHighPrice - targetLowPrice) / targetMeanPrice -- penalizes real
    disagreement about the outlook that the mean alone hides (e.g. a
    $83-$225 target range around a $110 stock). Negative upside, a 0 or
    missing recommendationMean, and a missing/inconsistent target triple
    are all ranked worst."""
    upside_ranks = rank_ascending(rows, neg_if_positive("targetUpside"))

    def recommendation_score(d):
        value = to_float(d.get("recommendationMean"))
        # 1 = strong buy, 5 = strong sell; 0 shows up for a couple of
        # thinly-covered tickers (1 analyst) and isn't a real position on
        # that scale, so it's treated as missing rather than "better than
        # strong buy". A continuous mean (e.g. 1.8 vs. 2.4, both "buy")
        # discriminates within a recommendationKey bucket that a mapped
        # categorical score can't.
        return value if value is not None and value > 0 else None

    recommendation_ranks = rank_ascending(rows, recommendation_score)

    def target_dispersion(d):
        # (high - low) / mean -- how much analysts disagree about where
        # this is going, relative to its own price level (so a $500 stock
        # with a $100 high-low spread isn't penalized the same as a $20
        # stock with the same dollar spread). high < low shouldn't
        # happen, but ranked worst rather than trusted if the data is
        # that inconsistent.
        high = to_float(d.get("targetHighPrice"))
        low = to_float(d.get("targetLowPrice"))
        mean = to_float(d.get("targetMeanPrice"))
        if high is None or low is None or mean is None or mean <= 0 or high < low:
            return None
        return (high - low) / mean

    dispersion_ranks = rank_ascending(rows, target_dispersion)
    return {
        symbol: (upside_ranks[symbol] + recommendation_ranks[symbol] + dispersion_ranks[symbol]) / 3
        for symbol, _ in rows
    }


# ---------------------------------------------------------------------- #
#  Short interest (contrarian)                                            #
# ---------------------------------------------------------------------- #
def load_short_interest_scores(short_interest_file, raw_data_file):
    """{ticker: {pctOfFloat, daysToCover, changePercent}}, blending
    FINRA's own biweekly settlement file (finra.fetch_short_interest --
    currentShortPositionQuantity/daysToCoverQuantity/changePercent) with
    raw_data.json's own floatShares (already on disk from the normal
    yfinance pass -- no separate fetch needed here) to turn FINRA's raw
    share count into the same percent-of-float scale short_interest_rank
    always ranked on. Deliberately not yfinance's own shortPercentOfFloat/
    shortRatio pair: cross-checked against a live example (Alcoa, AA) and
    confirmed yfinance's sharesShortPriorMonth matches FINRA's PRIOR
    settlement exactly -- yfinance only ever reflects the month-end
    settlement, silently skipping the mid-month one, so it's a strict
    subset of what FINRA itself publishes twice as often. changePercent
    -- FINRA's own period-over-period % change in short interest -- has
    no yfinance equivalent at all; it's a genuinely new signal (whether
    the short build is accelerating or unwinding), not a fresher version
    of an existing one.

    A ticker missing from FINRA's file (thinly-shorted enough not to be
    reported) or missing floatShares (no raw_data.json entry at all) is
    left out of the returned map -- short_interest_rank already ranks a
    missing score worst, same treatment as every other factor's missing
    data."""
    try:
        with open(short_interest_file) as f:
            finra = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        finra = {}
    try:
        with open(raw_data_file) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        raw = {}

    scores = {}
    for ticker, row in finra.items():
        float_shares = to_float(raw.get(ticker, {}).get("floatShares"))
        current = to_float(row.get("currentShortPositionQuantity"))
        pct_of_float = current / float_shares if current is not None and float_shares else None
        scores[ticker] = {
            "pctOfFloat": pct_of_float,
            "daysToCover": to_float(row.get("daysToCoverQuantity")),
            "changePercent": to_float(row.get("changePercent")),
        }
    return scores


def short_interest_rank(rows, short_interest_scores):
    """Average of high pct-of-float, high days-to-cover, and high
    change-percent ranks (see load_short_interest_scores for where all
    three come from); missing ranked worst. Deliberately the opposite
    direction of every other factor here -- this is a contrarian/squeeze-
    potential bet (the more a stock is shorted, and the faster that
    position is BUILDING, the better it scores), not a quality signal.
    pctOfFloat (short interest normalized to tradable float) is the
    standard, cross-company-comparable short-interest metric; daysToCover
    captures how much actual squeeze pressure that short interest carries
    -- a stock that's heavily shorted but easy to unwind in an afternoon
    is a weaker setup than one that'd take many days of average volume to
    cover; changePercent adds a momentum read neither of the other two
    has on its own -- a short position still building (positive
    changePercent) is a stronger contrarian setup than one already
    unwinding (negative), even at the same current level. Averaging
    ranks (not raw values, which are on incompatible scales) blends all
    three dimensions."""
    augmented = [(symbol, {**d, **(short_interest_scores.get(symbol) or {})}) for symbol, d in rows]
    pct_float_ranks = rank_ascending(augmented, high_is_better_key("pctOfFloat"))
    days_cover_ranks = rank_ascending(augmented, high_is_better_key("daysToCover"))
    change_pct_ranks = rank_ascending(augmented, high_is_better_key("changePercent"))
    return {
        symbol: (pct_float_ranks[symbol] + days_cover_ranks[symbol] + change_pct_ranks[symbol]) / 3
        for symbol, _ in rows
    }


# ---------------------------------------------------------------------- #
#  Composite score                                                         #
# ---------------------------------------------------------------------- #
# Which curated `sector` values (see main.py's load_sectors/symbols.json)
# get the Financials formula below instead of the standard one --
# exactly the set web/src/sectorGroups.js's own EXACT map sends to
# "Financial Services" (that file's own comment: a display/filter
# grouping, kept separate from the granular value scoring/sector-
# relative P/E stay keyed on) -- duplicated here rather than shared
# since one's JS and one's Python; keep in sync by hand if that file's
# own Financial Services entries change. _FINANCIALS_KEYWORDS mirrors
# that same file's KEYWORD_FALLBACKS entries that resolve to Financial
# Services, as a substring fallback for a curated industry not in the
# set below (e.g. one symbols.json hasn't been given the exact string
# for yet).
FINANCIALS_SECTORS = {
    "Asset Management",
    "Banks - Diversified",
    "Banks - Regional",
    "Capital Markets",
    "Credit Services",
    "Financial Conglomerates",
    "Financial Data & Stock Exchanges",
    "Insurance - Diversified",
    "Insurance - Life",
    "Insurance - Property & Casualty",
    "Insurance - Reinsurance",
    "Insurance - Specialty",
    "Insurance Brokers",
    "Mortgage Finance",
}
_FINANCIALS_KEYWORDS = ("bank", "insurance", "financial", "asset")


def is_financials_sector(sector):
    """True for a curated `sector` (really an industry -- see this
    module's own docstring) that belongs to the broad Financial Services
    group -- what score_rows uses to pick FINANCIALS_WEIGHTS over
    STANDARD_WEIGHTS. Exists because Yahoo Finance simply doesn't
    populate debtToEquity/quickRatio/currentRatio/enterpriseToEbitda/
    freeCashflow for this sector (confirmed live: even JPM/WFC show None
    for all five) -- a bank's balance sheet doesn't map onto the
    "current assets vs. liabilities"/EBITDA framework those fields
    assume, it's not a data-quality gap specific to smaller/obscure
    names. Since rank_ascending treats a missing value as the WORST
    rank, not neutral, every Financials ticker was structurally taking
    the worst score on debt_rank/liquidity_rank/ev_ebitda_rank/fcf_rank
    (17% of the composite) for a reason that has nothing to do with its
    actual fundamentals -- confirmed live via sorted_screen.csv: Banks -
    Regional was 11.4% of the universe but 16.8% of Strong Sell."""
    if not sector:
        return False
    if sector in FINANCIALS_SECTORS:
        return True
    lower = sector.lower()
    return any(k in lower for k in _FINANCIALS_KEYWORDS)


# Same idea as FINANCIALS_SECTORS above, for the broad Utilities group
# (sectorGroups.js's own EXACT map entries that resolve to "Utilities").
UTILITIES_SECTORS = {
    "Utilities - Diversified",
    "Utilities - Independent Power Producers",
    "Utilities - Regulated Electric",
    "Utilities - Regulated Gas",
    "Utilities - Regulated Water",
    "Utilities - Renewable",
}
_UTILITIES_KEYWORDS = ("utilit",)


def is_utilities_sector(sector):
    """True for a curated `sector` in the broad Utilities group -- what
    score_rows uses to pick UTILITIES_WEIGHTS over STANDARD_WEIGHTS.
    Unlike is_financials_sector, this isn't a missing-data problem --
    Yahoo reports quickRatio/currentRatio for utilities fine (confirmed
    live: 59/59 populated) -- it's that liquidity_rank compares the raw
    ratio against the WHOLE universe, not the ticker's own sector, the
    one thing debt_rank already does right for exactly this reason.
    Utilities structurally run quick/current ratios well under 1 (median
    ~0.4/0.8 vs. a ~1.2/1.8 universe median) because a regulated
    monopoly's cash flow is about as predictable as it gets -- there's
    no need to hoard working capital the way a business with uncertain
    revenue would, so a low ratio here isn't the distress signal it
    would be almost anywhere else. Confirmed live: liquidity_rank's own
    average percentile for utilities was 0.840 (0=best, 1=worst,
    0.5=neutral) -- the single worst-skewed factor of any checked,
    worse even than fcf_rank's 0.861 despite fcf_rank being a much
    murkier case (utilities' negative FCF is real heavy regulated capex,
    not obviously mis-scored the way a flat universe-wide liquidity
    comparison is) -- while every ticker still had real quickRatio/
    currentRatio data, just structurally sector-low ones."""
    if not sector:
        return False
    if sector in UTILITIES_SECTORS:
        return True
    lower = sector.lower()
    return any(k in lower for k in _UTILITIES_KEYWORDS)


# Same idea again, for the broad Real Estate group (sectorGroups.js's own
# EXACT map entries that resolve to "Real Estate").
REAL_ESTATE_SECTORS = {
    "REIT - Diversified",
    "REIT - Healthcare Facilities",
    "REIT - Hotel & Motel",
    "REIT - Industrial",
    "REIT - Mortgage",
    "REIT - Office",
    "REIT - Residential",
    "REIT - Retail",
    "REIT - Specialty",
    "Real Estate - Development",
    "Real Estate - Diversified",
    "Real Estate Services",
}
_REAL_ESTATE_KEYWORDS = ("reit", "real estate")


def is_real_estate_sector(sector):
    """True for a curated `sector` in the broad Real Estate group -- what
    score_rows uses to pick REAL_ESTATE_WEIGHTS over STANDARD_WEIGHTS
    (same shape as FINANCIALS_WEIGHTS -- see that column's own comment --
    except pe/fcf are left at their standard 5% each rather than also
    zeroing fcf out). Addresses the same missing-data problem
    is_financials_sector does, confirmed live for mortgage REITs (18
    tickers) specifically: 0/18 have enterpriseToEbitda or priceToFCF
    coverage from Yahoo at all (they're financial institutions holding
    mortgage-backed securities, not physical-property businesses), and
    ev_ebitda_rank's average percentile across them was 1.000 -- the
    single worst possible score, every time. Equity REITs separately run
    heavy non-cash real-estate depreciation through GAAP net income
    (the reason the industry itself reports FFO/AFFO instead of EPS),
    which understates roe_rank/pe_vs_trailing_rank and inflates
    peg_rank (confirmed live: peg_rank's own average across all of Real
    Estate was 0.830, the worst-skewed factor checked for this sector) --
    a real distortion, but a deliberately different one from the
    missing-data case above, and NOT addressed by REAL_ESTATE_WEIGHTS:
    explicit call not to build a third, more targeted formula for it."""
    if not sector:
        return False
    if sector in REAL_ESTATE_SECTORS:
        return True
    lower = sector.lower()
    return any(k in lower for k in _REAL_ESTATE_KEYWORDS)


# {factor key: (label, standard weight, Financials weight, Utilities
# weight, Real Estate weight)} -- single source of truth for score_rows'
# weighted sums below
# AND for ib_server.py's GET /api/scoring-formula (the Scoring tab's own
# table), so the page displaying "what the formula is" can never drift
# from what score_rows actually computes. Every weight column must sum
# to 1.00 -- there's no runtime assertion, same as this module's own
# original docstring note on the single-formula weights it replaces.
#
# The Financials column zeroes out debt/liquidity/ev_ebitda/fcf/margin
# (22% combined -- see is_financials_sector's own docstring for the
# debt/liquidity/ev_ebitda/fcf reasoning; margin is the same class of
# not-comparable-for-this-sector problem: a bank's "revenue" in the
# profitMargins/operatingMargins ratio is net interest income + fees, a
# structurally much smaller denominator than a retailer's gross revenue,
# so every bank looks like a margin outlier -- confirmed live: bank
# median profitMargins ~30%/operatingMargins ~43% vs. the rest of the
# universe's ~4%/~9%, not a real profitability edge) and redistributes
# that 22% onto eps_trend (+10%, standard 5% -> 15%) and growth (+2%,
# standard 8% -> 10%). Separately (not part of that 22%, funded by
# trimming pe and short_interest instead -- see below), sector_pe goes
# +10% (standard 5% -> 15%) and peg goes +3% (standard 5% -> 8%): pe on
# its own isn't sector-relative, and confirmed live it was doing most of
# the damage in an earlier version of this column -- Financials
# structurally trades at low ABSOLUTE P/E multiples market-wide (banks
# and insurers both averaged ~0.25 percentile on raw pe_rank vs. 0.50
# universe-wide), which isn't the same thing as being cheap for ITS OWN
# sector (sector_pe only read ~0.56-0.62 for the same names) -- boosting
# the sector-relative version instead of the universe-wide one, and
# adding peg (also comparable across sectors) funded by trimming
# short_interest (-3%, standard 8% -> 5%, since it isn't a
# comparability-across-sectors factor the way pe/margin/debt/etc. are),
# corrects that without re-introducing a missing-data problem.
#
# The Utilities column zeroes out liquidity (2% -- see
# is_utilities_sector's own docstring for why just this one, not the
# broader growth/momentum/PEG tilt also observed for this sector, which
# reads more like real signal than a bug) and redistributes that 2% onto
# sector_pe alone; fcf is zeroed out entirely (standard 5% -> 0%, same
# not-comparable-for-this-sector reasoning as Financials' fcf zeroing --
# a capital-intensive, heavily-regulated-capex sector's negative free
# cash flow isn't a real quality signal) with 2% of that landing on
# short_interest (standard 8% -> 10%) and the remaining 3% on eps_trend
# (standard 5% -> 8%, same "Yahoo reports this cleanly for the sector"
# reasoning Financials' own eps_trend boost uses).
#
# The Real Estate column is otherwise IDENTICAL to the ORIGINAL
# Financials treatment (same debt/liquidity/ev_ebitda/margin zeroing,
# same sector_pe/eps_trend/growth boosts) except pe and fcf are both
# left at their standard 5% instead of being touched at all -- explicit
# call: mortgage REITs share Financials' missing-EBITDA/FCF-data problem
# (see is_real_estate_sector's own docstring), but equity REITs don't
# share its P/E-vs-market or FCF-coverage story, so neither gets pushed
# to a Financials-specific extreme here. Real Estate was NOT revisited
# when Financials' pe/sector_pe/peg/short_interest were rebalanced above
# -- it never had pe/short_interest boosted or trimmed in the first
# place, so that specific distortion doesn't apply to it the same way.
#
# eps_volatility (see IBApp._eps_volatility/eps_volatility_rank) is a
# flat 5% in EVERY column, sector_pe trimmed 5% to fund it in every
# column too -- unlike the sector-specific reweights above, this one's
# universal: explicit call that low earnings volatility is a quality
# signal worth crediting everywhere, not just certain sectors. The
# three special columns' own sector_pe boosts (see their own comments
# just above, still described as relative deltas over the standard
# column) land on top of this trim, e.g. Financials' sector_pe is
# STANDARD_WEIGHTS["sector_pe"] (0.05) + 0.05 = 0.10, not the old 0.15.
FACTOR_WEIGHTS = {
    "pe": ("Forward P/E", 0.05, 0.05, 0.05, 0.05),
    "sector_pe": ("Forward P/E vs. sector average", 0.05, 0.15, 0.07, 0.10),
    "eps_volatility": ("Yearly EPS volatility", 0.05, 0.05, 0.05, 0.05),
    "fcf": ("Price/FCF", 0.05, 0.0, 0.0, 0.05),
    "ev_ebitda": ("EV/EBITDA", 0.05, 0.0, 0.05, 0.0),
    "momentum": ("Daily-timeframe strength (MFI/RSI)", 0.05, 0.05, 0.05, 0.05),
    "mean_reversion": ("Hourly-timeframe overbought/oversold (MFI)", 0.05, 0.05, 0.05, 0.05),
    "eps_trend": ("EPS-estimate revision trend", 0.05, 0.15, 0.08, 0.10),
    "analyst": ("Analyst conviction", 0.07, 0.07, 0.07, 0.07),
    "pe_vs_trailing": ("Forward P/E vs. Trailing P/E", 0.05, 0.05, 0.05, 0.05),
    "peg": ("PEG ratio", 0.05, 0.08, 0.05, 0.05),
    "trailing_ps": ("Trailing P/S", 0.02, 0.02, 0.02, 0.02),
    "growth": ("Revenue growth", 0.08, 0.10, 0.08, 0.10),
    "debt": ("Debt/equity vs. sector average", 0.05, 0.0, 0.05, 0.0),
    "liquidity": ("Quick/current ratio", 0.02, 0.0, 0.0, 0.0),
    "roe": ("Return on equity", 0.03, 0.03, 0.03, 0.03),
    "short_interest": ("Short interest (contrarian)", 0.08, 0.05, 0.10, 0.08),
    "sentiment": ("News/social/institutional sentiment", 0.05, 0.05, 0.05, 0.05),
    "insiders": ("Insider open-market buy/sell activity", 0.05, 0.05, 0.05, 0.05),
    "margin": ("Profit/operating margins", 0.05, 0.0, 0.05, 0.05),
}
STANDARD_WEIGHTS = {factor: v[1] for factor, v in FACTOR_WEIGHTS.items()}
FINANCIALS_WEIGHTS = {factor: v[2] for factor, v in FACTOR_WEIGHTS.items()}
UTILITIES_WEIGHTS = {factor: v[3] for factor, v in FACTOR_WEIGHTS.items()}
REAL_ESTATE_WEIGHTS = {factor: v[4] for factor, v in FACTOR_WEIGHTS.items()}


def score_rows(rows, sentiment_scores=None, insider_scores=None, short_interest_scores=None):
    """Composite score per (symbol, d) -- lower is better. Every ticker's
    rank on each factor is computed once, across the WHOLE universe in
    `rows` (see each factor function's own docstring for its ranking
    rule), then combined into one weighted sum using STANDARD_WEIGHTS.
    STANDARD_WEIGHTS includes eps_volatility_rank (5%, funded by trimming
    sector_relative_pe_rank 5% -- applies in every column below too, not
    just this one, see FACTOR_WEIGHTS' own comment) -- unlike the three
    sector-specific reweights that follow, this one's universal: low
    earnings volatility is a quality signal everywhere, not a Financials/
    Utilities/Real-Estate-specific concern. On top of that, a Financials-
    sector ticker (see is_financials_sector) uses FINANCIALS_WEIGHTS
    instead (debt_rank/liquidity_rank/ev_ebitda_rank/fcf_rank/margin_rank
    zeroed out, 22% redistributed onto eps_trend_rank/growth_rank --
    eps_trend_rank getting the lion's share, 15% vs. the standard 5%;
    separately, pe_rank is trimmed back to its standard 5% -- not sector-
    relative on its own, it was overstating how cheap Financials looks --
    with that weight plus a short_interest_rank trim landing on
    sector_relative_pe_rank and peg_rank instead, both properly
    comparable across sectors); a Utilities-
    sector ticker (see is_utilities_sector), which uses UTILITIES_WEIGHTS
    instead (liquidity_rank zeroed out with that 2% redistributed onto
    sector_relative_pe_rank, and fcf_rank zeroed out entirely with that
    5% split onto short_interest_rank and eps_trend_rank); or a Real-
    Estate-sector ticker (see is_real_estate_sector), which uses
    REAL_ESTATE_WEIGHTS instead -- identical to the ORIGINAL Financials
    treatment (debt/liquidity/ev_ebitda/margin zeroed, eps_trend/growth
    boosted) except pe_rank/fcf_rank are both left at their standard 5%,
    and this column was never revisited for Financials' later
    pe/sector_pe/peg/short_interest rebalance above. See FACTOR_WEIGHTS'
    own comment for the full reasoning on all three. The underlying rank
    computations
    themselves are identical in every case -- only which weight gets
    applied to which ticker differs, so e.g. a Real-Estate ticker's
    sector_pe percentile still reflects the whole market, not just
    other Real Estate names."""
    sentiment_scores = sentiment_scores or {}
    insider_scores = insider_scores or {}
    short_interest_scores = short_interest_scores or {}

    ranks_by_factor = {
        "pe": pe_rank(rows),
        "sector_pe": sector_relative_pe_rank(rows),
        "eps_volatility": eps_volatility_rank(rows),
        "fcf": fcf_rank(rows),
        "ev_ebitda": ev_ebitda_rank(rows),
        "momentum": momentum_rank(rows),
        "mean_reversion": mean_reversion_rank(rows),
        "eps_trend": eps_trend_rank(rows),
        "analyst": analyst_conviction_rank(rows),
        "pe_vs_trailing": pe_vs_trailing_rank(rows),
        "peg": peg_rank(rows),
        "trailing_ps": trailing_ps_rank(rows),
        "growth": growth_rank(rows),
        "debt": debt_rank(rows),
        "liquidity": liquidity_rank(rows),
        "roe": roe_rank(rows),
        "short_interest": short_interest_rank(rows, short_interest_scores),
        "sentiment": sentiment_rank(rows, sentiment_scores),
        "insiders": insiders_rank(rows, insider_scores),
        "margin": margin_rank(rows),
    }
    scored = []
    for symbol, d in rows:
        sector = d.get("sector")
        if is_financials_sector(sector):
            weights = FINANCIALS_WEIGHTS
        elif is_utilities_sector(sector):
            weights = UTILITIES_WEIGHTS
        elif is_real_estate_sector(sector):
            weights = REAL_ESTATE_WEIGHTS
        else:
            weights = STANDARD_WEIGHTS
        score = sum(ranks_by_factor[factor][symbol] * weight for factor, weight in weights.items())
        scored.append((symbol, d, score))
    return scored


# ---------------------------------------------------------------------- #
#  Rating -- what to do with the score once it's computed                 #
# ---------------------------------------------------------------------- #
# Zacks Rank's actual bucket shape (roughly 6/14/60/14/6), not equal
# quintiles -- a top-6% "Strong Buy" is a meaningfully selective badge,
# unlike a generic top-20% one. Thresholds are on percentile position (0 =
# best score, approaching 1 = worst); symmetric around the middle so
# Strong Buy and Strong Sell always come out to the same count (up to
# rounding by however many rows don't divide evenly).
RATING_THRESHOLDS = [
    (0.06, "Strong Buy"),
    (0.20, "Buy"),
    (0.80, "Hold"),
    (0.94, "Sell"),
]
RATING_WORST = "Strong Sell"
# Not a percentile bucket at all -- for the negative/non-positive-forwardPE
# tickers write_sorted_screen_csv appends without a score (see that
# function's own docstring for why they're excluded from scoring
# entirely). "NA" makes that explicit and filterable, rather than an
# empty string a filter UI would have to special-case separately from
# "no rating data at all".
RATING_NA = "NA"


def rating_for_percentile(pct):
    for threshold, label in RATING_THRESHOLDS:
        if pct < threshold:
            return label
    return RATING_WORST
