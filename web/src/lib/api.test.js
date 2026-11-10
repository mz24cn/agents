/**
 * Tests for api.js — env and sessions API methods
 * Validates: Requirements 6.1–6.6, 4.1, 5.5, 5.6
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { env, sessions, inferStream, subscribeSessionStream, isSessionStreamWatched, subscribeSessionEvents, __resetSessionEventsHubForTests, __resetSessionFramesHubForTests, remoteEnv, remoteEnvHomeUrl, maskRemoteEnvUrl, isRemoteLogoImage, resolveRemoteEnvLogo, tools, build } from './api.js'

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

// ---------------------------------------------------------------------------
// subscribeSessionStream — 每 tab 单条共享帧通道（connection budget）
// ---------------------------------------------------------------------------
// 背景：浏览器对同一 origin 的 HTTP/1.1 并发连接上限通常是 6 条（WS 也计入）。
// 旧实现每个流式会话各占一条长连接，3-4 个并发推理 + 事件 hub + 终端 WS 就把
// 6 条占满，之后的普通请求在浏览器侧排队，表现为"后端卡住"。现在每 tab 只有
// 一条 GET /v1/sessions/frames，会话通过短命令增减，连接数与并发会话数无关。

function makeChannel() {
  const encoder = new TextEncoder()
  let controller = null
  const response = new Response(new ReadableStream({
    start(c) { controller = c },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
  return {
    response,
    push: (chunk) => controller.enqueue(encoder.encode(chunk)),
    end: () => controller.close(),
  }
}

/** fetch mock that separates the channel connection from the short commands. */
function framesTransport(channels = []) {
  const calls = { channel: [], commands: [] }
  let index = 0
  const fetchMock = vi.fn((url, opts = {}) => {
    if (url.startsWith('/v1/sessions/frames?conn=')) {
      calls.channel.push({ url, opts })
      const channel = index < channels.length ? channels[index] : channels[channels.length - 1]
      index += 1
      return Promise.resolve(channel.response)
    }
    if (url.startsWith('/v1/sessions/frames/')) {
      calls.commands.push({ url, body: JSON.parse(opts.body) })
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ ok: true, registered: true, active: true, persisted_seq: 0, latest_seq: 0, replayed: 0 }),
      })
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) })
  })
  return { calls, fetch: fetchMock }
}

const chanInit = (conn) => `event: chan_init\ndata: {"conn":"${conn}","generation":1}\n\n`
const subInit = (sid, persisted = 0) => `event: sub_init\ndata: {"session_id":"${sid}","active":true,"persisted_seq":${persisted},"latest_seq":0}\n\n`
const chanFrame = (sid, seq, content) => `event: frame\nid: ${seq}\ndata: {"session_id":"${sid}","seq":${seq},"event":null,"frame":{"role":"assistant","content":"${content}"}}\n\n`
const chanDone = (sid) => `event: done\ndata: {"session_id":"${sid}"}\n\n`

/** Drain the reader pump: every enqueued chunk costs several microtask ticks. */
async function settle() {
  for (let i = 0; i < 30; i++) await Promise.resolve()
}

