/**
 * Tests for lib/compression-status.js -- the markers a context compression
 * leaves inside the conversation.
 *
 * One attempt must look the same whether it arrived live (the started/completed
 * SSE pair, attached to the message it belongs to) or was read back from
 * conversation.json (messages[i].meta.compressions) after the session was
 * reopened.  The pairing rules below are what keeps that single representation
 * honest: a completion closes the attempt it answers, a duplicate event is
 * applied once, and a marker never claims to still be running after the
 * generation that ran it is over.
 */
import { describe, expect, it } from 'vitest'

import {
  applyCompressionEntry,
  attachCompressionFrame,
  compressionDetailLines,
  compressionEntriesOf,
  compressionFailed,
  compressionFrameFromEvent,
  compressionIcon,
  compressionIsPending,
  compressionLabel,
  compressionResultIcon,
  compressionStats,
  compressionTargetIndex,
  compressionTooltip,
  isCompressionFrame,
  settleCompressionEntries,
} from './compression-status.js'

/** Minimal translator: returns the key plus its vars so assertions stay readable. */
const t = (key, vars) => `${key}${vars ? ' ' + JSON.stringify(vars) : ''}`

const started = (over = {}) => ({
  role: 'status',
  status: 'context_compression',
  session_id: 's1',
  phase: 'started',
  attempt: 'a1',
  trigger: 'in_loop',
  model_id: 'summary',
  tokens: 92000,
  threshold: 65536,
  delta_start: 12,
  delta_turns: 7,
  summary_version: 3,
  ...over,
})

const completed = (over = {}) => ({
  role: 'status',
  status: 'context_compression',
  session_id: 's1',
  phase: 'completed',
  attempt: 'a1',
  trigger: 'in_loop',
  model_id: 'summary',
  tokens: 92000,
  threshold: 65536,
  delta_start: 12,
  delta_turns: 7,
  summary_version: 4,
  summarized_up_to_turn: 18,
  summary_chars: 1194,
  memory_entries: 73,
  duration_ms: 2400,
  success: true,
  error: '',
  at: '2026-01-01T00:00:00',
  ...over,
})

describe('isCompressionFrame', () => {
  it('recognises only the compression control frame', () => {
    expect(isCompressionFrame(started())).toBe(true)
    expect(isCompressionFrame(completed())).toBe(true)
    // The other status frame in this stream is the client-side tool handoff.
    expect(isCompressionFrame({ role: 'status', status: 'client_tool_calls' })).toBe(false)
    expect(isCompressionFrame({ role: 'assistant', content: 'hi' })).toBe(false)
    expect(isCompressionFrame(null)).toBe(false)
  })
})

describe('compressionEntriesOf', () => {
  it('reads the audit trail persisted on a message', () => {
    const msg = { role: 'assistant', meta: { compressions: [{ attempt: 'a1', success: true }] } }
    expect(compressionEntriesOf(msg)).toEqual([{ attempt: 'a1', success: true }])
  })

  it('is empty for a message without one (including a malformed meta)', () => {
    expect(compressionEntriesOf({ role: 'assistant' })).toEqual([])
    expect(compressionEntriesOf({ role: 'assistant', meta: null })).toEqual([])
    expect(compressionEntriesOf({ role: 'assistant', meta: { compressions: 'no' } })).toEqual([])
    expect(compressionEntriesOf(undefined)).toEqual([])
  })
})

describe('compressionTargetIndex', () => {
  it('attaches to the last assistant turn, like the backend marker does', () => {
    const messages = [
      { role: 'user' }, { role: 'assistant' }, { role: 'tool' }, { role: 'assistant' }, { role: 'tool' },
    ]
    expect(compressionTargetIndex(messages)).toBe(3)
  })

  it('falls back to the last message when there is no assistant turn yet', () => {
    // A first request compressed before it was ever sent.
    expect(compressionTargetIndex([{ role: 'user' }, { role: 'user' }])).toBe(1)
    expect(compressionTargetIndex([])).toBe(-1)
    expect(compressionTargetIndex(null)).toBe(-1)
  })
})

