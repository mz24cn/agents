import { describe, expect, it } from 'vitest'
import { mergeToolCallDeltas, startsNewToolCallRound } from './stream-messages.js'

describe('mergeToolCallDeltas', () => {
  it('appends fragments for the same indexed call', () => {
    const first = mergeToolCallDeltas([], [
      { _index: 0, id: 'call-a', name: 'exec_', arguments: '{"com' },
    ])
    expect(mergeToolCallDeltas(first, [
      { _index: 0, name: 'shell', arguments: 'mand":"pwd"}' },
    ])).toEqual([
      { _index: 0, id: 'call-a', name: 'exec_shell', arguments: '{"command":"pwd"}' },
    ])
  })

  it('does not treat a missing historical index as index zero', () => {
    const existing = [{ id: 'old-call', name: 'read_file', arguments: '{}' }]
    expect(mergeToolCallDeltas(existing, [
      { _index: 0, id: 'new-call', name: 'exec_shell', arguments: '{"command":"pwd"}' },
    ])).toEqual([
      { id: 'old-call', name: 'read_file', arguments: '{}' },
      { _index: 0, id: 'new-call', name: 'exec_shell', arguments: '{"command":"pwd"}' },
    ])
  })

  it('is idempotent for replayed complete calls without an index', () => {
    const existing = [{ id: 'call-a', name: 'exec_shell', arguments: { command: 'pwd' } }]
    expect(mergeToolCallDeltas(existing, [
      { id: 'call-a', name: 'exec_shell', arguments: { command: 'pwd' } },
    ])).toEqual(existing)
  })
})

describe('startsNewToolCallRound', () => {
  it('detects a new id restarting at delta index zero', () => {
    expect(startsNewToolCallRound(
      { tool_calls: [{ _index: 0, id: 'call-a', name: 'exec_shell' }] },
      { tool_calls: [{ _index: 0, id: 'call-b', name: 'read_file' }] },
    )).toBe(true)
  })

  it('keeps fragments for the same call in the current round', () => {
    expect(startsNewToolCallRound(
      { tool_calls: [{ _index: 0, id: 'call-a', name: 'exec_' }] },
      { tool_calls: [{ _index: 0, id: 'call-a', name: 'shell' }] },
    )).toBe(false)
  })
})
