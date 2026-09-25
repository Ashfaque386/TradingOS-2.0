'use client'

import { useEffect, useState } from 'react'
import { PieChart, RefreshCw, Sparkles, ThumbsDown, ThumbsUp } from 'lucide-react'
import { ShellLayout } from '@/components/shell/shell-layout'
import { useAuth } from '@/components/auth/auth-provider'
import {
  type PortfolioRecommendation,
  type PortfolioSummary,
  ApiError,
  acceptPortfolioRecommendation,
  generatePortfolioRecommendation,
  getPortfolioSummary,
  listPortfolioRecommendations,
  rejectPortfolioRecommendation,
} from '@/lib/api'

function Panel({ title, eyebrow, children, actions }: { title: string; eyebrow?: string; children: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <section className="orders-panel">
      <div className="orders-panel-head" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>{eyebrow && <p className="eyebrow">{eyebrow}</p>}<h2>{title}</h2></div>
        {actions}
      </div>
      {children}
    </section>
  )
}

function actionBadgeClass(action: string): string {
  if (action === 'increase') return 'status-filled'
  if (action === 'decrease') return 'status-rejected'
  return 'status-open'
}

function statusBadgeClass(status: string): string {
  if (status === 'accepted') return 'status-filled'
  if (status === 'rejected') return 'status-rejected'
  return 'status-open'
}

