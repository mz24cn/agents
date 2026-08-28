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

/** Model CRUD helpers. */
export const models = {
  list:   (from_disk = false)    => request('GET',    '/v1/models' + (from_disk ? '?from_disk=true' : '')),
  create: (config)            => request('POST',   '/v1/models', config),
  update: (modelId, config)   => request('PUT',    `/v1/models/${modelId}`, config),
  delete: (modelId)           => request('DELETE', `/v1/models/${modelId}`),
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
 * @returns {function}          Call to abort the stream
 */
export function inferStream(body, onMessage, onDone, onError, onInit = null, onSequence = null) {
  const controller = new AbortController()

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

      function pump() {
        reader.read().then(({ done, value }) => {
          if (done) {
            onError(new Error('Inference stream ended before [DONE]'))
            return
          }
          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          // Keep the last (possibly incomplete) line in the buffer
          buffer = lines.pop() || ''
          for (const line of lines) {
            const trimmed = line.trim()
            if (trimmed.startsWith('event: ')) {
              currentEvent = trimmed.slice(7).trim()
            } else if (trimmed.startsWith('id:')) {
              const parsed = Number.parseInt(trimmed.slice(3).trim(), 10)
              currentId = Number.isFinite(parsed) ? parsed : null
            } else if (trimmed.startsWith('data: ')) {
              const payload = trimmed.slice(6).trim()
              if (payload === '[DONE]') {
                onDone()
                return
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
              } catch {
                // skip malformed JSON chunks
              }
            } else if (trimmed === '') {
              currentEvent = 'message'  // Reset to default event type after blank line
              currentId = null
            }
          }
          pump()
        }).catch((err) => {
          // AbortError is expected when user cancels — treat as clean done
          if (err.name === 'AbortError') {
            onDone()
          } else {
            onError(err)
          }
        })
      }

      pump()
    })
    .catch((err) => {
      if (err.name === 'AbortError') {
        onDone()
      } else {
        onError(err)
      }
    })

  return () => controller.abort()
}

/**
 * Abort an active stream inference by session_id.
 * Sends POST /v1/infer/abort so the backend can set the cancel_event
 * even while a delegate sub-agent is running (no SSE writes happening).
 *
 * @param {string} sessionId
 * @param {boolean} [forced=false]  When true, kills running tool processes
 *        (exec_shell, MCP) and forces session status to done.  Use when the
 *        session is stuck in a tool call that won't respond to a normal abort.
 * @returns {Promise<void>}
 */
export async function abortInferStream(sessionId, forced = false) {
  try {
    await apiFetch('/v1/infer/abort', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, forced }),
    })
  } catch {
    // best-effort — ignore network errors
  }
}

/** 环境变量 API */
export const env = {
  list:   (from_disk = false) => request('GET',    '/v1/env' + (from_disk ? '?from_disk=true' : '')),
  set:    (key, value)    => request('POST',   '/v1/env', { key, value }),
  delete: (key)           => request('DELETE', `/v1/env/${encodeURIComponent(key)}`),
  detect: ()              => request('POST',   '/v1/env/detect'),
}

/** 会话 API */
export const sessions = {
  list:          (page = 1, pageSize = 100) => request('GET', `/v1/sessions?page=${encodeURIComponent(page)}&page_size=${encodeURIComponent(pageSize)}`),
  search:        (query, page = 1, pageSize = 100) => request('GET', `/v1/sessions/search?q=${encodeURIComponent(query)}&page=${encodeURIComponent(page)}&page_size=${encodeURIComponent(pageSize)}`),
  get:           (sessionId, onProgress = null) => onProgress
    ? requestWithDownloadProgress(`/v1/sessions/${encodeURIComponent(sessionId)}`, onProgress)
    : request('GET', `/v1/sessions/${encodeURIComponent(sessionId)}`),
  logDir:        (sessionId)     => request('GET',    `/v1/sessions/${encodeURIComponent(sessionId)}/log-dir`),
  executionAnalysis: (sessionId) => request('GET',    `/v1/sessions/${encodeURIComponent(sessionId)}/execution-analysis`),
  delete:        (sessionId)     => request('DELETE', `/v1/sessions/${encodeURIComponent(sessionId)}`),
  generateTitle: (sessionId)     => request('POST',   `/v1/sessions/${encodeURIComponent(sessionId)}/generate-title`),
  regenerateSummary: (sessionId) => request('POST',   `/v1/sessions/${encodeURIComponent(sessionId)}/regenerate-summary`),
  revoke:        (sessionId, timestamp, { forced = false, keepFiles = false } = {}) => request('POST', `/v1/sessions/${encodeURIComponent(sessionId)}/revoke`, { session_id: sessionId, timestamp, forced, keep_files: keepFiles }),
  markRead:      (sessionId)     => request('POST',   `/v1/sessions/${encodeURIComponent(sessionId)}/read`),
  fileJournals:  (sessionId)     => request('GET',    `/v1/sessions/${encodeURIComponent(sessionId)}/file-journals`),
  fileJournalDiff: (sessionId, turnKey) => request('GET', `/v1/sessions/${encodeURIComponent(sessionId)}/file-journals/${encodeURIComponent(turnKey)}`),
  setFlightMode: (sessionId, enabled) => request('POST', `/v1/sessions/${encodeURIComponent(sessionId)}/flight`, { enabled }),
}

