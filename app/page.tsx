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
      <div className="mx-auto flex w-full max-w-[1700px] flex-col gap-5">
        <section className="hero-grid relative overflow-hidden rounded-2xl border border-cyan-300/15 px-6 py-6 sm:px-8">
          <div className="relative z-10 flex flex-col justify-between gap-6 lg:flex-row lg:items-end">
            <div>
              <p className="eyebrow">TRADINGOS // INTELLIGENCE LAYER 02</p>
              <h1 className="mt-3 max-w-2xl text-4xl font-semibold tracking-[-0.04em] text-foreground sm:text-5xl">Good morning, <span className="text-cyan-300">Trader.</span></h1>
              <p className="mt-3 max-w-xl text-sm leading-6 text-muted-foreground">Your autonomous trading network is online. Monitor agents, validate strategies, and move with signal-level clarity.</p>
            </div>
            <div className="flex items-center gap-3 rounded-xl border border-emerald-300/20 bg-emerald-300/10 px-4 py-3 text-xs text-emerald-200 shadow-[0_0_30px_rgba(52,211,153,0.08)]">
              <span className="size-2 animate-pulse rounded-full bg-emerald-300 shadow-[0_0_12px_#6ee7b7]" />
              <span><strong className="block font-medium text-emerald-100">All systems nominal</strong><span className="text-emerald-200/60">NSE / BSE · 09:42:18 IST</span></span>
            </div>
          </div>
          <div className="scanline" aria-hidden="true" />
        </section>

        <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {metrics.map((metric) => {
            const Icon = metric.icon
            return (
              <article key={metric.label} className="metric-card group">
                <div className="flex items-start justify-between"><p className="eyebrow text-[10px]">{metric.label}</p><Icon className={`size-4 ${metric.accent} transition-transform group-hover:scale-110`} aria-hidden="true" /></div>
                <p className="mt-5 font-mono text-3xl font-semibold tracking-tight text-foreground">{metric.value}</p>
                <p className="mt-2 text-xs text-muted-foreground">{metric.detail}</p>
                <div className="mt-4 h-px w-full bg-gradient-to-r from-cyan-300/40 to-transparent" />
              </article>
            )
          })}
        </section>

        <section className="grid gap-4 xl:grid-cols-[1.55fr_1fr]">
          <article className="panel-glow min-h-[330px] p-5 sm:p-6">
            <div className="flex items-center justify-between"><div><p className="text-sm font-medium text-foreground">Portfolio intelligence</p><p className="mt-1 text-xs text-muted-foreground">Intraday equity trajectory · paper trading</p></div><span className="chip"><Activity className="size-3" /> LIVE FEED</span></div>
            <div className="relative mt-8 h-48 overflow-hidden rounded-xl border border-cyan-300/10 bg-[#071525]/70 p-4">
              <div className="chart-grid absolute inset-0" aria-hidden="true" />
              <div className="relative flex h-full items-end gap-2 opacity-90" aria-label="Equity curve visualization">
                {[32,42,38,56,52,66,61,78,72,88,82,96,90,100].map((height,index) => <div key={index} className="bar flex-1 rounded-t-sm" style={{height: `${height}%`, animationDelay: `${index * 40}ms`}} />)}
              </div>
              <div className="absolute left-4 top-4 font-mono text-xs text-cyan-200">+₹84,260 <span className="text-emerald-300">+2.18%</span></div>
            </div>
            <div className="mt-4 flex justify-between font-mono text-[10px] text-muted-foreground"><span>09:15</span><span>12:30</span><span>15:30</span></div>
          </article>

          <article className="panel-glow min-h-[330px] p-5 sm:p-6"><div className="flex items-center justify-between"><div><p className="text-sm font-medium text-foreground">Agent activity</p><p className="mt-1 text-xs text-muted-foreground">Autonomous event stream</p></div><WalletCards className="size-4 text-violet-300" aria-hidden="true" /></div><div className="mt-7 flex flex-col gap-5">{['Momentum Scout completed scan','Risk Sentinel updated exposure','Nifty Breakout queued backtest'].map((event,index)=><div key={event} className="flex gap-3"><span className={`mt-1.5 size-2 shrink-0 rounded-full ${index === 1 ? 'bg-amber-300' : 'bg-emerald-300'} shadow-[0_0_10px_currentColor]`} /><div><p className="text-sm text-foreground">{event}</p><p className="mt-1 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">{index + 2}m ago · verified</p></div></div>)}</div></article>
        </section>
      </div>
    </ShellLayout>
  )
}
