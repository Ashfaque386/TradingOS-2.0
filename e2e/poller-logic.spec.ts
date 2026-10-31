import { expect, test } from '@playwright/test'
import { createPoller } from '../lib/poller'

// The polling rules behind usePolling, driven by a clock the test advances by hand.

function fakeClock() {
  let nextId = 1
  const pending = new Map<number, { at: number; callback: () => void }>()
  let now = 0
  return {
    schedule: (callback: () => void, ms: number) => {
      const id = nextId++
      pending.set(id, { at: now + ms, callback })
      return id
    },
    cancel: (handle: unknown) => {
      pending.delete(handle as number)
    },
    pendingCount: () => pending.size,
    advance(ms: number) {
      now += ms
      for (const [id, item] of [...pending]) {
        if (item.at <= now) {
          pending.delete(id)
          item.callback()
        }
      }
    },
  }
}

/** A task whose requests finish when the test says so. */
function controlledTask() {
  const calls: Array<() => void> = []
  return {
    task: () => new Promise<void>((resolve) => calls.push(resolve)),
    started: () => calls.length,
    finish: async (index = calls.length - 1) => {
      calls[index]()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    },
  }
}

test('it fetches at once, then again one interval after each request finishes', async () => {
  const clock = fakeClock()
  const work = controlledTask()
  const poller = createPoller(work.task, { intervalMs: () => 1000, ...clock })

  poller.start()
  expect(work.started()).toBe(1)

  clock.advance(5000) // the first request is still running: no second one starts
  expect(work.started()).toBe(1)

  await work.finish()
  clock.advance(999)
  expect(work.started()).toBe(1)
  clock.advance(1)
  expect(work.started()).toBe(2)
})

test('a failing request does not stop the loop', async () => {
  const clock = fakeClock()
  let calls = 0
  const poller = createPoller(
    async () => {
      calls += 1
      throw new Error('server down')
    },
    { intervalMs: () => 1000, ...clock },
  )
  poller.start()
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
  clock.advance(1000)
  expect(calls).toBe(2)
})

test('a refresh while a request is in flight joins it instead of starting another', async () => {
  const clock = fakeClock()
  const work = controlledTask()
  const poller = createPoller(work.task, { intervalMs: () => 1000, ...clock })

  poller.start()
  const joined = poller.refresh()
  expect(work.started()).toBe(1)
  await work.finish()
  await joined
})

test('the interval is read at each schedule, so a desk can speed up and slow down', async () => {
  const clock = fakeClock()
  const work = controlledTask()
  let interval = 30_000
  const poller = createPoller(work.task, { intervalMs: () => interval, ...clock })

  poller.start()
  await work.finish()
  clock.advance(30_000)
  expect(work.started()).toBe(2)

  interval = 5_000
  await work.finish()
  clock.advance(5_000)
  expect(work.started()).toBe(3)
})

test('nothing is fetched while the tab is hidden, and it catches up the moment it is shown', async () => {
  const clock = fakeClock()
  const work = controlledTask()
  let hidden = false
  const poller = createPoller(work.task, { intervalMs: () => 1000, isHidden: () => hidden, ...clock })

  poller.start()
  await work.finish()
  hidden = true
  clock.advance(1000)
  clock.advance(1000)
  expect(work.started()).toBe(1) // skipped, not queued

  hidden = false
  poller.resume()
  expect(work.started()).toBe(2)
})

test('resume does nothing when no tick was skipped', async () => {
  const clock = fakeClock()
  const work = controlledTask()
  const poller = createPoller(work.task, { intervalMs: () => 1000, ...clock })
  poller.start()
  await work.finish()
  poller.resume()
  expect(work.started()).toBe(1)
})

test('stop cancels the timer, and a request that finishes afterwards does not schedule another', async () => {
  const clock = fakeClock()
  const work = controlledTask()
  const poller = createPoller(work.task, { intervalMs: () => 1000, ...clock })

  poller.start()
  poller.stop()
  await work.finish()
  expect(clock.pendingCount()).toBe(0)
  clock.advance(10_000)
  expect(work.started()).toBe(1)
})
