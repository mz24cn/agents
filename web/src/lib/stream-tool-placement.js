// Placement rules for streamed tool-result messages.
//
// Why this exists: the interject endpoint echoes the queued user frame onto
// the session stream immediately at enqueue time (so the bubble appears at
// once), while the result frame of a still-running tool can only be published
// when the tool finishes. In the live SSE that means the user frame arrives
// BETWEEN an assistant tool-call frame and its tool-result frame, and the
// persisted conversation never has that order (results are written before the
// drained interjection). MessageList groups strictly on user-message
// boundaries and matches tool results to calls only within one block
// (tool_use_id lookup is block-local), so a tail-appended late result gets
// orphaned into the next reply while the original call shows ⏳ forever.
//
// Fix: insert a newly created tool message right after the result cluster of
// the assistant frame that declares its tool_use_id, instead of appending it
// at the array tail. When nothing follows the cluster (the normal case) this
// is identical to an append.

export function assistantIndexForToolCall(messages, toolUseId) {
  if (!toolUseId) return -1
  return messages.findLastIndex(message =>
    message.role === 'assistant'
    && (message.tool_calls || []).some(call =>
      (call?.id || call?.tool_use_id) === toolUseId
    )
  )
}

/**
 * Index at which a tool frame with `toolUseId` should be inserted:
 * right after the declaring assistant and its already-arrived results.
 * Returns messages.length when placement is unambiguous (append).
 */
export function findToolInsertIndex(messages, toolUseId) {
  const callIndex = assistantIndexForToolCall(messages, toolUseId)
  if (callIndex < 0) return messages.length
  let insertIndex = callIndex + 1
  while (insertIndex < messages.length && messages[insertIndex].role === 'tool') {
    insertIndex++
  }
  return insertIndex
}

/** Immutably insert `toolMsg` at its resolved placement in `messages`. */
export function insertToolMessage(messages, toolMsg, toolUseId) {
  const insertIndex = findToolInsertIndex(messages, toolUseId)
  if (insertIndex >= messages.length) return [...messages, toolMsg]
  const next = [...messages]
  next.splice(insertIndex, 0, toolMsg)
  return next
}
