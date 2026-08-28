"""FinBERT-based bullish/bearish scoring for news headlines and, where
available, article bodies.

Rates each article 1 (very bearish) to 5 (very bullish) for the stock it's
about, using ProsusAI/finbert (a BERT model fine-tuned on financial text for
3-class positive/negative/neutral sentiment). Runs entirely locally on CPU --
no API key, no per-call cost -- but model load (a few seconds, once) and
inference (CPU-bound) mean callers on an asyncio event loop should offload
through asyncio.to_thread rather than calling score_headlines/score_articles
directly (see ib_server.py's news_loop).

Two scoring paths, deliberately different scopes:
  score_headlines (headline-only) -- what news_loop/_backfill_news_sentiment
    use for every article the moment it's first seen. Cheap, no network
    call beyond the headline fetch itself already made.
  score_articles (headline + body, when a body is available) -- what
    ib_server.py's body_fetch_loop upgrades a RECENT article to (a rolling
    24h window only -- IB's reqNewsArticle has no bulk form, one paced
    request per article, so fetching a body for the ENTIRE cache would
    take days; a rolling day's worth is cheap and sustainable indefinitely,
    see that function's own docstring) and what _handle_news_article
    upgrades a single article to on demand (checks the cache first, live-
    fetches on a miss). Once a body is available, it's unconditionally
    preferred over the headline-only read -- explicit instruction, with a
    known tradeoff: a real A/B comparison across 47 already-fetched bodies
    found this fixes some genuinely wrong headline-only reads but also
    dilutes some already-correct ones into mush (e.g. "Price Target
    Raised" -- clearly bullish alone -- read as neutral once a long,
    multi-topic body was mixed in). Accepted anyway, in exchange for using
    an article's actual substance whenever it's cheaply available rather
    than never.

Both paths share the same fast_path_score regex shortcuts (see below) --
the deterministic, zero-dilution-risk fix for recurring auto-generated
templates FinBERT reads wrong, and where new problems should keep getting
fixed as they're found rather than leaned on FinBERT (or a body) to sort
out.

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

# IBD's pre-market twin of the "Stock Market Today:" recap above --
# "Dow Jones Futures Fall/Rise/Due/Loom/Waver ...; <company(s)> Earnings/
# News <Due/Loom/Late/Beat/Skids> -- IBD" (and a ":"-punctuated variant of
# the same template). Same problem, same fix: Dow futures direction and
# macro events (Fed, jobs data, tariffs) drive the headline, with company
# names/tickers appearing as incidental "who's moving or reporting today"
# flavor text -- confirmed against every one of 66 distinct cached
# headlines using this prefix. "Nvidia Earnings Due" is pure calendar
# info about Nvidia specifically, not a signal, exactly the same as
# _EARNINGS_CALENDAR_RE's problem -- yet this got attributed to NVDA's
# own feed and scored bearish. A minority of these DO embed a real
# per-company fact for some OTHER mentioned ticker (e.g. "Target Earnings
# Beat") -- accepted the same way _MARKET_RECAP_RE already does, since
# that same fact is essentially always also reported as its own dedicated
# headline elsewhere in that company's feed, not lost entirely.
_FUTURES_RECAP_RE = re.compile(r"^dow jones futures\b", re.I)

# MarketWatch/Dow Jones' own premarket-futures-movers template -- "S&P 500
# Futures Climb/Rise/Up/Fall/Flat/Steady In Premarket Trading; <Company1>,
# <Company2> Lead/Lag". Same class of problem as _FUTURES_RECAP_RE, and
# demonstrably worse in practice: found via a systematic scan (98
# occurrences across 20 distinct cached headlines, checked every one) that
# FinBERT scores almost entirely off the INDEX's own direction word --
# e.g. a company explicitly tagged "Lag" (underperforming) inside a
# "Climb" headline still reads bullish (4), the opposite of what "Lag"
# actually says about that company. Forced neutral rather than trying to
# read Lead/Lag correctly per company, which the text alone doesn't
# reliably disambiguate (multiple companies, one shared verb).
_SP500_FUTURES_RECAP_RE = re.compile(r"^s&p 500 futures\b", re.I)

# Dow Jones' recurring analyst price-target template -- "<Company> Price
# Target Raised/Cut to $X.XX/Share From $Y.YY by <Firm>" for an actual
# change, or "<Company> Price Target Maintained With a $X.XX/Share by
# <Firm>" for no change at all. Confirmed against this project's own
# cache (3,114 Raised / 1,334 Cut / 259 Maintained): FinBERT correctly
# reads Raised as bullish (4) and Cut as bearish (2) -- real directional
# language it's fine-tuned to catch -- but scores Maintained bullish (4)
# too, indistinguishable from Raised, apparently from the dollar figure
# and generally positive-sounding analyst-coverage framing rather than
# "maintained" itself, which is structurally a NO-CHANGE signal (an
# analyst reiterating an existing view, not revising it). Only the
# Maintained case is intercepted -- Raised/Cut are left to FinBERT, which
# already gets them right.
_PRICE_TARGET_MAINTAINED_RE = re.compile(r"\bprice target maintained\b", re.I)

# Dow Jones' recurring "U.S. Earnings Preview: Before Market Open/After
# Market Close <date>" headline -- a pure earnings-calendar listing (which
# tickers report today and when), attributed to every reporting ticker's
# own news feed with zero company-specific content in the headline itself.
# Confirmed against every one of the 24 distinct cached headlines using
# this exact prefix (146 total occurrences): all just a date, no analysis
# -- same "no real signal, just a listing" class of problem
# _MARKET_RECAP_RE exists for, and FinBERT is just as consistently wrong
# about it, scoring every single cached occurrence bearish (2). A
# genuine company-specific earnings PREVIEW piece (e.g. "Home Depot
# Expected to Post Higher 2Q Sales, Revenue -- Earnings Preview") is a
# DIFFERENT headline shape entirely -- real analysis, real signal worth
# scoring -- and is deliberately NOT matched by this regex (anchored to
# the "U.S. Earnings Preview:" calendar-listing prefix specifically, not
# a bare "Earnings Preview" suffix).
_EARNINGS_CALENDAR_RE = re.compile(r"^u\.s\. earnings preview\s*:", re.I)

# Generic provider footer/legal-disclaimer boilerplate every fetched
# article body ends with -- Dow Jones Newswires' own copyright + "not
# independent research" disclaimer ("The statements in this document
# shall not be considered as an objective or independent explanation...
# not subject to any prohibition on dealing ahead of the dissemination
# or publication of investment research"), confirmed present in 99/101
# of this project's own cached article bodies; Briefing.com's shorter
# "Issuance Date: ... Copyright <year> Briefing.com, Inc." footer for the
# other two. Pure noise, zero connection to the actual news, but
# confirmed to materially matter: stripping it from a genuinely bullish
# Semtech earnings-beat article ("swung to a profit... beat on EPS and
# revenue... raised third-quarter guidance") flipped FinBERT's read on
# the full body from neutral (3) to the correct bullish (4) -- the
# disclaimer's own hedge-heavy, negation-heavy legal language ("shall
# not", "not... independent", "not subject to") was diluting an
# otherwise clear signal. A third marker, "Ratings actions from
# Benzinga: <url>", is Dow Jones' own trailing self-promotional pointer
# to Benzinga's ratings-history page for the ticker (present whenever a
# DJ-N article touches analyst ratings, confirmed across 177 cached
# bodies) -- it always sits immediately before the "(END) Dow Jones
# Newswires" marker above, so on its own it'd already get cut by that
# marker except for the fact that IT comes first in the text; listed
# here too so the earliest of all three markers wins, whichever it is.
# Not real article content in any of the three cases, just a URL to
# somewhere else. Matched on whichever marker starts first and
# everything from there to the end is cut (see strip_boilerplate) --
# left untouched if none appear (a provider with no recognized footer
# pattern).
_BOILERPLATE_START_RE = re.compile(
    r"\(END\) Dow Jones Newswires"
    r"|The statements in this document shall not be considered"
    r"|Issuance Date:[\s\S]*?Copyright \d{4} Briefing\.com, Inc\."
    r"|Ratings actions from Benzinga:",
    re.I,
)


def strip_boilerplate(body):
    """See _BOILERPLATE_START_RE's own comment for what this removes and
    why. Returns `body` unchanged if no known footer marker is found."""
    m = _BOILERPLATE_START_RE.search(body)
    return body[: m.start()].strip() if m else body

# MT Newswires' recurring "CFA <Sector>:Insider Review For Week Ended
# <date>" weekly-report title (and its "-N-" multi-part continuations,
# which repeat the identical title) -- purely a report NAME, no actual
# insider-trading direction, volume, or company-specific content at all.
# Found via a systematic scan of this project's own cache for headline
# templates recurring across many distinct tickers (not just reacting to
# one flagged example): 3,482 occurrences across every GICS-sector
# variant of this template. Almost all score neutral (3) by luck, but
# "Financials" and "Health Care" specifically score bearish (2) instead
# -- 253 occurrences -- purely because FinBERT reads something
# faintly negative in those two sector-name strings, with zero
# connection to any actual insider-trading signal (there isn't one in
# the headline to read). Forced neutral for every sector, closing that
# gap instead of leaving it to chance which sector name happens to read
# clean.
_INSIDER_REVIEW_TITLE_RE = re.compile(r"^cfa\s+[\w &]+:insider review for week ended", re.I)

# Barron's recurring "<Company1>, <Company2>, ... and More/Other Stocks That
# Explain Today's Market -- Barrons.com" listicle -- the same "multi-company
# namecheck roundup" problem _MARKET_RECAP_RE/_FUTURES_RECAP_RE/
# _SP500_FUTURES_RECAP_RE already handle for IBD/MarketWatch, just Barron's
# own template and phrasing. Confirmed via a systematic scan of this
# project's own cache: 406 occurrences across 73 distinct headlines, every
# one a bare list of company names with no direction word or per-company
# fact at all (unlike _MARKET_RECAP_RE's cousins, there isn't even an index
# up/down verb to misread -- FinBERT's inconsistent Bearish/Neutral split
# on this exact template, confirmed on SMTC's own two cached occurrences,
# is pure noise). Matched anywhere in the headline (not anchored to the
# start) since the company-name prefix varies every time and only the
# "Stocks That Explain Today's Market" phrase itself is the fixed part.
_STOCKS_EXPLAIN_MARKET_RE = re.compile(r"stocks that explains? today'?s? market", re.I)

# IBD's earnings-reaction template -- "<what happened, the real reason>.
# <Company/ticker/'Stock'> <direction verb>. -- IBD", e.g. "Chipmaker
# Semtech Smashes Sales, Earnings Targets. Stock Rises. -- IBD". Unlike the
# mechanical-move-only cases above, this headline DOES carry a real reason
# in its first sentence -- but FinBERT still misreads it on at least one
# confirmed case (the Semtech example above scored Bearish despite "Smashes
# ... Targets. Stock Rises." being unambiguously bullish), apparently
# thrown off by negation-heavy or unusual phrasing in the reason clause
# itself. IBD's own second sentence already states the market reaction in
# plain, unambiguous language -- "Stock Rises"/"Stock Falls" are a direct
# verdict, not something to infer -- so this reads the verb directly
# instead of trusting FinBERT's read of the combined text. Confirmed
# against every one of the 4 distinct cached headlines using this exact
# "Stock <verb>. -- IBD" ending. Verb list matches _MECHANICAL_MOVE_RE's
# own bullish/bearish split for consistency.
_IBD_EARNINGS_REACTION_RE = re.compile(
    r"\bstock\s+(rises?|climbs?|advances?|gains?|jumps?|rall(?:y|ies)|surges?|soars?)\.\s*--\s*ibd\s*$"
    r"|\bstock\s+(falls?|declines?|slides?|slips?|drops?|sinks?|plunges?|sheds?|skids?|tumbles?)\.\s*--\s*ibd\s*$",
    re.I,
)

# Dow Jones' "Data Talk" column -- a purely mechanical, auto-generated
# intraday-stat blurb ("<Company> Up/Down Over/Nearly N%, On Pace/Track for
# Largest Percent Increase/Decrease Since <date>", "... for Record High/Low
# Close", "... Currently Up/Down N Consecutive Days", "... Worst/Best
# Performer in the <index>", etc. -- always some permutation of these).
# Confirmed via a systematic scan of every one of this project's 269
# distinct cached "-- Data Talk" headlines: zero embed an actual reason
# (no "on earnings", "after guidance", "following FDA approval", etc. --
# checked) -- pure price/streak statistics with no company-specific
# analysis at all, same "no real signal" class _MARKET_RECAP_RE exists for,
# just at the single-stock level. Same explicit instruction this whole
# rule-set follows: "anything that only speaks about stock movements is
# just neutral." Matched on the "-- Data Talk" suffix alone, same as
# _MARKET_RECAP_RE's prefix-only match -- the column name itself reliably
# identifies the template regardless of which specific stat it reports.
_DATA_TALK_RE = re.compile(r"--\s*data talk\s*$", re.I)

# Analyst rating-action templates -- "<Company> Raised to <Rating> From
# <Rating> by <Firm>", "<Company> Cut to <Rating> From <Rating> by <Firm>",
# "<Company> Initiated at <Rating> by <Firm>", and the rarer "<Company>
# Stock Upgraded/Downgraded to <Rating>" phrasing -- all four share one
# ground truth: the TARGET rating tier itself, not the Raised/Cut/
# Initiated/Upgraded/Downgraded verb. Confirmed via a systematic scan of
# every distinct rating word appearing after these four verbs across this
# project's own cache (471 matched article instances, 21 distinct rating
# strings, all accounted for in the tier sets below) that FinBERT reads
# the rating tier itself far worse than the verb alone would suggest: it
# gets "Initiated at Buy" right only ~21% of the time (mostly reading
# Neutral instead), and on "Cut to Underweight/Underperform" it's actually
# BACKWARDS in a large share of cases -- scoring a real downgrade
# Bullish, apparently pattern-matching on the FROM-rating word (often
# itself a positive-sounding tier like "Overweight"/"Outperform") rather
# than the actual downgrade. The rating tier is explicit, structured text
# in every one of these headlines, not something to infer -- reading it
# directly removes the ambiguity FinBERT is failing on. Tier vocabulary
# (bullish/neutral/bearish split) matches how each word is actually used
# as a real research-rating tier by the firms issuing them, not guessed.
_BULLISH_RATING_TIERS = {
    "strong buy", "buy", "outperform", "overweight", "accumulate",
    "market outperform", "sector outperform", "positive",
}
_BEARISH_RATING_TIERS = {"underweight", "underperform", "sell", "reduce"}
_ANALYST_RATING_ACTION_RE = re.compile(
    r"\b(?:Raised to|Cut to|Initiated at|Upgraded to|Downgraded to)\s+"
    r"(Strong Buy|Buy|Outperform|Overweight|Accumulate|Market Outperform|Sector Outperform|Positive"
    r"|Neutral|Hold|Equal-Weight|Market Perform|Sector Perform|Peer Perform|Perform|In-Line|Sector Weight"
    r"|Underweight|Underperform|Sell|Reduce)\b",
    re.I,
)

# The recurring "$HAREHOLDER ALERT: The M&A Class Action Firm Announces An
# Investigation of <Company>" (and "...Continues to Investigate...")
# press-release template -- a securities-law firm's own boilerplate
# client-solicitation ad, filed on essentially every M&A deal regardless
# of merit, not company-specific bad news (the "$H" typo for "Sh" is the
# provider's own, present in every one of this project's cached
# occurrences). Confirmed via a systematic scan: 21 distinct occurrences,
# every one this exact solicitation template, and FinBERT is inconsistent
# on it (mostly Neutral, sometimes Bear, for what's structurally the same
# non-event every time). Deliberately narrow -- matched on "ALERT...Class
# Action Firm" specifically, NOT bare "class action" -- so a genuine,
# company-specific securities-fraud class-action headline (real bad news,
# e.g. "Faces Securities Class Action After Second Major Selloff") is left
# to FinBERT rather than swept into this net.
_SHAREHOLDER_ALERT_RE = re.compile(r"(?:\$?HAREHOLDER|Shareholder)\s+ALERT.*Class Action Firm", re.I)

# The recurring "<Ticker/Company> Investors/Shareholders (Who Lost Money/
# with Substantial Losses/with Losses in Excess of $100K) Have Opportunity
# to Lead <Company> Securities (Fraud) (Class Action) Lawsuit" (and its
# "Investor Alert: Contact <Firm> by <date> for Opportunity to Lead..."
# twin) -- a DIFFERENT securities-law firm's lead-plaintiff solicitation
# template from _SHAREHOLDER_ALERT_RE above (that one solicits around an
# M&A deal; this one solicits around a stock that already had a real
# decline -- by the time this ad runs, the underlying bad news is already
# stale/priced-in, and the ad itself is not new company-specific
# information, same "boilerplate advertising, not a fresh signal"
# reasoning). Confirmed via a systematic scan: 107 distinct occurrences,
# every one this same law-firm solicitation shape (zero false positives
# checked against non-lawsuit content), and FinBERT is badly inconsistent
# on it -- 61 Bull / 146 Neutral / 51 Bear instances for what's
# structurally the same non-event every time (the same underlying case
# gets reworded across a dozen near-identical press releases as a deadline
# approaches, and FinBERT's read swings across all three buckets for the
# same story). Matched on "Opportunity to Lead" specifically -- confirmed
# every match is already accompanied by "Securities"/"Class Action"/
# "Lawsuit" elsewhere in the headline, so this doesn't need those words in
# the pattern itself to stay narrow.
_LEAD_PLAINTIFF_SOLICITATION_RE = re.compile(r"Opportunity to Lead", re.I)

# "<Company> Is Maintained at <Rating> by <Firm>" -- the largest single
# recurring template found in this project's own cache (4,039 headline
# instances, dwarfing every other rule here including
# _ANALYST_RATING_ACTION_RE's 471), and one this module already has an
# established, explicit answer for: _PRICE_TARGET_MAINTAINED_RE's own
# comment above states "Maintained... structurally a NO-CHANGE signal (an
# analyst reiterating an existing view, not revising it)" for the price-
# target version of this same word -- confirmed to apply identically here.
# Unlike _ANALYST_RATING_ACTION_RE (Raised/Cut/Initiated -- a real change
# in view, where the target tier IS the signal), a maintained rating is
# explicitly NOT new information regardless of which tier is being
# reaffirmed, so this forces neutral rather than reading the tier the way
# _ANALYST_RATING_ACTION_RE does. Currently ~87% of cached occurrences
# already read Neutral by luck, but the remaining ~13% leak through
# inconsistently -- e.g. "AAON Is Maintained at Outperform by Baird" reads
# Bullish while "10x Genomics Is Maintained at Overweight by Stephens &
# Co." (the same bullish tier) reads Neutral -- this closes that gap for
# every tier, not just the ones FinBERT happens to get right.
_RATING_MAINTAINED_RE = re.compile(r"\bIs Maintained at\s+[\w \-]+?\s+by\b", re.I)

# "<Company> Raises/Boosts/Hikes ... Guidance/Dividend" -- FinBERT already
# gets the clear majority of these right (this is a lower-value fix than
# the ones above, closing a residual error rate rather than a systemic
# miss), but a regex removes the remaining gap entirely rather than
# leaving it to chance. Two deliberate exclusions on the guidance side,
# confirmed against real cached traps found in this project's own corpus:
# (1) "<Company> Raises Guidance, But Shares Fall/Dip For One Key Reason"
# -- the headline's own "But Shares Fall" clause states the actual market
# reaction was negative, so this is left to FinBERT (which already reads
# it Bearish) rather than forced Bullish; (2) "<Company> Had Been Expected
# to Raise Annual Guidance, CEO Says" -- "Expected to Raise" is
# hypothetical/anticipatory framing, not a raise that already happened
# (rule: a bullish/bearish verb only counts as signal when it directly
# describes something that happened, not something that was expected to).
# No dividend-side traps found on the same check. Zero false positives
# across a systematic corpus scan of all three families (guidance: 112
# distinct headlines, dividends: 100/14 boost/cut).
_GUIDANCE_RAISE_RE = re.compile(r"\bRaises?\s+.*Guidance\b", re.I)
_GUIDANCE_HYPOTHETICAL_RE = re.compile(r"\b(?:Expected|Set|Poised|Likely|May|Could|Should|Seen)\s+to\s+Raise", re.I)
_GUIDANCE_CONTRADICTS_PRICE_RE = re.compile(r"\bBut\s+Shares?\s+(?:Fall|Fell|Drop|Dip|Decline|Slide|Tumble)", re.I)
_DIVIDEND_RAISE_RE = re.compile(r"\b(?:Boosts?|Raises?|Hikes?)\s+.*Dividend\b", re.I)

# "FDA Approves ..." -- a genuine, unambiguous positive regulatory event
# every time (26 distinct cached occurrences, all a real drug/device
# approval, no "expected to approve" or conditional-framing traps found).
_FDA_APPROVES_RE = re.compile(r"\bFDA\s+Approv", re.I)

# "<Company/Product> Recalls ..." -- a genuine, unambiguous negative event
# every time (17 distinct cached occurrences, food-safety and drug recalls
# alike, no "considering a recall" hypothetical-framing traps found).
_RECALLS_RE = re.compile(r"\bRecalls?\b", re.I)

# MarketWatch/Dow Jones' auto-generated (often literally "Automated
# Insights"-branded) index-attribution template -- "<Company1>, <Company2>
# Share Gains/Losses Contribute To Dow/S&P 500/Nasdaq's N-Point Rally/Jump/
# Climb/Drop", attributed identically to EVERY namechecked ticker's own
# news feed. Same class of problem _MARKET_RECAP_RE/_FUTURES_RECAP_RE/
# _SP500_FUTURES_RECAP_RE/_STOCKS_EXPLAIN_MARKET_RE already exist for --
# this is a story about the INDEX's own move, with a couple of companies
# namechecked as incidental "who's dragging/lifting it today" color, not
# real company-specific news -- explicit instruction: "completely ignore
# the news like this where it is just an analysis of the contribution to
# an index." Confirmed via a systematic scan: 6 distinct cached headlines,
# and FinBERT's read is driven entirely by the Gains/Losses word, applied
# IDENTICALLY to both namechecked companies regardless of their own
# individual moves (e.g. "Caterpillar, Cisco Share Gains Contribute To
# Dow's 909-Point Rally" reads Bullish for both, with no distinction
# between them at all) -- exactly the shared/index-level attribution this
# whole rule family exists to catch, not a per-company signal.
_INDEX_CONTRIBUTION_RE = re.compile(
    r"Share (?:Gains|Losses) Contribute To (?:Dow|S&P ?500|Nasdaq)'?s? [\d,]+-Point", re.I
)

# The extreme labels (1/5) originally triggered at just |polarity| >= 0.55
# -- in practice FinBERT is confidently non-neutral far more often than
# it's genuinely "very" bullish/bearish (a 500-headline sample from this
# project's own cache came back 97 "very bullish" vs. only 22 "bullish",
# and 38 "very bearish" vs. 15 "bearish" -- the extreme labels dominating
# their moderate counterparts almost 5:1 is backwards from what "very"
# should mean), so the threshold was raised to a symmetric 0.97. That
# turned out to overshoot: a systematic 4,000-headline classified sample
# from this project's own non-fast-path corpus found FinBERT's actual
# polarity range on headline-only text tops out around -0.967/+0.941 --
# 0.97 sits just past BOTH tails, so it never fired at all (confirmed:
# 0/54,540 currently-scored articles land in either extreme bucket).
# Re-tuned to a symmetric 0.90, per instruction -- comfortably inside
# both observed tails (unlike 0.97) so "very" is reachable again on both
# sides, while still requiring a strong, confident read rather than just
# a clear lean. The inner (neutral-band) thresholds are unchanged.
_POLARITY_BUCKETS = [
    (-0.90, 1),
    (-0.2, 2),
    (0.2, 3),
    (0.90, 4),
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
    """Returns the fixed score (3 neutral, 2/4 mild, or 2/4 read directly
    off an explicit direction verb) for a headline that matches one of the
    recurring auto-generated templates below, or None if it needs real
    FinBERT classification -- these are the actual, proven fix for this
    project's scoring problems (see this module's own docstring on why a
    headline+body approach was tried and reverted). Each is decided from
    the headline's own wording alone -- a fixed, recognizable template
    with no real per-company signal in it, or (for the mechanical-move/
    insider-sale/IBD-reaction cases) a template FinBERT reliably misreads
    -- confirmed against this project's own cached headlines, not
    guessed."""
    if not headline:
        return 3
    if (
        _MECHANICAL_MOVE_RE.search(headline)
        or _MARKET_RECAP_RE.search(headline)
        or _FUTURES_RECAP_RE.search(headline)
        or _SP500_FUTURES_RECAP_RE.search(headline)
        or _PRICE_TARGET_MAINTAINED_RE.search(headline)
        or _EARNINGS_CALENDAR_RE.search(headline)
        or _INSIDER_REVIEW_TITLE_RE.search(headline)
        or _STOCKS_EXPLAIN_MARKET_RE.search(headline)
        or _DATA_TALK_RE.search(headline)
        or _SHAREHOLDER_ALERT_RE.search(headline)
        or _RATING_MAINTAINED_RE.search(headline)
        or _LEAD_PLAINTIFF_SOLICITATION_RE.search(headline)
        or _INDEX_CONTRIBUTION_RE.search(headline)
    ):
        return 3
    if _INSIDER_SALES_RE.search(headline):
        return 2
    if _INSIDER_PURCHASES_RE.search(headline):
        return 4
    if _FDA_APPROVES_RE.search(headline):
        return 4
    if _RECALLS_RE.search(headline):
        return 2
    if _DIVIDEND_RAISE_RE.search(headline):
        return 4
    if (
        _GUIDANCE_RAISE_RE.search(headline)
        and not _GUIDANCE_HYPOTHETICAL_RE.search(headline)
        and not _GUIDANCE_CONTRADICTS_PRICE_RE.search(headline)
    ):
        return 4
    ibd_reaction = _IBD_EARNINGS_REACTION_RE.search(headline)
    if ibd_reaction:
        return 4 if ibd_reaction.group(1) else 2
    rating_action = _ANALYST_RATING_ACTION_RE.search(headline)
    if rating_action:
        tier = rating_action.group(1).lower()
        if tier in _BULLISH_RATING_TIERS:
            return 4
        if tier in _BEARISH_RATING_TIERS:
            return 2
        return 3
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
    the article's score entirely. The body's own generic provider-footer/
    legal-disclaimer boilerplate is stripped first (see
    strip_boilerplate) -- confirmed to matter, not just tidiness: left
    in, its hedge-heavy "not independent research" language measurably
    dilutes an otherwise-clear signal."""
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
        if body:
            body = strip_boilerplate(body)
        text = f"{h}. {body[:_MAX_BODY_CHARS]}" if body else h
        to_classify_idx.append(i)
        texts.append(text)
    if to_classify_idx:
        classified = _classify(texts)
        for i, score in zip(to_classify_idx, classified):
            scores[i] = score
    return scores


