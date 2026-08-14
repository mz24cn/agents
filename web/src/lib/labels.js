/**
 * Parse a label string into an array of label values.
 * Supports comma (half-width), full-width comma (，), space, and tab as delimiters.
 * Labels themselves cannot contain these delimiter characters.
 * Empty labels and whitespace-only labels are filtered out.
 *
 * @param {string} text - Raw label input string
 * @returns {string[]} Clean array of label strings
 */
export function parseLabels(text) {
  if (!text || typeof text !== 'string') return []
  // Split on: comma, full-width comma, whitespace (space/tab/newline) — one or more
  return text.split(/[，,\s]+/).map(s => s.trim()).filter(Boolean)
}
