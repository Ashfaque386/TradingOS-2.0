'use client'

import { FormEvent, useEffect, useState } from 'react'
import { CalendarRange, FileText, RefreshCw, Sparkles } from 'lucide-react'
import { ShellLayout } from '@/components/shell/shell-layout'
import { useAuth } from '@/components/auth/auth-provider'
import {
  type InvestorReport,
  type InvestorReportSummary,
  ApiError,
  generateInvestorReport,
  getInvestorReport,
  listInvestorReports,
} from '@/lib/api'

function todayIso(): string {
  return new Date().toISOString().slice(0, 10)
}

function weekAgoIso(): string {
  const d = new Date()
  d.setDate(d.getDate() - 7)
  return d.toISOString().slice(0, 10)
}

export default function InvestorReportsPage() {
  const { role } = useAuth()
  // POST /investor-reports/generate is SystemAdministrator/PortfolioManager-only
  // server-side (src/api/routes/investor_reports.py's _OPERATOR_ROLES).
  const canGenerate = role === 'SystemAdministrator' || role === 'PortfolioManager'
  const [reports, setReports] = useState<InvestorReportSummary[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [selected, setSelected] = useState<InvestorReport | null>(null)
  const [periodStart, setPeriodStart] = useState(weekAgoIso())
  const [periodEnd, setPeriodEnd] = useState(todayIso())
  const [cadence, setCadence] = useState<'weekly' | 'monthly'>('weekly')
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function reload() {
    const data = await listInvestorReports()
    setReports(data)
    setSelectedId((prev) => prev ?? data[0]?.id ?? null)
  }

  useEffect(() => {
    reload()
  }, [])

  useEffect(() => {
    if (!selectedId) {
      setSelected(null)
      return
    }
    getInvestorReport(selectedId).then(setSelected).catch(() => setSelected(null))
  }, [selectedId])

  async function handleGenerate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setGenerating(true)
    setError(null)
    try {
      const report = await generateInvestorReport({
        period_start: periodStart,
        period_end: periodEnd,
        cadence,
      })
      await reload()
      setSelectedId(report.id)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to generate report')
    } finally {
      setGenerating(false)
    }
  }

  return (
    <ShellLayout>
      <div className="mx-auto flex w-full max-w-[1700px] flex-col gap-5">
        <header className="mission-hero">
          <div>
            <p className="eyebrow flex items-center gap-2"><FileText className="size-3 text-cyan-300" />TRADINGOS // INVESTOR REPORTS</p>
            <h1 className="mt-2 text-3xl font-semibold tracking-[-.04em]">Investor reporting</h1>
            <p className="mt-2 max-w-2xl text-sm text-muted-foreground">Real performance narratives generated from paper fills, live trade counts, and Post-Trade Review findings -- weekly by default, browsable here. Never a fabricated figure: a zero-trade period says so plainly.</p>
          </div>
        </header>

        {canGenerate && (
          <form onSubmit={handleGenerate} className="flex flex-wrap items-end gap-3 rounded-xl border border-white/10 bg-white/[.02] p-3">
            <CalendarRange className="size-4 shrink-0 text-cyan-300" />
            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
              Period start
              <input type="date" value={periodStart} onChange={(e) => setPeriodStart(e.target.value)} className="rounded-lg border border-white/10 bg-black/20 px-2 py-1.5 text-sm text-foreground" />
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
              Period end
              <input type="date" value={periodEnd} onChange={(e) => setPeriodEnd(e.target.value)} className="rounded-lg border border-white/10 bg-black/20 px-2 py-1.5 text-sm text-foreground" />
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
              Cadence
              <select value={cadence} onChange={(e) => setCadence(e.target.value as 'weekly' | 'monthly')} className="rounded-lg border border-white/10 bg-black/20 px-2 py-1.5 text-sm text-foreground">
                <option value="weekly">Weekly</option>
                <option value="monthly">Monthly</option>
              </select>
            </label>
            <button type="submit" disabled={generating} className="button-primary">
              {generating ? <RefreshCw className="size-3 animate-spin" /> : <Sparkles className="size-3" />}Generate now
            </button>
            {error && <span className="text-xs text-rose-300">{error}</span>}
          </form>
        )}

        <div className="grid gap-5 xl:grid-cols-[320px_1fr]">
          <section className="pulse-panel flex flex-col overflow-hidden">
            <div className="border-b border-white/8 p-4">
              <p className="eyebrow">REPORT HISTORY · {reports.length}</p>
            </div>
            <div className="flex max-h-[70vh] flex-col overflow-y-auto p-2">
              {reports.length === 0 && <p className="p-3 text-xs text-muted-foreground">No investor reports yet. The weekly scheduler generates one automatically (Monday 07:00 IST), or use the form above to generate one now.</p>}
              {reports.map((r) => (
                <button
                  key={r.id}
                  onClick={() => setSelectedId(r.id)}
                  className={`flex flex-col gap-1 rounded-lg px-3 py-2.5 text-left text-xs transition-colors ${selectedId === r.id ? 'bg-cyan-400/10 text-cyan-100' : 'text-muted-foreground hover:bg-white/[.03]'}`}
                >
                  <strong className="text-foreground">{r.period_start} → {r.period_end}</strong>
                  <span className="font-mono text-[10px] uppercase tracking-wide">{r.cadence} · generated {new Date(r.generated_at).toLocaleString()}</span>
                </button>
              ))}
            </div>
          </section>

          <section className="pulse-panel overflow-hidden">
            <div className="border-b border-white/8 p-4">
              <p className="eyebrow">REPORT NARRATIVE</p>
              {selected && <p className="mt-1 text-xs text-muted-foreground">{selected.period_start} → {selected.period_end} · {selected.cadence} · generated by {selected.generated_by}</p>}
            </div>
            <div className="p-5">
              {!selectedId && <p className="text-xs text-muted-foreground">Select a report from the list, or generate one above.</p>}
              {selectedId && !selected && <p className="text-xs text-muted-foreground">Loading...</p>}
              {selected && <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-relaxed text-foreground">{selected.content_markdown}</pre>}
            </div>
          </section>
        </div>
      </div>
    </ShellLayout>
  )
}
