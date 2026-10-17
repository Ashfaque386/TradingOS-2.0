'use client'

import { useEffect, useId, useRef, useState } from 'react'
import { type PaperSymbolHit, searchPaperSymbols } from '@/lib/api'

const DEBOUNCE_MS = 250

/** A symbol field that searches the connected brokers' own instrument lists as you type. It is
 * still a text field: with no broker connected (nothing to search) or no match, what is typed is
 * what is used, and it says so. */
export function SymbolPicker({
  id,
  value,
  onChange,
  kindHint,
  onSettled,
}: {
  id: string
  value: string
  onChange: (value: string) => void
  /** `index` narrows the hint for futures and options, which trade on an underlying. */
  kindHint: 'equity' | 'underlying'
  /** `false` while what is typed is still only part of a name the search is offering. */
  onSettled?: (settled: boolean) => void
}) {
  const listId = useId()
  const [hits, setHits] = useState<PaperSymbolHit[]>([])
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(-1)
  const [searchable, setSearchable] = useState<{ ok: boolean; reason: string | null } | null>(null)
  const [searching, setSearching] = useState(false)
  const latest = useRef(0)
  // Picking a suggestion changes `value`, which must not search (and reopen the list) again.
  const picked = useRef<string | null>(null)

  useEffect(() => {
    const query = value.trim()
    if (query.length === 0 || picked.current === value) {
      setHits([])
      setSearching(false)
      return
    }
    const ticket = ++latest.current
    setSearching(true)
    const timer = setTimeout(() => {
      searchPaperSymbols(query)
        .then((result) => {
          if (ticket !== latest.current) return // a newer search superseded this one
          setSearchable({ ok: result.available, reason: result.reason })
          setHits(result.symbols)
          setActive(-1)
        })
        .catch(() => {
          if (ticket !== latest.current) return
          setSearchable({ ok: false, reason: 'Symbol search is unavailable right now.' })
          setHits([])
        })
        .finally(() => {
          if (ticket === latest.current) setSearching(false)
        })
    }, DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [value])

  // The text is "settled" unless the broker's list offers names it is only the start of: that is
  // someone still typing, and checking a half-typed name would only report it as unlisted.
  const exact = hits.some((hit) => hit.symbol === value.trim().toUpperCase())
  const partial = searchable?.ok === true && hits.length > 0 && !exact && picked.current !== value
  const settledNow = !partial && !searching
  useEffect(() => {
    onSettled?.(settledNow)
  }, [settledNow, onSettled])

  const choose = (hit: PaperSymbolHit) => {
    picked.current = hit.symbol
    onChange(hit.symbol)
    setOpen(false)
    setHits([])
  }

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setOpen(true)
      setActive((index) => (hits.length === 0 ? -1 : (index + 1) % hits.length))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActive((index) => (hits.length === 0 ? -1 : (index <= 0 ? hits.length : index) - 1))
    } else if (event.key === 'Enter' && open && active >= 0 && hits[active]) {
      event.preventDefault() // choose the suggestion; do not submit the form
      choose(hits[active])
    } else if (event.key === 'Escape' && open) {
      event.stopPropagation() // close the list, not the dialog around it
      setOpen(false)
    }
  }

  const showList = open && hits.length > 0
  const noMatch = searchable?.ok && !searching && value.trim() !== '' && hits.length === 0 && picked.current !== value
  const status = searching
    ? 'Searching…'
    : showList
      ? `${hits.length} ${hits.length === 1 ? 'match' : 'matches'}`
      : noMatch
        ? 'No listed symbol matches that.'
        : searchable && !searchable.ok
          ? `${searchable.reason ?? 'Symbol search is unavailable.'} Type the symbol.`
          : kindHint === 'equity'
            ? 'NSE symbol, for example RELIANCE'
            : 'For example NIFTY or RELIANCE'

  return (
    <div className="pt-combo">
      <input
        id={id}
        className="pt-input"
        role="combobox"
        aria-expanded={showList}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={active >= 0 ? `${listId}-${active}` : undefined}
        value={value}
        autoComplete="off"
        spellCheck={false}
        onChange={(event) => {
          picked.current = null
          onChange(event.target.value.toUpperCase())
          setOpen(true)
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 120)}
        onKeyDown={onKeyDown}
      />
      {showList && (
        <ul id={listId} role="listbox" className="pt-combo-list" aria-label="Matching symbols">
          {hits.map((hit, index) => (
            <li
              key={hit.symbol}
              id={`${listId}-${index}`}
              role="option"
              aria-selected={index === active}
              data-active={index === active}
              onMouseDown={(event) => {
                event.preventDefault() // keep focus in the field
                choose(hit)
              }}
              onMouseEnter={() => setActive(index)}
            >
              <span>{hit.symbol}</span>
              <small>{hit.kind === 'index' ? 'Index' : 'Share'}</small>
            </li>
          ))}
        </ul>
      )}
      <small role="status" className="pt-combo-status">{status}</small>
    </div>
  )
}
