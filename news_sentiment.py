"""FinBERT-based bullish/bearish scoring for news headlines.

Rates each headline 1 (very bearish) to 5 (very bullish) for the stock it's
about, using ProsusAI/finbert (a BERT model fine-tuned on financial text for
3-class positive/negative/neutral sentiment). Runs entirely locally on CPU --
no API key, no per-call cost -- but model load (a few seconds, once) and
inference (CPU-bound) mean callers on an asyncio event loop should offload
through asyncio.to_thread rather than calling score_headlines directly (see
ib_price_server.py's news_loop).

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
# to neutral (see score_headlines) rather than run through FinBERT at
# all, both for correctness and because it's one less headline to run
# inference on. Deliberately narrow (requires the "Outperforms/
# Underperforms Peers/Market" suffix) so it doesn't catch a headline that
# reports a move alongside a real reason, e.g. "AMD Shares Fall 8% as
# Musk Commits to Nvidia Chips for SpaceX" -- that "as ..." clause is
# exactly the kind of content this template never has, and exactly what
# makes a price-move headline worth scoring for real.
_MECHANICAL_MOVE_RE = re.compile(
    r"\bstock\s+(?:rises?|falls?|climbs?|slides?|advances?|declines?|gains?|drops?|jumps?|sinks?|surges?|plunges?)"
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


def score_headlines(headlines):
    """headlines: list[str], already cleaned (see clean_headline). Returns
    a list[int] of 1-5 scores, same order/length as the input, so callers
    can zip this 1:1 against their input list. Three cases skip FinBERT
    entirely rather than being run through it: an empty string and a
    _MECHANICAL_MOVE_RE match both score neutral (3); an
    _INSIDER_SALES_RE/_INSIDER_PURCHASES_RE match scores a fixed 2/4 (see
    that regex's own comment for why FinBERT can't be trusted here)."""
    if not headlines:
        return []
    scores = [3] * len(headlines)
    to_classify_idx = []
    for i, h in enumerate(headlines):
        if not h or _MECHANICAL_MOVE_RE.search(h):
            continue
        if _INSIDER_SALES_RE.search(h):
            scores[i] = 2
        elif _INSIDER_PURCHASES_RE.search(h):
            scores[i] = 4
        else:
            to_classify_idx.append(i)
    if to_classify_idx:
        classifier = _get_classifier()
        results = classifier([headlines[i] for i in to_classify_idx], truncation=True)
        for i, class_probs in zip(to_classify_idx, results):
            probs = {d["label"]: d["score"] for d in class_probs}
            polarity = probs.get("positive", 0.0) - probs.get("negative", 0.0)
            scores[i] = _polarity_to_score(polarity)
    return scores
