// ib_price_server.py's default; matches the port it prints on startup.
// Single source of truth so PeTable.jsx and PositionsView.jsx can't drift.
export const IB_STREAM_URL = 'http://localhost:8765/api/stream'
