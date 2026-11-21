// @vitest-environment jsdom
// What a backgrounded tab keeps.
//
// Hiding a tab is not leaving it: same watches, same socket, and the generations
// it started keep running whether or not anything reads them. This page used to
// hand the frame channel back the moment it went to the background -- which gave
// back one of the browser's ~6 per-origin slots and, in exchange, left a hole in
// the middle of a live run: frames of a generation nobody had stopped, only
// replayable after a reconnect that a hidden tab had been made unwilling to open.
// So visibility decides nothing here. What releases a channel is having nothing
// left to route, and the browser saying the page is going away (pagehide) -- the
// only absence a page can actually know about.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  subscribeSessionStream,
  subscribeSessionEvents,
  __resetSessionFramesHubForTests,
  __resetSessionEventsHubForTests,
  __reconcileSessionFramesForTests,
} from './api.js'

function makeChannel() {
  const encoder = new TextEncoder()
  let controller = null
  const response = new Response(new ReadableStream({
    start(c) { controller = c },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
  return {
    response,
    // Bytes, as a socket hands them: the reader decodes, so a string here would
    // break the pump and the test would be watching a dead channel.
    push: (chunk) => controller.enqueue(
      typeof chunk === 'string' ? encoder.encode(chunk) : chunk,
    ),
    end: () => controller.close(),
  }
}

function framesTransport(channels) {
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
const subInit = (sid, persisted) =>
  `event: sub_init\ndata: {"session_id":"${sid}","persisted_seq":${persisted},"latest_seq":${persisted}}\n\n`
const frame = (sid, seq, content) =>
  `event: frame\ndata: {"session_id":"${sid}","seq":${seq},"event":"message","frame":{"role":"assistant","content":"${content}"}}\n\n`

async function settle() {
  for (let i = 0; i < 30; i++) await Promise.resolve()
}

function setVisibility(state) {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: state })
}

function commands(transport, path) {
  return transport.calls.commands.filter((c) => c.url === path)
}

describe('frame channel and page visibility', () => {
  beforeEach(() => {
    __resetSessionFramesHubForTests()
    __resetSessionEventsHubForTests()
    setVisibility('visible')
  })
  afterEach(() => {
    __resetSessionFramesHubForTests()
    __resetSessionEventsHubForTests()
    setVisibility('visible')
    vi.unstubAllGlobals()
  })

  it('keeps the channel, and keeps delivering, while the page is in the background', async () => {
    const channel = makeChannel()
    const transport = framesTransport([channel])
    vi.stubGlobal('fetch', transport.fetch)
    const onMessage = vi.fn()

    subscribeSessionStream('session-a', onMessage, vi.fn(), vi.fn(), null, 7)
    await settle()
    channel.push(chanInit('t'))
    channel.push(subInit('session-a', 7))
    await settle()
    expect(transport.calls.channel).toHaveLength(1)

    setVisibility('hidden')
    document.dispatchEvent(new Event('visibilitychange'))
    await settle()

    // The reason this whole policy exists: a run that keeps producing while the
    // tab is away has to be found complete when the user comes back, and the way
    // to get that is to keep reading it -- not to stop reading and replay later.
    channel.push(frame('session-a', 8, 'still working'))
    channel.push(frame('session-a', 9, 'and again'))
    await settle()
    expect(onMessage).toHaveBeenCalledTimes(2)

    // Nothing was announced about the page's absence, because nothing changed.
    expect(commands(transport, '/v1/sessions/frames/close')).toHaveLength(0)
  })

  it('arms a channel for a watch opened on a background page', async () => {
    const channel = makeChannel()
    const transport = framesTransport([channel])
    vi.stubGlobal('fetch', transport.fetch)

    // Opened as a background tab, or re-armed by the sweep while hidden: no
    // visibilitychange ever fires, and a run that is producing has a reader
    // waiting for it the moment the user looks -- not one that has to be
    // re-opened, re-subscribed and replayed first.
    setVisibility('hidden')
    subscribeSessionStream('session-a', vi.fn(), vi.fn(), vi.fn())
    await settle()
    for (let i = 0; i < 5; i++) {
      __reconcileSessionFramesForTests()
      await settle()
    }

    expect(transport.calls.channel).toHaveLength(1)
    expect(commands(transport, '/v1/sessions/frames/close')).toHaveLength(0)
  })

  it('does not reconnect when the page comes back, because nothing was dropped', async () => {
    const channel = makeChannel()
    const transport = framesTransport([channel])
    vi.stubGlobal('fetch', transport.fetch)

    subscribeSessionStream('session-a', vi.fn(), vi.fn(), vi.fn(), null, 7)
    await settle()
    channel.push(chanInit('t'))
    channel.push(subInit('session-a', 7))
    await settle()

    setVisibility('hidden')
    document.dispatchEvent(new Event('visibilitychange'))
    await settle()
    setVisibility('visible')
    document.dispatchEvent(new Event('visibilitychange'))
    await settle()

    // One socket for the whole trip away. A second channel request here would
    // mean the page had been cut off, and would cost a fresh subscribe plus a
    // replay of everything the tab already had.
    expect(transport.calls.channel).toHaveLength(1)
    expect(commands(transport, '/v1/sessions/frames/subscribe')).toHaveLength(1)
  })

  it('holds both long-lived streams while the tab is in the background', async () => {
    // The connection ledger, end to end: a tab watching sessions holds one frame
    // channel plus one status stream, and stays at two whether it is in the
    // foreground or not. The status stream is what tells a background tab that a
    // run it is not looking at has finished -- the sidebar has to be right before
    // the user comes back.
    const frames = makeChannel()
    const events = makeChannel()
    const calls = { frames: [], events: [], commands: [] }
    vi.stubGlobal('fetch', vi.fn((url, opts = {}) => {
      if (url.startsWith('/v1/sessions/frames?conn=')) {
        calls.frames.push(url)
        return Promise.resolve(frames.response)
      }
      if (url.startsWith('/v1/sessions/events')) {
        calls.events.push(url)
        return Promise.resolve(events.response)
      }
      if (url.startsWith('/v1/sessions/frames/')) {
        calls.commands.push({ url, body: JSON.parse(opts.body || '{}') })
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve({ ok: true, registered: true, active: true }),
        })
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) })
    }))

    const stopEvents = subscribeSessionEvents(vi.fn(), vi.fn())
    subscribeSessionStream('session-a', vi.fn(), vi.fn(), vi.fn())
    await settle()
    frames.push(chanInit('t'))
    await settle()
    expect(calls.frames).toHaveLength(1)
    expect(calls.events).toHaveLength(1)

    setVisibility('hidden')
    document.dispatchEvent(new Event('visibilitychange'))
    await settle()
    for (let i = 0; i < 3; i++) {
      __reconcileSessionFramesForTests()
      await settle()
    }

    expect(calls.frames).toHaveLength(1)
    expect(calls.events).toHaveLength(1)
    expect(calls.commands.filter((c) => c.url === '/v1/sessions/frames/close')).toHaveLength(0)
    stopEvents()
  })

  it('releases when there is nothing left to route, hidden or not', async () => {
    const channel = makeChannel()
    const transport = framesTransport([channel])
    vi.stubGlobal('fetch', transport.fetch)

    const stop = subscribeSessionStream('session-a', vi.fn(), vi.fn(), vi.fn())
    await settle()
    channel.push(chanInit('t'))
    await settle()

    // What the budget really keys on: this tab stopped routing the conversation.
    // The generations it watched keep running on the server either way.
    stop()
    setVisibility('hidden')
    document.dispatchEvent(new Event('visibilitychange'))
    __reconcileSessionFramesForTests()
    await settle()

    const closed = commands(transport, '/v1/sessions/frames/close')
    expect(closed).toHaveLength(1)
    expect(closed[0].body.reason).toBe('idle')
  })

  it('releases a page that went into the bfcache without ending its runs', async () => {
    const channel = makeChannel()
    const transport = framesTransport([channel])
    vi.stubGlobal('fetch', transport.fetch)

    subscribeSessionStream('session-a', vi.fn(), vi.fn(), vi.fn())
    await settle()
    channel.push(chanInit('t'))
    await settle()

    setVisibility('hidden')
    const event = new Event('pagehide')
    event.persisted = true
    window.dispatchEvent(event)
    await settle()

    // A restorable page is frozen, not finished -- and its run is not the page's
    // to lose either way, so bfcache and plain unload differ only in the reason.
    const closed = commands(transport, '/v1/sessions/frames/close')
    expect(closed).toHaveLength(1)
    expect(closed[0].body.reason).toBe('bfcache')
  })

  it('does not read a plain unload as the user finishing the run', async () => {
    const channel = makeChannel()
    const transport = framesTransport([channel])
    vi.stubGlobal('fetch', transport.fetch)

    subscribeSessionStream('session-a', vi.fn(), vi.fn(), vi.fn())
    await settle()
    channel.push(chanInit('t'))
    await settle()

    // Browsers fire pagehide on a reload too. Releasing the socket is correct,
    // claiming "nobody will come back" is not: the reload re-attaches seconds
    // later and must find the same generation running.
    const event = new Event('pagehide')
    event.persisted = false
    window.dispatchEvent(event)
    await settle()

    const closed = commands(transport, '/v1/sessions/frames/close')
    expect(closed).toHaveLength(1)
    // The whole payload: a release, and no claim about what should happen next.
    expect(closed[0].body).toEqual({ conn: expect.any(String), reason: 'page_unload' })
  })
})
