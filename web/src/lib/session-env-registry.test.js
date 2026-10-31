import { describe, it, expect, beforeEach } from 'vitest'
import {
  rememberSessionEnv,
  rememberSessionWorkspace,
  forgetSession,
  sessionEnvId,
  sessionWorkspace,
  resolveTerminalEnvId,
} from './session-env-registry.svelte.js'

// The bug this pins: a terminal belongs to the environment of **its own**
// session. Keying off the current session's binding silently opened background
// terminals as local channels — a parent-side PTY for a session whose tools run
// in a child environment.
describe('session env registry', () => {
  beforeEach(() => {
    for (const sid of ['s1', 's2', 's3']) forgetSession(sid)
  })

  it('a background session keeps its own environment', () => {
    rememberSessionEnv('s1', 'http://child.example:7988')
    // The user has since switched to s2, which runs locally.
    expect(resolveTerminalEnvId('s1', 's2', '')).toBe('http://child.example:7988')
    expect(resolveTerminalEnvId('s2', 's2', '')).toBe('')
  })

  it('the current session follows the environment selector', () => {
    // Restore sets the selector synchronously; the registry is filled by an
    // async fetch, so for the displayed session the selector is authoritative.
    rememberSessionEnv('s1', 'http://child.example:7988')
    expect(resolveTerminalEnvId('s1', 's1', '')).toBe('')
    expect(resolveTerminalEnvId('s1', 's1', 'http://other:7988')).toBe('http://other:7988')
  })

  it('unknown or deleted sessions resolve to local', () => {
    rememberSessionEnv('s1', 'tunnel:node-a')
    expect(resolveTerminalEnvId('s2', 's1', '')).toBe('')
    forgetSession('s1')
    expect(sessionEnvId('s1')).toBe('')
    expect(resolveTerminalEnvId('s1', 's2', '')).toBe('')
  })

  it('empty session id is local, never a lookup on undefined', () => {
    rememberSessionEnv('s1', 'env-a')
    expect(resolveTerminalEnvId('', 's1', 'env-b')).toBe('')
    expect(resolveTerminalEnvId(null, null, 'env-b')).toBe('')
  })

  it('remembering "" clears the binding', () => {
    rememberSessionEnv('s1', 'env-a')
    rememberSessionEnv('s1', '')
    expect(sessionEnvId('s1')).toBe('')
  })

  it('tracks the workspace per session and clears it with forgetSession', () => {
    rememberSessionWorkspace('s1', '/root/remote-dir')
    expect(sessionWorkspace('s1')).toBe('/root/remote-dir')
    forgetSession('s1')
    expect(sessionWorkspace('s1')).toBe('')
    expect(sessionEnvId('s1')).toBe('')
  })

  it('ignores writes without a session id', () => {
    rememberSessionEnv(null, 'env-a')
    rememberSessionWorkspace(undefined, '/tmp')
    expect(sessionEnvId('s1')).toBe('')
    expect(sessionWorkspace('s1')).toBe('')
    expect(sessionEnvId(null)).toBe('')
  })
})
