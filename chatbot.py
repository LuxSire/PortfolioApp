"""
chatbot.py — LangChain + Ollama tool-calling chatbot for the Recommendations
tab, answering free-form questions using this project's own data: screener
scores/ratings (sorted_screen.csv), the Recommendations candidate pool
(data/recommendations.json), news+sentiment (data/news.json), insider Form 4
activity (data/sec/form4/insider_transactions.json), 13F institutional
holdings (data/sec/13f/institutional_holdings.json), theme tags
(data/theme_taxonomy.json / data/ticker_themes.json), business descriptions
(data/raw_data.json's longBusinessSummary), price history
(data/price_history.json), and live positions/prices/account status (passed
in per-call as `live_state`, not read from a file -- see answer_question).

Local-only, no external API -- same precedent as news_sentiment.py's FinBERT
and theme_classifier.py's zero-shot model, extended here via Ollama (a local
model server this machine talks to over localhost:11434) instead of a
directly-loaded HuggingFace pipeline, since a general-purpose chat/reasoning
model is a different class of task than those two narrow classifiers.

A tool-calling agent (langchain.agents.create_agent, backed by
langchain_ollama.ChatOllama), not a RAG/vector-store pipeline -- almost
everything here is already structured (CSV/JSON with a fixed schema), so a
named-function-call per data source is both simpler and more accurate than
embedding-search would be for "what's AAPL's forward PE" style questions.
Free text (news headlines, business summaries) is exposed as its own tool
rather than pre-embedded, since it's a small enough slice to just hand the
model directly when asked.

Standalone module, own file reads (no import from main.py/scoring.py) --
same arm's-length convention sec_edgar.py/social_sentiment.py already use,
so ib_server.py (which imports this lazily, see its /api/chat
handler) never risks a circular import back into itself.

Known scope boundary: the Recommendations tab's own Long/Short/To-close
selection logic (momentum direction gate, sector/theme hedge-preference
bonus, the 15-per-side cap) lives in RecommendationsView.jsx, not in any
file this module can read. get_recommendations below gives the model
everything it needs to reason about a ticker's own score/rating/momentum/
short-interest/signals, which covers most "why isn't X shown" questions,
but this module can't literally replicate that frontend ranking algorithm.
"""
import csv
import json
import os
from datetime import datetime, timedelta
from typing import Optional

from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_ollama import ChatOllama

DATA_DIR = "data"
SORTED_SCREEN_CSV = "sorted_screen.csv"
RAW_DATA_FILE = os.path.join(DATA_DIR, "raw_data.json")
NEWS_FILE = os.path.join(DATA_DIR, "news.json")
RECOMMENDATIONS_FILE = os.path.join(DATA_DIR, "recommendations.json")
FORM4_FILE = os.path.join(DATA_DIR, "sec", "form4", "insider_transactions.json")
THIRTEENF_FILE = os.path.join(DATA_DIR, "sec", "13f", "institutional_holdings.json")
THEME_TAXONOMY_FILE = os.path.join(DATA_DIR, "theme_taxonomy.json")
TICKER_THEMES_FILE = os.path.join(DATA_DIR, "ticker_themes.json")
PRICE_HISTORY_FILE = os.path.join(DATA_DIR, "price_history.json")

OLLAMA_MODEL = "llama3.1"

# Caps how many rows a single tool call can return. Unbounded, a
# search_screener/get_recommendations call against the ~1,800-ticker
# universe would blow the model's context window and drown the one answer
# it actually needs in noise it never asked for.
MAX_ROWS = 25

SYSTEM_PROMPT = (
    "You are a research assistant for this user's personal stock screener and "
    "IBKR portfolio. Answer only using the tools provided -- never invent a "
    "score, price, rating, or fact you haven't looked up. If a tool has "
    "nothing on file for a ticker, say so plainly rather than guessing. Give "
    "a comprehensive, well-structured answer: use multiple paragraphs or "
    "bullet points when the question calls for it, not a one-line reply.\n\n"
    "IMPORTANT -- how to use tools: some questions need more than one tool "
    "call, one after another, where a later call's arguments depend on an "
    "earlier call's result (e.g. get_recommendations for a ticker, then "
    "get_news for that same ticker for more detail). When you realize you "
    "need another tool call, MAKE THE CALL -- do not describe your plan, do "
    "not write the tool name or its arguments as text, and do not stop to "
    "explain what you are about to do. A response that contains anything "
    "shaped like {\"name\": ..., \"parameters\": ...} as plain text is always "
    "wrong -- that must be a real tool call instead, never words in your "
    "answer. Only write your final answer once every tool call you need has "
    "actually been made and you have every fact required."
)


