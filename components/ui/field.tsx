'use client'

import { useId } from 'react'

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
    <div className="ui-field">
      <label htmlFor={id}>{label}</label>
      {children(id)}
      {error ? <small className="ui-field-error" role="alert">{error}</small> : hint ? <small>{hint}</small> : null}
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
    <div className="ui-field" role="radiogroup" aria-label={label}>
      <span className="ui-legend">{label}</span>
      <div className="ui-seg">
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
