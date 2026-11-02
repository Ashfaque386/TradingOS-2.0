'use client'

import { FormEvent, useEffect, useRef, useState } from 'react'
import { Check, Download, MessageSquare, Pencil, Pin, PinOff, Plus, Search, Send, Square, Trash2, X } from 'lucide-react'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { ErrorNotice } from '@/components/ui/error-notice'
import {
  type ChatMessage,
  type ChatSession,
  type LlmProviderStatus,
  abortChatMessage,
  createChatSession,
  deleteChatSession,
  downloadChatExport,
  listChatMessages,
  listChatSessions,
  listLlmProviderStatus,
  streamChatMessage,
  updateChatSession,
} from '@/lib/api'
import { providerChoices, titleProblem } from '@/lib/chat-view'
import { messageOf } from '@/lib/errors'
import { indiaStamp } from '@/lib/time'

// Phase 22 (docs/phase20-old-vs-new-comparison.md item 16): a pure
// frontend build over src/api/routes/chat.py, which has been real since
// well before this page existed -- session CRUD, search, pin, a
// per-session model switch, streaming replies, abort, and export.
// Phase 9 slice 7b: rename, the model switch, a confirmation before a delete, a JSON export, a
// failed list said as such, and a conversation a screen reader is told about as it grows.

let pendingIdCounter = 0

