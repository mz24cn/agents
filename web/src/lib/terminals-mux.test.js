/**
 * Tests for the shared terminal socket (web/src/lib/terminals-mux.js).
 *
 * The point of the hub is the connection ledger: a browser gives one origin
 * ~6 HTTP/1.1 connections and a WebSocket holds one of them, so one socket per
 * terminal means a few terminals and every ordinary request queues behind them
 * (docs/plan-connection-budget.md).  These tests pin the ledger and the routing
 * that makes sharing safe.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { openTerminalChannel, __resetTerminalHubsForTests } from './terminals-mux.js'

class FakeWebSocket {
  static instances = []
  static OPEN = 1
  static CLOSED = 3

  constructor(url) {
    this.url = url
    this.readyState = 0
    this.sent = []
    FakeWebSocket.instances.push(this)
  }

  send(data) { this.sent.push(JSON.parse(data)) }

  close() {
    this.readyState = FakeWebSocket.CLOSED
    this.onclose?.()
  }

  // test-side triggers
  accept() { this.readyState = FakeWebSocket.OPEN; this.onopen?.() }
  emit(msg) { this.onmessage?.({ data: JSON.stringify(msg) }) }
  drop() { this.readyState = FakeWebSocket.CLOSED; this.onclose?.() }
  ops() { return this.sent.filter((m) => m.op) }
  lastOp() { return this.ops().at(-1) }
}

const LOCAL_URL = 'ws://localhost:7988/v1/terminals/ws'

function open(terminalId, extra = {}) {
  const statuses = []
  const chunks = []
  const handle = openTerminalChannel({
    url: LOCAL_URL,
    terminalId,
    cols: 100,
    rows: 30,
    onData: (c) => chunks.push(c),
    onStatus: (s) => statuses.push(s),
    ...extra,
  })
  return { handle, statuses, chunks }
}

beforeEach(() => {
  FakeWebSocket.instances = []
  vi.stubGlobal('WebSocket', FakeWebSocket)
  vi.useFakeTimers()
})

afterEach(() => {
  __resetTerminalHubsForTests()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('terminal mux hub', () => {
  it('puts every terminal of the tab on one socket', () => {
    open('sess-a')
    open('sess-b')
    open('sess-c')

    expect(FakeWebSocket.instances).toHaveLength(1)
    const ws = FakeWebSocket.instances[0]
    ws.accept()

    expect(ws.sent.map((m) => [m.op, m.c])).toEqual([
      ['open', 'sess-a'],
      ['open', 'sess-b'],
      ['open', 'sess-c'],
    ])
    expect(ws.sent[0]).toMatchObject({ cols: 100, rows: 30 })
  })

  it('routes output to the channel named by the frame', () => {
    const a = open('sess-a')
    const b = open('sess-b')
    const ws = FakeWebSocket.instances[0]
    ws.accept()
    ws.emit({ op: 'opened', c: 'sess-a' })
    ws.emit({ c: 'sess-b', d: 'beta out' })
    ws.emit({ c: 'sess-a', d: 'alpha out' })

    expect(a.chunks).toEqual(['alpha out'])
    expect(b.chunks).toEqual(['beta out'])
    expect(a.statuses.at(-1)).toMatchObject({ connected: true })
  })

  it('drops frames for channels that are already gone', () => {
    const a = open('sess-a')
    const ws = FakeWebSocket.instances[0]
    ws.accept()
    a.handle.close()
    ws.emit({ c: 'sess-a', d: 'late' })

    expect(a.chunks).toEqual([])
  })

  it('keeps the socket while other terminals remain, releases it when none do', () => {
    const a = open('sess-a')
    const b = open('sess-b')
    const ws = FakeWebSocket.instances[0]
    ws.accept()

    a.handle.close()
    expect(ws.readyState).toBe(FakeWebSocket.OPEN)
    expect(b.handle.isOpen()).toBe(true)
    expect(ws.lastOp()).toEqual({ op: 'close', c: 'sess-a' })

    b.handle.close()
    expect(ws.readyState).toBe(FakeWebSocket.CLOSED)
    expect(FakeWebSocket.instances).toHaveLength(1)

    // A later terminal dials a fresh socket.
    open('sess-c')
    expect(FakeWebSocket.instances).toHaveLength(2)
  })

  it('keeps hidden terminals attached and re-opens them after a socket loss', () => {
    const a = open('sess-a')
    const b = open('sess-b')
    const ws = FakeWebSocket.instances[0]
    ws.accept()

    // Hiding a terminal never closes its channel: the PTY prints nothing back
    // on re-attach, so the live stream is the only history.
    expect(ws.ops().filter((m) => m.op === 'close')).toEqual([])

    ws.drop()
    expect(a.statuses.at(-1)).toMatchObject({ connected: false, loading: true })

    vi.advanceTimersByTime(1000)
    expect(FakeWebSocket.instances).toHaveLength(2)
    const next = FakeWebSocket.instances[1]
    next.accept()
    expect(next.sent.map((m) => [m.op, m.c])).toEqual([
      ['open', 'sess-a'],
      ['open', 'sess-b'],
    ])
    next.emit({ c: 'sess-a', d: 'resumed' })
    expect(a.chunks).toEqual(['resumed'])
  })

  it('stops retrying once the last channel is closed', () => {
    const a = open('sess-a')
    const ws = FakeWebSocket.instances[0]
    ws.accept()
    ws.drop()

    a.handle.close()
    vi.advanceTimersByTime(5000)
    expect(FakeWebSocket.instances).toHaveLength(1)
  })

  it('sends keystrokes, resizes and workspace on the shared socket', () => {
    const a = open('sess-a', { workspace: '/work/a' })
    const ws = FakeWebSocket.instances[0]
    ws.accept()

    a.handle.send('ls -l\r')
    a.handle.resize(120, 40)
    a.handle.send('\x03')

    expect(ws.ops()).toEqual([
      { op: 'open', c: 'sess-a', env_id: 'local', cols: 100, rows: 30, workspace: '/work/a' },
      { op: 'input', c: 'sess-a', d: 'ls -l\r' },
      { op: 'resize', c: 'sess-a', cols: 120, rows: 40 },
      { op: 'input', c: 'sess-a', d: '\x03' },
    ])
  })

  it('re-opens the newest channel when the same terminal is opened twice', () => {
    const first = open('sess-a')
    const ws = FakeWebSocket.instances[0]
    ws.accept()

    const second = open('sess-a')
    expect(ws.ops()).toEqual([
      { op: 'open', c: 'sess-a', env_id: 'local', cols: 100, rows: 30 },
      { op: 'close', c: 'sess-a' },
      { op: 'open', c: 'sess-a', env_id: 'local', cols: 100, rows: 30 },
    ])
    ws.emit({ c: 'sess-a', d: 'to the newest' })
    expect(second.chunks).toEqual(['to the newest'])
    expect(first.chunks).toEqual([])
    // The stale handle is inert: closing it must not detach the new channel.
    first.handle.close()
    expect(ws.lastOp()).toEqual({ op: 'open', c: 'sess-a', env_id: 'local', cols: 100, rows: 30 })
    expect(ws.readyState).toBe(FakeWebSocket.OPEN)
  })

  it('names the environment on the open frame, and only on the open frame', () => {
    // env_id is the routing key (runtime/terminal_env_bridge.py): "local" is
    // served by this service, anything else the parent dials for us.  Keystrokes
    // and resizes stay keyed by channel id alone.
    const local = open('sess-a')
    const child = open('sess-child', { envId: 'tunnel:0123456789abcdef' })
    const ws = FakeWebSocket.instances[0]
    ws.accept()

    expect(ws.ops()).toEqual([
      { op: 'open', c: 'sess-a', env_id: 'local', cols: 100, rows: 30 },
      { op: 'open', c: 'sess-child', env_id: 'tunnel:0123456789abcdef', cols: 100, rows: 30 },
    ])
    child.handle.send('ls\r')
    child.handle.resize(120, 40)
    expect(ws.ops().slice(2)).toEqual([
      { op: 'input', c: 'sess-child', d: 'ls\r' },
      { op: 'resize', c: 'sess-child', cols: 120, rows: 40 },
    ])
    // Keystrokes of another channel are keyed by channel id alone: the env was
    // decided once, at open.
    local.handle.send('\x03')
    expect(ws.ops().at(-1)).toEqual({ op: 'input', c: 'sess-a', d: '\x03' })
  })

  it('keeps ONE socket for the tab no matter how many environments are open', () => {
    // The whole point: an extra environment must not cost the browser a
    // connection (docs/plan-connection-budget.md), and no child credential may
    // show up in a socket URL.
    open('sess-local')
    open('sess-child-a', { envId: 'http://10.0.0.5:7988' })
    open('sess-child-b', { envId: 'tunnel:0123456789abcdef' })
    open('sess-child-c', { envId: 'https://sub.example.com:8443' })

    expect(FakeWebSocket.instances).toHaveLength(1)
    const ws = FakeWebSocket.instances[0]
    expect(ws.url).toBe(LOCAL_URL)
    ws.accept()
    expect(ws.sent.map((m) => m.env_id)).toEqual([
      'local', 'http://10.0.0.5:7988', 'tunnel:0123456789abcdef', 'https://sub.example.com:8443',
    ])
    expect(ws.url).not.toContain('token=')
  })

  it('re-opens each channel in the environment it belongs to', () => {
    open('sess-local')
    open('sess-child', { envId: 'tunnel:0123456789abcdef' })
    FakeWebSocket.instances[0].drop()

    vi.advanceTimersByTime(1000)
    const next = FakeWebSocket.instances[1]
    next.accept()
    expect(next.sent.map((m) => [m.op, m.c, m.env_id])).toEqual([
      ['open', 'sess-local', 'local'],
      ['open', 'sess-child', 'tunnel:0123456789abcdef'],
    ])
  })

  it('reports an unreachable environment on its own channel only', () => {
    const local = open('sess-local')
    const child = open('sess-child', { envId: 'tunnel:0123456789abcdef' })
    const ws = FakeWebSocket.instances[0]
    ws.accept()

    ws.emit({ op: 'error', c: 'sess-child', error: 'environment unreachable: child tunnel is offline' })
    expect(child.statuses.at(-1)).toMatchObject({
      connected: false,
      error: 'environment unreachable: child tunnel is offline',
    })
    // The local terminal of the same tab is untouched, and the socket lives.
    expect(local.statuses.filter((s) => s.error)).toEqual([])
    expect(ws.readyState).toBe(FakeWebSocket.OPEN)
  })
})
