// Calendar days as `YYYY-MM-DD` text, worked out in UTC so the browser's time zone cannot move a day.
// "Today" is always India's (the exchange's calendar).

import { istDate } from '@/lib/paper-trading'

export const DAY_MS = 86_400_000

/** Midnight UTC of a real `YYYY-MM-DD` day, or NaN for anything else (30 February is not a day). */
export function utcOf(iso: string): number {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso)
  if (!match) return Number.NaN
  const ms = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
  return new Date(ms).toISOString().slice(0, 10) === iso ? ms : Number.NaN
}

export function isoOf(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10)
}

export function addDays(iso: string, days: number): string {
  return isoOf(utcOf(iso) + days * DAY_MS)
}

/** Whole days from `from` to `to` (negative when `to` is earlier); NaN if either is not a day. */
export function daysBetween(from: string, to: string): number {
  return Math.round((utcOf(to) - utcOf(from)) / DAY_MS)
}

/** Today's date in India, `2026-11-02`, whatever the browser's time zone. */
export function todayInIndia(now: Date): string {
  const { year, month, day } = istDate(now)
  return `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`
}
