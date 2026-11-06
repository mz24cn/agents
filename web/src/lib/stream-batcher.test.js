import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  createStreamFrameBatcher,
  canMergeStreamFrames,
  mergeStreamFrame,
  HIDDEN_BATCH_MS,
} from './stream-batcher.js'

// ---------------------------------------------------------------------------
// Why this file exists: the chat page batches streamed frames so a burst of
// one-character tokens does not thrash the DOM. It used to schedule that batch
// with requestAnimationFrame unconditionally -- and browsers do not run rAF
// callbacks for a hidden tab. A conversation that was not on screen therefore
// applied nothing at all (the drain callbacks that advance the resume cursor
// and finish the stream never ran either), and everything landed at once when
// the user came back. These tests pin both halves of that contract.
// ---------------------------------------------------------------------------

describe('createStreamFrameBatcher', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('drains on a timer while the tab is hidden, because rAF is frozen there', () => {
    vi.useFakeTimers()
    // A hidden tab never invokes the rAF callback.
    const raf = vi.fn(() => 0)
    vi.stubGlobal('requestAnimationFrame', raf)

    const applied = []
    const batcher = createStreamFrameBatcher((frame) => applied.push(frame), {
      isHidden: () => true,
    })

    let finished = false
    batcher.push({ role: 'assistant', content: 'a' })
    batcher.push({ role: 'assistant', content: 'b' })
    batcher.then(() => { finished = true })

    expect(applied).toEqual([])   // batching, not synchronous application
    expect(raf).not.toHaveBeenCalled()

    vi.advanceTimersByTime(HIDDEN_BATCH_MS + 10)

    expect(applied.map((f) => f.content)).toEqual(['ab'])   // merged into one paint
    expect(finished).toBe(true)                             // drain callbacks run too
  })

  it('still paints on animation frames while the page is visible', () => {
    const callbacks = []
    vi.stubGlobal('requestAnimationFrame', (cb) => { callbacks.push(cb); return 1 })

    const applied = []
    const batcher = createStreamFrameBatcher((frame) => applied.push(frame), {
      isHidden: () => false,
    })
    batcher.push({ role: 'assistant', content: 'x' })
    batcher.push({ role: 'assistant', content: 'y' })

    expect(applied).toEqual([])
    expect(callbacks).toHaveLength(1)
    callbacks[0]()
    expect(applied.map((f) => f.content)).toEqual(['xy'])
  })

  it('keeps frames that carry a stream sequence apart, so the cursor advances per frame', () => {
    vi.useFakeTimers()
    vi.stubGlobal('requestAnimationFrame', () => 0)
    const applied = []
    const batcher = createStreamFrameBatcher((frame) => applied.push(frame), {
      isHidden: () => true,
    })
    batcher.push({ role: 'assistant', content: 'a', __streamSeq: 1 })
    batcher.push({ role: 'assistant', content: 'b', __streamSeq: 2 })

    vi.advanceTimersByTime(HIDDEN_BATCH_MS + 10)

    expect(applied.map((f) => f.__streamSeq)).toEqual([1, 2])
  })

  it('continues draining frames pushed while a flush is in flight', () => {
    vi.useFakeTimers()
    vi.stubGlobal('requestAnimationFrame', () => 0)
    const applied = []
    const batcher = createStreamFrameBatcher((frame) => {
      applied.push(frame.content)
      if (applied.length === 1) batcher.push({ role: 'assistant', content: 'second' })
    }, { isHidden: () => true })

    batcher.push({ role: 'assistant', content: 'first' })
    vi.advanceTimersByTime(HIDDEN_BATCH_MS * 3)
    expect(applied).toEqual(['first', 'second'])
    expect(batcher.pendingCount()).toBe(0)
  })
})

describe('merge rules', () => {
  it('merges assistant text and thinking, and tool deltas of the same call', () => {
    expect(mergeStreamFrame(
      { role: 'assistant', content: 'a', thinking: 't1' },
      { role: 'assistant', content: 'b', thinking: 't2' },
    )).toMatchObject({ content: 'ab', thinking: 't1t2' })
    expect(mergeStreamFrame(
      { role: 'tool', streaming: true, name: 'exec', delta: 'x' },
      { role: 'tool', streaming: true, name: 'exec', delta: 'y' },
    ).delta).toBe('xy')
  })

  it('does not merge across roles, tool calls, or sequenced frames', () => {
    const same = { role: 'tool', streaming: true, name: 'exec', tool_use_id: 'a' }
    const other = { ...same, tool_use_id: 'b' }
    expect(canMergeStreamFrames(same, other)).toBe(false)
    expect(canMergeStreamFrames({ role: 'assistant', content: 'a' }, { role: 'tool' })).toBe(false)
    expect(canMergeStreamFrames({ role: 'assistant', __streamSeq: 1 }, { role: 'assistant' })).toBe(false)
  })
})
