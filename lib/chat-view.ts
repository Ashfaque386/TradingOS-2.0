// What the Chat page decides: whether a title can be saved, and the providers a chat may be pinned to.
// The server is the authority (a title is trimmed and 1 to 200 characters; a provider is one the app
// knows); this says it before a request is made.

import { LLM_PROVIDERS, LLM_PROVIDER_LABELS, type LlmProviderStatus } from '@/lib/api'

export const MAX_TITLE = 200

/** Why this title cannot be saved, or null when it can. */
export function titleProblem(text: string): string | null {
  const title = text.trim()
  if (title === '') return 'A chat needs a title.'
  if (title.length > MAX_TITLE) return `A title is at most ${MAX_TITLE} characters; this is ${title.length}.`
  return null
}

export interface ProviderChoice {
  /** '' is "auto": the configured fallback order. */
  value: string
  label: string
}

/** The answers a chat may be pinned to: auto, then every provider the app knows, each marked when its
 * key or address has not been saved (a chat pinned to one still answers, from the next in the chain). */
export function providerChoices(statuses: LlmProviderStatus[] | null): ProviderChoice[] {
  const configured = statuses ? new Set(statuses.filter((s) => s.configured).map((s) => s.provider)) : null
  return [
    { value: '', label: 'Auto (the fallback order)' },
    ...LLM_PROVIDERS.map((provider) => ({
      value: provider,
      label: `${LLM_PROVIDER_LABELS[provider]}${configured && !configured.has(provider) ? ' (not configured)' : ''}`,
    })),
  ]
}

/** The file a conversation is saved as, the server's own name for it. */
export function exportFilename(sessionId: string, format: 'markdown' | 'json'): string {
  return `chat_${sessionId}.${format === 'json' ? 'json' : 'md'}`
}
