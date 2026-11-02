import { expect, test } from '@playwright/test'
import { ANALYSIS_TABS, isSymbol, viewFromQuery, viewToQuery } from '../lib/analysis-view'
import { addDays, daysBetween, todayInIndia, utcOf } from '../lib/dates'
import { behindWords, pipelineLabel, statusLight } from '../lib/datalake'
import { holidayCounts, nextClosure, whenWords, yearChoices } from '../lib/holidays'
import {
  DEFAULT_PARAMS,
  INDICATOR_FIELDS,
  draftOf,
  indicatorProblems,
  indicatorQuery,
  isDefault,
  paramsOf,
  readParams,
  unfilled,
  warmUps,
} from '../lib/indicator-params'
import { defaultPeriod, periodProblem, reportsFor } from '../lib/investor-report-period'
import { parseInline, parseMarkdown, reportFilename } from '../lib/markdown'
import { decisionWords, money, netOfCosts } from '../lib/portfolio-view'
import type { HolidayEntry } from '../lib/api'

// What the Market Analysis, Investor Reports and Portfolio pages decide, with no browser and no backend:
// each case states the answer worked out by hand.

test.describe('the indicator parameters', () => {
  test('the usual periods are all acceptable, and each box holds the bounds the server holds it to', () => {
    expect(indicatorProblems(draftOf(DEFAULT_PARAMS))).toEqual({})
    // The server's bounds (backend `indicators_endpoint`): a change there must change these.
    expect(INDICATOR_FIELDS.map((f) => [f.key, f.min, f.max])).toEqual([
      ['lookbackDays', 5, 2000],
      ['smaWindow', 2, 400],
      ['emaSpan', 2, 400],
      ['rsiPeriod', 2, 100],
      ['macdFast', 2, 100],
      ['macdSlow', 3, 200],
      ['macdSignal', 2, 100],
      ['bollingerWindow', 2, 400],
      ['bollingerStd', 0, 5],
    ])
  })

  test('a box that is empty, not a number, fractional or out of range says which, in its own name', () => {
    const problem = (text: string) => indicatorProblems({ ...draftOf(DEFAULT_PARAMS), smaWindow: text }).smaWindow
    expect(problem('')).toBe('SMA window must be a number.')
    expect(problem('  ')).toBe('SMA window must be a number.')
    expect(problem('abc')).toBe('SMA window must be a number.')
    expect(problem('1.5')).toBe('SMA window must be a whole number.')
    expect(problem('1')).toBe('SMA window must be between 2 and 400.')
    expect(problem('401')).toBe('SMA window must be between 2 and 400.')
    expect(problem('2')).toBeUndefined()
    expect(problem('400')).toBeUndefined()
  })

  test('the deviations may be fractional but not zero, and no more than five', () => {
    const problem = (text: string) => indicatorProblems({ ...draftOf(DEFAULT_PARAMS), bollingerStd: text }).bollingerStd
    expect(problem('0')).toBe('Bollinger deviations must be more than 0 and at most 5.')
    expect(problem('-1')).toBe('Bollinger deviations must be more than 0 and at most 5.')
    expect(problem('5.01')).toBe('Bollinger deviations must be more than 0 and at most 5.')
    expect(problem('0.1')).toBeUndefined()
    expect(problem('2.5')).toBeUndefined()
    expect(problem('5')).toBeUndefined()
  })

  test('the MACD fast average must be shorter than the slow one, in the server\'s words', () => {
    const draft = (fast: string, slow: string) => indicatorProblems({ ...draftOf(DEFAULT_PARAMS), macdFast: fast, macdSlow: slow })
    expect(draft('26', '26').macdFast).toBe("The MACD's fast average (26) must be shorter than its slow one (26).")
    expect(draft('30', '26').macdFast).toBe("The MACD's fast average (30) must be shorter than its slow one (26).")
    expect(draft('25', '26')).toEqual({})
    // A box already wrong is named for that, not also for the comparison
    expect(draft('1', '26').macdFast).toBe('MACD fast must be between 2 and 100.')
  })

  test('parameters are read only when every box is sound, and sent in full', () => {
    expect(readParams({ ...draftOf(DEFAULT_PARAMS), smaWindow: '0' })).toBeNull()
    expect(readParams({ ...draftOf(DEFAULT_PARAMS), smaWindow: ' 50 ', bollingerStd: '1.5' })).toEqual({
      ...DEFAULT_PARAMS,
      smaWindow: 50,
      bollingerStd: 1.5,
    })
    expect(indicatorQuery(DEFAULT_PARAMS)).toBe(
      'lookback_days=250&sma_window=20&ema_span=20&rsi_period=14&macd_fast=12&macd_slow=26&macd_signal_period=9&bollinger_window=20&bollinger_std=2',
    )
    expect(isDefault(DEFAULT_PARAMS)).toBe(true)
    expect(isDefault({ ...DEFAULT_PARAMS, rsiPeriod: 7 })).toBe(false)
  })

  test('the bars each indicator needs match what the server computes, and the ones the history is too short for are named', () => {
    expect(warmUps(DEFAULT_PARAMS).map((w) => [w.name, w.needs])).toEqual([
      ['SMA(20)', 20],
      ['EMA(20)', 20],
      ['RSI(14)', 15],
      ['MACD(12, 26, 9)', 26],
      ['Bollinger(20, 2)', 20],
    ])
    expect(unfilled(DEFAULT_PARAMS, 26)).toEqual([])
    expect(unfilled(DEFAULT_PARAMS, 20).map((w) => w.name)).toEqual(['MACD(12, 26, 9)'])
    expect(unfilled(DEFAULT_PARAMS, 14).map((w) => w.name)).toEqual(['SMA(20)', 'EMA(20)', 'RSI(14)', 'MACD(12, 26, 9)', 'Bollinger(20, 2)'])
  })

  test('the parameters a series was computed with are read back from the series', () => {
    expect(
      paramsOf({
        lookback_days: 120,
        sma_window: 5,
        ema_span: 6,
        rsi_period: 7,
        macd_fast: 3,
        macd_slow: 8,
        macd_signal_period: 4,
        bollinger_window: 9,
        bollinger_std: 1.5,
      }),
    ).toEqual({ lookbackDays: 120, smaWindow: 5, emaSpan: 6, rsiPeriod: 7, macdFast: 3, macdSlow: 8, macdSignal: 4, bollingerWindow: 9, bollingerStd: 1.5 })
  })
})