describe('subscribeSessionStream (shared frame channel)', () => {
  beforeEach(() => {
    __resetSessionFramesHubForTests()
  })
  afterEach(() => {
    __resetSessionFramesHubForTests()
    vi.useRealTimers()
  })

  it('watches several sessions over one channel connection', async () => {
    vi.useFakeTimers()
    const channel = makeChannel()
    const transport = framesTransport([channel])
    vi.stubGlobal('fetch', transport.fetch)

    subscribeSessionStream('session-a', vi.fn(), vi.fn(), vi.fn())
    subscribeSessionStream('session-b', vi.fn(), vi.fn(), vi.fn())
    await settle()

    expect(transport.calls.channel).toHaveLength(1)
    expect(transport.calls.channel[0].url).toMatch(/^\/v1\/sessions\/frames\?conn=/)
    const subscribed = transport.calls.commands
      .filter((c) => c.url === '/v1/sessions/frames/subscribe')
      .map((c) => c.body.session_id)
    expect(subscribed).toEqual(['session-a', 'session-b'])
    // Both commands ride the same tab channel id.
    const conns = new Set(transport.calls.commands.map((c) => c.body.conn))
    expect(conns.size).toBe(1)
  })

  it('reports whether the channel watches a session, so the page can re-arm', async () => {
    vi.useFakeTimers()
    const channel = makeChannel()
    const transport = framesTransport([channel])
    vi.stubGlobal('fetch', transport.fetch)

    expect(isSessionStreamWatched('session-a')).toBe(false)
    const stop = subscribeSessionStream('session-a', vi.fn(), vi.fn(), vi.fn())
    await settle()
    expect(isSessionStreamWatched('session-a')).toBe(true)

    // An unexpected per-session "done" drops the entry: the page must observe
    // that, otherwise nothing would ever resume a still-running inference.
    channel.push(chanDone('session-a'))
    await settle()
    expect(isSessionStreamWatched('session-a')).toBe(false)
    stop()
  })

  it('re-registers watched sessions exactly once after a reconnect', async () => {
    vi.useFakeTimers()
    const first = makeChannel()
    const second = makeChannel()
    const transport = framesTransport([first, second])
    vi.stubGlobal('fetch', transport.fetch)

    subscribeSessionStream('session-a', vi.fn(), vi.fn(), vi.fn())
    await settle()
    expect(transport.calls.commands.filter((c) => c.url === '/v1/sessions/frames/subscribe')).toHaveLength(1)

    first.end()
    await settle()
    vi.advanceTimersByTime(1000)   // FRAMES_RECONNECT_MS
    await settle()

    expect(transport.calls.channel).toHaveLength(2)
    const subscribed = transport.calls.commands
      .filter((c) => c.url === '/v1/sessions/frames/subscribe')
      .map((c) => c.body.session_id)
    expect(subscribed).toEqual(['session-a', 'session-a'])
  })

  it('delivers frames of every session and finalizes only the finished one', async () => {
    vi.useFakeTimers()
    const channel = makeChannel()
    const transport = framesTransport([channel])
    vi.stubGlobal('fetch', transport.fetch)

    const aMessages = []
    const bMessages = []
    const aDone = vi.fn()
    const bDone = vi.fn()
    subscribeSessionStream('session-a', (m) => aMessages.push(m.content), aDone, vi.fn())
    subscribeSessionStream('session-b', (m) => bMessages.push(m.content), bDone, vi.fn())
    await settle()

    channel.push(chanInit('t'))
    channel.push(subInit('session-a'))
    channel.push(chanFrame('session-a', 1, 'a1'))
    channel.push(chanFrame('session-b', 1, 'b1'))
    channel.push(chanDone('session-a'))
    await settle()

    expect(aMessages).toEqual(['a1'])
    expect(bMessages).toEqual(['b1'])
    expect(aDone).toHaveBeenCalledOnce()
    expect(bDone).not.toHaveBeenCalled()
  })

  it('reconnects the channel once and replays each session from its own cursor', async () => {
    vi.useFakeTimers()
    const first = makeChannel()
    const second = makeChannel()
    const transport = framesTransport([first, second])
    vi.stubGlobal('fetch', transport.fetch)

    const messages = []
    subscribeSessionStream('session-a', (m) => messages.push(m.content), vi.fn(), vi.fn())
    subscribeSessionStream('session-b', (m) => messages.push(m.content), vi.fn(), vi.fn())
    await settle()

    first.push(chanInit('t'))
    first.push(chanFrame('session-a', 4, 'a4'))
    first.push(chanFrame('session-b', 7, 'b7'))
    await settle()
    first.end()

    await vi.advanceTimersByTimeAsync(1000)
    await settle()

    // One reconnect for the whole tab, not one per session.
    expect(transport.calls.channel).toHaveLength(2)
    const resumed = transport.calls.commands
      .filter((c) => c.url === '/v1/sessions/frames/subscribe')
      .map((c) => [c.body.session_id, c.body.after])
    // The first two commands are the initial subscriptions; the last two are
    // the post-reconnect resumes, each carrying its own cursor.
    expect(resumed.slice(-2)).toEqual([['session-a', 4], ['session-b', 7]])

    second.push(chanInit('t'))
    second.push(chanFrame('session-a', 5, 'a5'))
    await settle()
    expect(messages).toEqual(['a4', 'b7', 'a5'])
  })

  it('uses persisted_seq as the resume baseline when the first delivery drops', async () => {
    vi.useFakeTimers()
    const first = makeChannel()
    const second = makeChannel()
    const transport = framesTransport([first, second])
    vi.stubGlobal('fetch', transport.fetch)

    subscribeSessionStream('session-a', vi.fn(), vi.fn(), vi.fn())
    await settle()
    first.push(chanInit('t'))
    first.push(subInit('session-a', 9))
    await settle()
    first.end()

    await vi.advanceTimersByTimeAsync(1000)
    await settle()

    const resumed = transport.calls.commands.filter((c) => c.url === '/v1/sessions/frames/subscribe')
    expect(resumed.at(-1).body.after).toBe(9)
  })

  it('releases the resume cursor when the page never acknowledges frames', async () => {
    vi.useFakeTimers()
    const first = makeChannel()
    const second = makeChannel()
    const transport = framesTransport([first, second])
    vi.stubGlobal('fetch', transport.fetch)

    // A page that cannot flush its batcher (a hidden tab whose animation frames
    // are frozen, or a detached component) never invokes the acknowledge
    // callback. The frames were still delivered, so the cursor must not stay
    // pinned at the old value: every reconnect would replay the whole
    // generation, which is what made a backgrounded conversation look frozen.
    subscribeSessionStream('session-a', vi.fn(), vi.fn(), vi.fn(), vi.fn(), -1, () => {})
    await settle()
    first.push(chanInit('t'))
    first.push(subInit('session-a', 0))
    for (let seq = 1; seq <= 600; seq++) first.push(chanFrame('session-a', seq, `x${seq}`))
    for (let i = 0; i < 40; i++) await settle()
    first.end()

    await vi.advanceTimersByTimeAsync(1000)
    await settle()

    const resumed = transport.calls.commands.filter((c) => c.url === '/v1/sessions/frames/subscribe')
    expect(resumed.at(-1).body.after).toBeGreaterThanOrEqual(512)
  })

  it('closes the channel when the last watched session goes away', async () => {
    vi.useFakeTimers()
    const channel = makeChannel()
    const transport = framesTransport([channel])
    vi.stubGlobal('fetch', transport.fetch)

    const unsubscribeA = subscribeSessionStream('session-a', vi.fn(), vi.fn(), vi.fn())
    subscribeSessionStream('session-b', vi.fn(), vi.fn(), vi.fn())
    await settle()

    unsubscribeA()
    await settle()
    expect(transport.calls.commands.some((c) => c.url === '/v1/sessions/frames/unsubscribe')).toBe(true)
    expect(transport.calls.channel).toHaveLength(1)

    // The remaining session keeps the channel alive; no extra connection.
    channel.push(chanInit('t'))
    channel.push(chanFrame('session-b', 1, 'still-here'))
    await settle()
    expect(transport.calls.channel).toHaveLength(1)
  })
})