export default function ChatPage() {
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [sessionsError, setSessionsError] = useState<string | null>(null)
  const [sessionsLoaded, setSessionsLoaded] = useState(false)
  const [search, setSearch] = useState('')
  const [pinnedOnly, setPinnedOnly] = useState(false)
  const [activeId, setActiveId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [draft, setDraft] = useState('')
  const [streamingText, setStreamingText] = useState<string | null>(null)
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [renaming, setRenaming] = useState(false)
  const [titleDraft, setTitleDraft] = useState('')
  const [deleting, setDeleting] = useState<ChatSession | null>(null)
  const [providers, setProviders] = useState<LlmProviderStatus[] | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  async function reloadSessions() {
    try {
      setSessions(await listChatSessions({ search: search || undefined, pinnedOnly }))
      setSessionsError(null)
      setSessionsLoaded(true)
    } catch (err) {
      setSessionsError(messageOf(err, 'the request failed'))
    }
  }

  useEffect(() => {
    reloadSessions()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, pinnedOnly])

  // Which providers have a key saved, to mark the ones a chat pinned to them would skip. Only a hint:
  // when it cannot be read, the list is the same without the marks.
  useEffect(() => {
    listLlmProviderStatus().then(setProviders).catch(() => setProviders(null))
  }, [])

  useEffect(() => {
    if (!activeId) {
      setMessages([])
      return
    }
    listChatMessages(activeId)
      .then(setMessages)
      .catch((err) => {
        setMessages([])
        setError(messageOf(err, 'Could not load this conversation'))
      })
  }, [activeId])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, streamingText])

  async function handleNewChat() {
    try {
      const session = await createChatSession({})
      await reloadSessions()
      setActiveId(session.id)
    } catch (err) {
      setError(messageOf(err, 'Could not start a new chat'))
    }
  }

  async function handleSend(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const userText = draft.trim()
    if (!userText || streaming) return

    let sessionId = activeId
    if (!sessionId) {
      try {
        const session = await createChatSession({ title: userText.slice(0, 60) })
        await reloadSessions()
        setActiveId(session.id)
        sessionId = session.id
      } catch (err) {
        setError(messageOf(err, 'Could not start a new chat'))
        return
      }
    }

    setDraft('')
    setMessages((prev) => [
      ...prev,
      {
        id: `pending-${pendingIdCounter++}`,
        session_id: sessionId!,
        role: 'user',
        content: userText,
        provider: null,
        aborted: false,
        created_at: new Date().toISOString(),
      },
    ])
    setStreaming(true)
    setStreamingText('')
    setError(null)
    try {
      await streamChatMessage(sessionId, userText, (chunk) => {
        setStreamingText((prev) => (prev ?? '') + chunk.text)
      })
    } catch (err) {
      setError(messageOf(err, 'Failed to send message'))
    } finally {
      setStreaming(false)
      setStreamingText(null)
      try {
        setMessages(await listChatMessages(sessionId))
      } catch {
        // session may have just been deleted from another tab -- leave
        // the optimistic messages in place rather than clearing the thread.
      }
      reloadSessions()
    }
  }

  async function handleAbort() {
    if (!activeId) return
    try {
      await abortChatMessage(activeId)
    } catch (err) {
      setError(messageOf(err, 'Could not stop the reply'))
    }
  }

  async function handleTogglePin(session: ChatSession) {
    try {
      await updateChatSession(session.id, { pinned: !session.pinned })
    } catch (err) {
      setError(messageOf(err, 'Could not pin or unpin that chat'))
    }
    reloadSessions()
  }

  // Confirmed in the dialog, which reports a failure by the error this throws.
  async function confirmDelete(session: ChatSession) {
    await deleteChatSession(session.id)
    if (activeId === session.id) setActiveId(null)
    await reloadSessions()
  }

  async function handleExport(session: ChatSession, format: 'markdown' | 'json') {
    setError(null)
    try {
      await downloadChatExport(session.id, format)
    } catch (err) {
      setError(messageOf(err, 'Could not export that chat'))
    }
  }

  const active = sessions.find((s) => s.id === activeId) ?? null
  const titleError = renaming ? titleProblem(titleDraft) : null

  function startRename(session: ChatSession) {
    setTitleDraft(session.title)
    setRenaming(true)
  }

  async function saveTitle(event: FormEvent) {
    event.preventDefault()
    if (!active || titleProblem(titleDraft)) return
    try {
      await updateChatSession(active.id, { title: titleDraft.trim() })
      setRenaming(false)
      await reloadSessions()
    } catch (err) {
      setError(messageOf(err, 'Could not rename that chat'))
    }
  }

  async function switchModel(session: ChatSession, value: string) {
    setError(null)
    try {
      await updateChatSession(session.id, value === '' ? { clear_model: true } : { model: value })
    } catch (err) {
      setError(messageOf(err, 'Could not change which provider answers'))
    }
    await reloadSessions()
  }

  return (
    <>
      <div className="mx-auto flex w-full max-w-[1700px] flex-col gap-5">
        <header className="mission-hero">
          <div>
            <p className="eyebrow flex items-center gap-2">
              <MessageSquare className="size-3 text-cyan-300" />
              TRADINGOS // CHAT
            </p>
            <h1 className="mt-2 text-3xl font-semibold tracking-[-.04em]">Chat with the CEO Agent</h1>
            <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
              A direct line to the same LLM-routed agent organization driving strategy generation
              and orchestration elsewhere in this console -- real streamed replies, not a scripted
              demo.
            </p>
          </div>
        </header>

        <div className="grid gap-5 xl:grid-cols-[320px_1fr]">
          <section className="pulse-panel flex flex-col overflow-hidden">
            <div className="flex flex-col gap-2 border-b border-white/8 p-3">
              <button onClick={handleNewChat} className="button-primary justify-center">
                <Plus className="size-3.5" />
                New chat
              </button>
              <div className="flex items-center gap-2 rounded-lg border border-white/10 bg-black/20 px-2 py-1.5">
                <Search className="size-3.5 shrink-0 text-muted-foreground" />
                <input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search sessions"
                  className="w-full bg-transparent text-xs text-foreground outline-none"
                />
              </div>
              <label className="flex items-center gap-2 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={pinnedOnly}
                  onChange={(e) => setPinnedOnly(e.target.checked)}
                />
                Pinned only
              </label>
            </div>
            <div className="flex max-h-[65vh] flex-col overflow-y-auto p-2">
              {sessionsError && <ErrorNotice className="m-1" message={`Could not load your chats: ${sessionsError}`} onRetry={() => void reloadSessions()} />}
              {!sessionsError && sessionsLoaded && sessions.length === 0 && (
                <p className="p-3 text-xs text-muted-foreground">
                  {search || pinnedOnly ? 'No chat matches that.' : 'No chat sessions yet. Start one above.'}
                </p>
              )}
              {sessions.map((s) => (
                <div
                  key={s.id}
                  className={`group flex items-center gap-1 rounded-lg px-2 py-2 text-xs transition-colors ${activeId === s.id ? 'bg-cyan-400/10 text-cyan-100' : 'text-muted-foreground hover:bg-white/[.03]'}`}
                >
                  <button onClick={() => { setRenaming(false); setActiveId(s.id) }} className="flex-1 truncate text-left">
                    <strong className="block truncate text-foreground">{s.title}</strong>
                    <span className="font-mono text-[10px] uppercase tracking-wide">
                      {s.model ?? 'auto'} · {indiaStamp(s.updated_at)}
                    </span>
                  </button>
                  <button
                    onClick={() => handleTogglePin(s)}
                    className="shrink-0 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100 pointer-coarse:opacity-100"
                    aria-label={s.pinned ? 'Unpin session' : 'Pin session'}
                  >
                    {s.pinned ? (
                      <Pin className="size-3 text-amber-300" />
                    ) : (
                      <PinOff className="size-3" />
                    )}
                  </button>
                  <button
                    onClick={() => setDeleting(s)}
                    className="shrink-0 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100 pointer-coarse:opacity-100"
                    aria-label="Delete session"
                  >
                    <Trash2 className="size-3 text-rose-300" />
                  </button>
                </div>
              ))}
            </div>
          </section>

          <section className="pulse-panel flex flex-col overflow-hidden">
            <div className="flex flex-wrap items-start justify-between gap-3 border-b border-white/8 p-4">
              <div className="min-w-0">
                {renaming && active ? (
                  <form onSubmit={saveTitle} className="flex items-center gap-2">
                    <input
                      autoFocus
                      aria-label="Chat title"
                      value={titleDraft}
                      onChange={(e) => setTitleDraft(e.target.value)}
                      onKeyDown={(e) => { if (e.key === 'Escape') setRenaming(false) }}
                      className="w-72 max-w-full rounded-lg border border-white/10 bg-black/20 px-2 py-1 text-sm text-foreground"
                    />
                    <button type="submit" disabled={titleError !== null} aria-label="Save title" className="button-primary px-2 py-1"><Check className="size-3.5" /></button>
                    <button type="button" onClick={() => setRenaming(false)} aria-label="Cancel rename" className="button-secondary px-2 py-1"><X className="size-3.5" /></button>
                  </form>
                ) : (
                  <p className="eyebrow flex items-center gap-2">
                    {active ? active.title : 'NEW CHAT'}
                    {active && (
                      <button type="button" onClick={() => startRename(active)} aria-label="Rename chat" className="text-muted-foreground hover:text-foreground">
                        <Pencil className="size-3" />
                      </button>
                    )}
                  </p>
                )}
                {titleError && <p role="alert" className="mt-1 text-xs text-rose-300">{titleError}</p>}
                {active && (
                  <p className="mt-1 text-xs text-muted-foreground">
                    Created by {active.created_by} · {indiaStamp(active.created_at)} India time
                  </p>
                )}
              </div>
              {active && (
                <div className="flex flex-wrap items-center gap-2">
                  <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    Answer with
                    <select
                      aria-label="Answer with"
                      value={active.model ?? ''}
                      onChange={(e) => void switchModel(active, e.target.value)}
                      className="rounded-lg border border-white/10 bg-black/20 px-2 py-1 text-xs text-foreground"
                    >
                      {providerChoices(providers).map((choice) => (
                        <option key={choice.value} value={choice.value}>{choice.label}</option>
                      ))}
                    </select>
                  </label>
                  <button type="button" onClick={() => handleExport(active, 'markdown')} className="button-secondary">
                    <Download className="size-3" />
                    Export
                  </button>
                  <button type="button" onClick={() => handleExport(active, 'json')} className="button-secondary">
                    <Download className="size-3" />
                    Export JSON
                  </button>
                </div>
              )}
            </div>

            <div
              ref={scrollRef}
              role="log"
              aria-label="Conversation"
              aria-live="polite"
              aria-relevant="additions text"
              aria-busy={streaming}
              className="flex min-h-[50vh] max-h-[60vh] flex-1 flex-col gap-4 overflow-y-auto p-5"
            >
              {messages.length === 0 && !streamingText && (
                <p className="text-xs text-muted-foreground">No messages yet. Say hello below.</p>
              )}
              {messages.map((m) => (
                <div
                  key={m.id}
                  className={`max-w-[80%] rounded-xl px-4 py-3 text-sm leading-relaxed ${m.role === 'user' ? 'self-end bg-cyan-400/10 text-cyan-50' : 'self-start bg-white/[.04] text-foreground'}`}
                >
                  <p className="whitespace-pre-wrap break-words">{m.content}</p>
                  {m.aborted && (
                    <p className="mt-1 text-[10px] uppercase tracking-wide text-amber-300">
                      Aborted
                    </p>
                  )}
                  {m.provider && (
                    <p className="mt-1 text-[10px] uppercase tracking-wide text-muted-foreground">
                      {m.provider}
                    </p>
                  )}
                </div>
              ))}
              {streamingText !== null && (
                <div className="max-w-[80%] self-start rounded-xl bg-white/[.04] px-4 py-3 text-sm leading-relaxed text-foreground">
                  <p className="whitespace-pre-wrap break-words">
                    {streamingText}
                    <span className="animate-pulse" aria-hidden="true">▍</span>
                  </p>
                </div>
              )}
            </div>

            {error && <p role="alert" className="px-5 pb-2 text-xs text-rose-300">{error}</p>}

            <form onSubmit={handleSend} className="flex items-end gap-2 border-t border-white/8 p-4">
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    ;(e.currentTarget.form as HTMLFormElement)?.requestSubmit()
                  }
                }}
                placeholder="Message the CEO Agent..."
                aria-label="Message"
                rows={2}
                className="flex-1 resize-none rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm text-foreground outline-none"
              />
              {streaming ? (
                <button type="button" onClick={handleAbort} className="button-danger">
                  <Square className="size-3.5" />
                  Stop
                </button>
              ) : (
                <button type="submit" disabled={!draft.trim()} className="button-primary">
                  <Send className="size-3.5" />
                  Send
                </button>
              )}
            </form>
          </section>
        </div>
      </div>
      {deleting && (
        <ConfirmDialog
          title="Delete this chat?"
          description={`“${deleting.title}” and every message in it are deleted, for everyone who can see chats. This cannot be undone: export it first if you want a copy.`}
          tone="danger"
          confirmLabel="Delete"
          busyLabel="Deleting…"
          onConfirm={() => confirmDelete(deleting)}
          onClose={() => setDeleting(null)}
        />
      )}
    </>
  )
}