describe('applyCompressionEntry', () => {
  it('opens an attempt on started', () => {
    const [entry] = applyCompressionEntry([], started())
    expect(compressionIsPending(entry)).toBe(true)
    expect(entry.attempt).toBe('a1')
    expect(entry.tokens).toBe(92000)
  })

  it('does not persist the transport fields of the frame', () => {
    const [entry] = applyCompressionEntry([], started())
    for (const field of ['role', 'status', 'session_id', 'phase']) {
      if (field === 'phase') continue  // kept, it marks the attempt as running
      expect(entry).not.toHaveProperty(field)
    }
  })

  it('closes its own attempt on completed instead of adding a second marker', () => {
    const entries = applyCompressionEntry([], started())
    const settled = applyCompressionEntry(entries, completed())
    expect(settled).toHaveLength(1)
    expect(compressionIsPending(settled[0])).toBe(false)
    expect(settled[0].success).toBe(true)
    expect(settled[0].summary_version).toBe(4)
    expect(settled[0].duration_ms).toBe(2400)
    // The persisted entry keeps the started-side facts of the same attempt.
    expect(settled[0].model_id).toBe('summary')
    expect(settled[0].delta_turns).toBe(7)
  })

  it('closes the attempt by id even when several are recorded', () => {
    let entries = applyCompressionEntry([], started({ attempt: 'a1' }))
    entries = applyCompressionEntry(entries, completed({ attempt: 'a1' }))
    entries = applyCompressionEntry(entries, started({ attempt: 'a2' }))
    const settled = applyCompressionEntry(entries, completed({ attempt: 'a2', summary_version: 5 }))
    expect(settled).toHaveLength(2)
    expect(settled.map((e) => [e.attempt, e.success])).toEqual([['a1', true], ['a2', true]])
    expect(settled.every((e) => !compressionIsPending(e))).toBe(true)
  })

  it('applies the same attempt twice only once', () => {
    // The starter connection mirrors a frame that the retained stream replays.
    let entries = applyCompressionEntry([], started())
    entries = applyCompressionEntry(entries, started())
    expect(entries).toHaveLength(1)
    entries = applyCompressionEntry(entries, completed())
    entries = applyCompressionEntry(entries, completed())
    expect(entries).toHaveLength(1)
    expect(entries[0].success).toBe(true)
  })

  it('a replayed started cannot resurrect a finished marker', () => {
    let entries = applyCompressionEntry([], started())
    entries = applyCompressionEntry(entries, completed())
    entries = applyCompressionEntry(entries, started())
    expect(entries).toHaveLength(1)
    expect(compressionIsPending(entries[0])).toBe(false)
  })

  it('shows a completed that arrived with nothing to close', () => {
    // A tab that attached mid-generation never saw the started event.
    const [entry] = applyCompressionEntry([], completed())
    expect(compressionIsPending(entry)).toBe(false)
    expect(entry.success).toBe(true)
  })
})

describe('attachCompressionFrame', () => {
  it('writes the attempt into meta.compressions of the assistant message', () => {
    const messages = [{ role: 'user' }, { role: 'assistant', content: 'a' }]
    const applied = attachCompressionFrame(messages, started())
    expect(applied.index).toBe(1)
    expect(compressionEntriesOf(applied.messages[1])[0].attempt).toBe('a1')
    // The conversation itself grew no message.
    expect(applied.messages).toHaveLength(2)
  })

  it('returns a new array and leaves the original message objects untouched', () => {
    const messages = [{ role: 'assistant', content: 'a' }]
    const applied = attachCompressionFrame(messages, completed())
    expect(applied.messages).not.toBe(messages)
    expect(applied.messages[0]).not.toBe(messages[0])
    expect(messages[0].meta).toBeUndefined()
  })

  it('keeps the other keys of the turn meta (model / tools snapshot)', () => {
    const messages = [{ role: 'assistant', meta: { model_id: 'm1', tool_ids: ['read_file'] } }]
    const applied = attachCompressionFrame(messages, completed())
    const meta = applied.messages[0].meta
    expect(meta.model_id).toBe('m1')
    expect(meta.tool_ids).toEqual(['read_file'])
    expect(meta.compressions).toHaveLength(1)
  })

  it('returns null for other frames, for an empty list, and for no change', () => {
    expect(attachCompressionFrame([{ role: 'assistant' }], { role: 'tool', name: 'x' })).toBeNull()
    expect(attachCompressionFrame([], started())).toBeNull()
    const messages = [{ role: 'assistant', meta: { compressions: [] } }]
    const once = attachCompressionFrame(messages, started())
    expect(attachCompressionFrame(once.messages, started())).toBeNull()
  })
})

describe('settleCompressionEntries', () => {
  it('drops markers whose completion event never arrived', () => {
    const messages = [
      { role: 'assistant', meta: { compressions: [{ attempt: 'a1', phase: 'started' }] } },
      { role: 'assistant', meta: { compressions: [{ attempt: 'a0', success: true }] } },
    ]
    const settled = settleCompressionEntries(messages)
    expect(compressionEntriesOf(settled[0])).toEqual([])
    // Finished markers are evidence and stay.
    expect(compressionEntriesOf(settled[1])).toHaveLength(1)
  })

  it('returns null when there is nothing to settle (no needless re-render)', () => {
    expect(settleCompressionEntries([{ role: 'assistant', meta: { compressions: [{ success: true }] } }])).toBeNull()
    expect(settleCompressionEntries([{ role: 'assistant' }])).toBeNull()
  })
})