test.describe('the view Market Analysis is showing, kept in its address', () => {
  test('an empty address opens the Pulse, this year, no share', () => {
    expect(viewFromQuery('', 2026)).toEqual({ tab: 'pulse', symbol: null, year: 2026 })
  })

  test('a tab, a share (upper-cased) and a year are read from the address', () => {
    expect(viewFromQuery('?tab=technical&symbol=reliance', 2026)).toEqual({ tab: 'technical', symbol: 'RELIANCE', year: 2026 })
    expect(viewFromQuery('?tab=holidays&year=2027', 2026)).toEqual({ tab: 'holidays', symbol: null, year: 2027 })
    expect(viewFromQuery('?symbol=M%26M', 2026).symbol).toBe('M&M')
  })

  test('anything the page does not know is ignored, not obeyed', () => {
    expect(viewFromQuery('?tab=admin', 2026).tab).toBe('pulse')
    expect(viewFromQuery('?symbol=%3Cscript%3Ealert(1)%3C%2Fscript%3E', 2026).symbol).toBeNull()
    expect(viewFromQuery(`?symbol=${'A'.repeat(33)}`, 2026).symbol).toBeNull()
    for (const year of ['abc', '1999', '2101', '2027.5', '']) expect(viewFromQuery(`?year=${year}`, 2026).year).toBe(2026)
  })

  test('the address holds only what the view needs, and nothing for the view it opens on', () => {
    expect(viewToQuery({ tab: 'pulse', symbol: 'X', year: 2027 }, 2026)).toBe('')
    expect(viewToQuery({ tab: 'technical', symbol: 'RELIANCE', year: 2026 }, 2026)).toBe('?tab=technical&symbol=RELIANCE')
    expect(viewToQuery({ tab: 'freshness', symbol: null, year: 2026 }, 2026)).toBe('?tab=freshness')
    expect(viewToQuery({ tab: 'datalake', symbol: 'X', year: 2026 }, 2026)).toBe('?tab=datalake')
    expect(viewToQuery({ tab: 'holidays', symbol: 'X', year: 2026 }, 2026)).toBe('?tab=holidays')
    expect(viewToQuery({ tab: 'holidays', symbol: null, year: 2027 }, 2026)).toBe('?tab=holidays&year=2027')
    expect(viewToQuery({ tab: 'technical', symbol: 'M&M', year: 2026 }, 2026)).toBe('?tab=technical&symbol=M%26M')
  })

  test('every view survives a round trip through the address', () => {
    for (const tab of ANALYSIS_TABS) {
      const view = { tab, symbol: tab === 'technical' || tab === 'freshness' ? 'BAJAJ-AUTO' : null, year: tab === 'holidays' ? 2028 : 2026 }
      expect(viewFromQuery(viewToQuery(view, 2026), 2026)).toEqual(view)
    }
  })

  test('a share symbol is letters, digits and the few marks a listed name holds', () => {
    for (const ok of ['RELIANCE', 'M&M', 'BAJAJ-AUTO', 'NIFTY_50', 'A.B']) expect(isSymbol(ok)).toBe(true)
    for (const bad of ['', '  ', 'two words', 'a/b', 'x;y', 'A'.repeat(33)]) expect(isSymbol(bad)).toBe(false)
  })
})

