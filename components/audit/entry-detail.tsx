'use client'

import { useState } from 'react'
import { Check, Copy } from 'lucide-react'
import type { AuditLogEntry } from '@/lib/api'

/** One hash, whole, with a button to copy it: an auditor compares these, so they are never shortened here. */
function HashField({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // clipboard unavailable (insecure origin, denied): the value is still on screen to select
    }
  }
  return (
    <div className="audit-hash">
      <span className="audit-hash-label">{label}</span>
      <code className="mono">{value}</code>
      <button type="button" className="audit-copy-btn" onClick={() => void copy()} aria-label={`Copy ${label.toLowerCase()}`}>
        {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
        {copied ? 'Copied' : 'Copy'}
      </button>
    </div>
  )
}

/** Everything a log entry holds: what was recorded, and the two hashes that chain it to the entry before. */
export function EntryDetail({ entry }: { entry: AuditLogEntry }) {
  return (
    <div className="diff-grid">
      <div className="diff-col">
        <p className="diff-col-label">Details</p>
        <pre className="diff-pre diff-pre-after">
          {entry.details === null ? '// no details recorded' : JSON.stringify(entry.details, null, 2)}
        </pre>
      </div>
      <div className="diff-col audit-chain-col">
        <p className="diff-col-label">Chain link, entry #{entry.sequence}</p>
        <HashField label="Previous hash" value={entry.previous_hash} />
        <HashField label="This entry’s hash" value={entry.hash} />
        {entry.correlation_id && (
          <>
            <p className="diff-col-label audit-chain-gap">Correlation ID</p>
            <pre className="diff-pre diff-pre-before mono">{entry.correlation_id}</pre>
          </>
        )}
      </div>
    </div>
  )
}
