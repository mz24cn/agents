/**
 * API client module — unified fetch wrapper and resource endpoints.
 *
 * All paths are relative (e.g. "/v1/models") so that Vite's dev-server
 * proxy forwards them to the Python backend automatically.
 */

import { ensureAuthenticated } from './auth-state.svelte.js'

function streamDebug(event, details = {}) {
  try {
    if (localStorage.getItem('session_stream_debug') !== '1') return
    console.warn(`[session-stream] ${event}`, {
      at: new Date().toISOString(),
      ...details,
    })
  } catch { /* diagnostics must never affect streaming */ }
}

function shouldLogStreamFrame(seq, data) {
  // Logging every one-character token can itself freeze DevTools and make the
  // stream appear stalled. Always log structural/final frames, but sample noisy
  // content/delta frames so diagnostics remain usable during long talk_to runs.
  if (data?.streaming === false || data?.role === 'usage' || data?.role === 'system' || data?.role === 'user') return true
  if (data?.tool_calls) return true
  return Number.isFinite(seq) && seq % 100 === 0
}

function isAuthFailure(res) {
  return res && res.status === 401
}

export async function apiFetch(path, opts = {}, { retryAuth = true } = {}) {
  let res = await fetch(path, opts)
  if (retryAuth && isAuthFailure(res) && String(path).startsWith('/v1/') && !String(path).startsWith('/v1/auth/login')) {
    await ensureAuthenticated()
    res = await fetch(path, opts)
  }
  return res
}

async function readJsonMaybe(res) {
  try { return await res.json() } catch { return null }
}

/**
 * Base fetch helper with unified error handling.
 *
 * @param {string} method  HTTP method
 * @param {string} path    URL path (e.g. "/v1/models")
 * @param {object|null} body  JSON body (omitted for GET/DELETE)
 * @returns {Promise<any>} Parsed JSON response
 */
function throwResponseError(res, data) {
  // Support both single "error" string and "errors" array
  const msg = data?.message
    || data?.error
    || (Array.isArray(data?.errors) ? data.errors.join('\n') : null)
    || `Request failed: ${res.status}`
  const err = new Error(msg)
  err.status = res.status
  err.data = data
  err.code = data?.error
  throw err
}

async function request(method, path, body = null) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  }
  if (body !== null) {
    opts.body = JSON.stringify(body)
  }
  const res = await apiFetch(path, opts)
  const data = await readJsonMaybe(res)
  if (!res.ok) throwResponseError(res, data)
  return data
}

/**
 * Download and parse a JSON response while reporting actual response bytes.
 * The backend sends Content-Length for JSON responses, so progress is
 * calculated from bytes read from the response stream rather than estimated.
 */
async function requestWithDownloadProgress(path, onProgress) {
  const res = await apiFetch(path, {
    method: 'GET',
    headers: { 'Content-Type': 'application/json' },
  })

  const total = Number.parseInt(res.headers?.get?.('Content-Length') || '0', 10) || 0
  let received = 0
  onProgress?.({ received, total })

  // Keep compatibility with environments/mocks that do not expose a stream.
  if (!res.body?.getReader) {
    const data = await readJsonMaybe(res)
    if (!res.ok) throwResponseError(res, data)
    return data
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let text = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    received += value.byteLength
    text += decoder.decode(value, { stream: true })
    onProgress?.({ received, total })
  }
  text += decoder.decode()

  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = null
  }
  if (!res.ok) throwResponseError(res, data)
  if (data === null && text) {
    throw new Error('Invalid JSON response')
  }
  return data
}

/**
 * Ask the backend to probe an upstream model endpoint's own model list.
 *
 * The request is issued server-side (the endpoint may only be reachable from
 * there), and the upstream key travels in a header rather than the query
 * string so it cannot end up in a request log.
 *
 * @param {string} apiBase  Upstream base URL, usually ending with "/v1"
 * @param {string} apiKey   Upstream key (optional; {{ENV}} placeholders are
 *                          resolved by the backend, like at inference time)
 * @returns {Promise<{models_url: string, count: number,
 *                    models: Array<{model_name: string, max_context: number,
 *                                    max_context_source: string,
 *                                    input_modalities: string[],
 *                                    output_modalities: string[],
 *                                    status: string, owned_by: string}>}>}
 */
export async function probeModels(apiBase, apiKey = '') {
  const res = await apiFetch('/v1/models/probe?api_base=' + encodeURIComponent(apiBase), {
    method: 'GET',
    headers: {
      'Content-Type': 'application/json',
      ...(apiKey ? { 'X-Probe-Api-Key': apiKey } : {}),
    },
  })
  const data = await readJsonMaybe(res)
  if (!res.ok) throwResponseError(res, data)
  return data
}

/** Model CRUD helpers. */
export const models = {
  list:   (from_disk = false)    => request('GET',    '/v1/models' + (from_disk ? '?from_disk=true' : '')),
  create: (config)            => request('POST',   '/v1/models', config),
  update: (modelId, config)   => request('PUT',    `/v1/models/${modelId}`, config),
  delete: (modelId)           => request('DELETE', `/v1/models/${modelId}`),
  probe:  (apiBase, apiKey)   => probeModels(apiBase, apiKey),
}

/**
 * JSON -> base64url header value for the transport-layer request context.
 *
 * Padding is kept on purpose: the Python side decodes this header with
 * ``base64.urlsafe_b64decode``, which rejects an unpadded value with
 * "Incorrect padding" (verified against runtime/common.py
 * ``decode_forwarded_context``).
 */
function base64UrlEncodeJson(value) {
  const bytes = new TextEncoder().encode(JSON.stringify(value))
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_')
}

/** Tool CRUD helpers. */
export const tools = {
  list:        (from_disk = false)    => request('GET',    '/v1/tools' + (from_disk ? '?from_disk=true' : '')),
  create:      (config)            => request('POST',   '/v1/tools', config),
  createMcp:   (config)            => request('POST',   '/v1/tools/mcp', config),
  createSkill: (skillDir)          => request('POST',   '/v1/tools/skill', { skill_dir: skillDir }),
  test:        (config)            => request('POST',   '/v1/tools/test', config),
  update:      (toolId, config)    => request('PUT',    `/v1/tools/${toolId}`, config),
  delete:      (toolId)            => request('DELETE', `/v1/tools/${toolId}`),
  batchDelete: (toolIds)           => request('DELETE', '/v1/tools/batch', { tool_ids: toolIds }),
  /**
   * Direct tool call test (POST /v1/tools/call).
   * Unlike the other helpers: on success the backend returns the raw result
   * text (default format=text/plain), not JSON, so this cannot reuse
   * request() (which forces JSON parsing).
   * @param {string} toolId  tool id
   * @param {object} args    argument object
   * @param {{base64Auto?: boolean}} [options]  when base64Auto, send the
   *   X-Agents-Request-Context header ({"base64":"auto"} base64url-encoded)
   *   so the server applies the same base64 marshalling as model tool calls:
   *   path-like base64 arguments are read & encoded, long base64 results are
   *   saved to a server-local file and replaced by its path.
   * @returns {Promise<string>} raw tool result text
   */
  async call(toolId, args, { base64Auto = false } = {}) {
    const headers = { 'Content-Type': 'application/json' }
    if (base64Auto) {
      headers['X-Agents-Request-Context'] = base64UrlEncodeJson({ base64: 'auto' })
    }
    const res = await apiFetch('/v1/tools/call', {
      method: 'POST',
      headers,
      body: JSON.stringify({ tool_id: toolId, arguments: args ?? {} }),
    })
    if (res.ok) return res.text()
    throwResponseError(res, await readJsonMaybe(res))
  },
}

