'use client'

import { useEffect, useId, useState } from 'react'
import {
  type EnrolPaperRequest,
  type PaperInstrumentKind,
  type PaperOptionStrategy,
  type PaperSubscription,
  type Strategy,
  ApiError,
  activatePaperSubscription,
  closePaperPosition,
  deactivatePaperSubscription,
  enrolPaperSubscription,
  listStrategies,
} from '@/lib/api'
import { KIND_LABEL, OPTION_STRATEGY_LABEL, strategySummary, subscriptionName } from '@/lib/paper-trading'
import { Modal } from './modal'

function messageOf(error: unknown, fallback: string): string {
  return error instanceof ApiError || error instanceof Error ? error.message : fallback
}

/** The strategy version a new subscription is attached to: the newest version that passed static
 * validation, or the newest at all. */
function latestVersionId(strategy: Strategy): string | null {
  const versions = [...strategy.versions].sort((a, b) => b.version_number - a.version_number)
  return (versions.find((v) => v.static_validation_passed) ?? versions[0])?.id ?? null
}

function Field({ label, hint, children }: { label: string; hint?: string; children: (id: string) => React.ReactNode }) {
  const id = useId()
  return (
    <div className="pt-field">
      <label htmlFor={id}>{label}</label>
      {children(id)}
      {hint && <small>{hint}</small>}
    </div>
  )
}

