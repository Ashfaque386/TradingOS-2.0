import type { Role } from './api'
import { type Action, can } from './permissions'

export type NavTarget = {
  label: string
  href: string
  keywords: string
  /** The call the page is built on; a role that may not make it is not offered the page. */
  requires?: Action
}

export type SearchEntity = {
  kind: 'strategy' | 'order' | 'agent'
  name: string
  detail: string
  href: string
}

export const NAV_TARGETS: readonly NavTarget[] = [
  { label: 'Overview', href: '/', keywords: 'home pulse organization' },
  { label: 'Mission Control', href: '/mission-control', keywords: 'kanban sign-off queue tasks' },
  { label: 'Chat', href: '/chat', keywords: 'ceo agent conversation ask' },
  { label: 'Agent Fleet', href: '/agent-fleet', keywords: 'agents org tree' },
  { label: 'Strategies', href: '/strategies', keywords: 'strategy board review' },
  { label: 'Backtests', href: '/backtests', keywords: 'backtest monte carlo equity' },
  { label: 'Orders & Trades', href: '/orders', keywords: 'orders trades ledger fills' },
  { label: 'Paper Desk', href: '/paper-trading', keywords: 'paper trading subscriptions futures options legs costs positions' },
  { label: 'Portfolio', href: '/portfolio', keywords: 'allocation recommendations accept reject risk metrics' },
  { label: 'Market Analysis', href: '/analysis', keywords: 'vix sectors option chain indicators holidays calendar trading days data lake providers' },
  { label: 'Investor Reports', href: '/investor-reports', keywords: 'report weekly monthly performance download markdown' },
  { label: 'Audit Log', href: '/audit', keywords: 'audit hash chain integrity', requires: 'GET /api/v1/audit/entries' },
  { label: 'Settings', href: '/settings', keywords: 'config credentials skills risk limits' },
]

/** The pages this role may use: the Audit Log, for one, is for the auditor and the administrator. */
export function navFor(role: Role | null | undefined): readonly NavTarget[] {
  return NAV_TARGETS.filter((target) => !target.requires || can(role, target.requires))
}
