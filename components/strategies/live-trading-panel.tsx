'use client'

import { FormEvent, useEffect, useState } from 'react'
import { ShieldAlert, SlidersHorizontal, Zap } from 'lucide-react'
import {
  type EnrollLiveTradingInput,
  type LiveOrderIntentDto,
  type LiveTradingSubscriptionDto,
  ApiError,
  enrollLiveTrading,
  generateLiveIntent,
  listLiveSubscriptions,
  listPositionsByStrategy,
  setLiveAutonomy,
} from '@/lib/api'
import { LiveLimitsDialog, SwitchOffDialog } from '@/components/strategies/live-limits-dialog'
import { heldBy, limitLines } from '@/lib/live-limits'
import { useQuery } from '@/lib/use-query'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { ErrorNotice } from '@/components/ui/error-notice'

const ENROLL_DEFAULTS: Omit<EnrollLiveTradingInput, 'strategy_id'> = {
  symbol: '',
  broker_name: 'zerodha',
  builtin_strategy: 'sma_crossover',
  sma_window: 15,
  initial_capital: 100_000,
  stop_loss_pct: 3,
  position_size_pct: 5,
  intent_expiry_seconds: 90,
  max_intents_per_window: 5,
  rate_limit_window_minutes: 60,
  max_notional_per_intent: 50_000,
}

