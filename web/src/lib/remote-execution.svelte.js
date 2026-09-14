/**
 * 远程执行（remote execution）— 会话级执行环境绑定。
 *
 * A chat session bound to a remote environment runs ALL of its tools in the
 * child environment while inference (parent model + conversation history)
 * stays in the parent.  This module owns:
 *
 *  - the active binding (which session + which child env), set by ChatPage
 *    on session restore / env switch,
 *  - direct browser → child request helpers (workspace files, file journals,
 *    tools, terminal WebSocket) — large payloads never pass through the
 *    parent,
 *  - URL/token helpers derived from the registered setup URL.
 *
 * Revoke stays on the PARENT (it coordinates child journal restore first);
 * session deletion also stays on the parent (fire-and-forget child cleanup).
 */

import { remoteEnv } from './api.js'

/** Active binding for the current chat session.
 *  sessionId: string | null — the session this binding applies to
 *  env: { id, url, base, token, host, title } | null
 */
export const remoteExecution = $state({ sessionId: null, env: null })

/**
 * Parse a registered setup URL into base + token.
 *
 * The registered URL is the canonical ``scheme://netloc[/prefix]/v1/setup``
 * form and may carry ``?token=...`` (the only auth channel for URL-typed
 * resources and WebSocket handshakes).
 *
 * @returns {{ base: string, token: string, host: string }}
 *   base — scheme://netloc plus any path prefix before /v1 (no trailing /v1)
 *   token — query token or ''
 *   host — netloc for WebSocket URL construction
 */
export function parseSetupUrl(url) {
  const u = new URL(url)
  let base = `${u.protocol}//${u.host}`
  // Keep any path prefix before the /v1 segment (sub-path deployments).
  const segs = u.pathname.split('/').filter(Boolean)
  const v1Idx = segs.indexOf('v1')
  if (v1Idx > 0) {
    base += '/' + segs.slice(0, v1Idx).join('/')
  } else if (v1Idx === -1) {
    // No /v1 segment (not a setup URL) — keep the whole path.
    base += u.pathname
  }
  const token = u.searchParams.get('token') || ''
  return { base, token, host: u.host }
}

let _envListCache = null
let _envListCacheAt = 0

/** Fetch the parent's registered remote env list (short TTL cache). */
export async function fetchRemoteEnvList(force = false) {
  const now = Date.now()
  if (!force && _envListCache && now - _envListCacheAt < 10_000) {
    return _envListCache
  }
  const data = await remoteEnv.list()
  _envListCache = data.envs || []
  _envListCacheAt = now
  return _envListCache
}

/** Invalidate the list cache (after add/remove/push-update). */
export function invalidateRemoteEnvCache() {
  _envListCache = null
  _envListCacheAt = 0
}

/**
 * Resolve an env id (scheme://netloc) to a registered record.
 * @returns {Promise<object|null>}
 */
export async function resolveRemoteEnv(envId) {
  if (!envId) return null
  const list = await fetchRemoteEnvList()
  return list.find(e => e.id === envId) ?? null
}

/**
 * Bind a session to a remote execution environment (or unbind with ''/null).
 *
 * @param {string|null} sessionId session id the binding applies to
 * @param {string} envId registered env id ('' = local)
 * @returns {Promise<object|null>} the env record or null when local/unknown
 */
