"""Catalog of every variable the app DOWNLOADS (yfinance, Eulerpool, FINRA, SEC)
and where each one is used -- feeds the "Data variables" tab of the Maths page
(web/public/maths_variables.json). Run `python -m modules.variable_catalog` to
regenerate after changing the pipeline.

A variable counts as USED when its name is read by code: a string constant /
attribute inside any function (or a module-level table, except main.py's
column lists, which only pass data through) of the logic modules below, or any
word in the web app's source. Docstrings and comments don't count. "Used"
therefore means "read by something" -- directly, or to build a derived column
-- not that the final number is a high-weight scoring factor.
"""
import ast
import csv
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_FILE = os.path.join(REPO, "web", "public", "maths_variables.json")

# file -> short label shown in the "Where used" column
PY_AREAS = {
    "modules/scoring.py": "Score",
    "modules/simulations.py": "Simulations",
    "modules/derive.py": "Derived metrics",
    "modules/backtest.py": "Backtest",
    "modules/recommendations.py": "Recommendations",
    "modules/portfolio_optimizer.py": "Target portfolio",
    "modules/news_sentiment.py": "News",
    "modules/social_sentiment.py": "Social sentiment",
    "modules/sec_edgar.py": "SEC parsing",
    "modules/finra.py": "FINRA parsing",
    "modules/eulerpool.py": "Eulerpool download",
    "modules/fetch_data.py": "yfinance download",
    "modules/chatbot.py": "Chatbot",
    "modules/IBApp.py": "IB Gateway",
    "ib_server.py": "IB server",
    "main.py": "Pipeline (main.py)",
}
MODULE_LEVEL_OK = {"modules/scoring.py", "modules/simulations.py", "modules/derive.py", "modules/backtest.py",
                   "modules/recommendations.py", "modules/portfolio_optimizer.py", "modules/fetch_data.py"}
WEB_EXT = (".ts", ".tsx", ".js", ".jsx")


def _strip_docstrings(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body[0] = ast.Pass()
    return tree


def _py_usage():
    """{token: {(area, location)}} for every string constant / attribute name."""
    usage = {}

    def add(tok, area, loc):
        usage.setdefault(tok, set()).add((area, loc))

    for rel, area in PY_AREAS.items():
        path = os.path.join(REPO, rel)
        if not os.path.exists(path):
            continue
        tree = _strip_docstrings(ast.parse(open(path).read()))

        def visit(node, func):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, func if func and func != "<module>" and False else child.name)
                    continue
                if func == "<module>" and rel not in MODULE_LEVEL_OK:
                    visit(child, func)
                    continue
                if isinstance(child, ast.Constant) and isinstance(child.value, str) and len(child.value) <= 60:
                    add(child.value, area, func)
                elif isinstance(child, ast.Attribute):
                    add(child.attr, area, func)
                elif isinstance(child, ast.keyword) and child.arg:
                    add(child.arg, area, func)
                visit(child, func)

        visit(tree, "<module>")
    return usage


def _strip_js_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)(^|[^:])//.*$", r"\1", src)


def _web_usage():
    """{word: {page/file name}} for every identifier-like word in web/src."""
    usage = {}
    root = os.path.join(REPO, "web", "src")
    for dp, _, files in os.walk(root):
        for f in files:
            if not f.endswith(WEB_EXT):
                continue
            src = _strip_js_comments(open(os.path.join(dp, f), errors="ignore").read())
            label = f.rsplit(".", 1)[0]
            for w in set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", src)):
                usage.setdefault(w, set()).add(label)
            for q in set(re.findall(r"['\"]([^'\"\n]{1,60})['\"]", src)):
                usage.setdefault(q, set()).add(label)
    return usage


def _union_keys(path, getter=lambda v: v):
    d = json.load(open(os.path.join(REPO, path)))
    keys = {}
    for v in d.values():
        for k in getter(v) or []:
            keys[k] = keys.get(k, 0) + 1
    return keys


