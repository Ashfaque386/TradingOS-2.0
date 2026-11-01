'use client'

import { useEffect, useMemo, useState } from 'react'
import { Plus, RefreshCw } from 'lucide-react'
import { useAuth } from '@/components/auth/auth-provider'
import { Attention } from '@/components/paper-trading/attention'
import { CloseDialog, DeactivateDialog, EnrolDialog, SettingsDialog, activate } from '@/components/paper-trading/dialogs'
import { EngineAlertsPanel } from '@/components/paper-trading/engine-alerts'
import { EquityPanel } from '@/components/equity/equity-panel'
import { Ledger } from '@/components/paper-trading/ledger'
import { Lane } from '@/components/paper-trading/lane'
import {
  type PaperSubscription,
  type TodaysPaperPnl,
  type UnrealizedPaperPnl,
  getTodaysPaperPnl,
  getUnrealizedPaperPnl,
  listPaperSubscriptions,
} from '@/lib/api'
import { type AttentionItem, attentionItems } from '@/lib/paper-trading'
import { can } from '@/lib/permissions'
import { useConsoleState } from '@/components/shell/console-state'
import { usePolling } from '@/lib/use-polling'

type Filter = 'all' | 'active' | 'attention' | 'off'
const FILTERS: { id: Filter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'active', label: 'Running' },
  { id: 'attention', label: 'Needs attention' },
  { id: 'off', label: 'Off' },
]

const POLL_OPEN_MS = 5_000
const POLL_SHUT_MS = 30_000

type Dialog =
  | { kind: 'enrol' }
  | { kind: 'deactivate'; sub: PaperSubscription }
  | { kind: 'close'; sub: PaperSubscription }
  | { kind: 'settings'; sub: PaperSubscription }
  | null