test.describe('the holiday calendar in words', () => {
  const holiday = (date: string, weekday: string, onWeekend: boolean, name: string | null = 'Holiday'): HolidayEntry => ({
    date,
    weekday,
    name,
    source: 'calendar file',
    on_weekend: onWeekend,
  })
  const list = [
    holiday('2026-10-20', 'Tuesday', false, 'Dussehra'),
    holiday('2026-11-08', 'Sunday', true, 'Diwali Laxmi Pujan'),
    holiday('2026-11-10', 'Tuesday', false, 'Diwali Balipratipada'),
    holiday('2026-12-25', 'Friday', false, 'Christmas'),
  ]

  test('the next closure skips a weekend holiday, includes today, and is null when none is left', () => {
    expect(nextClosure(list, '2026-11-02')?.name).toBe('Diwali Balipratipada')
    expect(nextClosure(list, '2026-11-10')?.name).toBe('Diwali Balipratipada')
    expect(nextClosure(list, '2026-11-11')?.name).toBe('Christmas')
    expect(nextClosure(list, '2026-12-26')).toBeNull()
    expect(nextClosure([], '2026-11-02')).toBeNull()
  })

  test('how far away a day is', () => {
    expect(whenWords('2026-11-02', '2026-11-02')).toBe('today')
    expect(whenWords('2026-11-03', '2026-11-02')).toBe('tomorrow')
    expect(whenWords('2026-11-01', '2026-11-02')).toBe('yesterday')
    expect(whenWords('2026-11-10', '2026-11-02')).toBe('in 8 days')
    expect(whenWords('2026-10-30', '2026-11-02')).toBe('3 days ago')
  })

  test('the weekend holidays are counted apart, because they close nothing extra', () => {
    expect(holidayCounts(list)).toEqual({ listed: 4, onWeekend: 1, closures: 3 })
    expect(holidayCounts([])).toEqual({ listed: 0, onWeekend: 0, closures: 0 })
  })

  test('two years either side of this one are offered, and a year from the address is added in its place', () => {
    expect(yearChoices(2026)).toEqual([2024, 2025, 2026, 2027, 2028])
    expect(yearChoices(2026, 2027)).toEqual([2024, 2025, 2026, 2027, 2028])
    expect(yearChoices(2026, 2040)).toEqual([2024, 2025, 2026, 2027, 2028, 2040])
    expect(yearChoices(2026, 2001)).toEqual([2001, 2024, 2025, 2026, 2027, 2028])
  })
})

