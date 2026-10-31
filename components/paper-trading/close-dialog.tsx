'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import {
  type PaperClosePreview,
  type PaperSubscription,
  closePaperPosition,
  previewPaperClose,
} from '@/lib/api'
import {
  closeConfirmationPhrase,
  confirmationMatches,
  describeContract,
  formatAge,
  formatInr,
  subscriptionName,
  tone,
} from '@/lib/paper-trading'
import { Field } from '@/components/ui/field'
import { messageOf } from './helpers'
import { Modal } from '@/components/ui/modal'

/** What closing would do right now, leg by leg, from the same price rule and order book the close
 * itself uses. An estimate: the price moves, and the risk gate judges the order when it is placed. */
function Estimate({ preview, now }: { preview: PaperClosePreview; now: Date }) {
  return (
    <div className="pt-estimate">
      <div className="table-scroll">
        <table className="pt-table">
          <thead>
            <tr>
              <th>Order</th><th className="pt-r">Price used</th><th className="pt-r">Book</th>
              <th className="pt-r">Estimated fill</th><th className="pt-r">Costs</th><th className="pt-r">Result</th>
            </tr>
          </thead>
          <tbody>
            {preview.legs.map((leg) => (
              <tr key={leg.symbol} data-problem={leg.problem !== null}>
                <td>
                  <span className={`pt-chip ${leg.side === 'buy' ? 'pt-chip-warn' : 'pt-chip-ok'}`}>{leg.side === 'buy' ? 'Buy back' : 'Sell'}</span>{' '}
                  {leg.quantity} {describeContract(leg.symbol, now)}
                </td>
                {leg.problem !== null && leg.est_filled === 0 ? (
                  <td colSpan={5} className="pt-leg-problem">{leg.problem}</td>
                ) : (
                  <>
                    <td className="pt-r pt-num">
                      {leg.reference_price === null ? '—' : formatInr(leg.reference_price, { decimals: 2 })}
                      <small>{leg.reference_age_seconds === null ? '' : formatAge(leg.reference_age_seconds)}{leg.reference_source ? ` · ${leg.reference_source}` : ''}</small>
                    </td>
                    <td className="pt-r pt-num">
                      {leg.book_age_seconds === null ? '—' : formatAge(leg.book_age_seconds)}
                      {leg.book_source !== null && leg.book_source !== 'real' && <small className="pt-leg-partial">generated book</small>}
                    </td>
                    <td className="pt-r pt-num">
                      {leg.est_avg_price === null ? '—' : formatInr(leg.est_avg_price, { decimals: 2 })}
                      <small className={leg.est_remaining > 0 ? 'pt-leg-partial' : ''}>
                        {leg.est_remaining > 0 ? `only ${leg.est_filled} of ${leg.quantity} fill` : `${leg.est_filled} filled`}
                      </small>
                    </td>
                    <td className="pt-r pt-num">{formatInr(leg.est_costs, { decimals: 2 })}</td>
                    <td className={`pt-r pt-num pt-${tone(leg.est_gross_pnl)}`}>{formatInr(leg.est_gross_pnl, { signed: true, decimals: 2 })}</td>
                  </>
                )}
              </tr>
            ))}
          </tbody>
          {preview.est_costs_total !== null && (
            <tfoot>
              <tr>
                <td colSpan={4} className="pt-r pt-muted">Estimated total</td>
                <td className="pt-r pt-num">{formatInr(preview.est_costs_total, { decimals: 2 })}</td>
                <td className={`pt-r pt-num pt-${tone(preview.est_gross_pnl)}`}>{formatInr(preview.est_gross_pnl, { signed: true, decimals: 2 })}</td>
              </tr>
            </tfoot>
          )}
        </table>
      </div>
      {preview.est_costs_total !== null && preview.est_gross_pnl !== null && (
        <p className="pt-estimate-net">
          After costs, about{' '}
          <b className={`pt-num pt-${tone(preview.est_gross_pnl - preview.est_costs_total)}`}>
            {formatInr(preview.est_gross_pnl - preview.est_costs_total, { signed: true, decimals: 2 })}
          </b>{' '}
          on this close.
        </p>
      )}
    </div>
  )
}

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
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [preview, setPreview] = useState<PaperClosePreview | null>(null)
  const [loading, setLoading] = useState(true)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [refreshes, setRefreshes] = useState(0)
  const [now, setNow] = useState(() => new Date())

  const single = sub.open_legs === 1 // one price cannot stand for the legs of a spread
  const supplied = single && price.trim() !== ''
  const suppliedValue = supplied ? Number(price) : undefined
  const priceValid = suppliedValue === undefined || suppliedValue > 0
  const phrase = closeConfirmationPhrase(sub.symbol, administrative)

  // The estimate: on open, whenever a supplied price changes, and on request. Only the latest
  // answer is kept.
  const previewPrice = priceValid ? suppliedValue : undefined
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    const timer = setTimeout(() => {
      previewPaperClose(sub.id, previewPrice)
        .then((result) => {
          if (cancelled) return
          setPreview(result)
          setPreviewError(null)
          setNow(new Date())
        })
        .catch((err) => {
          if (!cancelled) setPreviewError(messageOf(err, 'the estimate is unavailable'))
        })
        .finally(() => {
          if (!cancelled) setLoading(false)
        })
    }, 300)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [sub.id, previewPrice, refreshes])

  const refresh = useCallback(() => setRefreshes((n) => n + 1), [])

  // Whether the button may be pressed, and if not, what is missing: said under the button.
  const missing = useMemo((): string | null => {
    if (!confirmationMatches(typed, phrase)) return `Type ${phrase} to confirm.`
    if (reason.trim().length < 3) return 'Give a reason of at least 3 characters.'
    if (!priceValid) return 'The price must be above zero.'
    if (administrative) return supplied ? null : 'An administrative close needs the price to close at.'
    if (loading) return 'Checking what the close would do…'
    if (preview !== null && !preview.can_close) {
      return preview.blockers[0] ?? 'It cannot close right now: see the estimate above.'
    }
    return null
  }, [typed, phrase, reason, priceValid, administrative, supplied, loading, preview])

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (missing) return setError(missing)
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
      wide
    >
      <form className="ui-form" onSubmit={submit}>
        {!administrative && (
          <section aria-label="What closing would do" aria-busy={loading}>
            <div className="pt-estimate-head">
              <h3>What closing would do</h3>
              <button type="button" className="button-secondary" onClick={refresh} disabled={loading} aria-label="Refresh the estimate">
                <RefreshCw className={`size-3 ${loading ? 'pt-spin' : ''}`} aria-hidden="true" />Refresh
              </button>
            </div>
            {previewError && <div className="ui-error" role="alert">Couldn't estimate this: {previewError}</div>}
            {preview && preview.blockers.length > 0 && (
              <ul className="pt-notes pt-notes-problem" role="alert">
                {preview.blockers.map((text) => <li key={text}>{text}</li>)}
              </ul>
            )}
            {preview && preview.legs.length > 0 && <Estimate preview={preview} now={now} />}
            {preview && preview.notes.length > 0 && (
              <ul className="pt-notes">
                {preview.notes.map((text) => <li key={text}>{text}</li>)}
              </ul>
            )}
            {!preview && loading && <p className="pt-muted">Checking the price and the order book…</p>}
          </section>
        )}

        <Field label="Reason" hint="Recorded in the audit log.">
          {(id) => <input id={id} className="ui-input" value={reason} onChange={(e) => setReason(e.target.value)} autoComplete="off" />}
        </Field>
        {single && (
          <Field label="Price (₹), when there is no fresh real price" hint="Leave empty to use a real price under two minutes old. With the market shut there is none, so enter one; it is recorded as supplied by you.">
            {(id) => <input id={id} className="ui-input" type="number" min={0} step="any" value={price} onChange={(e) => setPrice(e.target.value)} />}
          </Field>
        )}
        {single && isAdmin && (
          <label className="ui-radio" data-on={administrative}>
            <input type="checkbox" checked={administrative} onChange={(e) => { setAdministrative(e.target.checked); setTyped('') }} />
            <div><strong>Administrative close</strong><span>For clearing stale or demo positions: skips the risk gate and fills the whole quantity at the price above, with no costs. Audited as administrative.</span></div>
          </label>
        )}
        {administrative && supplied && preview?.legs[0] && (
          <p className="pt-estimate-net">
            Fills all {preview.legs[0].quantity} at {formatInr(Number(price), { decimals: 2 })}, no costs: about{' '}
            <b className={`pt-num pt-${tone((Number(price) - preview.legs[0].avg_cost) * (preview.legs[0].side === 'sell' ? 1 : -1))}`}>
              {formatInr((Number(price) - preview.legs[0].avg_cost) * (preview.legs[0].side === 'sell' ? 1 : -1) * preview.legs[0].quantity, { signed: true, decimals: 2 })}
            </b>.
          </p>
        )}

        <Field label={`Type ${phrase} to confirm`}>
          {(id) => <input id={id} className="ui-input ui-confirm" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" spellCheck={false} aria-describedby="pt-close-missing" />}
        </Field>

        {error && <div className="ui-error" role="alert">{error}</div>}
        <div className="ui-modal-foot">
          {missing && <small id="pt-close-missing" className="ui-foot-hint">{missing}</small>}
          <button type="button" className="button-secondary" onClick={onClose} disabled={busy}>Cancel</button>
          <button type="submit" className="button-danger" disabled={busy || missing !== null}>{busy ? 'Closing…' : 'Close position'}</button>
        </div>
      </form>
    </Modal>
  )
}
