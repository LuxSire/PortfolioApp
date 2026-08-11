# PortfolioApp

A stock screener and IBKR portfolio tracker. A Python backend (yfinance +
`ib_insync` + local FinBERT/zero-shot models) feeds a React/Vite frontend
with seven tabs:

- **Screener** — a ranked, filterable, paginated table of US stocks scored on
  a 19-factor composite (valuation, quality, growth/momentum/mean-reversion,
  EPS estimate trend, analyst conviction, short interest, combined
  news+social sentiment, and SEC Form 4 insider buy/sell activity — see
  [Score formula](#score-formula) below).
- **Positions** — live IB Gateway prices/positions/account data streamed over
  SSE, with daily and since-acquisition P&L per holding, long/short and
  sector grouping, portfolio volatility decomposition, ARKK sensitivity, and
  candlestick/volume detail charts.
- **Trades** — today's fills, one row per ticker traded (net signed
  quantity, average fill price, net dollar value).
- **Portfolio** — daily account performance (cash, NAV, stock long/short/net/
  gross, realized/unrealized P&L, commissions, dividends, interest,
  deposits/withdrawals, Sharpe/Sortino/volatility) sourced from an IBKR Flex
  Query, with a NAV chart and a full daily breakdown table.
- **News** — every FinBERT-scored headline currently cached, across the
  whole screener universe, newest first, filterable by ticker with neutral
  headlines hidden by default.
- **Themes** — every held position's dollar exposure grouped by a
  hand-curated/model-classified theme taxonomy (Gold & Precious Metals,
  Semiconductors & AI, Fintech & Payments, etc. — see `theme_classifier.py`),
  split evenly across a ticker's tags so every theme's net exposure sums
  exactly to the portfolio's own net value.
- **Sectors** — the full screener universe grouped into an expandable
  Sector → Industry → Asset tree, each level averaging the same 19 factors
  the Screener itself ranks on.

## Requirements

- Python 3, and IB Gateway or TWS running and logged in (TWS API socket,
  default port `4001`) for live prices, positions, account data, and news
  headlines.
- Node.js for the frontend.
- An IBKR Flex Query (Activity type, daily granularity, **rolling** Date
  Period like "Last N Calendar Days" — not a fixed range) configured with
  Change in NAV, Equity Summary by Report Date, and FIFO Performance Summary
  sections, for the Portfolio tab.
- `transformers`/`torch` (in `requirements.txt`) pull down `ProsusAI/finbert`
  on first use — CPU inference, no API key, no per-call cost, but the first
  news-scoring pass will download the model.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in IB_ACCOUNT, QUERY_ID, QUERY_TOKEN
cd web && npm install
```

`QUERY_ID`/`QUERY_TOKEN` come from IBKR Account Management → Reports →
Flex Queries → a token generated for that query.

## Running

```bash
# Screener data (forward P/E, momentum, sentiment, score) -> data/raw_data.json,
# forward_pe.csv, sorted_screen.csv, data/social_sentiment.json
python main.py

# Live price/position streaming server + IB history + snapshot polling +
# FinBERT-scored news (needs IB Gateway). Add `no_news` to skip
# re-downloading headlines this run (still scores/serves whatever's cached).
python ib_price_server.py

# One-shot Flex Query fetch -> data/portfolio_performance.json
python ib_price_server.py performance

# Frontend dev server
cd web && npm run dev
```

`npm run dev`/`npm run build` copy the latest generated data files from
`data/` into `web/public/` automatically (see `web/package.json`'s
`sync-data` script) before starting Vite.

## Score formula

`sorted_screen.csv`'s `score` column (lower is better) blends 18 factors —
see `scoring.py` for the exact ranking rule and edge-case handling behind
each one:

| Weight | Factor |
|---|---|
| 5% | Low forward P/E |
| 10% | Low forward P/E relative to sector average |
| 5% | Low price/FCF (negative or missing FCF ranked worst) |
| 5% | Low EV/EBITDA (negative EBITDA ranked worst) |
| 2.5% | Low trailing P/S (price / trailing-12-month revenue — a separate valuation lens that stays meaningful for unprofitable/negative-FCF names) |
| 5% | High daily-timeframe momentum (regression-slope trend / its own volatility) |
| 5% | High hourly-timeframe mean reversion (negated regression-slope trend on the hourly series — a short-term reversal signal, independent of the daily momentum factor above) |
| 5% | EPS trend (current- + next-fiscal-year 30-day consensus estimate revision, from yfinance's `get_eps_trend()`) |
| 7.5% | High revenue growth |
| 7.5% | Analyst conviction (target upside + recommendation + low target-price dispersion) |
| 5% | Forward P/E vs. trailing P/E (more negative is better) |
| 5% | Low PEG ratio |
| 5% | Low debt/equity relative to sector average |
| 2.5% | Liquidity (quick + current ratio) |
| 5% | High return on equity |
| 5% | Short interest (deliberately contrarian — more shorted scores better) |
| 5% | Combined news + social + institutional sentiment (QoQ institutional share-change from SEC 13F, clipped to ±50%) |
| 5% | Insiders (SEC Form 4 open-market buys minus sells, as a share of both; missing ranked worst) |
| 5% | Margins (profit + operating) |

The Screener's "Score formula" info popup shows this same breakdown in the
UI. `rating_for_percentile` then buckets the score into a forced
Strong Buy/Buy/Hold/Sell/Strong Sell distribution shaped like Zacks Rank
(top/bottom 5% = Strong Buy/Strong Sell), independent of the `Rec` column's
raw analyst consensus.

## Project layout

| Path | Purpose |
|---|---|
| `main.py` | Screener download pipeline entry point (forward P/E, price-to-FCF, momentum) and all file I/O — owns `download_all`/`download_prices`/`download_symbols`. |
| `scoring.py` | Every scoring indicator/factor calculation, plus `score_rows` (combines them with the weights above) and the Strong Buy/…/Strong Sell rating bucketing. |
| `fetch_data.py` | Yahoo Finance + IBKR watchlist data fetchers used by `main.py`. |
| `social_sentiment.py` | StockTwits social sentiment fetcher. |
| `sec_edgar.py` | SEC EDGAR fetchers — Form 4 insider transactions (`python main.py form4`), XBRL company facts (`python main.py xbrl`, multi-year revenue/income/assets/equity/EPS history), and 13F institutional holdings (`python main.py 13f`, matched by company name from SEC's quarterly bulk dataset since 13F has no per-ticker CIK; downloads current + prior quarter to compute QoQ institutional share-count change, blended into the sentiment factor above and shown as its own "Inst Change" column). No API key needed, just a descriptive User-Agent and staying under SEC's rate limit. |
| `news_sentiment.py` | FinBERT (`ProsusAI/finbert`) headline scoring — 1 (very bearish) to 5 (very bullish), with a filter for mechanical "Stock Rises X%, Outperforms Peers"-style headlines that carry no real signal. |
| `theme_classifier.py` | Zero-shot classification (`facebook/bart-large-mnli`, local, no API key) of a ticker's `longBusinessSummary` against `data/theme_taxonomy.json`'s fixed theme list, for the Themes tab. Best-effort (~84% top-1 accuracy measured against hand-verified tags) — never overwrites an existing `data/ticker_themes.json` entry, only fills in untagged tickers. Run via `python main.py themes TICKER [TICKER ...]`. |
| `IBApp.py` | `ib_insync`-based IB Gateway client: connection, historical data, news headlines, momentum, Flex Query fetch. |
| `ib_price_server.py` | Local HTTP/SSE server: live prices, positions, account data, trades, snapshot polling, FinBERT-scored news, Flex Query parsing for the Portfolio tab. |
| `symbols.json` | Curated ticker universe (`active` flag controls inclusion in the screener). |
| `data/theme_taxonomy.json`, `data/ticker_themes.json` | Hand-curated (not fetched) theme definitions and per-ticker tags for the Themes tab — `ticker_themes.json` is seeded by hand-reading each holding's business description, then extended by `theme_classifier.py` for new tickers only. |
| `data/` | Every other JSON file the downloaders above write (see `main.py`'s `DATA_DIR`) — regenerated by running the scripts above, not hand-maintained. |
| `web/` | React/Vite frontend — `PeTable.jsx` (Screener), `PositionsView.jsx`, `TradesView.jsx`, `PortfolioView.jsx`, `NewsView.jsx`/`NewsPopup.jsx`, `ThemesView.jsx`, `Asset.jsx` (per-ticker detail page). |

## Notes

- `.env`, Flex Query exports (`Results.xml`/`Results.csv` at the root,
  `data/NAVs.xml`), and `data/portfolio_performance.json` are gitignored —
  they contain your real account ID, balances, and positions. Regenerate
  `data/portfolio_performance.json` locally with `python ib_price_server.py
  performance`.
- The other data files (`data/raw_data.json`, `forward_pe.csv`,
  `sorted_screen.csv`, `data/price_history*.json`,
  `data/social_sentiment.json`, `data/news*.json`) are also gitignored as
  generated caches — run `python main.py` (and `python ib_price_server.py`
  for the news files) to produce them.
- News headlines and their FinBERT scores are cached for a rolling 30-day
  window (`ib_price_server.py`'s `NEWS_WINDOW_DAYS`); a headline is scored
  once, on first sight, never rescored.
