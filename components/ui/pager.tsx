'use client'

import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from 'lucide-react'
import { pageWindow } from '@/lib/paging'

/** First, previous, next and last for a list the server pages, with where in the list the person is.
 * `note` is for anything that explains what the page is doing (for example that it is not refreshing). */
export function Pager({
  total,
  page,
  size,
  noun,
  onPage,
  note,
}: {
  total: number
  page: number
  size: number
  /** What the rows are, plural: "executions". */
  noun: string
  onPage: (page: number) => void
  note?: string
}) {
  const view = pageWindow(total, page, size)
  const format = (n: number) => n.toLocaleString('en-IN')
  return (
    <nav className="ui-pager" aria-label={`${noun} pages`}>
      <span>
        Showing {format(view.from)}&ndash;{format(view.to)} of {format(total)} {noun}
      </span>
      {note && <span className="ui-pager-note">{note}</span>}
      <div className="ui-pager-btns">
        <button type="button" disabled={view.current === 1} onClick={() => onPage(1)} aria-label="First page">
          <ChevronsLeft className="size-3.5" />
        </button>
        <button type="button" disabled={view.current === 1} onClick={() => onPage(view.current - 1)} aria-label="Previous page">
          <ChevronLeft className="size-3.5" />
        </button>
        <span aria-current="page">
          {view.current} / {view.pages}
        </span>
        <button type="button" disabled={view.current === view.pages} onClick={() => onPage(view.current + 1)} aria-label="Next page">
          <ChevronRight className="size-3.5" />
        </button>
        <button type="button" disabled={view.current === view.pages} onClick={() => onPage(view.pages)} aria-label="Last page">
          <ChevronsRight className="size-3.5" />
        </button>
      </div>
    </nav>
  )
}
