/**
 * What the backend's session-status snapshot means for this page's belief.
 *
 * The events stream opens (and re-opens) with an ``init`` snapshot: every session
 * the backend has, each with the status it is in. That snapshot is a complete
 * statement, so it *replaces* what the page thought rather than adding to it --
 * which is the part that used to be wrong. A set that only ever grew kept
 * reporting a session as streaming after its generation had finished while this
 * tab's events were away, and every consumer of that set (re-arming the frame
 * watch, the "still working" affordance, the re-subscribe-on-done check) then
 * acted on a run that no longer existed.
 */

/** The statuses that say a generation is over, however it ended. */
export function isTerminalStatus(status) {
  return String(status || '').startsWith('done_')
}

/**
 * Split one snapshot into the three sets a page needs, and name the sessions
 * whose local view is now a lie.
 *
 * @param previousStreaming what this page believed was running until now
 * @param sessions          ``data.sessions`` of the init event: sid -> status
 * @param carriedByThisTab  sessions this tab is itself reading over its own
 *                          ``POST /v1/infer/stream`` -- streaming by definition,
 *                          even if the snapshot was taken a beat before the
 *                          backend registered that run
 * @returns {{ streaming: Set, paused: Set, ended: string[] }}
 */
export function reconcileStatusSnapshot(previousStreaming, sessions, carriedByThisTab = new Set()) {
  const streaming = new Set()
  const paused = new Set()
  const ended = []

  for (const [sessionId, status] of Object.entries(sessions || {})) {
    if (status === 'streaming') streaming.add(sessionId)
    else if (status === 'paused') paused.add(sessionId)
    else if (isTerminalStatus(status) && previousStreaming.has(sessionId)) {
      // This page still shows it working -- a streaming flag, a compression
      // marker whose completion event never arrived. The snapshot says otherwise.
      ended.push(sessionId)
    }
  }
  for (const sessionId of previousStreaming) {
    if (carriedByThisTab.has(sessionId)) streaming.add(sessionId)
  }

  return { streaming, paused, ended }
}