# ─── Headline importance (separate from bull/bear sentiment above) ──────────
#
# A star rating (0/1/3, see headline_importance below) answering a different
# question than score_headlines/score_articles: not "is this bullish or
# bearish" but "does this headline matter at all." Explicit instruction:
# earnings/revenue news should outrank macro/index-recap noise, with stars
# in the UI to tell important news apart from garbage at a glance. Reuses
# the SAME template-recognition regexes fast_path_score already has (a
# market recap, a Data Talk blurb, a shareholder-alert ad, etc. are exactly
# as unimportant as they are unscoreable for sentiment) plus a few new ones
# for the two tiers fast_path_score has no reason to distinguish between
# (sentiment-neutral doesn't mean importance-low, and vice versa -- a
# "Reports Second Quarter Results" headline is often sentiment-neutral on
# its own, per bare_reports_results in data/news_regex_candidates.json, but
# it's exactly the HIGH-importance earnings news this feature exists for).

# The largest single headline category in this project's own cache (4,652
# distinct occurrences) and, until now, matched by no fast_path_score rule
# at all (only the AGGREGATE "Substantial Insider Sales: Afternoon Report"
# template is, via _INSIDER_SALES_RE/_INSIDER_PURCHASES_RE) -- a single
# officer's routine transaction, reported one at a time, every time an SEC
# Form 4 is filed. Real but routine: Low importance, not Medium, the same
# tier as the aggregate reports' own sentiment treatment already implies.
_FORM4_TRANSACTION_RE = re.compile(
    r"^(?:CEO|CFO|COO|Pres|Chairman|EVP|SVP|VP|Officer|Dir)\s+\S.*"
    r"\b(?:Sells|Buys|Registers|Acquires|Disposes)\s+[\d,]+\s+Of\s+.*\s+>[A-Z.]+$",
    re.I,
)