/** MCP server helpers. */
export const mcpServers = {
  list:    (from_disk = false)            => request('GET',    '/v1/mcp-servers' + (from_disk ? '?from_disk=true' : '')),
  delete:  (serverName)                => request('DELETE', `/v1/mcp-servers/${encodeURIComponent(serverName)}`),
  restore: (serverName, config)        => request('PUT',    `/v1/mcp-servers/${encodeURIComponent(serverName)}`, config),
}

/** Prompt template CRUD helpers. */
export const promptTemplates = {
  list:   (from_disk = false)           => request('GET',    '/v1/prompt-templates' + (from_disk ? '?from_disk=true' : '')),
  create: (data)                    => request('POST',   '/v1/prompt-templates', data),
  update: (templateId, data)        => request('PUT',    `/v1/prompt-templates/${encodeURIComponent(templateId)}`, data),
  delete: (templateId)              => request('DELETE', `/v1/prompt-templates/${encodeURIComponent(templateId)}`),
}

/**
 * Stream inference via SSE (Server-Sent Events) using fetch + ReadableStream.
 *
 * @param {object}   body       Request body for POST /v1/infer/stream
 * @param {function} onMessage  Called with each parsed SSE JSON message
 * @param {function} onDone     Called when the stream ends ([DONE])
 * @param {function} onError    Called on fetch or parse errors
 * @param {object}   [options]  `{ onHandoff }`: `onHandoff(session_id, afterSeq)`
 *        takes over this session's frames on the tab's shared frame channel and
 *        returns the (optional) teardown. The POST connection is only released
 *        once the backend actually watches the session, so the page keeps one
 *        observable subscription instead of api.js creating an untracked one.
 * @returns {function}          Call to abort the stream
 */
export function inferStream(body, onMessage, onDone, onError, onInit = null, onSequence = null, options = {}) {
  const onHandoff = options.onHandoff ?? null
  // Called when the tab's frame channel could not take the session over, so this
  // POST keeps streaming it itself: the page must treat the session as directly
  // consumed again, otherwise a later re-subscription would apply the same
  // frames twice.
  const onHandoffFallback = options.onHandoffFallback ?? null
  const controller = new AbortController()
  // Set once the session has been handed over to the tab's shared frame
  // channel: the POST connection is then released on purpose, so its
  // AbortError must not be reported as a finished/failed inference.
  let handedOff = false
  let stopChannel = null

  apiFetch('/v1/infer/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: controller.signal,
  })
    .then((res) => {
      if (!res.ok) {
        return res.json().then((d) => {
          throw new Error(d.error || `Stream request failed: ${res.status}`)
        })
      }
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let currentEvent = 'message'
      let currentId = null
      // Lines of the chunk that carried the `init` event and came after it. They
      // are normally replayed by the frame channel (its cursor starts at the
      // sequence the init reported), but if the take-over fails they must be fed
      // through this stream instead of being dropped.
      let deferredLines = []

      /** Consumes one SSE line. Returns 'done', 'handed-off' or null. */
      function consumeLine(line) {
        const trimmed = line.trim()
        if (trimmed.startsWith('event: ')) {
          currentEvent = trimmed.slice(7).trim()
          return null
        }
        if (trimmed.startsWith('id:')) {
          const parsed = Number.parseInt(trimmed.slice(3).trim(), 10)
          currentId = Number.isFinite(parsed) ? parsed : null
          return null
        }
        if (trimmed === '') {
          currentEvent = 'message'  // Reset to default event type after blank line
          currentId = null
          return null
        }
        if (!trimmed.startsWith('data: ')) return null

        const payload = trimmed.slice(6).trim()
        if (payload === '[DONE]') {
          onDone()
          return 'done'
        }
        try {
          const data = JSON.parse(payload)
          if (currentEvent === 'init' && onInit) {
            onInit(data)
          } else if (currentEvent !== 'init') {
            onMessage(data, currentEvent)
            if (currentId !== null) onSequence?.(currentId)
            if (shouldLogStreamFrame(currentId, data)) {
              streamDebug('direct_frame', { seq: currentId, role: data?.role, name: data?.name, streaming: data?.streaming })
            }
          }
          if (currentEvent === 'init' && data?.session_id) {
            // Connection budget: the POST stream is only the initiator
            // handshake. Move this session's frames onto the tab's shared frame
            // channel and give the POST connection back, so N concurrent
            // inferences no longer cost N connections. `stream_seq` is the
            // broker sequence of the last frame the POST already published,
            // i.e. the replay baseline.
            handedOff = true
            const afterSeq = Number.isFinite(data.stream_seq) ? data.stream_seq : -1
            if (onHandoff) {
              // The page subscribes (and tracks) the session itself.
              onHandoff(data.session_id, afterSeq)
            } else {
              stopChannel = framesHandoff(data.session_id, afterSeq, {
                onMessage,
                onDone,
                onApplied: (acknowledge) => acknowledge(),
              })
            }
            if (Number.isFinite(data.stream_seq)) onSequence?.(data.stream_seq)
            // Release the POST connection only once the channel confirms it
            // watches this session: outside flight mode the backend cancels a
            // generation as soon as its starter is gone while nobody observes
            // it, so aborting before the subscription landed showed up as
            // "Error: user interrupted." for inferences the user never aborted.
            framesWatchingReady(data.session_id)
              .catch(() => false)
              .then((watched) => {
                if (watched) {
                  if (!controller.signal.aborted) controller.abort()
                  return
                }
                // No take-over (hub unavailable, command refused or too slow):
                // keep this POST as the session's data plane rather than
                // dropping the only observer mid-generation.
                streamDebug('frames_handoff_fallback', { sessionId: data.session_id })
                if (stopChannel) {
                  stopChannel()
                  stopChannel = null
                } else {
                  unwatchSessionStream(data.session_id)
                  onHandoffFallback?.(data.session_id, afterSeq)
                }
                handedOff = false
                const pending = deferredLines
                deferredLines = []
                for (const pendingLine of pending) {
                  if (consumeLine(pendingLine) === 'done') return
                }
                pump()
              })
            return 'handed-off'
          }
        } catch {
          // skip malformed JSON chunks
        }
        return null
      }

      function pump() {
        reader.read().then(({ done, value }) => {
          if (done) {
            if (!handedOff) onError(new Error('Inference stream ended before [DONE]'))
            return
          }
          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          // Keep the last (possibly incomplete) line in the buffer
          buffer = lines.pop() || ''
          for (let index = 0; index < lines.length; index++) {
            const outcome = consumeLine(lines[index])
            if (outcome === 'done') return
            if (outcome === 'handed-off') {
              // Reading stops here on purpose: the channel owns the session from
              // now on. The rest of this chunk is remembered so a failed
              // take-over can still deliver them itself.
              deferredLines = lines.slice(index + 1)
              return
            }
          }
          pump()
        }).catch((err) => {
          // AbortError is expected when user cancels — treat as clean done.
          // It is also how the deliberate post-init handoff ends this stream.
          if (err.name === 'AbortError') {
            if (!handedOff) onDone()
          } else {
            onError(err)
          }
        })
      }

      pump()
    })
    .catch((err) => {
      if (err.name === 'AbortError') {
        if (!handedOff) onDone()
      } else {
        onError(err)
      }
    })

  return () => {
    controller.abort()
    stopChannel?.()
  }
}

