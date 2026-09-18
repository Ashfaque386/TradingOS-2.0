export type SearchEntity = {
  kind: 'strategy' | 'order' | 'agent'
  name: string
  detail: string
  href: string
}

export const NAV_TARGETS = [
  { label: 'Overview', href: '/', keywords: 'home pulse organization' },
  { label: 'Mission Control', href: '/mission-control', keywords: 'kanban sign-off queue tasks' },
  { label: 'Agent Fleet', href: '/agent-fleet', keywords: 'agents org tree' },
  { label: 'Strategies', href: '/strategies', keywords: 'strategy board review' },
  { label: 'Backtests', href: '/backtests', keywords: 'backtest monte carlo equity' },
  { label: 'Orders & Trades', href: '/orders', keywords: 'orders trades ledger fills' },
  { label: 'Market Analysis', href: '/analysis', keywords: 'vix sectors option chain indicators' },
  { label: 'Audit Log', href: '/audit', keywords: 'audit hash chain integrity' },
  { label: 'Settings', href: '/settings', keywords: 'config credentials skills risk limits' },
] as const