function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: T
  options: { value: T; label: string }[]
  onChange: (value: T) => void
}) {
  return (
    <div className="pt-field" role="radiogroup" aria-label={label}>
      <span className="pt-legend">{label}</span>
      <div className="pt-seg">
        {options.map((option) => (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={value === option.value}
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>
    </div>
  )
}

// ---- Enrol ----

export function EnrolDialog({ onClose, onDone }: { onClose: () => void; onDone: (id: string) => void }) {
  const [strategies, setStrategies] = useState<Strategy[] | null>(null)
  const [strategyId, setStrategyId] = useState('')
  const [kind, setKind] = useState<PaperInstrumentKind>('equity')
  const [symbol, setSymbol] = useState('')
  const [lots, setLots] = useState('1')
  const [optionStrategy, setOptionStrategy] = useState<PaperOptionStrategy>('vertical_spread')
  const [width, setWidth] = useState('2')
  const [capital, setCapital] = useState('100000')
  const [stopLoss, setStopLoss] = useState('')
  const [signal, setSignal] = useState<'sma_crossover' | 'always_long'>('sma_crossover')
  const [smaWindow, setSmaWindow] = useState('15')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    listStrategies()
      .then((all) => {
        if (cancelled) return
        const usable = all.filter((s) => latestVersionId(s) !== null)
        setStrategies(usable)
        if (usable.length > 0) setStrategyId((current) => current || usable[0].id)
      })
      .catch((err) => {
        if (!cancelled) {
          setStrategies([])
          setError(messageOf(err, 'Could not load strategies'))
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  const defaultStop = kind === 'option' ? '50' : '3'
  const stopHint =
    kind === 'option'
      ? 'Percent of the premium. Options swing far more than shares, so the default is 50.'
      : 'Percent move against the position that closes it.'

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    const strategy = strategies?.find((s) => s.id === strategyId)
    const versionId = strategy ? latestVersionId(strategy) : null
    if (!versionId) return setError('Choose a strategy first.')
    if (!symbol.trim()) return setError(kind === 'equity' ? 'Enter a symbol, for example RELIANCE.' : 'Enter the underlying, for example NIFTY.')
    const body: EnrolPaperRequest = {
      strategy_version_id: versionId,
      symbol: symbol.trim().toUpperCase(),
      instrument_kind: kind,
      builtin_strategy: signal,
      sma_window: Number(smaWindow),
      initial_capital: Number(capital),
      stop_loss_pct: Number(stopLoss || defaultStop),
    }
    if (kind !== 'equity') body.lots = Number(lots)
    if (kind === 'option') {
      body.option_strategy = optionStrategy
      if (optionStrategy === 'vertical_spread') body.spread_width = Number(width)
    }
    setBusy(true)
    setError(null)
    try {
      const created = await enrolPaperSubscription(body)
      onDone(created.id)
    } catch (err) {
      setError(messageOf(err, 'Could not add the subscription'))
      setBusy(false)
    }
  }

  return (
    <Modal
      title="Add a paper subscription"
      description="Trades on real prices only. If no real price feed exists for it, it waits instead of making one up."
      onClose={onClose}
      busy={busy}
    >
      <form className="pt-form" onSubmit={submit}>
        <Field label="Strategy" hint={strategies?.length === 0 ? 'No strategy has a version yet. Create one on the Strategies page.' : undefined}>
          {(id) => (
            <select id={id} className="pt-input" value={strategyId} onChange={(e) => setStrategyId(e.target.value)} disabled={!strategies?.length}>
              {strategies === null && <option>Loading…</option>}
              {strategies?.map((s) => (
                <option key={s.id} value={s.id}>{s.name}</option>
              ))}
            </select>
          )}
        </Field>

        <Segmented
          label="Trades"
          value={kind}
          options={(['equity', 'future', 'option'] as const).map((value) => ({ value, label: KIND_LABEL[value] }))}
          onChange={setKind}
        />

        <div className="pt-row">
          <Field label={kind === 'equity' ? 'Symbol' : 'Underlying'} hint={kind === 'equity' ? 'NSE symbol, for example RELIANCE' : 'For example NIFTY or RELIANCE'}>
            {(id) => <input id={id} className="pt-input" value={symbol} onChange={(e) => setSymbol(e.target.value)} autoComplete="off" spellCheck={false} />}
          </Field>
          {kind !== 'equity' && (
            <Field label="Lots" hint="Whole lots per entry">
              {(id) => <input id={id} className="pt-input" type="number" min={1} max={1000} step={1} value={lots} onChange={(e) => setLots(e.target.value)} />}
            </Field>
          )}
        </div>

        {kind === 'option' && (
          <>
            <Segmented
              label="Structure"
              value={optionStrategy}
              options={(['vertical_spread', 'long_option'] as const).map((value) => ({ value, label: OPTION_STRATEGY_LABEL[value] }))}
              onChange={setOptionStrategy}
            />
            <p className="pt-muted" style={{ fontSize: '.64rem', marginTop: '-.4rem' }}>
              {optionStrategy === 'vertical_spread'
                ? 'Sells the at-the-money option and buys a hedge further out, so the loss is capped. Naked selling is not allowed.'
                : strategySummary('long_option')}
            </p>
            {optionStrategy === 'vertical_spread' && (
              <Field label="Hedge distance" hint="Strikes between the sold and the bought option">
                {(id) => <input id={id} className="pt-input" type="number" min={1} max={20} step={1} value={width} onChange={(e) => setWidth(e.target.value)} />}
              </Field>
            )}
          </>
        )}

        <div className="pt-row">
          <Field label="Capital (₹)">
            {(id) => <input id={id} className="pt-input" type="number" min={1} step="any" value={capital} onChange={(e) => setCapital(e.target.value)} />}
          </Field>
          <Field label="Stop-loss (%)" hint={stopHint}>
            {(id) => <input id={id} className="pt-input" type="number" min={0.1} step="any" placeholder={defaultStop} value={stopLoss} onChange={(e) => setStopLoss(e.target.value)} />}
          </Field>
        </div>

        <Segmented
          label="Signal"
          value={signal}
          options={[
            { value: 'sma_crossover', label: 'Moving average' },
            { value: 'always_long', label: 'Always long' },
          ]}
          onChange={setSignal}
        />
        {signal === 'sma_crossover' && (
          <Field label="Average length" hint="Price points in the moving average. It waits until it has this much real history.">
            {(id) => <input id={id} className="pt-input" type="number" min={2} step={1} value={smaWindow} onChange={(e) => setSmaWindow(e.target.value)} />}
          </Field>
        )}

        {error && <div className="pt-error" role="alert">{error}</div>}
        <div className="pt-modal-foot">
          <button type="button" className="button-secondary" onClick={onClose} disabled={busy}>Cancel</button>
          <button type="submit" className="button-primary" disabled={busy || !strategies?.length}>{busy ? 'Adding…' : 'Add subscription'}</button>
        </div>
      </form>
    </Modal>
  )
}

// ---- Deactivate ----

export function DeactivateDialog({ sub, onClose, onDone }: { sub: PaperSubscription; onClose: () => void; onDone: () => void }) {
  const holding = sub.open_legs > 0
  const [choice, setChoice] = useState<'close' | 'leave_unmanaged'>('close')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (reason.trim().length < 3) return setError('Give a reason of at least 3 characters.')
    setBusy(true)
    setError(null)
    try {
      await deactivatePaperSubscription(sub.id, { reason: reason.trim(), ...(holding ? { on_open_position: choice } : {}) })
      onDone()
    } catch (err) {
      setError(messageOf(err, 'Could not switch it off'))
      setBusy(false)
    }
  }

  return (
    <Modal
      title={`Switch off ${subscriptionName(sub)}`}
      description={holding ? `It holds ${sub.open_legs} open ${sub.open_legs === 1 ? 'leg' : 'legs'}. Choose what happens to ${sub.open_legs === 1 ? 'it' : 'them'}.` : 'It holds no position, so nothing else changes.'}
      onClose={onClose}
      busy={busy}
    >
      <form className="pt-form" onSubmit={submit}>
        {holding && (
          <div className="pt-field" role="radiogroup" aria-label="Open position">
            <label className="pt-radio" data-on={choice === 'close'}>
              <input type="radio" name="on_open" checked={choice === 'close'} onChange={() => setChoice('close')} />
              <div><strong>Close it now</strong><span>Sold legs are bought back first, then the rest, at real depth prices.</span></div>
            </label>
            <label className="pt-radio" data-on={choice === 'leave_unmanaged'}>
              <input type="radio" name="on_open" checked={choice === 'leave_unmanaged'} onChange={() => setChoice('leave_unmanaged')} />
              <div><strong>Leave it open, unmanaged</strong><span>Nothing watches its stop-loss until you close it or switch the subscription back on.</span></div>
            </label>
          </div>
        )}
        <Field label="Reason" hint="Recorded in the audit log.">
          {(id) => <input id={id} className="pt-input" value={reason} onChange={(e) => setReason(e.target.value)} autoComplete="off" />}
        </Field>
        {error && <div className="pt-error" role="alert">{error}</div>}
        <div className="pt-modal-foot">
          <button type="button" className="button-secondary" onClick={onClose} disabled={busy}>Keep running</button>
          <button type="submit" className="button-danger" disabled={busy}>{busy ? 'Switching off…' : 'Switch off'}</button>
        </div>
      </form>
    </Modal>
  )
}

// ---- Close position ----

export function CloseDialog({
  sub,
  isAdmin,
  onClose,
  onDone,
}: {
  sub: PaperSubscription
  isAdmin: boolean
  onClose: () => void
  onDone: (result: { legs: number; failures: string[] }) => void
}) {
  const [reason, setReason] = useState('')
  const [price, setPrice] = useState('')
  const [administrative, setAdministrative] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const single = sub.open_legs === 1 // one price cannot stand for the legs of a spread
  const supplied = single && price.trim() !== ''

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (reason.trim().length < 3) return setError('Give a reason of at least 3 characters.')
    if (supplied && !(Number(price) > 0)) return setError('The price must be above zero.')
    if (administrative && !supplied) return setError('An administrative close needs the price to close at.')
    setBusy(true)
    setError(null)
    try {
      const result = await closePaperPosition(sub.id, {
        reason: reason.trim(),
        ...(supplied ? { price: Number(price) } : {}),
        ...(single && administrative ? { administrative: true } : {}),
      })
      onDone({ legs: result.legs.length || 1, failures: result.failures })
    } catch (err) {
      setError(messageOf(err, 'Could not close the position'))
      setBusy(false)
    }
  }

  return (
    <Modal
      title={`Close ${subscriptionName(sub)}`}
      description={
        sub.open_legs === 1
          ? 'Sells or buys back the position through the risk gate, walking the real market depth. It can fill partly.'
          : `Closes the ${sub.open_legs} open legs one by one through the risk gate. Sold legs are bought back first so a hedge is never left behind.`
      }
      onClose={onClose}
      busy={busy}
    >
      <form className="pt-form" onSubmit={submit}>
        <Field label="Reason" hint="Recorded in the audit log.">
          {(id) => <input id={id} className="pt-input" value={reason} onChange={(e) => setReason(e.target.value)} autoComplete="off" />}
        </Field>
        {single && (
          <Field label="Price (₹), when there is no fresh real price" hint="Leave empty to use a real price under two minutes old. With the market shut there is none, so enter one; it is recorded as supplied by you.">
            {(id) => <input id={id} className="pt-input" type="number" min={0} step="any" value={price} onChange={(e) => setPrice(e.target.value)} />}
          </Field>
        )}
        {single && isAdmin && (
          <label className="pt-radio" data-on={administrative}>
            <input type="checkbox" checked={administrative} onChange={(e) => setAdministrative(e.target.checked)} />
            <div><strong>Administrative close</strong><span>For clearing stale or demo positions: skips the risk gate and fills the whole quantity at the price above, with no costs. Audited as administrative.</span></div>
          </label>
        )}
        {error && <div className="pt-error" role="alert">{error}</div>}
        <div className="pt-modal-foot">
          <button type="button" className="button-secondary" onClick={onClose} disabled={busy}>Cancel</button>
          <button type="submit" className="button-danger" disabled={busy}>{busy ? 'Closing…' : 'Close position'}</button>
        </div>
      </form>
    </Modal>
  )
}

/** Switch a subscription back on (no dialog: it has no choices). */
export async function activate(sub: PaperSubscription): Promise<string | null> {
  try {
    await activatePaperSubscription(sub.id)
    return null
  } catch (err) {
    return messageOf(err, 'Could not switch it on')
  }
}
