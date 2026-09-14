/**
 * Type definitions for TradingOS mock data
 * These are shaped like future API responses for easy swap-in of real data
 */

export interface Agent {
  id: string
  name: string
  status: 'idle' | 'analyzing' | 'backtesting' | 'paper_trading' | 'error'
  skillset: string[]
  lastActive: string
  successRate: number
}

export interface OrganizationRun {
  id: string
  name: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  createdAt: string
  startedAt?: string
  completedAt?: string
  agentsInvolved: string[]
}

export interface Task {
  id: string
  title: string
  status: 'pending' | 'in_progress' | 'completed' | 'failed'
  assignedAgent: string
  priority: 'low' | 'medium' | 'high' | 'critical'
  createdAt: string
  dueAt?: string
}

export interface ApprovalRequest {
  id: string
  type: 'trade_execution' | 'strategy_deploy' | 'capital_adjustment' | 'risk_limit'
  status: 'pending' | 'approved' | 'rejected'
  requiredApprovals: number
  currentApprovals: number
  risk: number
  createdAt: string
  expiresAt: string
}

export interface LiveOrderIntent {
  id: string
  symbol: string
  quantity: number
  side: 'buy' | 'sell'
  price?: number
  orderType: 'market' | 'limit' | 'stop'
  status: 'pending_approval' | 'submitted' | 'filled' | 'cancelled'
  createdBy: string
  createdAt: string
}

export interface Strategy {
  id: string
  name: string
  description: string
  status: 'draft' | 'active' | 'paused' | 'archived'
  winRate: number
  avgReturn: number
  maxDrawdown: number
  createdAt: string
  lastModified: string
}

export interface BacktestResult {
  id: string
  strategyId: string
  dateRange: { start: string; end: string }
  totalReturn: number
  sharpeRatio: number
  maxDrawdown: number
  tradeCount: number
  winRate: number
  status: 'completed' | 'running' | 'failed'
}

export interface Order {
  id: string
  symbol: string
  quantity: number
  executedQuantity: number
  side: 'buy' | 'sell'
  price: number
  executedPrice?: number
  status: 'pending' | 'partial' | 'filled' | 'cancelled'
  createdAt: string
  updatedAt: string
}

export interface Trade {
  id: string
  entryOrder: string
  exitOrder?: string
  symbol: string
  quantity: number
  entryPrice: number
  exitPrice?: number
  pnl?: number
  pnlPercent?: number
  status: 'open' | 'closed' | 'error'
  enteredAt: string
  exitedAt?: string
}
