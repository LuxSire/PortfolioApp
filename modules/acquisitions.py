"""acquisitions.py -- deactivate tickers whose news says they are being (or have
been) acquired -- explicit instruction: a pending or completed takeover pins the
price to the deal terms, so the name must leave the screen, the backtest pool and
the robot's candidates.

Reads data/IB/news.json ({ticker: [articles]}), flags a ticker only when one of
ITS OWN headlines names the company itself as the TARGET of a deal:

  "<buyer> to acquire|buy <TARGET>"         "<TARGET> to be acquired"
  "completes acquisition of <TARGET>"       "<TARGET> agrees to be bought/sold"
  "deal values <TARGET> at $N/share"        "<buyer> buyout of <TARGET>"

<TARGET> must be the ticker or the start of the company's own name (raw_data.json
shortName/longName without the Inc/Corp suffix). That is what keeps the acquirer
safe: Paramount's news bucket says "Paramount completes acquisition of Warner
Bros.", which matches WBD's name, not Paramount's.

Deactivation = "active": 0 in symbols.json plus an "inactiveReason" carrying the
headline and date, logged to data/acquisition_targets.json. Never re-activates
anything on its own: if a deal breaks, set "active" back to 1 by hand.
"""

import json
import os
import re

SYMBOLS_FILE = "symbols.json"
NEWS_FILE = os.path.join("data", "IB", "news.json")
RAW_DATA_FILE = os.path.join("data", "yfinance", "raw_data.json")
TARGETS_FILE = os.path.join("data", "acquisition_targets.json")

_SUFFIX = re.compile(
    r"[,.]?\s+(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|holdings?|group|n\.?v|s\.?a|se|ag|"
    r"l\.?p|llc|technologies|international|class [a-z])\.?$",
    re.I,
)
# Verbs whose OBJECT is the target ("<buyer> to acquire <T>").
_OBJ = r"(?:to|will|agrees? to|agreed to|deal to|plans? to|offers? to|moves? to|nears? deal to)\s+(?:acquire|buy|take over|purchase)"
_OBJ2 = r"(?:completes?|completed|closes?|closed|announces?|finali[sz]es?)\s+(?:the\s+)?(?:acquisition|purchase|takeover|buyout)\s+of"
_OBJ3 = r"(?:acquisition of|buyout of|takeover of|bid for|offer for|deal for|merger with)"
# Subject is the target ("<T> to be acquired").
_SUBJ = r"(?:to be (?:acquired|bought|taken private|sold)|agrees? to be (?:acquired|bought|sold)|agreed to be (?:acquired|bought)|"\
        r"to go private|accepts? (?:takeover|buyout) offer|nears? sale to|agrees? to sell itself)"


# Not (yet) a deal: talks, rumours, failed or abandoned bids (checked after _DEAL_DEAD).
_NOT_A_DEAL = re.compile(
    r"\b(talks?|walk(?:s|ed)? away|explor\w*|consider\w*|weigh\w*|mulls?|rejects?|rebuff\w*|abandon\w*|"
    r"terminat\w*|scrap\w*|calls? off|drops? (?:bid|offer|pursuit)|approach\w*|interest in|rumou?r\w*|could|may)\b",
    re.I,
)
# A newer headline like this about the company means the deal is dead: stop looking.
_DEAL = r"(?:bid|offer|proposal|deal|merger|acquisition|takeover|buyout|agreement|pursuit)"
_DEAL_DEAD = re.compile(
    rf"\b(?:(?:withdraw\w*|terminat\w*|scrap\w*|calls? off|abandon\w*|blocks?|blocked|drops?|ends?)\s+(?:\w+\s+){{0,4}}?{_DEAL}s?|"
    rf"walk(?:s|ed)? away|{_DEAL}s? (?:collaps\w*|fails|falls apart|is off|terminated|blocked))\b",
    re.I,
)
# What may follow the target's name: end of headline or a clause boundary -- so "buy
# Nvidia AI chips" (a product) and "Boeing's Wisk unit" (a division) don't count.
_AFTER = r"(?=\s*(?:$|for\b|in\b|at\b|--|,|\.|\(|:|;|>|\s-\s|with\b|from\b|to\b|after\b|as\b|and\b|remains?\b|is\b|has\b|deal\b|-\d))"
_GENERIC_FIRST = {"the", "first", "american", "united", "general", "national", "global", "international", "new"}


