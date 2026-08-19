// Types for DatasetView.tsx (the Dataset tab) — one entry from GET
// /api/dataset-status (see ib_server.py's DATASETS/
// _handle_dataset_status).
export interface DatasetFile {
  // This row's own stable identity (see ib_server.py's DATASETS' own
  // comment on why it's separate from path) -- more than one row can
  // point at the same file (e.g. Screener ranking's plain/prices/rescore
  // rows all regenerate sorted_screen.csv via a different command), so
  // path alone isn't a safe React key/run-request target any more.
  id: string
  path: string
  label: string
  command: string | null
  notes: string | null
  network: string | null
  exists: boolean
  mtime: string | null
  sizeBytes: number | null
  // Only populated for the two price-history time-series files (see
  // ib_server.py's _price_history_staleness) -- null for everything
  // else. A genuinely different freshness signal than mtime: mtime just
  // says the file was WRITTEN recently, which stayed true even during a
  // real incident where a partial yfinance fetch failure left ~99% of
  // tickers' actual last bar days behind.
  latestBarDate: string | null
  expectedBarDate: string | null
  stale: boolean | null
  // Whether ib_server.py has a run config for this row at all (see
  // DATASETS' own "run" field) -- the Dataset tab only shows a Run
  // button when this is true; a parametrized command or a hand-
  // maintained/auto-managed file has none.
  canRun: boolean
}

// One entry from GET/POST /api/admin/run-status / run-dataset (see
// ib_server.py's _current_job/_handle_run_status) -- the single global
// job slot the Dataset tab's Run button starts and polls. "idle" (every
// other field null/empty) before anything's ever been run this session.
export interface RunJobStatus {
  id: string | null
  label: string | null
  status: 'idle' | 'running' | 'done' | 'error'
  log: string[]
  returncode: number | null
  startedAt: string | null
  endedAt: string | null
}