# The earnings-release family, in its three recurring shapes -- confirmed
# via a systematic scan of this project's own cache: (1) the release's own
# title, "<Company> Reports/Announces <Quarter> <Year> (Financial)
# Results/Revenue" (732 distinct); (2) that SAME title's numbered
# continuation parts, "...<Quarter> <Year> -2-" etc. (478 distinct -- see
# this module's own module-level TODO on the still-unfixed continuation-
# chunk-inconsistency problem elsewhere; importance-wise these are just as
# much the earnings release as part 1 is); (3) the individual per-metric
# wire blurbs releases get split into, "* <Company> <N>Q (Adj) EPS/Sales/
# Net/Rev $X" (3,652 distinct). High importance regardless of which shape
# -- this is the actual fundamental data this whole feature exists to
# surface above macro noise.
_EARNINGS_RESULTS_RE = re.compile(
    r"\b(?:Reports|Announces)\s+(?:First|Second|Third|Fourth)\s+Quarter\s+\d{4}\s+(?:Financial\s+)?(?:Results|Revenue)\b",
    re.I,
)
_EARNINGS_RESULTS_CONTINUATION_RE = re.compile(
    r"\b(?:Reports|Announces)\s+(?:First|Second|Third|Fourth)\s+Quarter\s+\d{4}\s*-\d+-\s*$", re.I
)
_EARNINGS_FACTOID_RE = re.compile(r"^\*\s+\S.*\b[1-4]Q\s+(?:Adj\s+)?(?:EPS|Sales|Net|Rev|Revenue)\b", re.I)