test.describe('calendar days', () => {
  test('a day is real or it is nothing', () => {
    expect(Number.isNaN(utcOf('2026-02-30'))).toBe(true)
    expect(Number.isNaN(utcOf('2026-2-3'))).toBe(true)
    expect(Number.isNaN(utcOf(''))).toBe(true)
    expect(Number.isNaN(utcOf('2028-02-29'))).toBe(false)
    expect(Number.isNaN(utcOf('2027-02-29'))).toBe(true)
  })

  test('days are added across months, years and a leap day', () => {
    expect(addDays('2026-10-31', 1)).toBe('2026-11-01')
    expect(addDays('2026-01-01', -1)).toBe('2025-12-31')
    expect(addDays('2028-02-28', 1)).toBe('2028-02-29')
    expect(addDays('2027-02-28', 1)).toBe('2027-03-01')
    expect(daysBetween('2026-10-26', '2026-11-02')).toBe(7)
    expect(daysBetween('2026-11-02', '2026-10-26')).toBe(-7)
  })

  test("today is India's date whatever the browser's clock says", () => {
    expect(todayInIndia(new Date('2026-11-02T18:29:00Z'))).toBe('2026-11-02') // 23:59 in India
    expect(todayInIndia(new Date('2026-11-02T18:30:00Z'))).toBe('2026-11-03') // midnight in India
    expect(todayInIndia(new Date('2026-11-01T19:00:00Z'))).toBe('2026-11-02') // 00:30 in India
  })
})

test.describe('the data lake in words', () => {
  test('a pipeline is named and lit by its last run', () => {
    expect(pipelineLabel('incremental_daily')).toBe('Incremental Daily')
    expect(pipelineLabel('history_gap_repair')).toBe('History Gap Repair')
    expect(statusLight('success')).toBe('green')
    expect(statusLight('partial')).toBe('amber')
    expect(statusLight('failed')).toBe('red')
    expect(statusLight(null)).toBe('gray')
  })

  test('how far behind the newest daily data is', () => {
    expect(behindWords(null)).toEqual({ text: 'no daily data in the lake', tone: 'none' })
    expect(behindWords(0)).toEqual({ text: 'current', tone: 'ok' })
    expect(behindWords(1)).toEqual({ text: '1 trading day behind', tone: 'bad' })
    expect(behindWords(3)).toEqual({ text: '3 trading days behind', tone: 'bad' })
  })
})

