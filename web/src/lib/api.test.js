/**
 * Tests for api.js — env and sessions API methods
 * Validates: Requirements 6.1–6.6, 4.1, 5.5, 5.6
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { env, sessions, subscribeSessionStream } from './api.js'

// ---------------------------------------------------------------------------
// Helper: create a mock fetch that returns the given data with the given status
// ---------------------------------------------------------------------------

function mockFetch(data, status = 200) {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(data),
  })
}

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------

beforeEach(() => {
  // Reset fetch mock before each test
  vi.restoreAllMocks()
})

afterEach(() => {
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------------
// env.list — GET /v1/env
// ---------------------------------------------------------------------------

describe('env.list', () => {
  it('sends GET /v1/env and returns response data', async () => {
    const responseData = { env: { OPENAI_API_KEY: 'sk-xxx' } }
    vi.stubGlobal('fetch', mockFetch(responseData))

    const result = await env.list()

    expect(fetch).toHaveBeenCalledOnce()
    const [url, opts] = fetch.mock.calls[0]
    expect(url).toBe('/v1/env')
    expect(opts.method).toBe('GET')
    expect(result).toEqual(responseData)
  })

  it('throws Error when response is not ok (non-2xx)', async () => {
    vi.stubGlobal('fetch', mockFetch({ error: 'Internal Server Error' }, 500))

    await expect(env.list()).rejects.toThrow('Internal Server Error')
  })
})

// ---------------------------------------------------------------------------
// subscribeSessionStream — reconnect and resume by SSE frame id
// ---------------------------------------------------------------------------

function sseResponse(chunks) {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

async function flushPromises() {
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
}

describe('subscribeSessionStream', () => {
  it('reconnects after unexpected EOF and resumes after the last applied frame id', async () => {
    vi.useFakeTimers()
    const first = sseResponse([
      'event: init\ndata: {"active":true,"persisted_seq":4,"latest_seq":5}\n\n',
      'id: 5\ndata: {"role":"assistant","content":"first"}\n\n',
    ])
    const second = sseResponse([
      'event: init\ndata: {"active":true,"persisted_seq":5,"latest_seq":6}\n\n',
      'id: 6\ndata: {"role":"assistant","content":"second"}\n\n',
      'data: [DONE]\n\n',
    ])
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(second))
    const messages = []
    const done = vi.fn()

    const unsubscribe = subscribeSessionStream('session 1', (msg) => messages.push(msg.content), done, vi.fn())
    await flushPromises()
    expect(fetch.mock.calls[0][0]).toBe('/v1/sessions/session%201/stream?after=-1')

    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    expect(fetch.mock.calls[1][0]).toBe('/v1/sessions/session%201/stream?after=5')
    expect(messages).toEqual(['first', 'second'])
    expect(done).toHaveBeenCalledOnce()
    unsubscribe()
    vi.useRealTimers()
  })

  it('waits for batched UI acknowledgement before advancing the resume cursor', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(sseResponse([
        'event: init\ndata: {"active":true,"persisted_seq":4,"latest_seq":5}\n\n',
        'id: 5\ndata: {"role":"assistant","content":"first"}\n\n',
      ]))
      .mockResolvedValueOnce(sseResponse(['data: [DONE]\n\n'])))
    const acknowledgements = []

    const unsubscribe = subscribeSessionStream(
      'session-ack',
      vi.fn(),
      vi.fn(),
      vi.fn(),
      null,
      -1,
      (acknowledge) => acknowledgements.push(acknowledge),
    )
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    // EOF must not reconnect ahead of the queued animation-frame application.
    expect(fetch).toHaveBeenCalledOnce()
    expect(acknowledgements).toHaveLength(1)
    acknowledgements[0]()
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    expect(fetch.mock.calls[1][0]).toBe('/v1/sessions/session-ack/stream?after=5')
    unsubscribe()
    vi.useRealTimers()
  })

  it('uses persisted_seq as the resume baseline if the first connection drops after init', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(sseResponse([
        'event: init\ndata: {"active":true,"persisted_seq":9,"latest_seq":9}\n\n',
      ]))
      .mockResolvedValueOnce(sseResponse(['data: [DONE]\n\n'])))

    const unsubscribe = subscribeSessionStream('session-2', vi.fn(), vi.fn(), vi.fn())
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    expect(fetch.mock.calls[1][0]).toBe('/v1/sessions/session-2/stream?after=9')
    unsubscribe()
    vi.useRealTimers()
  })

  it('cancels a pending reconnect when unsubscribed', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sseResponse([])))

    const unsubscribe = subscribeSessionStream('session-3', vi.fn(), vi.fn(), vi.fn())
    await flushPromises()
    unsubscribe()
    await vi.advanceTimersByTimeAsync(1000)

    expect(fetch).toHaveBeenCalledOnce()
    vi.useRealTimers()
  })
})

// ---------------------------------------------------------------------------
// env.set — POST /v1/env with { key, value }
// ---------------------------------------------------------------------------

describe('env.set', () => {
  it('sends POST /v1/env with correct body and returns response data', async () => {
    const responseData = { env: { OPENAI_API_KEY: 'sk-new' } }
    vi.stubGlobal('fetch', mockFetch(responseData))

    const result = await env.set('OPENAI_API_KEY', 'sk-new')

    expect(fetch).toHaveBeenCalledOnce()
    const [url, opts] = fetch.mock.calls[0]
    expect(url).toBe('/v1/env')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ key: 'OPENAI_API_KEY', value: 'sk-new' })
    expect(result).toEqual(responseData)
  })

  it('sends correct key and value in request body', async () => {
    vi.stubGlobal('fetch', mockFetch({ env: {} }))

    await env.set('MY_KEY', 'my_value')

    const [, opts] = fetch.mock.calls[0]
    const body = JSON.parse(opts.body)
    expect(body.key).toBe('MY_KEY')
    expect(body.value).toBe('my_value')
  })

  it('throws Error when response is not ok (400)', async () => {
    vi.stubGlobal('fetch', mockFetch({ error: 'key 不能为空' }, 400))

    await expect(env.set('', 'value')).rejects.toThrow('key 不能为空')
  })
})

// ---------------------------------------------------------------------------
// env.delete — DELETE /v1/env/{key}，key 需 URL 编码
// ---------------------------------------------------------------------------

describe('env.delete', () => {
  it('sends DELETE /v1/env/{key} and returns response data', async () => {
    const responseData = { env: {} }
    vi.stubGlobal('fetch', mockFetch(responseData))

    const result = await env.delete('OPENAI_API_KEY')

    expect(fetch).toHaveBeenCalledOnce()
    const [url, opts] = fetch.mock.calls[0]
    expect(url).toBe('/v1/env/OPENAI_API_KEY')
    expect(opts.method).toBe('DELETE')
    expect(result).toEqual(responseData)
  })

  it('URL-encodes the key in the path', async () => {
    vi.stubGlobal('fetch', mockFetch({ env: {} }))

    await env.delete('MY KEY/WITH SPECIAL')

    const [url] = fetch.mock.calls[0]
    expect(url).toBe(`/v1/env/${encodeURIComponent('MY KEY/WITH SPECIAL')}`)
    expect(url).not.toContain(' ')
  })

  it('sends no request body for DELETE', async () => {
    vi.stubGlobal('fetch', mockFetch({ env: {} }))

    await env.delete('SOME_KEY')

    const [, opts] = fetch.mock.calls[0]
    expect(opts.body).toBeUndefined()
  })

  it('throws Error when response is not ok', async () => {
    vi.stubGlobal('fetch', mockFetch({ error: 'Request failed: 500' }, 500))

    await expect(env.delete('KEY')).rejects.toThrow()
  })
})

// ---------------------------------------------------------------------------
// env.detect — POST /v1/env/detect（无请求体）
// ---------------------------------------------------------------------------

describe('env.detect', () => {
  it('sends POST /v1/env/detect and returns response data', async () => {
    const responseData = { keys: ['OPENAI_API_KEY', 'ANTHROPIC_API_KEY'] }
    vi.stubGlobal('fetch', mockFetch(responseData))

    const result = await env.detect()

    expect(fetch).toHaveBeenCalledOnce()
    const [url, opts] = fetch.mock.calls[0]
    expect(url).toBe('/v1/env/detect')
    expect(opts.method).toBe('POST')
    expect(result).toEqual(responseData)
  })

  it('sends no request body for detect', async () => {
    vi.stubGlobal('fetch', mockFetch({ keys: [] }))

    await env.detect()

    const [, opts] = fetch.mock.calls[0]
    // body should be absent (null body is not serialized)
    expect(opts.body).toBeUndefined()
  })

  it('throws Error when response is not ok', async () => {
    vi.stubGlobal('fetch', mockFetch({ error: 'Request failed: 500' }, 500))

    await expect(env.detect()).rejects.toThrow()
  })
})

// ---------------------------------------------------------------------------
// sessions.list — GET /v1/sessions
// ---------------------------------------------------------------------------

describe('sessions.list', () => {
  it('sends GET /v1/sessions and returns response data', async () => {
    const responseData = { sessions: ['2026-04-19_12-01-50', '2026-04-19_10-23-12'] }
    vi.stubGlobal('fetch', mockFetch(responseData))

    const result = await sessions.list()

    expect(fetch).toHaveBeenCalledOnce()
    const [url, opts] = fetch.mock.calls[0]
    expect(url).toBe('/v1/sessions?page=1&page_size=100')
    expect(opts.method).toBe('GET')
    expect(result).toEqual(responseData)
  })

  it('throws Error when response is not ok', async () => {
    vi.stubGlobal('fetch', mockFetch({ error: 'Request failed: 500' }, 500))

    await expect(sessions.list()).rejects.toThrow()
  })
})

// ---------------------------------------------------------------------------
// sessions.get — GET /v1/sessions/{sessionId}，sessionId 需 URL 编码
// ---------------------------------------------------------------------------

describe('sessions.get', () => {
  it('sends GET /v1/sessions/{sessionId} and returns response data', async () => {
    const responseData = { meta: { session_id: '2026-04-19_12-01-50' }, messages: [] }
    vi.stubGlobal('fetch', mockFetch(responseData))

    const result = await sessions.get('2026-04-19_12-01-50')

    expect(fetch).toHaveBeenCalledOnce()
    const [url, opts] = fetch.mock.calls[0]
    expect(url).toBe('/v1/sessions/2026-04-19_12-01-50')
    expect(opts.method).toBe('GET')
    expect(result).toEqual(responseData)
  })

  it('URL-encodes the sessionId in the path', async () => {
    vi.stubGlobal('fetch', mockFetch({ meta: {}, messages: [] }))

    const sessionId = 'session with spaces/and slashes'
    await sessions.get(sessionId)

    const [url] = fetch.mock.calls[0]
    expect(url).toBe(`/v1/sessions/${encodeURIComponent(sessionId)}`)
    expect(url).not.toContain(' ')
  })

  it('reports real downloaded bytes and Content-Length when a progress callback is provided', async () => {
    const json = JSON.stringify({ meta: {}, messages: [{ role: 'user', content: 'hello' }] })
    const bytes = new TextEncoder().encode(json)
    const splitAt = Math.floor(bytes.length / 2)
    const progress = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(bytes.slice(0, splitAt))
        controller.enqueue(bytes.slice(splitAt))
        controller.close()
      },
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json', 'Content-Length': String(bytes.length) },
    })))

    const result = await sessions.get('session-1', progress)

    expect(result.messages[0].content).toBe('hello')
    expect(progress).toHaveBeenLastCalledWith({ received: bytes.length, total: bytes.length })
    expect(progress.mock.calls.some(([value]) => value.received > 0 && value.received < value.total)).toBe(true)
  })

  it('throws Error when session not found (404)', async () => {
    vi.stubGlobal('fetch', mockFetch({ error: 'Session not found: bad-id' }, 404))

    await expect(sessions.get('bad-id')).rejects.toThrow('Session not found: bad-id')
  })

  it('throws Error when conversation format is invalid (400)', async () => {
    vi.stubGlobal('fetch', mockFetch({ error: 'Invalid conversation format: ...' }, 400))

    await expect(sessions.get('some-id')).rejects.toThrow('Invalid conversation format')
  })
})

// ---------------------------------------------------------------------------
// sessions.revoke — POST /v1/sessions/{sessionId}/revoke
// ---------------------------------------------------------------------------

describe('sessions.revoke', () => {
  it('sends revoke request with forced false by default', async () => {
    vi.stubGlobal('fetch', mockFetch({ status: 'success' }))

    await sessions.revoke('session-1', '2026-05-13T10:00:00')

    expect(fetch).toHaveBeenCalledOnce()
    const [url, opts] = fetch.mock.calls[0]
    expect(url).toBe('/v1/sessions/session-1/revoke')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({
      session_id: 'session-1',
      timestamp: '2026-05-13T10:00:00',
      forced: false,
      keep_files: false,
    })
  })

  it('sends revoke request with forced true', async () => {
    vi.stubGlobal('fetch', mockFetch({ status: 'success' }))

    await sessions.revoke('session-1', '2026-05-13T10:00:00', { forced: true })

    const [, opts] = fetch.mock.calls[0]
    expect(JSON.parse(opts.body)).toEqual({
      session_id: 'session-1',
      timestamp: '2026-05-13T10:00:00',
      forced: true,
      keep_files: false,
    })
  })

  it('sends revoke request with keep_files true', async () => {
    vi.stubGlobal('fetch', mockFetch({ status: 'success' }))

    await sessions.revoke('session-1', '2026-05-13T10:00:00', { keepFiles: true })

    const [, opts] = fetch.mock.calls[0]
    expect(JSON.parse(opts.body)).toEqual({
      session_id: 'session-1',
      timestamp: '2026-05-13T10:00:00',
      forced: false,
      keep_files: true,
    })
  })

  it('preserves structured error metadata', async () => {
    const errorData = {
      error: 'JournalConflict',
      message: 'Current files do not match journal after-state',
      files: ['runtime/server.py'],
      can_force: true,
    }
    vi.stubGlobal('fetch', mockFetch(errorData, 409))

    try {
      await sessions.revoke('session-1', '2026-05-13T10:00:00')
      throw new Error('Expected revoke to fail')
    } catch (err) {
      expect(err.message).toBe('Current files do not match journal after-state')
      expect(err.status).toBe(409)
      expect(err.code).toBe('JournalConflict')
      expect(err.data).toEqual(errorData)
    }
  })
})
