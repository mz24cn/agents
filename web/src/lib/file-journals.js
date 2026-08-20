/**
 * Build tolerant aliases for a conversation / file-journal timestamp.
 *
 * Historical messages have used both `YYYY-MM-DDTHH:mm:ss` and
 * `YYYY-MM-DD HH:mm:ss`; some imported sessions also contain fractional
 * seconds. File-journal lookup should not depend on that presentation detail.
 */
export function timestampAliases(value) {
  if (value == null) return []
  const raw = String(value).trim()
  if (!raw) return []

  const aliases = new Set([raw])
  const withoutFraction = raw.replace(
    /^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})\.\d+(Z|[+-]\d{2}:?\d{2})?$/,
    '$1$2',
  )
  aliases.add(withoutFraction)

  for (const timestamp of [...aliases]) {
    if (timestamp.length >= 11 && (timestamp[10] === 'T' || timestamp[10] === ' ')) {
      aliases.add(`${timestamp.slice(0, 10)}T${timestamp.slice(11)}`)
      aliases.add(`${timestamp.slice(0, 10)} ${timestamp.slice(11)}`)
    }
  }
  return [...aliases]
}

/** Map every timestamp alias to the exact key returned by the backend. */
export function buildFileJournalTurnKeyMap(turnKeys = []) {
  const result = {}
  for (const turnKey of turnKeys || []) {
    if (turnKey == null) continue
    const exact = String(turnKey)
    for (const alias of timestampAliases(exact)) {
      // Keep the first backend key when malformed/imported data collides.
      if (!(alias in result)) result[alias] = exact
    }
  }
  return result
}

/** Resolve a message timestamp to the exact backend journal timestamp. */
export function resolveFileJournalTurnKey(turnKeyMap, messageTimestamp) {
  for (const alias of timestampAliases(messageTimestamp)) {
    if (turnKeyMap?.[alias]) return turnKeyMap[alias]
  }
  return null
}
