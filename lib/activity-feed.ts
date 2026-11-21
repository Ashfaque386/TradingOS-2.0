/**
 * How the console names an audit row's entity, and which rows are routine.
 *
 * The audit trail has two vocabularies for the same things: rows the audit middleware writes for
 * every mutating request take the URL segment (`broker-credentials`, `paper-trading`), and rows the
 * domain code writes itself take its own name (`broker_credentials`, `paper_trading_subscription`).
 * Both are permanent (the log is append-only), so they are joined here, for display, under one label.
 * The Audit Log page shows the stored value in its table; its filter offers these labels and asks for
 * every spelling behind one.
 *
 * Routine rows are sign-ins and inbound webhook deliveries (Alertmanager, Telegram...): real, kept in
 * the Audit Log, but on 2026-11-21 they were most of what the Overview feed showed.
 */

/** The fields of an audit row these helpers read. The columns are nullable in the database. */
export interface FeedRow {
  actor: string
  action: string
  entity_type: string | null
  entity_id: string | null
}

const ENTITY_LABELS: Record<string, string> = {
  'broker-credentials': 'Broker credentials',
  broker_credentials: 'Broker credentials',
  'notification-channels': 'Notification channels',
  notification_channel: 'Notification channels',
  'paper-trading': 'Paper trading',
  paper_trading_subscription: 'Paper trading',
  'live-trading': 'Live trading',
  live_trading_subscription: 'Live trading',
  live_order_intent: 'Live trading',
  trade: 'Live trading',
  'risk-limits': 'Risk limits',
  'risk-limit': 'Risk limits',
  risk: 'Risk limits',
  strategies: 'Strategies',
  strategy: 'Strategies',
  'kill-switch': 'Kill switch',
  gateway: 'Agent Gateway',
  agent_config_version: 'Agent Gateway',
  agents: 'Agents',
  prompt_version: 'Agents',
  settings: 'LLM providers',
  llm_provider_credentials: 'LLM providers',
  system: 'System',
  scheduled_job: 'Scheduled jobs',
  jwt_signing_key: 'Signing key',
  auth: 'Sign-ins',
  admin: 'Users',
  user: 'Users',
  audit: 'Audit',
  audit_log: 'Audit',
  webhooks: 'Webhooks',
  'operator-guidance': 'Operator guidance',
  operator_guidance: 'Operator guidance',
  'shadow-mode': 'Shadow mode',
  broker_adapter: 'Shadow mode',
  'engine-alerts': 'Engine alerts',
  'investor-reports': 'Investor reports',
  orchestration: 'Runs',
  'market-data': 'Market data',
}

/** The console's name for an entity type, whichever spelling the row was written with. */
export function entityLabel(entityType: string | null | undefined): string {
  if (!entityType) return 'Other'
  const known = ENTITY_LABELS[entityType]
  if (known) return known
  const words = entityType.replace(/[-_]+/g, ' ').trim()
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : 'Other'
}

/** One option per label, standing for every stored spelling behind it (sorted by label). */
export function entityOptions(entityTypes: string[]): { label: string; values: string[] }[] {
  const byLabel = new Map<string, string[]>()
  for (const type of entityTypes) {
    const label = entityLabel(type)
    byLabel.set(label, [...(byLabel.get(label) ?? []), type])
  }
  return [...byLabel.entries()]
    .map(([label, values]) => ({ label, values: [...values].sort() }))
    .sort((a, b) => a.label.localeCompare(b.label))
}

const ROUTINE_ENTITIES = new Set(['auth', 'webhooks'])

/** A sign-in, sign-out, registration or inbound webhook delivery. */
export function isRoutineEvent(event: Pick<FeedRow, 'entity_type' | 'action'>): boolean {
  if (event.entity_type && ROUTINE_ENTITIES.has(event.entity_type)) return true
  return /^POST \/api\/v1\/(auth\/(login|logout|register)|webhooks\/)/.test(event.action)
}

/** What an activity row says: who, what, and the entity in the console's words. */
export function describeEvent(event: FeedRow): string {
  const entity = event.entity_type ? entityLabel(event.entity_type) : ''
  return [`${event.actor} · ${event.action}`, entity, event.entity_id ?? ''].filter(Boolean).join(' ')
}