export async function bindSessionToRemoteEnv(sessionId, envId) {
  if (!envId) {
    remoteExecution.sessionId = null
    remoteExecution.env = null
    return null
  }
  let record = null
  try {
    record = await resolveRemoteEnv(envId)
  } catch {
    record = null
  }
  if (!record) {
    remoteExecution.sessionId = null
    remoteExecution.env = null
    return null
  }
  remoteExecution.sessionId = sessionId ?? null
  // Tunnel env (child dials into this parent over the reverse WS tunnel):
  // the browser must NOT talk to the child directly (it may be unreachable
  // behind NAT — that is the point of the tunnel).  Route everything through
  // the parent's same-origin bridge /v1/tunnel-proxy/{env_id}/... instead:
  // cookies work, no CORS, no child token in the URL (the child self-authorizes
  // requests arriving over the tunnel).
  const isTunnel = record.id.startsWith('tunnel:') || record.transport === 'ws-tunnel'
  if (isTunnel) {
    const prefix = `/v1/tunnel-proxy/${encodeURIComponent(record.id)}`
    remoteExecution.env = {
      id: record.id,
      url: '',
      base: `${location.protocol}//${location.host}${prefix}`,
      token: '',
      host: location.host,
      title: record.app_title || record.title || '',
      prefix,
      transport: 'ws-tunnel',
    }
  } else {
    const { base, token, host } = parseSetupUrl(record.url)
    remoteExecution.env = {
      id: record.id,
      url: record.url,
      base,
      token,
      host,
      title: record.app_title || record.title || '',
      prefix: '',
    }
  }
  return remoteExecution.env
}

export function clearRemoteBinding() {
  remoteExecution.sessionId = null
  remoteExecution.env = null
}

/** True when the given session (or the bound one when omitted) is remote. */
export function isRemoteSession(sessionId = null) {
  if (!remoteExecution.env) return false
  if (sessionId === null || sessionId === undefined) return true
  return remoteExecution.sessionId === sessionId
}

/**
 * Resolve the remote env a panel targets, following the active binding.
 *
 * Unlike the terminal (always session-scoped, only reachable from a session),
 * the workspace file manager can be opened *before* the first message creates
 * a session.  In that state ``handleRemoteEnvChange`` binds the selected env
 * with ``sessionId = null``, so comparing ``remoteExecution.sessionId ===
 * sessionId`` (both null) already matches and the panel follows the
 * execution-environment selector instead of silently falling back to local.
 *
 * @param {string|null} sessionId currently displayed session (null = none yet)
 * @param {boolean} [localMode] force local (e.g. "open session log dir")
 * @returns {object|null} env record to target, or null for the local env
 */
export function resolvePanelRemoteEnv(sessionId = null, localMode = false) {
  if (localMode || !remoteExecution.env) return null
  return remoteExecution.sessionId === (sessionId ?? null)
    ? remoteExecution.env
    : null
}

/**
 * Build an absolute child URL for a /v1/ path, appending the token.
 * Suitable for fetch() and for media src attributes (img/audio/video/
 * document previews) — the token query param is the URL auth channel.
 */
export function buildRemoteUrl(path, { query = {} } = {}) {
  const env = remoteExecution.env
  if (!env) throw new Error('No remote environment bound')
  const u = new URL(env.base + path)
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== null) u.searchParams.set(k, String(v))
  }
  if (env.token) u.searchParams.set('token', env.token)
  return u.toString()
}

/** Build a child WebSocket URL (ws/wss) with token. */
export function buildRemoteWsUrl(path, params = {}) {
  const env = remoteExecution.env
  if (!env) throw new Error('No remote environment bound')
  // 协议跟随子环境自身的 scheme（父子环境可能 http/https 不一致）；
  // 登记 URL 缺失时回退到页面协议。
  const proto = env.url && env.url.startsWith('https')
    ? 'wss:'
    : (env.url ? 'ws:' : (location.protocol === 'https:' ? 'wss:' : 'ws:'))
  const u = new URL(`${proto}//${env.host}`)
  // Tunnel envs: the WS path is bridged under /v1/tunnel-proxy/{env_id}/...
  // (same origin as the page); direct envs keep the bare child path.
  u.pathname = (env.prefix || '') + path
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null) u.searchParams.set(k, String(v))
  }
  if (env.token) u.searchParams.set('token', env.token)
  return u.toString()
}

