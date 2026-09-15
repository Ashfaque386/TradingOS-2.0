'use client'

import { Activity, Bot, FlaskConical, ShieldCheck, TrendingUp, WalletCards } from 'lucide-react'
import { ShellLayout } from '@/components/shell/shell-layout'

const metrics = [
  { label: 'Active agents', value: '24', detail: '22 online · 2 idle', icon: Bot, accent: 'text-cyan-300' },
  { label: 'Today\'s P&L', value: '+₹84,260', detail: '+2.18% vs. yesterday', icon: TrendingUp, accent: 'text-emerald-300' },
  { label: 'Strategies running', value: '08', detail: '3 awaiting approval', icon: FlaskConical, accent: 'text-violet-300' },
  { label: 'Risk exposure', value: '₹12.4L', detail: '34% of daily limit', icon: ShieldCheck, accent: 'text-amber-300' },
]

export default function Home() {
  return (
    <ShellLayout>
      <div className="mx-auto flex w-full max-w-[1600px] flex-col gap-6">
        <section className="flex flex-col justify-between gap-4 sm:flex-row sm:items-end">
          <div>
            <p className="font-mono text-xs uppercase tracking-[0.24em] text-cyan-300/70">NSE / BSE · 09:42:18 IST</p>
            <h1 className="mt-2 text-3xl font-semibold tracking-tight text-foreground">Overview</h1>
            <p className="mt-1 text-sm text-muted-foreground">Autonomous trading operations at a glance.</p>
          </div>
          <div className="flex items-center gap-2 rounded-lg border border-emerald-400/20 bg-emerald-400/10 px-3 py-2 text-xs text-emerald-200">
            <span className="size-2 animate-pulse rounded-full bg-emerald-300" />
            All systems nominal
          </div>
        </section>

        <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {metrics.map((metric) => {
            const Icon = metric.icon
            return (
              <article key={metric.label} className="glass group p-5 transition-transform duration-200 hover:-translate-y-0.5">
                <div className="flex items-start justify-between">
                  <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">{metric.label}</p>
                  <Icon className={`size-4 ${metric.accent}`} aria-hidden="true" />
                </div>
                <p className="mt-5 font-mono text-2xl font-semibold text-foreground">{metric.value}</p>
                <p className="mt-2 text-xs text-muted-foreground">{metric.detail}</p>
              </article>
            )
          })}
        </section>

        <section className="grid gap-4 xl:grid-cols-[1.6fr_1fr]">
          <article className="glass min-h-[280px] p-5">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-foreground">Portfolio equity</p>
                <p className="mt-1 text-xs text-muted-foreground">Intraday performance · paper trading</p>
              </div>
              <Activity className="size-4 text-cyan-300" aria-hidden="true" />
            </div>
            <div className="mt-8 flex h-40 items-end gap-2 opacity-80" aria-label="Equity curve placeholder">
              {[32, 42, 38, 56, 52, 66, 61, 78, 72, 88, 82, 96, 90, 100].map((height, index) => (
                <div key={index} className="flex-1 rounded-t-sm bg-gradient-to-t from-cyan-500/20 to-cyan-300/80" style={{ height: `${height}%` }} />
              ))}
            </div>
            <div className="mt-4 flex justify-between font-mono text-[10px] text-muted-foreground"><span>09:15</span><span>12:30</span><span>15:30</span></div>
          </article>

          <article className="glass min-h-[280px] p-5">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-foreground">Agent activity</p>
                <p className="mt-1 text-xs text-muted-foreground">Latest autonomous events</p>
              </div>
              <WalletCards className="size-4 text-violet-300" aria-hidden="true" />
            </div>
            <div className="mt-6 flex flex-col gap-4">
              {['Momentum Scout completed scan', 'Risk Sentinel updated exposure', 'Nifty Breakout queued backtest'].map((event, index) => (
                <div key={event} className="flex gap-3 border-b border-white/5 pb-3 last:border-0">
                  <span className={`mt-1 size-2 shrink-0 rounded-full ${index === 1 ? 'bg-amber-300' : 'bg-emerald-300'}`} />
                  <div><p className="text-sm text-foreground">{event}</p><p className="mt-1 font-mono text-[10px] text-muted-foreground">{index + 2}m ago</p></div>
                </div>
              ))}
            </div>
          </article>
        </section>
      </div>
    </ShellLayout>
  )
}
