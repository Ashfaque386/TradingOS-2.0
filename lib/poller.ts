/**
 * The polling rules behind `usePolling`, kept free of React and the DOM so a logic spec can drive
 * them with a fake clock: one request at a time, none while the tab is hidden, and the next one is
 * scheduled only after the last has finished (so a slow server is never hit by a backlog).
 */
export interface PollerOptions {
  /** Milliseconds between the end of one request and the start of the next; read at each schedule so it can change. */
  intervalMs: () => number
  /** True while the tab is hidden; a hidden tick is skipped, not queued. */
  isHidden?: () => boolean
  /** Injected in tests. */
  schedule?: (callback: () => void, ms: number) => unknown
  cancel?: (handle: unknown) => void
}

export interface Poller {
  start: () => void
  stop: () => void
  /** Run now. Resolves when that request has finished; joins the one in flight instead of starting a second. */
  refresh: () => Promise<void>
  /** Call when the tab becomes visible again: polls at once if a tick was skipped while hidden. */
  resume: () => void
}

export function createPoller(task: () => Promise<void>, options: PollerOptions): Poller {
  const schedule = options.schedule ?? ((callback, ms) => setTimeout(callback, ms))
  const cancel = options.cancel ?? ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>))
  const isHidden = options.isHidden ?? (() => false)

  let running = false
  let timer: unknown = null
  let inFlight: Promise<void> | null = null
  let skippedWhileHidden = false

  function arm() {
    if (!running) return
    if (timer !== null) cancel(timer)
    timer = schedule(tick, options.intervalMs())
  }

  function run(): Promise<void> {
    if (inFlight) return inFlight
    inFlight = task()
      .catch(() => undefined) // the task reports its own failure; the loop must survive it
      .finally(() => {
        inFlight = null
        arm()
      })
    return inFlight
  }

  function tick() {
    timer = null
    if (!running) return
    if (isHidden()) {
      skippedWhileHidden = true
      arm()
      return
    }
    void run()
  }

  return {
    start() {
      if (running) return
      running = true
      void run()
    },
    stop() {
      running = false
      if (timer !== null) cancel(timer)
      timer = null
    },
    refresh: run,
    resume() {
      if (!running || !skippedWhileHidden) return
      skippedWhileHidden = false
      if (timer !== null) cancel(timer)
      timer = null
      void run()
    },
  }
}
