/**
 * A scheduled job's trigger as a person reads it.
 *
 * The backend sends APScheduler's own text (`interval[0:00:03]`, `cron[day_of_week='mon-fri',
 * hour='8', minute='30']`), which Mission Control used to show as it was. Every scheduler in the
 * backend runs on India time, so a cron time is shown in IST. Anything this does not recognise is
 * shown exactly as sent: a wrong guess would be worse than the raw text.
 */

const DAY_NAMES: Record<string, string> = {
  mon: 'Mon',
  tue: 'Tue',
  wed: 'Wed',
  thu: 'Thu',
  fri: 'Fri',
  sat: 'Sat',
  sun: 'Sun',
}

const PLURAL_DAYS: Record<string, string> = {
  mon: 'Mondays',
  tue: 'Tuesdays',
  wed: 'Wednesdays',
  thu: 'Thursdays',
  fri: 'Fridays',
  sat: 'Saturdays',
  sun: 'Sundays',
}

function describeInterval(body: string): string | null {
  // "0:00:03", "1:00:00", "1 day, 0:00:00"
  const match = /^(?:(\d+) days?, )?(\d+):(\d{2}):(\d{2})$/.exec(body.trim())
  if (!match) return null
  const [, days, hours, minutes, seconds] = match
  const total = Number(days ?? 0) * 86_400 + Number(hours) * 3600 + Number(minutes) * 60 + Number(seconds)
  if (total <= 0) return null
  if (total % 86_400 === 0) return total === 86_400 ? 'every day' : `every ${total / 86_400} days`
  if (total % 3600 === 0) return total === 3600 ? 'every hour' : `every ${total / 3600} h`
  if (total % 60 === 0) return total === 60 ? 'every minute' : `every ${total / 60} min`
  return `every ${total} s`
}

function describeDays(spec: string): string | null {
  if (spec in PLURAL_DAYS) return PLURAL_DAYS[spec]
  const range = /^([a-z]{3})-([a-z]{3})$/.exec(spec)
  if (range && range[1] in DAY_NAMES && range[2] in DAY_NAMES) return `${DAY_NAMES[range[1]]}–${DAY_NAMES[range[2]]}`
  const list = spec.split(',')
  if (list.length > 1 && list.every((day) => day in DAY_NAMES)) return list.map((day) => DAY_NAMES[day]).join(', ')
  return null
}

function describeCron(body: string): string | null {
  const fields: Record<string, string> = {}
  const pairs = [...body.matchAll(/(\w+)='([^']*)'/g)]
  // Every character must belong to a pair or the ", " between them: otherwise it is a shape this
  // does not know, and the raw text is safer.
  if (pairs.map((p) => p[0]).join(', ') !== body.trim()) return null
  for (const [, key, value] of pairs) fields[key] = value
  const { hour, minute, day_of_week: dayOfWeek, second, ...rest } = fields
  if (Object.keys(rest).length > 0) return null
  if (second !== undefined && second !== '0') return null
  if (!/^\d{1,2}$/.test(hour ?? '') || !/^\d{1,2}$/.test(minute ?? '')) return null
  const time = `${hour.padStart(2, '0')}:${minute.padStart(2, '0')} IST`
  if (dayOfWeek === undefined || dayOfWeek === '*') return `daily ${time}`
  const days = describeDays(dayOfWeek)
  return days ? `${days} ${time}` : null
}

export function describeTrigger(trigger: string): string {
  const match = /^(interval|cron)\[(.*)\]$/.exec(trigger.trim())
  if (!match) return trigger
  const described = match[1] === 'interval' ? describeInterval(match[2]) : describeCron(match[2])
  return described ?? trigger
}
