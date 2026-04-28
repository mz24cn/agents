/**
 * API client module — unified fetch wrapper and resource endpoints.
 *
 * All paths are relative (e.g. "/v1/models") so that Vite's dev-server
 * proxy forwards them to the Python backend automatically.
 */

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
  const res = await fetch(path, opts)
  const data = await res.json()
  if (!res.ok) {
    // Support both single "error" string and "errors" array
    const msg = data.error
      || (Array.isArray(data.errors) ? data.errors.join('\n') : null)
      || `Request failed: ${res.status}`
    throw new Error(msg)
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
  list:   ()              => request('GET',    '/v1/mcp-servers'),
  delete: (serverName)    => request('DELETE', `/v1/mcp-servers/${encodeURIComponent(serverName)}`),
}

/** Prompt template CRUD helpers. */
export const promptTemplates = {
  list:   ()                        => request('GET',    '/v1/prompt-templates'),
  create: (data)                    => request('POST',   '/v1/prompt-templates', data),
  update: (templateId, data)        => request('PUT',    `/v1/prompt-templates/${templateId}`, data),
  delete: (templateId)              => request('DELETE', `/v1/prompt-templates/${templateId}`),
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
export function inferStream(body, onMessage, onDone, onError) {
  const controller = new AbortController()

  fetch('/v1/infer/stream', {
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
            if (!trimmed || !trimmed.startsWith('data:')) continue
            const payload = trimmed.slice(5).trim()
            if (payload === '[DONE]') {
              onDone()
              return
            }
            try {
              onMessage(JSON.parse(payload))
            } catch {
              // skip malformed JSON chunks
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

/** 环境变量 API */
export const env = {
  list:   ()              => request('GET',    '/v1/env'),
  set:    (key, value)    => request('POST',   '/v1/env', { key, value }),
  delete: (key)           => request('DELETE', `/v1/env/${encodeURIComponent(key)}`),
  detect: ()              => request('POST',   '/v1/env/detect'),
}

/** 会话 API */
export const sessions = {
  list:          ()              => request('GET',    '/v1/sessions'),
  get:           (sessionId)     => request('GET',    `/v1/sessions/${encodeURIComponent(sessionId)}`),
  delete:        (sessionId)     => request('DELETE', `/v1/sessions/${encodeURIComponent(sessionId)}`),
  generateTitle: (sessionId)     => request('POST',   `/v1/sessions/${encodeURIComponent(sessionId)}/generate-title`),
}