// ---------------------------------------------------------------------------
// inferStream — init 后把会话帧交接给共享通道，POST 连接立即归还
// ---------------------------------------------------------------------------

describe('inferStream (post-init handoff)', () => {
  beforeEach(() => {
    __resetSessionFramesHubForTests()
  })
  afterEach(() => {
    __resetSessionFramesHubForTests()
    vi.useRealTimers()
  })

  it('keeps the POST stream when the channel cannot take the session over', async () => {
    vi.useFakeTimers()
    const encoder = new TextEncoder()
    const postResponse = new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode('event: init\ndata: {"session_id":"session-f","stream_seq":1}\n\n'))
        controller.enqueue(encoder.encode('id: 2\ndata: {"role":"assistant","content":"carried-by-post"}\n\n'))
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } })

    // The shared channel is unusable: its connection fails outright.
    const fetchMock = vi.fn((url, opts = {}) => {
      if (url === '/v1/infer/stream') return Promise.resolve(postResponse)
      if (url.startsWith('/v1/sessions/frames')) {
        return Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({}) })
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) })
    })
    vi.stubGlobal('fetch', fetchMock)

    const messages = []
    const done = vi.fn()
    const error = vi.fn()
    inferStream(
      { messages: [] },
      (msg) => messages.push(msg.content),
      done,
      error,
    )
    await settle()
    vi.advanceTimersByTime(3000)
    await settle()

    // Releasing the POST while nobody watches the session would cancel the
    // inference (that is how "Error: user interrupted." appeared without the
    // user aborting), so the POST must keep carrying the frames itself.
    expect(messages).toEqual(['carried-by-post'])
    expect(isSessionStreamWatched('session-f')).toBe(false)
    expect(done).not.toHaveBeenCalled()
    // A channel failure is reported to the subscription, but it must not end the
    // inference the POST is still carrying.
    expect(error).not.toHaveBeenCalledWith(expect.anything())
  })

  it('releases the POST connection and keeps receiving frames on the channel', async () => {
    vi.useFakeTimers()
    const channel = makeChannel()
    const transport = framesTransport([channel])
    const encoder = new TextEncoder()
    const postResponse = new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode('event: init\ndata: {"session_id":"session-h","stream_seq":2}\n\n'))
        // The client is expected to abort right after init; keep the stream
        // open so a plain EOF cannot be mistaken for the handoff.
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } })

    const fetchMock = vi.fn((url, opts = {}) => {
      if (url === '/v1/infer/stream') {
        expect(opts.signal).toBeTruthy()
        return Promise.resolve(postResponse)
      }
      return transport.fetch(url, opts)
    })
    vi.stubGlobal('fetch', fetchMock)

    const messages = []
    const done = vi.fn()
    const error = vi.fn()
    const init = vi.fn()
    const sequences = []
    inferStream(
      { messages: [] },
      (msg, eventName) => messages.push([msg.content, eventName]),
      done,
      error,
      init,
      (seq) => sequences.push(seq),
    )
    await settle()

    expect(init).toHaveBeenCalledWith({ session_id: 'session-h', stream_seq: 2 })
    // The session is now watched on the tab's single channel…
    const subscribed = transport.calls.commands.filter((c) => c.url === '/v1/sessions/frames/subscribe')
    expect(subscribed.map((c) => [c.body.session_id, c.body.after])).toEqual([['session-h', 2]])
    // …and the POST connection is gone: no frames are delivered from it.
    expect(transport.calls.channel).toHaveLength(1)

    channel.push(chanInit('t'))
    channel.push(chanFrame('session-h', 3, 'via-channel'))
    await settle()
    expect(messages).toEqual([['via-channel', 'message']])
    // onSequence only reports what the POST itself published; from here on the
    // channel owns the per-session resume cursor (see the reconnect test).
    expect(sequences).toEqual([2])
    expect(error).not.toHaveBeenCalled()
    expect(done).not.toHaveBeenCalled()

    channel.push(chanDone('session-h'))
    await settle()
    expect(done).toHaveBeenCalledOnce()
  })

  it("hands a refusal's reason to onError, not just its text", async () => {
    // A second answer for one client-side call is refused with a structured
    // body. The page must be able to tell that deliberate refusal from a
    // dropped connection: one call has exactly one result, so the answer this
    // tab posted is not part of the conversation and its row has to give way.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: () => Promise.resolve({
        error: 'Client-side tool call(s) already answered in this session: call_c1',
        code: 'client_tool_already_answered',
        answered_call_ids: ['call_c1'],
      }),
    }))

    const error = vi.fn()
    inferStream({ messages: [] }, vi.fn(), vi.fn(), error)
    await settle()

    expect(error).toHaveBeenCalledOnce()
    const [err] = error.mock.calls[0]
    expect(err.status).toBe(409)
    expect(err.name).not.toBe('AbortError')
    expect(err.payload.code).toBe('client_tool_already_answered')
    expect(err.payload.answered_call_ids).toEqual(['call_c1'])
  })
})

