/**
 * Per-session execution-environment registry (frontend cache).
 *
 * A chat session that is bound to a child environment runs its tools -- and
 * its interactive terminal -- in that environment.  The page-wide
 * `remoteExecution` binding describes the **current** session only, which is
 * not enough: terminals render for **every** session the user opened one on
 * (the sidebar offers "open terminal" on any row), so a background session's
 * terminal would otherwise be opened as a local channel and spawn a
 * parent-side PTY for a session whose tools actually run in some child.
 *
 * This registry is the per-session answer to "which environment, which
 * workspace".  It is filled from session meta on restore, from the environment
 * selector when the user switches, and from the cheap
 * `GET /v1/sessions/{id}/meta` when a terminal is opened for a session this
 * page load never visited.  It is a cache of server-side truth, so losing it
 * only costs a re-fetch.
 *
 * Terminals need only the env **id**: the tab's single terminal socket routes
 * by `env_id` (web/src/lib/terminals-mux.js), so no child URL or credential
 * travels with it.
 */

/** sid -> env id / sid -> workspace. Plain objects keep Svelte's tracking cheap. */
export const sessionEnvRegistry = $state({ envs: {}, workspaces: {} })

/** Remember (or clear, with ''/null) the environment a session runs in. */
export function rememberSessionEnv(sessionId, envId) {
  if (!sessionId) return
  const next = { ...sessionEnvRegistry.envs }
  if (envId) next[sessionId] = envId
  else delete next[sessionId]
  sessionEnvRegistry.envs = next
}

/** Remember (or clear) the workspace a session's terminal should start in. */
export function rememberSessionWorkspace(sessionId, workspace) {
  if (!sessionId) return
  const next = { ...sessionEnvRegistry.workspaces }
  if (workspace) next[sessionId] = workspace
  else delete next[sessionId]
  sessionEnvRegistry.workspaces = next
}

/** Drop a session (deleted, or its bindings are no longer known). */
export function forgetSession(sessionId) {
  if (!sessionId) return
  rememberSessionEnv(sessionId, '')
  rememberSessionWorkspace(sessionId, '')
}

/** Last-known environment of a session ('' = local / unknown). */
export function sessionEnvId(sessionId) {
  return (sessionId && sessionEnvRegistry.envs[sessionId]) || ''
}

/** Last-known workspace of a session ('' = let the environment decide). */
export function sessionWorkspace(sessionId) {
  return (sessionId && sessionEnvRegistry.workspaces[sessionId]) || ''
}

/**
 * The environment a session's terminal channel belongs to.
 *
 * The **current** session follows the environment selector: during restore the
 * selector is set synchronously while the env record resolves asynchronously,
 * and the registry would briefly answer "local" for a session that is about to
 * run remotely.  Every other session uses its remembered binding, which is why
 * switching away must not drag a background terminal back to the parent.
 *
 * @param sessionId       session the terminal belongs to
 * @param currentSessionId session currently displayed ('' / null = none)
 * @param selectedEnvId   environment selector value for the current session
 * @returns env id ('' = local)
 */
export function resolveTerminalEnvId(sessionId, currentSessionId, selectedEnvId = '') {
  if (!sessionId) return ''
  if (sessionId === currentSessionId) return selectedEnvId || ''
  return sessionEnvRegistry.envs[sessionId] || ''
}
