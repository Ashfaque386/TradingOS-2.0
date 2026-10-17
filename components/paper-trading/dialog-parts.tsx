'use client'

import { useId } from 'react'
import { ApiError, type Strategy } from '@/lib/api'

export function messageOf(error: unknown, fallback: string): string {
  return error instanceof ApiError || error instanceof Error ? error.message : fallback
}

/** The strategy version a new subscription is attached to: the newest version that passed static
 * validation, or the newest at all. */
export function latestVersionId(strategy: Strategy): string | null {
  const versions = [...strategy.versions].sort((a, b) => b.version_number - a.version_number)
  return (versions.find((v) => v.static_validation_passed) ?? versions[0])?.id ?? null
}

export function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string
  hint?: string
  error?: string
  children: (id: string) => React.ReactNode
}) {
  const id = useId()
  return (
    <div className="pt-field">
      <label htmlFor={id}>{label}</label>
      {children(id)}
      {error ? <small className="pt-field-error" role="alert">{error}</small> : hint ? <small>{hint}</small> : null}
    </div>
  )
}

export function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: T
  options: { value: T; label: string }[]
  onChange: (value: T) => void
}) {
  return (
    <div className="pt-field" role="radiogroup" aria-label={label}>
      <span className="pt-legend">{label}</span>
      <div className="pt-seg">
        {options.map((option) => (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={value === option.value}
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>
    </div>
  )
}
