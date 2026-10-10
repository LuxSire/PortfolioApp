import { useEffect, useState } from 'react'
import type { RefObject } from 'react'

// Explicit instruction: one "Export" button next to IbFreshnessBadge in
// App.jsx's tab bar that downloads the ACTIVE tab's information as JSON.
// Generic on purpose -- it reads what the tab currently renders (inside
// App.jsx's tab-content container) rather than each page exporting its
// own data, so all 16 tabs get it without per-page code:
//   stats   every .stat block (".l" label -> ".n" value)
//   tables  every <table>: nearest heading as title, header cells as
//           columns, each body row as {column: cell text}; a cell's hover
//           title (e.g. Backtesting's n) is kept as "<column> (detail)"
//   text    the rest of the visible text, line by line (cards, notes,
//           headings) -- read with tables/stats/charts hidden so it
//           doesn't duplicate them
// Only what's rendered is exported: a paginated table exports its
// current page, a sub-tab only its active section, and chart SVGs are
// skipped (their numbers aren't in the DOM as text).
//
// The button opens a small popup: JSON (above) or PDF. PDF prints the active tab
// through the browser's own print dialog ("Save as PDF") -- no PDF library, so
// text and charts stay vector and every CSS feature this app uses renders (the
// usual html2canvas route can't draw color-mix()). printTabAsPdf keeps the
// on-screen layout: the tab content is pinned to its screen width and zoomed
// to fit an A4 landscape page, so tables and charts look like the page, and the
// print CSS (.print-app in styles.scss) hides the app chrome and keeps cards
// from splitting across pages.

type Props = {
  tabKey: string
  tabLabel: string
  contentRef: RefObject<HTMLDivElement | null>
}

type TableExport = {
  title: string | null
  columns: string[]
  rows: (Record<string, string> | string[])[]
}

const clean = (s: string | null | undefined): string => (s ?? '').replace(/\s+/g, ' ').trim()

// Closest heading above `el` inside `root`: previous siblings first
// (their own last heading, if they wrap one), then up a level.
function nearestHeading(el: Element, root: Element): string | null {
  const HEADING = 'h1, h2, h3, h4, h5, h6'
  let node: Element | null = el
  while (node && node !== root) {
    let sib = node.previousElementSibling
    while (sib) {
      if (sib.matches(HEADING)) return clean(sib.textContent)
      const inner = sib.querySelectorAll(HEADING)
      if (inner.length) return clean(inner[inner.length - 1].textContent)
      sib = sib.previousElementSibling
    }
    node = node.parentElement
  }
  return null
}

function cellsOf(row: HTMLTableRowElement): HTMLTableCellElement[] {
  return Array.from(row.cells)
}

function tableToJson(table: HTMLTableElement, root: Element): TableExport {
  const headRows = table.tHead ? Array.from(table.tHead.rows) : []
  let bodyRows = Array.from(table.tBodies).flatMap((b) => Array.from(b.rows))
  let headerRow: HTMLTableRowElement | undefined = headRows[headRows.length - 1]
  if (!headerRow && bodyRows.length && cellsOf(bodyRows[0]).every((c) => c.tagName === 'TH')) {
    headerRow = bodyRows[0]
    bodyRows = bodyRows.slice(1)
  }
  // Expand colspans so a grouped header still lines up with body cells.
  const columns: string[] = []
  if (headerRow) {
    for (const c of cellsOf(headerRow)) {
      const label = clean(c.textContent) || `col${columns.length + 1}`
      for (let i = 0; i < (c.colSpan || 1); i++) columns.push(c.colSpan > 1 ? `${label} ${i + 1}` : label)
    }
  }
  // Disambiguate repeated column names.
  const seen: Record<string, number> = {}
  const keys = columns.map((c) => {
    seen[c] = (seen[c] ?? 0) + 1
    return seen[c] > 1 ? `${c} (${seen[c]})` : c
  })
  const rows = bodyRows
    .map((r) => cellsOf(r))
    .filter((cells) => cells.length > 0)
    .map((cells) => {
      if (keys.length && cells.length === keys.length) {
        const obj: Record<string, string> = {}
        cells.forEach((c, i) => {
          obj[keys[i]] = clean(c.textContent)
          const detail = clean(c.getAttribute('title'))
          if (detail && detail !== obj[keys[i]]) obj[`${keys[i]} (detail)`] = detail
        })
        return obj
      }
      return cells.map((c) => clean(c.textContent))
    })
  return { title: nearestHeading(table, root), columns: keys, rows }
}

