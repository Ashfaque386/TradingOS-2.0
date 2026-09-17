'use client'

// WebSocket hook for the real backend channels (activity-feed, agent-logs,
// organization-events, sign-off-queue, ticks -- src/api/routes/websockets.py).
// Auth goes over a `?token=` query param, not a header: browsers cannot set
// custom headers on a WebSocket upgrade request, so this mirrors the
// backend's documented exception to its normal Bearer-header convention.

import { useEffect, useRef, useState } from 'react'
import { getAccessToken, API_BASE_URL } from './api'

const WS_BASE_URL =
  process.env.NEXT_PUBLIC_WS_URL ?? API_BASE_URL.replace(/^http/, 'ws')

export type WsStatus = 'connecting' | 'open' | 'closed'

/**
 * Subscribes to a backend WebSocket channel for the lifetime of the
 * component. Reconnects with exponential backoff (capped at 15s) on any
 * drop, including an auth failure after token refresh -- a fresh token is
 * read from storage on every (re)connect attempt.
 */
export function useWebSocketChannel<T = unknown>(
  path: string,
  params: Record<string, string>,
  onMessage: (data: T) => void,
): WsStatus {
  const [status, setStatus] = useState<WsStatus>('connecting')
  const onMessageRef = useRef(onMessage)
  onMessageRef.current = onMessage

  const paramsKey = JSON.stringify(params)

  useEffect(() => {
    let cancelled = false
    let socket: WebSocket | null = null
    let retryDelayMs = 1000
    let retryTimer: ReturnType<typeof setTimeout> | undefined

    function connect() {
      if (cancelled) return
      const token = getAccessToken()
      // WS routes are mounted under the same /api/v1 prefix as every REST
      // route (src/api/router.py includes websockets.router into api_router).
      const url = new URL(`${WS_BASE_URL}/api/v1${path}`)
      const parsedParams = JSON.parse(paramsKey) as Record<string, string>
      Object.entries(parsedParams).forEach(([key, value]) => url.searchParams.set(key, value))
      if (token) url.searchParams.set('token', token)

      setStatus('connecting')
      socket = new WebSocket(url.toString())

      socket.onopen = () => {
        if (cancelled) return
        retryDelayMs = 1000
        setStatus('open')
      }
      socket.onmessage = (event) => {
        if (cancelled) return
        try {
          onMessageRef.current(JSON.parse(event.data as string))
        } catch {
          // non-JSON payload -- ignore rather than crash the socket handler
        }
      }
      socket.onclose = () => {
        if (cancelled) return
        setStatus('closed')
        retryTimer = setTimeout(connect, retryDelayMs)
        retryDelayMs = Math.min(retryDelayMs * 2, 15000)
      }
      socket.onerror = () => {
        socket?.close()
      }
    }

    connect()

    return () => {
      cancelled = true
      if (retryTimer) clearTimeout(retryTimer)
      socket?.close()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, paramsKey])

  return status
}