function throwRemoteError(res, data) {
  const msg = data?.message
    || data?.error
    || (Array.isArray(data?.errors) ? data.errors.join('\n') : null)
    || `Remote request failed: ${res.status}`
  const err = new Error(msg)
  err.status = res.status
  err.data = data
  err.code = data?.error
  throw err
}

/**
 * fetch() the bound child environment with JSON in/out.
 *
 * @param {string} method HTTP method
 * @param {string} path   absolute /v1/ path (query included as needed)
 * @param {object|null} body JSON body (omitted for GET/DELETE)
 * @returns {Promise<any>} parsed JSON
 */
export async function remoteRequest(method, path, body = null) {
  const url = buildRemoteUrl(path)
  // token 查询参数覆盖 GET（子端 GET 授权）；Authorization 头覆盖
  // POST/PUT/DELETE（跨域浏览器请求可携带自定义头）。两者都带无副作用。
  const headers = { 'Content-Type': 'application/json' }
  if (remoteExecution.env?.token) {
    headers.Authorization = `Bearer ${remoteExecution.env.token}`
  }
  const opts = { method, headers }
  if (body !== null && body !== undefined) opts.body = JSON.stringify(body)
  const res = await fetch(url, opts)
  const data = await res.json().catch(() => null)
  if (!res.ok) throwRemoteError(res, data)
  // 2xx 但非 JSON（如误命中静态页面 / 空响应）：给出可读错误，
  // 而不是让调用方读到 null 后在属性访问上抛 TypeError。
  if (data === null) {
    const err = new Error(`Remote returned non-JSON response: ${res.status}`)
    err.status = res.status
    throw err
  }
  return data
}

/**
 * Child workspace API — mirrors the parent `workspace` object from api.js.
 * All paths are identical on the child; only the host + token differ.
 */
export const remoteWorkspace = {
  list: (path, page = 1, pageSize = 50, restrict = true, { sort = 'name', nameFilter = '' } = {}) => {
    const params = new URLSearchParams({
      path, page: String(page), page_size: String(pageSize),
      restrict: restrict ? '1' : '0', sort,
    })
    if (nameFilter) params.set('name_filter', nameFilter)
    return remoteRequest('GET', `/v1/workspace/list?${params.toString()}`)
  },
  tree: (path) => remoteRequest('GET', `/v1/workspace/tree?path=${encodeURIComponent(path)}`),
  children: (path) => remoteRequest('GET', `/v1/workspace/children?path=${encodeURIComponent(path)}`),
  search: (path, query, nameFilter = '') => {
    const params = new URLSearchParams({ path, query })
    if (nameFilter) params.set('name_filter', nameFilter)
    return remoteRequest('GET', `/v1/workspace/search?${params.toString()}`)
  },
  content: (path, restrict = true) => buildRemoteUrl(
    `/v1/workspace/content?path=${encodeURIComponent(path)}&restrict=${restrict ? 1 : 0}`),
  download: (path, restrict = true) => buildRemoteUrl(
    `/v1/workspace/download?path=${encodeURIComponent(path)}&restrict=${restrict ? 1 : 0}`),
  thumbnail: (path, restrict = true) => buildRemoteUrl(
    `/v1/workspace/thumbnail?path=${encodeURIComponent(path)}&restrict=${restrict ? 1 : 0}`),
  pasteDir: () => remoteRequest('GET', '/v1/workspace/paste-dir'),
  rename: (path, newName) => remoteRequest('POST', '/v1/workspace/rename', { path, new_name: newName }),
  mkdir: (parentPath, name) => remoteRequest('POST', '/v1/workspace/mkdir', { parent_path: parentPath, name }),
  duplicate: (path) => remoteRequest('POST', '/v1/workspace/duplicate', { path }),
  delete: (path) => remoteRequest('DELETE', '/v1/workspace/delete', { path }),
  move: (paths, destDir, overwrite = false) => remoteRequest('POST', '/v1/workspace/move', { paths, dest_dir: destDir, overwrite }),
  copy: (paths, destDir, overwrite = false) => remoteRequest('POST', '/v1/workspace/copy', { paths, dest_dir: destDir, overwrite }),
  uploadInit: (data) => remoteRequest('POST', '/v1/workspace/upload/init', data),
  uploadChunk: (uploadId, chunk, blob, onProgress) => {
    // XHR for progress reporting (mirrors parent uploadChunkWithProgress).
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest()
      const u = new URL(buildRemoteUrl(
        `/v1/workspace/upload/${encodeURIComponent(uploadId)}/chunk/${chunk.parallel_id}`))
      xhr.open('PUT', u.toString())
      xhr.setRequestHeader('Content-Type', 'application/octet-stream')
      xhr.setRequestHeader('X-Upload-Offset', String(chunk.offset))
      xhr.setRequestHeader('X-Upload-Size', String(chunk.size))
      xhr.setRequestHeader('X-File-Size', String(chunk.file_size))
      if (remoteExecution.env?.token) {
        xhr.setRequestHeader('Authorization', `Bearer ${remoteExecution.env.token}`)
      }
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable && onProgress) onProgress(event.loaded)
      }
      xhr.onload = () => {
        let data = null
        try { data = xhr.responseText ? JSON.parse(xhr.responseText) : null } catch { data = xhr.responseText }
        if (xhr.status >= 200 && xhr.status < 300) resolve(data)
        else {
          const message = data?.message || data?.error || xhr.responseText || `Request failed: ${xhr.status}`
          const err = new Error(message)
          err.status = xhr.status
          err.data = data
          reject(err)
        }
      }
      xhr.onerror = () => reject(new Error('Upload network error'))
      xhr.onabort = () => reject(new DOMException('Upload aborted', 'AbortError'))
      xhr.send(blob)
    })
  },
  uploadComplete: (uploadId) => remoteRequest('POST', `/v1/workspace/upload/${encodeURIComponent(uploadId)}/complete`, {}),
  uploadCancel: (uploadId) => remoteRequest('DELETE', `/v1/workspace/upload/${encodeURIComponent(uploadId)}`),
}

