import { Fragment, useEffect, useRef, useState } from 'react'
import { AlertTriangle } from 'lucide-react'
import { IB_DATASET_STATUS_URL, IB_RUN_DATASET_URL, IB_RUN_STATUS_URL } from '../ibStream'
import type { DatasetFile, RunJobStatus } from '../interfaces/IDatasetView'

// Table has Dataset/Path/Last generated/Size/Regenerate with/Requires/
// Action -- the log row (see below) spans all of them.
const COLUMN_COUNT = 7
// How often to poll GET /api/admin/run-status while a job is running --
// a human watching ticker codes scroll by doesn't need sub-second
// latency, and this keeps the polling loop simple (no SSE reconnect
// logic) for what's fundamentally a local dev/ops tool.
const RUN_POLL_MS = 1000

// mtime is a full ISO 8601 timestamp (see ib_server.py's
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
// ib_server.py's DATASETS/_handle_dataset_status for where this all
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

  // Run button/log panel state -- one global job at a time, mirroring
  // ib_server.py's own single _current_job slot (see that module's own
  // comment on why: two dataset-refresh commands racing each other would
  // just corrupt each other's output). `job` starts out null and ONLY
  // ever gets set from handleRun -- deliberately not seeded from GET
  // /api/admin/run-status on mount, even though ib_server.py keeps the
  // last job around after it finishes: seeding it would pop the log
  // panel open on every page load/reload for whatever was last run,
  // possibly in a previous session, with nobody having clicked Run this
  // time. `logDismissed` only hides the panel locally, it doesn't touch
  // server-side job state.
  const [job, setJob] = useState<RunJobStatus | null>(null)
  const [runError, setRunError] = useState<string | null>(null)
  const [logDismissed, setLogDismissed] = useState(false)
  const pollRef = useRef<number | null>(null)
  const logBodyRef = useRef<HTMLPreElement | null>(null)

  function pollRunStatus() {
    fetch(IB_RUN_STATUS_URL)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d: RunJobStatus) => {
        setJob(d)
        if (d.status !== 'running' && pollRef.current !== null) {
          window.clearInterval(pollRef.current)
          pollRef.current = null
        }
      })
      .catch(() => {})
  }

  useEffect(() => {
    return () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current)
    }
  }, [])

  useEffect(() => {
    if (job?.status === 'running' && pollRef.current === null) {
      pollRef.current = window.setInterval(pollRunStatus, RUN_POLL_MS)
    }
  }, [job?.status])

  useEffect(() => {
    if (logBodyRef.current) logBodyRef.current.scrollTop = logBodyRef.current.scrollHeight
  }, [job?.log.length])

  function handleRun(id: string) {
    setRunError(null)
    setLogDismissed(false)
    fetch(IB_RUN_DATASET_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id }),
    })
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d) => {
        if (d.error) {
          setRunError(d.error)
          return
        }
        pollRunStatus()
      })
      .catch(() => setRunError('Failed to reach ib_server.py to start the job'))
  }

  const jobRunning = job?.status === 'running'

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
        {runError && <span className="dataset-run-error">{runError}</span>}
      </div>

      {error && (
        <div className="asset-card">
          Couldn't reach ib_server.py's dataset-status endpoint — is ib_server.py running?
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
                <th className="col-left">Action</th>
              </tr>
            </thead>
            <tbody>
              {files.map((f) => (
                <Fragment key={f.id}>
                  <tr>
                    <td className="col-left dataset-label">{f.label}</td>
                    <td className="col-left">
                      <code>{f.path}</code>
                    </td>
                    <td className="col-left" title={f.exists ? fmtAbsoluteTime(f.mtime) : undefined}>
                      {f.exists ? fmtRelativeTime(f.mtime) : <span className="dataset-missing">never generated</span>}
                      {f.stale && (
                        <AlertTriangle
                          className="dataset-stale-icon"
                          size={13}
                          aria-label="Stale"
                          title={`Most recent price bar (excluding today's own, still-forming one) is ${f.latestBarDate} -- expected ${f.expectedBarDate} or newer. The file itself may still be getting rewritten on schedule; it's the actual price data inside it that hasn't advanced.`}
                        />
                      )}
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
                    <td className="col-left">
                      {f.canRun && (
                        <button
                          type="button"
                          className="dataset-run-btn"
                          disabled={jobRunning}
                          title={jobRunning && job?.id !== f.id ? `Already running: ${job?.label}` : undefined}
                          onClick={() => handleRun(f.id)}
                        >
                          {jobRunning && job?.id === f.id ? 'Running…' : 'Run'}
                        </button>
                      )}
                    </td>
                  </tr>
                  {job && job.id === f.id && !logDismissed && (
                    <tr className="dataset-log-row">
                      <td colSpan={COLUMN_COUNT}>
                        <div className="dataset-log">
                          <div className="dataset-log-header">
                            <span className={`dataset-log-status dataset-log-status-${job.status}`}>
                              {job.status === 'running' ? 'Running…' : job.status === 'done' ? 'Done' : 'Error'}
                            </span>
                            {job.status !== 'running' && job.returncode !== null && (
                              <span className="dataset-log-returncode">exit code {job.returncode}</span>
                            )}
                            <button
                              type="button"
                              className="dataset-log-close"
                              onClick={() => setLogDismissed(true)}
                              aria-label="Close log"
                            >
                              ×
                            </button>
                          </div>
                          <pre className="dataset-log-body" ref={logBodyRef}>
                            {job.log.length ? job.log.join('\n') : job.status === 'running' ? 'Starting…' : ''}
                          </pre>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
