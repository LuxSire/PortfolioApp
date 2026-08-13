import { useEffect, useState } from 'react'
import { IB_DATASET_STATUS_URL } from '../ibStream'
import type { DatasetFile } from '../interfaces/IDatasetView'

// mtime is a full ISO 8601 timestamp (see ib_price_server.py's
// _handle_dataset_status) -- shown as a compact relative reading ("2h
// ago") for at-a-glance staleness, with the exact date/time as the title
// tooltip for whoever wants precision. No color-coded staleness threshold
// here on purpose: what counts as "stale" varies enormously by file --
// Screener ranking going 2 days without a refresh is a problem, a 13F
// filing going 2 months without one is completely normal (SEC only
// publishes quarterly) -- so a single hard cutoff would be wrong for most
// rows more often than it'd be right for any of them.
function fmtRelativeTime(iso: string | null): string | null {
  if (!iso) return null
  const diffMs = Date.now() - new Date(iso).getTime()
  const diffSec = Math.round(diffMs / 1000)
  if (diffSec < 5) return 'just now'
  if (diffSec < 60) return `${diffSec}s ago`
  const diffMin = Math.round(diffSec / 60)
  if (diffMin < 60) return `${diffMin}m ago`
  const diffHr = Math.round(diffMin / 60)
  if (diffHr < 24) return `${diffHr}h ago`
  const diffDay = Math.round(diffHr / 24)
  if (diffDay < 30) return `${diffDay}d ago`
  const diffMonth = Math.round(diffDay / 30)
  return `${diffMonth}mo ago`
}

function fmtAbsoluteTime(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function fmtSize(bytes: number | null): string {
  if (bytes === null || bytes === undefined) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// Every generated data file this app's Python backend produces, when it
// was last written, and the exact command that regenerates it -- see
// ib_price_server.py's DATASETS/_handle_dataset_status for where this all
// actually comes from (this component is a thin render of that endpoint,
// no logic of its own beyond formatting). Built after a real incident:
// RecommendationsView.tsx's Long/Short lists silently ranked candidates
// against an hours-stale recommendations.json because nobody had an easy
// way to see which files were current and which weren't.
export default function DatasetView() {
  const [files, setFiles] = useState<DatasetFile[] | null>(null)
  const [error, setError] = useState(false)
  const [refreshedAt, setRefreshedAt] = useState<Date | null>(null)

  function load() {
    setError(false)
    fetch(IB_DATASET_STATUS_URL)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d) => {
        setFiles(d.files || [])
        setRefreshedAt(new Date())
      })
      .catch(() => setError(true))
  }

  useEffect(load, [])

  return (
    <div className="positions-page dataset-page">
      <header className="masthead">
        <div className="title-block">
          <h1>Dataset</h1>
        </div>
        <div className="stat-row">
          <div className="stat">
            <span className="n num">{files ? files.length : '—'}</span>
            <span className="l">files tracked</span>
          </div>
          {refreshedAt && (
            <div className="stat">
              <span className="n num">{fmtRelativeTime(refreshedAt.toISOString())}</span>
              <span className="l">status checked</span>
            </div>
          )}
        </div>
      </header>

      <div className="controls">
        <button type="button" className="dataset-refresh-btn" onClick={load}>
          Refresh
        </button>
      </div>

      {error && (
        <div className="asset-card">
          Couldn't reach ib_price_server.py's dataset-status endpoint — is ib_price_server.py running?
        </div>
      )}
      {!error && !files && <div className="asset-card">Loading…</div>}

      {!error && files && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className="col-left">Dataset</th>
                <th className="col-left">Path</th>
                <th className="col-left">Last generated</th>
                <th>Size</th>
                <th className="col-left">Regenerate with</th>
                <th className="col-left">Requires</th>
              </tr>
            </thead>
            <tbody>
              {files.map((f) => (
                <tr key={f.path}>
                  <td className="col-left dataset-label">{f.label}</td>
                  <td className="col-left">
                    <code>{f.path}</code>
                  </td>
                  <td className="col-left" title={f.exists ? fmtAbsoluteTime(f.mtime) : undefined}>
                    {f.exists ? fmtRelativeTime(f.mtime) : <span className="dataset-missing">never generated</span>}
                  </td>
                  <td className="num">{f.exists ? fmtSize(f.sizeBytes) : '—'}</td>
                  <td className="col-left">
                    {f.command ? (
                      <>
                        <code>{f.command}</code>
                        {f.notes && <div className="dataset-notes">{f.notes}</div>}
                      </>
                    ) : (
                      <span className="dataset-missing">
                        {f.notes || 'no command — hand-maintained'}
                      </span>
                    )}
                  </td>
                  <td className="col-left">{f.network || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
