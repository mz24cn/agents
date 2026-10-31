import { describe, it, expect } from 'vitest'
import { canForkMessage, lastForkableIndex, forkContextWarning } from './session-fork.js'

// 衍生新会话的可 fork 判定必须和后端 _resolve_fork_cut 一致：
// role=assistant 且没有 tool_calls（一轮推理循环的收尾消息）。
describe('canForkMessage', () => {
  it('accepts a plain final assistant reply', () => {
    expect(canForkMessage({ role: 'assistant', content: 'done', timestamp: 't1' })).toBe(true)
  })

  it('rejects the assistant message that requested tool calls', () => {
    const msg = { role: 'assistant', content: '', tool_calls: [{ id: '1', name: 'exec_shell' }] }
    expect(canForkMessage(msg)).toBe(false)
  })

  it('treats an empty tool_calls array as a final reply', () => {
    expect(canForkMessage({ role: 'assistant', content: 'hi', tool_calls: [] })).toBe(true)
  })

  it('rejects a streaming assistant message (not persisted yet)', () => {
    expect(canForkMessage({ role: 'assistant', content: 'partial', streaming: true })).toBe(false)
  })

  it('rejects user, tool, and system messages', () => {
    for (const role of ['user', 'tool', 'system', 'function']) {
      expect(canForkMessage({ role, content: 'x', timestamp: 't' })).toBe(false)
    }
  })

  it('rejects missing or non-object messages', () => {
    expect(canForkMessage(null)).toBe(false)
    expect(canForkMessage(undefined)).toBe(false)
    expect(canForkMessage('assistant')).toBe(false)
  })
})

describe('lastForkableIndex', () => {
  const messages = [
    { role: 'user', content: 'q1', timestamp: 't0' },
    { role: 'assistant', content: '', tool_calls: [{ id: '1', name: 'exec_shell' }], timestamp: 't1' },
    { role: 'tool', content: 'ok', tool_use_id: '1', timestamp: 't2' },
    { role: 'assistant', content: 'a1', timestamp: 't3' },
    { role: 'user', content: 'q2', timestamp: 't4' },
    { role: 'assistant', content: 'still streaming', streaming: true, timestamp: 't5' },
  ]

  it('picks the newest forkable message inside a block', () => {
    expect(lastForkableIndex(messages, [0, 1, 2, 3])).toBe(3)
  })

  it('ignores a trailing streaming assistant', () => {
    expect(lastForkableIndex(messages, [4, 5])).toBe(-1)
    expect(lastForkableIndex(messages, [3, 4, 5])).toBe(3)
  })

  it('returns -1 for an empty index list', () => {
    expect(lastForkableIndex(messages, [])).toBe(-1)
  })
})

// 后端在复制历史超出上下文预算时会先为新会话重建摘要；只有没做成（或做完仍超）
// 才带 warning。fork 已经建好，所以这里只产出提示文案，不阻断切换。
describe('forkContextWarning', () => {
  const translate = (key, vars) => `${key}:${vars.estimated}/${vars.budget}`

  it('says nothing when the guard is satisfied', () => {
    expect(forkContextWarning(undefined, translate)).toBe('')
    expect(forkContextWarning(null, translate)).toBe('')
    expect(forkContextWarning({ warning: '', compressed: true }, translate)).toBe('')
  })

  it('quotes the measured tokens and the budget when it warns', () => {
    const guard = { warning: 'over budget', estimated_tokens: 307908, budget: 18000 }
    expect(forkContextWarning(guard, translate)).toBe('forkContextWarning:307908/18000')
  })

  it('falls back to placeholders when the report has no numbers', () => {
    expect(forkContextWarning({ warning: 'compressing failed' }, translate))
      .toBe('forkContextWarning:?/?')
  })
})
