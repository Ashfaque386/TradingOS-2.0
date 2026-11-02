// Which periods an investor report may be asked for, in the server's own words
// (backend `src/orchestration/investor_report_period.py`), so the form says what is wrong before a
// request is made. The server is the authority; the dates are India's, the exchange's calendar.

import { DAY_MS, addDays, daysBetween, isoOf, utcOf } from '@/lib/dates'

export type Cadence = 'weekly' | 'monthly'

/** The longest period each cadence covers, inclusive: the scheduler's week, and the longest month. */
export const MAX_PERIOD_DAYS: Record<Cadence, number> = { weekly: 7, monthly: 31 }

/** Why this period cannot be reported, or null when it can. */
export function periodProblem(start: string, end: string, cadence: Cadence, today: string): string | null {
  if (!start) return 'Choose the day the period starts.'
  if (!end) return 'Choose the day the period ends.'
  const from = utcOf(start)
  const to = utcOf(end)
  if (Number.isNaN(from) || Number.isNaN(to)) return 'The dates must be real days, such as 2026-10-19.'
  if (to < from) return `The period ends (${end}) before it starts (${start}).`
  if (to > utcOf(today)) return `The period ends on ${end}, after today (${today}) in India: nothing has happened on those days yet.`
  const days = daysBetween(start, end) + 1
  const longest = MAX_PERIOD_DAYS[cadence]
  if (days > longest) return `A ${cadence} report covers at most ${longest} days; ${start} to ${end} is ${days}.`
  return null
}

/** The period the scheduler would report next: the Monday-to-Sunday week just ended, or the calendar
 * month just ended. */
export function defaultPeriod(cadence: Cadence, today: string): { start: string; end: string } {
  const now = utcOf(today)
  if (cadence === 'weekly') {
    const sinceMonday = (new Date(now).getUTCDay() + 6) % 7 // Monday is 0
    const end = isoOf(now - (sinceMonday + 1) * DAY_MS)
    return { start: addDays(end, -6), end }
  }
  const thisMonth = new Date(now)
  const firstOfThisMonth = Date.UTC(thisMonth.getUTCFullYear(), thisMonth.getUTCMonth(), 1)
  const firstOfLast = Date.UTC(thisMonth.getUTCFullYear(), thisMonth.getUTCMonth() - 1, 1)
  return { start: isoOf(firstOfLast), end: isoOf(firstOfThisMonth - DAY_MS) }
}

/** The reports already written for exactly this period and cadence. */
export function reportsFor<R extends { period_start: string; period_end: string; cadence: string }>(
  reports: R[],
  start: string,
  end: string,
  cadence: Cadence,
): R[] {
  return reports.filter((r) => r.period_start === start && r.period_end === end && r.cadence === cadence)
}
