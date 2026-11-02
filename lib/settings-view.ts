// Which Settings section is open, kept in the address (`/settings?section=llm`) so a reload, a bookmark
// or a link lands on the same one. Anything in the address that is not a section is ignored.

export const SECTION_IDS = ['gateway', 'llm', 'broker', 'notifications', 'skills', 'risk', 'users', 'security', 'backups'] as const
export type SectionId = (typeof SECTION_IDS)[number]

/** The section named in `?section=`, or null when it names none the page has. */
export function sectionFromQuery(search: string): SectionId | null {
  const value = new URLSearchParams(search).get('section')
  return (SECTION_IDS as readonly string[]).includes(value ?? '') ? (value as SectionId) : null
}

/** The address for a section: the bare page for the one it opens on. */
export function sectionToUrl(section: SectionId): string {
  return section === 'gateway' ? '/settings' : `/settings?section=${section}`
}
