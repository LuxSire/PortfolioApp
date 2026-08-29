"""portfolio_optimizer.py — Sharpe-maximising long/short portfolio selector.

OVERVIEW
--------
Selects a 20-long + 20-short equity portfolio from the universe of rated
candidates, maximising the portfolio Sharpe ratio while explicitly penalising
sector concentration. The optimizer runs fully in Python (no network calls)
and writes data/output/target_portfolio.json, which TargetView.tsx simply
loads and displays.

INPUTS (both must be fresh before running this module)
------------------------------------------------------
  data/output/recommendations.json   ratings, screener scores, analyst data
  data/output/simulations.json       confidence-weighted EPS-DCF forecast
                                     returns, beta, sector, simulation
                                     probabilities

ALGORITHM
---------
1. PRE-FILTER
   Longs  : Strong Buy / Buy  AND forecastReturn > 0
   Shorts : Strong Sell / Sell AND forecastReturn < 0

2. COMPOSITE CANDIDATE SCORE  (4 equally-weighted rank-percentile signals)
   For each candidate compute a quality score independent of correlation:
     a. Individual Sharpe  = (positionReturn − rf) / vol
     b. Screener rank      = − scorePercentile  (lower pct = better screener rank)
     c. Rating strength    = Strong Buy/Sell → 1.0, Buy/Sell → 0.5  (already
                             filtered to rated candidates; this promotes conviction)
     d. Short interest     = shortInterest for a long, −shortInterest for a short
                             -- high short interest favors a long (squeeze/contrarian
                             upside against an already-crowded short) and penalizes a
                             short (piling onto a crowded trade is squeeze risk, not
                             thesis confirmation)
   Each signal is rank-percentile-normalised within the pool (0=worst, 1=best),
   then the four percentiles are averaged.

   Pre-screen: keep top CANDIDATE_POOL (default 160) per side by composite
   score before entering the optimiser.

3. COVARIANCE MATRIX  (factor model — no historical return data required)
   Total vol    : σ_i = clamp(|β_i|, 0.75, 2.0) × MARKET_VOL  → [15%, 40%]
   Market covar : cov_market(i,j) = β_i × β_j × MARKET_VOL²  (using clamped β)
   Sector floor : if sector_i == sector_j:
                      corr(i,j) = max(market_corr(i,j), SECTOR_CORR)
   Final covar  : cov(i,j) = corr(i,j) × σ_i × σ_j

   SECTOR_CORR = 0.65 ensures that two stocks in the same sector are treated
   as at least 65 % correlated, strongly discouraging cluster selection
   (4 semiconductors or 5 REITs in the same leg).

4. GREEDY MAX-SHARPE SELECTION
   Forward-selection: at each step add the candidate whose inclusion most
   increases the equal-weight portfolio Sharpe (expected return / portfolio
   vol). Runs in O(POOL × N²) — ≈ 400 000 evaluations for pool=160, N=50;
   completes in well under a second. The 2nd/3rd-best candidate
   evaluated at that same step (i.e. what would have been added instead,
   had the winner not been available — typically a correlated name from
   the same sector, crowded out by SECTOR_CORR) is kept as that position's
   "alternates" for the frontend.

5. PORTFOLIO STATS
   Equal-weight 1/50 per position within each leg (each leg 100% gross,
   200% gross total, dollar-neutral). Combined portfolio stats use the
   full 100-position covariance matrix (long-short correlation included):
     portReturn = meanLong + mean(−shortReturn)   (1.0 weight per leg)
     portVol    = sqrt(w^T Σ w)  where Σ is the 100×100 covariance matrix
     Sharpe     = (portReturn − rf) / portVol
     Sortino    ≈ Sharpe × √2  (half-normal downside vol approximation)

CONSTANTS
---------
  MARKET_VOL   = 0.20   S&P 500 annualised vol proxy
  RF           = 0.035  risk-free rate (same as PortfolioView)
  SECTOR_CORR  = 0.65   same-sector correlation floor
  CANDIDATE_POOL = 160  pre-screen pool size per side
  POSITIONS    = 50     final positions selected per side
  IDIO_VOL     = 0.25   idiosyncratic vol used in CAPM fallback (when no history)
  SHRINKAGE    = 0.10   toward-diagonal shrinkage applied to sample covariance
  MIN_HIST_BARS = 20    minimum daily bars required to use a ticker's history
  HIST_WINDOW  = 60     use last N daily bars per ticker
  ANNUALIZE    = 252    trading days per year
"""

