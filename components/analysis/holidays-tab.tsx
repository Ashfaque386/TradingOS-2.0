'use client'

import { AlertTriangle } from 'lucide-react'
import { ErrorNotice } from '@/components/ui/error-notice'
import { getHolidays } from '@/lib/api'
import { todayInIndia } from '@/lib/dates'
import { holidayCounts, nextClosure, whenWords, yearChoices } from '@/lib/holidays'
import { useQuery } from '@/lib/use-query'

/**
 * The days the exchange is shut for a holiday, from the same calendar the backtest gate, the Paper
 * Desk's market clock and the order checks read. Its movable holidays (Holi, Eid, Diwali) come from a
 * file kept against the exchange's circular; for a year the file does not cover they are treated as
 * trading days, and the page says so in the server's words rather than showing a list that looks whole.
 */
export function HolidaysTab({ year, onYear }: { year: number; onYear: (year: number) => void }) {
  const calendar = useQuery(`holidays:${year}`, () => getHolidays(year))
  const now = new Date()
  const today = todayInIndia(now)
  const thisYear = Number(today.slice(0, 4))
  const data = calendar.data?.year === year ? calendar.data : null
  const counts = data ? holidayCounts(data.holidays) : null
  const next = data && year === thisYear ? nextClosure(data.holidays, today) : null

  return (
    <div className="flex flex-col gap-4">
      {calendar.error && <ErrorNotice message={`Could not load the holiday calendar: ${calendar.error}`} onRetry={() => void calendar.refresh()} />}
      <section className="analysis-panel">
        <div className="analysis-panel-head">
          <div>
            <p className="eyebrow">NSE TRADING HOLIDAYS</p>
            <h2>Holidays in {year}</h2>
          </div>
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            Year
            <select value={year} onChange={(e) => onYear(Number(e.target.value))} className="backtest-select" style={{ minWidth: 0 }}>
              {yearChoices(thisYear, year).map((y) => <option key={y} value={y}>{y}</option>)}
            </select>
          </label>
        </div>
        {data && !data.calendar_file_covers_year && data.warning && (
          <div role="alert" className="holiday-warning">
            <AlertTriangle className="size-4 shrink-0" />
            <p>{data.warning}</p>
          </div>
        )}
        {!data && !calendar.error && <p className="p-4 text-xs text-muted-foreground">Loading the calendar…</p>}
        {data && counts && (
          <>
            <p className="holiday-summary">
              {counts.closures} weekday closure{counts.closures === 1 ? '' : 's'}
              {counts.onWeekend > 0 && <> · {counts.onWeekend} more fall{counts.onWeekend === 1 ? 's' : ''} on a weekend and change nothing</>}
              {next && (
                <> · <strong>Next closure: {next.name ?? 'holiday'}</strong>, {next.weekday} {next.date} ({whenWords(next.date, today)})</>
              )}
            </p>
            <div className="overflow-x-auto">
              <table className="analysis-table" aria-label={`Trading holidays in ${year}`}>
                <thead>
                  <tr><th>Date</th><th>Day</th><th>Holiday</th><th>Listed by</th><th>Effect</th></tr>
                </thead>
                <tbody>
                  {data.holidays.map((h) => (
                    <tr key={h.date} className={h.on_weekend ? 'holiday-weekend' : undefined}>
                      <td className="mono">{h.date}</td>
                      <td>{h.weekday}</td>
                      <td>{h.name ?? 'Holiday (not named in the calendar file)'}</td>
                      <td>{h.source}</td>
                      <td>{h.on_weekend ? 'None: a weekend anyway' : 'Exchange shut'}</td>
                    </tr>
                  ))}
                  {data.holidays.length === 0 && <tr><td colSpan={5} className="p-4 text-center text-xs text-muted-foreground">The calendar lists no holidays for {year}.</td></tr>}
                </tbody>
              </table>
            </div>
          </>
        )}
      </section>
    </div>
  )
}