def _catalog_variables():
    """[(source, dataset, variable, note)] -- everything downloaded."""
    out = []
    raw = _union_keys("data/yfinance/raw_data.json", lambda v: v if isinstance(v, dict) else [])
    for k in sorted(raw):
        if k == "lastDownload":  # our own fetch timestamp, not a yfinance field
            continue
        out.append(("yfinance", "Ticker.info (raw_data.json)", k, ""))
    st = json.load(open(os.path.join(REPO, "data/yfinance/raw_yf_statements.json")))
    stmts = set()
    for v in st.values():
        if isinstance(v, dict) and isinstance(v.get("incomeStmt"), dict):
            stmts |= set(v["incomeStmt"])
    for k in sorted(stmts):
        out.append(("yfinance", "Income statement (raw_yf_statements.json)", k, "annual and quarterly line item"))
    for k in ("earningsEstimate", "epsTrend", "earningsDates"):
        out.append(("yfinance", "Estimates (raw_yf_statements.json)", k, "table per ticker"))
    out += [("yfinance", "Price history (price_history.json)", "close", "daily close"),
            ("yfinance", "Price history (price_history.json)", "date", "daily bar date")]  # noqa
    for k in ("fwdEps0y", "fwdEps1y", "fwdEps2y", "fwdRevenue0y", "fwdRevenue1y", "fwdRevenue2y",
              "epsHigh0y", "epsHigh1y", "epsHigh2y", "epsLow0y", "epsLow1y", "epsLow2y",
              "epsAnalysts0y", "epsAnalysts1y", "epsAnalysts2y",
              "fiscalYearEnd0y", "fiscalYearEnd1y", "fiscalYearEnd2y"):
        out.append(("Eulerpool", "Forward estimates (forward_eps.json)", k, ""))
    for k in ("fairValue", "fairValueIncome", "fairValueRevenue", "fairValueDividend", "lastPrice", "upside", "isin"):
        out.append(("Eulerpool", "Fair value (fair_value.json)", k, ""))
    # "days" (the 10-day averaging window of shortVolumeRatio) is metadata, not a variable: left out.
    out += [("Eulerpool", "Short volume (short_volume.json)", "shortVolumeRatio", "10-day average")]
    for k in ("action", "date", "grading_company", "new_grade", "previous_grade"):
        out.append(("Eulerpool", "Analyst grades (analyst_grades.json)", k, ""))
    out += [("Eulerpool", "Past EPS estimates (eps_estimates.json)", "eps_estimates", "{fiscal period: EPS}"),
            ("Eulerpool", "Past revenue (revenue_estimates.json)", "revenue_estimates", "{fiscal period: revenue}")]
    for k in ("id", "title", "datePublished", "entries"):
        out.append(("Eulerpool", "Earnings-call transcripts (transcripts.json)", k, ""))
    for k in ("currentShortPositionQuantity", "previousShortPositionQuantity", "averageDailyVolumeQuantity",
              "daysToCoverQuantity", "changePercent", "settlementDate"):
        out.append(("FINRA", "Short interest (short_interest.json)", k, "bi-monthly settlement file"))
    for k in ("insiderName", "isDirector", "isOfficer", "isTenPercentOwner", "officerTitle", "filingDate", "accessionNumber"):
        out.append(("SEC", "Form 4 filings (insider_transactions.json)", k, ""))
    for k in ("acquiredDisposed", "code", "date", "pricePerShare", "shares", "sharesOwnedAfter"):
        out.append(("SEC", "Form 4 transactions (insider_transactions.json)", k, "per transaction"))
    for k in ("revenue", "revenueQuarterly", "netIncome", "operatingIncome", "totalAssets", "dilutedEPS", "dilutedShares", "stockholdersEquity"):
        out.append(("SEC", "XBRL company facts (company_facts.json)", k, ""))
    for k in ("totalShares", "totalValueUsd", "holderCount", "callShares", "putShares", "pctShareChangeQoQ"):
        out.append(("SEC", "13F holdings (institutional_holdings.json)", k, "per ticker, all holders"))
    for k in ("name", "shares", "valueUsd", "callShares", "putShares"):
        out.append(("SEC", "13F holders (institutional_holders.json)", k, "per holder"))
    out += [("SEC", "8-K earnings releases (earnings_releases.json)", "snippets", "guidance text"),
            ("SEC", "8-K guidance signal (guidance_signal.json)", "direction", ""),
            ("SEC", "8-K guidance signal (guidance_signal.json)", "evidenceSnippet", "")]
    return out