import csv
import json
import math
import os
import datetime

import numpy as np

from modules.sector_groups import get_sector_group

# ── constants ────────────────────────────────────────────────────────────────
MARKET_VOL = 0.20
RF = 0.035
BETA_FLOOR = 0.75   # clamp low-beta stocks: prevents artificial Sharpe inflation
BETA_CAP   = 2.0    # clamp high-beta stocks: prevents extreme vol estimates
# vol(i) = clamp(|beta_i|, BETA_FLOOR, BETA_CAP) × MARKET_VOL  → range [15%, 40%]
SECTOR_CORR = 0.65     # same-sector correlation floor (anti-concentration)
CANDIDATE_POOL = 160   # top N pre-screened per side before the greedy pass
POSITIONS = 50         # final portfolio size per side
IDIO_VOL = 0.25        # idiosyncratic vol added to diagonal in CAPM fallback
SHRINKAGE = 0.10       # toward-diagonal shrinkage applied to sample covariance
MIN_HIST_BARS = 20     # minimum daily bars required to use historical returns
HIST_WINDOW = 60       # use last N daily bars per ticker
ANNUALIZE = 252        # trading days per year

_HIST_IB_FILE = os.path.join("data", "IB", "price_history_daily_3mo.json")
_HIST_YF_FILE = os.path.join("data", "yfinance", "price_history.json")

# Hard overbought/oversold gate on the Long/Short pools, matching
# RecommendationsView.tsx's own MOMENTUM_OVERBOUGHT/OVERSOLD and
# MEAN_REVERSION_OVERBOUGHT/OVERSOLD zone thresholds exactly (MSI/ST-MSI
# in that page's own column labels -- "mom"/"mr" here, same raw [0, 100]
# values, no rescaling). Explicit instruction: don't let the optimizer
# select an already-overbought stock for the Long side or an already-
# oversold stock for the Short side, regardless of how well it otherwise
# scores -- a stock at a momentum/mean-reversion extreme in the wrong
# direction for its side is exactly the "chasing the move" entry the
# Recommendations page's own eligibleToBuy/eligibleToSell gates already
# warn against for live trade signals, and the target portfolio shouldn't
# recommend building a fresh position into that same risk. Missing mom/mr
# data does NOT exclude a candidate -- only an actual extreme reading
# does.
MOMENTUM_OVERBOUGHT = 70
MOMENTUM_OVERSOLD = 30
MEAN_REVERSION_OVERBOUGHT = 80
MEAN_REVERSION_OVERSOLD = 20

# Hard crowded-short gate on the Short pool, matching RecommendationsView.tsx's
# own MAX_SHORT_INTEREST / notCrowded gate: a name already shorted by more
# than this fraction of its float is a squeeze-risk trade the optimizer must
# not build a fresh short into, no matter how well it otherwise scores.
# shortInterest is the FINRA-preferred pct-of-float _make_candidate resolves
# (shortPctOfFloatFinra, else the recomputed shortPercentOfFloat). Missing
# short-interest data does NOT exclude a candidate -- only an actual reading
# above the bar does.
MAX_SHORT_INTEREST = 0.10

LONG_RATINGS = {"Strong Buy", "Buy"}
SHORT_RATINGS = {"Strong Sell", "Sell"}


# ── helpers ──────────────────────────────────────────────────────────────────

def _rank_percentile(values):
    """Rank-normalise a list of floats to [0, 1]. None → 0 (worst)."""
    vals = [v if v is not None else -math.inf for v in values]
    n = len(vals)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: vals[i])
    result = [0.0] * n
    for rank, idx in enumerate(order):
        result[idx] = rank / max(n - 1, 1)
    return result