// ---------------------------------------------------------------------------
// subscribeSessionEvents — 每 tab 单条共享 SSE 连接（hub）
// ---------------------------------------------------------------------------
// 背景：浏览器对同一 origin 的 HTTP/1.1 并发连接上限是 6 条。旧实现里
// Sidebar / ChatPage / AuthSettingsPage 各自开一条永不释放的
// /v1/sessions/events 长连接，3 个以上 tab 快速打开时连接池占满，
// 其余请求全部排队"卡死"。hub 保证同 tab 只有一条连接，并保留
// "每个订阅者都能拿到 init 快照"的语义。


function sseEventStream(chunks) {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

describe('subscribeSessionEvents (shared hub)', () => {
  beforeEach(() => {
    __resetSessionEventsHubForTests()
  })

  it('多个订阅者共享一条 SSE 连接，全部取消订阅后才断开', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      sseEventStream(['data: {"event":"init","sessions":{"s1":"streaming"},"titles":{}}\n\n']))
    )
    const seen1 = []
    const seen2 = []
    const un1 = subscribeSessionEvents((e) => seen1.push(e), () => {})
    const un2 = subscribeSessionEvents((e) => seen2.push(e), () => {})
    await flushPromises()

    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch.mock.calls[0][0]).toBe('/v1/sessions/events')
    expect(seen1[0].event).toBe('init')
    expect(seen2[0].event).toBe('init')

    un1()
    // 还有一位订阅者：不应重连，也不应断开
    await flushPromises()
    un2()
    await flushPromises()
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('晚到的订阅者先收到 init 快照及缓冲事件（与独立连接语义一致）', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      sseEventStream([
        'data: {"event":"init","sessions":{"s1":"streaming"},"titles":{}}\n\n',
        'data: {"event":"message","session_id":"s1","status":"done_success"}\n\n',
      ])
    ))
    const first = []
    const un1 = subscribeSessionEvents((e) => first.push(e), () => {})
    await flushPromises()

    const late = []
    const un2 = subscribeSessionEvents((e) => late.push(e), () => {})
    expect(late.map((e) => e.event)).toEqual(['init', 'message'])
    expect(late[0].sessions).toEqual({ s1: 'streaming' })
    un1(); un2()
  })

  it('订阅时连接尚未到达 init：缓冲事件在 init 后按序补发', async () => {
    let resolveFetch
    const fetchMock = vi.fn().mockImplementation(
      () => new Promise((resolve) => { resolveFetch = () => resolve(sseEventStream([
        'data: {"event":"init","sessions":{},"titles":{}}\n\n',
        'data: {"event":"message","session_id":"a","status":"streaming"}\n\n',
      ])) }))
    vi.stubGlobal('fetch', fetchMock)

    const late = []
    const un2 = subscribeSessionEvents((e) => late.push(e), () => {})
    await flushPromises()
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(late).toEqual([])

    resolveFetch()
    await flushPromises()
    expect(late.map((e) => [e.event, e.session_id ?? null])).toEqual([
      ['init', null],
      ['message', 'a'],
    ])
    un2()
  })

  it('流异常后延迟 1 秒重连，事件继续送达', async () => {
    vi.useFakeTimers()
    const first = sseEventStream(['data: {"event":"init","sessions":{},"titles":{}}\n\n'])
    const second = sseEventStream([
      'data: {"event":"init","sessions":{},"titles":{}}\n\n',
      'data: {"event":"message","session_id":"b","status":"streaming"}\n\n',
    ])
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(second))
    const events = []
    const un = subscribeSessionEvents((e) => events.push(e), () => {})
    await flushPromises()

    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    expect(fetch).toHaveBeenCalledTimes(2)
    expect(events.some((e) => e.event === 'message' && e.session_id === 'b')).toBe(true)
    un()
    vi.useRealTimers()
  })

  it('监听器抛错不影响 hub 及其他监听者', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      sseEventStream(['data: {"event":"init","sessions":{},"titles":{}}\n\n'])
    ))
    const good = []
    const un1 = subscribeSessionEvents(() => { throw new Error('boom') }, () => {})
    const un2 = subscribeSessionEvents((e) => good.push(e), () => {})
    await flushPromises()

    expect(good.length).toBe(1)
    expect(good[0].event).toBe('init')
    un1(); un2()
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

  it('adds category when listing a terminal directory node', async () => {
    vi.stubGlobal('fetch', mockFetch({ sessions: [] }))

    await sessions.list(2, 25, '1/2/3')

    expect(fetch.mock.calls[0][0]).toBe('/v1/sessions?page=2&page_size=25&category=1%2F2%2F3')
  })

  it('fetches the session category tree', async () => {
    const responseData = { version: 1, tree: [] }
    vi.stubGlobal('fetch', mockFetch(responseData))

    await expect(sessions.tree()).resolves.toEqual(responseData)
    expect(fetch.mock.calls[0][0]).toBe('/v1/sessions/tree')
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


describe('sessions.fork', () => {
  it('posts the anchor index and timestamp to the fork endpoint', async () => {
    vi.stubGlobal('fetch', mockFetch({
      status: 'success',
      session_id: 'session-2',
      parent_session_id: 'session-1',
      title: 'parent title',
      message_count: 4,
      cut_index: 3,
    }))

    const result = await sessions.fork('session-1', { messageIndex: 3, timestamp: '2026-05-13T10:00:00' })

    expect(fetch).toHaveBeenCalledOnce()
    const [url, opts] = fetch.mock.calls[0]
    expect(url).toBe('/v1/sessions/session-1/fork')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ message_index: 3, timestamp: '2026-05-13T10:00:00' })
    expect(result.session_id).toBe('session-2')
    expect(result.parent_session_id).toBe('session-1')
  })

  it('encodes the session id and tolerates a missing anchor timestamp', async () => {
    vi.stubGlobal('fetch', mockFetch({ session_id: 'session-2' }))

    await sessions.fork('session/1')

    const [url, opts] = fetch.mock.calls[0]
    expect(url).toBe('/v1/sessions/session%2F1/fork')
    expect(JSON.parse(opts.body)).toEqual({ message_index: null, timestamp: '' })
  })

  it('surfaces the backend error message (e.g. session inferring)', async () => {
    vi.stubGlobal('fetch', mockFetch({ error: 'Session is inferring, fork is not allowed now: session-1' }, 409))

    try {
      await sessions.fork('session-1', { messageIndex: 1 })
      throw new Error('Expected fork to fail')
    } catch (err) {
      expect(err.message).toBe('Session is inferring, fork is not allowed now: session-1')
      expect(err.status).toBe(409)
    }
  })
})