# Which consumer areas count as "use" for each dataset (the downloader / parser
# modules and the IB Gateway code are producers or unrelated, so they are not
# evidence of use -- e.g. IB's own bid/ask/volume are not yfinance's).
_YF = {"Score", "Simulations", "Derived metrics", "Backtest", "Recommendations", "Target portfolio",
       "Chatbot", "Pipeline (main.py)"}
SCOPES = {
    "yfinance": _YF,
    "Eulerpool": {"Score", "Simulations", "Derived metrics", "Recommendations", "Chatbot", "Pipeline (main.py)", "Backtest"},
    "FINRA": {"Score", "Pipeline (main.py)", "Recommendations", "Chatbot"},
    "SEC": {"Derived metrics", "Score", "Recommendations", "Chatbot", "Simulations", "Pipeline (main.py)", "Backtest"},
}
# Datasets whose only possible consumer is a specific module (a plain name like
# "action" would otherwise match unrelated code elsewhere).
DATASET_SCOPE = {
    "Analyst grades (analyst_grades.json)": {"Score"},
    "Fair value (fair_value.json)": {"Score"},
    "Form 4 transactions (insider_transactions.json)": {"Score", "Recommendations"},
    "Form 4 filings (insider_transactions.json)": {"Chatbot", "Score", "Recommendations"},
    "13F holdings (institutional_holdings.json)": {"Score", "Recommendations", "Chatbot"},
    "XBRL company facts (company_facts.json)": {"Derived metrics", "Score", "Simulations"},
    "8-K guidance signal (guidance_signal.json)": {"Score"},
    "Short interest (short_interest.json)": {"Score", "Pipeline (main.py)"},
}
# Words that appear everywhere in unrelated code (and the UI): never auto-matched,
# decided by hand below instead.
GENERIC = {"state", "symbol", "market", "currency", "exchange", "open", "volume", "ask", "bid", "close", "date",
           "name", "id", "title", "code", "shares", "days", "action", "entries", "direction", "country",
           "revenue", "upside", "lastPrice", "isin", "snippets"}
