import { expect, test } from '@playwright/test'
import type { LiveTradingSubscriptionDto, PositionByStrategy, StrategyVersion, SuggestionDto } from '../lib/api'
import { describeDiff, diffLines, diffSummary, foldUnchanged } from '../lib/diff'
import {
  changedLimits,
  describeLimitChange,
  heldBy,
  limitLines,
  limitProblems,
  limitsForm,
  limitsRequest,
  liveSubscriptionRows,
  switchOffBlocked,
} from '../lib/live-limits'
import {
  defaultCompare,
  sandboxFacts,
  sandboxNote,
  suggestionRows,
  suggestionStatus,
  validationNote,
  versionRows,
} from '../lib/strategy-versions'

// What the strategy pages decide, with no browser and no backend: each case states the answer worked out by hand.

const lines = (...text: string[]) => text.join('\n')

test.describe('the difference between two versions', () => {
  test('two identical texts have no difference', () => {
    const diff = diffLines(lines('a', 'b'), lines('a', 'b'))
    expect(diff.map((l) => l.kind)).toEqual(['same', 'same'])
    expect(describeDiff(diffSummary(diff))).toBe('No difference: the two are the same.')
  })

  test('a changed line is a removal and an addition, with the line numbers of each side', () => {
    const diff = diffLines(lines('a', 'b', 'c'), lines('a', 'B', 'c'))
    expect(diff).toEqual([
      { kind: 'same', text: 'a', oldLine: 1, newLine: 1 },
      { kind: 'remove', text: 'b', oldLine: 2, newLine: null },
      { kind: 'add', text: 'B', oldLine: null, newLine: 2 },
      { kind: 'same', text: 'c', oldLine: 3, newLine: 3 },
    ])
  })

  test('an added line in the middle shifts the numbers after it on one side only', () => {
    const diff = diffLines(lines('a', 'c'), lines('a', 'b', 'c'))
    expect(diff.map((l) => [l.kind, l.oldLine, l.newLine])).toEqual([
      ['same', 1, 1],
      ['add', null, 2],
      ['same', 2, 3],
    ])
  })

  test('lines that moved are a removal and an addition, in the order they appear', () => {
    const diff = diffLines(lines('a', 'b', 'c'), lines('b', 'c', 'a'))
    expect(diffSummary(diff)).toEqual({ added: 1, removed: 1, unchanged: 2 })
  })

  test('a text with nothing in common is all removed and all added', () => {
    expect(diffSummary(diffLines(lines('a', 'b'), lines('x', 'y', 'z')))).toEqual({ added: 3, removed: 2, unchanged: 0 })
  })

  test('from nothing is all added, to nothing is all removed', () => {
    expect(diffSummary(diffLines('', lines('a', 'b')))).toEqual({ added: 2, removed: 0, unchanged: 0 })
    expect(diffSummary(diffLines(lines('a', 'b'), ''))).toEqual({ added: 0, removed: 2, unchanged: 0 })
  })

  test('windows line endings do not make every line differ', () => {
    expect(diffSummary(diffLines('a\r\nb', 'a\nb'))).toEqual({ added: 0, removed: 0, unchanged: 2 })
  })

  test('a line removed at the end and one added at the start are told apart from the lines they surround', () => {
    const diff = diffLines(lines('keep', 'drop'), lines('new', 'keep'))
    expect(diff.map((l) => `${l.kind}:${l.text}`)).toEqual(['add:new', 'same:keep', 'remove:drop'])
  })

  test('says how many lines changed', () => {
    expect(describeDiff({ added: 12, removed: 5, unchanged: 3 })).toBe('+12 −5 lines')
  })

  test('long unchanged runs are folded away, keeping some lines around each change', () => {
    const before = Array.from({ length: 20 }, (_, i) => `line ${i + 1}`)
    const after = [...before]
    after[9] = 'changed'

    const folded = foldUnchanged(diffLines(before.join('\n'), after.join('\n')), 2)

    expect(folded.map((h) => (h.kind === 'gap' ? `gap ${h.hidden}` : `${h.kind}:${h.text}`))).toEqual([
      'gap 7',
      'same:line 8',
      'same:line 9',
      'remove:line 10',
      'add:changed',
      'same:line 11',
      'same:line 12',
      'gap 8',
    ])
  })

  test('nothing is folded when everything is near a change', () => {
    const folded = foldUnchanged(diffLines(lines('a', 'b', 'c'), lines('a', 'X', 'c')), 3)
    expect(folded.some((h) => h.kind === 'gap')).toBe(false)
  })
})

