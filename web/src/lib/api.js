/**
 * API client module — unified fetch wrapper and resource endpoints.
 *
 * All paths are relative (e.g. "/v1/models") so that Vite's dev-server
 * proxy forwards them to the Python backend automatically.
 */

import { ensureAuthenticated } from './auth-state.svelte.js'

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
  if (!res.ok) {
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
  return data
}

/** Model CRUD helpers. */
export const models = {
  list:   ()                  => request('GET',    '/v1/models'),
  create: (config)            => request('POST',   '/v1/models', config),
  update: (modelId, config)   => request('PUT',    `/v1/models/${modelId}`, config),
  delete: (modelId)           => request('DELETE', `/v1/models/${modelId}`),
}

/** Tool CRUD helpers. */
export const tools = {
  list:        ()                  => request('GET',    '/v1/tools'),
  create:      (config)            => request('POST',   '/v1/tools', config),
  createMcp:   (config)            => request('POST',   '/v1/tools/mcp', config),
  createSkill: (skillDir)          => request('POST',   '/v1/tools/skill', { skill_dir: skillDir }),
  update:      (toolId, config)    => request('PUT',    `/v1/tools/${toolId}`, config),
  delete:      (toolId)            => request('DELETE', `/v1/tools/${toolId}`),
  batchDelete: (toolIds)           => request('DELETE', '/v1/tools/batch', { tool_ids: toolIds }),
}

/** MCP server helpers. */
export const mcpServers = {
  list:    ()                          => request('GET',    '/v1/mcp-servers'),
  delete:  (serverName)                => request('DELETE', `/v1/mcp-servers/${encodeURIComponent(serverName)}`),
  restore: (serverName, config)        => request('PUT',    `/v1/mcp-servers/${encodeURIComponent(serverName)}`, config),
}

/** Prompt template CRUD helpers. */
export const promptTemplates = {
  list:   ()                        => request('GET',    '/v1/prompt-templates'),
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
export function inferStream(body, onMessage, onDone, onError, onInit = null) {
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

      function pump() {
        reader.read().then(({ done, value }) => {
          if (done) {
            onDone()
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
                  onMessage(data)
                }
              } catch {
                // skip malformed JSON chunks
              }
            } else if (trimmed === '') {
              currentEvent = 'message'  // Reset to default event type after blank line
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
  list:   ()              => request('GET',    '/v1/env'),
  set:    (key, value)    => request('POST',   '/v1/env', { key, value }),
  delete: (key)           => request('DELETE', `/v1/env/${encodeURIComponent(key)}`),
  detect: ()              => request('POST',   '/v1/env/detect'),
}

/** 会话 API */
export const sessions = {
  list:          (page = 1, pageSize = 100) => request('GET', `/v1/sessions?page=${encodeURIComponent(page)}&page_size=${encodeURIComponent(pageSize)}`),
  search:        (query, page = 1, pageSize = 100) => request('GET', `/v1/sessions/search?q=${encodeURIComponent(query)}&page=${encodeURIComponent(page)}&page_size=${encodeURIComponent(pageSize)}`),
  get:           (sessionId)     => request('GET',    `/v1/sessions/${encodeURIComponent(sessionId)}`),
  delete:        (sessionId)     => request('DELETE', `/v1/sessions/${encodeURIComponent(sessionId)}`),
  generateTitle: (sessionId)     => request('POST',   `/v1/sessions/${encodeURIComponent(sessionId)}/generate-title`),
  revoke:        (sessionId, timestamp, { forced = false, keepFiles = false } = {}) => request('POST', `/v1/sessions/${encodeURIComponent(sessionId)}/revoke`, { session_id: sessionId, timestamp, forced, keep_files: keepFiles }),
  markRead:      (sessionId)     => request('POST',   `/v1/sessions/${encodeURIComponent(sessionId)}/read`),
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
  const controller = new AbortController()

  apiFetch('/v1/sessions/events', {
    signal: controller.signal,
  })
    .then((res) => {
      if (!res.ok) {
        throw new Error(`Session events request failed: ${res.status}`)
      }
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      function pump() {
        reader.read().then(({ done, value }) => {
          if (done) {
            // Stream closed cleanly — reconnect is left to the caller
            return
          }
          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''
          for (const line of lines) {
            const trimmed = line.trim()
            if (trimmed.startsWith('data: ')) {
              const payload = trimmed.slice(6).trim()
              try {
                const data = JSON.parse(payload)
                onEvent(data)
              } catch {
                // skip malformed JSON
              }
            }
          }
          pump()
        }).catch((err) => {
          if (err.name !== 'AbortError') {
            onError(err)
          }
        })
      }

      pump()
    })
    .catch((err) => {
      if (err.name !== 'AbortError') {
        onError(err)
      }
    })

  return () => controller.abort()
}

export const auth = {
  config: () => request('GET', '/v1/auth/config'),
  updateConfig: (data) => request('POST', '/v1/auth/config', data),
  disable: () => request('POST', '/v1/auth/config', { disable_auth: true }),
  login: (password) => request('POST', '/v1/auth/login', { password }),
  logout: () => request('POST', '/v1/auth/logout', {}),
}

/** 智能体 API */
export const agents = {
  list:   ()              => request('GET',    '/v1/agents'),
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
  search:   (path, query) => request('GET', `/v1/workspace/search?path=${encodeURIComponent(path)}&query=${encodeURIComponent(query)}`),
  content:  (path, restrict = true) => `/v1/workspace/content?path=${encodeURIComponent(path)}&restrict=${restrict ? 1 : 0}`,
  download: (path, restrict = true) => `/v1/workspace/download?path=${encodeURIComponent(path)}&restrict=${restrict ? 1 : 0}`,
  thumbnail:(path, restrict = true) => `/v1/workspace/thumbnail?path=${encodeURIComponent(path)}&restrict=${restrict ? 1 : 0}`,
  rename:   (path, newName) => request('POST', '/v1/workspace/rename', { path, new_name: newName }),
  duplicate:(path) => request('POST', '/v1/workspace/duplicate', { path }),
  delete:   (path) => request('DELETE', '/v1/workspace/delete', { path }),
  move:     (paths, destDir, overwrite = false) => request('POST', '/v1/workspace/move', { paths, dest_dir: destDir, overwrite }),
  copy:     (paths, destDir, overwrite = false) => request('POST', '/v1/workspace/copy', { paths, dest_dir: destDir, overwrite }),
  uploadInit: (data) => request('POST', '/v1/workspace/upload/init', data),
  uploadChunk: (uploadId, chunk, blob, onProgress) => uploadChunkWithProgress(uploadId, chunk, blob, onProgress),
  uploadComplete: (uploadId) => request('POST', `/v1/workspace/upload/${encodeURIComponent(uploadId)}/complete`, {}),
  uploadCancel: (uploadId) => request('DELETE', `/v1/workspace/upload/${encodeURIComponent(uploadId)}`),
}
