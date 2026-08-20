import { useEffect, useMemo, useRef, useState, type ComponentType, type RefObject } from 'react'
import { ChevronDown } from 'lucide-react'

// Same click-outside-closes-the-popover hook as RecommendationsView.tsx's
// own copy (for its Score Formula-style popovers) and ScreenerView.tsx's
// own copy (for its ScoreFormula popover, which doesn't use this
// component) -- duplicated locally rather than shared, this project's
// existing convention for this particular hook.
function useOutsideClick(ref: RefObject<HTMLElement | null>, onOutside: () => void) {
  useEffect(() => {
    function handler(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) onOutside()
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [ref, onOutside])
}

// Generic multi-select checkbox dropdown -- a search box to narrow a long
// item list, a checkbox per item with its row count, Clear all/Done
// actions. Used as-is for Screener's industry/rating filters (each with
// their own noun/items/selected/onToggle/onClear), and as the rendering
// layer under SectorFilter (see that component) for the sector filter
// both Screener and Recommendations share. Extracted out of
// ScreenerView.tsx, which originally had this defined inline, so it
// could be reused outside that one page.
export default function FilterDropdown({
  noun,
  plural,
  items,
  selected,
  onToggle,
  onClear,
  getIcon,
}: {
  noun: string
  plural?: string
  items: [string, number][]
  selected: Set<string>
  onToggle: (name: string) => void
  onClear: () => void
  getIcon?: (name: string) => ComponentType<{ size?: number }>
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const wrapRef = useRef<HTMLDivElement>(null)
  useOutsideClick(wrapRef, () => setOpen(false))
  const pluralNoun = plural || `${noun}s`

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return items.filter(([name]) => !q || name.toLowerCase().includes(q))
  }, [items, query])

  return (
    <div className="sector-control" ref={wrapRef}>
      <button
        type="button"
        className="sector-toggle"
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span>{selected.size ? `${selected.size} ${selected.size > 1 ? pluralNoun : noun}` : `All ${pluralNoun}`}</span>
        <ChevronDown size={12} />
      </button>
      {open && (
        <div className="sector-panel open">
          <div className="sp-search">
            <input
              type="text"
              placeholder={`Filter ${pluralNoun}…`}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              autoFocus
            />
          </div>
          <div className="sp-list">
            {filtered.map(([name, count]) => {
              const Icon = getIcon ? getIcon(name) : null
              return (
                <label className="sp-item" key={name}>
                  <input type="checkbox" checked={selected.has(name)} onChange={() => onToggle(name)} />
                  {Icon && <Icon size={14} />}
                  <span>{name}</span>
                  <span className="cnt">{count}</span>
                </label>
              )
            })}
          </div>
          <div className="sp-actions">
            <button type="button" onClick={onClear}>
              Clear all
            </button>
            <button type="button" onClick={() => setOpen(false)}>
              Done
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