/**
 * Control an active stream inference by session_id.
 * Sends POST /v1/infer/abort with an explicit action so the backend can react
 * even while a delegate sub-agent is running (no SSE writes happening).
 *
 * @param {string} sessionId
 * @param {boolean|string} [action='stop']  'stop': cancel at the next check
 *        point (writes an interrupted assistant turn). 'force': additionally
 *        kills running tool processes (exec_shell, MCP) and forces the session
 *        status to done. 'pause': soft-pause — the inference keeps the current
 *        round intact and stops at the next round boundary (after all tool
 *        calls of the current round completed), leaving the conversation
 *        resumable. Legacy booleans are still accepted (true => 'force').
 * @returns {Promise<void>}
 */
export async function abortInferStream(sessionId, action = 'stop') {
  const act = action === true ? 'force' : action === false || !action ? 'stop' : action
  try {
    const res = await apiFetch('/v1/infer/abort', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // `forced` is kept for backward compatibility with older servers.
      body: JSON.stringify({ session_id: sessionId, action: act, forced: act === 'force' }),
    })
    // Return the parsed control response ({ ok, action, effective }) so the
    // caller can reflect "effective: false" (no active generation to pause).
    try { return await res.json() } catch { return null }
  } catch {
    // best-effort — ignore network errors
    return null
  }
}

/**
 * Soft-pause a running inference (see abortInferStream action 'pause').
 * The generation stops at its next round boundary and the session status
 * becomes "paused"; resume later with a normal continuation request
 * (resumeInferStream) or a plain new message.
 */
export async function pauseInferStream(sessionId) {
  await abortInferStream(sessionId, 'pause')
}

/**
 * Queue a user message into the currently running inference generation
 * (POST /v1/infer/interject). The backend appends it at the next round
 * boundary (after all tool calls of the current round completed) and the
 * SAME generation continues — a live interjection, not a new request.
 *
 * @returns {Promise<{ok:boolean,status:'queued'|'not_active'}|null>}
 *          null on network error; callers should fall back to a normal send.
 */
export async function interjectInfer(sessionId, content, timestamp) {
  // The client timestamp doubles as the bubble's identity: ChatPage renders
  // the local bubble immediately, and both the enqueue publish and the
  // boundary drain re-publish the same timestamp, which the session-stream
  // user-frame dedup (by timestamp) collapses into one message.
  const message = { role: 'user', content }
  if (timestamp) message.timestamp = timestamp
  try {
    const res = await apiFetch('/v1/infer/interject', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, message }),
    })
    try { return await res.json() } catch { return null }
  } catch {
    return null
  }
}

/** 环境变量 API */
export const env = {
  list:   (from_disk = false) => request('GET',    '/v1/env' + (from_disk ? '?from_disk=true' : '')),
  set:    (key, value)    => request('POST',   '/v1/env', { key, value }),
  delete: (key)           => request('DELETE', `/v1/env/${encodeURIComponent(key)}`),
  detect: ()              => request('POST',   '/v1/env/detect'),
}

/** 隧道（WS 反向隧道）子端管理 — 本环境作为子端注册到母环境。 */
export const tunnel = {
  parentStatus:     () => request('GET',  '/v1/tunnel/parent/status'),
  /** parent 非空时后端会校验并持久化为 SETUP_SOURCE，再用它注册。 */
  parentRegister:   (parent = '') => request('POST', '/v1/tunnel/parent/register', { parent }),
  parentUnregister: () => request('POST', '/v1/tunnel/parent/unregister'),
}

/** 会话 API */
export const sessions = {
  list:          (page = 1, pageSize = 100, category = '') => request('GET', `/v1/sessions?page=${encodeURIComponent(page)}&page_size=${encodeURIComponent(pageSize)}${category ? `&category=${encodeURIComponent(category)}` : ''}`),
  tree:          () => request('GET', '/v1/sessions/tree'),
  search:        (query, page = 1, pageSize = 100) => request('GET', `/v1/sessions/search?q=${encodeURIComponent(query)}&page=${encodeURIComponent(page)}&page_size=${encodeURIComponent(pageSize)}`),
  get:           (sessionId, onProgress = null) => onProgress
    ? requestWithDownloadProgress(`/v1/sessions/${encodeURIComponent(sessionId)}`, onProgress)
    : request('GET', `/v1/sessions/${encodeURIComponent(sessionId)}`),
  logDir:        (sessionId)     => request('GET',    `/v1/sessions/${encodeURIComponent(sessionId)}/log-dir`),
  // meta only — no conversation. Lets the browser resolve a *background*
  // session's bindings (remote_env / workspace) without downloading its chat.
  meta:          (sessionId)     => request('GET',    `/v1/sessions/${encodeURIComponent(sessionId)}/meta`),
  executionAnalysis: (sessionId) => request('GET',    `/v1/sessions/${encodeURIComponent(sessionId)}/execution-analysis`),
  delete:        (sessionId)     => request('DELETE', `/v1/sessions/${encodeURIComponent(sessionId)}`),
  // title 非空 = 人工设定标题；空 = 模型生成标题
  generateTitle: (sessionId, title = '') => request('POST', `/v1/sessions/${encodeURIComponent(sessionId)}/generate-title`, { title }),
  regenerateSummary: (sessionId) => request('POST',   `/v1/sessions/${encodeURIComponent(sessionId)}/regenerate-summary`),
  revoke:        (sessionId, timestamp, { forced = false, keepFiles = false } = {}) => request('POST', `/v1/sessions/${encodeURIComponent(sessionId)}/revoke`, { session_id: sessionId, timestamp, forced, keep_files: keepFiles }),
  markRead:      (sessionId)     => request('POST',   `/v1/sessions/${encodeURIComponent(sessionId)}/read`),
  fileJournals:  (sessionId)     => request('GET',    `/v1/sessions/${encodeURIComponent(sessionId)}/file-journals`),
  fileJournalDiff: (sessionId, turnKey) => request('GET', `/v1/sessions/${encodeURIComponent(sessionId)}/file-journals/${encodeURIComponent(turnKey)}`),
  setFlightMode: (sessionId, enabled) => request('POST', `/v1/sessions/${encodeURIComponent(sessionId)}/flight`, { enabled }),
}

/**
 * 会话帧共享通道（GET /v1/sessions/frames 的 SSE 单例）。
 *
 * 旧实现里每个正在流式的会话都各占一条 HTTP 长连接：发起方一条
 * POST /v1/infer/stream，旁观方一条 GET /v1/sessions/{id}/stream。浏览器对同一
 * origin 的 HTTP/1.1 并发上限通常是 6 条（WebSocket 也计入这个池），所以
 * 3-4 个并发推理 + 事件 hub + 终端 WS 就能把 6 条占满，其后的普通请求在
 * 浏览器侧排队等空闲连接，表现为"后端卡住"。
 *
 * 现在每个 tab 只维护一条帧通道，承载该 tab 正在观看的**所有**会话的帧；
 * 增减订阅通过短命令完成（POST /v1/sessions/frames/subscribe|unsubscribe）。
 * 后端按 conn id 建通道：没有则创建，有则复用；断线重连后按各会话自己的
 * lastSeq 重放缺失帧，语义与旧的每会话独立流一致。
 *
 * 连接数因此与并发会话数无关：1 条事件 hub + 1 条帧通道。
 */