// ---------------------------------------------------------------------------
// remoteEnv — remote environment management (远程环境管理)
// ---------------------------------------------------------------------------

describe('remoteEnv', () => {
  it('list: GET /v1/remote-envs', async () => {
    vi.stubGlobal('fetch', mockFetch({ envs: [] }))

    const result = await remoteEnv.list()

    const [url, opts] = fetch.mock.calls[0]
    expect(url).toBe('/v1/remote-envs')
    expect(opts.method).toBe('GET')
    expect(result).toEqual({ envs: [] })
  })

  it('add: POST /v1/remote-envs with the url only (backend probes hello)', async () => {
    vi.stubGlobal('fetch', mockFetch({ envs: [] }))

    await remoteEnv.add('http://172.28.70.13:7988/v1/setup?token=tok')

    const [url, opts] = fetch.mock.calls[0]
    expect(url).toBe('/v1/remote-envs')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({
      url: 'http://172.28.70.13:7988/v1/setup?token=tok',
    })
  })

  it('updateSnapshot: PUT /v1/remote-envs/{id} with encoded id', async () => {
    vi.stubGlobal('fetch', mockFetch({ envs: [] }))

    await remoteEnv.updateSnapshot('http://172.28.70.13:7988', { backend_build: 'b1' })

    const [url, opts] = fetch.mock.calls[0]
    expect(url).toBe('/v1/remote-envs/' + encodeURIComponent('http://172.28.70.13:7988'))
    expect(opts.method).toBe('PUT')
    expect(JSON.parse(opts.body)).toEqual({ snapshot: { backend_build: 'b1' } })
  })

  it('pushUpdate: POST /v1/remote-envs/{id}/push-update with no body', async () => {
    vi.stubGlobal('fetch', mockFetch({
      ok: true,
      updated: true,
      restart_backend: false,
      method: 'push',
      remote: { backend_build: 'b2' },
    }))

    const result = await remoteEnv.pushUpdate('http://172.28.70.13:7988')

    const [url, opts] = fetch.mock.calls[0]
    expect(url).toBe('/v1/remote-envs/' + encodeURIComponent('http://172.28.70.13:7988') + '/push-update')
    expect(opts.method).toBe('POST')
    expect(opts.body).toBeUndefined()
    expect(result.updated).toBe(true)
    expect(result.method).toBe('push')
  })

  it('pushUpdate: surfaces backend error codes for i18n mapping', async () => {
    vi.stubGlobal('fetch', mockFetch(
      { error: 'child_unreachable', message: 'Cannot reach child environment: timeout' },
      502,
    ))

    let caught = null
    try {
      await remoteEnv.pushUpdate('http://172.28.70.13:7988')
    } catch (err) {
      caught = err
    }
    expect(caught).not.toBeNull()
    expect(caught.code).toBe('child_unreachable')
    expect(caught.message).toBe('Cannot reach child environment: timeout')
  })

  it('remove: DELETE /v1/remote-envs/{id}', async () => {
    vi.stubGlobal('fetch', mockFetch({ envs: [] }))

    await remoteEnv.remove('http://172.28.70.13:7988')

    const [url, opts] = fetch.mock.calls[0]
    expect(url).toBe('/v1/remote-envs/' + encodeURIComponent('http://172.28.70.13:7988'))
    expect(opts.method).toBe('DELETE')
  })

})

