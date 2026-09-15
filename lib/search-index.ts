export type SearchEntity = {
  kind: 'strategy' | 'order' | 'agent'
  name: string
  detail: string
  href: string
}

// Lightweight, static index mirroring the mock data shown across the app so the
// command palette can jump to a page filtered/scoped to the matched entity.
export const SEARCH_ENTITIES: SearchEntity[] = [
  // Strategies
  { kind: 'strategy', name: 'NIFTY-Weekly-IronCondor', detail: 'Options · paper', href: '/strategies' },
  { kind: 'strategy', name: 'NIFTY-Pairs-v2', detail: 'Stat-arb · sign-off', href: '/strategies' },
  { kind: 'strategy', name: 'BANKNIFTY-ORB', detail: 'Breakout · paused', href: '/strategies' },
  { kind: 'strategy', name: 'Mean-Reversion-v4', detail: 'Reversion · live', href: '/strategies' },
  { kind: 'strategy', name: 'Momentum-Scout-v3', detail: 'Momentum · paper', href: '/strategies' },
  { kind: 'strategy', name: 'TCS-Momentum-Replay', detail: 'Momentum · review', href: '/strategies' },

  // Orders
  { kind: 'order', name: 'RELIANCE', detail: 'Equity · NSE', href: '/orders' },
  { kind: 'order', name: 'HDFCBANK', detail: 'Equity · NSE', href: '/orders' },
  { kind: 'order', name: 'INFY', detail: 'Equity · NSE', href: '/orders' },
  { kind: 'order', name: 'TCS', detail: 'Equity · NSE', href: '/orders' },
  { kind: 'order', name: 'NIFTY 24800 CE', detail: 'Option · F&O', href: '/orders' },
  { kind: 'order', name: 'BANKNIFTY 52000 PE', detail: 'Option · F&O', href: '/orders' },

  // Agents
  { kind: 'agent', name: 'Strategy Generator', detail: 'Research agent', href: '/agent-fleet' },
  { kind: 'agent', name: 'Risk Manager', detail: 'Risk agent', href: '/agent-fleet' },
  { kind: 'agent', name: 'Momentum Scout', detail: 'Signal agent', href: '/agent-fleet' },
  { kind: 'agent', name: 'Orchestrator', detail: 'Coordination agent', href: '/agent-fleet' },
  { kind: 'agent', name: 'Risk Sentinel', detail: 'Monitoring agent', href: '/agent-fleet' },
  { kind: 'agent', name: 'Alpha Engine', detail: 'Research agent', href: '/agent-fleet' },
]

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
