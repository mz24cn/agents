/**
 * Placeholder parsing utilities for prompt templates.
 *
 * Placeholders use the format {variable_name} (curly braces around a
 * word-character variable name).
 */

/**
 * Extract all unique placeholder variable names from template content.
 *
 * @param {string} content  Template text potentially containing {var} placeholders
 * @returns {string[]}  Array of unique variable names in order of first appearance
 */
export function extractPlaceholders(content) {
  const seen = new Set()
  const result = []
  for (const match of content.matchAll(/\{(\w+)\}/g)) {
    const name = match[1]
    if (!seen.has(name)) {
      seen.add(name)
      result.push(name)
    }
  }
  return result
}

/**
 * Replace placeholders in content with values from the given object.
 * Missing values are replaced with an empty string.
 *
 * @param {string} content  Template text with {var} placeholders
 * @param {Record<string, string>} values  Map of variable name → replacement value
 * @returns {string}  Content with placeholders replaced
 */
export function replacePlaceholders(content, values) {
  return content.replace(/\{(\w+)\}/g, (_, name) => values[name] ?? '')
}
