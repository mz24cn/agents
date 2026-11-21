import { describe, it, expect } from 'vitest'
import { isTerminalStatus, reconcileStatusSnapshot } from './session-status-snapshot.js'

describe('session status snapshot', () => {
  it('drops a session that finished while this tab was not listening', () => {
    // The bug this replaces: the set only ever grew, so a run that ended while the
    // events stream was down kept looking streaming -- and the page kept re-arming
    // a frame watch on a generation that no longer existed.
    const believed = new Set(['gone', 'still-running'])

    const snapshot = reconcileStatusSnapshot(believed, {
      'still-running': 'streaming',
      gone: 'done_success_unread',
      other: 'done_error_unread',
    })

    expect([...snapshot.streaming]).toEqual(['still-running'])
    expect(snapshot.ended).toEqual(['gone'])
  })

  it('names only the sessions this page has a stale view of', () => {
    // A conversation the page never saw streaming has nothing to correct; the
    // caller settles flags and half-finished compression markers for the rest.
    const snapshot = reconcileStatusSnapshot(new Set(['never-watched-locally']), {
      'never-watched-locally': 'done_success_unread',
    })

    expect(snapshot.streaming.size).toBe(0)
    expect(snapshot.ended).toEqual(['never-watched-locally'])
    // (It was believed streaming, which is exactly why it is named.)
    const untouched = reconcileStatusSnapshot(new Set(), { a: 'done_error_unread' })
    expect(untouched.ended).toEqual([])
  })

  it('keeps a run this tab is itself carrying over its own POST', () => {
    // The snapshot can be taken in the beat between starting a run here and the
    // backend having registered its status; the POST connection is proof enough.
    const snapshot = reconcileStatusSnapshot(
      new Set(['started-here']),
      { 'started-here': 'idle', elsewhere: 'streaming' },
      new Set(['started-here']),
    )

    expect([...snapshot.streaming].sort()).toEqual(['elsewhere', 'started-here'])
    expect(snapshot.ended).toEqual([])
  })

  it('reports the resting sessions as a complete set of their own', () => {
    const snapshot = reconcileStatusSnapshot(new Set(['was-pausing']), {
      'was-pausing': 'paused',
      finished: 'done_success_unread',
      running: 'streaming',
    })

    expect([...snapshot.paused]).toEqual(['was-pausing'])
    expect([...snapshot.streaming]).toEqual(['running'])
  })

  it('reads an empty or missing snapshot as nothing running', () => {
    expect(reconcileStatusSnapshot(new Set(['a']), {}).streaming.size).toBe(0)
    expect(reconcileStatusSnapshot(new Set(['a']), undefined).streaming.size).toBe(0)
  })

  it('recognises both ways a generation can end', () => {
    expect(isTerminalStatus('done_success_unread')).toBe(true)
    expect(isTerminalStatus('done_error_unread')).toBe(true)
    expect(isTerminalStatus('streaming')).toBe(false)
    expect(isTerminalStatus('paused')).toBe(false)
    expect(isTerminalStatus(undefined)).toBe(false)
  })
})
