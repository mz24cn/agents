// One WebSocket per origin carries every terminal of this tab -- local ones and
// the ones whose PTY lives in a child environment.
//
// The browser caps HTTP/1.1 connections per origin at ~6 and a WebSocket
// counts against that pool, so "one socket per terminal" means a few open
// terminals and the tab can no longer make ordinary requests — they queue
// until a socket frees up (docs/plan-connection-budget.md).  The terminal
// endpoint (`WS /v1/terminals/ws`) multiplexes instead: every frame carries its
// channel id, `{"c": id, "d": chunk}` for PTY output/keystrokes and
// `{"op": "open"|"input"|"resize"|"close", ...}` for control.
//
// Routing to an environment lives in the **frame**, not in the URL: `open`
// carries `env_id` ("local" for this service's own PTYs, otherwise a registered
// child environment the parent dials on our behalf — see
// runtime/terminal_env_bridge.py).  That is what keeps the ledger at one socket
// per tab no matter how many environments the user opens terminals in: dialing
// one socket per environment would put the cap right back where it was.  It
// also means no child credential ever enters a browser URL.
//
// A channel stays attached while its terminal is merely hidden
// (`visible=false`): the PTY keeps running server-side but nothing it prints
// while detached is replayed, so the xterm scrollback this socket keeps
// feeding is the only history there is.  With one shared socket, keeping
// hidden terminals attached costs nothing.
//
// The hub owns the socket; components own channels.  Losing the socket
// re-dials and re-opens every channel that is still registered (in the
// environment each one belongs to).

const RECONNECT_MS = 1000

/** env_id for terminals served by this service itself. */
export const LOCAL_ENV_ID = 'local'

/** url -> TerminalHub */
const hubs = new Map()

class TerminalHub {
  constructor(url) {
    this.url = url
    this.ws = null
    this.connecting = false
    this.reconnectTimer = null
    /** terminal_id -> { onData, onStatus, cols, rows, workspace, envId } */
    this.channels = new Map()
  }

  ensureConnected() {
    if (this.ws || this.connecting) return
    let ws
    try {
      ws = new WebSocket(this.url)
    } catch {
      this.scheduleReconnect()
      return
    }
    this.connecting = true
    this.ws = ws

    ws.onopen = () => {
      this.connecting = false
      // (Re)open every registered channel: after a reconnect the server has
      // dropped them all, and re-opening re-attaches the same PTY sessions.
      for (const id of [...this.channels.keys()]) this.sendOpen(id)
    }
    ws.onmessage = (evt) => this.handleMessage(evt.data)
    ws.onclose = () => {
      this.connecting = false
      if (this.ws !== ws) return // a newer socket already took over
      this.ws = null
      for (const ch of this.channels.values()) {
        ch.onStatus?.({ connected: false, loading: true, error: null })
      }
      if (this.channels.size) this.scheduleReconnect()
      else hubs.delete(this.url)
    }
    ws.onerror = () => {}
  }

