'use client'

import { useId, useRef } from 'react'
import { useFocusTrap } from '@/lib/use-focus-trap'

/** A dialog: labelled, Escape and a click on the backdrop close it, Tab stays inside it, and focus
 * returns to whatever opened it. `busy` blocks closing while a request is in flight. */
export function Modal({
  title,
  description,
  onClose,
  busy = false,
  wide = false,
  children,
}: {
  title: string
  description?: string
  onClose: () => void
  busy?: boolean
  wide?: boolean
  children: React.ReactNode
}) {
  const titleId = useId()
  const descriptionId = useId()
  const panel = useRef<HTMLDivElement>(null)
  const busyRef = useRef(busy)
  busyRef.current = busy

  useFocusTrap(panel, () => {
    if (!busyRef.current) onClose()
  })

  return (
    <div
      className="ui-scrim"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose()
      }}
    >
      <div
        ref={panel}
        className={`ui-modal${wide ? ' ui-modal-wide' : ''}`}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
      >
        <h2 id={titleId}>{title}</h2>
        {description && <p id={descriptionId}>{description}</p>}
        {children}
      </div>
    </div>
  )
}
