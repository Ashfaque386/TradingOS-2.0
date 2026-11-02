'use client'

import { type FormEvent, useState } from 'react'
import { AlertTriangle, ShieldCheck } from 'lucide-react'
import { type OrderIntentResult, submitOrderIntent } from '@/lib/api'
import { messageOf } from '@/lib/errors'
import { Panel } from './panel'

/** A dry run: the risk gate judges an order like this one and nothing is sent anywhere. */
export function PreTradeCheck() {
  const [live, setLive] = useState(false)
  // No pre-filled numbers: a default portfolio value or price would be checked as if it were real.
  const [symbol, setSymbol] = useState('')
  const [side, setSide] = useState<'buy' | 'sell'>('buy')
  const [qty, setQty] = useState('')
  const [price, setPrice] = useState('')
  const [referencePrice, setReferencePrice] = useState('')
  const [portfolioValue, setPortfolioValue] = useState('')
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [checkResult, setCheckResult] = useState<OrderIntentResult | null>(null)
  const [submitting, setSubmitting] = useState(false)

  // Is every number the check needs a real, positive one?
  const inputsReady = symbol.trim() !== '' && [qty, price, referencePrice, portfolioValue].every((v) => Number(v) > 0)

  async function runCheck(event: FormEvent) {
    event.preventDefault()
    if (!inputsReady) return
    setSubmitting(true)
    setSubmitError(null)
    setCheckResult(null)
    try {
      const proposedPrice = Number(price)
      const quantity = Number(qty)
      setCheckResult(
        await submitOrderIntent({
          mode: live ? 'live' : 'paper',
          symbol: symbol.trim(),
          side,
          quantity,
          proposed_price: proposedPrice,
          reference_price: Number(referencePrice),
          proposed_position_value: proposedPrice * quantity,
          portfolio_value: Number(portfolioValue),
        }),
      )
    } catch (err) {
      setSubmitError(messageOf(err, 'The pre-trade check could not be run'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Panel title="Pre-trade check" eyebrow="DRY RUN · NOTHING IS SENT">
      <form onSubmit={runCheck}>
        <p className="px-4 pt-3 text-xs text-muted-foreground">
          Asks the real risk gate (kill switch, compliance, naked options, correlation) whether an order like this would be allowed. It places no order and sends nothing to a broker; live orders come only from a strategy with autonomy on.
        </p>
        <div className="mode-toggle">
          <button type="button" className={!live ? 'selected-paper' : ''} onClick={() => setLive(false)}>
            PAPER<small>paper-mode checks</small>
          </button>
          <button type="button" className={live ? 'selected-live' : ''} onClick={() => setLive(true)}>
            LIVE<small>live-mode checks</small>
          </button>
        </div>
        {submitError && (
          <div className="live-warning" role="alert">
            <AlertTriangle className="size-4" />
            <span>
              <strong>Would be refused</strong>
              <small>{submitError}</small>
            </span>
          </div>
        )}
        {checkResult && (
          <div className="readiness-banner" role="status">
            <ShieldCheck className="size-4" />
            <span>
              <strong>Would be allowed</strong>
              <small>
                {checkResult.side.toUpperCase()} {checkResult.quantity} {checkResult.symbol} at ₹{checkResult.price.toFixed(2)} passes every {checkResult.mode} pre-trade check. Nothing was sent.
              </small>
            </span>
          </div>
        )}
        <label className="order-field">
          SYMBOL
          <input value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} />
        </label>
        <div className="field-grid">
          <label className="order-field">
            SIDE
            <select value={side} onChange={(e) => setSide(e.target.value as 'buy' | 'sell')}>
              <option value="buy">BUY</option>
              <option value="sell">SELL</option>
            </select>
          </label>
          <label className="order-field">
            QUANTITY
            <input type="number" min="1" value={qty} onChange={(e) => setQty(e.target.value)} />
          </label>
        </div>
        <div className="field-grid">
          <label className="order-field">
            PROPOSED PRICE
            <input type="number" min="0" step="any" value={price} onChange={(e) => setPrice(e.target.value)} />
          </label>
          <label className="order-field">
            REFERENCE PRICE
            <input type="number" min="0" step="any" value={referencePrice} onChange={(e) => setReferencePrice(e.target.value)} />
          </label>
        </div>
        <label className="order-field">
          PORTFOLIO VALUE
          <input type="number" min="0" step="any" value={portfolioValue} onChange={(e) => setPortfolioValue(e.target.value)} />
        </label>
        <p className="px-4 text-[11px] text-muted-foreground">Fill every field with a real figure: the position limit and the circuit band are judged against them.</p>
        <button type="submit" disabled={submitting || !inputsReady} className="submit-paper">
          <ShieldCheck className="size-4" />
          Run pre-trade check
        </button>
      </form>
    </Panel>
  )
}
