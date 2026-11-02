'use client'

import { useMemo, useState } from 'react'
import { Code2, GitCompare } from 'lucide-react'
import type { StrategyVersion, SuggestionDto } from '@/lib/api'
import { describeDiff, diffLines, diffSummary, foldUnchanged } from '@/lib/diff'
import { defaultCompare, newestFirst, sandboxFacts, suggestionRows, versionLabel, versionRows } from '@/lib/strategy-versions'
import { ErrorNotice } from '@/components/ui/error-notice'

/** Every version of a strategy with how it fared and where it came from, what changed between any two
 * (as a line-by-line difference), and the suggestions that were made for it. */
export function VersionsPanel({
  versions,
  suggestions,
  suggestionsError,
  onRetrySuggestions,
  onViewCode,
}: {
  versions: StrategyVersion[]
  suggestions: SuggestionDto[]
  suggestionsError: string | null
  onRetrySuggestions: () => void
  onViewCode: (versionId: string) => void
}) {
  const rows = useMemo(() => versionRows(versions, suggestions), [versions, suggestions])
  const [compare, setCompare] = useState(() => defaultCompare(versions))
  const [showAll, setShowAll] = useState(false)
  const from = versions.find((v) => v.id === compare.from) ?? null
  const to = versions.find((v) => v.id === compare.to) ?? null

  const lines = useMemo(() => (from && to ? diffLines(from.code, to.code) : []), [from, to])
  const summary = useMemo(() => diffSummary(lines), [lines])
  const shown = useMemo(() => (showAll ? lines : foldUnchanged(lines)), [lines, showAll])
  const history = useMemo(() => suggestionRows(suggestions, versions), [suggestions, versions])

  return (
    <div className="review-content">
      <p className="eyebrow">EVERY VERSION</p>
      <div className="overflow-x-auto">
        <table className="sv-table" aria-label="Versions">
          <thead>
            <tr><th>Version</th><th>Made (India)</th><th>Checks</th><th>Where it came from</th><th><span className="sr-only">Actions</span></th></tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.version.id}>
                <td><strong>{row.label}</strong></td>
                <td className="mono">{row.made}</td>
                <td>
                  <div>{row.validation}</div>
                  <details>
                    <summary>{row.sandbox}</summary>
                    {sandboxFacts(row.version).length === 0 ? (
                      <p className="sv-muted">The sandbox has not run this version.</p>
                    ) : (
                      <dl className="sv-facts">
                        {sandboxFacts(row.version).map((fact) => (
                          <div key={fact.label}><dt>{fact.label}</dt><dd><pre>{fact.value}</pre></dd></div>
                        ))}
                      </dl>
                    )}
                  </details>
                </td>
                <td>{row.from ?? <span className="sv-muted">—</span>}</td>
                <td>
                  <button type="button" className="button-secondary" onClick={() => onViewCode(row.version.id)} aria-label={`View the code of ${row.label}`}>
                    <Code2 className="size-3" />Code
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="eyebrow sv-gap">WHAT CHANGED</p>
      {versions.length < 2 ? (
        <p className="sv-muted">There is only one version so far, so there is nothing to compare.</p>
      ) : (
        <>
          <div className="sv-compare">
            <label>
              <span>From</span>
              <select aria-label="Compare from" value={compare.from ?? ''} onChange={(e) => setCompare((c) => ({ ...c, from: e.target.value }))}>
                {newestFirst(versions).map((v) => <option key={v.id} value={v.id}>{versionLabel(v)}</option>)}
              </select>
            </label>
            <GitCompare className="size-4" aria-hidden="true" />
            <label>
              <span>To</span>
              <select aria-label="Compare to" value={compare.to ?? ''} onChange={(e) => setCompare((c) => ({ ...c, to: e.target.value }))}>
                {newestFirst(versions).map((v) => <option key={v.id} value={v.id}>{versionLabel(v)}</option>)}
              </select>
            </label>
            <output className="sv-summary" aria-label="Difference">{describeDiff(summary)}</output>
            <label className="sv-toggle">
              <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />
              Show every line
            </label>
          </div>
          <div className="overflow-x-auto">
            <table className="sv-diff" aria-label="Changes between the two versions">
              <tbody>
                {shown.map((line, index) =>
                  line.kind === 'gap' ? (
                    <tr key={`gap-${index}`} data-kind="gap"><td colSpan={4}>{line.hidden} unchanged {line.hidden === 1 ? 'line' : 'lines'}</td></tr>
                  ) : (
                    <tr key={`${line.oldLine}-${line.newLine}-${index}`} data-kind={line.kind}>
                      <td className="sv-n">{line.oldLine ?? ''}</td>
                      <td className="sv-n">{line.newLine ?? ''}</td>
                      <td className="sv-sign" aria-label={line.kind === 'add' ? 'added' : line.kind === 'remove' ? 'removed' : 'unchanged'}>{line.kind === 'add' ? '+' : line.kind === 'remove' ? '−' : ' '}</td>
                      <td><code>{line.text}</code></td>
                    </tr>
                  ),
                )}
              </tbody>
            </table>
          </div>
        </>
      )}

      <p className="eyebrow sv-gap">SUGGESTIONS</p>
      {suggestionsError && <ErrorNotice message={`Could not load the suggestions: ${suggestionsError}`} onRetry={onRetrySuggestions} />}
      {!suggestionsError && history.length === 0 && <p className="sv-muted">No improvement has been suggested for this strategy yet. A suggestion is made from the Code tab.</p>}
      <ol className="sv-suggestions" aria-label="Suggestions">
        {history.map((row) => (
          <li key={row.suggestion.id}>
            <header>
              <span className="sv-status">{row.status}</span>
              <span className="mono">{row.asked}</span>
              <span>on {row.base}</span>
              {row.produced && <span>→ became {row.produced}</span>}
            </header>
            <p>{row.suggestion.suggestion_text}</p>
            {row.verdict && <p className="sv-muted">The reviewer: {row.verdict}</p>}
            {row.suggestion.regeneration_diff && (
              <details>
                <summary>What the regeneration changed</summary>
                <pre className="strategy-code"><code>{row.suggestion.regeneration_diff}</code></pre>
              </details>
            )}
          </li>
        ))}
      </ol>
    </div>
  )
}
