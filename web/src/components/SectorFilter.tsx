import { useMemo } from 'react'
import { getSectorGroup } from '../sectorGroups'
import FilterDropdown from './FilterDropdown'

// The broad GICS-style sector filter (Technology, Healthcare, Financial
// Services, ...) -- shared by Screener and Recommendations, explicit
// instruction: same component, not two independent implementations of
// the same filter. Takes the raw granular industry value per row
// (sorted_screen.csv's own `sector` field -- actually Yahoo's "industry",
// see scoring.py's module docstring on that naming) and derives the
// broad-sector item list via getSectorGroup itself, the same mapping
// CardFooter/RatingBreakdownTable already use -- callers don't need to
// pre-aggregate anything, just pass every row's own industry string.
//
// Controlled, not self-managing selection state: the caller owns
// `selected` (a Set of chosen sector-group names) and applies it to its
// own row list however it needs to (Screener and Recommendations filter
// differently-shaped data), this component only renders the picker and
// reports the next Set back via onChange.
export default function SectorFilter({
  industries,
  selected,
  onChange,
}: {
  industries: (string | null | undefined)[]
  selected: Set<string>
  onChange: (next: Set<string>) => void
}) {
  const groupCounts: [string, number][] = useMemo(() => {
    const counts = new Map<string, number>()
    for (const industry of industries) {
      const group = getSectorGroup(industry)
      counts.set(group, (counts.get(group) || 0) + 1)
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
  }, [industries])

  function toggle(name: string) {
    const next = new Set(selected)
    if (next.has(name)) next.delete(name)
    else next.add(name)
    onChange(next)
  }

  return (
    <FilterDropdown
      noun="sector"
      items={groupCounts}
      selected={selected}
      onToggle={toggle}
      onClear={() => onChange(new Set())}
    />
  )
}
