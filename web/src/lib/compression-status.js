/**
 * Context-compression markers.
 *
 * Compressing a session's context (summary + memory) stops a running inference
 * for seconds and produces no token, so the user would otherwise see a chat
 * that is simply quiet -- and after the round finished, nothing in the session
 * said it had happened at all (each new summary version overwrites the
 * previous one).
 *
 * It is therefore presented the way a tool call is: an attempt appears inside
 * the assistant message it belongs to, as a capsule pair (icon + label, then a
 * status icon that opens the details), pending while it runs and settled when
 * it finishes.  Two feeds drive that one piece of state:
 *
 *   - the live pair of SSE events per attempt -- ``phase: 'started'`` before
 *     the request, ``phase: 'completed'`` with the outcome -- arriving on the
 *     session's frame stream, or on the global session-events channel when no
 *     generation is live (manual regeneration of an idle session);
 *   - ``conversation.json``, where the same attempt is recorded on the turn it
 *     ran for (``messages[i].meta.compressions``).  That is what makes the
 *     marker survive a reload: re-entering a finished session restores it at
 *     the same position, with no stream involved.
 *
 * Both feeds produce entries of the same shape, and every attempt carries an
 * ``attempt`` id so a frame that reaches the client twice (mirrored to the
 * starter connection and replayed from the retained stream) is applied once.
 */

export const COMPRESSION_STATUS = 'context_compression'

/** Fields of a lifecycle frame that describe transport, not the attempt. */
const TRANSPORT_FIELDS = new Set(['role', 'status', 'session_id', 'phase'])

/** Frames the page must not treat as conversation content. */
export function isCompressionFrame(msg) {
  return !!msg && msg.role === 'status' && msg.status === COMPRESSION_STATUS
}

/**
 * Normalize one event of the global session-events channel
 * ``{event: 'compression', session_id, phase, ...}`` into the same shape a
 * session-stream frame carries, or ``null`` for anything else.
 */
export function compressionFrameFromEvent(event) {
  if (!event || event.event !== 'compression' || !event.session_id) return null
  if (event.phase !== 'started' && event.phase !== 'completed') return null
  return { ...event, role: 'status', status: COMPRESSION_STATUS }
}

/**
 * The compression attempts recorded on a message.
 *
 * Reading this from a message restored from ``conversation.json`` is what lets
 * a finished session still show where its context was rewritten.
 */
export function compressionEntriesOf(msg) {
  const entries = msg?.meta?.compressions
  return Array.isArray(entries) ? entries.filter((e) => e && typeof e === 'object') : []
}

/** A still-running attempt (its ``completed`` event has not been seen). */
export function compressionIsPending(entry) {
  return !!entry && entry.phase === 'started'
}

/** A finished attempt that failed; ``success`` is absent while pending. */
export function compressionFailed(entry) {
  return !!entry && !compressionIsPending(entry) && entry.success === false
}

/** One audit entry out of a lifecycle frame -- the persisted shape. */
function entryFromFrame(frame) {
  const entry = {}
  for (const [key, value] of Object.entries(frame)) {
    if (!TRANSPORT_FIELDS.has(key)) entry[key] = value
  }
  if (frame.phase === 'started') entry.phase = 'started'
  return entry
}

/**
 * The message a compression of this session belongs to: the last assistant
 * turn, or the last message when there is none yet (a first request that was
 * compressed before it was sent).  Deliberately the same rule the backend uses
 * when it writes the audit entry, so the live marker and the restored one land
 * on the same message.
 */
export function compressionTargetIndex(messages) {
  if (!Array.isArray(messages)) return -1
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i]?.role === 'assistant') return i
  }
  return messages.length ? messages.length - 1 : -1
}

/**
 * Apply one lifecycle frame to the list of attempts of one message.
 *
 * ``completed`` closes the attempt it answers instead of adding a second
 * marker, and a repeated ``attempt`` id is applied once -- the same attempt can
 * reach a client twice, and a session restored from disk can be fed frames it
 * already has.  A ``completed`` with nothing to close is still shown: a tab
 * that attached mid-generation never saw the ``started``.
 */
export function applyCompressionEntry(entries, frame) {
  const next = Array.isArray(entries) ? [...entries] : []
  const entry = entryFromFrame(frame)
  const attempt = entry.attempt || ''

  const sameAttempt = next.findIndex((e) => attempt && e.attempt === attempt)
  if (sameAttempt >= 0) {
    // Already seen this attempt: only a completion may overwrite it, and only
    // once -- a replayed ``started`` must not resurrect a finished marker.
    if (frame.phase === 'completed' && compressionIsPending(next[sameAttempt])) {
      next[sameAttempt] = entry
    }
    return next
  }
  if (frame.phase !== 'completed') {
    next.push(entry)
    return next
  }
  for (let i = next.length - 1; i >= 0; i--) {
    if (compressionIsPending(next[i])) {
      next[i] = entry
      return next
    }
  }
  next.push(entry)
  return next
}

/**
 * Attach one compression frame to the message list.
 *
 * Returns ``{messages, index}`` with a NEW array (Svelte state must see a new
 * reference) whose target message carries the updated ``meta.compressions``, or
 * ``null`` when the frame is not a compression event or there is no message to
 * attach it to.  Nothing is appended to the conversation itself: a compression
 * is bookkeeping about a turn, not a turn.
 */