# Forward guidance/outlook headlines that don't use the literal "Raises...
# Guidance" wording _GUIDANCE_RAISE_RE requires -- "Sees"/"Forecasts"/
# "Expects"/"Projects" framings instead ("Nvidia Forecasts 70% Rise In
# Revenue In 2028 -- WSJ", "Automatic Data Sees FY27 Revenue Up 5%-6%").
# User-flagged: the WSJ headline above was landing Medium despite being
# squarely the earnings/revenue-forecast news this feature exists to
# surface. Confirmed via a systematic scan: requiring all three of a
# quantified figure (a percent sign), an earnings/revenue keyword, AND a
# forecast/outlook verb -- not any one alone -- found 136 distinct
# matches, every one a genuine, specific company revenue/sales/EPS
# outlook statement, no false positives found in a full read of the
# results. Three independent conditions anded together, deliberately
# narrower than any single one of them would be on its own.
_FORECAST_VERB_RE = re.compile(r"\b(?:Forecast|Forecasts|Sees|Expects|Projects|Outlook|Guidance)\b", re.I)
_EARNINGS_KEYWORD_RE = re.compile(r"\b(?:Revenue|Earnings|Sales|Profit|EPS)\b", re.I)
_PERCENT_FIGURE_RE = re.compile(r"\d+%")