test.describe('the Markdown a report is written in', () => {
  const REPORT = [
    '# Investor Report: 2026-10-19 to 2026-10-25 (weekly)',
    '',
    '## Paper Trading Performance',
    '',
    '- Realized P&L this period: 250.00',
    '- Closing trades: 1 (1 winning, 0 losing), win rate 100%',
    '',
    '## By Strategy',
    '',
    '- **InvestorReportDemo**: 1 fill(s), realized P&L 250.00',
    '',
  ].join('\n')

  test('a real report becomes headings and bullet lists, with the strategy name in bold', () => {
    const blocks = parseMarkdown(REPORT)
    expect(blocks.map((b) => b.kind)).toEqual(['heading', 'heading', 'list', 'heading', 'list'])
    expect(blocks[0]).toEqual({ kind: 'heading', level: 1, inline: [{ kind: 'text', text: 'Investor Report: 2026-10-19 to 2026-10-25 (weekly)' }] })
    expect(blocks[2]).toMatchObject({ kind: 'list', ordered: false })
    expect((blocks[2] as { items: unknown[] }).items).toHaveLength(2)
    expect((blocks[4] as { items: unknown[][] }).items[0]).toEqual([
      { kind: 'bold', text: 'InvestorReportDemo' },
      { kind: 'text', text: ': 1 fill(s), realized P&L 250.00' },
    ])
  })

  test('bold, code and italic are read, and an underscore in a name is not emphasis', () => {
    expect(parseInline('a **b** `c` *d* e')).toEqual([
      { kind: 'text', text: 'a ' },
      { kind: 'bold', text: 'b' },
      { kind: 'text', text: ' ' },
      { kind: 'code', text: 'c' },
      { kind: 'text', text: ' ' },
      { kind: 'italic', text: 'd' },
      { kind: 'text', text: ' e' },
    ])
    expect(parseInline('max_drawdown_pct and _x_')).toEqual([{ kind: 'text', text: 'max_drawdown_pct and _x_' }])
  })

  test('markers that do not pair, and arithmetic, stay as written', () => {
    expect(parseInline('2 ** 3')).toEqual([{ kind: 'text', text: '2 ** 3' }])
    expect(parseInline('5 * 3 * 2')).toEqual([{ kind: 'text', text: '5 * 3 * 2' }])
    expect(parseInline('**')).toEqual([{ kind: 'text', text: '**' }])
  })

  test('markup that is not Markdown is text, never an element', () => {
    const [block] = parseMarkdown('<img src=x onerror=alert(1)> and [a link](javascript:alert(1))')
    expect(block).toEqual({ kind: 'paragraph', inline: [{ kind: 'text', text: '<img src=x onerror=alert(1)> and [a link](javascript:alert(1))' }] })
  })

  test('lines run together into a paragraph and a blank line ends it', () => {
    expect(parseMarkdown('line one\nline two\n\nnext')).toEqual([
      { kind: 'paragraph', inline: [{ kind: 'text', text: 'line one line two' }] },
      { kind: 'paragraph', inline: [{ kind: 'text', text: 'next' }] },
    ])
  })

  test('numbered lists, a change of list kind, and an indented continuation', () => {
    expect(parseMarkdown('1. a\n2. b')).toEqual([{ kind: 'list', ordered: true, items: [[{ kind: 'text', text: 'a' }], [{ kind: 'text', text: 'b' }]] }])
    expect(parseMarkdown('- a\n1. b').map((b) => b.kind)).toEqual(['list', 'list'])
    expect(parseMarkdown('- first\n  more\n- second')).toEqual([
      { kind: 'list', ordered: false, items: [[{ kind: 'text', text: 'first more' }], [{ kind: 'text', text: 'second' }]] },
    ])
  })

  test('a rule, a deep heading, and Windows line ends', () => {
    expect(parseMarkdown('---')).toEqual([{ kind: 'rule' }])
    expect(parseMarkdown('- - -')).toEqual([{ kind: 'rule' }])
    expect(parseMarkdown('##### deep')).toEqual([{ kind: 'heading', level: 4, inline: [{ kind: 'text', text: 'deep' }] }])
    expect(parseMarkdown('# a\r\n\r\n- b\r\n').map((b) => b.kind)).toEqual(['heading', 'list'])
    expect(parseMarkdown('')).toEqual([])
  })

  test('a report is saved under a name that says its period and cadence', () => {
    expect(reportFilename({ period_start: '2026-10-19', period_end: '2026-10-25', cadence: 'weekly' })).toBe('investor-report-2026-10-19-to-2026-10-25-weekly.md')
  })
})

