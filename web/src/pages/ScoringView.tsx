import ScoringFormulaTable from '../components/ScoringFormulaTable'
import RatingBreakdownTable from '../components/RatingBreakdownTable'

// The Scoring tab -- just the page wrapper around ScoringFormulaTable
// (the weights themselves) and RatingBreakdownTable (how those weights
// actually play out per sector). See those two components for everything
// else. Split out so each could be dropped into another page too, not
// just this one.
export default function ScoringView() {
  return (
    <div className="positions-page dataset-page">
      <ScoringFormulaTable />
      <RatingBreakdownTable />
    </div>
  )
}