def _names(ticker, info):
    """Ticker plus every leading-word prefix (>= 4 chars) of the company's name,
    longest first: "Warner Bros. Discovery", "Warner Bros.", "Warner"."""
    names = {ticker}
    for key in ("shortName", "longName"):
        n = ((info or {}).get(key) or "").strip().rstrip(" -")
        for _ in range(3):
            n = _SUFFIX.sub("", n).strip().rstrip(" -,")
        words = n.split()
        for k in range(1, len(words) + 1):
            p = " ".join(words[:k]).rstrip(",")
            if len(p) >= 4 and not (k == 1 and p.lower() in _GENERIC_FIRST):
                names.add(p)
    return sorted(names, key=len, reverse=True)


def _target_hit(headline, names):
    h = headline.replace("’", "'")
    if _NOT_A_DEAL.search(h):
        return None
    for n in names:
        nm = re.escape(n)
        word = rf"(?<![A-Za-z]){nm}(?![A-Za-z])"
        if (re.search(rf"\b(?:{_OBJ}|{_OBJ2}|{_OBJ3})\s+(?:the\s+)?{word}(?!'s){_AFTER}", h, re.I)
                or re.search(rf"{word}(?:'s)?\s+(?:\w+\s+){{0,3}}?{_SUBJ}", h, re.I)
                or re.search(rf"deal values\s+{word}\s+at\s+\$", h, re.I)):
            return n
    return None


def find_targets(news=None, raw=None):
    """{ticker: {"headline", "time", "match"}} -- the newest qualifying headline per ticker."""
    if news is None:
        with open(NEWS_FILE) as f:
            news = json.load(f)
    if raw is None:
        with open(RAW_DATA_FILE) as f:
            raw = json.load(f)
    hits = {}
    for ticker, articles in news.items():
        if not isinstance(articles, list):
            continue
        names = _names(ticker, raw.get(ticker))
        for a in sorted(articles, key=lambda x: str(x.get("time") or ""), reverse=True):
            headline = a.get("headline") or ""
            if _DEAL_DEAD.search(headline) and any(re.search(rf"(?<![A-Za-z]){re.escape(n)}(?![A-Za-z])", headline, re.I) for n in names):
                break  # newest news says the deal is off
            m = _target_hit(headline, names)
            if m:
                hits[ticker] = {"headline": headline, "time": str(a.get("time") or "")[:19], "match": m}
                break
    return hits


def deactivate_acquisition_targets(dry_run=False):
    """Sets active=0 for every active ticker find_targets flags. Returns the newly
    deactivated {ticker: hit}."""
    try:
        hits = find_targets()
    except (OSError, json.JSONDecodeError) as exc:
        print(f"acquisitions: skipped ({exc!r})")
        return {}
    with open(SYMBOLS_FILE) as f:
        symbols = json.load(f)
    newly = {}
    for s in symbols:
        t = (s.get("symbol") or "").strip().upper()
        if t in hits and s.get("active") == 1:
            newly[t] = hits[t]
            if not dry_run:
                s["active"] = 0
                s["inactiveReason"] = f"acquisition {hits[t]['time'][:10]}: {hits[t]['headline']}"
    for t, h in newly.items():
        print(f"acquisitions: {'would deactivate' if dry_run else 'deactivated'} {t} -- {h['time'][:10]} {h['headline']}")
    if newly and not dry_run:
        with open(SYMBOLS_FILE, "w") as f:
            json.dump(symbols, f, indent=2)
            f.write("\n")
        try:
            with open(TARGETS_FILE) as f:
                log = json.load(f)
        except (OSError, json.JSONDecodeError):
            log = {}
        log.update(newly)
        with open(TARGETS_FILE, "w") as f:
            json.dump(log, f, indent=2)
    return newly
