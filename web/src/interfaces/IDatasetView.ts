// Types for DatasetView.tsx (the Dataset tab) — one entry from GET
// /api/dataset-status (see ib_price_server.py's DATASETS/
// _handle_dataset_status).
export interface DatasetFile {
  path: string
  label: string
  command: string | null
  notes: string | null
  network: string | null
  exists: boolean
  mtime: string | null
  sizeBytes: number | null
}
