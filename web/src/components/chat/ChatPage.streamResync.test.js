/**
 * Wiring guard: the degrade signal has a listener, and the listener repairs the
 * view from the server rather than from the frames that failed to arrive.
 *
 * ``subscribeSessionStream`` reports that a watch has stopped being able to make
 * itself whole (frames were discarded on the way to this tab, the repair did not
 * close the hole, or the backlog under the cursor is bigger than it is worth
 * replaying). That signal is worthless if the page keeps appending to the
 * incomplete turn -- which is the shape the bug report had: a live answer with
 * paragraphs missing, while the persisted conversation was intact the whole time.
 *
 * Behaviour lives in api.js (see api.frameCursor.test.js); what is checked here
 * is the seam, because a listener that is never passed is invisible in the
 * browser: the page simply carries on rendering what partially arrived.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const source = readFileSync(join(__dirname, 'ChatPage.svelte'), 'utf8')

describe('ChatPage stream-view resync', () => {
  it('passes a degrade listener to every session-frame watch it arms', () => {
    const at = source.indexOf('unsubscribe = subscribeSessionStream(')
    expect(at, 'subscribeSessionStream call site not found').toBeGreaterThan(-1)
    const callArgs = source.slice(at, source.indexOf('\n    )\n', at))
    expect(callArgs).toMatch(/resyncStreamView\(/)
  })

  it('rebuilds the conversation from the server, not from the stalled cursor', () => {
    const fn = source.match(/async function resyncStreamView\([\s\S]*?\n {2}\}/)
    expect(fn, 'resyncStreamView not found').toBeTruthy()
    const body = fn[0]
    // The authoritative copy is the point of the repair.
    expect(body).toMatch(/sessionsApi\.get\(/)
    // The frame channel's cursor is what went stale; keeping it would replay the
    // same backlog the page just proved it cannot absorb.
    expect(body).toMatch(/directStreamLastSeq\.delete\(/)
    // And the watch has to stop before the messages are replaced, or frames still
    // in flight land on top of a conversation being rebuilt.
    expect(body.indexOf('unsubscribe?.()')).toBeLessThan(body.indexOf('sessionsApi.get('))
  })

  it('does not let the reconciler re-arm a watch the resync owns', () => {
    const rearm = source.match(/function rearmMissingStreamWatches\(\) \{[\s\S]*?\n {2}\}/)
    expect(rearm, 'rearmMissingStreamWatches not found').toBeTruthy()
    expect(rearm[0]).toMatch(/streamViewResyncInFlight\.has\(sid\)/)
  })

  it('does not resync under the tab that still owns the inference', () => {
    // The POST stream carries the frames of a locally started run; replacing the
    // store it writes into would fight it frame for frame.
    const fn = source.match(/async function resyncStreamView\([\s\S]*?\n {2}\}/)
    expect(fn[0]).toMatch(/directlyConsumedSessions\.has\(sid\)\s*\)\s*return/)
  })

  it('says something to the user instead of silently rewriting the screen', () => {
    const fn = source.match(/async function resyncStreamView\([\s\S]*?\n {2}\}/)
    expect(fn[0]).toMatch(/t\('streamViewResync'\)/)
    const i18n = readFileSync(join(__dirname, '..', '..', 'lib', 'i18n.svelte.js'), 'utf8')
    expect(i18n).toMatch(/streamViewResync:\s*'[^']+'/)
  })
})