def _rank_to_100(values):
    """Rank-rescale a list to [-100, 100]. Replicates screenerFactors.js rankTo100.
    Ties receive the average rank. None values stay None."""
    n = len(values)
    valid = [(v, i) for i, v in enumerate(values) if v is not None]
    valid.sort(key=lambda x: x[0])
    m = len(valid)
    result = [None] * n
    i = 0
    while i < m:
        j = i
        while j + 1 < m and valid[j + 1][0] == valid[i][0]:
            j += 1
        avg_rank = (i + j) / 2
        scaled = (avg_rank / (m - 1)) * 200 - 100 if m > 1 else 0.0
        for k in range(i, j + 1):
            result[valid[k][1]] = round(scaled, 4)
        i = j + 1
    return result


def _avg_news_sentiment(articles):
    """Replicate screenerFactors.js avgNewsSentiment.
    Returns avg_score − 3 (centered: 3 = neutral → 0), or None."""
    if not articles:
        return None
    scores = [s for s in articles.values() if s != 3]
    if not scores:
        return None
    return sum(scores) / len(scores) - 3.0


def _avg_insider_score(filings):
    """Replicate screenerFactors.js avgInsiderScore.
    Returns (buys−sells)/(buys+sells) × 100, or None."""
    if not filings:
        return None
    buys = sells = 0
    for filing in filings:
        for tx in filing.get("transactions", []):
            code = tx.get("code")
            if code == "P":
                buys += 1
            elif code == "S":
                sells += 1
    total = buys + sells
    return round((buys - sells) / total * 100, 4) if total else None


_SCREENER_CSV   = os.path.join("data", "output", "sorted_screen.csv")
_SOCIAL_SENT    = os.path.join("data", "social_sentiment.json")
_NEWS_SENT      = os.path.join("data", "output", "news_sentiment.json")
_INST_HOLDINGS  = os.path.join("data", "sec", "13f", "institutional_holdings.json")
_INSIDER_TXNS   = os.path.join("data", "sec", "form4", "insider_transactions.json")