// ---------------------------------------------------------------------------
// remoteEnvHomeUrl / isRemoteLogoImage / resolveRemoteEnvLogo
// ---------------------------------------------------------------------------

describe('remoteEnvHomeUrl', () => {
  it('strips the /v1/setup path and query', () => {
    expect(remoteEnvHomeUrl('http://172.28.70.13:7988/v1/setup?token=abc'))
      .toBe('http://172.28.70.13:7988/')
  })

  it('keeps the deployment prefix', () => {
    expect(remoteEnvHomeUrl('https://a.b.com:8443/sub/v1/setup/'))
      .toBe('https://a.b.com:8443/sub/')
  })

  it('falls back to the origin root without a setup path', () => {
    expect(remoteEnvHomeUrl('http://child:7988/')).toBe('http://child:7988/')
  })

  it('rejects non-URL input', () => {
    expect(() => remoteEnvHomeUrl('not a url')).toThrow()
  })

  it('drops the credential: the password never reaches the address bar', () => {
    // 登记的地址可以写成 https://user:password@host:27988/（凭据给后端拨号用），
    // 但打开首页是浏览器的事 —— 链接里不能留密码（地址栏、历史、referer 都会泄露）
    expect(remoteEnvHomeUrl('https://gw:secret@proxy.example.com:27988/v1/setup'))
      .toBe('https://proxy.example.com:27988/')
  })
})