export function attachCompressionFrame(messages, frame) {
  if (!isCompressionFrame(frame)) return null
  const index = compressionTargetIndex(messages)
  if (index < 0) return null

  const msg = messages[index]
  const prev = compressionEntriesOf(msg)
  const next = applyCompressionEntry(prev, frame)
  if (next.length === prev.length && next.every((e, i) => e === prev[i])) return null

  const meta = msg.meta && typeof msg.meta === 'object' ? { ...msg.meta } : {}
  meta.compressions = next
  const updated = [...messages]
  updated[index] = { ...msg, meta }
  return { messages: updated, index }
}

/**
 * The capsule icons, mirroring a tool call: the call capsule carries the
 * subject of the work, the result capsule its outcome (⏳ / ✔️ / ✖️).
 *
 * The clamp (🗜️) rather than a broom: nothing is swept away -- the older turns
 * are squeezed into the summary, and the transcript keeps them.
 */
export function compressionIcon() {
  return '\u{1F5DC}\uFE0F'
}

/** Same outcome glyphs the tool capsules use; the state lives in this icon. */
export function compressionResultIcon(entry) {
  if (compressionIsPending(entry)) return '⏳'
  return compressionFailed(entry) ? '\u2716\uFE0F' : '\u2714\uFE0F'
}

/**
 * Short label of the attempt; the pending one says so out loud.
 *
 * Deliberately short: this sits in an inline capsule next to the tool calls of
 * the same turn.  A failure's reason belongs to the detail block (and the
 * tooltip), not to the capsule -- a provider error can be a whole HTML page.
 */
export function compressionLabel(entry, t) {
  if (!entry) return ''
  if (compressionIsPending(entry)) return t('compressingContext')
  if (compressionFailed(entry)) return t('contextCompressFailedShort')
  return t('contextCompressed')
}

/** The capsule's tooltip: the label plus the reason of a failed attempt. */
export function compressionTooltip(entry, t) {
  const label = compressionLabel(entry, t)
  if (!compressionFailed(entry)) return label
  return `${label}: ${shorten(entry.error, 120)}`
}

/**
 * The capsule says what the compression produced, nothing else:
 * ``v2:1194字符,73条目`` -- summary version, summary size, memory entries.
 * The rest (trigger, budget, turns, duration) is in the full list behind the
 * result icon, and in conversation.json under the same keys.
 */
export function compressionStats(entry, t) {
  if (!entry || compressionIsPending(entry) || compressionFailed(entry)) return ''
  const bits = []
  if (entry.summary_chars != null) bits.push(t('compressionStatChars', { n: entry.summary_chars }))
  if (entry.memory_entries != null) bits.push(t('compressionStatEntries', { n: entry.memory_entries }))
  const version = entry.summary_version == null ? '' : `v${entry.summary_version}`
  const tail = bits.join(t('compressionStatSep'))
  if (!version) return tail
  return tail ? `${version}:${tail}` : version
}

/**
 * The audit trail of one attempt, one line per fact -- the same key/values that
 * are persisted in conversation.json, so what the user reads here can be
 * grepped in the file afterwards.
 */
export function compressionDetailLines(entry) {
  if (!entry) return []
  const lines = []
  if (entry.at) lines.push(`at=${entry.at}`)
  lines.push(`trigger=${entry.trigger || 'auto'}`)
  if (entry.model_id) lines.push(`model=${entry.model_id}`)
  if (entry.tokens != null) lines.push(`tokens=${entry.tokens}`)
  if (entry.threshold != null) lines.push(`threshold=${entry.threshold}`)
  if (entry.delta_turns != null) {
    lines.push(`turns=${entry.delta_turns} from=${entry.delta_start ?? 0}`)
  }
  if (entry.summarized_up_to_turn != null) {
    lines.push(`summarized_up_to_turn=${entry.summarized_up_to_turn}`)
  }
  if (entry.summary_version != null) lines.push(`summary_version=${entry.summary_version}`)
  if (entry.summary_chars != null) lines.push(`summary_chars=${entry.summary_chars}`)
  if (entry.memory_entries != null) {
    lines.push(`memory_entries=${entry.memory_entries}${entry.memory_rejected ? ` rejected=${entry.memory_rejected}` : ''}`)
  }
  if (entry.duration_ms != null) lines.push(`duration_ms=${entry.duration_ms}`)
  if (entry.retries) lines.push(`retries=${entry.retries}`)
  if (compressionIsPending(entry)) lines.push('status=running')
  else if (compressionFailed(entry)) lines.push(`status=failed error=${shorten(entry.error, 200)}`)
  else lines.push('status=ok')
  return lines
}

/**
 * Drop attempts that never got their ``completed`` event.
 *
 * The backend always answers a ``started`` on the same stream, so an unanswered
 * one means those frames were lost (a connection was cut, a tab attached late).
 * The authoritative record is in ``conversation.json`` and arrives with the next
 * load of the session; meanwhile a marker that claims to still be running would
 * be the one thing this display must never do.
 */
export function settleCompressionEntries(messages) {
  if (!Array.isArray(messages)) return null
  let changed = false
  const next = messages.map((msg) => {
    const entries = compressionEntriesOf(msg)
    const kept = entries.filter((e) => !compressionIsPending(e))
    if (kept.length === entries.length) return msg
    changed = true
    return { ...msg, meta: { ...(msg.meta && typeof msg.meta === 'object' ? msg.meta : {}), compressions: kept } }
  })
  return changed ? next : null
}

function shorten(text, max) {
  const s = String(text || '').replace(/\s+/g, ' ').trim()
  return s.length > max ? `${s.slice(0, max - 1)}\u2026` : s
}