/**
 * Subscribe to the retained/live inference stream for an existing session.
 *
 * Unexpected EOF and network/read errors are retried automatically.  Every
 * inference frame has a monotonically increasing SSE `id`; reconnects pass the
 * last successfully applied id back as `after`, so the backend replays only the
 * missing frames and then resumes the live stream. `onApplied`, when supplied,
 * receives acknowledgement callbacks so a batching UI can advance that cursor
 * only after applying the corresponding frame.
 */
export function subscribeSessionStream(sessionId, onMessage, onDone, onError, onInit = null, after = -1, onApplied = null) {
  let stopped = false
  let completed = false
  let controller = null
  let reconnectTimer = null
  let pendingApplications = 0
  let reconnectAfterApplications = false
  let lastSeq = Number.isFinite(Number(after)) ? Number(after) : -1
  let baselineInitialized = lastSeq >= 0

  const finish = () => {
    if (completed || stopped) return
    completed = true
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    onDone?.()
  }

  const scheduleReconnect = () => {
    if (stopped || completed || reconnectTimer) return
    if (pendingApplications > 0) {
      reconnectAfterApplications = true
      return
    }
    reconnectAfterApplications = false
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      connect()
    }, 1000)
  }

  const connect = () => {
    if (stopped || completed) return
    controller = new AbortController()
    streamDebug('retained_connect', { sessionId, after: lastSeq })
    const url = `/v1/sessions/${encodeURIComponent(sessionId)}/stream?after=${encodeURIComponent(lastSeq)}`
    apiFetch(url, { signal: controller.signal }).then((res) => {
      if (!res.ok) throw new Error(`Session stream request failed: ${res.status}`)
      if (!res.body?.getReader) throw new Error('Session stream response body is unavailable')

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let currentEvent = 'message'
      let currentId = null

      const processLine = (line) => {
        const trimmed = line.trim()
        if (trimmed.startsWith('event:')) {
          currentEvent = trimmed.slice(6).trim()
        } else if (trimmed.startsWith('id:')) {
          const parsed = Number.parseInt(trimmed.slice(3).trim(), 10)
          currentId = Number.isFinite(parsed) ? parsed : null
        } else if (trimmed.startsWith('data:')) {
          const payload = trimmed.slice(5).trim()
          if (payload === '[DONE]') {
            finish()
            return
          }
          try {
            const data = JSON.parse(payload)
            if (currentEvent === 'init') {
              // On the first connection, conversation.json is the local
              // baseline. Remember the matching persisted sequence before any
              // replay frames arrive, so even an immediate disconnect can
              // resume without losing frames persisted in the meantime.
              if (!baselineInitialized) {
                const persistedSeq = Number(data?.persisted_seq)
                if (Number.isFinite(persistedSeq)) lastSeq = Math.max(lastSeq, persistedSeq)
                baselineInitialized = true
              }
              streamDebug('retained_init', { sessionId, lastSeq, ...data })
              onInit?.(data)
            } else {
              // Delivery and application are separate when the UI batches
              // frames. The optional acknowledgement callback advances the
              // reconnect cursor only after the batcher has applied this frame.
              onMessage?.(data, currentId, currentEvent)
              if (currentId !== null) {
                if (onApplied) {
                  const appliedSeq = currentId
                  pendingApplications += 1
                  let acknowledged = false
                  onApplied(() => {
                    if (acknowledged) return
                    acknowledged = true
                    lastSeq = Math.max(lastSeq, appliedSeq)
                    pendingApplications = Math.max(0, pendingApplications - 1)
                    if (pendingApplications === 0 && reconnectAfterApplications) scheduleReconnect()
                  })
                } else {
                  lastSeq = Math.max(lastSeq, currentId)
                }
              }
              if (shouldLogStreamFrame(currentId, data)) {
                streamDebug('retained_frame', { sessionId, seq: currentId, lastSeq, role: data?.role, name: data?.name, streaming: data?.streaming })
              }
            }
          } catch {
            // Do not advance lastSeq for malformed/unapplied frames; a later
            // reconnect can replay them instead of silently creating a gap.
          }
        } else if (!trimmed) {
          currentEvent = 'message'
          currentId = null
        }
      }

      const pump = () => reader.read().then(({ done, value }) => {
        if (done) {
          // A valid terminal stream sends [DONE]. Plain EOF is treated as a
          // dropped connection and resumed from lastSeq.
          streamDebug('retained_eof', { sessionId, lastSeq })
          scheduleReconnect()
          return
        }
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''
        for (const line of lines) {
          processLine(line)
          if (completed || stopped) return
        }
        return pump()
      })

      return pump()
    }).catch((err) => {
      if (err.name === 'AbortError' || stopped || completed) return
      streamDebug('retained_error', { sessionId, lastSeq, error: String(err) })
      onError?.(err)
      scheduleReconnect()
    })
  }

  connect()
  return () => {
    stopped = true
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    controller?.abort()
  }
}

