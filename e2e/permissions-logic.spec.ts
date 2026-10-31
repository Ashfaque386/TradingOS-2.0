import { expect, test } from '@playwright/test'
import { can } from '../lib/permissions'

// can() answers from the backend's own policy table (lib/permissions.generated.json, kept fresh by
// backend/tests/test_frontend_permissions.py), so these pin the lookup, not the policy itself.

test('a role may do what the policy lists for it and nothing else', () => {
  const reset = 'POST /api/v1/kill-switch/{mode}/reset'
  expect(can('RiskManager', reset)).toBe(true)
  expect(can('SystemAdministrator', reset)).toBe(true)
  expect(can('PortfolioManager', reset)).toBe(false)
  expect(can('ReadOnlyAuditor', reset)).toBe(false)
})

test('an unknown role, or none, may do nothing', () => {
  expect(can(null, 'GET /api/v1/audit/entries')).toBe(false)
  expect(can(undefined, 'GET /api/v1/audit/entries')).toBe(false)
  expect(can('Intruder' as never, 'GET /api/v1/audit/entries')).toBe(false)
})

test('the audit log is for the auditor and the administrator only', () => {
  expect(can('ReadOnlyAuditor', 'GET /api/v1/audit/entries')).toBe(true)
  expect(can('SystemAdministrator', 'GET /api/v1/audit/entries')).toBe(true)
  expect(can('PortfolioManager', 'GET /api/v1/audit/entries')).toBe(false)
})