# Hand-verified (variable, dataset prefix) -> where used ([] = not used).
OVERRIDES = {
    ("country", "Ticker.info"): ["Pipeline (main.py) › download: keeps only US-listed companies in the universe"],
    ("date", "Past"): [],
    ("close", "Price history"): ["Derived metrics › reconcile_trend (yfinance close fallback)", "UI: Asset chart"],
    ("date", "Price history"): ["Derived metrics › reconcile_trend (yfinance close fallback)"],
    ("shortVolumeRatio", "Short volume"): None,  # auto
    ("isin", "Fair value"): [],
    ("lastPrice", "Fair value"): [],
    ("snippets", "8-K earnings releases"): ["SEC parsing › summarize_guidance (builds the guidance direction, see below)"],
    ("direction", "8-K guidance signal"): ["Score › load_guidance_scores (guidance factor)"],
    ("evidenceSnippet", "8-K guidance signal"): [],
    ("id", "Earnings-call transcripts"): [], ("title", "Earnings-call transcripts"): [],
    ("datePublished", "Earnings-call transcripts"): [], ("entries", "Earnings-call transcripts"): [],
    ("upside", "Fair value"): ["Score › load_fair_value_scores (Eulerpool upside factor)"],
    ("eps_estimates", "Past EPS"): ["Derived metrics › eps_volatility_merged (epsVolatility)", "UI: Asset page, Eulerpool Estimates table"],
    ("revenue_estimates", "Past revenue"): ["Derived metrics › reconcile_forward_eps (eulerRevGrowth0y)", "UI: Asset page, Eulerpool Estimates table"],
    ("epsHigh1y", "Forward"): ["Derived metrics › reconcile_forward_eps → Simulations (year-1 EPS draw)"],
    ("epsLow1y", "Forward"): ["Derived metrics › reconcile_forward_eps → Simulations (year-1 EPS draw)"],
    ("epsAnalysts1y", "Forward"): ["Derived metrics › reconcile_forward_eps → Simulations (year-1 EPS draw)"],
    ("epsHigh2y", "Forward"): ["Derived metrics › reconcile_forward_eps → Simulations (year-2 EPS draw)"],
    ("epsLow2y", "Forward"): ["Derived metrics › reconcile_forward_eps → Simulations (year-2 EPS draw)"],
    ("epsAnalysts2y", "Forward"): ["Derived metrics › reconcile_forward_eps → Simulations (year-2 EPS draw)"],
    ("fiscalYearEnd0y", "Forward"): ["Derived metrics › reconcile_forward_eps → Simulations (year-0 weight = share of the fiscal year still to run)"],
    ("fiscalYearEnd1y", "Forward"): [], ("fiscalYearEnd2y", "Forward"): [],
    ("epsHigh0y", "Forward"): ["Derived metrics › reconcile_forward_eps → Simulations (year-0 base EPS draw)"], ("epsLow0y", "Forward"): ["Derived metrics › reconcile_forward_eps → Simulations (year-0 base EPS draw)"], ("epsAnalysts0y", "Forward"): ["Derived metrics › reconcile_forward_eps → Simulations (year-0 base EPS draw)"],
    ("code", "Form 4 transactions"): ["Score › load_insider_scores (buy/sell code)", "Recommendations › _recent_insider_counts"],
    ("shares", "Form 4 transactions"): ["Score › load_insider_scores (dollar-weighted)"],
    ("date", "Form 4 transactions"): ["Recommendations › _recent_insider_counts (recency window)"],
    ("date", "Analyst grades"): ["Score › analyst consensus (recency window, latest grade per firm)"],
    ("action", "Analyst grades"): ["Score › _grade_move_score (upgrade / downgrade)"],
}
for _v in ("name", "shares", "valueUsd", "callShares", "putShares"):
    OVERRIDES[(_v, "13F holders")] = ["UI: Asset page and Holders page (holder tables)"]
OVERRIDES[("callShares", "13F holdings")] = []
OVERRIDES[("putShares", "13F holdings")] = []
for _v in ("state", "symbol", "market", "currency", "exchange", "open", "volume", "ask", "bid"):
    OVERRIDES[(_v, "Ticker.info")] = []


def _override(var, dataset):
    for (v, prefix), val in OVERRIDES.items():
        if v == var and dataset.startswith(prefix) and val is not None:
            return val
    return None


def build_catalog():
    py = _py_usage()
    web = _web_usage()
    rows = []
    for source, dataset, var, note in _catalog_variables():
        ov = _override(var, dataset)
        if ov is not None:
            used = list(ov)
        else:
            scope = DATASET_SCOPE.get(dataset, SCOPES[source])
            used, seen = [], set()
            for area, loc in sorted(py.get(var, ())):
                if area not in scope:
                    continue
                label = area + (" › field map" if loc == "<module>" else f" › {loc}")
                if label not in seen:
                    seen.add(label)
                    used.append(label)
            if var not in GENERIC and source == "yfinance" and dataset.startswith("Ticker.info"):
                used += [f"UI: {p}" for p in sorted(web.get(var, ())) if not re.match(r"I[A-Z]", p)]
            # present in derive.build_screen_row only = copied into screen_data.csv, nothing reads it
            if used and all(u in ("Derived metrics › build_screen_row", "Derived metrics › field map") for u in used):
                used = []
                note = (note + "; " if note else "") + "only copied into screen_data.csv"
        rows.append({"source": source, "dataset": dataset, "variable": var, "note": note, "usedIn": used, "used": bool(used)})
    return rows


if __name__ == "__main__":
    rows = build_catalog()
    with open(OUT_FILE, "w") as f:
        json.dump({"rows": rows}, f, indent=1)
    n_used = sum(1 for r in rows if r["used"])
    print(f"Wrote {OUT_FILE}: {len(rows)} variables, {n_used} used, {len(rows) - n_used} unused")
