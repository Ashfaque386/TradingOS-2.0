/**
 * A line-by-line difference between two versions of a text, kept free of React so a logic spec can drive
 * it. It is the longest-common-subsequence diff a reviewer is used to: lines in both are unchanged,
 * the rest are removed from the old text or added in the new one, in the order they appear.
 */

export type DiffLine = {
  kind: 'same' | 'add' | 'remove'
  text: string
  /** The line's number in the old text, or null for an added line. */
  oldLine: number | null
  /** The line's number in the new text, or null for a removed line. */
  newLine: number | null
}

/** Above this many cells (old lines times new lines) the exact diff is not worth waiting for. */
const MAX_CELLS = 4_000_000

const split = (text: string): string[] => (text === '' ? [] : text.replace(/\r\n/g, '\n').split('\n'))

export function diffLines(before: string, after: string): DiffLine[] {
  const a = split(before)
  const b = split(after)
  const out: DiffLine[] = []

  // Lines both texts start and end with are unchanged: trimming them leaves the middle to compare.
  let start = 0
  while (start < a.length && start < b.length && a[start] === b[start]) start += 1
  let endA = a.length
  let endB = b.length
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA -= 1
    endB -= 1
  }

  for (let i = 0; i < start; i += 1) out.push({ kind: 'same', text: a[i], oldLine: i + 1, newLine: i + 1 })

  const rowsA = a.slice(start, endA)
  const rowsB = b.slice(start, endB)
  const n = rowsA.length
  const m = rowsB.length

  if (n * m > MAX_CELLS) {
    // too large to align line by line: say the whole middle was replaced
    rowsA.forEach((text, i) => out.push({ kind: 'remove', text, oldLine: start + i + 1, newLine: null }))
    rowsB.forEach((text, j) => out.push({ kind: 'add', text, oldLine: null, newLine: start + j + 1 }))
  } else {
    // lcs[i][j]: how many lines rowsA[i..] and rowsB[j..] have in common
    const lcs: Uint32Array[] = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1))
    for (let i = n - 1; i >= 0; i -= 1) {
      for (let j = m - 1; j >= 0; j -= 1) {
        lcs[i][j] = rowsA[i] === rowsB[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1])
      }
    }
    let i = 0
    let j = 0
    while (i < n && j < m) {
      if (rowsA[i] === rowsB[j]) {
        out.push({ kind: 'same', text: rowsA[i], oldLine: start + i + 1, newLine: start + j + 1 })
        i += 1
        j += 1
      } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
        out.push({ kind: 'remove', text: rowsA[i], oldLine: start + i + 1, newLine: null })
        i += 1
      } else {
        out.push({ kind: 'add', text: rowsB[j], oldLine: null, newLine: start + j + 1 })
        j += 1
      }
    }
    for (; i < n; i += 1) out.push({ kind: 'remove', text: rowsA[i], oldLine: start + i + 1, newLine: null })
    for (; j < m; j += 1) out.push({ kind: 'add', text: rowsB[j], oldLine: null, newLine: start + j + 1 })
  }

  for (let k = 0; k < a.length - endA; k += 1) {
    out.push({ kind: 'same', text: a[endA + k], oldLine: endA + k + 1, newLine: endB + k + 1 })
  }
  return out
}

export type DiffSummary = { added: number; removed: number; unchanged: number }

export function diffSummary(lines: DiffLine[]): DiffSummary {
  return {
    added: lines.filter((l) => l.kind === 'add').length,
    removed: lines.filter((l) => l.kind === 'remove').length,
    unchanged: lines.filter((l) => l.kind === 'same').length,
  }
}

/** `+12 −5`, or that nothing changed. */
export function describeDiff(summary: DiffSummary): string {
  if (summary.added === 0 && summary.removed === 0) return 'No difference: the two are the same.'
  return `+${summary.added} −${summary.removed} lines`
}

export type Hunk = DiffLine | { kind: 'gap'; hidden: number }

/** The diff with long runs of unchanged lines folded away: `context` lines are kept on each side of a change. */
export function foldUnchanged(lines: DiffLine[], context = 3): Hunk[] {
  const keep = new Array<boolean>(lines.length).fill(false)
  lines.forEach((line, index) => {
    if (line.kind === 'same') return
    for (let k = Math.max(0, index - context); k <= Math.min(lines.length - 1, index + context); k += 1) keep[k] = true
  })
  const out: Hunk[] = []
  let hidden = 0
  lines.forEach((line, index) => {
    if (keep[index]) {
      if (hidden > 0) out.push({ kind: 'gap', hidden })
      hidden = 0
      out.push(line)
    } else {
      hidden += 1
    }
  })
  if (hidden > 0) out.push({ kind: 'gap', hidden })
  return out
}
