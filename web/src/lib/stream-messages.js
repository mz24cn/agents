/** Merge streamed tool-call fragments without crossing inference-round boundaries.
 *
 * Calls carrying `_index` are OpenAI-style deltas, so their name/arguments are
 * appended. Calls without `_index` are complete snapshots (for example Ollama
 * or restored data), so replaying the same call must replace supplied fields
 * rather than concatenate them again.
 */
export function mergeToolCallDeltas(existing = [], incoming = []) {
  const merged = existing.map(tc => ({ ...tc }))

  for (const inc of incoming) {
    const hasIndex = inc?._index !== undefined && inc?._index !== null
    const incIndex = inc?._index
    const incId = inc?.id || inc?.tool_use_id
    let pos = -1

    // Only an existing call that explicitly carries the same delta index may
    // match by index. Treating a missing index as zero makes every later
    // round's index-0 call merge into the first historical call.
    if (hasIndex) {
      pos = merged.findIndex(tc => tc?._index !== undefined && tc?._index !== null && tc._index === incIndex)
    }
    if (pos < 0 && incId) {
      pos = merged.findIndex(tc => tc?.id === incId || tc?.tool_use_id === incId)
    }
    if (pos < 0) {
      merged.push({ ...inc })
      continue
    }

    const cur = { ...merged[pos] }
    if (hasIndex) cur._index = incIndex
    if (inc.id) cur.id = inc.id
    if (inc.tool_use_id) cur.tool_use_id = inc.tool_use_id

    if (hasIndex) {
      // Indexed calls are protocol deltas.
      if (inc.name) cur.name = (cur.name || '') + inc.name
      if (inc.arguments !== undefined && inc.arguments !== null) {
        if (typeof inc.arguments === 'string') {
          cur.arguments = (cur.arguments || '') + inc.arguments
        } else {
          cur.arguments = inc.arguments
        }
      }
    } else {
      // Non-indexed calls are complete values. Make duplicate/replay delivery
      // idempotent instead of producing names such as exec_shellexec_shell.
      if (inc.name !== undefined) cur.name = inc.name
      if (inc.arguments !== undefined) cur.arguments = inc.arguments
      for (const [key, value] of Object.entries(inc)) {
        if (!['id', 'tool_use_id', 'name', 'arguments'].includes(key)) cur[key] = value
      }
    }

    merged[pos] = cur
  }

  return merged
}

/** Detect an index-0 call from a later model round accidentally routed into an
 * assistant bubble that already owns calls from an earlier round. Tool-call IDs
 * are globally unique for a request, so a new ID at index zero is a reliable
 * round boundary and also survives missing tool-result frames during reconnect.
 */
export function startsNewToolCallRound(existingAssistant, incomingMessage) {
  const existingCalls = existingAssistant?.tool_calls || []
  const incomingCalls = incomingMessage?.tool_calls || []
  if (!existingCalls.length || !incomingCalls.length) return false

  const existingIds = new Set(
    existingCalls.map(tc => tc?.id || tc?.tool_use_id).filter(Boolean),
  )
  return incomingCalls.some(tc => {
    if (tc?._index !== 0) return false
    const id = tc?.id || tc?.tool_use_id
    return !!id && !existingIds.has(id)
  })
}
