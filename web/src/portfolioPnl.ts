import type { PortfolioDayRow } from './interfaces/IPortfolioView'

// A day's Total P&L = realized + unrealized + commissions + dividends +
// interest + withholding tax (commissions and withholding tax as IB reports
// them, i.e. negative). With
// ib_server._apply_unrealized_from_nav, this sums to the day's NAV change
// less deposits/withdrawals. Null when realized/unrealized are unknown.
export function dayTotalPnl(
  r: Pick<PortfolioDayRow, 'realized' | 'unrealized' | 'commissions' | 'dividends' | 'interest' | 'withholdingTax'>,
): number | null {
  if (r.realized === null || r.unrealized === null) return null
  return r.realized + r.unrealized + (r.commissions ?? 0) + (r.dividends ?? 0) + (r.interest ?? 0) + (r.withholdingTax ?? 0)
}
