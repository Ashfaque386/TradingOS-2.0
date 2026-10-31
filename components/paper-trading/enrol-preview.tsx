'use client'

import { Check, CircleAlert, CircleHelp, X } from 'lucide-react'
import type { PaperEnrolPreview, PaperOptionScenario } from '@/lib/api'
import { formatExpiry, formatInr } from '@/lib/paper-trading'

/** How much of the account something commits against what a paper position may use: a bar, with
 * the limit marked. Red once it is over. */
export function FitMeter({ value, limit, label }: { value: number; limit: number; label: string }) {
  const scale = Math.max(value, limit) * 1.12 || 1
  const over = value > limit
  return (
    <div
      className={`pt-meter ${over ? 'pt-meter-over' : 'pt-meter-ok'}`}
      role="img"
      aria-label={`${label}: ${formatInr(value)} against a limit of ${formatInr(limit)}${over ? ', over the limit' : ''}`}
    >
      <div className="pt-meter-track">
        <i style={{ width: `${(value / scale) * 100}%` }} />
        <b style={{ left: `${(limit / scale) * 100}%` }} />
      </div>
      <div className="pt-meter-legend" aria-hidden="true">
        <span>{formatInr(value)}</span>
        <span>limit {formatInr(limit)}</span>
      </div>
    </div>
  )
}

function Listing({ listing }: { listing: PaperEnrolPreview['listing'] }) {
  if (listing === 'listed') return <span className="pt-chip pt-chip-ok"><Check className="size-3" aria-hidden="true" />Listed by your broker</span>
  if (listing === 'not_listed') return <span className="pt-chip pt-chip-crit"><X className="size-3" aria-hidden="true" />Not listed</span>
  return <span className="pt-chip pt-chip-warn"><CircleHelp className="size-3" aria-hidden="true" />Can't be checked: no broker</span>
}

function Scenario({ scenario, limit }: { scenario: PaperOptionScenario; limit: number }) {
  return (
    <div className="pt-scenario">
      <header>
        <strong>On a {scenario.signal === 'BUY' ? 'buy' : 'sell'} signal</strong>
        <span className="pt-muted">{scenario.view}</span>
      </header>
      {scenario.legs.length === 0 ? (
        <p className="pt-muted">{scenario.note}</p>
      ) : (
        <>
          <ul>
            {scenario.legs.map((leg) => (
              <li key={leg.symbol}>
                <span>
                  <span className={`pt-chip ${leg.side === 'sell' ? 'pt-chip-warn' : 'pt-chip-ok'}`}>{leg.side === 'sell' ? 'Sell' : 'Buy'}</span>{' '}
                  {leg.quantity} × {leg.strike} {leg.option_type}
                </span>
                <span className="pt-num">{leg.price === null ? 'no price' : formatInr(leg.price, { decimals: 2 })}</span>
              </li>
            ))}
          </ul>
          {scenario.max_loss === null ? (
            <p className="pt-muted">{scenario.note}</p>
          ) : (
            <>
              <p>
                The most it can lose: <b className="pt-num">{formatInr(Math.max(scenario.max_loss, 0))}</b>
                {scenario.max_loss < 0 && <span className="pt-muted"> (the credit exceeds the width)</span>}
              </p>
              <FitMeter value={Math.max(scenario.max_loss, 0)} limit={limit} label="Largest possible loss" />
            </>
          )}
        </>
      )}
    </div>
  )
}

/** What adding this subscription would do, before it is added: from the broker's own lists and
 * the engine's own sizing and margin rules. An estimate, labelled as one. */
export function EnrolPreviewPanel({
  preview,
  loading,
  error,
}: {
  preview: PaperEnrolPreview | null
  loading: boolean
  error: string | null
}) {
  if (error) return <div className="ui-error" role="alert">Couldn't check this: {error}</div>
  if (preview === null) {
    return loading ? <div className="pt-preview pt-preview-idle" aria-busy="true">Checking…</div> : null
  }
  const { equity, future, option } = preview
  return (
    <section className="pt-preview" aria-label="What adding this would do" aria-busy={loading}>
      <header>
        <h3>What this would do</h3>
        <Listing listing={preview.listing} />
      </header>

      {preview.price !== null && preview.price_symbol && (
        <p className="pt-muted">
          {preview.price_symbol} last traded at <b className="pt-num">{formatInr(preview.price, { decimals: 2 })}</b>
        </p>
      )}

      {equity && (
        <p>
          {equity.shares === null ? (
            <>Each entry uses {formatInr(equity.entry_capital)} of capital.</>
          ) : (
            <>
              Each entry buys <b>{equity.shares} {equity.shares === 1 ? 'share' : 'shares'}</b> for{' '}
              <b className="pt-num">{formatInr(equity.notional)}</b>.
            </>
          )}
        </p>
      )}

      {future && (
        <>
          <p>
            Trades <b>{future.contract}</b>, expiring {formatExpiry(future.expiry)} and rolled to the next month on {formatExpiry(future.rolls_on)}.
          </p>
          <p>
            <b>{future.lots} {future.lots === 1 ? 'lot' : 'lots'}</b> × {future.lot_size} = <b>{future.quantity} units</b>
            {future.notional !== null && <> · <span className="pt-num">{formatInr(future.notional)}</span> of exposure</>}
          </p>
          {future.margin !== null ? (
            <>
              <FitMeter value={future.margin} limit={future.margin_limit} label="Estimated margin" />
              <p className="pt-muted">
                Margin is an estimate ({future.margin_pct}% of exposure), not the exchange's SPAN figure.
              </p>
            </>
          ) : null}
        </>
      )}

      {option && (
        <>
          <p>
            Trades the {option.strategy === 'vertical_spread' ? 'credit spread' : 'long option'} expiring{' '}
            <b>{formatExpiry(option.expiry)}</b>
            {option.lot_size !== null && <>, {option.lots} {option.lots === 1 ? 'lot' : 'lots'} × {option.lot_size}</>}.
          </p>
          <div className="pt-scenarios">
            {preview.scenarios.map((scenario) => (
              <Scenario key={scenario.signal} scenario={scenario} limit={option.limit} />
            ))}
          </div>
        </>
      )}

      {preview.problems.length > 0 && (
        <ul className="pt-notes pt-notes-problem" role="alert">
          {preview.problems.map((text) => (
            <li key={text}><X className="size-3.5" aria-hidden="true" />{text}</li>
          ))}
        </ul>
      )}
      {preview.warnings.length > 0 && (
        <ul className="pt-notes pt-notes-warning">
          {preview.warnings.map((text) => (
            <li key={text}><CircleAlert className="size-3.5" aria-hidden="true" />{text}</li>
          ))}
        </ul>
      )}
    </section>
  )
}
