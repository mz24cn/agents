/**
 * Tests for remote-execution.svelte.js — session-level remote execution
 * binding + direct browser → child request helpers.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  remoteExecution,
  parseSetupUrl,
  fetchRemoteEnvList,
  invalidateRemoteEnvCache,
  resolveRemoteEnv,
  bindSessionToRemoteEnv,
  clearRemoteBinding,
  resolvePanelRemoteEnv,
  buildRemoteUrl,
  buildRemoteWsUrl,
  remoteRequest,
  remoteWorkspace,
  remoteSessions,
  fetchRemoteWorkspacePath,
  fetchRemoteTools,
} from './remote-execution.svelte.js'

function mockFetch(data, status = 200) {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(data),
  })
}

const ENV_RECORDS = {
  envs: [
    { id: 'http://10.0.0.5:7988', url: 'http://10.0.0.5:7988/v1/setup?token=as_abc', app_title: 'Child A' },
    { id: 'https://sub.example.com:8443', url: 'https://sub.example.com:8443/deploy/v1/setup', app_title: '' },
    { id: 'tunnel:0123456789abcdef', url: '', transport: 'ws-tunnel', app_title: 'Tun Child', status: 'online' },
  ],
}

beforeEach(() => {
  invalidateRemoteEnvCache()
  clearRemoteBinding()
  vi.stubGlobal('location', { protocol: 'http:', host: 'parent.local:7988' })
})

afterEach(() => {
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------------
// parseSetupUrl
// ---------------------------------------------------------------------------

describe('parseSetupUrl', () => {
  it('parses bare host with token', () => {
    const p = parseSetupUrl('http://10.0.0.5:7988/v1/setup?token=as_abc')
    expect(p.base).toBe('http://10.0.0.5:7988')
    expect(p.token).toBe('as_abc')
    expect(p.host).toBe('10.0.0.5:7988')
  })

  it('keeps sub-path prefix before /v1 and drops op param', () => {
    const p = parseSetupUrl('https://sub.example.com:8443/deploy/v1/setup?op=hello')
    expect(p.base).toBe('https://sub.example.com:8443/deploy')
    expect(p.token).toBe('')
  })

  it('handles path without /v1 segment', () => {
    const p = parseSetupUrl('http://h:1/a/b')
    expect(p.base).toBe('http://h:1/a/b')
  })
})

// ---------------------------------------------------------------------------
// bindSessionToRemoteEnv / resolveRemoteEnv
// ---------------------------------------------------------------------------

describe('bindSessionToRemoteEnv', () => {
  it('resolves the env record and populates the binding', async () => {
    vi.stubGlobal('fetch', mockFetch(ENV_RECORDS))
    const env = await bindSessionToRemoteEnv('sess-1', 'http://10.0.0.5:7988')
    expect(env).toMatchObject({
      id: 'http://10.0.0.5:7988',
      base: 'http://10.0.0.5:7988',
      token: 'as_abc',
      title: 'Child A',
    })
    expect(remoteExecution.sessionId).toBe('sess-1')
    expect(remoteExecution.env).toBe(env)
  })

  it('unbinds with empty env id', async () => {
    vi.stubGlobal('fetch', mockFetch(ENV_RECORDS))
    await bindSessionToRemoteEnv('sess-1', 'http://10.0.0.5:7988')
    await bindSessionToRemoteEnv('sess-1', '')
    expect(remoteExecution.sessionId).toBeNull()
    expect(remoteExecution.env).toBeNull()
  })

  it('returns null (and clears) for unknown env id', async () => {
    vi.stubGlobal('fetch', mockFetch(ENV_RECORDS))
    const env = await bindSessionToRemoteEnv('sess-1', 'http://nope:1')
    expect(env).toBeNull()
    expect(remoteExecution.env).toBeNull()
  })

  it('resolveRemoteEnv returns the raw record', async () => {
    vi.stubGlobal('fetch', mockFetch(ENV_RECORDS))
    const rec = await resolveRemoteEnv('https://sub.example.com:8443')
    expect(rec.url).toBe('https://sub.example.com:8443/deploy/v1/setup')
  })
})

// ---------------------------------------------------------------------------
// URL builders
// ---------------------------------------------------------------------------

describe('buildRemoteUrl / buildRemoteWsUrl', () => {
  it('appends token to child paths', async () => {
    vi.stubGlobal('fetch', mockFetch(ENV_RECORDS))
    await bindSessionToRemoteEnv('sess-1', 'http://10.0.0.5:7988')
    const url = buildRemoteUrl('/v1/tools')
    expect(url).toBe('http://10.0.0.5:7988/v1/tools?token=as_abc')
    const withQuery = buildRemoteUrl('/v1/workspace/list', { query: { path: 'a b', page: 2 } })
    expect(withQuery).toContain('path=a+b') // URLSearchParams encodes space as +
    expect(withQuery).toContain('page=2')
    expect(withQuery).toContain('token=as_abc')
  })

  it('keeps sub-path base for ws urls', async () => {
    vi.stubGlobal('fetch', mockFetch(ENV_RECORDS))
    await bindSessionToRemoteEnv('sess-2', 'https://sub.example.com:8443')
    const wsUrl = buildRemoteWsUrl('/v1/terminals/ws', { terminal_id: 't1' })
    expect(wsUrl).toBe('wss://sub.example.com:8443/v1/terminals/ws?terminal_id=t1')
  })

  it('tunnel envs bind to the same-origin tunnel-proxy bridge', async () => {
    vi.stubGlobal('fetch', mockFetch(ENV_RECORDS))
    const env = await bindSessionToRemoteEnv('sess-1', 'tunnel:0123456789abcdef')
    expect(env).toMatchObject({
      id: 'tunnel:0123456789abcdef',
      base: 'http://parent.local:7988/v1/tunnel-proxy/tunnel%3A0123456789abcdef',
      token: '',
      host: 'parent.local:7988',
      prefix: '/v1/tunnel-proxy/tunnel%3A0123456789abcdef',
      transport: 'ws-tunnel',
    })
    // HTTP through the bridge: same origin, no child token in the URL
    const url = buildRemoteUrl('/v1/workspace/list', { query: { path: '.' } })
    expect(url).toBe('http://parent.local:7988/v1/tunnel-proxy/tunnel%3A0123456789abcdef/v1/workspace/list?path=.')
    expect(url).not.toContain('token=')
    // terminal WS through the bridge (protocol follows the page)
    const wsUrl = buildRemoteWsUrl('/v1/terminals/ws', { terminal_id: 't9' })
    expect(wsUrl).toBe(
      'ws://parent.local:7988/v1/tunnel-proxy/tunnel%3A0123456789abcdef/v1/terminals/ws?terminal_id=t9')
    // workspace media URLs point at the bridge too
    expect(remoteWorkspace.content('a.png')).toBe(
      'http://parent.local:7988/v1/tunnel-proxy/tunnel%3A0123456789abcdef/v1/workspace/content?path=a.png&restrict=1')
  })

  it('tunnel id prefix without transport is also treated as tunnel', async () => {
    vi.stubGlobal('fetch', mockFetch({ envs: [{ id: 'tunnel:ffffffffffffffff', url: 'http://direct:1/v1/setup', status: 'online' }] }))
    const env = await bindSessionToRemoteEnv('sess-1', 'tunnel:ffffffffffffffff')
    expect(env.base).toContain('/v1/tunnel-proxy/')
    expect(env.token).toBe('')
  })

  it('throws when nothing is bound', () => {
    expect(() => buildRemoteUrl('/v1/tools')).toThrow()
    expect(() => buildRemoteWsUrl('/v1/terminals/ws')).toThrow()
  })
})

// ---------------------------------------------------------------------------
// resolvePanelRemoteEnv (file manager env resolution)
// ---------------------------------------------------------------------------

describe('resolvePanelRemoteEnv', () => {
  it('follows the selector when no session exists yet', async () => {
    // handleRemoteEnvChange binds with sessionId=null before the first message.
    vi.stubGlobal('fetch', mockFetch(ENV_RECORDS))
    const env = await bindSessionToRemoteEnv(null, 'http://10.0.0.5:7988')
    expect(resolvePanelRemoteEnv(null)).toBe(env)
    expect(resolvePanelRemoteEnv(undefined)).toBe(env)
  })

  it('follows the session binding when a session exists', async () => {
    vi.stubGlobal('fetch', mockFetch(ENV_RECORDS))
    const env = await bindSessionToRemoteEnv('sess-1', 'http://10.0.0.5:7988')
    expect(resolvePanelRemoteEnv('sess-1')).toBe(env)
  })

  it('stays local for a session that is not the bound one', async () => {
    vi.stubGlobal('fetch', mockFetch(ENV_RECORDS))
    await bindSessionToRemoteEnv('sess-1', 'http://10.0.0.5:7988')
    expect(resolvePanelRemoteEnv('sess-2')).toBeNull()
    // ...and a pre-session panel must not pick up another session's binding.
    expect(resolvePanelRemoteEnv(null)).toBeNull()
  })

  it('stays local when nothing is bound or in forced-local mode', async () => {
    expect(resolvePanelRemoteEnv(null)).toBeNull()
    vi.stubGlobal('fetch', mockFetch(ENV_RECORDS))
    await bindSessionToRemoteEnv(null, 'http://10.0.0.5:7988')
    expect(resolvePanelRemoteEnv(null, true)).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// remoteRequest + child API surfaces
// ---------------------------------------------------------------------------

describe('remoteRequest / remoteWorkspace / remoteSessions', () => {
  it('remoteRequest posts JSON and throws with status on error', async () => {
    vi.stubGlobal('fetch', mockFetch(ENV_RECORDS))
    await bindSessionToRemoteEnv('sess-1', 'http://10.0.0.5:7988')
    vi.stubGlobal('fetch', mockFetch({ error: 'JournalConflict' }, 409))
    await expect(remoteRequest('POST', '/v1/sessions/sess-1/revoke?journal_only=true', { timestamp: 't' }))
      .rejects.toMatchObject({ status: 409, code: 'JournalConflict' })
  })

  it('workspace content/download/thumbnail URLs carry the token', async () => {
    vi.stubGlobal('fetch', mockFetch(ENV_RECORDS))
    await bindSessionToRemoteEnv('sess-1', 'http://10.0.0.5:7988')
    expect(remoteWorkspace.content('a/b.png')).toBe(
      'http://10.0.0.5:7988/v1/workspace/content?path=a%2Fb.png&restrict=1&token=as_abc')
    expect(remoteWorkspace.download('x.txt', false)).toBe(
      'http://10.0.0.5:7988/v1/workspace/download?path=x.txt&restrict=0&token=as_abc')
    expect(remoteWorkspace.thumbnail('y.png')).toBe(
      'http://10.0.0.5:7988/v1/workspace/thumbnail?path=y.png&restrict=1&token=as_abc')
  })

  it('workspace list hits the child with full query', async () => {
    vi.stubGlobal('fetch', mockFetch(ENV_RECORDS))
    await bindSessionToRemoteEnv('sess-1', 'http://10.0.0.5:7988')
    const listFetch = mockFetch({ entries: [] })
    vi.stubGlobal('fetch', listFetch)
    await remoteWorkspace.list('dir', 1, 20, true, { sort: 'time', nameFilter: 'f' })
    const [url, opts] = listFetch.mock.calls[0]
    expect(url).toContain('/v1/workspace/list?')
    expect(url).toContain('path=dir')
    expect(url).toContain('name_filter=f')
    expect(url).toContain('token=as_abc')
    expect(opts.method).toBe('GET')
  })

  it('remoteSessions file-journals endpoints point at the child session', async () => {
    vi.stubGlobal('fetch', mockFetch(ENV_RECORDS))
    await bindSessionToRemoteEnv('sess-9', 'http://10.0.0.5:7988')
    const jf = mockFetch({ turn_keys: [] })
    vi.stubGlobal('fetch', jf)
    await remoteSessions.fileJournals('sess-9')
    expect(jf.mock.calls[0][0]).toBe('http://10.0.0.5:7988/v1/sessions/sess-9/file-journals?token=as_abc')
    const df = mockFetch({ diffs: {} })
    vi.stubGlobal('fetch', df)
    await remoteSessions.fileJournalDiff('sess-9', 'turn 1')
    expect(df.mock.calls[0][0]).toBe('http://10.0.0.5:7988/v1/sessions/sess-9/file-journals/turn%201?token=as_abc')
  })

  it('fetchRemoteWorkspacePath reads AGENTS_WORKSPACE from child env', async () => {
    vi.stubGlobal('fetch', mockFetch(ENV_RECORDS))
    await bindSessionToRemoteEnv('sess-1', 'http://10.0.0.5:7988')
    vi.stubGlobal('fetch', mockFetch({ env: { AGENTS_WORKSPACE: '/opt/child-ws' } }))
    await expect(fetchRemoteWorkspacePath()).resolves.toBe('/opt/child-ws')
  })

  it('fetchRemoteTools returns the child tool list', async () => {
    vi.stubGlobal('fetch', mockFetch(ENV_RECORDS))
    await bindSessionToRemoteEnv('sess-1', 'http://10.0.0.5:7988')
    const tools = [{ tool_id: 'write_file', name: 'write_file', tool_type: 'function' }]
    vi.stubGlobal('fetch', mockFetch({ tools }))
    await expect(fetchRemoteTools()).resolves.toEqual(tools)
  })

  it('remoteRequest throws a readable error on a 2xx non-JSON body (never returns null)', async () => {
    // Regression: a 200 + text/html (e.g. the SPA index.html served when the
    // tunnel-proxy child path fell back to "/") used to surface downstream as
    // "Cannot read properties of null (reading 'tools')".
    vi.stubGlobal('fetch', mockFetch(ENV_RECORDS))
    await bindSessionToRemoteEnv('sess-1', 'tunnel:0123456789abcdef')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.reject(new SyntaxError('Unexpected token <')),
    }))
    await expect(remoteRequest('GET', '/v1/tools'))
      .rejects.toMatchObject({ status: 200 })
    await expect(remoteRequest('GET', '/v1/tools'))
      .rejects.toThrow('non-JSON')
  })

  it('fetchRemoteEnvList caches within TTL', async () => {
    const lf = mockFetch(ENV_RECORDS)
    vi.stubGlobal('fetch', lf)
    await fetchRemoteEnvList()
    await fetchRemoteEnvList()
    expect(lf).toHaveBeenCalledTimes(1)
    invalidateRemoteEnvCache()
    await fetchRemoteEnvList()
    expect(lf).toHaveBeenCalledTimes(2)
  })
})
