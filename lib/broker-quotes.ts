// What a broker's status says about quotes that were answered and not acted on. Pure: no React, no
// network, no clock (e2e/broker-quotes-logic.spec.ts).

import type { BrokerCredentialStatus } from './api'

/** `2026-11-02T04:01:02Z` -> `09:31 IST`. */
function istTime(iso: string): string {
  const time = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hour12: false }).format(
    new Date(iso),
  )
  return `${time} IST`
}

/** One line about the quotes this broker answered that were refused (their exchange stamp was from a
 * previous day, or older than the limit), or `null` when none were. */
export function describeQuoteRefusals(
  status: Pick<
    BrokerCredentialStatus,
    'quotes_refused_count' | 'quotes_refused_last_symbol' | 'quotes_refused_last_reason' | 'quotes_refused_last_at'
  >,
): string | null {
  const count = status.quotes_refused_count ?? 0
  if (count <= 0) return null
  const noun = count === 1 ? 'quote' : 'quotes'
  const latest = [
    status.quotes_refused_last_symbol,
    status.quotes_refused_last_at ? `at ${istTime(status.quotes_refused_last_at)}` : null,
  ]
    .filter(Boolean)
    .join(' ')
  const why = status.quotes_refused_last_reason ? `: ${status.quotes_refused_last_reason}` : ''
  return `${count} ${noun} refused since the credentials last changed${latest ? `, the latest ${latest}` : ''}${why}. Nothing trades on a refused quote.`
}
