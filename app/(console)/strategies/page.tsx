'use client'

import { FormEvent, useMemo, useState } from 'react'
import { useRouter } from 'next/navigation'
import { ArrowRight, ChevronRight, RefreshCw, TrendingUp } from 'lucide-react'
import { useAuth } from '@/components/auth/auth-provider'
import { type Strategy, ApiError, createStrategy, listStrategies } from '@/lib/api'
import { Badge, type Stage, stageOf, stages } from '@/components/strategies/shared'
import { LiveSubscriptions } from '@/components/strategies/live-subscriptions'
import { ErrorNotice } from '@/components/ui/error-notice'
import { can } from '@/lib/permissions'
import { usePolling } from '@/lib/use-polling'

export default function StrategiesPage() {
  const { role } = useAuth()
  const canOperate = can(role, 'POST /api/v1/strategies')
  const router = useRouter()
  const [name, setName] = useState('')
  const [objective, setObjective] = useState('')
  const [instrumentClass, setInstrumentClass] = useState('equity')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)

  const { data: loadedStrategies, error: loadError, refresh: reload } = usePolling(listStrategies, { intervalMs: 15_000 })
  const strategies: Strategy[] = useMemo(() => loadedStrategies ?? [], [loadedStrategies])

  const grouped = useMemo(
    () => Object.fromEntries(stages.map((stage) => [stage, strategies.filter((s) => stageOf(s) === stage)])) as Record<Stage, Strategy[]>,
    [strategies],
  )

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!name.trim() || !objective.trim()) return
    setCreating(true)
    setCreateError(null)
    try {
      await createStrategy(name.trim(), objective.trim(), instrumentClass)
      setName('')
      setObjective('')
      await reload()
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : 'Failed to create strategy')
    } finally {
      setCreating(false)
    }
  }

  return <><div className="mx-auto w-full max-w-[1700px]">
    <div className="strategy-page-header"><div><p className="eyebrow">TRADINGOS // STRATEGY CONTROL PLANE</p><h1 className="mt-2 text-3xl font-semibold tracking-[-.04em] text-white">Strategy lifecycle</h1><p className="mt-2 text-sm text-slate-400">From generated hypothesis to controlled live capital. Select a strategy to open its review workspace.</p></div><div className="strategy-header-stats"><div><strong>{strategies.length}</strong><span>TRACKED</span></div><div><strong>{strategies.filter(s => !['Ideation', 'Live', 'Deprecated'].includes(stageOf(s))).length.toString().padStart(2, '0')}</strong><span>IN FLIGHT</span></div><div><strong className="text-emerald-300">{strategies.filter(s => stageOf(s) === 'Live').length.toString().padStart(2, '0')}</strong><span>LIVE</span></div></div></div>
    {canOperate && <form onSubmit={handleCreate} className="mb-5 flex flex-wrap items-center gap-2 rounded-xl border border-white/10 bg-white/[.02] p-3">
      <input value={name} onChange={e => setName(e.target.value)} placeholder="Strategy name" className="min-w-0 flex-1 basis-48 bg-transparent text-sm text-foreground outline-none placeholder:text-muted-foreground" />
      <input value={objective} onChange={e => setObjective(e.target.value)} placeholder="Objective for the AI generator..." className="min-w-0 flex-[2] basis-64 bg-transparent text-sm text-foreground outline-none placeholder:text-muted-foreground" />
      <select value={instrumentClass} onChange={e => setInstrumentClass(e.target.value)} className="rounded border border-white/10 bg-black/20 px-2 py-1 text-xs text-foreground"><option value="equity">Equity</option><option value="options">Options</option></select>
      <button type="submit" disabled={creating || !name.trim() || !objective.trim()} className="button-primary">{creating ? <RefreshCw className="size-3 animate-spin" /> : <ArrowRight className="size-3" />}Generate</button>
      {createError && <span className="text-xs text-rose-300">{createError}</span>}
      {loadError && <ErrorNotice className="w-full" message={`Could not load the strategies: ${loadError}`} onRetry={() => void reload()} />}
    </form>}
    <div className="strategy-board">{stages.map((stage, index) => <section className="strategy-column" key={stage}><div className="strategy-column-title"><div><span className="stage-index">0{index + 1}</span><h2>{stage}</h2></div><span className="stage-count">{grouped[stage].length}</span></div><div className="strategy-cards">{grouped[stage].map(item => <button className="strategy-card" key={item.id} onClick={() => router.push(`/strategies/${item.id}`)}><div className="flex items-start justify-between gap-2"><Badge tone={item.instrument_class === 'options' ? 'violet' : 'cyan'}>{item.instrument_class}</Badge><span className="font-mono text-[10px] text-slate-500">{item.versions.length} ver</span></div><h3>{item.name}</h3><div className="flex items-center justify-between gap-2"><span className="flex items-center gap-1 text-[10px] text-muted-foreground"><TrendingUp className="size-3" /> {item.versions[item.versions.length - 1]?.static_validation_passed ? 'validated' : 'awaiting validation'}</span></div><div className="mt-3 flex items-center justify-between border-t border-white/8 pt-2 text-[10px] text-slate-500"><span>REVIEW WORKSPACE</span><ChevronRight className="size-3.5 text-cyan-300" /></div></button>)}</div></section>)}</div>
    <LiveSubscriptions strategies={strategies} />
  </div></>
}
