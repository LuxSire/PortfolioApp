"""FinBERT-based bullish/bearish scoring for news headlines + article bodies.

Rates each article 1 (very bearish) to 5 (very bullish) for the stock it's
about, using ProsusAI/finbert (a BERT model fine-tuned on financial text for
3-class positive/negative/neutral sentiment). Runs entirely locally on CPU --
no API key, no per-call cost -- but model load (a few seconds, once) and
inference (CPU-bound) mean callers on an asyncio event loop should offload
through asyncio.to_thread rather than calling score_articles/score_headlines
directly (see ib_server.py's news_loop).

score_articles (the primary entry point -- see ib_server.py's news_loop/
_backfill_news_sentiment) classifies headline + full article body together
when a body is available (see IBApp.get_news_article_async): a headline
like "Semtech Stock Rises" reads as neutral market-move noise on its own,
but combined with the body's actual substance ("smashed analyst estimates
... raised its outlook") it's clearly bullish -- the body is fetched only
for scoring, never persisted. score_headlines (headline-only, no body) is
kept as a narrower building block underneath it, sharing the same fast-path
shortcuts (see fast_path_score).

Score is derived from the full 3-class probability distribution, not just
the top-1 label+confidence: polarity = P(positive) - P(negative), in
[-1, 1], then bucketed into 1-5. Using the full distribution (rather than a
confidence threshold on the winning label alone) gives a smoother score --
e.g. a headline FinBERT calls "negative" at low confidence because neutral
was a close second lands as a 2, not a 1.
"""
import re

_TAG_RE = re.compile(r"^\{[^}]*\}")

# Dow Jones auto-generates a specific daily-movers headline template --
# "<Company> Stock Rises 6.1%, Outperforms Peers" -- that's purely
# reporting today's price move relative to a peer/market benchmark, not
# any actual reasoning about the company. FinBERT reliably (and wrongly)
# scores these as maximally bullish/bearish just from the "rises"/"falls"
# + "outperforms"/"underperforms" wording -- a stock's own daily move
# isn't bullish/bearish news about the company, it's just the stock
# moving, which is neutral information, not a signal. Matched and forced
# to neutral (see fast_path_score) rather than run through FinBERT at
# all, both for correctness and because it's one less headline to run
# inference on. Deliberately narrow (requires the "Outperforms/
# Underperforms Peers/Market" suffix) so it doesn't catch a headline that
# reports a move alongside a real reason, e.g. "AMD Shares Fall 8% as
# Musk Commits to Nvidia Chips for SpaceX" -- that "as ..." clause is
# exactly the kind of content this template never has, and exactly what
# makes a price-move headline worth scoring for real.
# Verb list confirmed against this project's own cached headlines (data/
# news.json), not guessed -- rallies/sheds/slips were all real, currently
# occurring cases this regex was silently missing (e.g. "EQT Corp. Stock
# Rallies 4.6%, Underperforms Peers" fell through to FinBERT and scored
# bearish (2), purely from "Underperforms" outweighing "Rallies" in the
# model's own reading -- exactly the wrong kind of signal this whole
# regex exists to intercept before FinBERT ever sees it).
_MECHANICAL_MOVE_RE = re.compile(
    r"\bstock\s+(?:rises?|falls?|climbs?|slides?|slips?|advances?|declines?|gains?|drops?|jumps?|sinks?|surges?"
    r"|plunges?|rall(?:y|ies)|sheds?)"
    r"\s+[\d.]+%,\s*(?:out|under)performs\s+(?:peers|market|sector|industry)\b",
    re.I,
)