describe('compression display helpers', () => {
  it('a pending attempt is a spinner, a failed one is an error', () => {
    const pending = applyCompressionEntry([], started())[0]
    expect(compressionIsPending(pending)).toBe(true)
    expect(compressionResultIcon(pending)).toBe('⏳')
    expect(compressionLabel(pending, t)).toBe('compressingContext')

    const ok = applyCompressionEntry([], completed())[0]
    const failed = applyCompressionEntry([], completed({ success: false, error: 'HTTP 400: too large' }))[0]
    expect(compressionFailed(failed)).toBe(true)
    expect(compressionResultIcon(failed)).toBe('\u2716\uFE0F')
    // The capsule stays short -- a provider error can be a whole HTML page; its
    // reason belongs to the tooltip and the detail block.
    expect(compressionLabel(failed, t)).toBe('contextCompressFailedShort')
    expect(compressionTooltip(failed, t)).toContain('HTTP 400: too large')
    expect(compressionTooltip(ok, t)).toBe('contextCompressed')

    expect(compressionResultIcon(ok)).toBe('\u2714\uFE0F')
    expect(compressionLabel(ok, t)).toBe('contextCompressed')
    // The subject of the capsule is a clamp: the context is squeezed, while
    // every turn of the transcript is kept.
    expect(compressionIcon()).toBe('\u{1F5DC}\uFE0F')
  })

  it('the finished marker states only what the compression produced', () => {
    // v{summary_version}:{summary_chars},{memory_entries}; trigger, budget,
    // turns and duration wait in the full list behind the result icon.
    const zh = {
      compressionStatChars: '{n}字符',
      compressionStatEntries: '{n}条目',
      compressionStatSep: ',',
    }
    const zhT = (key, vars) => {
      let out = zh[key] ?? key
      for (const [name, value] of Object.entries(vars || {})) out = out.replaceAll(`{${name}}`, value)
      return out
    }
    const done = applyCompressionEntry([], completed())[0]
    expect(compressionStats(done, zhT)).toBe('v4:1194字符,73条目')
    expect(compressionStats(done, t)).toContain('compressionStatChars {"n":1194}')

    // A running attempt has no numbers yet; neither has a failed one, whose
    // capsule states the failure instead.
    expect(compressionStats(applyCompressionEntry([], started())[0], t)).toBe('')
    expect(compressionStats(applyCompressionEntry([], completed({ success: false, error: 'x' }))[0], t)).toBe('')
    // A record missing some sizes still shows the ones it has.
    expect(compressionStats({ summary_version: 2 }, t)).toBe('v2')
    expect(compressionStats({ memory_entries: 0 }, t)).toBe('compressionStatEntries {"n":0}')
  })

  it('the detail is the audit trail, with the same keys as conversation.json', () => {
    const detail = compressionDetailLines(applyCompressionEntry([], completed())[0]).join('\n')
    for (const key of [
      'trigger=in_loop', 'model=summary', 'tokens=92000', 'threshold=65536',
      'turns=7 from=12', 'summarized_up_to_turn=18', 'summary_version=4',
      'summary_chars=1194', 'memory_entries=73', 'duration_ms=2400',
      'at=2026-01-01T00:00:00', 'status=ok',
    ]) {
      expect(detail).toContain(key)
    }
  })

  it('a failed detail keeps the reason', () => {
    const entry = applyCompressionEntry([], completed({ success: false, error: 'boom' }))[0]
    expect(compressionDetailLines(entry).join('\n')).toContain('status=failed error=boom')
  })
})

describe('compressionFrameFromEvent', () => {
  it('normalizes the global session-events shape onto the frame shape', () => {
    const frame = compressionFrameFromEvent({
      event: 'compression', session_id: 's9', phase: 'started', attempt: 'a9', trigger: 'manual',
    })
    expect(isCompressionFrame(frame)).toBe(true)
    expect(frame.session_id).toBe('s9')
    expect(applyCompressionEntry([], frame)[0].trigger).toBe('manual')
  })

  it('rejects everything else on that shared channel', () => {
    expect(compressionFrameFromEvent({ event: 'message', session_id: 's1', status: 'streaming' })).toBeNull()
    expect(compressionFrameFromEvent({ event: 'compression', phase: 'started' })).toBeNull()
    expect(compressionFrameFromEvent({ event: 'compression', session_id: 's1', phase: 'weird' })).toBeNull()
    expect(compressionFrameFromEvent(null)).toBeNull()
  })
})