const version = (n: number, over: Partial<StrategyVersion> = {}): StrategyVersion => ({
  id: `v${n}`,
  version_number: n,
  code: `code ${n}`,
  static_validation_passed: true,
  static_validation_errors: null,
  sandbox_passed: true,
  sandbox_result: { success: true, result: { trades: 3 }, error: null, duration_seconds: 0.42 },
  options_legs: null,
  created_at: '2026-11-02T04:00:00Z',
  ...over,
})

const suggestion = (over: Partial<SuggestionDto> = {}): SuggestionDto => ({
  id: 's1',
  strategy_id: 'st',
  base_version_id: 'v1',
  suggestion_text: 'tighten the stop',
  status: 'regenerated',
  ai_verdict: { verdict: 'accept', notes: 'sensible' },
  regenerated_version_id: 'v2',
  regeneration_diff: '- a\n+ b',
  requested_by: 'alice',
  created_at: '2026-11-02T04:00:00Z',
  reviewed_at: null,
  ...over,
})

test.describe('a strategy’s versions', () => {
  test('say whether validation passed, and what failed', () => {
    expect(validationNote(version(1))).toBe('Passed static validation')
    expect(validationNote(version(1, { static_validation_passed: false, static_validation_errors: ['uses eval', 'imports os'] }))).toBe(
      'Failed static validation: uses eval; imports os',
    )
    expect(validationNote(version(1, { static_validation_passed: false, static_validation_errors: null }))).toBe('Failed static validation')
  })

  test('say whether the sandbox ran, and how long it took', () => {
    expect(sandboxNote(version(1))).toBe('Sandbox: passed in 0.4 s')
    expect(sandboxNote(version(1, { sandbox_passed: null, sandbox_result: null }))).toBe('Sandbox: not run')
    expect(sandboxNote(version(1, { sandbox_passed: false, sandbox_result: { success: false, error: 'timeout' } }))).toBe('Sandbox: failed')
  })

  test('list what a sandbox run recorded, the code’s own return value last', () => {
    expect(sandboxFacts(version(1)).map((f) => f.label)).toEqual(['Result', 'Took', 'What the code returned'])
    const failed = sandboxFacts(version(1, { sandbox_passed: false, sandbox_result: { success: false, error: ' boom ', result: null, duration_seconds: 1 } }))
    expect(failed).toEqual([
      { label: 'Result', value: 'Failed' },
      { label: 'Took', value: '1.00 seconds' },
      { label: 'Error', value: 'boom' },
    ])
    expect(sandboxFacts(version(1, { sandbox_result: null }))).toEqual([])
  })

  test('come newest first, the first from the objective and a later one from the suggestion that made it', () => {
    const rows = versionRows([version(1), version(2)], [suggestion()])

    expect(rows.map((r) => r.label)).toEqual(['v2', 'v1'])
    expect(rows[0].from).toBe('Regenerated from a suggestion by alice: “tighten the stop”')
    expect(rows[1].from).toBe('Generated from the strategy’s objective')
  })

  test('a version nothing says where it came from has no origin, rather than a made-up one', () => {
    const rows = versionRows([version(1), version(2), version(3)], [suggestion({ regenerated_version_id: 'v2' })])
    expect(rows[0].label).toBe('v3')
    expect(rows[0].from).toBeNull()
  })

  test('a long suggestion is clipped in the origin line', () => {
    const rows = versionRows([version(1), version(2)], [suggestion({ suggestion_text: 'x'.repeat(200) })])
    expect(rows[0].from?.endsWith('…”')).toBe(true)
    expect(rows[0].from?.length).toBeLessThan(120)
  })

  test('compare opens on the one before the newest and the newest', () => {
    expect(defaultCompare([version(1), version(3), version(2)])).toEqual({ from: 'v2', to: 'v3' })
    expect(defaultCompare([version(1)])).toEqual({ from: null, to: 'v1' })
    expect(defaultCompare([])).toEqual({ from: null, to: null })
  })
})

