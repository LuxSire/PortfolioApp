import { inverseRangeClass, inversePctThresholdClass, meanReversionClass, momentumClass, rangeClass } from '../colorRules'
import { fmtDebtToEquity, fmtIndex100, fmtNum, fmtPct, fmtPrice, fmtScore } from '../screenerFactors'

const signedClass = (v) => (v === null || v === undefined ? '' : v >= 0 ? 'perf-pos' : 'perf-neg')

// One <td> per factorTable.js's FACTOR_COLUMNS entry, in that same order.
// `factors` can be either a group-level average (see
// factorTable.js's computeFactorAverages return value) or a single
// ticker's own raw row (same key names, so a leaf/asset row renders
// through this exact same component with no averaging needed — an
// "average" of one row is just that row). Same formatting/color-
// threshold rules PeTable.jsx's per-row cells use, kept in sync by hand
// across both files whenever a factor's treatment changes.
export default function FactorCells({ factors }) {
  return (
    <>
      <td className="num">{fmtNum(factors.savgpe)}</td>
      <td className={`num ${rangeClass(factors.fpe, 10, 50)}`}>{fmtNum(factors.fpe)}</td>
      <td className="num">{fmtPrice(factors.feps)}</td>
      <td className={`num ${signedClass(factors.epsTrend)}`}>{fmtPct(factors.epsTrend)}</td>
      <td className={`num ${rangeClass(factors.tpe, 10, 50)}`}>{fmtNum(factors.tpe)}</td>
      <td className={`num ${rangeClass(factors.tps, 2, 10)}`}>{fmtNum(factors.tps)}</td>
      <td className={`num ${rangeClass(factors.peg, 1, 1)}`}>{fmtNum(factors.peg)}</td>
      <td className={`num ${inversePctThresholdClass(factors.revg, 0, 10)}`}>{fmtPct(factors.revg)}</td>
      <td className={`num ${rangeClass(factors.pfcf, 10, 50)}`}>{fmtNum(factors.pfcf)}</td>
      <td className={`num ${rangeClass(factors.evEbitda, 10, 20)}`}>{fmtNum(factors.evEbitda)}</td>
      <td className={`num ${inversePctThresholdClass(factors.opMargin, 0, 15)}`}>{fmtPct(factors.opMargin)}</td>
      <td className={`num ${rangeClass(factors.de, 50, 200)}`}>{fmtDebtToEquity(factors.de)}</td>
      <td className={`num ${inverseRangeClass(factors.liq, 1, 3)}`}>{fmtNum(factors.liq)}</td>
      <td className={`num ${inversePctThresholdClass(factors.shortInt, 2, 10)}`}>{fmtPct(factors.shortInt)}</td>
      <td className={`num ${signedClass(factors.upside)}`}>{fmtPct(factors.upside)}</td>
      <td className={`num ${momentumClass(factors.mom)}`}>{fmtNum(factors.mom)}</td>
      <td className={`num ${meanReversionClass(factors.mr)}`}>{fmtNum(factors.mr)}</td>
      <td className={`num ${signedClass(factors.sent)}`}>{fmtIndex100(factors.sent)}</td>
      <td className={`num ${signedClass(factors.newsSent)}`}>{fmtIndex100(factors.newsSent)}</td>
      <td className={`num ${signedClass(factors.instChange)}`}>{fmtIndex100(factors.instChange)}</td>
      <td className={`num ${signedClass(factors.insiders)}`}>{fmtIndex100(factors.insiders)}</td>
      <td className="num">{fmtScore(factors.sc)}</td>
    </>
  )
}
