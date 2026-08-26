import { describe, expect, it } from 'vitest'
import { buildTurnAgentGrouping } from './message-groups.js'

describe('buildTurnAgentGrouping', () => {
  it('groups all unscoped assistant messages and matched tool results into one block', () => {
    const messages = [
      { role: 'user', content: 'check it' },
      {
        role: 'assistant',
        content: 'I will inspect it.',
        tool_calls: [{ id: 'call-search', name: 'search_code' }],
      },
      { role: 'tool', tool_use_id: 'call-search', content: 'matches' },
      {
        role: 'assistant',
        content: 'I will verify it.',
        tool_calls: [{ id: 'call-shell', name: 'exec_shell' }],
      },
      { role: 'tool', tool_use_id: 'call-shell', content: 'ok' },
      { role: 'assistant', content: 'Verified.' },
    ]

    const { effectiveAgentIds, compactAgentBlocks } = buildTurnAgentGrouping(messages, 1, 5)

    expect([...effectiveAgentIds.values()]).toEqual([null, null, null, null, null])
    expect(compactAgentBlocks).toEqual([
      { start: 1, end: 5, indices: [1, 2, 3, 4, 5], agentId: null },
    ])
  })

  it('pairs an unscoped tool result with its unscoped declaring assistant by tool_use_id', () => {
    const messages = [
      {
        role: 'assistant',
        tool_calls: [{ id: 'call-default', name: 'read_file' }],
      },
      {
        role: 'assistant',
        agent_id: 'agent-a',
        content: 'agent output',
      },
      {
        role: 'tool',
        tool_use_id: 'call-default',
        agent_id: 'agent-a',
        content: 'file contents',
      },
    ]

    const { effectiveAgentIds, compactAgentBlocks } = buildTurnAgentGrouping(messages, 0, 2)

    expect(effectiveAgentIds.get(2)).toBeNull()
    expect(compactAgentBlocks).toEqual([
      { start: 0, end: 2, indices: [0, 2], agentId: null },
      { start: 1, end: 1, indices: [1], agentId: 'agent-a' },
    ])
  })

  it('keeps interleaved agents in one block each and pairs results by tool_use_id', () => {
    const messages = [
      {
        role: 'assistant',
        agent_id: 'agent-a',
        tool_calls: [{ id: 'call-a', name: 'search_code' }],
      },
      {
        role: 'assistant',
        agent_id: 'agent-b',
        tool_calls: [{ id: 'call-b', name: 'exec_shell' }],
      },
      { role: 'tool', tool_use_id: 'call-a', content: 'A result' },
      { role: 'tool', tool_use_id: 'call-b', content: 'B result' },
      { role: 'assistant', agent_id: 'agent-a', content: 'A done' },
      { role: 'assistant', agent_id: 'agent-b', content: 'B done' },
    ]

    const { compactAgentBlocks } = buildTurnAgentGrouping(messages, 0, 5)

    expect(compactAgentBlocks).toEqual([
      { start: 0, end: 4, indices: [0, 2, 4], agentId: 'agent-a' },
      { start: 1, end: 5, indices: [1, 3, 5], agentId: 'agent-b' },
    ])
  })

  it('keeps unmatched tool results in the shared unscoped assistant block', () => {
    const messages = [
      { role: 'assistant', content: 'before' },
      { role: 'tool', tool_use_id: 'missing-call', content: 'legacy result' },
      { role: 'assistant', content: 'after' },
    ]

    const { compactAgentBlocks } = buildTurnAgentGrouping(messages, 0, 2)

    expect(compactAgentBlocks).toEqual([
      { start: 0, end: 2, indices: [0, 1, 2], agentId: null },
    ])
  })
})
