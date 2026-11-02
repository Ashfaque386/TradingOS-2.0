// The Portfolio page's figures and the words of its decisions.

/** An amount in Indian digit grouping with up to two decimals: `12,34,567.5`, `-1,200`. */
export function money(value: number): string {
  return value.toLocaleString('en-IN', { maximumFractionDigits: 2 })
}

/** Today's paper P&L after costs. The server's figure is before them (it sums the fills' realized P&L,
 * and the costs are on the fills beside it), so the net is the difference, rounded to paise. */
export function netOfCosts(gross: number, costs: number): number {
  return Math.round((gross - costs) * 100) / 100
}

export type Decision = 'accept' | 'reject'

/** What the confirmation says, and what the button is called. Nothing here ever places an order. */
export function decisionWords(decision: Decision): { title: string; description: string; confirmLabel: string; busyLabel: string } {
  return decision === 'accept'
    ? {
        title: 'Accept this recommendation?',
        description:
          'This records that you accepted it, with your note. Nothing is traded or rebalanced: a recommendation is advice, and every allocation stays as it is.',
        confirmLabel: 'Accept',
        busyLabel: 'Accepting…',
      }
    : {
        title: 'Reject this recommendation?',
        description:
          'This records that you rejected it, with your note. A decision cannot be reopened; generate a new recommendation to look again.',
        confirmLabel: 'Reject',
        busyLabel: 'Rejecting…',
      }
}
