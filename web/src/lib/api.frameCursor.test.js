// @vitest-environment jsdom
// Reading the frame channel is idempotent, in order, and self-repairing -- and
// when it cannot repair itself it says so instead of showing what it has.
//
// Frames of one session arrive on the tab's channel in increasing sequence order,
// and the backend keeps every one of them retained. When its per-tab queue is over
// full it discards frames and says so in its log -- the run is unaffected, but
// what this tab has been told now has a hole in it. The page notices (the sequence
// jumped), asks for the gap the same way a reconnect does (re-subscribe from the
// cursor), and never applies a frame sitting above a hole: appending text over a
// gap is how 43,671 silently discarded frames turned into a conversation with
// paragraphs eaten -- which is what this file exists to keep fixed.
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
    // 10 sits above a hole: applying it now would paste text under an answer that
    // is missing the part before it. It waits, unseen, for the repair.
    channel.push(frame('session-a', 10, 'f10'))
    await settle()
    expect(appliedContents(onMessage)).toEqual(['f8'])

    // The replay that answers the repair carries everything from the cursor: 9,
    // then 10 again, then on. Only 9 and 10 are news, and they arrive in order.
    channel.push(frame('session-a', 9, 'f9'))
    channel.push(frame('session-a', 10, 'f10'))
    channel.push(frame('session-a', 11, 'f11'))
    await settle()

    expect(appliedContents(onMessage)).toEqual(['f8', 'f9', 'f10', 'f11'])
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

  it('a frame covering a run of sequences is not read as a hole', async () => {
    // The channel coalesces consecutive deltas: one frame stands for 9..11 and
    // says so. Without the range the page would see a jump from 8 to 11 and go
    // fetching frames it has already been given.
    const channel = makeChannel()
    const transport = framesTransport(channel)
    vi.stubGlobal('fetch', transport.fetch)
    const onMessage = vi.fn()

    await watching(transport, channel, onMessage)
    channel.push(`event: frame\ndata: {"session_id":"session-a","seq":11,"covers_from":9,`
      + `"event":"message","frame":{"role":"assistant","content":"f9-11"}}\n\n`)
    channel.push(frame('session-a', 12, 'f12'))
    await settle()

    expect(appliedContents(onMessage)).toEqual(['f8', 'f9-11', 'f12'])
    expect(subscribes(transport.calls)).toHaveLength(1)   // no repair was asked for
  })

  it('asks for the next chunk of a replay the backend truncated', async () => {
    const channel = makeChannel()
    const transport = framesTransport(channel)
    vi.stubGlobal('fetch', transport.fetch)
    const onMessage = vi.fn()
    subscribeSessionStream('session-a', onMessage, vi.fn(), vi.fn(), null, 7)
    await settle()
    channel.push(chanInit('t'))
    // sub_init says: here are frames 8 and 9, and 40 more are still held back.
    channel.push(`event: sub_init\ndata: {"session_id":"session-a","persisted_seq":7,`
      + `"latest_seq":49,"replayed":2,"replayed_through":9,"replay_pending":40}\n\n`)
    await settle()
    channel.push(frame('session-a', 8, 'f8'))
    channel.push(frame('session-a', 9, 'f9'))
    await settle()

    // Applying the chunk moved the cursor, so the page pulls the next one from
    // where it got to -- the retained backlog is fetched in steps, never dumped
    // into one delivery.
    const asked = subscribes(transport.calls)
    expect(asked[0].body).toMatchObject({ session_id: 'session-a', after: 7 })
    expect(asked.length).toBeGreaterThan(1)
    const pulled = asked.at(-1).body.after
    expect(pulled).toBeGreaterThan(7)      // the cursor moved: progress, not a loop
    expect(pulled).toBeLessThan(10)        // and never past what this tab applied

    // And a chunk nobody moved past is not asked for again.
    const asks = subscribes(transport.calls).length
    await settle()
    await settle()
    expect(subscribes(transport.calls)).toHaveLength(asks)
  })

  it('hands the conversation to the page when a hole cannot be repaired', async () => {
    const channel = makeChannel()
    const transport = framesTransport(channel)
    vi.stubGlobal('fetch', transport.fetch)
    const onMessage = vi.fn()
    const onDegrade = vi.fn()
    subscribeSessionStream('session-a', onMessage, vi.fn(), vi.fn(), null, 7, null, onDegrade)
    await settle()
    channel.push(chanInit('t'))
    channel.push(subInit('session-a', 7))
    await settle()
    channel.push(frame('session-a', 8, 'f8'))
    await settle()

    // Nothing arrives to fill the holes: the page asks a bounded number of times,
    // and then says the view it has is not trustworthy instead of going on
    // appending to it. The frames beyond a hole were never applied -- that is the
    // point; a paragraph-eaten answer is what appending to a hole looks like.
    for (const seq of [10, 12, 14, 16, 18]) channel.push(frame('session-a', seq, `f${seq}`))
    await settle()

    expect(subscribes(transport.calls)).toHaveLength(4)   // 1 + three repairs
    expect(appliedContents(onMessage)).toEqual(['f8'])
    expect(onDegrade).toHaveBeenCalledTimes(1)
    expect(onDegrade.mock.calls[0][0]).toMatchObject({
      sessionId: 'session-a', reason: 'gap_unrepaired', cursor: 8,
    })
  })

  it('stops replaying frames at a watch that is thousands behind its own run', async () => {
    const channel = makeChannel()
    const transport = framesTransport(channel)
    vi.stubGlobal('fetch', transport.fetch)
    const onDegrade = vi.fn()
    // This tab applied frame 8; the conversation has long since persisted past
    // 5000. Replaying ~5000 frames at it is both slow and pointless.
    subscribeSessionStream('session-a', vi.fn(), vi.fn(), vi.fn(), null, 8, null, onDegrade)
    await settle()
    channel.push(chanInit('t'))
    channel.push(subInit('session-a', 5000))
    await settle()

    expect(onDegrade).toHaveBeenCalledTimes(1)
    expect(onDegrade.mock.calls[0][0]).toMatchObject({ reason: 'behind_persisted', cursor: 8 })
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
