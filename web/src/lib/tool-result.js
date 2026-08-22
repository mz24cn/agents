const ERROR_MARKER = /Error:/i

/**
 * Tool results are considered failed when they contain a top-level JSON
 * `error` field, or when `Error:` appears near either edge of the displayed
 * text. Checking both edges accommodates wrapped talk_to/delegate results
 * without treating an incidental marker deep inside a long successful result
 * as a failure.
 */
export function isToolErrorContent(content) {
  const text = String(content ?? '').trim()

  try {
    const parsed = JSON.parse(text)
    if (
      parsed &&
      typeof parsed === 'object' &&
      !Array.isArray(parsed) &&
      Object.prototype.hasOwnProperty.call(parsed, 'error')
    ) {
      return true
    }
  } catch {}

  return ERROR_MARKER.test(text.slice(0, 50)) || ERROR_MARKER.test(text.slice(-100))
}