export default function PaperTradingPage() {
  const { role } = useAuth()
  const canManage = can(role, 'POST /api/v1/paper-trading/subscriptions')
  const isAdmin = role === 'SystemAdministrator'

  const [notice, setNotice] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [filter, setFilter] = useState<Filter>('all')
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [dialog, setDialog] = useState<Dialog>(null)

  const { marketHours } = useConsoleState()
  const marketOpen = marketHours?.is_open ?? false

  // Poll quickly while the market is open (prices move), slowly when it is shut.
  const {
    data: desk,
    error: loadError,
    lastUpdated: updatedAt,
    refresh: load,
  } = usePolling(
    async () => {
      const [subs, today, unrealized] = await Promise.all([
        listPaperSubscriptions(),
        getTodaysPaperPnl(),
        getUnrealizedPaperPnl(),
      ])
      return { subs, today, unrealized }
    },
    { intervalMs: marketOpen ? POLL_OPEN_MS : POLL_SHUT_MS },
  )
  const subs: PaperSubscription[] | null = desk?.subs ?? null
  const today: TodaysPaperPnl | null = desk?.today ?? null
  const unrealized: UnrealizedPaperPnl | null = desk?.unrealized ?? null
  const error = actionError ?? loadError
  const now = useMemo(() => updatedAt ?? new Date(), [updatedAt])
  const refreshKey = updatedAt?.getTime() ?? 0

  useEffect(() => {
    setActionError(null) // a fresh read supersedes the last action's complaint
  }, [updatedAt])

  const items = useMemo(() => attentionItems(subs ?? [], marketOpen), [subs, marketOpen])
  const attentionIds = useMemo(() => new Set(items.map((item) => item.subscriptionId)), [items])
  const openCount = useMemo(() => (subs ?? []).reduce((sum, s) => sum + s.open_legs, 0), [subs])

  const visible = useMemo(() => {
    const all = subs ?? []
    switch (filter) {
      case 'active': return all.filter((s) => s.is_active)
      case 'off': return all.filter((s) => !s.is_active)
      case 'attention': return all.filter((s) => attentionIds.has(s.id))
      default: return all
    }
  }, [subs, filter, attentionIds])

  const toggle = (id: string) =>
    setExpanded((current) => {
      const next = new Set(current)
      if (!next.delete(id)) next.add(id)
      return next
    })

  const afterChange = (message: string) => {
    setDialog(null)
    setNotice(message)
    void load()
  }

  const onAttentionAction = (item: AttentionItem) => {
    const sub = subs?.find((s) => s.id === item.subscriptionId)
    if (sub && item.action === 'close') setDialog({ kind: 'close', sub })
  }

  // From an engine alert: open the subscription it is about and bring it into view.
  const showSubscription = (id: string) => {
    setFilter('all')
    setExpanded((current) => new Set(current).add(id))
    window.setTimeout(() => document.getElementById(`lane-${id}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' }), 50)
  }

  const switchOn = async (sub: PaperSubscription) => {
    const problem = await activate(sub)
    if (problem) setActionError(problem)
    else setNotice(`${sub.symbol} is on again.`)
    void load()
  }

  return (
    <>
      <div className="pt-page">
        <header className="fleet-page-header">
          <div>
            <p className="eyebrow">TRADINGOS // PAPER TRADING</p>
            <h1>Paper desk</h1>
            <p>Strategies trading on real prices with real costs. Nothing here is simulated data: with no real feed, a subscription halts and tells you.</p>
          </div>
          {canManage && (
            <button type="button" className="button-primary" onClick={() => setDialog({ kind: 'enrol' })}>
              <Plus className="size-3.5" aria-hidden="true" />Add subscription
            </button>
          )}
        </header>

        <Ledger today={today} unrealized={unrealized} openCount={openCount} />
        <EquityPanel mode="paper" />

        {error && <div className="ui-error" role="alert">{error}</div>}
        {notice && (
          <div className="pt-alert pt-alert-info" role="status" style={{ gridTemplateColumns: 'auto 1fr auto' }}>
            <span aria-hidden="true" />
            <div><p style={{ marginTop: 0, color: '#e2e8f0' }}>{notice}</p></div>
            <button type="button" className="button-secondary" onClick={() => setNotice(null)}>Dismiss</button>
          </div>
        )}

        <Attention items={items} canAct={canManage} onAction={onAttentionAction} />
        <EngineAlertsPanel onShowSubscription={showSubscription} />

        <div className="pt-toolbar">
          <div className="pt-filter" role="group" aria-label="Show">
            {FILTERS.map((f) => (
              <button key={f.id} type="button" aria-pressed={filter === f.id} onClick={() => setFilter(f.id)}>
                {f.label}
              </button>
            ))}
          </div>
          <div className="pt-toolbar-meta">
            <span className="pt-live" data-open={marketOpen}>{marketHours === null ? 'Market hours unknown' : marketOpen ? 'Market open' : 'Market shut'}</span>
            <span>{updatedAt ? `Updated ${updatedAt.toLocaleTimeString()}` : 'Loading…'}</span>
            <button type="button" className="button-secondary" onClick={() => void load()} aria-label="Refresh now">
              <RefreshCw className="size-3" aria-hidden="true" />
            </button>
          </div>
        </div>

        <section className="pt-lanes" aria-label="Subscriptions">
          {subs === null && !error && <p className="pt-muted">Loading subscriptions…</p>}
          {subs !== null && subs.length === 0 && (
            <div className="pt-empty">
              <h3>No paper subscriptions yet</h3>
              <p>Attach a strategy to a share, a future or an option and it starts trading on real prices.</p>
              {canManage && (
                <button type="button" className="button-primary" onClick={() => setDialog({ kind: 'enrol' })}>
                  Add the first subscription
                </button>
              )}
            </div>
          )}
          {subs !== null && subs.length > 0 && visible.length === 0 && (
            <div className="pt-empty"><h3>Nothing matches</h3><p>No subscription is in this view.</p></div>
          )}
          {visible.map((sub) => (
            <Lane
              key={sub.id}
              sub={sub}
              expanded={expanded.has(sub.id)}
              onToggle={() => toggle(sub.id)}
              canManage={canManage}
              now={now}
              refreshKey={refreshKey}
              onDeactivate={() => setDialog({ kind: 'deactivate', sub })}
              onClose={() => setDialog({ kind: 'close', sub })}
              onActivate={() => void switchOn(sub)}
              onEditSettings={() => setDialog({ kind: 'settings', sub })}
            />
          ))}
        </section>
      </div>

      {dialog?.kind === 'enrol' && (
        <EnrolDialog
          onClose={() => setDialog(null)}
          onDone={(id) => {
            setExpanded((current) => new Set(current).add(id))
            afterChange('Subscription added. It trades once real prices and enough history exist.')
          }}
        />
      )}
      {dialog?.kind === 'deactivate' && (
        <DeactivateDialog sub={dialog.sub} onClose={() => setDialog(null)} onDone={() => afterChange(`${dialog.sub.symbol} is switched off.`)} />
      )}
      {dialog?.kind === 'settings' && (
        <SettingsDialog
          sub={dialog.sub}
          onClose={() => setDialog(null)}
          onDone={(summary) => afterChange(`${dialog.sub.symbol} settings saved: ${summary}.`)}
        />
      )}
      {dialog?.kind === 'close' && (
        <CloseDialog
          sub={dialog.sub}
          isAdmin={isAdmin}
          onClose={() => setDialog(null)}
          onDone={({ legs, failures }) =>
            afterChange(
              failures.length
                ? `Closed ${legs} ${legs === 1 ? 'leg' : 'legs'} of ${dialog.sub.symbol}, but not all: ${failures.join('; ')}`
                : `Closed ${dialog.sub.symbol}.`,
            )
          }
        />
      )}
    </>
  )
}
