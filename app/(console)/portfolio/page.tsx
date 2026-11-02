'use client'

import { useState } from 'react'
import { PieChart, RefreshCw, Sparkles, ThumbsDown, ThumbsUp } from 'lucide-react'
import { useAuth } from '@/components/auth/auth-provider'
import {
  type PortfolioRecommendation,
  type PortfolioRiskMetrics,
  type PortfolioSummary,
  ApiError,
  acceptPortfolioRecommendation,
  generatePortfolioRecommendation,
  getPortfolioRiskMetrics,
  getPortfolioSummary,
  listPortfolioRecommendations,
  rejectPortfolioRecommendation,
} from '@/lib/api'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { ErrorNotice } from '@/components/ui/error-notice'
import { section } from '@/lib/errors'
import { can } from '@/lib/permissions'
import { type Decision, decisionWords, money, netOfCosts } from '@/lib/portfolio-view'
import { indiaStamp } from '@/lib/time'
import { usePolling } from '@/lib/use-polling'

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
  const [confirming, setConfirming] = useState<Decision | null>(null)

  // Reports a failure by throwing, so the confirmation keeps it on screen. Reloads either way: when
  // someone else decided first, the card should not go on offering a decision that is gone.
  const decide = async (decision: Decision) => {
    try {
      if (decision === 'accept') await acceptPortfolioRecommendation(recommendation.id, notes.trim() || undefined)
      else await rejectPortfolioRecommendation(recommendation.id, notes.trim() || undefined)
    } catch (err) {
      onDecided()
      throw err instanceof ApiError ? err : new Error('Failed to record the decision')
    }
    onDecided()
  }

  return (
    <div className="rounded-xl border border-white/10 bg-black/20 p-4" data-testid="recommendation-card" data-status={recommendation.status}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className={`status-badge ${statusBadgeClass(recommendation.status)}`}>{recommendation.status}</span>
          <span className="font-mono text-[10px] text-muted-foreground">
            {recommendation.llm_source === 'llm' ? 'LLM-narrated' : 'fallback narrative (no LLM reachable)'}
          </span>
        </div>
        <span className="font-mono text-[10px] text-muted-foreground">{indiaStamp(recommendation.created_at)} India time</span>
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
          {recommendation.status} by {recommendation.reviewed_by} · {indiaStamp(recommendation.reviewed_at)} India time
          {recommendation.reviewer_notes && ` · "${recommendation.reviewer_notes}"`}
        </p>
      )}

      {recommendation.status === 'pending' && canDecide && (
        <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-white/8 pt-3">
          <input
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Optional note..."
            aria-label="Note for the decision"
            className="min-w-0 flex-1 rounded border border-white/10 bg-black/30 px-2 py-1.5 text-xs text-foreground outline-none"
          />
          <button type="button" onClick={() => setConfirming('accept')} className="button-primary"><ThumbsUp className="size-3" />Accept</button>
          <button type="button" onClick={() => setConfirming('reject')} className="button-danger"><ThumbsDown className="size-3" />Reject</button>
        </div>
      )}
      {confirming && (
        <ConfirmDialog
          {...decisionWords(confirming)}
          tone={confirming === 'reject' ? 'danger' : 'default'}
          onConfirm={() => decide(confirming)}
          onClose={() => setConfirming(null)}
        >
          {notes.trim() && <p className="text-xs text-muted-foreground">Your note: “{notes.trim()}”</p>}
        </ConfirmDialog>
      )}
    </div>
  )
}