test.describe('which periods an investor report may be asked for', () => {
  const today = '2026-11-02' // a Monday

  test('a week ending today, and a month, are accepted', () => {
    expect(periodProblem('2026-10-27', '2026-11-02', 'weekly', today)).toBeNull()
    expect(periodProblem('2026-10-02', '2026-11-01', 'monthly', today)).toBeNull() // 31 days
    expect(periodProblem('2026-11-02', '2026-11-02', 'weekly', today)).toBeNull()
  })

  test('each refusal is in the server\'s own words', () => {
    expect(periodProblem('2026-10-20', '2026-10-19', 'weekly', today)).toBe('The period ends (2026-10-19) before it starts (2026-10-20).')
    expect(periodProblem('2026-10-30', '2026-11-03', 'weekly', today)).toBe(
      'The period ends on 2026-11-03, after today (2026-11-02) in India: nothing has happened on those days yet.',
    )
    expect(periodProblem('2026-10-26', '2026-11-02', 'weekly', today)).toBe('A weekly report covers at most 7 days; 2026-10-26 to 2026-11-02 is 8.')
    expect(periodProblem('2026-10-02', '2026-11-02', 'monthly', today)).toBe('A monthly report covers at most 31 days; 2026-10-02 to 2026-11-02 is 32.')
  })

  test('a missing or impossible date is named before the others', () => {
    expect(periodProblem('', '2026-10-25', 'weekly', today)).toBe('Choose the day the period starts.')
    expect(periodProblem('2026-10-19', '', 'weekly', today)).toBe('Choose the day the period ends.')
    expect(periodProblem('2026-02-30', '2026-03-02', 'weekly', today)).toBe('The dates must be real days, such as 2026-10-19.')
  })

  test('the period offered is the week or month just ended', () => {
    // Monday, Wednesday and Sunday all name the Monday-to-Sunday week before the one in progress
    for (const day of ['2026-11-02', '2026-11-04', '2026-11-08']) expect(defaultPeriod('weekly', day)).toEqual({ start: '2026-10-26', end: '2026-11-01' })
    expect(defaultPeriod('monthly', '2026-11-02')).toEqual({ start: '2026-10-01', end: '2026-10-31' })
    expect(defaultPeriod('monthly', '2026-01-15')).toEqual({ start: '2025-12-01', end: '2025-12-31' })
    expect(defaultPeriod('monthly', '2026-03-01')).toEqual({ start: '2026-02-01', end: '2026-02-28' })
  })

  test('the period offered is always one the server will accept', () => {
    for (let i = 0; i < 400; i++) {
      const day = addDays('2026-01-01', i)
      for (const cadence of ['weekly', 'monthly'] as const) {
        const { start, end } = defaultPeriod(cadence, day)
        expect(periodProblem(start, end, cadence, day), `${cadence} on ${day}`).toBeNull()
      }
    }
  })

  test('the reports already written for exactly this period are found', () => {
    const reports = [
      { id: 'a', period_start: '2026-10-19', period_end: '2026-10-25', cadence: 'weekly' },
      { id: 'b', period_start: '2026-10-19', period_end: '2026-10-25', cadence: 'monthly' },
      { id: 'c', period_start: '2026-10-12', period_end: '2026-10-18', cadence: 'weekly' },
    ]
    expect(reportsFor(reports, '2026-10-19', '2026-10-25', 'weekly').map((r) => r.id)).toEqual(['a'])
    expect(reportsFor(reports, '2026-10-26', '2026-11-01', 'weekly')).toEqual([])
  })
})

test.describe('the Portfolio page\'s figures and decisions', () => {
  test('amounts are grouped the Indian way', () => {
    expect(money(1234567.5)).toBe('12,34,567.5')
    expect(money(-1200)).toBe('-1,200')
    expect(money(0)).toBe('0')
    expect(money(12.346)).toBe('12.35')
  })

  test('the net is the gross less the costs, to the paisa', () => {
    expect(netOfCosts(100.1, 0.3)).toBe(99.8)
    expect(netOfCosts(-50, 2.5)).toBe(-52.5)
    expect(netOfCosts(0.3, 0.1)).toBe(0.2)
    expect(netOfCosts(0, 0)).toBe(0)
  })

  test('accepting and rejecting each say that nothing is traded, and what the button is called', () => {
    expect(decisionWords('accept')).toMatchObject({ title: 'Accept this recommendation?', confirmLabel: 'Accept', busyLabel: 'Accepting…' })
    expect(decisionWords('accept').description).toContain('Nothing is traded or rebalanced')
    expect(decisionWords('reject')).toMatchObject({ title: 'Reject this recommendation?', confirmLabel: 'Reject', busyLabel: 'Rejecting…' })
    expect(decisionWords('reject').description).toContain('cannot be reopened')
  })
})