test.describe('suggestions', () => {
  test('say where each stands, in words', () => {
    expect(suggestionStatus('pending')).toBe('Waiting for the AI review')
    expect(suggestionStatus('regenerated')).toBe('Turned into a new version')
    expect(suggestionStatus('something-new')).toBe('something-new')
  })

  test('tell who asked, on which version, what the reviewer said and which version it became', () => {
    const [row] = suggestionRows([suggestion()], [version(1), version(2)])
    expect(row).toMatchObject({ status: 'Turned into a new version', base: 'v1', produced: 'v2', verdict: 'accept: sensible' })
    expect(row.asked).toBe('alice · 02 Nov 2026, 09:30:00')
  })

  test('one nobody reviewed or regenerated has neither a verdict nor a result', () => {
    const [row] = suggestionRows([suggestion({ status: 'pending', ai_verdict: null, regenerated_version_id: null, requested_by: null })], [version(1)])
    expect(row.verdict).toBeNull()
    expect(row.produced).toBeNull()
    expect(row.asked.startsWith('Someone')).toBe(true)
  })
})

const sub = (over: Partial<LiveTradingSubscriptionDto> = {}): LiveTradingSubscriptionDto => ({
  id: 'sub-1',
  strategy_id: 'st-1',
  symbol: 'RELIANCE',
  broker_name: 'zerodha',
  builtin_strategy: 'sma_crossover',
  sma_window: 15,
  initial_capital: 100000,
  stop_loss_pct: 3,
  position_size_pct: 5,
  intent_expiry_seconds: 90,
  is_active: true,
  autonomous_trading_enabled: false,
  autonomy_enabled_by: null,
  autonomy_enabled_at: null,
  max_intents_per_window: 5,
  rate_limit_window_minutes: 60,
  max_notional_per_intent: 50000,
  ...over,
})