export default function PortfolioPage() {
  const { role } = useAuth()
  const canManage = can(role, 'POST /api/v1/portfolio/recommendations/generate')

  const [generating, setGenerating] = useState(false)
  const [generateError, setGenerateError] = useState<string | null>(null)

  // Three reads that fail on their own: a broker that will not answer for the margin must not hide the
  // recommendations, and a recommendations failure must not read as "none yet".
  const { data: portfolio, refresh: reload } = usePolling(
    async () => {
      const [summary, riskMetrics, recommendations] = await Promise.all([
        section(getPortfolioSummary, 'the portfolio summary'),
        section(getPortfolioRiskMetrics, 'the risk metrics'),
        section(listPortfolioRecommendations, 'the recommendations'),
      ])
      return { summary, riskMetrics, recommendations }
    },
    { intervalMs: 30_000 },
  )
  const summary: PortfolioSummary | null = portfolio?.summary.data ?? null
  const riskMetrics: PortfolioRiskMetrics | null = portfolio?.riskMetrics.data ?? null
  const recommendations: PortfolioRecommendation[] = portfolio?.recommendations.data ?? []

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
    <>
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
        {portfolio?.summary.error && <ErrorNotice className="mb-3" message={portfolio.summary.error} onRetry={() => void reload()} />}
        {summary && <p className="mb-2 font-mono text-[10px] text-muted-foreground">As of {indiaStamp(summary.as_of)} India time, refreshed every 30 seconds</p>}

        {summary && (
          <div className="orders-summary">
            <div className="summary-stat"><span>ACTIVE STRATEGIES</span><strong>{summary.active_strategy_count}</strong><small>paper/live-eligible/live</small></div>
            <div className="summary-stat"><span>PAPER REALIZED P&amp;L TODAY (BEFORE COSTS)</span><strong className={summary.paper_realized_pnl_today >= 0 ? 'gain' : 'loss'}>{money(summary.paper_realized_pnl_today)}</strong><small>costs {money(summary.paper_costs_today)} · net {money(netOfCosts(summary.paper_realized_pnl_today, summary.paper_costs_today))} · {summary.paper_open_position_count} open paper position(s){summary.paper_unmanaged_position_count > 0 ? ` · ${summary.paper_unmanaged_position_count} unmanaged (subscription inactive)` : ''}</small></div>
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

        {portfolio?.riskMetrics.error && <ErrorNotice className="mb-3" message={portfolio.riskMetrics.error} onRetry={() => void reload()} />}
        {riskMetrics && (
          <Panel title="Risk metrics" eyebrow="REAL EXPOSURE AND CONCENTRATION -- NO FABRICATED VaR MODEL">
            <div className="grid gap-3 p-3 sm:grid-cols-3">
              <div className="rounded-lg border border-white/8 bg-black/20 p-3">
                <p className="font-mono text-[9px] uppercase tracking-wider text-muted-foreground">Total exposure</p>
                <p className="mt-1 font-mono text-lg text-foreground">{riskMetrics.total_exposure.toLocaleString('en-IN', { maximumFractionDigits: 2 })}</p>
                <p className="mt-0.5 text-[10px] text-muted-foreground">{riskMetrics.open_position_count} open position(s)</p>
              </div>
              <div className="rounded-lg border border-white/8 bg-black/20 p-3">
                <p className="font-mono text-[9px] uppercase tracking-wider text-muted-foreground">Largest position concentration</p>
                <p className="mt-1 font-mono text-lg text-foreground">{riskMetrics.largest_position_concentration_pct === null ? '—' : `${riskMetrics.largest_position_concentration_pct.toFixed(1)}%`}</p>
                <p className="mt-0.5 text-[10px] text-muted-foreground">{riskMetrics.exposure_by_position[0]?.strategy_name ?? 'no open positions'}</p>
              </div>
              <div className="rounded-lg border border-white/8 bg-black/20 p-3">
                <p className="font-mono text-[9px] uppercase tracking-wider text-muted-foreground">Margin utilization</p>
                <p className="mt-1 font-mono text-lg text-foreground">{riskMetrics.margin_utilization_pct === null ? '—' : `${riskMetrics.margin_utilization_pct.toFixed(1)}%`}</p>
                <p className="mt-0.5 text-[10px] text-muted-foreground">{riskMetrics.broker_configured ? 'used / (used + available)' : 'no broker configured'}</p>
              </div>
            </div>
            {riskMetrics.exposure_by_position.length > 0 && (
              <div className="overflow-x-auto px-3 pb-3">
                <table className="orders-table">
                  <thead><tr><th>Strategy</th><th>Mode</th><th>Symbol</th><th>Exposure</th></tr></thead>
                  <tbody>
                    {riskMetrics.exposure_by_position.map((e, i) => (
                      <tr key={i}><td>{e.strategy_name}</td><td className="mono">{e.mode}</td><td className="mono">{e.symbol}</td><td className="mono">{e.exposure.toLocaleString('en-IN', { maximumFractionDigits: 2 })}</td></tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        )}

        <Panel title="Recommendations" eyebrow="ADVISORY ONLY -- ACCEPT/REJECT IS RECORDED, NEVER AUTO-EXECUTED">
          <div className="flex flex-col gap-3 p-3">
            {portfolio?.recommendations.error && <ErrorNotice message={portfolio.recommendations.error} onRetry={() => void reload()} />}
            {portfolio && !portfolio.recommendations.error && recommendations.length === 0 && (
              <p className="p-2 text-xs text-muted-foreground">No recommendations yet. {canManage ? 'Generate one above.' : 'A Portfolio Manager or System Administrator can generate one.'}</p>
            )}
            {recommendations.map((r) => (
              <RecommendationCard key={r.id} recommendation={r} canDecide={canManage} onDecided={reload} />
            ))}
          </div>
        </Panel>
      </main>
    </>
  )
}
