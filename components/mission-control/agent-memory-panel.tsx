'use client'

import { FormEvent, useCallback, useEffect, useState } from 'react'
import { AlertCircle, Brain, Plus, RefreshCw, Search, Trash2 } from 'lucide-react'
import {
  type MemoryHit,
  type MemoryKind,
  type MemoryStatus,
  ApiError,
  forgetMemory,
  getMemoryStatus,
  queryMemory,
  rememberMemory,
} from '@/lib/api'

// Agent long-term vector memory (backlog item 18). What the pipeline recalls
// into its prompts before a run, and records after it (rejections, outcomes),
// is browsable and editable here. When no Qdrant server or embedding provider
// is configured the panel says exactly what is missing, taken straight from
// the backend's status -- nothing is simulated.

const KINDS: MemoryKind[] = ['strategy', 'news', 'organization']

const errorText = (err: unknown, fallback: string) => (err instanceof ApiError ? err.message : fallback)

export function AgentMemoryPanel({ canWrite, canDelete }: { canWrite: boolean; canDelete: boolean }) {
  const [status, setStatus] = useState<MemoryStatus | null>(null)
  const [statusError, setStatusError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [queryKind, setQueryKind] = useState<MemoryKind | ''>('')
  const [hits, setHits] = useState<MemoryHit[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [newKind, setNewKind] = useState<MemoryKind>('strategy')
  const [newText, setNewText] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await getMemoryStatus())
      setStatusError(null)
    } catch (err) {
      setStatusError(errorText(err, 'Could not read memory status'))
    }
  }, [])

  useEffect(() => {
    void refreshStatus()
  }, [refreshStatus])

  async function runSearch(text: string) {
    setSearching(true)
    setError(null)
    try {
      setHits(await queryMemory(text, queryKind || undefined))
    } catch (err) {
      setHits(null)
      setError(errorText(err, 'Search failed'))
    } finally {
      setSearching(false)
    }
  }

  function handleSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (query.trim()) void runSearch(query.trim())
  }

  async function handleRemember(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!newText.trim()) return
    setSaving(true)
    setError(null)
    try {
      await rememberMemory(newKind, newText.trim())
      setNewText('')
      await refreshStatus()
    } catch (err) {
      setError(errorText(err, 'Could not save the memory'))
    } finally {
      setSaving(false)
    }
  }

  async function handleForget(hit: MemoryHit) {
    setError(null)
    try {
      await forgetMemory(hit.kind, hit.id)
      setHits((prev) => (prev ? prev.filter((h) => h.id !== hit.id) : prev))
      await refreshStatus()
    } catch (err) {
      setError(errorText(err, 'Could not delete the memory'))
    }
  }

  const available = status?.available === true

  return (
    <section className="pulse-panel flex flex-col overflow-hidden" aria-label="Agent memory">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/8 p-4">
        <div>
          <p className="eyebrow flex items-center gap-2">
            <Brain className="size-3 text-cyan-300" />
            AGENT MEMORY · LONG-TERM
            {status && (
              <span className="font-mono text-[9px] tracking-wider text-muted-foreground">
                {available ? 'ONLINE' : 'UNAVAILABLE'}
              </span>
            )}
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            {available
              ? `Lessons the pipeline recalls before a run and records after it · ${status?.embedding_provider} / ${status?.embedding_model}`
              : 'Vector memory the agents recall from and write to across runs'}
          </p>
        </div>
        <button type="button" onClick={() => void refreshStatus()} className="button-secondary" aria-label="Refresh memory status">
          <RefreshCw className="size-3" />
        </button>
      </div>

      {statusError && <p className="px-4 pt-3 text-xs text-rose-300">{statusError}</p>}

      {status && !available && (
        <div className="m-4 flex items-start gap-2 rounded-xl border border-amber-300/20 bg-amber-300/[.06] p-3 text-xs text-amber-100" role="status">
          <AlertCircle className="mt-0.5 size-4 shrink-0" />
          <div>
            <p>Memory is not available. Pipeline runs proceed without recalled lessons.</p>
            <p className="mt-1 font-mono text-[10px] text-amber-100/80">{status.reason}</p>
          </div>
        </div>
      )}

      {available && (
        <>
          <div className="flex flex-wrap gap-2 border-b border-white/5 px-4 py-3 text-[10px]" aria-label="Memory collections">
            {status?.collections.map((c) => (
              <span key={c.kind} className="rounded-md border border-white/10 px-2 py-1 font-mono text-muted-foreground">
                {c.kind} <span className="text-foreground">{c.count}</span>
                {c.dimension ? ` · ${c.dimension}d` : ''}
              </span>
            ))}
          </div>

          <form onSubmit={handleSearch} className="flex flex-wrap items-center gap-2 border-b border-white/5 px-4 py-3">
            <Search className="size-4 shrink-0 text-cyan-300" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search what the agents remember..."
              aria-label="Memory search"
              className="min-w-0 flex-1 bg-transparent text-sm text-foreground outline-none placeholder:text-muted-foreground"
            />
            <select
              value={queryKind}
              onChange={(e) => setQueryKind(e.target.value as MemoryKind | '')}
              aria-label="Memory kind filter"
              className="rounded-md border border-white/10 bg-transparent px-2 py-1 text-xs"
            >
              <option value="">all kinds</option>
              {KINDS.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
            <button type="submit" disabled={searching || !query.trim()} className="button-primary">
              {searching ? <RefreshCw className="size-3 animate-spin" /> : <Search className="size-3" />}
              Search
            </button>
          </form>

          {canWrite && (
            <form onSubmit={handleRemember} className="flex flex-wrap items-center gap-2 border-b border-white/5 px-4 py-3">
              <Plus className="size-4 shrink-0 text-cyan-300" />
              <input
                value={newText}
                onChange={(e) => setNewText(e.target.value)}
                placeholder="Add a note for the agents to remember..."
                aria-label="New memory"
                className="min-w-0 flex-1 bg-transparent text-sm text-foreground outline-none placeholder:text-muted-foreground"
              />
              <select
                value={newKind}
                onChange={(e) => setNewKind(e.target.value as MemoryKind)}
                aria-label="New memory kind"
                className="rounded-md border border-white/10 bg-transparent px-2 py-1 text-xs"
              >
                {KINDS.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
              <button type="submit" disabled={saving || !newText.trim()} className="button-primary">
                {saving ? <RefreshCw className="size-3 animate-spin" /> : <Plus className="size-3" />}
                Remember
              </button>
            </form>
          )}

          {error && <p className="px-4 pt-3 text-xs text-rose-300">{error}</p>}

          <div className="max-h-[260px] overflow-y-auto" aria-label="Memory results">
            {hits === null && <div className="p-4 text-xs text-muted-foreground">Search to see the closest memories.</div>}
            {hits?.length === 0 && <div className="p-4 text-xs text-muted-foreground">Nothing remembered that is close to that yet.</div>}
            {hits?.map((hit) => (
              <div key={hit.id} className="flex items-start gap-3 border-b border-white/5 px-4 py-2.5" data-memory-kind={hit.kind}>
                <div className="min-w-0 flex-1">
                  <p className="text-xs text-foreground">{hit.text}</p>
                  <p className="mt-0.5 font-mono text-[10px] text-muted-foreground">
                    {hit.kind} · similarity {hit.score.toFixed(2)}
                    {typeof hit.metadata.event === 'string' ? ` · ${hit.metadata.event}` : ''}
                  </p>
                </div>
                {canDelete && (
                  <button type="button" onClick={() => void handleForget(hit)} className="button-secondary" aria-label="Delete memory">
                    <Trash2 className="size-3" />
                  </button>
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  )
}
