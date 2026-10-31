// @vitest-environment jsdom
import { describe, it, expect } from 'vitest'
import { findToolInsertIndex, insertToolMessage, assistantIndexForToolCall } from './stream-tool-placement.js'

const call = (id) => ({ role: 'assistant', agent_id: 'backend_dev', tool_calls: [{ id, name: 'run_in_terminal' }], content: '检查改动' })
const result = (id, content = 'diff output') => ({ role: 'tool', tool_use_id: id, agent_id: 'backend_dev', content })
const user = (text, ts) => ({ role: 'user', content: text, timestamp: ts })

describe('assistantIndexForToolCall', () => {
  it('finds the declaring assistant by call id', () => {
    const msgs = [user('q', 't0'), call('diff1'), user('interject', 't1')]
    expect(assistantIndexForToolCall(msgs, 'diff1')).toBe(1)
    expect(assistantIndexForToolCall(msgs, 'nope')).toBe(-1)
    expect(assistantIndexForToolCall(msgs, null)).toBe(-1)
  })

  it('also matches calls keyed by tool_use_id', () => {
    const msgs = [{ role: 'assistant', tool_calls: [{ tool_use_id: 'x1' }] }]
    expect(assistantIndexForToolCall(msgs, 'x1')).toBe(0)
  })
})

describe('findToolInsertIndex', () => {
  it('returns append position when the cluster is already last', () => {
    const msgs = [user('q', 't0'), call('diff1'), result('status1')]
    expect(findToolInsertIndex(msgs, 'diff1')).toBe(3)
  })

  it('returns append position when the declaring call is unknown', () => {
    const msgs = [user('q', 't0')]
    expect(findToolInsertIndex(msgs, 'ghost')).toBe(1)
  })

  it('places a late result before an interjected user bubble (screenshot case)', () => {
    // live stream: assistant(git diff call) → interject echo → (later) result
    const msgs = [user('看下状态', 't0'), call('diff1'), user('但排除debugger.py', 't1')]
    expect(findToolInsertIndex(msgs, 'diff1')).toBe(2)
    const next = insertToolMessage(msgs, result('diff1'), 'diff1')
    expect(next.map(m => m.role)).toEqual(['user', 'assistant', 'tool', 'user'])
  })

  it('inserts after the existing result cluster, not right after the call', () => {
    const msgs = [
      user('q', 't0'),
      { role: 'assistant', tool_calls: [{ id: 'a' }, { id: 'b' }] },
      result('a'),
      user('interject', 't1'),
      user('再插一条', 't2'),
    ]
    const next = insertToolMessage(msgs, result('b'), 'b')
    expect(next.map(m => [m.role, m.tool_use_id || m.content])).toEqual([
      ['user', 'q'], ['assistant', undefined], ['tool', 'a'], ['tool', 'b'], ['user', 'interject'], ['user', '再插一条'],
    ])
  })

  it('does not move results that already append normally', () => {
    const msgs = [user('q', 't0'), call('diff1')]
    const next = insertToolMessage(msgs, result('diff1'), 'diff1')
    expect(next.length).toBe(3)
    expect(next.at(-1).role).toBe('tool')
  })

  it('in group chat, lands the late result next to its declaring call', () => {
    // backend_dev calls d1; qa_agent then calls and finishes q1; user interjects;
    // backend_dev's git diff result arrives last (it was the slow one).
    const msgs = [
      user('q', 't0'),
      { role: 'assistant', agent_id: 'backend_dev', tool_calls: [{ id: 'd1' }] },
      { role: 'assistant', agent_id: 'qa', tool_calls: [{ id: 'q1' }] },
      result('q1', 'qa output'),
      user('interject', 't1'),
    ]
    const next = insertToolMessage(msgs, result('d1'), 'd1')
    expect(next.map(m => m.role)).toEqual(['user', 'assistant', 'tool', 'assistant', 'tool', 'user'])
    expect(next[2].tool_use_id).toBe('d1') // stays inside backend_dev's block
  })
})
