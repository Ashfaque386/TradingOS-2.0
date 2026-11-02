'use client'

import { Suspense, useEffect, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import { CalendarRange, Download, FileText, RefreshCw, Sparkles } from 'lucide-react'
import { useAuth } from '@/components/auth/auth-provider'
import { ErrorNotice } from '@/components/ui/error-notice'
import { Markdown } from '@/components/ui/markdown'
import { type InvestorReport, generateInvestorReport, getInvestorReport, listInvestorReports } from '@/lib/api'
import { todayInIndia } from '@/lib/dates'
import { downloadText } from '@/lib/download'
import { messageOf } from '@/lib/errors'
import { type Cadence, defaultPeriod, periodProblem, reportsFor } from '@/lib/investor-report-period'
import { reportFilename } from '@/lib/markdown'
import { can } from '@/lib/permissions'
import { indiaStamp } from '@/lib/time'
import { useQuery } from '@/lib/use-query'

function InvestorReports() {
  const { role } = useAuth()
  // POST /investor-reports/generate is SystemAdministrator/PortfolioManager-only
  // server-side (src/api/routes/investor_reports.py's _OPERATOR_ROLES).
  const canGenerate = can(role, 'POST /api/v1/investor-reports/generate')
  const today = todayInIndia(new Date())

  const reports = useQuery('investor-reports', listInvestorReports)
  const history = reports.data ?? []

  // Which report is open: one chosen here, or one named in the address, else the newest.
  const search = useSearchParams()
  const [chosen, setChosen] = useState<string | null>(() => search.get('report'))
  const selectedId = chosen ?? history[0]?.id ?? null
  const detail = useQuery(`investor-report:${selectedId}`, () => getInvestorReport(selectedId as string), { enabled: selectedId !== null })
  const selected: InvestorReport | null = detail.data?.id === selectedId ? detail.data : null

  useEffect(() => {
    if (chosen) window.history.replaceState(null, '', `${window.location.pathname}?report=${encodeURIComponent(chosen)}`)
  }, [chosen])

  // The form opens on the period the scheduler would report next, and follows the cadence until the dates are edited.
  const [cadence, setCadence] = useState<Cadence>('weekly')
  const [period, setPeriod] = useState(() => defaultPeriod('weekly', today))
  const [edited, setEdited] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [generateError, setGenerateError] = useState<string | null>(null)
  const [generated, setGenerated] = useState<string | null>(null)

  const problem = periodProblem(period.start, period.end, cadence, today)
  const existing = problem ? [] : reportsFor(history, period.start, period.end, cadence)

  const changeCadence = (next: Cadence) => {
    setCadence(next)
    setGenerated(null)
    if (!edited) setPeriod(defaultPeriod(next, today))
  }
  const changeDate = (field: 'start' | 'end', value: string) => {
    setEdited(true)
    setGenerated(null)
    setPeriod((p) => ({ ...p, [field]: value }))
  }

  async function handleGenerate(event: React.FormEvent) {
    event.preventDefault()
    if (problem) return
    setGenerating(true)
    setGenerateError(null)
    setGenerated(null)
    try {
      const report = await generateInvestorReport({ period_start: period.start, period_end: period.end, cadence })
      await reports.refresh()
      setChosen(report.id)
      setGenerated(`Generated the ${report.cadence} report for ${report.period_start} to ${report.period_end}.`)
    } catch (err) {
      setGenerateError(messageOf(err, 'Failed to generate report'))
    } finally {
      setGenerating(false)
    }
  }

  const input = 'rounded-lg border border-white/10 bg-black/20 px-2 py-1.5 text-sm text-foreground'

  return (
    <>
      <div className="mx-auto flex w-full max-w-[1700px] flex-col gap-5">
        <header className="mission-hero">
          <div>
            <p className="eyebrow flex items-center gap-2"><FileText className="size-3 text-cyan-300" />TRADINGOS // INVESTOR REPORTS</p>
            <h1 className="mt-2 text-3xl font-semibold tracking-[-.04em]">Investor reporting</h1>
            <p className="mt-2 max-w-2xl text-sm text-muted-foreground">Real performance narratives generated from paper fills, live trade counts, and Post-Trade Review findings -- weekly by default, browsable here. Never a fabricated figure: a zero-trade period says so plainly.</p>
          </div>
        </header>

        {reports.error && <ErrorNotice message={`Could not load the reports: ${reports.error}`} onRetry={() => void reports.refresh()} />}

        {canGenerate && (
          <form onSubmit={handleGenerate} className="flex flex-col gap-2 rounded-xl border border-white/10 bg-white/[.02] p-3" aria-label="Generate a report" noValidate>
            <div className="flex flex-wrap items-end gap-3">
              <CalendarRange className="size-4 shrink-0 text-cyan-300" />
              <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                Period start
                <input type="date" value={period.start} max={today} onChange={(e) => changeDate('start', e.target.value)} className={input} />
              </label>
              <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                Period end
                <input type="date" value={period.end} max={today} onChange={(e) => changeDate('end', e.target.value)} className={input} />
              </label>
              <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                Cadence
                <select value={cadence} onChange={(e) => changeCadence(e.target.value as Cadence)} className={input}>
                  <option value="weekly">Weekly</option>
                  <option value="monthly">Monthly</option>
                </select>
              </label>
              <button type="submit" disabled={generating || problem !== null} className="button-primary">
                {generating ? <RefreshCw className="size-3 animate-spin" /> : <Sparkles className="size-3" />}Generate now
              </button>
            </div>
            {problem && <p role="alert" className="text-xs text-rose-300">{problem}</p>}
            {existing.length > 0 && (
              <p className="text-xs text-amber-200">
                A {cadence} report for this period already exists (generated {indiaStamp(existing[0].generated_at)} India time). Generating again writes a second one, which is worth doing only if more has been filled since.
              </p>
            )}
            {generateError && <ErrorNotice message={generateError} />}
            {generated && <p role="status" className="text-xs text-emerald-300">{generated}</p>}
          </form>
        )}

        <div className="grid gap-5 xl:grid-cols-[320px_1fr]">
          <section className="pulse-panel flex flex-col overflow-hidden">
            <div className="border-b border-white/8 p-4">
              <p className="eyebrow">REPORT HISTORY · {history.length}</p>
            </div>
            <div className="flex max-h-[70vh] flex-col overflow-y-auto p-2">
              {reports.loading && !reports.error && <p className="p-3 text-xs text-muted-foreground">Loading the reports…</p>}
              {!reports.loading && !reports.error && history.length === 0 && <p className="p-3 text-xs text-muted-foreground">No investor reports yet. The scheduler generates one on its own schedule (see the automation schedule in Mission Control){canGenerate ? ', or use the form above to generate one now.' : '.'}</p>}
              {history.map((r) => (
                <button
                  key={r.id}
                  onClick={() => setChosen(r.id)}
                  aria-current={selectedId === r.id ? 'true' : undefined}
                  className={`flex flex-col gap-1 rounded-lg px-3 py-2.5 text-left text-xs transition-colors ${selectedId === r.id ? 'bg-cyan-400/10 text-cyan-100' : 'text-muted-foreground hover:bg-white/[.03]'}`}
                >
                  <strong className="text-foreground">{r.period_start} → {r.period_end}</strong>
                  <span className="font-mono text-[10px] uppercase tracking-wide">{r.cadence} · generated {indiaStamp(r.generated_at)}</span>
                </button>
              ))}
            </div>
          </section>

          <section className="pulse-panel overflow-hidden" aria-label="Report narrative">
            <div className="flex flex-wrap items-start justify-between gap-3 border-b border-white/8 p-4">
              <div>
                <p className="eyebrow">REPORT NARRATIVE</p>
                {selected && <p className="mt-1 text-xs text-muted-foreground">{selected.period_start} → {selected.period_end} · {selected.cadence} · generated {indiaStamp(selected.generated_at)} India time by {selected.generated_by}</p>}
              </div>
              {selected && (
                <button type="button" className="button-secondary" onClick={() => downloadText(reportFilename(selected), selected.content_markdown, 'text/markdown;charset=utf-8')}>
                  <Download className="size-3" />Download Markdown
                </button>
              )}
            </div>
            <div className="p-5">
              {detail.error && selectedId && !selected && <ErrorNotice message={`Could not open that report: ${detail.error}`} onRetry={() => void detail.refresh()} />}
              {!selectedId && !reports.loading && <p className="text-xs text-muted-foreground">{reports.error ? 'The reports could not be loaded, so none can be opened.' : `Select a report from the list${canGenerate ? ', or generate one above' : ''}.`}</p>}
              {selectedId && !selected && !detail.error && <p className="text-xs text-muted-foreground">Loading…</p>}
              {selected && (
                <>
                  <Markdown source={selected.content_markdown} />
                  <details className="mt-6 text-xs text-muted-foreground">
                    <summary className="cursor-pointer">The report as written (Markdown)</summary>
                    <pre className="mt-2 overflow-x-auto whitespace-pre-wrap rounded-lg border border-white/8 bg-black/20 p-3 font-mono text-[11px] leading-relaxed">{selected.content_markdown}</pre>
                  </details>
                </>
              )}
            </div>
          </section>
        </div>
      </div>
    </>
  )
}

export default function InvestorReportsPage() {
  return (
    <Suspense fallback={<div className="mx-auto w-full max-w-[1700px] p-8 text-sm text-muted-foreground">Loading reports…</div>}>
      <InvestorReports />
    </Suspense>
  )
}