  scheduleReconnect() {
    if (this.reconnectTimer || !this.channels.size) return
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.ensureConnected()
    }, RECONNECT_MS)
  }

  sendControl(payload) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      try {
        this.ws.send(JSON.stringify(payload))
      } catch {}
    }
  }

  sendOpen(terminalId) {
    const ch = this.channels.get(terminalId)
    if (!ch) return
    // env_id is what routes this channel: "local" is served by this service,
    // anything else is dialed to that child environment by the parent.  It
    // rides on every (re)open, so a re-dial re-attaches each terminal in the
    // environment it belongs to.
    const payload = {
      op: 'open', c: terminalId, env_id: ch.envId, cols: ch.cols, rows: ch.rows,
    }
    if (ch.workspace) payload.workspace = ch.workspace
    this.sendControl(payload)
  }

  handleMessage(data) {
    let msg
    try {
      msg = JSON.parse(data)
    } catch {
      return
    }
    if (!msg || typeof msg !== 'object') return
    const id = msg.c
    if (typeof id !== 'string') return
    const ch = this.channels.get(id)
    if (!ch) return // channel closed while its frame was in flight
    if (msg.op === 'opened') {
      ch.onStatus?.({ connected: true, loading: false, error: null })
    } else if (msg.op === 'closed') {
      ch.onStatus?.({ connected: false, loading: false, error: null, closed: true })
    } else if (msg.op === 'error') {
      ch.onStatus?.({ connected: false, loading: false, error: msg.error || 'terminal error' })
    } else if (typeof msg.d === 'string') {
      ch.onData?.(msg.d)
    }
  }

  closeChannel(terminalId) {
    if (!this.channels.delete(terminalId)) return
    this.sendControl({ op: 'close', c: terminalId })
    if (!this.channels.size) this.closeSocket()
  }

  /** Detach a channel only if ``entry`` is still the one registered. */
  releaseChannel(terminalId, entry) {
    if (this.channels.get(terminalId) !== entry) return
    this.closeChannel(terminalId)
  }

  closeSocket() {
    clearTimeout(this.reconnectTimer)
    this.reconnectTimer = null
    this.connecting = false
    const ws = this.ws
    this.ws = null
    hubs.delete(this.url)
    if (ws) {
      try {
        ws.onclose = null
        ws.onmessage = null
        ws.onerror = null
        ws.close()
      } catch {}
    }
  }
}

function hubFor(url) {
  let hub = hubs.get(url)
  if (!hub) {
    hub = new TerminalHub(url)
    hubs.set(url, hub)
  }
  return hub
}

/**
 * Attach one terminal to this origin's shared socket.
 *
 * @param {object} opts
 * @param {string} opts.url          mux socket URL (this service's origin)
 * @param {string} opts.terminalId   terminal/session id, the channel id
 * @param {string} [opts.envId]      "local", or a child environment id
 * @param {string} [opts.workspace]  workspace to spawn the PTY in (first open wins)
 * @param {number} [opts.cols] [opts.rows]
 * @param {(chunk: string) => void} [opts.onData]    PTY output
 * @param {(s: {connected: boolean, loading: boolean, error?: string|null}) => void} [opts.onStatus]
 * @returns {{send: (data: string) => void, resize: (cols: number, rows: number) => void,
 *            close: () => void, isOpen: () => boolean}}
 */
export function openTerminalChannel({
  url,
  terminalId,
  envId = LOCAL_ENV_ID,
  workspace = '',
  cols = 80,
  rows = 24,
  onData = null,
  onStatus = null,
}) {
  const hub = hubFor(url)
  // Same id opened twice (remount, target switch racing an old handle): the
  // newest component owns the channel.  Detach the previous one server-side
  // without tearing the socket down, and make the stale handle inert below.
  if (hub.channels.has(terminalId)) hub.sendControl({ op: 'close', c: terminalId })

  const entry = { onData, onStatus, cols, rows, workspace, envId }
  hub.channels.set(terminalId, entry)
  hub.ensureConnected()
  hub.sendOpen(terminalId)

  let closed = false
  return {
    send(data) {
      if (closed || !data) return
      hub.sendControl({ op: 'input', c: terminalId, d: data })
    },
    resize(nextCols, nextRows) {
      if (closed) return
      entry.cols = nextCols
      entry.rows = nextRows
      hub.sendControl({ op: 'resize', c: terminalId, cols: nextCols, rows: nextRows })
    },
    close() {
      if (closed) return
      closed = true
      hub.releaseChannel(terminalId, entry)
    },
    isOpen() {
      return !closed && hub.ws?.readyState === WebSocket.OPEN
    },
  }
}

/** Tear every hub down (tests). */
export function __resetTerminalHubsForTests() {
  for (const hub of [...hubs.values()]) hub.closeSocket()
  hubs.clear()
}
