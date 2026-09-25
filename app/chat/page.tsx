'use client'

import { FormEvent, useEffect, useRef, useState } from 'react'
import { Download, MessageSquare, Pin, PinOff, Plus, Search, Send, Square, Trash2 } from 'lucide-react'
import { ShellLayout } from '@/components/shell/shell-layout'
import {
  type ChatMessage,
  type ChatSession,
  ApiError,
  abortChatMessage,
  chatExportUrl,
  createChatSession,
  deleteChatSession,
  listChatMessages,
  listChatSessions,
  streamChatMessage,
  updateChatSession,
} from '@/lib/api'

// Phase 22 (docs/phase20-old-vs-new-comparison.md item 16): a pure
// frontend build over src/api/routes/chat.py, which has been real since
// well before this page existed -- session CRUD, search, pin, a
// per-session model switch, streaming replies, abort, and export.

let pendingIdCounter = 0

export default function ChatPage() {
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [search, setSearch] = useState('')
  const [pinnedOnly, setPinnedOnly] = useState(false)
  const [activeId, setActiveId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [draft, setDraft] = useState('')
  const [streamingText, setStreamingText] = useState<string | null>(null)
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  async function reloadSessions() {
    const data = await listChatSessions({ search: search || undefined, pinnedOnly })
    setSessions(data)
  }

  useEffect(() => {
    reloadSessions()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, pinnedOnly])

  useEffect(() => {
    if (!activeId) {
      setMessages([])
      return
    }
    listChatMessages(activeId)
      .then(setMessages)
      .catch(() => setMessages([]))
  }, [activeId])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, streamingText])

  async function handleNewChat() {
    const session = await createChatSession({})
    await reloadSessions()
    setActiveId(session.id)
  }

  async function handleSend(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const userText = draft.trim()
    if (!userText || streaming) return

    let sessionId = activeId
    if (!sessionId) {
      const session = await createChatSession({ title: userText.slice(0, 60) })
      await reloadSessions()
      setActiveId(session.id)
      sessionId = session.id
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
      setError(err instanceof ApiError ? err.message : 'Failed to send message')
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
    await abortChatMessage(activeId)
  }

  async function handleTogglePin(session: ChatSession) {
    await updateChatSession(session.id, { pinned: !session.pinned })
    reloadSessions()
  }

  async function handleDelete(session: ChatSession) {
    if (activeId === session.id) setActiveId(null)
    await deleteChatSession(session.id)
    reloadSessions()
  }

  const active = sessions.find((s) => s.id === activeId) ?? null

  return (
    <ShellLayout>
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
              {sessions.length === 0 && (
                <p className="p-3 text-xs text-muted-foreground">
                  No chat sessions yet. Start one above.
                </p>
              )}
              {sessions.map((s) => (
                <div
                  key={s.id}
                  className={`group flex items-center gap-1 rounded-lg px-2 py-2 text-xs transition-colors ${activeId === s.id ? 'bg-cyan-400/10 text-cyan-100' : 'text-muted-foreground hover:bg-white/[.03]'}`}
                >
                  <button onClick={() => setActiveId(s.id)} className="flex-1 truncate text-left">
                    <strong className="block truncate text-foreground">{s.title}</strong>
                    <span className="font-mono text-[10px] uppercase tracking-wide">
                      {s.model ?? 'auto'} · {new Date(s.updated_at).toLocaleString()}
                    </span>
                  </button>
                  <button
                    onClick={() => handleTogglePin(s)}
                    className="shrink-0 opacity-0 group-hover:opacity-100"
                    aria-label={s.pinned ? 'Unpin session' : 'Pin session'}
                  >
                    {s.pinned ? (
                      <Pin className="size-3 text-amber-300" />
                    ) : (
                      <PinOff className="size-3" />
                    )}
                  </button>
                  <button
                    onClick={() => handleDelete(s)}
                    className="shrink-0 opacity-0 group-hover:opacity-100"
                    aria-label="Delete session"
                  >
                    <Trash2 className="size-3 text-rose-300" />
                  </button>
                </div>
              ))}
            </div>
          </section>

          <section className="pulse-panel flex flex-col overflow-hidden">
            <div className="flex items-center justify-between border-b border-white/8 p-4">
              <div>
                <p className="eyebrow">{active ? active.title : 'NEW CHAT'}</p>
                {active && (
                  <p className="mt-1 text-xs text-muted-foreground">
                    Model: {active.model ?? 'auto'} · created by {active.created_by}
                  </p>
                )}
              </div>
              {active && (
                <a
                  href={chatExportUrl(active.id)}
                  target="_blank"
                  rel="noreferrer"
                  className="button-secondary"
                >
                  <Download className="size-3" />
                  Export
                </a>
              )}
            </div>

            <div
              ref={scrollRef}
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
                    <span className="animate-pulse">▍</span>
                  </p>
                </div>
              )}
            </div>

            {error && <p className="px-5 pb-2 text-xs text-rose-300">{error}</p>}

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
    </ShellLayout>
  )
}
