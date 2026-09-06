import { useEffect, useState } from 'react'
import { IB_SCORING_FORMULA_URL } from '../ibStream'
import type { ScoringFactor } from '../interfaces/IScoringView'

function fmtPct(weight: number): string {
  return `${Math.round(weight * 1000) / 10}%`
}

// The composite-score formulas sorted_screen.csv's `score` column is built
// from (see scoring.py's score_rows/FACTOR_WEIGHTS) -- Standard for
// everything outside the special columns below, Financials for a
// Financials-sector ticker (banks, insurers, asset managers, capital
// markets -- see scoring.is_financials_sector), Utilities for a
// Utilities-sector ticker (see scoring.is_utilities_sector), Real Estate
// for a Real-Estate-sector ticker (see scoring.is_real_estate_sector),
// and Growth for a ticker outside those three sectors that is
// high-growth but pre-profitability -- revenueGrowth > 20% AND a
// negative current EV/EBITDA (see scoring.is_growth_cohort). The sector
// columns take precedence over Growth when a ticker matches both.
//
// Each row is one scoring factor; a few blend more than one signal
// internally. "Simulations (sim return)" ranks on the Monte Carlo
// simReturn = simPrice / currentPrice - 1, where simPrice is the
// p5/p95-winsorized simulated-path price scaled by the risk-premium
// multiple haircut (see scoring.forecast_return_rank / modules/
// simulations.py). forecastReturn (the confidence-shrunk point estimate)
// was an equal-weight second leg until it was dropped.
// "Revenue growth" and "Earnings growth" are separate rows on purpose:
// top-line vs bottom-line growth are distinct signals, and revenue growth
// is only capped by (never rewarded for) earnings growth internally.
//
// Built after real incidents: Yahoo Finance doesn't populate
// debtToEquity/quickRatio/currentRatio/enterpriseToEbitda/freeCashflow for
// Financials at all (confirmed even for JPM/WFC) or for mortgage REITs
// specifically (confirmed 0/18); Utilities structurally run quick/current
// ratios well under 1 (a normal trait of a regulated monopoly's
// predictable cash flow, not distress) that liquidity_rank -- unlike
// debt_rank -- never compared against the ticker's own sector. Each case
// left rank_ascending treating a normal-for-the-sector (or simply
// unreported) value as the WORST rank, not neutral. This component is a
// thin render of GET /api/scoring-formula, no logic of its own beyond
// formatting -- the weights themselves live only in scoring.py's
// FACTOR_WEIGHTS, so this table can never drift from what score_rows
// actually computes.
//
// Self-contained (own fetch, no props) -- extracted out of ScoringView.tsx
// (the Scoring tab) so it could be dropped into another page too, not just
// this one.
export default function ScoringFormulaTable() {
  const [factors, setFactors] = useState<ScoringFactor[] | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    fetch(IB_SCORING_FORMULA_URL)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d) => setFactors(d.factors || []))
      .catch(() => setError(true))
  }, [])

  const standardTotal = factors?.reduce((s, f) => s + f.standardWeight, 0) ?? 0
  const financialsTotal = factors?.reduce((s, f) => s + f.financialsWeight, 0) ?? 0
  const utilitiesTotal = factors?.reduce((s, f) => s + f.utilitiesWeight, 0) ?? 0
  const realEstateTotal = factors?.reduce((s, f) => s + f.realEstateWeight, 0) ?? 0
  const growthTotal = factors?.reduce((s, f) => s + f.growthWeight, 0) ?? 0

  return (
    <>
      <header className="masthead">
        <div className="title-block">
          <h1>Scoring</h1>
        </div>
        <div className="stat-row">
          <div className="stat">
            <span className="n num">{factors ? factors.length : '—'}</span>
            <span className="l">factors</span>
          </div>
          <div className="stat">
            <span className="n num">{fmtPct(standardTotal)}</span>
            <span className="l">standard total</span>
          </div>
          <div className="stat">
            <span className="n num">{fmtPct(financialsTotal)}</span>
            <span className="l">financials score total</span>
          </div>
          <div className="stat">
            <span className="n num">{fmtPct(utilitiesTotal)}</span>
            <span className="l">utilities score total</span>
          </div>
          <div className="stat">
            <span className="n num">{fmtPct(realEstateTotal)}</span>
            <span className="l">real estate score total</span>
          </div>
          <div className="stat">
            <span className="n num">{fmtPct(growthTotal)}</span>
            <span className="l">growth score total</span>
          </div>
        </div>
      </header>

      {error && <div className="asset-card">Couldn't reach ib_server.py's scoring-formula endpoint — is ib_server.py running?</div>}
      {!error && !factors && <div className="asset-card">Loading…</div>}

      {!error && factors && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className="col-left">Factor</th>
                <th>Standard</th>
                <th>Financials</th>
                <th>Utilities</th>
                <th>Real Estate</th>
                <th>Growth</th>
              </tr>
            </thead>
            <tbody>
              {factors.map((f) => {
                const financialsChanged = f.standardWeight !== f.financialsWeight
                const utilitiesChanged = f.standardWeight !== f.utilitiesWeight
                const realEstateChanged = f.standardWeight !== f.realEstateWeight
                const growthChanged = f.standardWeight !== f.growthWeight
                return (
                  <tr
                    key={f.key}
                    className={
                      financialsChanged || utilitiesChanged || realEstateChanged || growthChanged
                        ? 'scoring-row-changed'
                        : undefined
                    }
                  >
                    <td className="col-left">{f.label}</td>
                    <td className="num">{fmtPct(f.standardWeight)}</td>
                    <td className={`num${financialsChanged ? ' scoring-weight-changed' : ''}`}>{fmtPct(f.financialsWeight)}</td>
                    <td className={`num${utilitiesChanged ? ' scoring-weight-changed' : ''}`}>{fmtPct(f.utilitiesWeight)}</td>
                    <td className={`num${realEstateChanged ? ' scoring-weight-changed' : ''}`}>{fmtPct(f.realEstateWeight)}</td>
                    <td className={`num${growthChanged ? ' scoring-weight-changed' : ''}`}>{fmtPct(f.growthWeight)}</td>
                  </tr>
                )
              })}
            </tbody>
            <tfoot>
              <tr>
                <td className="col-left">Total</td>
                <td className="num">{fmtPct(standardTotal)}</td>
                <td className="num">{fmtPct(financialsTotal)}</td>
                <td className="num">{fmtPct(utilitiesTotal)}</td>
                <td className="num">{fmtPct(realEstateTotal)}</td>
                <td className="num">{fmtPct(growthTotal)}</td>
              </tr>
            </tfoot>
          </table>
        </div>
      )}
    </>
  )
}