export function LiveTradingPanel({ strategyId, canOperate, canSignOffLive }: { strategyId: string; canOperate: boolean; canSignOffLive: boolean }) {
  // Tightening a limit and switching a subscription off are for the same two roles as the autonomy switch.
  const canManageLimits = canSignOffLive
  const [dialog, setDialog] = useState<'limits' | 'switch-off' | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const positions = useQuery('live-held', listPositionsByStrategy, { intervalMs: 15_000 })
  const [subscription, setSubscription] = useState<LiveTradingSubscriptionDto | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [enrollForm, setEnrollForm] = useState({ ...ENROLL_DEFAULTS })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmOpen, setConfirmOpen] = useState<'enable' | 'disable' | 'tick' | null>(null)
  const [tickPrice, setTickPrice] = useState('')
  const [lastIntent, setLastIntent] = useState<LiveOrderIntentDto | null>(null)

  const [loadError, setLoadError] = useState<string | null>(null)

  async function reload() {
    try {
      const subs = await listLiveSubscriptions()
      setSubscription(subs.find((s) => s.strategy_id === strategyId) ?? null)
      setLoaded(true)
      setLoadError(null)
    } catch (err) {
      setLoadError(err instanceof Error && err.message ? err.message : 'Could not load the live trading status')
    }
  }

  useEffect(() => {
    reload()
  }, [strategyId])

  async function handleEnroll(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!enrollForm.symbol.trim() || !enrollForm.broker_name.trim()) return
    setBusy(true)
    setError(null)
    try {
      await enrollLiveTrading({
        strategy_id: strategyId,
        ...enrollForm,
        symbol: enrollForm.symbol.trim().toUpperCase(),
      })
      await reload()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to enroll in live trading')
    } finally {
      setBusy(false)
    }
  }

  // Throws to keep the confirmation open with the reason; the dialog closes itself when this returns.
  async function applyAutonomy(enable: boolean) {
    if (!subscription) return
    try {
      setSubscription(await setLiveAutonomy(subscription.id, enable))
    } catch (err) {
      throw new Error(err instanceof ApiError ? err.message : 'Failed to update autonomous trading state')
    }
  }

  async function generateTick() {
    if (!subscription || !tickPrice.trim()) return
    setError(null)
    try {
      setLastIntent(await generateLiveIntent(subscription.id, Number(tickPrice)))
    } catch (err) {
      throw new Error(err instanceof ApiError ? err.message : 'Failed to generate an intent from this tick')
    }
  }

  // With autonomy on, a tick that produces a signal sends a real order, so it is confirmed like the switch is.
  async function handleGenerateTick() {
    if (!subscription || !tickPrice.trim()) return
    if (subscription.autonomous_trading_enabled) return setConfirmOpen('tick')
    setBusy(true)
    try {
      await generateTick()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to generate an intent from this tick')
    } finally {
      setBusy(false)
    }
  }

  if (!loaded) {
    return <div className="mt-6 rounded-xl border border-white/10 p-4">
      <p className="eyebrow">LIVE TRADING · AUTONOMOUS EXECUTION</p>
      {loadError
        ? <ErrorNotice className="mt-3" message={`Could not load the live trading status, so enrolling and the switches are hidden: ${loadError}`} onRetry={() => void reload()} />
        : <p className="mt-3 text-xs text-muted-foreground">Loading the live trading status…</p>}
    </div>
  }

  return <div className="mt-6 rounded-xl border border-white/10 p-4">
    <p className="eyebrow">LIVE TRADING · AUTONOMOUS EXECUTION</p>
    {loadError && <ErrorNotice className="mt-3" message={`The status below may be out of date: ${loadError}`} onRetry={() => void reload()} />}
    {!subscription && <>
      <p className="mt-2 text-xs text-muted-foreground">No live-trading subscription yet for this strategy. Enrolling wires it into the autonomous execution pipeline -- autonomy itself stays off, by default, until explicitly enabled below.</p>
      {canOperate ? <form onSubmit={handleEnroll} className="mt-3 grid grid-cols-2 gap-3 text-xs">
        <label className="flex flex-col gap-1">Symbol<input value={enrollForm.symbol} onChange={(e) => setEnrollForm({ ...enrollForm, symbol: e.target.value })} placeholder="e.g. RELIANCE" className="rounded border border-white/10 bg-black/20 px-2 py-1" /></label>
        <label className="flex flex-col gap-1">Broker<select value={enrollForm.broker_name} onChange={(e) => setEnrollForm({ ...enrollForm, broker_name: e.target.value })} className="rounded border border-white/10 bg-black/20 px-2 py-1"><option value="zerodha">Zerodha</option><option value="upstox">Upstox</option></select></label>
        <label className="flex flex-col gap-1">Max orders / window (standing cap)<input type="number" value={enrollForm.max_intents_per_window} onChange={(e) => setEnrollForm({ ...enrollForm, max_intents_per_window: Number(e.target.value) })} className="rounded border border-white/10 bg-black/20 px-2 py-1" /></label>
        <label className="flex flex-col gap-1">Window (minutes)<input type="number" value={enrollForm.rate_limit_window_minutes} onChange={(e) => setEnrollForm({ ...enrollForm, rate_limit_window_minutes: Number(e.target.value) })} className="rounded border border-white/10 bg-black/20 px-2 py-1" /></label>
        <label className="flex flex-col gap-1">Max notional / order (₹)<input type="number" value={enrollForm.max_notional_per_intent} onChange={(e) => setEnrollForm({ ...enrollForm, max_notional_per_intent: Number(e.target.value) })} className="rounded border border-white/10 bg-black/20 px-2 py-1" /></label>
        <label className="flex flex-col gap-1">Position size (% of capital)<input type="number" value={enrollForm.position_size_pct} onChange={(e) => setEnrollForm({ ...enrollForm, position_size_pct: Number(e.target.value) })} className="rounded border border-white/10 bg-black/20 px-2 py-1" /></label>
        <div className="col-span-2"><button type="submit" disabled={busy || !enrollForm.symbol.trim()} className="button-secondary">Enroll in live trading</button></div>
      </form> : <p className="mt-3 text-xs text-muted-foreground">Your role can view live-trading state but not enroll a subscription.</p>}
    </>}
    {subscription && <>
      <div className="danger-banner mt-3">
        {subscription.autonomous_trading_enabled ? <Zap className="size-4 text-emerald-300" /> : <ShieldAlert className="size-4" />}
        <div>
          <strong className={subscription.autonomous_trading_enabled ? 'text-emerald-300' : ''}>{subscription.autonomous_trading_enabled ? 'LIVE AUTONOMOUS TRADING IS ON' : 'Autonomous trading is OFF'}</strong>
          <small>{subscription.symbol} via {subscription.broker_name} · {subscription.autonomous_trading_enabled ? `enabled by ${subscription.autonomy_enabled_by ?? 'unknown'}${subscription.autonomy_enabled_at ? ` at ${new Date(subscription.autonomy_enabled_at).toLocaleString()}` : ''}` : 'no order can be generated for this strategy until this is explicitly enabled'}</small>
        </div>
      </div>
      {!subscription.is_active && <p className="mt-3 rounded-lg border border-white/10 bg-white/[.03] p-3 text-xs text-muted-foreground" role="status"><strong className="text-foreground">Switched off.</strong> {subscription.symbol} no longer trades, and autonomous trading for it ended. To trade it again, enrol the strategy again.</p>}
      <div className="mt-3 text-[11px] text-muted-foreground" aria-label="Standing limits">
        <p className="eyebrow">STANDING LIMITS</p>
        <ul className="mt-1 grid gap-0.5">{limitLines(subscription).map((line) => <li key={line}>{line}</li>)}</ul>
        {canManageLimits && subscription.is_active && <div className="mt-2 flex flex-wrap gap-2">
          <button type="button" className="button-secondary" onClick={() => setDialog('limits')}><SlidersHorizontal className="size-3" />Tighten limits</button>
          <button type="button" className="button-secondary" onClick={() => setDialog('switch-off')}>Switch off this subscription</button>
        </div>}
        {notice && <p className="mt-2 text-emerald-300" role="status">{notice}</p>}
      </div>
      {canOperate && <div className="mt-4 rounded-lg border border-white/10 p-3">
        <p className="eyebrow">SIMULATE A TICK · OPS VISIBILITY, NEVER REQUIRED</p>
        <p className="mt-1 text-[11px] text-muted-foreground">Runs the same step the scheduler runs on its own. <strong className="text-foreground">{subscription.autonomous_trading_enabled ? 'Autonomy is on, so a signal from this tick sends a real order to the broker.' : 'Autonomy is off, so this generates and sends nothing.'}</strong></p>
        <div className="mt-2 flex gap-2"><input type="number" value={tickPrice} onChange={(e) => setTickPrice(e.target.value)} placeholder="tick price" className="min-w-0 flex-1 rounded border border-white/10 bg-black/20 px-2 py-1 text-xs" /><button disabled={busy || !tickPrice.trim()} onClick={handleGenerateTick} className="button-secondary">Generate intent</button></div>
        {lastIntent === null && <p className="mt-2 text-[11px] text-muted-foreground">No intent yet this session.{!subscription.autonomous_trading_enabled && ' Autonomy is off, so a tick generates nothing.'}</p>}
        {lastIntent && <p className="mt-2 text-[11px] text-muted-foreground">Last result: <strong className="text-foreground">{lastIntent.side.toUpperCase()} {lastIntent.quantity} {lastIntent.symbol}</strong> · status <strong className="text-foreground">{lastIntent.status.toUpperCase()}</strong></p>}
      </div>}
      {canSignOffLive && subscription.is_active ? <div className="mt-3">
        {subscription.autonomous_trading_enabled
          ? <button disabled={busy} onClick={() => setConfirmOpen('disable')} className="button-danger">Disable autonomous trading</button>
          : <button disabled={busy} onClick={() => setConfirmOpen('enable')} className="button-primary"><Zap className="size-3" />Enable autonomous trading</button>}
      </div> : !canSignOffLive ? <p className="mt-3 text-xs text-muted-foreground">Only SystemAdministrator/RiskManager can flip the autonomy switch.</p> : null}
    </>}
    {error && <div className="mt-3 rounded-lg border border-rose-400/30 bg-rose-400/10 p-3 text-xs text-rose-200">{error}</div>}
    {dialog === 'limits' && subscription && (
      <LiveLimitsDialog
        sub={subscription}
        held={heldBy(positions.data, subscription)}
        onClose={() => setDialog(null)}
        onDone={(updated, summary) => {
          setSubscription(updated)
          setNotice(`Limits tightened: ${summary}.`)
          setDialog(null)
        }}
      />
    )}
    {dialog === 'switch-off' && subscription && (
      <SwitchOffDialog
        sub={subscription}
        held={heldBy(positions.data, subscription)}
        onClose={() => setDialog(null)}
        onDone={(updated) => {
          setSubscription(updated)
          setNotice(`${updated.symbol} is switched off.`)
          setDialog(null)
        }}
      />
    )}
    {confirmOpen && subscription && (
      <ConfirmDialog
        title={confirmOpen === 'tick' ? `Simulate a tick for ${subscription.symbol}` : confirmOpen === 'enable' ? `Enable live autonomous trading for ${subscription.symbol}` : `Disable live autonomous trading for ${subscription.symbol}`}
        description={confirmOpen === 'tick'
          ? `Autonomy is on for this strategy, so a signal from a tick at ${tickPrice} sends a real order to the broker, exactly as the scheduler would. Type the symbol to go ahead.`
          : confirmOpen === 'enable'
            ? "Once enabled, every real signal this strategy generates submits straight to the broker with no per-order human approval -- only the Kill Switch, Compliance Checker, Correlation Constraint, and this subscription's own standing rate/notional caps can stop an order. Type the symbol to confirm."
            : 'This strategy stops generating any new live orders immediately. Type the symbol to confirm.'}
        confirmLabel={confirmOpen === 'tick' ? 'Run the tick' : confirmOpen === 'enable' ? 'Confirm and enable autonomy' : 'Confirm and disable autonomy'}
        tone={confirmOpen === 'disable' ? 'default' : 'danger'}
        phrase={subscription.symbol}
        onConfirm={() => (confirmOpen === 'tick' ? generateTick() : applyAutonomy(confirmOpen === 'enable'))}
        onClose={() => setConfirmOpen(null)}
      />
    )}
  </div>
}
