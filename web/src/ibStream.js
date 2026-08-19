// ib_server.py's default; matches the port it prints on startup.
// Single source of truth so PeTable.jsx and PositionsView.jsx can't drift.
export const IB_STREAM_URL = 'http://localhost:8765/api/stream'
export const IB_NEWS_URL = 'http://localhost:8765/api/news'
export const IB_NEWS_ARTICLE_URL = 'http://localhost:8765/api/news/article'
export const IB_CHAT_URL = 'http://localhost:8765/api/chat'
export const IB_DATASET_STATUS_URL = 'http://localhost:8765/api/dataset-status'
export const IB_RUN_DATASET_URL = 'http://localhost:8765/api/admin/run-dataset'
export const IB_RUN_STATUS_URL = 'http://localhost:8765/api/admin/run-status'
export const IB_SCORING_FORMULA_URL = 'http://localhost:8765/api/scoring-formula'