/**
 * Subscribe to session status events via SSE (GET /v1/sessions/events).
 *
 * @param {function} onEvent  Called with each parsed event object:
 *   - init:         { event: 'init', sessions: { <sid>: <status>, ... } }
 *   - message:      { event: 'message', session_id: '<sid>', status: '<status>' }
 *   - title_update: { event: 'title_update', session_id: '<sid>', title: '<title>' }
 * @param {function} onError  Called on fetch or stream errors (except AbortError).
 * @returns {function}        Call to close/abort the SSE connection.
 */
export function subscribeSessionEvents(onEvent, onError) {
  let stopped = false
  let controller = null
  let reconnectTimer = null
  let watchdogTimer = null
  let connectionGeneration = 0
  const WATCHDOG_MS = 45_000

  const clearWatchdog = () => {
    if (watchdogTimer) clearTimeout(watchdogTimer)
    watchdogTimer = null
  }

  const armWatchdog = (generation) => {
    clearWatchdog()
    watchdogTimer = setTimeout(() => {
      if (stopped || generation !== connectionGeneration) return
      streamDebug('events_watchdog', { generation })
      controller?.abort()
      scheduleReconnect()
    }, WATCHDOG_MS)
  }

  const scheduleReconnect = () => {
    if (stopped || reconnectTimer) return
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      connect()
    }, 1000)
  }

  const connect = () => {
    if (stopped) return
    const generation = ++connectionGeneration
    controller = new AbortController()
    streamDebug('events_connect', { generation })
    armWatchdog(generation)
    apiFetch('/v1/sessions/events', { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(`Session events request failed: ${res.status}`)
        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''

        const pump = () => reader.read().then(({ done, value }) => {
          if (generation !== connectionGeneration) return
          if (done) {
            clearWatchdog()
            streamDebug('events_eof', { generation })
            scheduleReconnect()
            return
          }
          // Includes SSE comment heartbeats. Any bytes prove that this
          // connection is still delivering data through the proxy/browser.
          armWatchdog(generation)
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
                onEvent(event)
              } catch {
                // skip malformed JSON
              }
            }
          }
          return pump()
        })

        return pump()
      })
      .catch((err) => {
        if (generation !== connectionGeneration || stopped) return
        clearWatchdog()
        if (err.name !== 'AbortError') {
          streamDebug('events_error', { generation, error: String(err) })
          onError?.(err)
        }
        scheduleReconnect()
      })
  }

  connect()
  return () => {
    stopped = true
    connectionGeneration += 1
    clearWatchdog()
    if (reconnectTimer) clearTimeout(reconnectTimer)
    reconnectTimer = null
    controller?.abort()
  }
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
