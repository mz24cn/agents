/**
 * Streaming-frame batching (used by the chat page).
 *
 * The network readers can deliver thousands of one-character frames faster than
 * Svelte/Markdown can render them, so frames are applied at most once per
 * animation frame: one DOM commit per batch keeps the tab responsive.
 *
 * The animation frame is an optimisation, never a precondition. Browsers do
 * **not** run `requestAnimationFrame` callbacks for a hidden tab, so a rAF-only
 * scheduler silently freezes every conversation that is not currently on screen:
 * nothing gets applied, the drain callbacks that advance the retained cursor
 * and end the stream never run, and the whole generation lands as one wall of
 * output the moment the user comes back (or the page re-subscribes). While the
 * document is not visible the batcher therefore drains on a timer instead --
 * still batched, just at a rate no pixels depend on.
 */

/** Default accessor for the tool-call identity used by the merge rule. */
export function defaultToolUseId(frame) {
  return frame?.tool_use_id || null
}

/**
 * Can two queued frames be applied as one? Sequence numbers are transport
 * acknowledgements, so frames carrying one stay separate: the retained cursor
 * may only advance after each frame has actually been applied to the UI.
 */
export function canMergeStreamFrames(previous, next, toolUseId = defaultToolUseId) {
  if (!previous || !next || previous.role !== next.role) return false
  if (previous.__streamSeq != null || next.__streamSeq != null) return false
  if (next.role === 'assistant') {
    return !previous.tool_calls && !next.tool_calls
      && (previous.agent_id || previous.assistant_id || '') === (next.agent_id || next.assistant_id || '')
  }
  if (next.role === 'tool' && previous.streaming === true && next.streaming === true) {
    return previous.name === next.name
      && toolUseId(previous) === toolUseId(next)
      && (previous.agent_id || '') === (next.agent_id || '')
      && (previous.target_agent_id || '') === (next.target_agent_id || '')
  }
  return false
}

/** Concatenate two mergeable frames (text/thinking for assistants, deltas for tools). */
export function mergeStreamFrame(previous, next) {
  if (next.role === 'assistant') {
    return {
      ...previous,
      ...next,
      content: (previous.content || '') + (next.content || ''),
      thinking: (previous.thinking || '') + (next.thinking || ''),
    }
  }
  return {
    ...previous,
    ...next,
    delta: (previous.delta || '') + (next.delta || ''),
  }
}

/** Drain rate for a hidden document: 10 Hz, no paint to keep up with. */
export const HIDDEN_BATCH_MS = 100

function documentIsHidden() {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden'
}

/**
 * Create a batcher: `push(frame)` queues, `then(callback)` runs after the queue
 * has been drained. Both schedule a flush; the flush applies merged frames in
 * order and then runs the queued `then` callbacks.
 *
 * @param onFrame    applied once per (merged) frame
 * @param toolUseId  accessor for tool-call identity (defaults to `tool_use_id`)
 * @param isHidden   true when nothing is being painted; injectable for tests
 */
export function createStreamFrameBatcher(onFrame, {
  toolUseId = defaultToolUseId,
  isHidden = documentIsHidden,
} = {}) {
  let queue = []
  let scheduled = false
  let afterDrain = []

  const flush = () => {
    scheduled = false
    const pending = queue
    queue = []
    const merged = []
    for (const frame of pending) {
      const last = merged.at(-1)
      if (canMergeStreamFrames(last, frame, toolUseId)) merged[merged.length - 1] = mergeStreamFrame(last, frame)
      else merged.push(frame)
    }
    for (const frame of merged) onFrame(frame)
    if (queue.length) schedule()
    else {
      const callbacks = afterDrain
      afterDrain = []
      for (const callback of callbacks) callback()
    }
  }

  const schedule = () => {
    if (scheduled) return
    scheduled = true
    // Paint-aligned while something is actually being painted; timer-driven when
    // the tab is hidden (rAF is frozen there) or when rAF is unavailable.
    if (isHidden() || typeof requestAnimationFrame !== 'function') {
      setTimeout(flush, HIDDEN_BATCH_MS)
    } else {
      requestAnimationFrame(flush)
    }
  }

  return {
    push(frame) { queue.push(frame); schedule() },
    then(callback) { afterDrain.push(callback); schedule() },
    /** Diagnostics/tests: how much is waiting for a flush. */
    pendingCount() { return queue.length + afterDrain.length },
  }
}
