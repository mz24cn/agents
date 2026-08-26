/**
 * Resolve message ownership and build compact-mode blocks for one user turn.
 *
 * Assistant messages without an agent ID intentionally share one unscoped
 * assistant block. Tool results are owned by the assistant that declared their
 * tool_use_id; an explicit agent_id on the result is only a fallback when the
 * declaring call cannot be found.
 */
export function buildTurnAgentGrouping(messages = [], startIndex = 0, endIndex = messages.length - 1) {
  const toolOwnerByCallId = new Map()

  for (let index = startIndex; index <= endIndex; index++) {
    const message = messages[index]
    if (message?.role !== 'assistant') continue

    const owner = message.agent_id || message.assistant_id || null
    for (const call of message.tool_calls || []) {
      const callId = call?.id || call?.tool_use_id
      // Map.has() distinguishes an unscoped declaring assistant (null) from a
      // call ID that was not found at all.
      if (callId && !toolOwnerByCallId.has(callId)) {
        toolOwnerByCallId.set(callId, owner)
      }
    }
  }

  const effectiveAgentIds = new Map()
  for (let index = startIndex; index <= endIndex; index++) {
    const message = messages[index]
    let owner = message?.agent_id || message?.assistant_id || null

    if (message?.role === 'tool') {
      const toolUseId = message.tool_use_id || null
      // The call declaration is authoritative. This keeps a result with its
      // tool card even when a legacy/replayed result lacks (or has stale)
      // agent metadata.
      if (toolUseId && toolOwnerByCallId.has(toolUseId)) {
        owner = toolOwnerByCallId.get(toolUseId)
      }
    }

    effectiveAgentIds.set(index, owner)
  }

  const compactAgentBlocks = []
  const compactBlockByAgent = new Map()
  const unscopedBlockKey = Symbol('unscoped-assistant')

  for (let index = startIndex; index <= endIndex; index++) {
    const agentId = effectiveAgentIds.get(index)
    // All messages without an agent ID belong to the same default assistant.
    const blockKey = agentId || unscopedBlockKey
    let block = compactBlockByAgent.get(blockKey)
    if (!block) {
      block = { start: index, end: index, indices: [], agentId }
      compactBlockByAgent.set(blockKey, block)
      compactAgentBlocks.push(block)
    }
    block.indices.push(index)
    block.end = index
  }

  return { effectiveAgentIds, compactAgentBlocks }
}
