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
  Semiconductors & AI, Fintech & Payments, etc. — see `modules/theme_classifier.py`),
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
# Full screener pipeline (forward P/E, momentum, sentiment, score) --
# also refreshes IB Gateway's own daily+hourly bars for the WHOLE active
# universe in the background (async, gated by a 3h cooldown -- see "IB
# Gateway bar downloads" below) -> data/yfinance/raw_data.json,
# data/yfinance/forward_pe.csv, data/output/sorted_screen.csv, data/social_sentiment.json
python main.py
python main.py all

# Same as `all`, but forces a fresh IB daily+hourly refresh even if one
# completed within the last 3h. The only command that can bypass the
# cooldown.
python main.py all overwrite

# Reuse forward-PE data already on disk; only refresh the momentum score
# from whatever IB/yfinance bars currently exist on disk. Does NOT
# refresh IB Gateway's own bars itself -- run `ibprices`/`ibhprices` (or
# `all`) first if those need refreshing too.
python main.py prices

# Standalone full-universe IB Gateway daily/hourly bar refresh, without
# the rest of the pipeline -- same 3h cooldown as `all`; these commands
# cannot override it themselves (only `all overwrite` can).
python main.py ibprices
python main.py ibhprices

# yfinance's own daily closes (close-only fallback, separate from IB's
# bars above -- see "IB Gateway bar downloads" below) -> data/yfinance/price_history.json
python main.py yfprices

# EPS-driven Monte Carlo price simulation -- zero network calls, reads
# data/yfinance/forward_pe.csv only -> data/output/simulations.json. Every
# simulated ticker is logged to the terminal as it completes. Defaults to
# the FULL active universe (same scope as the Screener itself, what feeds
# the Simulations tab; well under a minute, numpy-vectorized) when no
# tickers are given -- same as `simulations --all` -- see "Monte Carlo EPS
# forecast" below for the formula.
python main.py simulations
python main.py simulations --all

# A specific ticker list only, for a quick one-off check.
python main.py simulations TSLA NFLX

# Live price/position streaming server + IB history + snapshot polling +
# FinBERT-scored news (needs IB Gateway). Add `no_news` to skip
# re-downloading headlines this run (still scores/serves whatever's cached).
python ib_server.py

# One-shot Flex Query fetch -> data/portfolio_performance.json
python ib_server.py performance

