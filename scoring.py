"""scoring.py — every function that goes into sorted_screen.csv's composite
score: the shared ranking primitives, the enrichment steps that compute a
couple of indicators in the first place (targetUpside, LiqRatio), the
individual indicator/factor calculations, and score_rows itself, which
combines all of them with the weights below into one number per ticker.
Pulled out of main.py (which still owns the download pipeline and file I/O)
so the scoring logic has one home instead of being buried in a 230-line
function.

Weights (must sum to 1.00 — score_rows's own weighted sum is the only place
that's enforced, there's no runtime assertion):
   5% pe_rank                     10% sector_relative_pe_rank
   5% fcf_rank                     5% ev_ebitda_rank
   5% momentum_rank                5% mean_reversion_rank
   5% eps_trend_rank             7.5% analyst_conviction_rank
   5% pe_vs_trailing_rank          5% peg_rank
2.5% trailing_ps_rank             7.5% growth_rank
   5% debt_rank                  2.5% liquidity_rank
   5% roe_rank                     5% short_interest_rank
   5% sentiment_rank               5% insiders_rank
   5% margin_rank
See each function's own docstring for what "better" means for that factor,
and score_rows's docstring for the full picture.
"""

import json
import math


# ---------------------------------------------------------------------- #
#  Shared ranking primitives                                              #
# ---------------------------------------------------------------------- #
def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def rank_ascending(rows, value_fn):
    """Percentile rank (0 = best/lowest, 1 = worst) of rows by value_fn(d); rows
    where value_fn returns None get the worst rank."""
    valid = [(symbol, value_fn(d)) for symbol, d in rows if value_fn(d) is not None]
    valid.sort(key=lambda item: item[1])
    n = len(valid)
    ranks = {symbol: i / (n - 1) if n > 1 else 0 for i, (symbol, _) in enumerate(valid)}
    return {symbol: ranks.get(symbol, 1.0) for symbol, _ in rows}


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
# more in the sentiment blend below, same reasoning IBApp.py's
# GROWTH_CAP/MARGIN_CAP already use for revenueGrowth/operatingMargins.
INST_CHANGE_CLIP = 0.5


