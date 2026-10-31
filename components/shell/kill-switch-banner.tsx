'use client'

import { useState } from 'react'
import { ShieldAlert } from 'lucide-react'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { type KillSwitchMode, type KillSwitchState, type Role, resetKillSwitch } from '@/lib/api'
import { can } from '@/lib/permissions'
import { useConsoleState } from './console-state'

const MODES: KillSwitchMode[] = ['paper', 'live']

function percent(value: number | null): string {
  return value === null ? 'not recorded' : `${value.toFixed(1)}%`
}

function Tripped({ mode, state, canReset, onReset }: {
  mode: KillSwitchMode
  state: KillSwitchState
  canReset: boolean
  onReset: () => void
}) {
  return (
    <div className="kill-banner-row">
      <div>
        <strong>The {mode} kill switch is tripped. {mode === 'live' ? 'No live order will be sent' : 'No paper order will be placed'} until it is reset.</strong>
        <small>
          {state.trip_reason ?? 'No reason was recorded.'} Drawdown {percent(state.last_drawdown_pct)} against a {percent(state.threshold_pct)} limit
          {state.tripped_at ? `, since ${new Date(state.tripped_at).toLocaleString()}` : ''}.
        </small>
      </div>
      {canReset && (
        <button type="button" className="kill-switch-reset-btn" onClick={onReset}>
          Reset {mode} kill switch
        </button>
      )}
    </div>
  )
}

/**
 * On every page: a tripped kill switch, with why and since when, and a reset that asks for a reason
 * (kept in the audit log) and a typed confirmation. When the state cannot be read it says so, instead
 * of looking like nothing is wrong.
 */
export function KillSwitchBanner({ role }: { role: Role }) {
  const { killSwitch, loading, refresh } = useConsoleState()
  const [resetting, setResetting] = useState<KillSwitchMode | null>(null)
  const canReset = can(role, 'POST /api/v1/kill-switch/{mode}/reset')

  const tripped = MODES.filter((mode) => killSwitch[mode]?.tripped)
  const unreadable = MODES.filter((mode) => killSwitch[mode] === null)

  return (
    <>
      {tripped.length > 0 && (
        <div className="kill-banner" role="alert">
          <ShieldAlert aria-hidden="true" />
          <div>
            {tripped.map((mode) => (
              <Tripped
                key={mode}
                mode={mode}
                state={killSwitch[mode] as KillSwitchState}
                canReset={canReset}
                onReset={() => setResetting(mode)}
              />
            ))}
          </div>
        </div>
      )}
      {!loading && unreadable.length > 0 && (
        <div className="kill-banner kill-banner-unknown" role="status">
          <ShieldAlert aria-hidden="true" />
          <div>
            <strong>The console cannot read the {unreadable.join(' and ')} kill switch, so it does not know whether trading is blocked.</strong>
            <small>It keeps trying every 30 seconds. If this lasts, check that the server is up.</small>
          </div>
        </div>
      )}
      {resetting && (
        <ConfirmDialog
          title={`Reset the ${resetting} kill switch`}
          description={`This lets the ${resetting} engine place orders again. Check why it tripped first: the reset is recorded with your name and the reason you give.`}
          confirmLabel={`Reset ${resetting} kill switch`}
          busyLabel="Resetting…"
          tone="danger"
          phrase={`RESET ${resetting.toUpperCase()}`}
          reason={{ label: 'Why is it safe to resume?', hint: 'Kept in the audit log.', minLength: 5 }}
          onConfirm={async (reason) => {
            try {
              await resetKillSwitch(resetting, reason)
            } finally {
              // e.g. 409 when someone else already reset it: show the real state either way
              await refresh()
            }
          }}
          onClose={() => setResetting(null)}
        />
      )}
    </>
  )
}