describe('maskRemoteEnvUrl', () => {
  it('masks the password, keeps the user for context', () => {
    expect(maskRemoteEnvUrl('https://gw:secret@proxy.example.com:27988/v1/setup'))
      .toBe('https://gw:***@proxy.example.com:27988/v1/setup')
  })

  it('returns credential-free URLs unchanged', () => {
    expect(maskRemoteEnvUrl('http://172.28.70.13:7988/v1/setup?token=tok'))
      .toBe('http://172.28.70.13:7988/v1/setup?token=tok')
  })

  it('tolerates empty and unparseable input', () => {
    expect(maskRemoteEnvUrl('')).toBe('')
    expect(maskRemoteEnvUrl(null)).toBe('')
    expect(maskRemoteEnvUrl('not a url')).toBe('not a url')
  })
})

describe('isRemoteLogoImage', () => {
  it.each([
    '/logo.png',
    'logo.png',
    'https://cdn.example.com/logo.svg',
    '//cdn.example.com/logo.svg',
    'data:image/png;base64,AAA',
    'logo.png?v=1',
  ])('treats %s as an image', (value) => {
    expect(isRemoteLogoImage(value)).toBe(true)
  })

  it.each(['', '   ', '🤖', 'Some App Name'])('treats %s as not an image', (value) => {
    expect(isRemoteLogoImage(value)).toBe(false)
  })
})