def load_sentiment_scores(social_sentiment_file, news_sentiment_file, institutional_holdings_file=None):
    """{ticker: score in [-1, 1]}, blending StockTwits social sentiment
    (social_sentiment_file, already -1..1 -- see social_sentiment.py),
    FinBERT news sentiment (news_sentiment_file, {ticker: {articleId:
    score}} with each score 1 (very bearish) - 5 (very bullish) --
    written by ib_price_server.py's news_loop), and institutional
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
    """[0, 1] score (0=best/all-buys, 1=worst/all-sells) directly from
    each ticker's raw open-market buy/sell ratio (see load_insider_scores
    -- (buys-sells)/(buys+sells), already bounded [-1, 1]), linearly
    rescaled via (1 - score) / 2; missing ranked worst. Deliberately NOT
    a population-relative percentile rank the way every other *_rank
    function here uses rank_ascending: insider open-market PURCHASES are
    rare industry-wide (confirmed on this universe's own Form4 data:
    ~78% of tickers with any open-market P/S activity have a raw ratio of
    exactly -1.0, i.e. literally zero buys), so a percentile rank ties
    that entire zero-buy majority to the worst rank and then lets a
    SINGLE stray buy against dozens of sells vault a ticker dramatically
    up the scale just for not being tied to that block -- confirmed live:
    LQDA (1 buy, 79 sells, raw ratio -0.976) ranked at the 78th
    percentile under the old rank_ascending treatment, reading as
    strongly bullish despite being 98.8% sells. The raw ratio is already
    a bounded, comparable value -- unlike momentum or the other unbounded
    factors rank_ascending exists to protect against one outlier
    crushing, it needs no percentile treatment at all, just a direct
    linear rescale into the same [0, 1] rank-space every other factor
    here reports."""
    return {symbol: (1 - insider_scores[symbol]) / 2 if symbol in insider_scores else 1.0 for symbol, _ in rows}


# ---------------------------------------------------------------------- #
#  Valuation                                                              #
# ---------------------------------------------------------------------- #
def pe_rank(rows):
    """Low forwardPE ranks better; missing ranked worst."""
    return rank_ascending(rows, lambda d: to_float(d.get("forwardPE")))


def _sector_avg_forward_pe(rows):
    """Average forwardPE per sector, across rows that have both a sector and a forwardPE."""
    sums, counts = {}, {}
    for _, d in rows:
        sector = d.get("sector")
        fwd_pe = to_float(d.get("forwardPE"))
        if sector and fwd_pe is not None:
            sums[sector] = sums.get(sector, 0) + fwd_pe
            counts[sector] = counts.get(sector, 0) + 1
    return {sector: sums[sector] / counts[sector] for sector in sums}


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
    """Average non-negative debtToEquity per sector, across rows that have
    both a sector and a non-negative debtToEquity (negative debtToEquity,
    i.e. negative shareholder equity, is excluded so one distressed company
    doesn't skew its sector's baseline)."""
    sums, counts = {}, {}
    for _, d in rows:
        sector = d.get("sector")
        de = to_float(d.get("debtToEquity"))
        if sector and de is not None and de >= 0:
            sums[sector] = sums.get(sector, 0) + de
            counts[sector] = counts.get(sector, 0) + 1
    return {sector: sums[sector] / counts[sector] for sector in sums}


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
    """Average of high profitMargins and high operatingMargins ranks.
    Negative margins mean the company is losing money on an operating or
    net basis -- a qualitatively worse signal than "low positive" margins,
    same neg_if_positive treatment as revenueGrowth/ROE. profitMargins is
    the all-costs-in bottom line; operatingMargins isolates the core
    business from financing/tax noise, so a name can look fine on one and
    weak on the other."""
    profit_margin_ranks = rank_ascending(rows, neg_if_positive("profitMargins"))
    operating_margin_ranks = rank_ascending(rows, neg_if_positive("operatingMargins"))
    return {symbol: (profit_margin_ranks[symbol] + operating_margin_ranks[symbol]) / 2 for symbol, _ in rows}


# ---------------------------------------------------------------------- #
#  Growth & momentum                                                      #
# ---------------------------------------------------------------------- #
def growth_rank(rows):
    """High revenueGrowth ranks better; negative growth ranked worst, not
    just low."""
    return rank_ascending(rows, neg_if_positive("revenueGrowth"))


def momentum_rank(rows):
    """High momentum (regression-slope momentum score, divided by the
    annualized volatility of log returns -- the 3-month IB Gateway daily
    series where available, else the plain ~1-month yfinance calculation;
    see IBApp.get_momentum / _regression_momentum) ranks better; missing
    ranked worst. A pure daily-timeframe trend read, independent of
    mean_reversion_rank's hourly one below -- see that function's
    docstring for why they're two separate factors, not one blended
    number. Ranks the already-computed `momentum` field -- computing it
    in the first place (main.py's add_momentum) needs a live IBApp
    connection and writes price_history.json, so that stays in main.py
    rather than here."""
    return rank_ascending(rows, neg_perf("momentum"))


def mean_reversion_rank(rows):
    """Low mean_reversion ranks better; missing ranked worst.
    mean_reversion is the SAME regression-momentum formula as momentum_rank
    scores, just measured on the hourly IB Gateway series instead of the
    daily one (see IBApp.get_momentum) -- same sign convention as momentum
    (positive = hourly uptrend, negative = hourly downtrend), scored here
    with the opposite direction on purpose: a stock already trending up
    hard on the hourly timeframe is a stock this factor treats as being
    chased rather than caught early, so a LOW (negative-hourly-momentum,
    i.e. a stock that's just pulled back) reading ranks best, the mirror
    of momentum_rank's own daily-timeframe "high is better" direction.
    Kept as its own factor (rather than blended into momentum_rank as it
    originally was) so each timeframe's signal can be weighted, ranked,
    and reasoned about independently. Only populated for tickers IB
    Gateway has fetched hourly candlesticks for (CANDLESTICK_TOP_N
    ranked/held tickers, not the whole universe) -- there's no fallback
    data source for hourly bars the way momentum_rank falls back to
    yfinance daily, so this is missing far more often."""
    return rank_ascending(rows, lambda d: to_float(d.get("meanReversion")))


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
def short_interest_rank(rows):
    """Average of high shortRatio and high shortPercentOfFloat ranks;
    missing ranked worst. Deliberately the opposite direction of every
    other factor here -- this is a contrarian/squeeze-potential bet (the
    more a stock is shorted, the better it scores), not a quality signal.
    shortPercentOfFloat (short interest normalized to tradable float) is
    the standard, cross-company-comparable short-interest metric;
    shortRatio (days-to-cover) captures how much actual squeeze pressure
    that short interest carries -- a stock that's heavily shorted but easy
    to unwind in an afternoon is a weaker setup than one that'd take many
    days of average volume to cover. Averaging their ranks (not raw
    values, which are on incompatible scales) blends both dimensions."""
    short_ratio_ranks = rank_ascending(rows, high_is_better_key("shortRatio"))
    short_float_ranks = rank_ascending(rows, high_is_better_key("shortPercentOfFloat"))
    return {symbol: (short_ratio_ranks[symbol] + short_float_ranks[symbol]) / 2 for symbol, _ in rows}


# ---------------------------------------------------------------------- #
#  Composite score                                                         #
# ---------------------------------------------------------------------- #
def score_rows(rows, sentiment_scores=None, insider_scores=None):
    """Composite score per (symbol, d): 5% low forwardPE (pe_rank -- down
    from 10%, moved to eps_trend_rank below), 10% low forwardPE
    relative to its sector's average forwardPE (sector_relative_pe_rank),
    5% low priceToFCF (fcf_rank; negative or missing FCF treated as a
    fixed 200 for this factor only) + 5% low enterpriseToEbitda
    (ev_ebitda_rank; negative EBITDA ranked worst -- two independent
    cash-flow-valuation factors, not blended into one; see
    ev_ebitda_rank's docstring for why), 5% high daily-timeframe momentum
    (momentum_rank; missing ranked worst) + 5% low hourly-timeframe mean
    reversion (mean_reversion_rank; missing ranked worst -- a stock
    already trending up hard on the hour is being chased, not caught
    early, so LOW/negative hourly momentum ranks best here, the mirror of
    momentum_rank's own daily-timeframe direction; split out of
    momentum_rank, which used to blend both timeframes into one 10%
    factor; see that function's docstring for why they're independent
    now), 5% earnings estimate revision trend (eps_trend_rank --
    average of the current- and next-fiscal-year 30-day EPS-estimate
    revision ranks; missing ranked worst; taken out of pe_rank's weight),
    7.5% analyst conviction
    (analyst_conviction_rank -- down from 10%, the other 2.5% moved to
    insiders below), 5% forwardPE - trailingPE
    (pe_vs_trailing_rank; more negative is better, negative/infinite
    trailingPE ranked worst -- down from 10%), 5% low
    pegRatio (peg_rank; negative PEG ranked worst, not best), 2.5% low
    trailingPS (trailing_ps_rank; missing ranked worst -- a separate
    valuation lens from pe_rank/fcf_rank/ev_ebitda_rank, not blended with
    any of them, that stays meaningful for unprofitable/negative-FCF
    names those break down for; taken out of liquidity's weight below),
    7.5% high revenueGrowth (growth_rank; negative growth ranked worst,
    not just low -- down from 10%, moved to margins below), 5% low
    debtToEquity relative to its sector's average (debt_rank; negative or
    missing debtToEquity ranked worst), 2.5% liquidity (liquidity_rank;
    missing ranked worst -- down from 5%, moved to trailing_ps above),
    5% high returnOnEquity (roe_rank; negative ROE ranked
    worst, not just low), 5% short interest (short_interest_rank; missing
    ranked worst -- deliberately contrarian, see that function's
    docstring), 5% combined news + social + institutional-QoQ-share-change
    sentiment (sentiment_rank; see load_sentiment_scores -- taken out of
    forwardPE's own weight, previously 15%), 5% insider open-market
    buy/sell activity (insiders_rank; see load_insider_scores -- missing
    ranked worst; up from 2.5%, the other 2.5% taken out of margin_rank's
    weight below), 5% margins (margin_rank; negative margins ranked worst
    -- down from 7.5%, see insiders_rank above for where that 2.5% went).
    Lower score is better. Every individual factor function above has the
    exact ranking rule in its own docstring."""
    sentiment_scores = sentiment_scores or {}
    insider_scores = insider_scores or {}

    weighted_ranks = {
        "pe": (pe_rank(rows), 0.05),
        "sector_pe": (sector_relative_pe_rank(rows), 0.10),
        "fcf": (fcf_rank(rows), 0.05),
        "ev_ebitda": (ev_ebitda_rank(rows), 0.05),
        "momentum": (momentum_rank(rows), 0.05),
        "mean_reversion": (mean_reversion_rank(rows), 0.05),
        "eps_trend": (eps_trend_rank(rows), 0.05),
        "analyst": (analyst_conviction_rank(rows), 0.075),
        "pe_vs_trailing": (pe_vs_trailing_rank(rows), 0.05),
        "peg": (peg_rank(rows), 0.05),
        "trailing_ps": (trailing_ps_rank(rows), 0.025),
        "growth": (growth_rank(rows), 0.075),
        "debt": (debt_rank(rows), 0.05),
        "liquidity": (liquidity_rank(rows), 0.025),
        "roe": (roe_rank(rows), 0.05),
        "short_interest": (short_interest_rank(rows), 0.05),
        "sentiment": (sentiment_rank(rows, sentiment_scores), 0.05),
        "insiders": (insiders_rank(rows, insider_scores), 0.05),
        "margin": (margin_rank(rows), 0.05),
    }
    scored = []
    for symbol, d in rows:
        score = sum(ranks[symbol] * weight for ranks, weight in weighted_ranks.values())
        scored.append((symbol, d, score))
    return scored


# ---------------------------------------------------------------------- #
#  Rating -- what to do with the score once it's computed                 #
# ---------------------------------------------------------------------- #
# Zacks Rank's actual bucket shape (roughly 5/15/60/15/5), not equal
# quintiles -- a top-5% "Strong Buy" is a meaningfully selective badge,
# unlike a generic top-20% one. Thresholds are on percentile position (0 =
# best score, approaching 1 = worst); symmetric around the middle so
# Strong Buy and Strong Sell always come out to the same count (up to
# rounding by however many rows don't divide evenly).
RATING_THRESHOLDS = [
    (0.05, "Strong Buy"),
    (0.20, "Buy"),
    (0.80, "Hold"),
    (0.95, "Sell"),
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
