/** Which rows a page shows, and how many pages there are; a page past the end is clamped to the last. */
export function pageWindow(total: number, page: number, size: number) {
  const pages = Math.max(1, Math.ceil(total / size))
  const current = Math.min(Math.max(1, page), pages)
  const offset = (current - 1) * size
  return { pages, current, offset, from: total === 0 ? 0 : offset + 1, to: Math.min(total, offset + size) }
}