const FRAMES_WATCHDOG_MS = 45_000
const FRAMES_RECONNECT_MS = 1_000
const FRAMES_CONN_KEY = 'agents_frames_conn'
// Upper bound for waiting until the channel really watches a session before the
// POST that started the inference releases itself. The backend tolerates a
// momentary loss of every observer (SESSION_UNOBSERVED_GRACE_S), so a slow
// subscribe command is survivable; this bound only stops a broken channel from
// pinning the POST connection forever.
const FRAMES_HANDOFF_WAIT_MS = 2_000
const FRAMES_SUBSCRIBE_RETRY_MS = 400

/** Per-tab channel id: stable across reloads of the same tab. */
function framesConnId() {
  let id = ''
  try { id = sessionStorage.getItem(FRAMES_CONN_KEY) || '' } catch { /* private mode */ }
  if (!id) {
    id = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`
    try { sessionStorage.setItem(FRAMES_CONN_KEY, id) } catch { /* private mode */ }
  }
  return id
}

const sessionFramesHub = {
  conn: '',
  entries: new Map(),   // sessionId -> subscription entry
  controller: null,
  reconnectTimer: null,
  watchdogTimer: null,
  generation: 0,
  connecting: false,
  connected: false,
  lastError: null,
}

function framesClearWatchdog() {
  if (sessionFramesHub.watchdogTimer) clearTimeout(sessionFramesHub.watchdogTimer)
  sessionFramesHub.watchdogTimer = null
}

function framesArmWatchdog(generation) {
  framesClearWatchdog()
  sessionFramesHub.watchdogTimer = setTimeout(() => {
    if (generation !== sessionFramesHub.generation) return
    streamDebug('frames_watchdog', { generation })
    sessionFramesHub.controller?.abort()
  }, FRAMES_WATCHDOG_MS)
}

function framesCommand(path, payload) {
  return apiFetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
    .then(async (res) => {
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        streamDebug('frames_cmd_error', { path, status: res.status })
        return { ok: false, status: res.status }
      }
      return { ok: true, ...data }
    })
    .catch((err) => {
      if (err?.name !== 'AbortError') streamDebug('frames_cmd_error', { path, error: String(err) })
      return { ok: false, error: String(err) }
    })
}

/** Ask the backend to (re-)register this session on the tab's channel. */
function framesSubscribeCommand(entry) {
  const hub = sessionFramesHub
  if (!hub.conn) return (entry.subscribePending = Promise.resolve(false))
  // Idempotent per channel generation: the initial subscribe of a session and a
  // connect/reconnect sweep both want the same registration, and a duplicate
  // command would make the backend replay the same gap twice.
  if (entry.registeredGeneration === hub.generation && entry.subscribePending) {
    return entry.subscribePending
  }
  entry.registeredGeneration = hub.generation
  const send = () => framesCommand('/v1/sessions/frames/subscribe', {
    conn: hub.conn,
    session_id: entry.sessionId,
    after: entry.lastSeq,
  }).then((result) => result.ok === true && result.registered !== false)
  return (entry.subscribePending = send().then((registered) => {
    if (registered) return true
    // The command did not land (proxy hiccup, 5xx, dropped connection): the
    // backend still has no observer for this session, so retry once on the same
    // channel before the caller decides whether it may release its own stream.
    if (entry.registeredGeneration !== hub.generation || !hub.conn) return false
    return new Promise((resolve) => setTimeout(resolve, FRAMES_SUBSCRIBE_RETRY_MS))
      .then(() => {
        if (entry.registeredGeneration !== hub.generation || !hub.conn) return false
        entry.registeredGeneration = -1
        return (entry.subscribePending = send())
      })
  }))
}

// Callers that must not release their own connection before the channel is
// watching wait on these; resolved by whichever connect attempt finishes first.
const framesReadyWaiters = []

function framesResolveReady(value) {
  for (const resolve of framesReadyWaiters.splice(0, framesReadyWaiters.length)) resolve(value)
}

function framesAwaitConnection() {
  if (sessionFramesHub.connected) return Promise.resolve(true)
  return new Promise((resolve) => framesReadyWaiters.push(resolve))
}

/**
 * Does this tab's shared channel currently watch this session? Pages use it to
 * tell an intentional unsubscribe from a lost subscription (a torn-down channel,
 * an unexpected per-session "done") and re-arm the latter.
 */
export function isSessionStreamWatched(sessionId) {
  return sessionFramesHub.entries.has(String(sessionId))
}

/**
 * Promise that settles true once the backend confirmed it watches this session on
 * this tab's channel, false if that did not happen within FRAMES_HANDOFF_WAIT_MS.
 * `subscribeSessionStream` records the pending command per entry, so this never
 * triggers a second subscribe.
 */
function framesWatchingReady(sessionId) {
  const entry = sessionFramesHub.entries.get(String(sessionId))
  if (!entry) return Promise.resolve(false)
  const pending = entry.subscribePending
  if (!pending) return Promise.resolve(false)
  return Promise.race([
    Promise.resolve(pending).then((registered) => registered === true),
    new Promise((resolve) => setTimeout(() => resolve(false), FRAMES_HANDOFF_WAIT_MS)),
  ])
}

/**
 * Advance the resume cursor. Frames are applied in batches by the UI, so the
 * cursor only moves once the batcher has acknowledged them; a reconnect that
 * happens earlier is deferred until then.
 */
function framesAcknowledge(entry, seq) {
  entry.lastSeq = Math.max(entry.lastSeq, seq)
  entry.pendingApplications = Math.max(0, entry.pendingApplications - 1)
  if (entry.pendingApplications === 0 && entry.reconnectAfterApplications) {
    entry.reconnectAfterApplications = false
    if (sessionFramesHub.connected) framesSubscribeCommand(entry)
  }
}

function framesApply(entry, seq, eventName, frame) {
  entry.onMessage?.(frame, seq, eventName)
  if (seq == null) return
  if (entry.onApplied) {
    entry.pendingApplications += 1
    let acknowledged = false
    entry.onApplied(() => {
      if (acknowledged) return
      acknowledged = true
      framesAcknowledge(entry, seq)
    })
  } else {
    entry.lastSeq = Math.max(entry.lastSeq, seq)
  }
}

/** The per-session stream ended ([DONE] equivalent for this session only). */
function framesFinish(entry) {
  const hub = sessionFramesHub
  if (hub.entries.get(entry.sessionId) === entry) {
    hub.entries.delete(entry.sessionId)
  }
  try {
    entry.onDone?.()
  } catch (err) {
    console.warn('[sessionFramesHub] onDone error', err)
  }
  if (hub.entries.size === 0) framesTeardown()
}

function framesDispatch(currentEvent, payload) {
  const hub = sessionFramesHub
  if (currentEvent === 'chan_init') {
    streamDebug('frames_chan_init', { conn: hub.conn, generation: payload?.generation })
    return
  }
  const entry = hub.entries.get(payload?.session_id)
  if (!entry) return   // unsubscribed while the frame was in flight
  if (currentEvent === 'sub_init') {
    if (!entry.baselineInitialized) {
      // First registration: conversation.json is the local baseline, so the
      // resume cursor starts at the last persisted sequence.
      const persistedSeq = Number(payload?.persisted_seq)
      if (Number.isFinite(persistedSeq)) entry.lastSeq = Math.max(entry.lastSeq, persistedSeq)
      entry.baselineInitialized = true
    }
    streamDebug('frames_sub_init', { sessionId: entry.sessionId, lastSeq: entry.lastSeq, ...payload })
    entry.onInit?.(payload)
    return
  }
  if (currentEvent === 'done') {
    streamDebug('frames_done', { sessionId: entry.sessionId, lastSeq: entry.lastSeq })
    framesFinish(entry)
    return
  }
  if (currentEvent === 'frame') {
    const seq = Number.isFinite(payload?.seq) ? payload.seq : null
    framesApply(entry, seq, payload?.event || 'message', payload?.frame)
    if (shouldLogStreamFrame(seq, payload?.frame)) {
      streamDebug('frames_frame', {
        sessionId: entry.sessionId, seq, lastSeq: entry.lastSeq,
        role: payload?.frame?.role, name: payload?.frame?.name, streaming: payload?.frame?.streaming,
      })
    }
  }
}

function framesDispatchError(err) {
  if (err?.name === 'AbortError') return
  streamDebug('frames_error', { error: String(err) })
  for (const entry of sessionFramesHub.entries.values()) {
    try {
      entry.onError?.(err)
    } catch {
      // a listener throwing must not break hub delivery
    }
  }
}

function framesScheduleReconnect() {
  const hub = sessionFramesHub
  if (hub.reconnectTimer) return
  hub.reconnectTimer = setTimeout(() => {
    hub.reconnectTimer = null
    hub.connected = false
    if (hub.entries.size > 0) framesConnect()
  }, FRAMES_RECONNECT_MS)
}

function framesTeardown() {
  const hub = sessionFramesHub
  hub.generation += 1
  framesClearWatchdog()
  framesResolveReady(false)
  if (hub.reconnectTimer) {
    clearTimeout(hub.reconnectTimer)
    hub.reconnectTimer = null
  }
  if (hub.conn) framesCommand('/v1/sessions/frames/close', { conn: hub.conn })
  hub.controller?.abort()
  hub.controller = null
  hub.connected = false
  hub.connecting = false
  hub.conn = ''
  hub.lastError = null
}

function framesConnect() {
  const hub = sessionFramesHub
  if (hub.connecting || hub.connected) return
  if (hub.entries.size === 0) return
  hub.connecting = true
  const generation = ++hub.generation
  hub.conn = framesConnId()
  hub.controller = new AbortController()
  framesArmWatchdog(generation)
  streamDebug('frames_connect', { conn: hub.conn, generation, sessions: hub.entries.size })
  apiFetch(`/v1/sessions/frames?conn=${encodeURIComponent(hub.conn)}`, { signal: hub.controller.signal })
    .then((res) => {
      if (generation !== hub.generation) return
      if (!res.ok) throw new Error(`Session frames request failed: ${res.status}`)
      hub.connected = true
      hub.connecting = false
      hub.lastError = null
      // The channel now exists (created or re-attached). Re-register every
      // watched session: the backend replays each session's gap frames from its
      // own cursor, so a reconnect loses nothing.
      for (const entry of hub.entries.values()) framesSubscribeCommand(entry)
      framesResolveReady(true)

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let currentEvent = 'message'

      const pump = () => reader.read().then(({ done, value }) => {
        if (generation !== hub.generation) return
        if (done) {
          framesClearWatchdog()
          streamDebug('frames_eof', { generation })
          hub.connected = false
          hub.connecting = false
          hub.lastError = new Error('Session frames stream closed')
          framesDispatchError(hub.lastError)
          if (hub.entries.size > 0) framesScheduleReconnect()
          return
        }
        // Any bytes (including SSE comment heartbeats) prove the connection is
        // still delivering data.
        framesArmWatchdog(generation)
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''
        for (const line of lines) {
          const trimmed = line.trim()
          if (trimmed.startsWith('event:')) {
            currentEvent = trimmed.slice(6).trim()
          } else if (trimmed.startsWith('data:')) {
            try {
              framesDispatch(currentEvent, JSON.parse(trimmed.slice(5).trim()))
            } catch {
              // skip malformed JSON
            }
          } else if (!trimmed) {
            currentEvent = 'message'
          }
        }
        return pump()
      })

      return pump()
    })
    .catch((err) => {
      if (generation !== hub.generation) return
      framesClearWatchdog()
      hub.connected = false
      hub.connecting = false
      // Nobody may wait on a connection that is never going to happen: release
      // the hand-off waiters so the POST initiator can decide to keep carrying
      // the session itself instead of sitting on its own timeout.
      framesResolveReady(false)
      if (err?.name !== 'AbortError') {
        hub.lastError = err
        framesDispatchError(err)
      }
      if (hub.entries.size > 0) framesScheduleReconnect()
    })
}

/**
 * Subscribe to the retained/live inference frames of one session.
 *
 * Every watched session in this tab shares the single frame channel above;
 * this function only registers (or replaces) that session's entry, so opening
 * a fifth concurrent inference no longer costs a fifth connection.
 *
 * Unexpected EOF and network/read errors are retried automatically by the hub.
 * Every frame has a monotonically increasing per-session sequence; reconnects
 * pass the last applied sequence back as `after`, so the backend replays only
 * the missing frames and then resumes live delivery. `onApplied`, when
 * supplied, receives acknowledgement callbacks so a batching UI can advance
 * that cursor only after applying the corresponding frame.
 *
 * @returns {function} Call to stop watching this session (and to close the
 *                     shared channel when it was the last one).
 */
export function subscribeSessionStream(sessionId, onMessage, onDone, onError, onInit = null, after = -1, onApplied = null) {
  const hub = sessionFramesHub
  const entry = {
    sessionId,
    onMessage, onDone, onError, onInit, onApplied,
    lastSeq: Number.isFinite(Number(after)) ? Number(after) : -1,
    baselineInitialized: Number.isFinite(Number(after)) && Number(after) >= 0,
    pendingApplications: 0,
    reconnectAfterApplications: false,
  }
  // Replaces any previous subscription for the same session (e.g. the POST
  // stream handing the session over to the channel, or a re-subscribe after a
  // retired inference generation).
  hub.entries.set(sessionId, entry)
  if (hub.connected) {
    entry.subscribePending = framesSubscribeCommand(entry)
  } else {
    // The channel does not exist yet: connect first, then register. Waiting on
    // this promise is how the POST initiator knows the hand-off completed.
    entry.subscribePending = framesAwaitConnection()
      .then((connected) => (connected ? framesSubscribeCommand(entry) : false))
    framesConnect()
  }

  return () => framesUnwatch(sessionId, entry)
}

/**
 * Stop watching one session on this tab's channel. `onlyIfEntry` makes the call a
 * no-op when the subscription was already replaced by a newer one (e.g. the POST
 * stream fell back to carrying the session itself and a page-level subscription
 * took its place meanwhile).
 */
function framesUnwatch(sessionId, onlyIfEntry = null) {
  const hub = sessionFramesHub
  const key = String(sessionId)
  const entry = hub.entries.get(key)
  if (!entry || (onlyIfEntry && entry !== onlyIfEntry)) return false
  hub.entries.delete(key)
  if (hub.conn) framesCommand('/v1/sessions/frames/unsubscribe', { conn: hub.conn, session_id: key })
  if (hub.entries.size === 0) framesTeardown()
  return true
}

/** Drop a page-owned watch (used when the POST keeps carrying the session). */
export function unwatchSessionStream(sessionId) {
  return framesUnwatch(sessionId)
}

/**
 * Route one inference's frames through the shared channel and stop holding a
 * dedicated POST connection for it. Called by inferStream() right after the
 * POST's `init` event, which carries the session id and the sequence baseline.
 */
function framesHandoff(sessionId, afterSeq, { onMessage, onDone, onError, onApplied }) {
  return subscribeSessionStream(
    sessionId,
    (frame, seq, eventName) => onMessage?.(frame, eventName, seq),
    () => onDone?.(),
    onError ?? null,
    null,
    afterSeq,
    onApplied ?? null,
  )
}

/**
 * 会话事件共享 Hub（GET /v1/sessions/events 的 SSE 单例）。
 *
 * 同一浏览器对同一 origin 的 HTTP/1.1 并发连接上限是 6 条。旧实现里
 * Sidebar / ChatPage / AuthSettingsPage 等每个组件各自开一条永不释放的
 * SSE 长连接：3 个以上 tab 快速打开时 6 条连接瞬间占满，其余所有
 * 普通请求（文件管理器、会话列表……）在浏览器侧排队等空闲连接而
 * "卡死"，关掉任意一个 tab 释放一条连接后立即恢复。
 *
 * 修复：每个 tab 全局只维护这一条 SSE 连接；组件调用
 * subscribeSessionEvents() 只是在 hub 上注册监听器，全部取消订阅后
 * 连接才关闭。晚到的监听者会先收到最近一次的 init 快照 + 缓冲的事件，
 * 语义与旧实现（每个订阅者都有独立 init）保持一致。
 */
const sessionEventsHub = {
  listeners: new Map(),
  controller: null,
  reconnectTimer: null,
  watchdogTimer: null,
  generation: 0,
  connecting: false,
  lastInit: null,
  pending: new Map(),   // 等待 init 的迟到监听者：sid -> [event, ...]
  buffer: [],           // init 之后到达的近期事件（有界），供迟到监听者补发
  bufferInitSeq: -1,    // buffer 所基于的 init 的 generation
  lastError: null,
}
const HUB_WATCHDOG_MS = 45_000
const HUB_BUFFER_MAX = 1000

function hubClearWatchdog() {
  if (sessionEventsHub.watchdogTimer) clearTimeout(sessionEventsHub.watchdogTimer)
  sessionEventsHub.watchdogTimer = null
}

function hubArmWatchdog(generation) {
  hubClearWatchdog()
  sessionEventsHub.watchdogTimer = setTimeout(() => {
    if (generation !== sessionEventsHub.generation) return
    streamDebug('events_watchdog', { generation })
    sessionEventsHub.controller?.abort()
  }, HUB_WATCHDOG_MS)
}

/** 把事件分发给全体监听器；init 之前到达的事件为等待者缓存。 */
function hubDispatch(event) {
  if (event?.event === 'init') {
    sessionEventsHub.lastInit = event
    sessionEventsHub.buffer = []
    sessionEventsHub.bufferInitSeq = sessionEventsHub.generation
  } else {
    sessionEventsHub.buffer.push(event)
    if (sessionEventsHub.buffer.length > HUB_BUFFER_MAX) sessionEventsHub.buffer.shift()
  }
  for (const entry of sessionEventsHub.listeners.values()) {
    try {
      entry.onEvent(event)
    } catch (err) {
      console.warn('[sessionEventsHub] listener error', err)
    }
  }
  if (event?.event === 'init') {
    // init 之后，等待中的迟到监听者进入常规分发：只补发 init 前缓存
    // 的那部分事件（此时恰是 pending 队列中的内容），init 本身由上面的
    // 主循环正常分发，避免重复。
    for (const [sid, queue] of sessionEventsHub.pending) {
      sessionEventsHub.pending.delete(sid)
      const entry = sessionEventsHub.listeners.get(sid)
      if (!entry) continue
      for (const ev of queue) {
        try { entry.onEvent(ev) } catch (err) { console.warn('[sessionEventsHub] listener error', err) }
      }
    }
  } else {
    for (const queue of sessionEventsHub.pending.values()) {
      queue.push(event)
    }
  }
}

function hubDispatchError(err) {
  if (err?.name === 'AbortError') return
  streamDebug('events_error', { error: String(err) })
  for (const entry of sessionEventsHub.listeners.values()) {
    const { onError } = entry
    try {
      onError?.(err)
    } catch {
      // a listener throwing must not break hub delivery
    }
  }
}

function hubDeliverLate(sid, entry) {
  const queue = sessionEventsHub.pending.get(sid) || []
  sessionEventsHub.pending.delete(sid)
  const { onEvent, onError } = entry
  const lastError = sessionEventsHub.lastError
  // 顺序与"当时立即订阅"一致：init（若已到达）→ init 后已缓冲的事件 → 错误
  if (sessionEventsHub.lastInit && sessionEventsHub.bufferInitSeq === sessionEventsHub.generation) {
    const replay = (ev) => {
      try { onEvent(ev) } catch (err) { console.warn('[sessionEventsHub] listener error', err) }
    }
    replay(sessionEventsHub.lastInit)
    for (const ev of sessionEventsHub.buffer) replay(ev)
  }
  for (const ev of queue) {
    try { onEvent(ev) } catch (err) { console.warn('[sessionEventsHub] listener error', err) }
  }
  if (lastError) {
    try { onError?.(lastError) } catch { /* ignore */ }
  }
}

function hubConnect() {
  const hub = sessionEventsHub
  if (hub.connecting) return
  if (hub.listeners.size === 0) return
  hub.connecting = true
  const generation = ++hub.generation
  hub.controller = new AbortController()
  streamDebug('events_connect', { generation })
  hubArmWatchdog(generation)
  apiFetch('/v1/sessions/events', { signal: hub.controller.signal })
    .then((res) => {
      if (generation !== hub.generation) return
      if (!res.ok) throw new Error(`Session events request failed: ${res.status}`)
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      const pump = () => reader.read().then(({ done, value }) => {
        if (generation !== hub.generation) return
        if (done) {
          hubClearWatchdog()
          streamDebug('events_eof', { generation })
          hub.connecting = false
          hub.lastError = new Error('Session events stream closed')
          hubDispatchError(hub.lastError)
          if (hub.listeners.size > 0) hub.scheduleReconnect()
          return
        }
        // Includes SSE comment heartbeats. Any bytes prove that this
        // connection is still delivering data through the proxy/browser.
        hubArmWatchdog(generation)
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''
        for (const line of lines) {
          const trimmed = line.trim()
          if (trimmed.startsWith('data: ')) {
            const payload = trimmed.slice(6).trim()
            try {
              const event = JSON.parse(payload)
              streamDebug('events_event', {
                generation,
                event: event?.event,
                sessionId: event?.session_id,
                status: event?.status,
                sessions: event?.event === 'init' ? Object.keys(event?.sessions || {}).length : undefined,
              })
              hubDispatch(event)
            } catch {
              // skip malformed JSON
            }
          }
        }
        return pump()
      })

      hub.connecting = false
      return pump()
    })
    .catch((err) => {
      if (generation !== hub.generation) return
      hubClearWatchdog()
      if (err.name !== 'AbortError') {
        hub.lastError = err
        hubDispatchError(err)
      }
      if (hub.listeners.size > 0) hub.scheduleReconnect()
    })
}

sessionEventsHub.scheduleReconnect = () => {
  const hub = sessionEventsHub
  if (hub.reconnectTimer) return
  hub.reconnectTimer = setTimeout(() => {
    hub.reconnectTimer = null
    hub.connecting = false
    if (hub.listeners.size > 0) hubConnect()
  }, 1000)
}

/**
 * Subscribe to session status events via SSE (GET /v1/sessions/events).
 *
 * All subscribers within this tab share a single SSE connection (see the
 * sessionEventsHub above); the returned unsubscribe removes only this
 * listener and closes the connection when it is the last one.
 *
 * @param {function} onEvent  Called with each parsed event object:
 *   - init:         { event: 'init', sessions: { <sid>: <status>, ... },
 *                     titles: { <sid>: { title, title_given }, ... } }
 *   - message:      { event: 'message', session_id: '<sid>', status: '<status>' }
 *                  (terminal done_* events also carry the canonical
 *                   title: '<title>' and title_given: <boolean>)
 *   - title_update: { event: 'title_update', session_id: '<sid>', title: '<title>' }
 * @param {function} onError  Called on fetch or stream errors (except AbortError).
 * @returns {function}        Call to remove this listener (and close the
 *                            shared connection when it was the last one).
 */
export function subscribeSessionEvents(onEvent, onError) {
  const hub = sessionEventsHub
  if (hub.listeners.size === 0 && !hub.reconnectTimer) {
    // Fresh hub: the first listener gets the "just connected" experience —
    // lastError starts clean for it.
    hub.lastError = null
  }
  const sid = Math.random().toString(16).slice(2)
  hub.listeners.set(sid, { sid, onEvent, onError })
  if (hub.lastInit && hub.bufferInitSeq === hub.generation) {
    // Connected (or reconnecting with a fresh init): deliver immediately.
    try { onEvent(hub.lastInit) } catch (err) { console.warn('[sessionEventsHub] listener error', err) }
    for (const ev of hub.buffer) {
      try { onEvent(ev) } catch (err) { console.warn('[sessionEventsHub] listener error', err) }
    }
  } else {
    hub.pending.set(sid, [])
  }
  const lastError = hub.lastError
  if (lastError) {
    try { onError?.(lastError) } catch { /* ignore */ }
  }
  hubConnect()
  return () => {
    hub.listeners.delete(sid)
    hub.pending.delete(sid)
    if (hub.listeners.size === 0) {
      // Last listener left: tear the shared connection down.
      hub.generation += 1
      hubClearWatchdog()
      if (hub.reconnectTimer) {
        clearTimeout(hub.reconnectTimer)
        hub.reconnectTimer = null
      }
      hub.controller?.abort()
      hub.controller = null
      hub.connecting = false
      hub.lastInit = null
      hub.buffer = []
      hub.bufferInitSeq = -1
      hub.lastError = null
      hub.pending.clear()
    }
  }
}

/** Test helper: fully reset the shared hub between test cases. */
export function __resetSessionEventsHubForTests() {
  const hub = sessionEventsHub
  hub.generation += 1
  hubClearWatchdog()
  if (hub.reconnectTimer) {
    clearTimeout(hub.reconnectTimer)
    hub.reconnectTimer = null
  }
  hub.controller?.abort()
  hub.controller = null
  hub.connecting = false
  hub.listeners.clear()
  hub.lastInit = null
  hub.buffer = []
  hub.bufferInitSeq = -1
  hub.lastError = null
  hub.pending.clear()
}

/** Drop the shared frame channel without issuing a close command (tests only). */
export function __resetSessionFramesHubForTests() {
  const hub = sessionFramesHub
  hub.generation += 1
  framesClearWatchdog()
  if (hub.reconnectTimer) {
    clearTimeout(hub.reconnectTimer)
    hub.reconnectTimer = null
  }
  hub.controller?.abort()
  hub.controller = null
  hub.entries.clear()
  hub.connected = false
  hub.connecting = false
  hub.conn = ''
  hub.lastError = null
  try { sessionStorage.removeItem(FRAMES_CONN_KEY) } catch { /* private mode */ }
}


export const auth = {
  config: () => request('GET', '/v1/auth/config'),
  updateConfig: (data) => request('POST', '/v1/auth/config', data),
  disable: () => request('POST', '/v1/auth/config', { disable_auth: true }),
  login: (password) => request('POST', '/v1/auth/login', { password }),
  logout: () => request('POST', '/v1/auth/logout', {}),
}

/** \u6784\u5efa\u4fe1\u606f API */
export const build = {
  info: () => request('GET', '/v1/setup?op=hello'),
  restartBackend: () => request('GET', '/v1/setup?op=restart_backend'),
  update: (source, frontendBuild, backendBuild, lastConfig) => {
    const params = new URLSearchParams({
      op: 'update',
      source,
      frontend_build: frontendBuild,
      backend_build: backendBuild,
      last_config: lastConfig,
    })
    return request('GET', `/v1/setup?${params.toString()}`)
  },
  /** 检查更新源：由后端代理请求 source 的 op=hello，返回 {remote, local}。
   *  400 source_auth（更新源启用授权需带 token）/ 502 source_unreachable。 */
  checkSource: (source) => {
    const params = new URLSearchParams({ op: 'source-hello', source })
    return request('GET', `/v1/setup?${params.toString()}`)
  },
}

/**
 * 远程环境管理 API（母环境视角管理的子环境，记录在 DATA_DIR/remote_envs.json）。
 * 状态检查（op=hello）由后端代理发起（浏览器可能无法直连目标环境：CORS、
 * 仅服务端可路由的地址等；目标环境启用授权时，登记的 URL 需携带 ?token=...）；
 * 更新由后端把增量推送到目标环境（POST /v1/remote-envs/{id}/push-update，
 * 目标环境无需能访问母环境地址）。
 */
export const remoteEnv = {
  list: () => request('GET', '/v1/remote-envs'),
  // 后端自行向目标环境发起 op=hello 探测：成功后连同快照与 online=true
  // 持久化到 remote_envs.json；失败时不记录（400 child_auth /
  // 502 child_unreachable）。
  add: (url) => request('POST', '/v1/remote-envs', { url }),
  updateSnapshot: (id, snapshot = null, online = null) =>
    request('PUT', `/v1/remote-envs/${encodeURIComponent(id)}`, {
      ...(snapshot ? { snapshot } : {}),
      ...(typeof online === 'boolean' ? { online } : {}),
    }),
  /** 推送更新：母环境构建增量并 POST 到该环境的 /v1/setup?op=push。 */
  pushUpdate: (id) => request('POST', `/v1/remote-envs/${encodeURIComponent(id)}/push-update`, null),
  /** 刷新该环境的版本快照（隧道环境经反向隧道）。 */
  hello: (id) => request('POST', `/v1/remote-envs/${encodeURIComponent(id)}/hello`, null),
  remove: (id) => request('DELETE', `/v1/remote-envs/${encodeURIComponent(id)}`),
}


/**
 * 由登记的环境 URL 推导该环境的 Web 首页地址（id 列链接的目标）：
 * 去掉 /v1/setup 路径与查询参数，保留 scheme://netloc 与部署前缀。
 * 例如 `http://host:7988/v1/setup?token=x` -> `http://host:7988/`，
 * `https://a.b.com:8443/sub/v1/setup/` -> `https://a.b.com:8443/sub/`。
 *
 * @param {string} url 登记的环境 URL（canonical_setup_url 形态）
 * @returns {string} 可直接在新标签页打开的环境首页 URL
 * @throws {TypeError} url 不是合法 URL 时
 */
export function remoteEnvHomeUrl(url) {
  const u = new URL(String(url).trim())
  const marker = '/v1/setup'
  const idx = u.pathname.indexOf(marker)
  let base = idx === -1 ? '' : u.pathname.slice(0, idx)
  if (base && !base.endsWith('/')) base += '/'
  u.pathname = base || '/'
  u.search = ''
  u.hash = ''
  return u.toString()
}

/** 判断一个值是否为可直接用作图片的地址（与 AppLogo 组件的判断一致）。 */
export function isRemoteLogoImage(logo) {
  const value = String(logo ?? '').trim()
  if (!value) return false
  if (value.startsWith('data:')) return true
  if (value.startsWith('http') || value.startsWith('//')) return true
  if (value.startsWith('/') || value.startsWith('./') || value.startsWith('../')) return true
  return /\.(png|jpe?g|gif|svg|ico|webp|bmp)(\?.*)?$/i.test(value)
}

/**
 * 解析远程环境 logo 的展示地址：
 * data: / http(s):// / // 开头的原样使用；/ 开头或相对路径
 * 的子端路径走同源 env-proxy 桥（与文件管理器同一通道：子端 token 由
 * 母端注入，浏览器无需可达子端）：/v1/env-proxy/{env_id}{logo}。
 *
 * @param {string} [origin] 同源前缀（默认 ``location.origin``，测试可注入）
 */
export function resolveRemoteEnvLogo(envId, logo, origin = typeof location !== 'undefined' ? location.origin : '') {
  const value = String(logo ?? '').trim()
  if (!value) return ''
  if (value.startsWith('data:') || value.startsWith('http') || value.startsWith('//')) return value
  const id = String(envId ?? '').trim()
  if (!id) return value
  const path = value.startsWith('/') ? value : '/' + value
  return `${origin}/v1/env-proxy/${encodeURIComponent(id)}${path}`
}

/** AI代理 API */
export const agents = {
  list:   (from_disk = false) => request('GET',    '/v1/agents' + (from_disk ? '?from_disk=true' : '')),
  get:    (agentId)       => request('GET',    `/v1/agents/${encodeURIComponent(agentId)}`),
  create: (data)          => request('POST',   '/v1/agents', data),
  update: (agentId, data) => request('PUT',    `/v1/agents/${encodeURIComponent(agentId)}`, data),
  delete: (agentId)       => request('DELETE', `/v1/agents/${encodeURIComponent(agentId)}`),
}

function uploadChunkWithProgress(uploadId, chunk, blob, onProgress) {
  let xhr
  let rejectPromise

  const promise = new Promise((resolve, reject) => {
    rejectPromise = reject
    xhr = new XMLHttpRequest()
    xhr.open('PUT', `/v1/workspace/upload/${encodeURIComponent(uploadId)}/chunk/${chunk.parallel_id}`)
    xhr.setRequestHeader('Content-Type', 'application/octet-stream')
    xhr.setRequestHeader('X-Upload-Offset', String(chunk.offset))
    xhr.setRequestHeader('X-Upload-Size', String(chunk.size))
    xhr.setRequestHeader('X-File-Size', String(chunk.file_size))

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(event.loaded)
      }
    }

    xhr.onload = () => {
      let data = null
      try {
        data = xhr.responseText ? JSON.parse(xhr.responseText) : null
      } catch {
        data = xhr.responseText
      }

      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(data)
      } else {
        const message = data?.message || data?.error || xhr.responseText || `Request failed: ${xhr.status}`
        const err = new Error(message)
        err.status = xhr.status
        err.data = data
        reject(err)
      }
    }

    xhr.onerror = () => reject(new Error('Upload network error'))
    xhr.onabort = () => {
      const err = new DOMException('Upload aborted', 'AbortError')
      reject(err)
    }
    xhr.send(blob)
  })

  return {
    promise,
    abort: () => {
      if (xhr) xhr.abort()
      else if (rejectPromise) rejectPromise(new DOMException('Upload aborted', 'AbortError'))
    },
  }
}

