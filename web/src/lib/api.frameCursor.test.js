// @vitest-environment jsdom
// Reading the frame channel is idempotent, and a hole in it closes itself.
//
// Frames of one session arrive on the tab's channel in increasing sequence order,
// and the backend keeps every one of them retained. When its per-tab queue is over
// full it drops a live frame and says so in its log -- the run is unaffected, but
// what this tab has been told now has a hole in it. The page notices (the sequence
// jumped), asks for the gap the same way a reconnect does (re-subscribe from the
// cursor), and skips whatever it already has. Without that, the 43,671
// "frame_channel queue_full" lines of one afternoon were silently invisible: the
// conversation on screen just quietly lacked pieces.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  subscribeSessionStream,
  __resetSessionFramesHubForTests,
} from './api.js'

function makeChannel() {
  let controller = null
  const encoder = new TextEncoder()
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

function framesTransport(channel) {
  const calls = { channel: [], commands: [] }
  const fetch = vi.fn((url, opts = {}) => {
    if (url.startsWith('/v1/sessions/frames?conn=')) {
      calls.channel.push(url)
      return Promise.resolve(channel.response)
    }
    if (url.startsWith('/v1/sessions/frames/')) {
      calls.commands.push({ url, body: JSON.parse(opts.body || '{}') })
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ ok: true, registered: true, active: true }),
      })
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) })
  })
  return { calls, fetch }
}

const chanInit = (conn) => `event: chan_init\ndata: {"conn":"${conn}","generation":1}\n\n`
const subInit = (sid, persisted, latest) =>
  `event: sub_init\ndata: {"session_id":"${sid}","persisted_seq":${persisted},"latest_seq":${latest ?? persisted}}\n\n`
const frame = (sid, seq, content) =>
  `event: frame\nid: ${seq}\ndata: {"session_id":"${sid}","seq":${seq},"event":"message",`
  + `"frame":{"role":"assistant","content":"${content}"}}\n\n`

async function settle() {
  for (let i = 0; i < 30; i++) await Promise.resolve()
}

function appliedContents(spy) {
  return spy.mock.calls.map(([payload]) => payload?.content)
}

function subscribes(calls) {
  return calls.commands.filter((c) => c.url === '/v1/sessions/frames/subscribe')
}

describe('frame cursor: holes, replays and duplicates', () => {
  beforeEach(() => {
    __resetSessionFramesHubForTests()
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  })
  afterEach(() => {
    __resetSessionFramesHubForTests()
    vi.unstubAllGlobals()
  })

  async function watching(transport, channel, onMessage) {
    subscribeSessionStream('session-a', onMessage, vi.fn(), vi.fn(), null, 7)
    await settle()
    channel.push(chanInit('t'))
    channel.push(subInit('session-a', 7))
    await settle()
    channel.push(frame('session-a', 8, 'f8'))
    await settle()
  }

  it('a jump in the sequence asks for the gap it actually has', async () => {
    const channel = makeChannel()
    const transport = framesTransport(channel)
    vi.stubGlobal('fetch', transport.fetch)
    const onMessage = vi.fn()

    await watching(transport, channel, onMessage)

    // 9 never arrived: the backend dropped it because this tab was reading too
    // slowly. Frame 10 is still real content, so it is kept -- and the gap under
    // it is asked for.
    channel.push(frame('session-a', 10, 'f10'))
    await settle()

    const asked = subscribes(transport.calls)
    expect(asked).toHaveLength(2)
    // The point of the whole exchange: ask from where this tab is complete, not
    // from the highest frame it happened to see -- otherwise the hole is skipped
    // over and lost from the answer for good.
    expect(asked[0].body).toMatchObject({ session_id: 'session-a', after: 7 })
    expect(asked[1].body).toMatchObject({ session_id: 'session-a', after: 8 })
  })

  it('fills the hole without applying what it already showed', async () => {
    const channel = makeChannel()
    const transport = framesTransport(channel)
    vi.stubGlobal('fetch', transport.fetch)
    const onMessage = vi.fn()

    await watching(transport, channel, onMessage)
    channel.push(frame('session-a', 10, 'f10'))
    await settle()

    // The replay that answers the repair carries everything from the cursor: 9,
    // then 10 again, then on. Only 9 is news.
    channel.push(frame('session-a', 9, 'f9'))
    channel.push(frame('session-a', 10, 'f10'))
    channel.push(frame('session-a', 11, 'f11'))
    await settle()

    expect(appliedContents(onMessage)).toEqual(['f8', 'f10', 'f9', 'f11'])
  })

  it('costs nothing when the frames are contiguous', async () => {
    const channel = makeChannel()
    const transport = framesTransport(channel)
    vi.stubGlobal('fetch', transport.fetch)
    const onMessage = vi.fn()

    await watching(transport, channel, onMessage)
    for (const seq of [9, 10, 11]) channel.push(frame('session-a', seq, `f${seq}`))
    await settle()

    expect(appliedContents(onMessage)).toEqual(['f8', 'f9', 'f10', 'f11'])
    expect(subscribes(transport.calls)).toHaveLength(1)
  })

  it('gives up on a hole the backend can no longer replay, without stopping', async () => {
    const channel = makeChannel()
    const transport = framesTransport(channel)
    vi.stubGlobal('fetch', transport.fetch)
    const onMessage = vi.fn()

    await watching(transport, channel, onMessage)

    // Nothing arrives to fill the holes (the frames were trimmed out of the
    // backend's retention buffer): the page asks a bounded number of times and
    // then gets on with the run instead of re-subscribing forever.
    for (const seq of [10, 12, 14, 16, 18]) channel.push(frame('session-a', seq, `f${seq}`))
    await settle()

    expect(subscribes(transport.calls)).toHaveLength(4)   // 1 + three repairs
    // Still delivering: giving up on the holes is not giving up on the session.
    expect(appliedContents(onMessage)).toEqual(['f8', 'f10', 'f12', 'f14', 'f16', 'f18'])
  })

  it('a frame the page has already been told is never applied twice', async () => {
    const channel = makeChannel()
    const transport = framesTransport(channel)
    vi.stubGlobal('fetch', transport.fetch)
    const onMessage = vi.fn()

    await watching(transport, channel, onMessage)

    // A re-subscribe that overlaps what already arrived -- the repair replay, or a
    // watch re-armed while the old one was still delivering.
    channel.push(frame('session-a', 9, 'f9'))
    channel.push(frame('session-a', 8, 'f8'))
    channel.push(frame('session-a', 9, 'f9'))
    await settle()

    expect(appliedContents(onMessage)).toEqual(['f8', 'f9'])
  })
})