def _load_json_or_empty(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _load_screener_rows():
    try:
        with open(SORTED_SCREEN_CSV, newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


# raw_data.json is ~23MB -- loaded once on first get_business_summary call
# and cached here, not re-read from disk on every call.
_raw_data_cache = None


def _raw_data():
    global _raw_data_cache
    if _raw_data_cache is None:
        _raw_data_cache = _load_json_or_empty(RAW_DATA_FILE)
    return _raw_data_cache


@tool
def search_screener(
    ticker: Optional[str] = None,
    rating: Optional[str] = None,
    sector: Optional[str] = None,
    top_n: Optional[int] = None,
    worst_first: bool = False,
) -> str:
    """Search the full stock screener universe (sorted_screen.csv -- every
    tracked ticker, not just held positions or Strong Buy/Sell names).
    Filter by an exact ticker, a rating ("Strong Buy", "Buy", "Hold",
    "Sell", "Strong Sell", or "NA" for tickers with no positive forward
    P/E to score), and/or a sector (substring match, case-insensitive).
    top_n caps how many rows come back (best score first by default, or
    worst_first=True for the weakest names). Returns one line per ticker:
    rating, score percentile, forward P/E, price, sector."""
    rows = _load_screener_rows()
    if ticker:
        rows = [r for r in rows if r["ticker"].upper() == ticker.upper()]
    if rating:
        rows = [r for r in rows if r.get("rating", "").lower() == rating.lower()]
    if sector:
        rows = [r for r in rows if sector.lower() in (r.get("sector") or "").lower()]
    scored = [r for r in rows if r.get("score")]
    scored.sort(key=lambda r: float(r["score"]), reverse=worst_first)
    scored = scored[: (top_n or MAX_ROWS)]
    if not scored:
        return "No matching tickers found."
    lines = []
    for r in scored:
        pct = round(float(r["score"]) * 100, 1)
        lines.append(
            f"{r['ticker']} ({r['name']}): {r['rating']}, score percentile {pct}, "
            f"forward P/E {r.get('forwardPE') or '—'}, price ${r.get('price') or '—'}, "
            f"sector {r.get('sector') or '—'}"
        )
    return "\n".join(lines)


@tool
def get_recommendations(ticker: Optional[str] = None) -> str:
    """Look up this app's own Recommendations candidate pool
    (data/recommendations.json) -- every Strong Buy/Buy/Sell/Strong Sell
    ticker's composite score, momentum, recent news sentiment (last 7
    days), insider Form 4 activity (last 90 days), and 13F
    quarter-over-quarter institutional share change. Pass a ticker to look
    up just that one; omit it for the full list (capped, best score
    first). Best source for "why is X rated the way it is" or "what's
    changed recently for X" questions."""
    data = _load_json_or_empty(RECOMMENDATIONS_FILE)
    candidates = data.get("candidates", [])
    if ticker:
        candidates = [c for c in candidates if c["ticker"].upper() == ticker.upper()]
        if not candidates:
            return (
                f"{ticker.upper()} isn't in the Recommendations candidate pool "
                "(not currently rated Strong Buy/Buy/Sell/Strong Sell)."
            )
    else:
        candidates = sorted(candidates, key=lambda c: c.get("score") if c.get("score") is not None else 1)[:MAX_ROWS]
    lines = []
    for c in candidates:
        news = c.get("news7d") or {}
        insiders = c.get("insiders90d") or {}
        lines.append(
            f"{c['ticker']} ({c['name']}): {c['rating']}, score percentile {c['scorePercentile']}, "
            f"momentum {c['momentum']}, news last 7d: {news.get('bullish', 0)} bullish/{news.get('bearish', 0)} bearish, "
            f"insiders last 90d: {insiders.get('buys', 0)} buys/{insiders.get('sells', 0)} sells, "
            f"institutional 13F QoQ share change: {c.get('instChangeQoQ')}, "
            f"analyst target upside: {c.get('targetUpside')}, short % of float: {c.get('shortPercentOfFloat')}"
        )
    return "\n".join(lines)


@tool
def get_news(ticker: str, days: int = 7) -> str:
    """Recent news headlines and FinBERT sentiment score (1 = very
    bearish, 5 = very bullish, 3 = neutral) for one ticker, from
    data/news.json."""
    news = _load_json_or_empty(NEWS_FILE)
    articles = news.get(ticker.upper(), [])
    cutoff = datetime.now() - timedelta(days=days)
    recent = []
    for a in articles:
        try:
            t = datetime.fromisoformat(a["time"])
        except (KeyError, ValueError):
            continue
        if t >= cutoff:
            recent.append(a)
    if not recent:
        return f"No news for {ticker.upper()} in the last {days} days."
    recent.sort(key=lambda a: a["time"], reverse=True)
    lines = [f"{a['time']} [{a.get('sentiment')}/5] {a['headline']}" for a in recent[:MAX_ROWS]]
    return "\n".join(lines)


@tool
def get_insider_activity(ticker: str) -> str:
    """SEC Form 4 open-market insider buy/sell transactions for one ticker
    (data/sec/form4/insider_transactions.json) -- only purchases (P) and
    sales (S), not routine compensation mechanics like option exercises or
    tax withholding."""
    filings = _load_json_or_empty(FORM4_FILE).get(ticker.upper(), [])
    lines = []
    for filing in filings:
        for tx in filing.get("transactions", []):
            if tx.get("code") in ("P", "S"):
                action = "bought" if tx["code"] == "P" else "sold"
                lines.append(
                    f"{tx['date']}: {filing.get('insiderName')} ({filing.get('officerTitle') or 'insider'}) "
                    f"{action} {tx.get('shares')} shares at ${tx.get('pricePerShare')}"
                )
    if not lines:
        return f"No open-market insider buy/sell activity on file for {ticker.upper()}."
    lines.sort(reverse=True)
    return "\n".join(lines[:MAX_ROWS])


@tool
def get_institutional_holdings(ticker: str) -> str:
    """Latest-quarter 13F institutional ownership for one ticker
    (data/sec/13f/institutional_holdings.json): total value/shares held by
    institutions, holder count, and quarter-over-quarter share change."""
    d = _load_json_or_empty(THIRTEENF_FILE).get(ticker.upper())
    if not d:
        return f"No 13F institutional-holdings match for {ticker.upper()}."
    change = d.get("pctShareChangeQoQ")
    change_text = f"{change * 100:.1f}% change in shares held vs. last quarter" if change is not None else "no prior-quarter comparison available"
    return (
        f"{ticker.upper()}: institutions hold ${d.get('totalValueUsd', 0):,.0f} across "
        f"{d.get('totalShares', 0):,.0f} shares, {d.get('holderCount')} holders, {change_text}"
    )


@tool
def get_themes(ticker: Optional[str] = None) -> str:
    """Thematic exposure tags for a ticker (e.g. "gold_precious_metals",
    "semiconductors_ai") from data/ticker_themes.json, or the full theme
    taxonomy with descriptions (data/theme_taxonomy.json) if no ticker is
    given."""
    if ticker:
        themes = _load_json_or_empty(TICKER_THEMES_FILE).get(ticker.upper())
        if not themes:
            return f"No theme tags on file for {ticker.upper()}."
        return f"{ticker.upper()}: {', '.join(themes)}"
    taxonomy = _load_json_or_empty(THEME_TAXONOMY_FILE)
    if not isinstance(taxonomy, list):
        return "No theme taxonomy file found."
    return "\n".join(f"{t['key']}: {t['label']} -- {t['description']}" for t in taxonomy)


@tool
def get_business_summary(ticker: str) -> str:
    """What a company actually does -- yfinance's longBusinessSummary for
    one ticker, from data/raw_data.json."""
    info = _raw_data().get(ticker.upper())
    if not info or not info.get("longBusinessSummary"):
        return f"No business summary on file for {ticker.upper()}."
    return info["longBusinessSummary"]


@tool
def get_price_history(ticker: str, days: int = 30) -> str:
    """Recent daily closing prices for one ticker, from
    data/price_history.json (~1 month of yfinance daily closes)."""
    series = _load_json_or_empty(PRICE_HISTORY_FILE).get(ticker.upper(), [])
    if not series:
        return f"No price history on file for {ticker.upper()}."
    recent = series[-days:]
    return "\n".join(f"{b['date']}: ${b['close']}" for b in recent)


def _make_live_tools(live_state):
    """Builds list_positions/get_account_summary closing over a snapshot of
    ib_server.py's own in-memory state (positions_by_ticker,
    last_price_by_ticker, account_status), taken under its lock right
    before this call -- see answer_question. Fresh tools per call (agent
    construction is cheap; the model itself is the already-running Ollama
    server, not something this loads) rather than module-level mutable
    state, so concurrent /api/chat requests can't race each other."""
    live_state = live_state or {}
    positions = live_state.get("positions") or {}
    prices = live_state.get("prices") or {}
    account = live_state.get("account") or {}

    @tool
    def list_positions() -> str:
        """Current held positions (live, from IB Gateway): shares, average
        cost, and current price for every open position. Positive shares
        means long, negative means short. For "what sector/theme am I
        long or short in" questions, use get_position_sectors_and_themes
        instead -- it already joins positions against sector/theme data in
        one call."""
        rows = [(t, p) for t, p in positions.items() if p.get("shares")]
        if not rows:
            return "No open positions (or ib_server.py isn't connected to IB Gateway)."
        lines = []
        for ticker, p in sorted(rows):
            shares = p["shares"]
            price = (prices.get(ticker) or {}).get("last")
            side = "long" if shares > 0 else "short"
            lines.append(
                f"{ticker}: {abs(shares):,.0f} shares ({side}), avg cost ${p.get('avgCost')}, "
                f"current price {'$' + str(price) if price else 'unknown'}"
            )
        return "\n".join(lines)

    @tool
    def get_account_summary() -> str:
        """Current account-level figures (live, from IB Gateway): net
        liquidation value, buying power, and similar account status
        tags."""
        if not account:
            return "No account data available (ib_server.py isn't connected to IB Gateway)."
        return "\n".join(f"{k}: {v}" for k, v in account.items())

    @tool
    def get_position_sectors_and_themes() -> str:
        """Every held position's ticker, side (long/short), sector, and
        theme tags, in one lookup -- the direct source for "what
        sectors/themes am I long or short in" or "am I overweight any
        sector" style questions. Already joins live positions against
        sorted_screen.csv's sector and ticker_themes.json's theme tags, so
        prefer this over combining list_positions with a separate
        search_screener/get_themes call per ticker."""
        rows = [(t, p) for t, p in positions.items() if p.get("shares")]
        if not rows:
            return "No open positions."
        screener_by_ticker = {r["ticker"]: r for r in _load_screener_rows()}
        themes_by_ticker = _load_json_or_empty(TICKER_THEMES_FILE)
        lines = []
        for ticker, p in sorted(rows):
            side = "long" if p["shares"] > 0 else "short"
            sector = (screener_by_ticker.get(ticker) or {}).get("sector") or "unknown"
            themes = ", ".join(themes_by_ticker.get(ticker, [])) or "untagged"
            lines.append(f"{ticker} ({side}): sector={sector}, themes={themes}")
        return "\n".join(lines)

    return [list_positions, get_account_summary, get_position_sectors_and_themes]


def answer_question(question, history=None, live_state=None):
    """Runs one turn of the chatbot. `history` is a list of
    {"role": "user" | "assistant", "content": str} from prior turns in
    this conversation -- the caller (ib_server.py's /api/chat
    handler) keeps no server-side session, the frontend resends the whole
    history each turn, same stateless-per-request convention every other
    endpoint in this app already follows. `live_state` is
    {"positions": ..., "prices": ..., "account": ...}, a snapshot of
    ib_server.py's own in-memory state at call time; None/omitted
    just means the live-data tools report no data, same as
    ib_server.py not being connected to IB Gateway."""
    tools = [
        search_screener,
        get_recommendations,
        get_news,
        get_insider_activity,
        get_institutional_holdings,
        get_themes,
        get_business_summary,
        get_price_history,
        *_make_live_tools(live_state),
    ]
    llm = ChatOllama(model=OLLAMA_MODEL, temperature=0)
    agent = create_agent(llm, tools, system_prompt=SYSTEM_PROMPT)

    messages = list(history or [])
    messages.append({"role": "user", "content": question})
    result = agent.invoke({"messages": messages})
    return result["messages"][-1].content
