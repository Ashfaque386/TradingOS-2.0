'use client'

import { useEffect, useMemo, useState } from 'react'
import {
  type EnrolPaperRequest,
  type PaperEnrolPreview,
  type PaperInstrumentKind,
  type PaperOptionStrategy,
  type StrategySummary,
  enrolPaperSubscription,
  listStrategies,
  previewPaperEnrolment,
} from '@/lib/api'
import { KIND_LABEL, OPTION_STRATEGY_LABEL, strategySummary } from '@/lib/paper-trading'
import { Field, Segmented } from '@/components/ui/field'
import { latestVersionId, messageOf } from './helpers'
import { EnrolPreviewPanel } from './enrol-preview'
import { Modal } from '@/components/ui/modal'
import { SymbolPicker } from './symbol-picker'

const PREVIEW_DEBOUNCE_MS = 500

export function EnrolDialog({ onClose, onDone }: { onClose: () => void; onDone: (id: string) => void }) {
  const [strategies, setStrategies] = useState<StrategySummary[] | null>(null)
  const [strategyId, setStrategyId] = useState('')
  const [kind, setKind] = useState<PaperInstrumentKind>('equity')
  const [symbol, setSymbol] = useState('')
  const [lots, setLots] = useState('1')
  const [optionStrategy, setOptionStrategy] = useState<PaperOptionStrategy>('vertical_spread')
  const [width, setWidth] = useState('2')
  const [capital, setCapital] = useState('100000')
  const [stopLoss, setStopLoss] = useState('')
  const [sizePct, setSizePct] = useState('5')
  const [signal, setSignal] = useState<'sma_crossover' | 'always_long'>('sma_crossover')
  const [smaWindow, setSmaWindow] = useState('15')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [preview, setPreview] = useState<PaperEnrolPreview | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [symbolSettled, setSymbolSettled] = useState(true)

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

  const strategyVersion = useMemo(() => {
    const strategy = strategies?.find((s) => s.id === strategyId)
    return strategy ? latestVersionId(strategy) : null
  }, [strategies, strategyId])

  /** What would be sent, or why nothing can be sent yet. One place, so the preview and the add
   * button agree about what the form says. */
  const request = useMemo((): { body: EnrolPaperRequest | null; problem: string | null } => {
    if (!strategyVersion) return { body: null, problem: 'Choose a strategy first.' }
    if (!symbol.trim()) {
      return { body: null, problem: kind === 'equity' ? 'Enter a symbol, for example RELIANCE.' : 'Enter the underlying, for example NIFTY.' }
    }
    const body: EnrolPaperRequest = {
      strategy_version_id: strategyVersion,
      symbol: symbol.trim().toUpperCase(),
      instrument_kind: kind,
      builtin_strategy: signal,
      sma_window: Number(smaWindow),
      initial_capital: Number(capital),
      stop_loss_pct: Number(stopLoss || defaultStop),
    }
    if (kind === 'equity') body.position_size_pct = Number(sizePct)
    if (kind !== 'equity') body.lots = Number(lots)
    if (kind === 'option') {
      body.option_strategy = optionStrategy
      if (optionStrategy === 'vertical_spread') body.spread_width = Number(width)
    }
    const numbers = [body.sma_window, body.initial_capital, body.stop_loss_pct, body.lots ?? 1, body.spread_width ?? 1, body.position_size_pct ?? 1]
    if (numbers.some((n) => !Number.isFinite(n) || (n as number) <= 0)) {
      return { body: null, problem: 'Every number must be above zero.' }
    }
    return { body, problem: null }
  }, [strategyVersion, symbol, kind, signal, smaWindow, capital, stopLoss, defaultStop, sizePct, lots, optionStrategy, width])

  // What adding it would do, refreshed as the form changes. Only the latest answer is kept.
  useEffect(() => {
    if (!request.body || !symbolSettled) {
      setPreview(null)
      setPreviewError(null)
      setPreviewing(false)
      return
    }
    let cancelled = false
    setPreviewing(true)
    const timer = setTimeout(() => {
      previewPaperEnrolment(request.body as EnrolPaperRequest)
        .then((result) => {
          if (cancelled) return
          setPreview(result)
          setPreviewError(null)
        })
        .catch((err) => {
          if (!cancelled) setPreviewError(messageOf(err, 'the preview is unavailable'))
        })
        .finally(() => {
          if (!cancelled) setPreviewing(false)
        })
    }, PREVIEW_DEBOUNCE_MS)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [request, symbolSettled])

  const blocked = !previewing && preview !== null && preview.problems.length > 0

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!request.body) return setError(request.problem)
    setBusy(true)
    setError(null)
    try {
      const created = await enrolPaperSubscription(request.body)
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
      wide
    >
      <form className="ui-form" onSubmit={submit}>
        <Field label="Strategy" hint={strategies?.length === 0 ? 'No strategy has a version yet. Create one on the Strategies page.' : undefined}>
          {(id) => (
            <select id={id} className="ui-input" value={strategyId} onChange={(e) => setStrategyId(e.target.value)} disabled={!strategies?.length}>
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

        <div className="ui-row">
          <Field label={kind === 'equity' ? 'Symbol' : 'Underlying'}>
            {(id) => <SymbolPicker id={id} value={symbol} onChange={setSymbol} kindHint={kind === 'equity' ? 'equity' : 'underlying'} onSettled={setSymbolSettled} />}
          </Field>
          {kind !== 'equity' ? (
            <Field label="Lots" hint="Whole lots per entry">
              {(id) => <input id={id} className="ui-input" type="number" min={1} max={1000} step={1} value={lots} onChange={(e) => setLots(e.target.value)} />}
            </Field>
          ) : (
            <Field label="Size per entry (%)" hint="Share of capital one entry may use">
              {(id) => <input id={id} className="ui-input" type="number" min={0.1} max={100} step="any" value={sizePct} onChange={(e) => setSizePct(e.target.value)} />}
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
                {(id) => <input id={id} className="ui-input" type="number" min={1} max={20} step={1} value={width} onChange={(e) => setWidth(e.target.value)} />}
              </Field>
            )}
          </>
        )}

        <div className="ui-row">
          <Field label="Capital (₹)">
            {(id) => <input id={id} className="ui-input" type="number" min={1} step="any" value={capital} onChange={(e) => setCapital(e.target.value)} />}
          </Field>
          <Field label="Stop-loss (%)" hint={stopHint}>
            {(id) => <input id={id} className="ui-input" type="number" min={0.1} step="any" placeholder={defaultStop} value={stopLoss} onChange={(e) => setStopLoss(e.target.value)} />}
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
            {(id) => <input id={id} className="ui-input" type="number" min={2} step={1} value={smaWindow} onChange={(e) => setSmaWindow(e.target.value)} />}
          </Field>
        )}

        <EnrolPreviewPanel preview={preview} loading={previewing} error={previewError} />

        {error && <div className="ui-error" role="alert">{error}</div>}
        <div className="ui-modal-foot">
          <button type="button" className="button-secondary" onClick={onClose} disabled={busy}>Cancel</button>
          <button type="submit" className="button-primary" disabled={busy || !strategies?.length || blocked}>
            {busy ? 'Adding…' : 'Add subscription'}
          </button>
        </div>
      </form>
    </Modal>
  )
}