# The earnings-reaction-analysis family -- prose journalism headlines
# (Barron's/IBD/WSJ house style) reporting an actual beat/miss against
# Street estimates, e.g. "How Nvidia Blew Up Wall Street's Expectations
# Game -- WSJ" (user-flagged: no percent figure, no bare "Revenue"/
# "Earnings" keyword either, so neither _EARNINGS_RESULTS_RE nor the
# forecast-verb-and-percent rule above caught it). Confirmed via a
# systematic scan: a reaction verb (Beat/Blew Up/Miss/Topped/Exceeded/
# Crushed/Smashed) combined with an Expectations/Estimates/Consensus/
# Forecasts noun -- 97 distinct matches, every one read as a genuine
# earnings-reaction piece in a full sample review. Deliberately doesn't
# care about direction (beat vs. miss, stock up vs. down on the news) --
# importance and sentiment are different questions (see this section's
# own module comment); "Corning Earnings Beat Estimates. Why It's Not
# Enough as the Stock Sinks." is just as much earnings news as one where
# the stock rallies on the beat.
_EARNINGS_REACTION_ANALYSIS_RE = re.compile(
    r"\b(?:Beats?|Blew Up|Blows? Up|Miss(?:es|ed)?|Topp?ed|Exceeded?|Surpassed?|Crush(?:es|ed)?|Smash(?:es|ed)?)\b"
    r".{0,40}\b(?:Expectations?|Estimates?|Consensus|Forecasts?)\b",
    re.I,
)