/** 工作区文件管理 API */
export const workspace = {
  list:     (path, page = 1, pageSize = 50, restrict = true, { sort = 'name', nameFilter = '' } = {}) => {
    const params = new URLSearchParams({
      path,
      page: String(page),
      page_size: String(pageSize),
      restrict: restrict ? '1' : '0',
      sort,
    })
    if (nameFilter) params.set('name_filter', nameFilter)
    return request('GET', `/v1/workspace/list?${params.toString()}`)
  },
  tree:     (path) => request('GET', `/v1/workspace/tree?path=${encodeURIComponent(path)}`),
  children: (path) => request('GET', `/v1/workspace/children?path=${encodeURIComponent(path)}`),
  search:   (path, query, nameFilter = '') => {
    const params = new URLSearchParams({ path, query })
    if (nameFilter) params.set('name_filter', nameFilter)
    return request('GET', `/v1/workspace/search?${params.toString()}`)
  },
  content:  (path, restrict = true) => `/v1/workspace/content?path=${encodeURIComponent(path)}&restrict=${restrict ? 1 : 0}`,
  download: (path, restrict = true) => `/v1/workspace/download?path=${encodeURIComponent(path)}&restrict=${restrict ? 1 : 0}`,
  thumbnail:(path, restrict = true) => `/v1/workspace/thumbnail?path=${encodeURIComponent(path)}&restrict=${restrict ? 1 : 0}`,
  pasteDir: () => request('GET', '/v1/workspace/paste-dir'),
  rename:   (path, newName) => request('POST', '/v1/workspace/rename', { path, new_name: newName }),
  mkdir:    (parentPath, name) => request('POST', '/v1/workspace/mkdir', { parent_path: parentPath, name }),
  duplicate:(path) => request('POST', '/v1/workspace/duplicate', { path }),
  delete:   (path) => request('DELETE', '/v1/workspace/delete', { path }),
  move:     (paths, destDir, overwrite = false) => request('POST', '/v1/workspace/move', { paths, dest_dir: destDir, overwrite }),
  copy:     (paths, destDir, overwrite = false) => request('POST', '/v1/workspace/copy', { paths, dest_dir: destDir, overwrite }),
  uploadInit: (data) => request('POST', '/v1/workspace/upload/init', data),
  uploadChunk: (uploadId, chunk, blob, onProgress) => uploadChunkWithProgress(uploadId, chunk, blob, onProgress),
  uploadComplete: (uploadId) => request('POST', `/v1/workspace/upload/${encodeURIComponent(uploadId)}/complete`, {}),
  uploadCancel: (uploadId) => request('DELETE', `/v1/workspace/upload/${encodeURIComponent(uploadId)}`),
}
