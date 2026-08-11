"""Builds data/recommendations.json for the Recommendations tab: one row per
Strong Buy/Buy/Sell/Strong Sell ticker (see main.py's RATED_FOR_EXTRAS --
the same scoping already used for Form4/XBRL/13F/social-sentiment downloads,
since a Hold-rated ticker isn't a conviction pick in either direction), with
its composite score/rating plus three recency signals layered on top:
recent news (last RECENT_NEWS_DAYS days, from data/news.json), recent
insider open-market activity (last RECENT_INSIDER_DAYS days, from SEC Form 4
via sec_edgar.FORM4_FILE), and the latest 13F quarter-over-quarter
institutional share-count change (sec_edgar.THIRTEENF_FILE). The composite
score already blends whole-window versions of sentiment/insiders (see
scoring.load_sentiment_scores/load_insider_scores) -- these recency windows
are a separate, narrower read (days, not the sentiment blend's ~month) so a
ticker whose news/insider activity just turned can be surfaced with a "why
now" even if its steady-state score hasn't moved yet.

Deliberately does NOT decide the final 5 buys / 5 sells itself -- that needs
to know which tickers are currently held, and (like Positions/Sectors/Themes/
News) this app reads live positions client-side from ib_price_server.py's
SSE stream, not from a file this script could read. RecommendationsView.jsx
does that split: Buy candidates are non-held Strong Buy/Buy rows (new-idea
scope only, per user's explicit choice); Sell candidates are held Sell/
Strong Sell rows first (actionable exits), topped up with non-held Strong
Sell rows labeled "Avoid" if fewer than 5 held rows qualify.
"""
import csv
import json
import os
from datetime import datetime, timedelta

RECENT_NEWS_DAYS = 7
RECENT_INSIDER_DAYS = 90

OUT_FILE = os.path.join("data", "recommendations.json")


def _load_json_or_empty(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _recent_news_counts(news_file, tickers, now, days):
    """{ticker: {bullish, bearish, neutral, total}} counting only articles
    within the last `days`, scored 4-5 = bullish, 1-2 = bearish, 3 = neutral
    -- same bucket boundaries PeTable.jsx's avgNewsSentiment/NewsView's
    showNeutral treatment already use elsewhere in this app."""
    news = _load_json_or_empty(news_file)
    cutoff = now - timedelta(days=days)
    counts = {}
    for ticker in tickers:
        bullish = bearish = neutral = 0
        for article in news.get(ticker, []):
            try:
                article_time = datetime.fromisoformat(article["time"])
            except (KeyError, ValueError):
                continue
            if article_time < cutoff:
                continue
            score = article.get("sentiment")
            if score is None:
                continue
            if score >= 4:
                bullish += 1
            elif score <= 2:
                bearish += 1
            else:
                neutral += 1
        counts[ticker] = {"bullish": bullish, "bearish": bearish, "neutral": neutral, "total": bullish + bearish + neutral}
    return counts


def _recent_insider_counts(form4_file, tickers, now, days):
    """{ticker: {buys, sells}} counting only open-market Form 4 transactions
    (transactionCode P/S -- see scoring.load_insider_scores for why only
    those two codes count as a discretionary bet) filed within the last
    `days`."""
    filings_by_ticker = _load_json_or_empty(form4_file)
    cutoff = now - timedelta(days=days)
    counts = {}
    for ticker in tickers:
        buys = sells = 0
        for filing in filings_by_ticker.get(ticker, []):
            for tx in filing.get("transactions", []):
                try:
                    tx_date = datetime.fromisoformat(tx["date"])
                except (KeyError, ValueError):
                    continue
                if tx_date < cutoff:
                    continue
                if tx.get("code") == "P":
                    buys += 1
                elif tx.get("code") == "S":
                    sells += 1
        counts[ticker] = {"buys": buys, "sells": sells}
    return counts


def build_recommendations(sorted_screen_csv, news_file, form4_file, institutional_holdings_file, ratings, now=None):
    """Returns the dict written to recommendations.json (see module
    docstring). `ratings` is the set of `rating` column values to include --
    pass main.RATED_FOR_EXTRAS. `now` is injectable for tests; defaults to
    the current time."""
    now = now or datetime.now()

    try:
        with open(sorted_screen_csv, newline="") as f:
            rows = [row for row in csv.DictReader(f) if row.get("rating") in ratings]
    except FileNotFoundError:
        return {"generatedAt": now.isoformat(), "candidates": []}

    tickers = [row["ticker"] for row in rows]
    news_counts = _recent_news_counts(news_file, tickers, now, RECENT_NEWS_DAYS)
    insider_counts = _recent_insider_counts(form4_file, tickers, now, RECENT_INSIDER_DAYS)
    institutional = _load_json_or_empty(institutional_holdings_file)

    def to_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    candidates = []
    for row in rows:
        ticker = row["ticker"]
        score = to_float(row.get("score"))
        inst_change = institutional.get(ticker, {}).get("pctShareChangeQoQ")
        candidates.append(
            {
                "ticker": ticker,
                "name": row.get("name"),
                "sector": row.get("sector"),
                "rating": row.get("rating"),
                "score": score,
                "scorePercentile": round(score * 100, 1) if score is not None else None,
                "price": to_float(row.get("price")),
                "momentum": to_float(row.get("momentum")),
                "shortPercentOfFloat": to_float(row.get("shortPercentOfFloat")),
                "targetMeanPrice": to_float(row.get("targetMeanPrice")),
                "targetUpside": to_float(row.get("targetUpside")),
                "recommendationMean": to_float(row.get("recommendationMean")),
                "numberOfAnalystOpinions": to_float(row.get("numberOfAnalystOpinions")),
                "news7d": news_counts.get(ticker, {"bullish": 0, "bearish": 0, "neutral": 0, "total": 0}),
                "insiders90d": insider_counts.get(ticker, {"buys": 0, "sells": 0}),
                "instChangeQoQ": inst_change,
            }
        )

    return {"generatedAt": now.isoformat(), "candidates": candidates}


def write_recommendations(sorted_screen_csv, news_file, form4_file, institutional_holdings_file, ratings, out_file=OUT_FILE):
    result = build_recommendations(sorted_screen_csv, news_file, form4_file, institutional_holdings_file, ratings)
    with open(out_file, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Wrote {len(result['candidates'])} candidate(s) to {out_file}")