# Frontend dev server
cd web && npm run dev
```

`npm run dev`/`npm run build` copy the latest generated data files from
`data/` into `web/public/` automatically (see `web/package.json`'s
`sync-data` script) before starting Vite.

### IB Gateway bar downloads

`data/IB/price_history_daily_3mo.json` (3-month daily bars, primary source
for the daily-timeframe momentum factor) and `data/IB/price_history_hourly.json`
(1-month hourly bars, sole source for the hourly overbought/oversold
factor — no fallback exists for it) come from IB Gateway. They are
**never merged** with `data/yfinance/price_history.json` (yfinance's daily
closes, close-only, refreshed independently via `python main.py
yfprices`) — that file is only the fallback the daily momentum factor
falls back to for a ticker IB Gateway's bars don't cover.

- `all`, `ibprices`, and `ibhprices` all request the **whole** active
  universe (`symbols.json`, `active == 1`) from IB Gateway.
- To avoid hammering IB Gateway with repeated full-universe pulls, any
  such explicit-scope request is gated by a 3-hour cooldown: if the
  same kind of refresh (daily or hourly — tracked independently)
  already completed within the last 3h, it's skipped immediately, no IB
  Gateway connection even opened. **Only `python main.py all overwrite`
  bypasses this** — `ibprices`/`ibhprices` never override it themselves.
- The cooldown is tracked on disk (`data/ib_refresh_state.json`) and
  shared between `main.py` and `ib_server.py`, so it's respected no
  matter which process actually performs the fetch — `main.py` connects
  to IB Gateway directly if `ib_server.py` isn't running, or routes
  through its already-open connection if it is (IB Gateway refuses a
  second simultaneous API connection).
- `ib_server.py`'s own default/startup refresh (used by its Dataset tab
  Run buttons when no ticker list is given) stays a narrower
  ranked/rated/held-only scope, and is **not** subject to the 3h
  cooldown — only its own per-ticker staleness gate (skip a ticker
  whose most recent bar already covers the latest completed trading
  session).
- IB's historical-data pacing limit is ~200 requests per rolling
  6-minute window (`IBApp.HISTORICAL_PACING_MAX_REQUESTS`/
  `HISTORICAL_PACING_WINDOW_SECONDS`); a full-universe pull (~2,340
  tickers) can still take on the order of an hour or more per kind
  (daily/hourly run concurrently during `all`, each its own IB Gateway
  connection when connecting directly).
- `data/missings.json` — `{"ib_daily": [...], "ib_hourly": [...],
  "yfinance": [...]}` — tickers each source came back with literally no
  data for on the most recent explicit-scope run that checked them.
  Written by `ibprices`/`ibhprices` (and `all`, which runs both) for the
  `ib_daily`/`ib_hourly` keys, and by `yfprices` (and `all`/`prices`,
  which share the same underlying yfinance fetch) for the `yfinance`
  key. Each key is overwritten wholesale by whichever process last
  checked that source — not accumulated — so a ticker that recovers
  disappears from the list on the next run of that same command.

## Score formula

`data/output/sorted_screen.csv`'s `score` column (lower is better) blends 18 factors —
see `modules/scoring.py` for the exact ranking rule and edge-case handling behind
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

## Monte Carlo EPS forecast (prototype)

`python main.py simulations [TICKER ...]` runs an EPS-driven Monte Carlo
price simulation — zero network calls, reads `data/yfinance/forward_pe.csv`
only — and writes `data/output/simulations.json`. Defaults to the **full
active universe** when no tickers are given, same as `simulations --all`.
Full implementation in `modules/simulations.py`.

**The question it answers:** given what's already known about a company's
earnings trajectory — where analysts are revising estimates and whether
revenue growth can sustain that — what is this stock's plausible fair
value TODAY at the industry's own median valuation multiple?

**The formula:**

**Inputs shared across all years:**

- `ownGrowthRate = avg(epsTrend, marginAdjustedRevenueGrowth)` — this
  ticker's own signals. `marginAdjustedRevenueGrowth = revenueGrowth ×
  operatingMargin` converts raw revenue growth to its earnings-equivalent.
  `epsTrend` is the 30-day consensus estimate revision (avg of
  `epsRevision0y`/`1y`, whichever present).
- `industryGrowthRate` = the same combination built from the peer group's
  **median** `epsTrend`/`revenueGrowth`/`operatingMargin` (granular
  industry ≥ 20 peers, otherwise widened to the broad GICS-style sector).
- `w_t = sqrt((N−t) / N)`, N = 4 — concave weight decaying from ~0.87
  at year 1 to 0 at year 4.
- All growth rates are clamped to [−99%, +100%].

**EPS path** (5 values, years 0–4):

- **Year 0** — `anchorEps = price / industryMedianPE` — the EPS today's
  price already implies at the peer group's own multiple. When no
  industry multiple is available, falls back to a 50/50 blend of
  `epsCurrentYear` (current-fiscal-year consensus EPS) and `forwardEps`,
  then a 50/50 blend of `trailingEps = price / trailingPE` and
  `forwardEps`, then `forwardEps` alone — blending rather than trusting
  either fallback estimate in isolation, since each is a single
  (potentially noisy) data source.
- **Year 1 growth rate** — schedule blended 50/50 with a forward-EPS drift:
  1. Schedule: `g1 = w1 × ownGrowthRate + (1−w1) × industryGrowthRate`
  2. Forward drift: `g_fwd = forwardEps / anchorEps − 1` — the consensus
     year-1 estimate nudges (without fully anchoring) the first step.
  3. Blend: `g1* = 0.5 × g1 + 0.5 × g_fwd`
- **Years 2–4** — `g_t = w_t × ownGrowthRate + (1−w_t) × industryGrowthRate`
  (no `forwardEps` drift beyond year 1).

**mu_eps** — 5-year discounted average:

```
r = DISCOUNT_RATE × clamp(beta, BETA_FLOOR, BETA_CAP)   # cost-of-equity proxy, beta clamped to [0.5, 3.0]
discountedPath[i] = epsPath[i] / (1 + r)^i               # i = 0..4
mu_eps = mean(discountedPath)
```

Discounting prevents later (more speculative) years from carrying the
same weight as year 1 — this is what makes mu_eps a genuine present
value, not a nominal figure that still needs discounting later. `r`
scales by beta (higher systematic risk → higher discount rate), clamped
so one extreme beta reading (e.g. a raw beta of 5+) can't over-discount
the whole 5-year path.

**Monte Carlo draws** (N = 20,000):

```
combinedVol = sqrt(epsVolatility² + analystDispersion²)   # RSS of two independent uncertainty sources
sigma_eps = combinedVol × |mu_eps|
eps_i ~ Normal(mu_eps, sigma_eps), floored (no cap) at the analyst-target-implied EPS
```

`epsVolatility` is stdev/mean(|EPS|) over ≤5 trailing years of annual EPS
(floored at 20% for missing/implausibly-quiet data). `analystDispersion =
(targetHighPrice − targetLowPrice) / (2 × targetMeanPrice)` — how much
sell-side analysts disagree with EACH OTHER, in the same relative-%
space as `epsVolatility`; falls back to `epsVolatility` alone when
analyst targets aren't on file.

**Pricing** — single scenario, industry median forwardPE only:

```
price_i = max(eps_i, 0) × industryMedianPE
```

No analyst-target-derived floor or cap — an earlier version had both
(`targetLowPrice`/`targetHighPrice` converted to an EPS bound via
`industryMedianPE`, scaled to `mu_eps` space via `mu_eps / forwardEps`),
but whenever the bound's own `min()`/`max()` picked the `abs(forwardEps)`
branch, the rescaling collapsed it to *exactly* `mu_eps` — silently
clipping half the distribution to a single point. Confirmed live on the
cap side for individual tickers, and on the floor side for **83% of the
simulated universe** (binding on >25% of draws; several tickers had
`epsFloor == muEps` to the last decimal, and for the worst cases even
the *median* collapsed to the same clipped value as P5/P25). Both
removed rather than patched — `eps_i` is now floored only at `0` (a
below-zero draw isn't sellable through this model).

**Forecast price and return:**

```
confidence = 1 / (1 + combinedVol)
forecastPrice = max(0, currentPrice + confidence × (median(price_i) − currentPrice))
forecastReturn = forecastPrice / currentPrice − 1
```

`forecastPrice` IS the confidence-weighted fair value today — no further
adjustment needed to call it that, since mu_eps is already a genuine
present value (it's the mean of the *discounted* 5-year EPS path above).
An earlier version additionally multiplied by `(1 + r)` here, reasoning
that a fairly-valued asset's price should also mechanically drift up by
its cost of equity over the next year — dropped, because for a high-beta
ticker that let a beta-sized markup dominate `forecastReturn` regardless
of the earnings view, and put `forecastPrice` on a different horizon (12
months out) than `probAboveCurrentPrice` (today), so the two could
disagree about which side of even a ticker was on for no visible reason.

`confidence` pulls the fair price toward `currentPrice` for historically
volatile earners, or ones analysts strongly disagree about — a large
projected upside built on an unpredictable estimate is worth less than
the same upside from a predictable one. `forecastReturn` is what the
Simulations tab ranks on.

**Caveats** — treat the output as a probabilistic sanity-check range,
not a price target:
- Normal is a simplifying assumption. Real EPS distributions are often
  skewed/fat-tailed in ways a symmetric bell curve understates.
- Earnings-driven only — says nothing about sentiment, macro, rate moves,
  or multiple re-rating, usually the bigger driver of short-term price action.
- `industryMedianPE` is a fixed current snapshot, not a forecast of where
  the multiple is headed.

## Project layout

| Path | Purpose |
|---|---|
| `main.py` | Screener download pipeline entry point (forward P/E, price-to-FCF, momentum) and all file I/O — owns `download_all`/`download_prices`/`download_symbols`. |
| `modules/` | Every fetcher/calculation module `main.py`/`ib_server.py` import from (see rows below) — kept out of the project root to separate the entry points from what they import. |
| `modules/scoring.py` | Every scoring indicator/factor calculation, plus `score_rows` (combines them with the weights above) and the Strong Buy/…/Strong Sell rating bucketing. |
| `modules/fetch_data.py` | Yahoo Finance + IBKR watchlist data fetchers used by `main.py`. |
| `modules/social_sentiment.py` | StockTwits social sentiment fetcher. |
| `modules/sec_edgar.py` | SEC EDGAR fetchers — Form 4 insider transactions (`python main.py form4`), XBRL company facts (`python main.py xbrl`, multi-year revenue/income/assets/equity/EPS history), and 13F institutional holdings (`python main.py 13f`, matched by company name from SEC's quarterly bulk dataset since 13F has no per-ticker CIK; downloads current + prior quarter to compute QoQ institutional share-count change, blended into the sentiment factor above and shown as its own "Inst Change" column). No API key needed, just a descriptive User-Agent and staying under SEC's rate limit. |
| `modules/news_sentiment.py` | FinBERT (`ProsusAI/finbert`) headline scoring — 1 (very bearish) to 5 (very bullish), with a filter for mechanical "Stock Rises X%, Outperforms Peers"-style headlines that carry no real signal. |
| `modules/theme_classifier.py` | Zero-shot classification (`facebook/bart-large-mnli`, local, no API key) of a ticker's `longBusinessSummary` against `data/theme_taxonomy.json`'s fixed theme list, for the Themes tab. Best-effort (~84% top-1 accuracy measured against hand-verified tags) — never overwrites an existing `data/ticker_themes.json` entry, only fills in untagged tickers. Run via `python main.py themes TICKER [TICKER ...]`. |
| `modules/simulations.py` | EPS-driven Monte Carlo price simulation prototype — see [Monte Carlo EPS forecast](#monte-carlo-eps-forecast-prototype) above for the formula. Zero network calls, reads `data/yfinance/forward_pe.csv` only. Run via `python main.py simulations [TICKER ...]`. |
| `modules/sector_groups.py` | Python port of `web/src/sectorGroups.js`'s granular-industry → broad-GICS-sector mapping, kept in sync by hand (no shared layer across the Python/JS boundary) — used by `modules/simulations.py`'s industry→sector peer-group fallback. |
| `modules/IBApp.py` | `ib_insync`-based IB Gateway client: connection, historical data, news headlines, momentum, Flex Query fetch. |
| `ib_server.py` | Local HTTP/SSE server: live prices, positions, account data, trades, snapshot polling, FinBERT-scored news, Flex Query parsing for the Portfolio tab. |
| `symbols.json` | Curated ticker universe (`active` flag controls inclusion in the screener). |
| `data/theme_taxonomy.json`, `data/ticker_themes.json` | Hand-curated (not fetched) theme definitions and per-ticker tags for the Themes tab — `ticker_themes.json` is seeded by hand-reading each holding's business description, then extended by `modules/theme_classifier.py` for new tickers only. |
| `data/` | Every other file the downloaders above write, JSON and CSV alike (`data/output/sorted_screen.csv`, IB-derived bars/exports/news under `data/IB/`, yfinance's own raw payload/price history/forward-PE CSV under `data/yfinance/`, etc. — see `main.py`'s `DATA_DIR`/`IB_DIR`/`YFINANCE_DIR`/`OUTPUT_DIR`) — regenerated by running the scripts above, not hand-maintained, except `data/IB/ts.json` (see the Notes section below). |
| `web/` | React/Vite frontend — `ScreenerView.tsx`, `PositionsView.tsx`, `TradesView.tsx`, `PortfolioView.tsx`, `FactsheetView.tsx`, `NewsView.tsx`, `ThemesView.tsx`, `SectorsView.tsx`, `HoldersView.tsx`, `RecommendationsView.tsx`, `SimulationsView.tsx`, `DatasetView.tsx`, `ScoringView.tsx`, `AssetView.tsx` (per-ticker detail page). |

## Notes

- `.env`, Flex Query exports (`Results.xml`/`Results.csv` at the root,
  `data/IB/NAVs.xml`), and `data/portfolio_performance.json` are gitignored —
  they contain your real account ID, balances, and positions. Regenerate
  `data/portfolio_performance.json` locally with `python ib_server.py
  performance`.
- The other data files (`data/yfinance/raw_data.json`, `data/yfinance/forward_pe.csv`,
  `data/output/sorted_screen.csv`, `data/IB/price_history*.json`,
  `data/social_sentiment.json`, `data/IB/news.json`,
  `data/output/news_sentiment.json`, `data/ib_refresh_state.json`,
  `data/missings.json`, `data/output/simulations.json`) are also gitignored
  as generated caches — run `python main.py` (and `python ib_server.py` for
  the news files, `python main.py simulations` for the Monte Carlo output)
  to produce them. `data/IB/trades.json` and `data/output/recommendations.json`
  are the exceptions: both stay tracked in git (like `data/IB/ts.json`
  below), not gitignored, despite living in otherwise-generated-cache
  folders.
- `data/IB/ts.json` is the one exception under `data/IB/`: a one-off
  multi-year backtest export (see `export_daily_history_on_demand`),
  meant to be committed into git by hand rather than left as a
  gitignored cache.
- News headlines and their FinBERT scores are cached for a rolling 30-day
  window (`ib_server.py`'s `NEWS_WINDOW_DAYS`); a headline is scored
  once, on first sight, never rescored.
