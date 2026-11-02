import type { Strategy } from '@/lib/api'

export type Stage = 'Ideation' | 'Coding' | 'Backtesting' | 'Paper Trading' | 'Live Eligible' | 'Live' | 'Deprecated'
export const stages: Stage[] = ['Ideation', 'Coding', 'Backtesting', 'Paper Trading', 'Live Eligible', 'Live', 'Deprecated']

const STAGE_LABELS: Record<string, Stage> = {
  Ideation: 'Ideation',
  Coding: 'Coding',
  Backtesting: 'Backtesting',
  PaperTrading: 'Paper Trading',
  LiveEligible: 'Live Eligible',
  Live: 'Live',
  Deprecated: 'Deprecated',
}

export function stageOf(strategy: Strategy): Stage {
  return STAGE_LABELS[strategy.status] ?? 'Ideation'
}

export function Badge({ children, tone = 'cyan' }: { children: React.ReactNode; tone?: string }) {
  return <span className={`strategy-badge tone-${tone}`}>{children}</span>
}