/**
 * Child session-scoped read APIs (file journals / diffs).
 * Revoke + delete intentionally NOT included — the parent coordinates them.
 */
export const remoteSessions = {
  fileJournals: (sessionId) => remoteRequest('GET', `/v1/sessions/${encodeURIComponent(sessionId)}/file-journals`),
  fileJournalDiff: (sessionId, turnKey) => remoteRequest('GET', `/v1/sessions/${encodeURIComponent(sessionId)}/file-journals/${encodeURIComponent(turnKey)}`),
}

/**
 * Fetch a child's default workspace path (AGENTS_WORKSPACE env var).
 *
 * @param {object|null} [envOverride] env record to target; defaults to the
 *   currently bound env.  Needed when a panel (e.g. file manager) points at a
 *   session's child env that differs from the active binding.
 */
export async function fetchRemoteWorkspacePath(envOverride = null) {
  const env = envOverride || remoteExecution.env
  if (!env) throw new Error('No remote environment bound')
  const u = new URL(env.base + '/v1/env')
  if (env.token) u.searchParams.set('token', env.token)
  const res = await fetch(u.toString())
  const data = await res.json().catch(() => null)
  if (!res.ok) throw new Error(`Remote request failed: ${res.status}`)
  return (data?.env || {}).AGENTS_WORKSPACE || ''
}

/** Fetch the child's tool list (for the ToolSelector override). */
export async function fetchRemoteTools() {
  const data = await remoteRequest('GET', '/v1/tools')
  return data?.tools || data?.items || []
}

/**
 * Destroy the child terminal for a session (the terminal runs on the child).
 */
export function destroyRemoteTerminal(sessionId) {
  return remoteRequest('DELETE', `/v1/terminals/${encodeURIComponent(sessionId)}`).catch(err => {
    console.warn('Failed to delete remote terminal:', err)
  })
}