# Low-importance fast_path_score templates reused here -- every one of
# these is either a macro/index-level recap with no per-company substance
# (_MARKET_RECAP_RE and its IBD/MarketWatch/Barron's/Dow-point-drop
# cousins), a pure calendar listing or boilerplate title with no content
# of its own (_EARNINGS_CALENDAR_RE, _INSIDER_REVIEW_TITLE_RE), a
# solicitation ad (_SHAREHOLDER_ALERT_RE, _LEAD_PLAINTIFF_SOLICITATION_RE),
# or a no-change reaffirmation stating nothing new happened
# (_RATING_MAINTAINED_RE, _PRICE_TARGET_MAINTAINED_RE) -- see each one's
# own comment above for why. Not fast_path_score's full rule set --
# _INSIDER_SALES_RE/_INSIDER_PURCHASES_RE/_DIVIDEND_RAISE_RE/
# _ANALYST_RATING_ACTION_RE (a real Raised/Cut/Initiated) are real,
# routine-but-not-noise news, so they're deliberately left OUT of this
# list to land in the Medium default below instead.
_LOW_IMPORTANCE_RES = (
    _MARKET_RECAP_RE,
    _FUTURES_RECAP_RE,
    _SP500_FUTURES_RECAP_RE,
    _STOCKS_EXPLAIN_MARKET_RE,
    _INDEX_CONTRIBUTION_RE,
    _DATA_TALK_RE,
    _MECHANICAL_MOVE_RE,
    _EARNINGS_CALENDAR_RE,
    _INSIDER_REVIEW_TITLE_RE,
    _SHAREHOLDER_ALERT_RE,
    _LEAD_PLAINTIFF_SOLICITATION_RE,
    _RATING_MAINTAINED_RE,
    _PRICE_TARGET_MAINTAINED_RE,
    _FORM4_TRANSACTION_RE,
)