def _load_screener_signals():
    """Load MSI/ST-MSI/Sentiment/News/InstChange/Insiders for every screened ticker.

    * mom / mr   — raw [0, 100] momentum / mean-reversion index (no rescaling).
    * sent / newsSent / instChange — rank-rescaled to [-100, 100] against the
      full screener population, exactly as ScreenerView.tsx does on the frontend.
    * insiders — raw (buys−sells)/(buys+sells) × 100, bounded [-100, 100].

    Returns {ticker: {mom, mr, sent, newsSent, instChange, insiders}} or {}.
    """
    # ── 1. Load raw values from each source ──────────────────────────────────
    screen_mom: dict[str, float | None] = {}
    screen_mr:  dict[str, float | None] = {}
    if os.path.exists(_SCREENER_CSV):
        with open(_SCREENER_CSV, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                t = row.get("ticker", "").strip()
                if not t:
                    continue
                def _fn(k):
                    try: return float(row[k]) if row.get(k) not in (None, "", "nan") else None
                    except (ValueError, KeyError): return None
                screen_mom[t] = _fn("momentum")
                screen_mr[t]  = _fn("meanReversion")
    tickers = list(screen_mom.keys())

    social: dict[str, float | None] = {}
    if os.path.exists(_SOCIAL_SENT):
        with open(_SOCIAL_SENT) as f:
            ss = json.load(f)
        for t, v in ss.items():
            s = v.get("score")
            social[t] = (s * 100 - 50) if s is not None else None  # [0,1]→[-50,+50]

    news_raw: dict[str, float | None] = {}
    if os.path.exists(_NEWS_SENT):
        with open(_NEWS_SENT) as f:
            ns = json.load(f)
        for t, articles in ns.items():
            news_raw[t] = _avg_news_sentiment(articles)

    inst_raw: dict[str, float | None] = {}
    if os.path.exists(_INST_HOLDINGS):
        with open(_INST_HOLDINGS) as f:
            ih = json.load(f)
        for t, v in ih.items():
            inst_raw[t] = v.get("pctShareChangeQoQ")

    insider_scores: dict[str, float | None] = {}
    if os.path.exists(_INSIDER_TXNS):
        with open(_INSIDER_TXNS) as f:
            it = json.load(f)
        for t, filings in it.items():
            insider_scores[t] = _avg_insider_score(filings)

    # ── 2. Rank-rescale sent/newsSent/instChange across screener population ──
    sent_vals   = [social.get(t)   for t in tickers]
    news_vals   = [news_raw.get(t) for t in tickers]
    inst_vals   = [inst_raw.get(t) for t in tickers]
    sent_scaled   = _rank_to_100(sent_vals)
    news_scaled   = _rank_to_100(news_vals)
    inst_scaled   = _rank_to_100(inst_vals)

    # ── 3. Build per-ticker dict ──────────────────────────────────────────────
    signals: dict[str, dict] = {}
    for i, t in enumerate(tickers):
        signals[t] = {
            "mom":        screen_mom.get(t),
            "mr":         screen_mr.get(t),
            "sent":       sent_scaled[i],
            "newsSent":   news_scaled[i],
            "instChange": inst_scaled[i],
            "insiders":   insider_scores.get(t),
        }
    return signals


def _composite_score(candidates, side):
    """Compute composite quality score for each candidate (list of dicts).

    Four equally-weighted rank-percentile signals:
      a. Individual Sharpe  — risk-adjusted forecast return
      b. Screener rank      — scorePercentile (lower = better for longs)
      c. Rating strength    — Strong Buy/Sell = 1.0, Buy/Sell = 0.5
      d. Short interest     — favors high short interest for a long
         (squeeze/contrarian upside against an already-crowded short)
         and penalizes it for a short (piling onto a crowded trade is
         squeeze risk, not confirmation of the thesis)
    """
    # Signal a: individual Sharpe
    sharpe_pct = _rank_percentile([c["indivSharpe"] for c in candidates])

    # Signal b: screener rank (lower percentile = better for longs)
    if side == "Long":
        screen_raw = [-(c["scorePercentile"] or 100) for c in candidates]
    else:
        screen_raw = [(c["scorePercentile"] or 0) for c in candidates]
    screen_pct = _rank_percentile(screen_raw)

    # Signal c: rating strength — Strong Buy/Sell scores higher than Buy/Sell
    strong = {"Strong Buy", "Strong Sell"}
    rating_raw = [1.0 if c.get("rating") in strong else 0.5 for c in candidates]
    rating_pct = _rank_percentile(rating_raw)

    # Signal d: short interest — high short interest ranks BEST for a long
    # (raw = shortInterest itself) and WORST for a short (raw =
    # -shortInterest, so low short interest ranks best instead). Missing
    # data ranks worst on either side via _rank_percentile's own None
    # handling, same "no data, ranks worst" convention scorePercentile's
    # own missing-value default above already follows.
    if side == "Long":
        short_int_raw = [c.get("shortInterest") for c in candidates]
    else:
        short_int_raw = [-c["shortInterest"] if c.get("shortInterest") is not None else None for c in candidates]
    short_int_pct = _rank_percentile(short_int_raw)

    scores = []
    for i in range(len(candidates)):
        scores.append((sharpe_pct[i] + screen_pct[i] + rating_pct[i] + short_int_pct[i]) / 4)
    return scores


# ── historical returns ───────────────────────────────────────────────────────

def _load_daily_returns():
    """Load the most recent HIST_WINDOW daily log-returns per ticker.

    Prefers IB 3-month daily bars (price_history_daily_3mo.json, ~62 bars)
    and falls back to yfinance weekly closes (price_history.json) for tickers
    not covered by IB. Returns {ticker: [log_return, ...]} for tickers with
    at least MIN_HIST_BARS observations.
    """
    try:
        with open(_HIST_IB_FILE) as f:
            ib = json.load(f)
    except FileNotFoundError:
        ib = {}
    try:
        with open(_HIST_YF_FILE) as f:
            yf = json.load(f)
    except FileNotFoundError:
        yf = {}

    # IB overrides yfinance for tickers present in both
    merged = {**yf, **ib}
    result = {}
    for ticker, bars in merged.items():
        closes = [b["close"] for b in bars[-HIST_WINDOW:] if b.get("close") is not None]
        if len(closes) < MIN_HIST_BARS:
            continue
        result[ticker] = [math.log(closes[i] / closes[i - 1])
                          for i in range(1, len(closes))]
    return result


# ── covariance ────────────────────────────────────────────────────────────────

def _build_cov(pool, hist_returns=None):
    """Build annualised covariance matrix.

    When hist_returns is provided and covers ≥ 50 % of the pool, uses the
    sample covariance of daily log-returns (ANNUALIZE × sample cov) with
    toward-diagonal shrinkage (SHRINKAGE) for numerical stability.  A small
    positive ridge is added if any eigenvalue is non-positive.

    Tickers without history are handled by CAPM cross-covariance:
      cov_ij = beta_i × beta_j × MARKET_VOL²
    and CAPM + idiosyncratic variance on the diagonal:
      cov_ii = (beta_i × MARKET_VOL)² + IDIO_VOL²

    Same-sector correlation floor (SECTOR_CORR) is applied for all off-
    diagonal pairs when no history is used for that pair.
    """
    n = len(pool)
    tickers = [c["ticker"] for c in pool]
    betas = np.array([c["beta"] for c in pool])   # already clamped
    sectors = [c.get("sector") or "" for c in pool]

    available = {t: hist_returns[t] for t in tickers
                 if hist_returns and t in hist_returns}
    coverage = len(available) / n

    if coverage >= 0.5:
        # ── sample covariance path ───────────────────────────────────────────
        min_len = min(len(v) for v in available.values())
        ret_matrix = np.array([
            available[t][-min_len:] if t in available else [0.0] * min_len
            for t in tickers
        ])
        cov = np.cov(ret_matrix, ddof=1) * ANNUALIZE

        # Replace rows/cols for tickers without history with CAPM estimates
        for i in range(n):
            if tickers[i] not in available:
                vi = betas[i] * MARKET_VOL
                cov[i, i] = vi ** 2 + IDIO_VOL ** 2
                for j in range(n):
                    if j != i:
                        c_ij = betas[i] * betas[j] * MARKET_VOL ** 2
                        if sectors[i] and sectors[i] == sectors[j]:
                            vj = math.sqrt(max(float(cov[j, j]), 1e-8))
                            c_ij = max(c_ij, SECTOR_CORR * vi * vj)
                        cov[i, j] = c_ij
                        cov[j, i] = c_ij

        # Toward-diagonal shrinkage
        diag = np.diag(np.diag(cov))
        cov = (1.0 - SHRINKAGE) * cov + SHRINKAGE * diag

        # Ridge to guarantee positive-definiteness
        min_eig = float(np.min(np.linalg.eigvalsh(cov)))
        if min_eig < 1e-8:
            cov += (-min_eig + 1e-6) * np.eye(n)

        return cov

    else:
        # ── CAPM + idiosyncratic fallback ────────────────────────────────────
        vols = betas * MARKET_VOL
        sys_cov = np.outer(betas, betas) * MARKET_VOL ** 2
        cov = sys_cov.copy()
        np.fill_diagonal(cov, vols ** 2 + IDIO_VOL ** 2)

        total_vols = np.sqrt(np.diag(cov))
        for i in range(n):
            for j in range(i + 1, n):
                if sectors[i] and sectors[i] == sectors[j]:
                    floor = SECTOR_CORR * total_vols[i] * total_vols[j]
                    if cov[i, j] < floor:
                        cov[i, j] = floor
                        cov[j, i] = floor
        return cov


# ── local-search optimiser ────────────────────────────────────────────────────

def _portfolio_sharpe(cov, rets, idx):
    """Equal-weight portfolio Sharpe over the position indices in idx (a
    list or set) -- shared by _local_search_max_sharpe's initial score,
    its swap evaluation, and its post-convergence alternates pass, so all
    three use the exact same math."""
    idx = list(idx)
    m = len(idx)
    w = np.full(m, 1.0 / m)
    sub_rets = rets[idx]
    sub_cov = cov[np.ix_(idx, idx)]
    port_var = float(w @ sub_cov @ w)
    port_ret = float(w @ sub_rets)
    return (port_ret - RF) / math.sqrt(port_var) if port_var > 1e-12 else -math.inf


# Hard cap on local-search passes -- pure safety net, not expected to bind:
# each accepted swap strictly increases Sharpe (only a swap that improves
# on the current best is ever taken), and there are finitely many n_select-
# subsets of a pool this size, so the loop is mathematically guaranteed to
# terminate on its own; this just guards against a pathological floating-
# point oscillation at the convergence boundary rather than a real
# non-termination risk.
_LOCAL_SEARCH_MAX_PASSES = 50


def _local_search_max_sharpe(cov, position_returns, n_select):
    """Pairwise-exchange local search maximising equal-weight portfolio
    Sharpe -- explicit instruction, replacing an earlier forward-greedy
    version: start from the top n_select candidates by composite score
    (pool is already sorted that way by the caller, so this is just
    range(n_select)), then repeatedly swap a held position out for a
    benched one whenever it improves the WHOLE portfolio's Sharpe, until
    no swap improves it anymore.

    Each pass evaluates every (held, benched) pair -- up to n_select ×
    (n_total − n_select) swaps -- and applies only the single
    best-improving one before starting a new pass (explicit instruction:
    best-improvement per pass, not first-improvement), so the result
    doesn't depend on scan order the way first-improvement would.
    Fixes forward-greedy's real structural weakness: once greedy picked
    something, it could never reconsider that pick once more of the
    portfolio was built around it; this can undo an early pick once a
    later swap makes it look worse relative to the set it's now part of.

    Returns (selected indices, final Sharpe, alternates) where
    alternates[i] is the up-to-2 benched candidates that would have come
    closest to displacing selected[i] specifically, evaluated once at
    convergence (not during the swap search itself, which only tracks the
    single best swap each pass) -- the natural analog of forward-greedy's
    old "runner-up at the step this was picked" for an algorithm that
    doesn't select sequentially."""
    n_total = len(position_returns)
    rets = np.array(position_returns)
    selected = set(range(min(n_select, n_total)))
    bench = set(range(n_total)) - selected

    current_sharpe = _portfolio_sharpe(cov, rets, selected)
    for _ in range(_LOCAL_SEARCH_MAX_PASSES):
        best_swap = None
        best_sharpe = current_sharpe
        for out_idx in selected:
            trial_base = selected - {out_idx}
            for in_idx in bench:
                s = _portfolio_sharpe(cov, rets, trial_base | {in_idx})
                if s > best_sharpe:
                    best_sharpe = s
                    best_swap = (out_idx, in_idx)
        if best_swap is None:
            break
        out_idx, in_idx = best_swap
        selected.discard(out_idx)
        selected.add(in_idx)
        bench.discard(in_idx)
        bench.add(out_idx)
        current_sharpe = best_sharpe

    selected_list = sorted(selected)
    # Alternates: for each held position, which benched candidates would
    # have produced the highest Sharpe if swapped in for THAT specific
    # position (not the single best swap overall) -- "who almost took
    # your spot," evaluated fresh at the final, converged portfolio.
    alternates = []
    for out_idx in selected_list:
        trial_base = selected - {out_idx}
        trial_scores = sorted(
            ((_portfolio_sharpe(cov, rets, trial_base | {in_idx}), in_idx) for in_idx in bench),
            reverse=True,
        )
        alternates.append([k for _, k in trial_scores[:2]])

    return selected_list, current_sharpe, alternates


# ── main entry point ──────────────────────────────────────────────────────────

def build_target_portfolio(rec_file, sim_file, exclude_groups=None):
    """Read recommendations + simulations, run the optimiser, return result dict.

    exclude_groups: optional iterable of broad sector-group names (see
    modules.sector_groups.get_sector_group -- e.g. "Financial Services",
    "Healthcare") to drop from BOTH candidate pools before optimising, so
    the caller can build a variant portfolio that never touches those
    sectors. None (default) keeps the full universe.

    Returns a dict ready to be JSON-serialised with keys:
      longs, shorts, longPool, shortPool, stats, generatedAt
    """
    exclude_groups = set(exclude_groups) if exclude_groups else set()
    with open(rec_file) as f:
        rec_data = json.load(f)
    with open(sim_file) as f:
        sim_list = json.load(f)

    rec_by_ticker = {c["ticker"]: c for c in rec_data.get("candidates", [])}
    sim_by_ticker = {r["ticker"]: r for r in sim_list
                     if "ticker" in r and not r.get("error")}

    hist_returns = _load_daily_returns()
    screener_signals = _load_screener_signals()

    def _make_candidate(ticker, side):
        rec = rec_by_ticker.get(ticker)
        sim = sim_by_ticker.get(ticker)
        if not rec or not sim:
            return None
        fr = sim.get("forecastReturn")
        if fr is None:
            return None
        beta_raw = sim.get("inputs", {}).get("beta")
        raw_beta = abs(beta_raw) if beta_raw is not None else 1.0
        beta = max(BETA_FLOOR, min(raw_beta, BETA_CAP))
        vol = beta * MARKET_VOL
        position_return = fr if side == "Long" else -fr
        indiv_sharpe = (position_return - RF) / vol
        prob_above = (sim.get("priceAtIndustryMultiple") or {}).get("probAboveCurrentPrice")
        # FINRA's fresher biweekly-settlement figure preferred over
        # yfinance's staler month-end one -- same preference order as
        # RecommendationsView.tsx's own crowded-short gate (see
        # recommendations.py's own shortPctOfFloatFinra comment).
        short_interest = rec.get("shortPctOfFloatFinra")
        if short_interest is None:
            short_interest = rec.get("shortPercentOfFloat")
        return {
            "ticker": ticker,
            "name": sim.get("name") or rec.get("name"),
            "sector": sim.get("sector") or rec.get("sector"),
            "price": sim.get("currentPrice") or rec.get("price"),
            "side": side,
            "rating": rec.get("rating"),
            "score": rec.get("score"),
            "scorePercentile": rec.get("scorePercentile"),
            "forecastReturn": fr,
            "positionReturn": position_return,
            "vol": vol,
            "beta": beta,
            "indivSharpe": indiv_sharpe,
            "analysts": rec.get("numberOfAnalystOpinions"),
            "targetUpside": rec.get("targetUpside"),
            "probAbove": prob_above,
            "shortInterest": short_interest,
            **screener_signals.get(ticker, {}),
        }

    def _build_pool(side):
        ratings = LONG_RATINGS if side == "Long" else SHORT_RATINGS
        direction = (lambda fr: fr > 0) if side == "Long" else (lambda fr: fr < 0)
        raw = []
        for ticker, rec in rec_by_ticker.items():
            if rec.get("rating") not in ratings:
                continue
            sim = sim_by_ticker.get(ticker)
            if not sim:
                continue
            fr = sim.get("forecastReturn")
            if fr is None or not direction(fr):
                continue
            c = _make_candidate(ticker, side)
            if not c:
                continue
            if exclude_groups and get_sector_group(c.get("sector")) in exclude_groups:
                continue
            mom, mr = c.get("mom"), c.get("mr")
            if side == "Long":
                if (mom is not None and mom > MOMENTUM_OVERBOUGHT) or (
                    mr is not None and mr >= MEAN_REVERSION_OVERBOUGHT
                ):
                    continue
            else:
                if (mom is not None and mom < MOMENTUM_OVERSOLD) or (
                    mr is not None and mr <= MEAN_REVERSION_OVERSOLD
                ):
                    continue
                si = c.get("shortInterest")
                if si is not None and si > MAX_SHORT_INTEREST:
                    continue
            raw.append(c)
        return raw

    results = {}
    candidate_pools = {}
    for side in ("Long", "Short"):
        pool = _build_pool(side)
        if not pool:
            results[side] = []
            candidate_pools[side] = []
            continue

        # Composite pre-screening: keep top CANDIDATE_POOL
        scores = _composite_score(pool, side)
        for c, s in zip(pool, scores):
            c["compositeScore"] = round(s, 4)
        pool.sort(key=lambda c: c["compositeScore"], reverse=True)
        pool_size = len(pool)
        for rank, c in enumerate(pool, start=1):
            c["poolRank"] = rank
            c["poolSize"] = pool_size
        candidate_pools[side] = [
            {
                "ticker": c["ticker"],
                "poolRank": c["poolRank"],
                "poolSize": c["poolSize"],
                "compositeScore": c["compositeScore"],
            }
            for c in pool
        ]
        pool = pool[:CANDIDATE_POOL]

        # Build covariance and run the local-search optimiser
        cov = _build_cov(pool, hist_returns)
        position_returns = [c["positionReturn"] for c in pool]
        selected_idx, _, runners_up_idx = _local_search_max_sharpe(cov, position_returns, POSITIONS)

        selected = [pool[i] for i in selected_idx]
        # 2nd/3rd choice for each slot -- see _local_search_max_sharpe's
        # own docstring for why this is "who almost displaced this
        # specific holding" at the converged portfolio, not just
        # "next-best composite score somewhere in the pool" (explicit
        # instruction: what would have taken this exact spot, not a
        # generic runner-up).
        for c, ru_idx in zip(selected, runners_up_idx):
            c["alternates"] = [
                {
                    "ticker": pool[i]["ticker"],
                    "name": pool[i]["name"],
                    "rating": pool[i]["rating"],
                    "forecastReturn": pool[i]["forecastReturn"],
                    "compositeScore": pool[i]["compositeScore"],
                    "poolRank": pool[i]["poolRank"],
                }
                for i in ru_idx
            ]

        results[side] = selected

    longs = results.get("Long", [])
    shorts = results.get("Short", [])

    # Per-leg statistics -- each leg on its own, 1/50 equal weight (100%
    # gross), so the long book and the short book can be compared
    # side by side. The short leg's return is the profit from prices
    # FALLING (sign -1 on forecastReturn); vol is sign-invariant. Sharpe
    # uses the same (leg return - RF) / leg vol convention as the combined
    # stat below.
    def _leg_stats(positions, sign):
        if not positions:
            return {"return": None, "vol": None, "sharpe": None}
        m = len(positions)
        w_leg = np.full(m, 1.0 / m)
        cov_leg = _build_cov(positions, hist_returns)
        rets_leg = np.array([c["forecastReturn"] for c in positions])
        leg_ret = sign * float(w_leg @ rets_leg)
        leg_var = float(w_leg @ cov_leg @ w_leg)
        leg_vol = math.sqrt(leg_var) if leg_var > 0 else 0.0
        return {
            "return": round(leg_ret, 6),
            "vol": round(leg_vol, 6),
            "sharpe": round((leg_ret - RF) / leg_vol, 4) if leg_vol > 0 else None,
        }

    # Combined portfolio statistics (100-position covariance)
    all_pos = longs + shorts
    stats = {
        "portfolioReturn": None, "portfolioVol": None, "sharpe": None, "sortino": None,
        "long": _leg_stats(longs, 1.0), "short": _leg_stats(shorts, -1.0),
    }
    if all_pos:
        n = len(all_pos)
        # Signed weights: +1/n_longs for each long, -1/n_shorts for each
        # short -- i.e. 1/50 per position, each leg 100% gross (200% gross
        # total, dollar-neutral), NOT 1/100 (which would be a half-gross
        # 50/50 book). The signed form still lets a long and a short with
        # positive stock-return correlation reduce portfolio variance (they
        # partially hedge) rather than add to it.
        n_longs = len(longs)
        n_shorts = n - n_longs
        per_long = 1.0 / n_longs if n_longs else 0.0
        per_short = 1.0 / n_shorts if n_shorts else 0.0
        w = np.array([per_long] * n_longs + [-per_short] * n_shorts)
        cov_all = _build_cov(all_pos, hist_returns)
        # Use raw forecastReturn (not positionReturn) since sign is in weights
        raw_returns = np.array([c["forecastReturn"] for c in all_pos])
        port_ret = float(w @ raw_returns)
        port_var = float(w @ cov_all @ w)
        port_vol = math.sqrt(port_var) if port_var > 0 else 0.0
        excess = port_ret - RF
        stats["portfolioReturn"] = round(port_ret, 6)
        stats["portfolioVol"] = round(port_vol, 6)
        stats["sharpe"] = round(excess / port_vol, 4) if port_vol > 0 else None
        stats["sortino"] = round(excess / (port_vol / math.sqrt(2)), 4) if port_vol > 0 else None

    return {
        "longs": longs,
        "shorts": shorts,
        "longPool": candidate_pools.get("Long", []),
        "shortPool": candidate_pools.get("Short", []),
        "stats": stats,
        "generatedAt": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
