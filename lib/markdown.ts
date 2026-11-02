// A small Markdown reader for the documents this console shows: the investor reports the server
// writes (headings, bullet lists, **bold**), and any note that follows the same conventions. It turns
// text into plain blocks, and the component draws those as elements, so nothing is ever put in a page
// as HTML and a line of commentary cannot inject anything. What it does not know (links, images,
// tables, HTML) stays as the text it was written as, readable but not active.

export type Inline = { kind: 'text' | 'bold' | 'italic' | 'code'; text: string }

export type MdBlock =
  | { kind: 'heading'; level: 1 | 2 | 3 | 4; inline: Inline[] }
  | { kind: 'list'; ordered: boolean; items: Inline[][] }
  | { kind: 'paragraph'; inline: Inline[] }
  | { kind: 'rule' }

// **bold**, `code`, *italic*. Underscores are left alone: they are in names (max_drawdown_pct), not emphasis.
const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*)/

export function parseInline(text: string): Inline[] {
  const out: Inline[] = []
  for (const piece of text.split(INLINE)) {
    if (piece === '') continue
    if (piece.startsWith('**') && piece.endsWith('**') && piece.length > 4) out.push({ kind: 'bold', text: piece.slice(2, -2) })
    else if (piece.startsWith('`') && piece.endsWith('`') && piece.length > 2) out.push({ kind: 'code', text: piece.slice(1, -1) })
    else if (piece.startsWith('*') && piece.endsWith('*') && piece.length > 2) out.push({ kind: 'italic', text: piece.slice(1, -1) })
    else out.push({ kind: 'text', text: piece })
  }
  return out
}

const HEADING = /^(#{1,6})\s+(.*\S)\s*$/
const BULLET = /^\s*[-*+]\s+(.*)$/
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/

export function parseMarkdown(source: string): MdBlock[] {
  const blocks: MdBlock[] = []
  let paragraph: string[] = []
  let list: { ordered: boolean; items: string[] } | null = null

  const flushParagraph = () => {
    if (paragraph.length > 0) blocks.push({ kind: 'paragraph', inline: parseInline(paragraph.join(' ')) })
    paragraph = []
  }
  const flushList = () => {
    if (list) blocks.push({ kind: 'list', ordered: list.ordered, items: list.items.map(parseInline) })
    list = null
  }
  const flush = () => {
    flushParagraph()
    flushList()
  }

  for (const line of source.replace(/\r\n?/g, '\n').split('\n')) {
    if (line.trim() === '') {
      flush()
      continue
    }
    const rule = RULE.test(line)
    const heading = HEADING.exec(line)
    // A rule of dashes would also read as an empty bullet: it is checked first.
    if (rule) {
      flush()
      blocks.push({ kind: 'rule' })
      continue
    }
    if (heading) {
      flush()
      blocks.push({ kind: 'heading', level: Math.min(heading[1].length, 4) as 1 | 2 | 3 | 4, inline: parseInline(heading[2]) })
      continue
    }
    const bullet = BULLET.exec(line)
    const numbered = bullet ? null : NUMBERED.exec(line)
    const item = bullet ?? numbered
    if (item) {
      flushParagraph()
      const ordered = numbered !== null
      if (list && list.ordered !== ordered) flushList()
      if (!list) list = { ordered, items: [] }
      list.items.push(item[1])
      continue
    }
    // An indented line under a bullet continues that bullet; anything else is a paragraph line.
    if (list && /^\s+\S/.test(line)) {
      list.items[list.items.length - 1] += ` ${line.trim()}`
      continue
    }
    flushList()
    paragraph.push(line.trim())
  }
  flush()
  return blocks
}

/** The file a report is saved as: `investor-report-2026-10-19-to-2026-10-25-weekly.md`. */
export function reportFilename(report: { period_start: string; period_end: string; cadence: string }): string {
  return `investor-report-${report.period_start}-to-${report.period_end}-${report.cadence}.md`
}