test.describe('a live subscription’s limits', () => {
  test('open as they are, with nothing changed', () => {
    const form = limitsForm(sub())
    expect(form).toEqual({ max_intents_per_window: '5', rate_limit_window_minutes: '60', max_notional_per_intent: '50000', intent_expiry_seconds: '90' })
    expect(changedLimits(sub(), form)).toEqual([])
    expect(limitProblems(sub(), form, null)).toEqual({})
  })

  test('a stricter value for each is accepted and is the only thing sent, with the reason trimmed', () => {
    const form = { ...limitsForm(sub()), max_intents_per_window: '2', rate_limit_window_minutes: '120' }
    expect(limitProblems(sub(), form, null)).toEqual({})
    expect(limitsRequest(sub(), form, '  too many orders  ')).toEqual({ max_intents_per_window: 2, rate_limit_window_minutes: 120, reason: 'too many orders' })
  })

  test('a looser value is refused, with the way it may move', () => {
    const form = { ...limitsForm(sub()), max_intents_per_window: '9', rate_limit_window_minutes: '30' }
    const problems = limitProblems(sub(), form, null)
    expect(problems.max_intents_per_window).toMatch(/can only be lowered here \(it is 5\)/)
    expect(problems.rate_limit_window_minutes).toMatch(/can only be raised here \(it is 60\)/)
  })

  test('a value that is not a usable number is refused', () => {
    const problems = limitProblems(sub(), { ...limitsForm(sub()), max_intents_per_window: '', rate_limit_window_minutes: '0', max_notional_per_intent: 'abc', intent_expiry_seconds: '301' }, null)
    expect(Object.keys(problems).sort()).toEqual(['intent_expiry_seconds', 'max_intents_per_window', 'max_notional_per_intent', 'rate_limit_window_minutes'])
    expect(problems.intent_expiry_seconds).toBe('At most 300.')
  })

  test('a count is a whole number', () => {
    expect(limitProblems(sub(), { ...limitsForm(sub()), max_intents_per_window: '2.5' }, null).max_intents_per_window).toBe('It is a whole number.')
    expect(limitProblems(sub(), { ...limitsForm(sub()), max_notional_per_intent: '12345.5' }, null).max_notional_per_intent).toBeUndefined()
  })

  test('an order cap below what is held is refused, because it would block the order that closes it', () => {
    const held = { quantity: 10, cost: 20000 }
    expect(limitProblems(sub(), { ...limitsForm(sub()), max_notional_per_intent: '15000' }, held).max_notional_per_intent).toMatch(/would block the order that closes them/)
    expect(limitProblems(sub(), { ...limitsForm(sub()), max_notional_per_intent: '25000' }, held)).toEqual({})
    expect(limitProblems(sub(), { ...limitsForm(sub()), max_notional_per_intent: '15000' }, null)).toEqual({})
  })

  test('what is held comes from the live position on that symbol only', () => {
    const position = (over: Partial<PositionByStrategy>): PositionByStrategy => ({ mode: 'live', strategy_id: 'st-1', strategy_name: 'S', symbol: 'RELIANCE', quantity: 10, avg_cost: 2000, realized_pnl: 0, managed: true, ...over })
    expect(heldBy([position({})], sub())).toEqual({ quantity: 10, cost: 20000 })
    expect(heldBy([position({ quantity: -4, avg_cost: 100 })], sub())).toEqual({ quantity: -4, cost: 400 })
    expect(heldBy([position({ mode: 'paper' }), position({ symbol: 'TCS' }), position({ strategy_id: 'other' }), position({ quantity: 0 })], sub())).toBeNull()
    expect(heldBy(null, sub())).toBeNull()
  })

  test('are described in a line each, and a change as before and after', () => {
    expect(limitLines(sub())).toEqual(['5 orders every 60 minutes', '₹50,000 at most in one order', 'an order lives 90 seconds'])
    expect(describeLimitChange('max_notional_per_intent', 50000, 20000)).toBe('Largest order (₹) ₹50,000 → ₹20,000')
    expect(describeLimitChange('max_intents_per_window', 5, 2)).toBe('Orders per window 5 → 2')
  })

  test('a subscription holding shares cannot be switched off, with the way out; one that is flat can', () => {
    expect(switchOffBlocked(sub(), { quantity: 7, cost: 700 })).toMatch(/holds 7 shares of RELIANCE.*Turn autonomous trading off instead/)
    expect(switchOffBlocked(sub(), null)).toBeNull()
    expect(switchOffBlocked(sub({ is_active: false }), null)).toBe('It is already switched off.')
  })
})

test.describe('the live subscriptions list', () => {
  const strategies = [{ id: 'st-1', name: 'Alpha' }, { id: 'st-2', name: 'Beta' }]

  test('puts what trades on its own first and what is switched off last', () => {
    const rows = liveSubscriptionRows(
      [
        sub({ id: 'off', strategy_id: 'st-1', is_active: false }),
        sub({ id: 'idle', strategy_id: 'st-2' }),
        sub({ id: 'on', strategy_id: 'st-2', symbol: 'TCS', autonomous_trading_enabled: true, autonomy_enabled_by: 'risk@example.com' }),
      ],
      strategies,
    )

    expect(rows.map((r) => [r.id, r.state])).toEqual([['on', 'autonomous'], ['idle', 'autonomy-off'], ['off', 'switched-off']])
    expect(rows[0]).toMatchObject({ stateLabel: 'Trading on its own', autonomyNote: 'turned on by risk@example.com', strategyName: 'Beta' })
  })

  test('says each row’s limits in a line, and a switched-off subscription is not trading whatever its flag says', () => {
    const [row] = liveSubscriptionRows([sub({ is_active: false, autonomous_trading_enabled: true })], strategies)
    expect(row.state).toBe('switched-off')
    expect(row.limits).toBe('5 orders / 60 min · up to ₹50,000 an order')
    expect(row.autonomyNote).toBeNull()
  })

  test('a strategy the list does not know is shown by the start of its id', () => {
    expect(liveSubscriptionRows([sub({ strategy_id: 'abcdef12-0000' })], strategies)[0].strategyName).toBe('abcdef12')
  })
})
