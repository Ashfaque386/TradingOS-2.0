/** Turn a typed list of sender IDs ("123, 456 789") into the list the server stores: split on commas,
 * semicolons and spaces, trimmed, without blanks or repeats, in the order typed. */
export function parseSenderIds(text: string): string[] {
  return [...new Set(text.split(/[\s,;]+/).map((id) => id.trim()).filter(Boolean))]
}
