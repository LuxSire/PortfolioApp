import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// ib_server.py's own default port -- see ibStream.js's own IB_STREAM_URL
// etc. comment on why this is duplicated rather than imported (this file
// runs under Node, not the browser bundle).
const IB_SERVER_URL = 'http://localhost:8765'

// Every file `npm run dev`'s own predev hook (see package.json's
// sync-data script) copies into public/ once at startup -- proxied to
// ib_server.py here instead of falling through to that one-time-copied
// snapshot, so a later rescore/ibprices/form4/etc. run (which rewrites
// the REAL file, not the copy) shows up on a plain page refresh instead
// of needing `npm run dev` restarted. See ib_server.py's own STATIC_FILES
// dict for the server side of this exact same path list -- keep both in
// sync by hand if either changes. Doesn't help a production `npm run
// build`: a built dist/ is a static snapshot with no dev server behind
// it to proxy through, so that still bakes in whatever sync-data copied
// at build time.
const STATIC_DATA_PATHS = [
  '/sorted_screen.csv',
  '/raw_data.json',
  '/social_sentiment.json',
  '/news_sentiment.json',
  '/price_history.json',
  '/price_history_hourly.json',
  '/price_history_daily_3mo.json',
  '/portfolio_performance.json',
  '/theme_taxonomy.json',
  '/ticker_themes.json',
  '/sec/form4/insider_transactions.json',
  '/sec/13f/institutional_holdings.json',
  '/sec/13f/institutional_holders.json',
  '/recommendations.json',
  '/ARKK_HOLDINGS.csv',
]

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: Object.fromEntries(STATIC_DATA_PATHS.map((path) => [path, { target: IB_SERVER_URL, changeOrigin: true }])),
  },
})