describe('resolveRemoteEnvLogo', () => {
  it('keeps absolute URLs and data URIs as-is', () => {
    expect(resolveRemoteEnvLogo('http://child:7988', 'https://cdn.example.com/l.png'))
      .toBe('https://cdn.example.com/l.png')
    expect(resolveRemoteEnvLogo('http://child:7988', 'data:image/png;base64,AAA'))
      .toBe('data:image/png;base64,AAA')
  })

  it('routes /-prefixed logos through the same-origin env-proxy bridge', () => {
    expect(resolveRemoteEnvLogo('http://child:7988', '/logo.png', 'http://parent:7988'))
      .toBe('http://parent:7988/v1/env-proxy/http%3A%2F%2Fchild%3A7988/logo.png')
    expect(resolveRemoteEnvLogo('https://a.b.com:8443', '/sub/logo.png', 'http://parent:7988'))
      .toBe('http://parent:7988/v1/env-proxy/https%3A%2F%2Fa.b.com%3A8443/sub/logo.png')
  })

  it('routes relative logos through the bridge and encodes tunnel ids', () => {
    expect(resolveRemoteEnvLogo('http://child:7988', 'logo.png', 'http://parent:7988'))
      .toBe('http://parent:7988/v1/env-proxy/http%3A%2F%2Fchild%3A7988/logo.png')
    expect(resolveRemoteEnvLogo('tunnel:0123456789abcdef', '/l.png', 'http://parent:7988'))
      .toBe('http://parent:7988/v1/env-proxy/tunnel%3A0123456789abcdef/l.png')
  })

  it('returns an empty string for a missing logo', () => {
    expect(resolveRemoteEnvLogo('http://child:7988', '')).toBe('')
    expect(resolveRemoteEnvLogo('http://child:7988', undefined)).toBe('')
  })
})

// ---------------------------------------------------------------------------
// build.checkSource — server-side update-source probe (op=source-hello)
// ---------------------------------------------------------------------------

describe('build.checkSource', () => {
  it('GETs /v1/setup with op=source-hello and an encoded source', async () => {
    vi.stubGlobal('fetch', mockFetch({ remote: {}, local: {} }))

    const result = await build.checkSource('http://parent:7988/v1/setup?token=p')

    const [url] = fetch.mock.calls[0]
    const parsed = new URL('http://localhost' + url)
    expect(parsed.pathname).toBe('/v1/setup')
    expect(parsed.searchParams.get('op')).toBe('source-hello')
    expect(parsed.searchParams.get('source')).toBe('http://parent:7988/v1/setup?token=p')
    expect(result).toEqual({ remote: {}, local: {} })
  })
})