def headline_importance(headline):
    """Returns a star count -- 0 (Low), 1 (Medium), or 3 (High) -- for how
    much this headline matters, independent of score_headlines/
    score_articles' own bull/bear read above (see this section's own
    module comment for why these are deliberately separate questions). Low is every recurring
    template fast_path_score already knows carries no real per-company
    signal, plus the newly-added per-transaction Form 4 template; High is
    the earnings-release family plus guidance/forecast/FDA/recall
    (genuinely fundamental, market-moving events) plus a rating action
    that reaches Strong Buy/Strong Sell specifically (the most extreme,
    highest-conviction analyst calls -- a lesser Buy/Sell/Outperform/
    Underperform stays Medium); everything else -- most real, non-
    templated company news, which doesn't fit a rigid recurring template
    at all -- defaults to Medium rather than Low, so genuinely important
    untemplated news is never mislabeled as garbage.

    Known real gap, not fixable by adding another regex: a well-written,
    non-formulaic journalism headline with no standard financial
    vocabulary at all (e.g. a WSJ analysis piece titled "How Nvidia Blew
    Up Wall Street's Expectations Game") genuinely can't be distinguished
    from an unimportant one by keyword/template matching -- there's no
    recurring phrase to anchor a rule to without it being a one-headline
    overfit. Rule-based importance, like fast_path_score's rule-based
    sentiment, has a real recall ceiling on prose headlines; only a
    semantic (not keyword) read could close this gap, which is a
    materially bigger undertaking than this feature's existing regex
    approach."""
    if not headline:
        return 1
    if any(r.search(headline) for r in _LOW_IMPORTANCE_RES):
        return 0
    rating_action = _ANALYST_RATING_ACTION_RE.search(headline)
    if (
        _EARNINGS_RESULTS_RE.search(headline)
        or _EARNINGS_RESULTS_CONTINUATION_RE.search(headline)
        or _EARNINGS_FACTOID_RE.search(headline)
        or _FDA_APPROVES_RE.search(headline)
        or _RECALLS_RE.search(headline)
        or _IBD_EARNINGS_REACTION_RE.search(headline)
        or (
            _GUIDANCE_RAISE_RE.search(headline)
            and not _GUIDANCE_HYPOTHETICAL_RE.search(headline)
            and not _GUIDANCE_CONTRADICTS_PRICE_RE.search(headline)
        )
        or (_PERCENT_FIGURE_RE.search(headline) and _EARNINGS_KEYWORD_RE.search(headline) and _FORECAST_VERB_RE.search(headline))
        or _EARNINGS_REACTION_ANALYSIS_RE.search(headline)
        or (rating_action and rating_action.group(1).lower() in ("strong buy", "strong sell"))
    ):
        return 3
    return 1
