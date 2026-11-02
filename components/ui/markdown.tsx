import { Fragment, createElement, useMemo } from 'react'
import { type Inline, parseMarkdown } from '@/lib/markdown'

function Inlines({ inline }: { inline: Inline[] }) {
  return (
    <>
      {inline.map((part, i) =>
        part.kind === 'bold' ? <strong key={i}>{part.text}</strong>
        : part.kind === 'italic' ? <em key={i}>{part.text}</em>
        : part.kind === 'code' ? <code key={i}>{part.text}</code>
        : <Fragment key={i}>{part.text}</Fragment>,
      )}
    </>
  )
}

/** A document drawn from `lib/markdown.ts`. Headings start `firstHeading` levels deep, so a document's
 * own top heading sits under the page's one `h1`. */
export function Markdown({ source, firstHeading = 2, className = '' }: { source: string; firstHeading?: number; className?: string }) {
  const blocks = useMemo(() => parseMarkdown(source), [source])
  return (
    <div className={`md-doc ${className}`}>
      {blocks.map((block, i) => {
        if (block.kind === 'rule') return <hr key={i} />
        if (block.kind === 'paragraph') return <p key={i}><Inlines inline={block.inline} /></p>
        if (block.kind === 'heading') {
          const tag = `h${Math.min(block.level + firstHeading - 1, 6)}`
          return createElement(tag, { key: i }, <Inlines inline={block.inline} />)
        }
        const List = block.ordered ? 'ol' : 'ul'
        return (
          <List key={i}>
            {block.items.map((item, j) => <li key={j}><Inlines inline={item} /></li>)}
          </List>
        )
      })}
    </div>
  )
}