# "Substantial Insider Sales/Purchases: Morning/Afternoon Report" is
# another huge, recurring auto-generated template (Dow Jones runs it on a
# schedule, not per-event -- confirmed 800+ occurrences in this project's
# own cache, only 4 distinct base headlines). Unlike the mechanical-move
# case above, this one isn't noise to neutralize -- insider selling/buying
# is a real, if mild, signal -- but FinBERT itself completely misses it:
# these score ~0.05 polarity (86% "neutral"), because the headline text
# alone carries none of the overtly positive/negative financial-sentiment
# wording FinBERT was fine-tuned to recognize. No polarity-bucket
# adjustment can fix a polarity that's genuinely near zero at the source,
# so this bypasses the classifier entirely too, with a fixed mild
# score -- 2 (bearish, not 1/very bearish: one report of insider selling
# isn't catastrophic) or 4 (bullish) rather than FinBERT's own 3.
_INSIDER_SALES_RE = re.compile(r"\binsider (?:sales?|selling)\b", re.I)
_INSIDER_PURCHASES_RE = re.compile(r"\binsider (?:purchases?|buying)\b", re.I)

# IBD's (and occasionally another outlet's) recurring "Stock Market Today:
# ..." market-recap headline -- reports the INDEX's own move (Dow/Nasdaq,
# usually tied to a macro event like a jobs report, Fed meeting, or tariff
# news), with individual company names/tickers appearing only as a
# "notable mover of the day" mention, not because the headline is actually
# about that company. This gets attributed to every namechecked ticker's
# own news feed, and FinBERT scores it per whichever "Rises"/"Falls"/
# "Soars"/"Skids" word happens to land near THAT ticker's mention -- the
# same wrong signal _MECHANICAL_MOVE_RE above exists to intercept, just at
# the whole-market level instead of a single stock, and a far higher-volume
# case in practice (573 occurrences across 177 distinct headlines in this
# project's own cached data/IB/news.json, vs. a handful for the narrower
# per-stock template). Matched on the "Stock Market Today:" prefix alone --
# confirmed against every distinct cached headline using it, IBD's own
# "(Live Coverage)"-tagged recaps and a rarer WSJ one alike, every single
# one a pure market/macro recap, never an actual company-specific piece --
# and forced to neutral rather than run through FinBERT at all, same
# treatment as the mechanical-move case above. Explicit instruction:
# "anything that only speaks about stock movements is just neutral."
_MARKET_RECAP_RE = re.compile(r"^stock market today\s*:", re.I)

# The extreme labels (1/5) used to trigger at just |polarity| >= 0.55 --
# in practice FinBERT is confidently non-neutral far more often than it's
# genuinely "very" bullish/bearish (a 500-headline sample from this
# project's own cache came back 97 "very bullish" vs. only 22 "bullish",
# and 38 "very bearish" vs. 15 "bearish" -- the extreme labels dominating
# their moderate counterparts almost 5:1 is backwards from what "very"
# should mean). Confirmed against two real examples: a routine "opens a
# new campus" press release scored 0.885 (should read as bullish, not
# very bullish) and a "stock down 33%" headline scored -0.964 (should
# read as bearish, not very bearish) -- 0.97 sits just above both, so
# "very" now requires close to total model confidence in one direction,
# not just a clear lean. The inner (neutral-band) thresholds are
# unchanged; only how far out "very" starts has moved.
_POLARITY_BUCKETS = [
    (-0.97, 1),
    (-0.2, 2),
    (0.2, 3),
    (0.97, 4),
]

_classifier = None

# A cap on how much body text actually gets combined with the headline for
# scoring -- generous relative to FinBERT/BERT's own ~512-token input limit
# (classifier(..., truncation=True) below would silently cut off anything
# past that anyway), just avoids building/tokenizing a needlessly huge
# string for the rare very-long article body.
_MAX_BODY_CHARS = 4000


def clean_headline(headline):
    """Strips the leading `{A:800015:L:en}`-style provider metadata tag
    Dow Jones headlines carry (article/language IDs, not part of the
    actual headline text) -- left in, it's just noise to the classifier."""
    return _TAG_RE.sub("", headline or "").strip()


def _get_classifier():
    global _classifier
    if _classifier is None:
        from transformers import pipeline

        _classifier = pipeline("sentiment-analysis", model="ProsusAI/finbert", top_k=None)
    return _classifier


