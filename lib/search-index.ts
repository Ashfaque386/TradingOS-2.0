export type SearchEntity = {
  kind: 'strategy' | 'order' | 'agent'
  name: string
  detail: string
  href: string
}

export const NAV_TARGETS = [
  { label: 'Overview', href: '/', keywords: 'home pulse organization' },
  { label: 'Mission Control', href: '/mission-control', keywords: 'kanban sign-off queue tasks' },
  { label: 'Chat', href: '/chat', keywords: 'ceo agent conversation ask' },
  { label: 'Agent Fleet', href: '/agent-fleet', keywords: 'agents org tree' },
  { label: 'Strategies', href: '/strategies', keywords: 'strategy board review' },
  { label: 'Backtests', href: '/backtests', keywords: 'backtest monte carlo equity' },
  { label: 'Orders & Trades', href: '/orders', keywords: 'orders trades ledger fills' },
  { label: 'Paper Desk', href: '/paper-trading', keywords: 'paper trading subscriptions futures options legs costs positions' },
  { label: 'Portfolio', href: '/portfolio', keywords: 'allocation recommendations risk metrics' },
  { label: 'Market Analysis', href: '/analysis', keywords: 'vix sectors option chain indicators' },
  { label: 'Investor Reports', href: '/investor-reports', keywords: 'report weekly monthly performance' },
  { label: 'Audit Log', href: '/audit', keywords: 'audit hash chain integrity' },
  { label: 'Settings', href: '/settings', keywords: 'config credentials skills risk limits' },
] as const
