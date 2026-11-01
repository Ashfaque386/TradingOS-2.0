import { expect, test } from '@playwright/test'
import { describeQuoteRefusals } from '../lib/broker-quotes'
import { circuitLine, exchangeFreshness, formatIstClock } from '../lib/paper-trading'

// What the desk says about a price's freshness and its circuit band, and what a broker's status says
// about refused quotes. No browser, no backend: each case states the answer worked out by hand.

test.describe('the exchange clock', () => {
  test('is shown in India time whatever the browser zone is', () => {
    expect(formatIstClock('2026-11-02T04:01:02Z')).toBe('09:31:02 IST')
    expect(formatIstClock('2026-11-01T18:30:00Z')).toBe('00:00:00 IST')
  })
})

test.describe('how fresh a price is by the exchange stamp', () => {
  test('no price yet', () => {
    expect(exchangeFreshness(null)).toEqual({ tone: 'unknown', text: 'No price has arrived yet.' })
  })

  test('a broker that sends no stamp is "not known", never "fresh"', () => {
    for (const exchange_time of [null, undefined]) {
      const result = exchangeFreshness({ exchange_time, exchange_age_seconds: null })
      expect(result.tone).toBe('unknown')
      expect(result.text).toBe('This broker sends no exchange timestamp we can read, so the price cannot be dated by the exchange.')
    }
  })

  test('a stamp of the last minute is fresh, and says when the last trade was', () => {
    const result = exchangeFreshness({ exchange_time: '2026-11-02T04:01:02Z', exchange_age_seconds: 3 })
    expect(result).toEqual({ tone: 'fresh', text: 'Last traded at 09:31:02 IST, 3s ago.' })
    expect(exchangeFreshness({ exchange_time: '2026-11-02T04:01:02Z', exchange_age_seconds: 60 }).tone).toBe('fresh')
  })

  test('an older stamp is a quiet instrument or a stopped feed, and the desk says it cannot tell which', () => {
    const result = exchangeFreshness({ exchange_time: '2026-11-02T04:01:02Z', exchange_age_seconds: 61 })
    expect(result.tone).toBe('quiet')
    expect(result.text).toBe('Last traded at 09:31:02 IST, 1m ago. A quiet instrument, or a feed that has stopped.')
  })
})

test.describe('which circuit band applies', () => {
  test('the exchange limits when the quote carried them', () => {
    expect(circuitLine({ lower_circuit: 90, upper_circuit: 110 }, 20)).toEqual({
      source: 'exchange',
      text: 'Exchange circuit limits ₹90.00 to ₹110.00.',
    })
  })

  test('the reference table, said to be exactly that, when it did not', () => {
    const expected = {
      source: 'reference_table',
      text: "The quote carried no exchange circuit limits, so the risk gate uses the reference table's 20% band from the previous close (not an exchange figure).",
    }
    expect(circuitLine({ lower_circuit: null, upper_circuit: null }, 20)).toEqual(expected)
    expect(circuitLine({ lower_circuit: 90, upper_circuit: null }, 20)).toEqual(expected) // half a band is no band
    expect(circuitLine(null, 20)).toEqual(expected)
  })

  test('nothing known at all', () => {
    expect(circuitLine(null, null)).toEqual({ source: 'none', text: 'No circuit band is known for this price.' })
    expect(circuitLine({}, undefined).source).toBe('none')
  })
})

test.describe('what a broker status says about refused quotes', () => {
  const status = (overrides = {}) => ({
    quotes_refused_count: 0,
    quotes_refused_last_symbol: null,
    quotes_refused_last_reason: null,
    quotes_refused_last_at: null,
    ...overrides,
  })

  test('nothing refused says nothing', () => {
    expect(describeQuoteRefusals(status())).toBeNull()
    expect(describeQuoteRefusals({})).toBeNull()
  })

  test('one refusal, with its symbol, time and reason', () => {
    expect(
      describeQuoteRefusals(
        status({
          quotes_refused_count: 1,
          quotes_refused_last_symbol: 'INFY',
          quotes_refused_last_at: '2026-11-02T04:01:00Z',
          quotes_refused_last_reason: 'the exchange stamp (2026-10-30 15:29 IST) is from a previous day',
        }),
      ),
    ).toBe(
      '1 quote refused since the credentials last changed, the latest INFY at 09:31 IST: the exchange stamp (2026-10-30 15:29 IST) is from a previous day. Nothing trades on a refused quote.',
    )
  })

  test('several, with only what is known', () => {
    expect(describeQuoteRefusals(status({ quotes_refused_count: 4 }))).toBe(
      '4 quotes refused since the credentials last changed. Nothing trades on a refused quote.',
    )
  })
})
