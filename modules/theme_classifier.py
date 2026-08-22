"""theme_classifier.py — zero-shot classification of held tickers' business
descriptions against a fixed theme taxonomy (data/theme_taxonomy.json),
producing data/ticker_themes.json ({ticker: [theme_key, ...]}) for the
Themes tab.

Runs entirely locally (facebook/bart-large-mnli via transformers, same
"local model, no API key" pattern news_sentiment.py's FinBERT already
uses) against raw_data.json's longBusinessSummary -- no network call
beyond the one-time model download.

Best-effort, not authoritative. The original 21-entry taxonomy was
validated against the 32 tickers this project's own portfolio already
had hand-verified tags for (see data/ticker_themes.json's original
curation): top-1 accuracy ~84% (27/32) using the "This company's core,
primary business is {}." hypothesis template below -- a plain "{}"
template scored meaningfully worse in the same test (58%), because vague
labels like "Diversified Thematic Growth (ETF)" spuriously matched
almost any growth-sounding description. That theme is EXCLUDED from
classification here for a different, harder reason: it describes a
fund, not a company, and funds don't carry a longBusinessSummary in the
first place to classify from (confirmed: ARKK's raw_data.json entry has
none) -- assigning it stays a manual-only, human judgment call.

The taxonomy was later expanded to 50 industry/factor-style entries
(banks, utilities, REITs, transportation, etc. -- see
data/theme_taxonomy.json) grounded in sorted_screen.csv's own yfinance
industry breakdown, for broad screener-universe coverage rather than a
handful of portfolio-specific niche exposures -- that 84% figure is
therefore a datapoint about the hypothesis template's usefulness in
general, not a re-verified number for this larger label set (more
candidate labels gives zero-shot classification more ways to be wrong,
and finer-grained industries like "Banks & Regional Lenders" vs.
"Capital Markets & Brokerage" are closer together in meaning than the
original taxonomy's niche themes were).

Because automatic top-1 picks are known to sometimes be simply wrong
(verified, not hypothetical -- e.g. AVGO, a pure semiconductor company,
top-scored "Telecom & Broadband" at 0.36 confidence in testing with the
original taxonomy), classify_themes NEVER overwrites a ticker that
already has an entry in ticker_themes.json. It only fills in tickers
with no entry at all, so a bad automatic guess can never silently
replace a manually-verified tag, and re-running this on the same
tickers is always a safe no-op.
"""

import json
import os

DATA_DIR = "data"
TAXONOMY_FILE = os.path.join(DATA_DIR, "theme_taxonomy.json")
TICKER_THEMES_FILE = os.path.join(DATA_DIR, "ticker_themes.json")
RAW_DATA_FILE = os.path.join(DATA_DIR, "yfinance", "raw_data.json")

MODEL_NAME = "facebook/bart-large-mnli"
HYPOTHESIS_TEMPLATE = "This company's core, primary business is {}."
# Below this score, a candidate theme isn't confident enough to auto-tag.
# Picked empirically (see module docstring's validation numbers): high
# enough to exclude most near-zero noise a wrong/ambiguous description
# produces, low enough to still keep a genuine secondary theme (e.g. a
# real second theme scoring 0.3-0.5 alongside a dominant ~0.9 primary
# one, the same shape GOOG's own two real tags score at).
CONFIDENCE_THRESHOLD = 0.3
# A fund, not a company -- see module docstring for why this is excluded
# from automatic classification entirely, not just given a low prior.
_EXCLUDED_THEME_KEY = "diversified_thematic_etf"

_classifier = None


def _get_classifier():
    global _classifier
    if _classifier is None:
        from transformers import pipeline

        _classifier = pipeline("zero-shot-classification", model=MODEL_NAME)
    return _classifier


def load_taxonomy():
    with open(TAXONOMY_FILE) as f:
        return json.load(f)


def classify_themes(tickers, threshold=CONFIDENCE_THRESHOLD):
    """For every ticker in `tickers` that doesn't already have an entry in
    ticker_themes.json (see module docstring on why existing entries are
    never touched), classifies its raw_data.json longBusinessSummary
    against the taxonomy (excluding _EXCLUDED_THEME_KEY) and keeps every
    theme scoring at or above `threshold`, multi-label -- a company can
    genuinely have more than one real theme (see ticker_themes.json's own
    GOOG entry). If NONE clear threshold, falls back to the single
    closest-scoring theme instead of leaving the ticker unclassified --
    explicit instruction: every ticker with a business description to
    classify from must land somewhere in the taxonomy, even a low-
    confidence single tag, rather than sitting in the Themes tab's
    "Unclassified" bucket indefinitely. Only a ticker with no
    longBusinessSummary AT ALL (an ETF, or one main.py hasn't fetched
    yet -- see no_summary below) is left unclassified, since there's
    nothing to classify from in the first place, low-confidence or
    otherwise.

    Merges into ticker_themes.json and returns {ticker: [theme_key, ...]}
    for just the tickers classified this call (not the full merged
    file)."""
    try:
        with open(TICKER_THEMES_FILE) as f:
            existing = json.load(f)
    except FileNotFoundError:
        existing = {}

    to_classify = [t for t in tickers if t not in existing]
    if not to_classify:
        print("Every requested ticker already has theme tags -- nothing to classify.")
        return {}

    try:
        with open(RAW_DATA_FILE) as f:
            raw = json.load(f)
    except FileNotFoundError:
        raw = {}

    taxonomy = load_taxonomy()
    candidates = [t for t in taxonomy if t["key"] != _EXCLUDED_THEME_KEY]
    label_to_key = {t["label"]: t["key"] for t in candidates}
    candidate_labels = list(label_to_key.keys())

    results = {}
    no_summary = []
    classifier = None
    for ticker in to_classify:
        summary = raw.get(ticker, {}).get("longBusinessSummary")
        if not summary:
            no_summary.append(ticker)
            continue
        if classifier is None:
            print(f"Loading {MODEL_NAME} for theme classification (first call only, a bit slow)...", flush=True)
            classifier = _get_classifier()
        result = classifier(
            summary[:1000], candidate_labels, multi_label=True, hypothesis_template=HYPOTHESIS_TEMPLATE
        )
        keys = [
            label_to_key[label] for label, score in zip(result["labels"], result["scores"]) if score >= threshold
        ]
        if keys:
            results[ticker] = keys
            print(f"{ticker}: {', '.join(keys)}", flush=True)
        else:
            # Nothing cleared threshold -- result["labels"]/["scores"] are
            # already sorted descending by the pipeline, so [0] is the
            # closest match regardless of how low its score is. Assigned
            # as a single tag rather than left unclassified -- see this
            # function's own docstring.
            top_label, top_score = result["labels"][0], result["scores"][0]
            results[ticker] = [label_to_key[top_label]]
            print(f"{ticker}: no theme cleared the confidence threshold -- assigned closest match {top_label} ({top_score:.3f})", flush=True)

    if no_summary:
        print(
            f"{len(no_summary)} ticker(s) have no longBusinessSummary to classify "
            f"(e.g. ETFs, or not yet fetched by main.py): {', '.join(no_summary)}"
        )

    if results:
        existing.update(results)
        with open(TICKER_THEMES_FILE, "w") as f:
            json.dump(existing, f, indent=2)
        print(f"Wrote {TICKER_THEMES_FILE} ({len(results)} newly classified)")

    return results
