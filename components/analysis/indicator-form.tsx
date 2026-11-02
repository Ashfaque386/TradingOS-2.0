'use client'

import { useState } from 'react'
import { Field } from '@/components/ui/field'
import {
  DEFAULT_PARAMS,
  INDICATOR_FIELDS,
  type IndicatorParams,
  draftOf,
  indicatorProblems,
  isDefault,
  readParams,
} from '@/lib/indicator-params'

/**
 * The periods the indicators are computed with. What is typed is held back until it is something the
 * server will take (each box says what is wrong with it), and Apply sends it; Reset puts the usual
 * periods back. The chart titles below read the parameters the server answered with, so what they say
 * is what was drawn.
 */
export function IndicatorForm({ applied, onApply }: { applied: IndicatorParams; onApply: (params: IndicatorParams) => void }) {
  const [draft, setDraft] = useState(() => draftOf(applied))
  const problems = indicatorProblems(draft)
  const parsed = readParams(draft)
  const changed = parsed !== null && INDICATOR_FIELDS.some((f) => parsed[f.key] !== applied[f.key])
  const atDefaults = isDefault(applied) && INDICATOR_FIELDS.every((f) => draft[f.key] === String(DEFAULT_PARAMS[f.key]))

  const apply = (event: React.FormEvent) => {
    event.preventDefault()
    if (parsed && changed) onApply(parsed)
  }
  const reset = () => {
    setDraft(draftOf(DEFAULT_PARAMS))
    onApply(DEFAULT_PARAMS)
  }

  return (
    <form className="indicator-form" onSubmit={apply} aria-label="Indicator settings" noValidate>
      {INDICATOR_FIELDS.map((field) => (
        <Field key={field.key} label={field.label} hint={field.hint} error={problems[field.key]}>
          {(id) => (
            <input
              id={id}
              inputMode={field.integer ? 'numeric' : 'decimal'}
              value={draft[field.key]}
              aria-invalid={problems[field.key] ? true : undefined}
              onChange={(e) => setDraft((d) => ({ ...d, [field.key]: e.target.value }))}
            />
          )}
        </Field>
      ))}
      <div className="indicator-form-actions">
        <button type="submit" className="button-primary" disabled={!parsed || !changed}>Apply</button>
        <button type="button" className="button-secondary" onClick={reset} disabled={atDefaults}>
          Reset to defaults
        </button>
      </div>
    </form>
  )
}
