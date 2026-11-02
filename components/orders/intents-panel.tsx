'use client'

import { useState } from 'react'
import { Check, Clock3, Timer, X } from 'lucide-react'
import { Pager } from '@/components/ui/pager'
import { ErrorNotice } from '@/components/ui/error-notice'
import { type Strategy, listLiveIntents } from '@/lib/api'
import { INTENT_STATUSES, statusLabel, statusTone } from '@/lib/orders'
import { indiaStamp } from '@/lib/time'
import { useQuery } from '@/lib/use-query'
import { Panel } from './panel'

const SIZE = 25
const LIVE_REFRESH_MS = 10_000

/** What the safety layer did with every order a live strategy tried to place: let it through or blocked it, and why not. */
export function IntentsPanel({ strategies }: { strategies: Strategy[] }) {
  const [status, setStatus] = useState('')
  const [strategyId, setStrategyId] = useState('')
  const [page, setPage] = useState(1)

  const query = { statusFilter: status, strategyId, limit: SIZE, offset: (page - 1) * SIZE }
  const { data, error, loading, refresh } = useQuery(JSON.stringify(query), () => listLiveIntents(query), {
    intervalMs: page === 1 ? LIVE_REFRESH_MS : undefined,
  })
  const intents = data?.items ?? []
  const names = new Map(strategies.map((s) => [s.id, s.name]))

  return (
    <Panel title="Live Order Intents" eyebrow="FULL HISTORY · AUTONOMOUS SAFETY-LAYER TRAIL">
      <div className="orders-filters">
        <select aria-label="Intent status" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1) }}>
          <option value="">All statuses</option>
          {INTENT_STATUSES.map((s) => (
            <option key={s} value={s}>
              {statusLabel(s)}
            </option>
          ))}
        </select>
        <select aria-label="Intent strategy" value={strategyId} onChange={(e) => { setStrategyId(e.target.value); setPage(1) }}>
          <option value="">All strategies</option>
          {strategies.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
      </div>
      {error && <ErrorNotice className="m-3" message={`Could not load the intents: ${error}`} onRetry={() => void refresh()} />}
      <div className="intent-history" aria-busy={loading}>
        {intents.map((intent) => {
          const tone = statusTone(intent.status)
          return (
            <div className="intent-row" key={intent.id}>
              <div className={`intent-ring ring-${intent.status.toLowerCase()}`}>
                <Timer className="size-4" />
              </div>
              <div className="intent-info">
                <strong>{intent.symbol}</strong>
                <span>
                  {intent.side.toUpperCase()} · {intent.quantity} qty · {intent.intent_type}
                </span>
                <small>
                  {names.get(intent.strategy_id) ?? intent.strategy_id.slice(0, 8)} · {indiaStamp(intent.generated_at)}
                </small>
              </div>
              <span className={`outcome outcome-${intent.status.toLowerCase()}`} data-tone={tone}>
                {tone === 'good' ? <Check className="size-3" /> : tone === 'bad' ? <X className="size-3" /> : <Clock3 className="size-3" />}
                {statusLabel(intent.status).toUpperCase()}
              </span>
            </div>
          )
        })}
        {intents.length === 0 && (
          <p className="p-4 text-xs text-muted-foreground">
            {loading ? 'Loading the intents…' : status || strategyId ? 'No intents match these filters.' : 'No live order intents recorded.'}
          </p>
        )}
      </div>
      <Pager
        total={data?.total ?? 0}
        page={page}
        size={SIZE}
        noun="intents"
        onPage={setPage}
        note={page === 1 ? 'Refreshes every 10 seconds' : 'Not refreshing on this page, so the rows stay put'}
      />
    </Panel>
  )
}