def _polarity_to_score(polarity):
    for threshold, score in _POLARITY_BUCKETS:
        if polarity < threshold:
            return score
    return 5


def fast_path_score(headline):
    """Returns the fixed score (3 neutral, 2/4 mild) for a headline that
    matches one of the four recurring auto-generated templates below, or
    None if it needs real FinBERT classification. These are all decided
    from the headline's own wording alone -- a fixed, recognizable
    template -- so there's nothing an article body would add that changes
    the answer; callers (see ib_server.py's _fetch_article_bodies) use
    this to skip fetching a body at all for one of these, not just to
    skip classification."""
    if not headline:
        return 3
    if _MECHANICAL_MOVE_RE.search(headline) or _MARKET_RECAP_RE.search(headline):
        return 3
    if _INSIDER_SALES_RE.search(headline):
        return 2
    if _INSIDER_PURCHASES_RE.search(headline):
        return 4
    return None


def _classify(texts):
    """Runs FinBERT on texts that already cleared fast_path_score (no
    shortcut applies) -- returns list[int] 1-5 scores, same order. Shared
    by score_headlines/score_articles below."""
    classifier = _get_classifier()
    results = classifier(texts, truncation=True)
    scores = []
    for class_probs in results:
        probs = {d["label"]: d["score"] for d in class_probs}
        polarity = probs.get("positive", 0.0) - probs.get("negative", 0.0)
        scores.append(_polarity_to_score(polarity))
    return scores


def score_headlines(headlines):
    """headlines: list[str], already cleaned (see clean_headline). Returns
    a list[int] of 1-5 scores, same order/length as the input, so callers
    can zip this 1:1 against their input list. Headline-only -- see
    score_articles for the headline+body path this project actually uses
    for real classification; kept as a narrower building block underneath
    it (and for a caller with no body text available at all)."""
    if not headlines:
        return []
    scores = [3] * len(headlines)
    to_classify_idx = []
    for i, h in enumerate(headlines):
        fast = fast_path_score(h)
        if fast is not None:
            scores[i] = fast
        else:
            to_classify_idx.append(i)
    if to_classify_idx:
        classified = _classify([headlines[i] for i in to_classify_idx])
        for i, score in zip(to_classify_idx, classified):
            scores[i] = score
    return scores


def score_articles(headlines, bodies):
    """headlines: list[str], already cleaned (see clean_headline). bodies:
    a parallel list[str | None], same length -- bodies[i] is that
    article's full plain-text body (see IBApp.get_news_article_async)
    when one was fetched, or None when it wasn't (fast-path headline,
    fetch failed/timed out, or no body available). Returns a list[int]
    1-5 scores, same order/length.

    Same fast_path_score shortcuts as score_headlines (checked on the
    headline alone) for the four recurring templates that don't need real
    classification at all -- a body is never even fetched for these (see
    ib_server.py's _fetch_article_bodies), let alone scored. Every other
    article is classified on headline + body combined when a body is
    available -- the body carries the actual substance (the beat/raise
    numbers, the "why") that a headline like "Stock Rises" reads as
    neutral market-move noise on its own; falls back to headline-only
    when no body came back, same as score_headlines, rather than dropping
    the article's score entirely."""
    if not headlines:
        return []
    if bodies is None:
        bodies = [None] * len(headlines)
    scores = [3] * len(headlines)
    to_classify_idx = []
    texts = []
    for i, h in enumerate(headlines):
        fast = fast_path_score(h)
        if fast is not None:
            scores[i] = fast
            continue
        body = bodies[i] if i < len(bodies) else None
        text = f"{h}. {body[:_MAX_BODY_CHARS]}" if body else h
        to_classify_idx.append(i)
        texts.append(text)
    if to_classify_idx:
        classified = _classify(texts)
        for i, score in zip(to_classify_idx, classified):
            scores[i] = score
    return scores