function RecommendationCard({
  recommendation,
  canDecide,
  onDecided,
}: {
  recommendation: PortfolioRecommendation
  canDecide: boolean
  onDecided: () => void
}) {
  const [notes, setNotes] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const decide = async (accept: boolean) => {
    setBusy(true)
    setError(null)
    try {
      if (accept) await acceptPortfolioRecommendation(recommendation.id, notes || undefined)
      else await rejectPortfolioRecommendation(recommendation.id, notes || undefined)
      onDecided()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to record decision')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-xl border border-white/10 bg-black/20 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className={`status-badge ${statusBadgeClass(recommendation.status)}`}>{recommendation.status}</span>
          <span className="font-mono text-[10px] text-muted-foreground">
            {recommendation.llm_source === 'llm' ? 'LLM-narrated' : 'fallback narrative (no LLM reachable)'}
          </span>
        </div>
        <span className="font-mono text-[10px] text-muted-foreground">{new Date(recommendation.created_at).toLocaleString()}</span>
      </div>

      <p className="mt-3 text-xs leading-relaxed text-foreground">{recommendation.summary}</p>

      <div className="mt-3 overflow-x-auto">
        <table className="orders-table">
          <thead><tr><th>Strategy</th><th>Status</th><th>Action</th><th>Sharpe</th><th>Max DD</th><th>Rationale</th></tr></thead>
          <tbody>
            {recommendation.allocations.map((a) => (
              <tr key={a.strategy_id}>
                <td>{a.strategy_name}</td>
                <td className="mono">{a.strategy_status}</td>
                <td><span className={`outcome ${actionBadgeClass(a.action)}`}>{a.action}</span></td>
                <td className="mono">{a.sharpe === null ? '—' : a.sharpe.toFixed(2)}</td>
                <td className="mono">{a.max_drawdown === null ? '—' : `${(a.max_drawdown * 100).toFixed(1)}%`}</td>
                <td className="text-muted-foreground">{a.rationale}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {recommendation.status !== 'pending' && (
        <p className="mt-3 font-mono text-[10px] text-muted-foreground">
          {recommendation.status} by {recommendation.reviewed_by} · {recommendation.reviewed_at && new Date(recommendation.reviewed_at).toLocaleString()}
          {recommendation.reviewer_notes && ` · "${recommendation.reviewer_notes}"`}
        </p>
      )}

      {recommendation.status === 'pending' && canDecide && (
        <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-white/8 pt-3">
          <input
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Optional note..."
            className="min-w-0 flex-1 rounded border border-white/10 bg-black/30 px-2 py-1.5 text-xs text-foreground outline-none"
          />
          <button type="button" onClick={() => decide(true)} disabled={busy} className="button-primary"><ThumbsUp className="size-3" />Accept</button>
          <button type="button" onClick={() => decide(false)} disabled={busy} className="button-danger"><ThumbsDown className="size-3" />Reject</button>
        </div>
      )}
      {error && <p className="mt-2 text-[10px] text-rose-300">{error}</p>}
    </div>
  )
}

export default function PortfolioPage() {
  const { role } = useAuth()
  const canManage = role === 'SystemAdministrator' || role === 'PortfolioManager'

  const [summary, setSummary] = useState<PortfolioSummary | null>(null)
  const [recommendations, setRecommendations] = useState<PortfolioRecommendation[]>([])
  const [generating, setGenerating] = useState(false)
  const [generateError, setGenerateError] = useState<string | null>(null)

  const reload = async () => {
    const [s, r] = await Promise.all([getPortfolioSummary(), listPortfolioRecommendations()])
    setSummary(s)
    setRecommendations(r)
  }

  useEffect(() => {
    reload()
    const interval = setInterval(reload, 30000)
    return () => clearInterval(interval)
  }, [])

  const handleGenerate = async () => {
    setGenerating(true)
    setGenerateError(null)
    try {
      await generatePortfolioRecommendation()
      await reload()
    } catch (err) {
      setGenerateError(err instanceof ApiError ? err.message : 'Failed to generate a recommendation')
    } finally {
      setGenerating(false)
    }
  }

  return (
    <ShellLayout>
      <main className="orders-page mx-auto w-full max-w-[1700px] pb-10">
        <header className="orders-header">
          <div>
            <p className="eyebrow flex items-center gap-2"><PieChart className="size-3 text-cyan-300" />TRADINGOS // PORTFOLIO</p>
            <h1>Portfolio</h1>
            <p>Real cross-broker rollup and advisory-only rebalancing recommendations -- every allocation action comes from real backtest metrics, and nothing here ever places a trade.</p>
          </div>
          {canManage && (
            <button type="button" onClick={handleGenerate} disabled={generating} className="button-primary">
              {generating ? <RefreshCw className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" />}
              Generate recommendation
            </button>
          )}
        </header>
        {generateError && <p className="mb-3 text-xs text-rose-300">{generateError}</p>}

        {summary && (
          <div className="orders-summary">
            <div className="summary-stat"><span>ACTIVE STRATEGIES</span><strong>{summary.active_strategy_count}</strong><small>paper/live-eligible/live</small></div>
            <div className="summary-stat"><span>PAPER REALIZED P&amp;L TODAY</span><strong className={summary.paper_realized_pnl_today >= 0 ? 'gain' : 'loss'}>{summary.paper_realized_pnl_today.toLocaleString('en-IN', { maximumFractionDigits: 2 })}</strong><small>{summary.paper_open_position_count} open paper position(s)</small></div>
            <div className="summary-stat"><span>LIVE POSITIONS</span><strong>{summary.live_position_count}</strong><small>open live position(s)</small></div>
            <div className="summary-stat">
              <span>BROKER MARGIN</span>
              {summary.broker_configured ? (
                <>
                  <strong>{summary.available_margin?.toLocaleString('en-IN', { maximumFractionDigits: 2 }) ?? '—'}</strong>
                  <small>{summary.broker_name} · used {summary.used_margin?.toLocaleString('en-IN', { maximumFractionDigits: 2 }) ?? '—'}</small>
                </>
              ) : (
                <>
                  <strong className="text-muted-foreground">—</strong>
                  <small>no broker configured</small>
                </>
              )}
            </div>
          </div>
        )}

        <Panel title="Recommendations" eyebrow="ADVISORY ONLY -- ACCEPT/REJECT IS RECORDED, NEVER AUTO-EXECUTED">
          <div className="flex flex-col gap-3 p-3">
            {recommendations.length === 0 && (
              <p className="p-2 text-xs text-muted-foreground">No recommendations yet. {canManage ? 'Generate one above.' : 'A Portfolio Manager or System Administrator can generate one.'}</p>
            )}
            {recommendations.map((r) => (
              <RecommendationCard key={r.id} recommendation={r} canDecide={canManage} onDecided={reload} />
            ))}
          </div>
        </Panel>
      </main>
    </ShellLayout>
  )
}
