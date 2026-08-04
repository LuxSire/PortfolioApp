# PortfolioApp

A stock screener and IBKR portfolio tracker. Python backend (yfinance +
`ib_insync`) feeds a React/Vite frontend with three views:

- **Screener** — a ranked, filterable, paginated table of US stocks scored on
  forward P/E, price-to-FCF, momentum, analyst upside, and social sentiment.
- **Positions** — live IB Gateway prices/positions/account data streamed over
  SSE, with daily and since-acquisition P&L per holding, sector grouping, and
  candlestick/volume detail charts.
- **Portfolio** — daily account performance (cash, NAV, stock long/short/net/
  gross, realized/unrealized P&L, commissions, dividends, interest,
  deposits/withdrawals) sourced from an IBKR Flex Query, with a NAV chart and
  a full daily breakdown table.

## Requirements

- Python 3, and IB Gateway or TWS running and logged in (TWS API socket,
  default port `4001`) for live prices, positions, and account data.
- Node.js for the frontend.
- An IBKR Flex Query (Activity type, daily granularity) configured with
  Change in NAV, Equity Summary by Report Date, and FIFO Performance Summary
  sections, for the Portfolio tab.

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
# Screener data (forward P/E, momentum, sentiment) -> raw_data.json, forward_pe.csv,
# screen.csv, sorted_screen.csv, social_sentiment.json
python main.py

# Live price/position streaming server + IB history + snapshot polling (needs IB Gateway)
python ib_price_server.py

# One-shot Flex Query fetch -> portfolio_performance.json
python ib_price_server.py performance

# Frontend dev server
cd web && npm run dev
```

`npm run dev`/`npm run build` copy the latest generated data files into
`web/public/` automatically (see `web/package.json`'s `sync-data` script)
before starting Vite.

## Project layout

| Path | Purpose |
|---|---|
| `main.py` | Screener data pipeline entry point (forward P/E, price-to-FCF, momentum score). |
| `fetch_data.py` | Yahoo Finance + IBKR watchlist data fetchers used by `main.py`. |
| `social_sentiment.py` | StockTwits sentiment fetcher. |
| `IBApp.py` | `ib_insync`-based IB Gateway client: connection, historical data, Flex Query fetch. |
| `ib_price_server.py` | Local HTTP/SSE server: live prices, positions, account data, snapshot polling, Flex Query parsing for the Portfolio tab. |
| `symbols.json` | Curated ticker universe (`active` flag controls inclusion in the screener). |
| `web/` | React/Vite frontend (`PeTable.jsx`, `PositionsView.jsx`, `PortfolioView.jsx`, `Asset.jsx`). |

## Notes

- `.env`, Flex Query exports (`Results.xml`/`Results.csv`), and
  `portfolio_performance.json` are gitignored — they contain your real
  account ID, balances, and positions. Regenerate
  `portfolio_performance.json` locally with `python ib_price_server.py
  performance`.
- The other data files (`raw_data.json`, `forward_pe.csv`, `screen.csv`,
  `sorted_screen.csv`, `price_history*.json`, `social_sentiment.json`) are
  also gitignored as generated caches — run `python main.py` to produce them.
