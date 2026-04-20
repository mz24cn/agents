/**
 * Tests for api.js — env and sessions API methods
 * Validates: Requirements 6.1–6.6, 4.1, 5.5, 5.6
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { env, sessions } from './api.js'

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
    expect(url).toBe('/v1/sessions')
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

  it('throws Error when session not found (404)', async () => {
    vi.stubGlobal('fetch', mockFetch({ error: 'Session not found: bad-id' }, 404))

    await expect(sessions.get('bad-id')).rejects.toThrow('Session not found: bad-id')
  })

  it('throws Error when conversation format is invalid (400)', async () => {
    vi.stubGlobal('fetch', mockFetch({ error: 'Invalid conversation format: ...' }, 400))

    await expect(sessions.get('some-id')).rejects.toThrow('Invalid conversation format')
  })
})