function exportTab(root: HTMLElement, tabKey: string, tabLabel: string) {
  const stats: Record<string, string> = {}
  root.querySelectorAll('.stat').forEach((s) => {
    const label = clean(s.querySelector('.l')?.textContent)
    const value = clean(s.querySelector('.n')?.textContent)
    if (label) stats[label] = value
  })

  const tables = Array.from(root.querySelectorAll('table')).map((t) => tableToJson(t, root))

  // Hide what's already captured (and charts), read the remaining visible
  // text with real line breaks, then restore -- all within one task, so
  // nothing visibly flickers.
  const hidden = Array.from(root.querySelectorAll<HTMLElement>('table, .stat, svg, button, select, input'))
  const prev = hidden.map((el) => el.style.display)
  hidden.forEach((el) => (el.style.display = 'none'))
  const text = root.innerText
    .split('\n')
    .map((l) => clean(l))
    .filter(Boolean)
  hidden.forEach((el, i) => (el.style.display = prev[i]))

  const payload = { tab: tabKey, label: tabLabel, exportedAt: new Date().toISOString(), stats, tables, text }
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const stamp = new Date().toISOString().slice(0, 16).replace(/[-:]/g, '').replace('T', '_')
  const a = document.createElement('a')
  a.href = url
  a.download = `${tabKey}_${stamp}.json`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

// A4 landscape printable width at 96 dpi: (297 mm - 2 x 10 mm margins) / 25.4 x 96.
const PRINT_WIDTH_PX = ((297 - 20) / 25.4) * 96

function printTabAsPdf(root: HTMLElement, tabLabel: string) {
  // The Factsheet has its own one-page print layout (FactsheetView.tsx handlePrint);
  // use it there instead of the generic multi-page one, so the two never mix.
  const ownPrint = root.querySelector<HTMLButtonElement>('.factsheet-page .print-btn')
  if (ownPrint) {
    ownPrint.click()
    return
  }
  const html = document.documentElement
  const width = root.scrollWidth
  const zoom = Math.min(1, PRINT_WIDTH_PX / width)
  const pageStyle = document.createElement('style')
  pageStyle.textContent = '@page { size: A4 landscape; margin: 10mm; }'
  document.head.appendChild(pageStyle)
  html.classList.add('print-app')
  html.style.setProperty('--print-app-width', `${width}px`)
  html.style.setProperty('--print-app-zoom', String(zoom))
  const prevTitle = document.title
  // Chrome/Safari use the document title as the suggested PDF file name.
  document.title = `${tabLabel} ${new Date().toISOString().slice(0, 10)}`
  const cleanup = () => {
    html.classList.remove('print-app')
    html.style.removeProperty('--print-app-width')
    html.style.removeProperty('--print-app-zoom')
    pageStyle.remove()
    document.title = prevTitle
    window.removeEventListener('afterprint', cleanup)
  }
  window.addEventListener('afterprint', cleanup)
  // Let the class/zoom land (and charts settle) before the dialog snapshots the page.
  requestAnimationFrame(() => requestAnimationFrame(() => window.print()))
}

export default function ExportTabButton({ tabKey, tabLabel, contentRef }: Props) {
  const [open, setOpen] = useState(false)
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  const run = (kind: 'json' | 'pdf') => {
    setOpen(false)
    const root = contentRef.current
    if (!root) return
    if (kind === 'json') exportTab(root, tabKey, tabLabel)
    else setTimeout(() => printTabAsPdf(root, tabLabel), 0) // after the popup has closed
  }

  return (
    <>
      <button
        type="button"
        className="export-tab-btn no-print"
        title={`Export the ${tabLabel} tab as JSON or PDF`}
        onClick={() => setOpen(true)}
      >
        ⤓ Export
      </button>
      {open && (
        <div className="export-modal-backdrop no-print" onClick={() => setOpen(false)}>
          <div className="export-modal" role="dialog" aria-label="Export" onClick={(e) => e.stopPropagation()}>
            <h3>Export “{tabLabel}”</h3>
            <div className="export-modal-options">
              <button type="button" onClick={() => run('json')}>
                <strong>JSON</strong>
                <span>Stats, tables and text as data</span>
              </button>
              <button type="button" onClick={() => run('pdf')}>
                <strong>PDF</strong>
                <span>The page as it looks — choose “Save as PDF” in the print dialog</span>
              </button>
            </div>
            <button type="button" className="export-modal-cancel" onClick={() => setOpen(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </>
  )
}
