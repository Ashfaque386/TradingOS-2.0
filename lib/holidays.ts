// What the holiday calendar says in words: which closure is next, how many of the year's holidays
// change anything, and how far away the next one is. The dates are India's.

import { daysBetween } from '@/lib/dates'
import type { HolidayEntry } from '@/lib/api'

/** The next day the exchange is shut for a holiday, today included: a holiday on a Saturday or Sunday
 * is not one, because the exchange is closed that day anyway. Null when none is left in the list. */
export function nextClosure(holidays: HolidayEntry[], today: string): HolidayEntry | null {
  return (
    [...holidays]
      .filter((h) => !h.on_weekend && h.date >= today)
      .sort((a, b) => a.date.localeCompare(b.date))[0] ?? null
  )
}

/** `today`, `tomorrow`, `in 6 days`, or `3 days ago`. */
export function whenWords(date: string, today: string): string {
  const days = daysBetween(today, date)
  if (days === 0) return 'today'
  if (days === 1) return 'tomorrow'
  if (days === -1) return 'yesterday'
  return days > 0 ? `in ${days} days` : `${-days} days ago`
}

export interface HolidayCounts {
  /** Every date the calendar lists. */
  listed: number
  /** Those that fall on a Saturday or Sunday and so close nothing extra. */
  onWeekend: number
  /** Weekdays the exchange is shut. */
  closures: number
}

export function holidayCounts(holidays: HolidayEntry[]): HolidayCounts {
  const onWeekend = holidays.filter((h) => h.on_weekend).length
  return { listed: holidays.length, onWeekend, closures: holidays.length - onWeekend }
}

/** The years the page offers: two before this one to two after, and the one being shown if an address
 * named a year outside that (so the list never claims a year it is not showing). */
export function yearChoices(thisYear: number, shown: number = thisYear): number[] {
  const years = [thisYear - 2, thisYear - 1, thisYear, thisYear + 1, thisYear + 2]
  return years.includes(shown) ? years : [...years, shown].sort((a, b) => a - b)
}
