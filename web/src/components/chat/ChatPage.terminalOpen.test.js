// @vitest-environment jsdom
/**
 * 会话「打开终端」子菜单里进入嵌套子终端（terminal_layer）时，顶栏的终端标记必须
 * 走完 Loading... → 已连接。
 *
 * 报告的现象：从会话右键菜单的「打开终端」面板里双击一个子终端层，ChatPage 顶栏
 * 的终端标记永远停在 Loading...；双击环境节点本身（没有进入指令链）则一切正常。
 *
 * 原因在 ChatPage 的 terminalOpen effect：它调用 openTerminal()，而 openTerminal
 * 既读 terminals（找已有条目）又写 terminals（新 Map + 新条目）。effect 因此依赖了
 * 自己写的那个 state —— 带指令链时每一轮都会把 chain 换成新数组再写一次，永远不自愈
 * （effect_update_depth_exceeded），整次 flush 被中止，Terminal 的连接 effect 根本没
 * 跑到，open 帧一帧都发不出去。空链时第二轮不再写，所以只有子终端会卡。
 *
 * 断言三件事：
 *   1. open 帧带着面板给出的 env_id 发出去了（连接真的建立，而不是停在 Loading...）；
 *   2. 顶栏标记离开 Loading...（出现 ▼ 收起按钮）；
 *   3. 该层的进入指令被注入这条通道 —— 只有链路跑起来才会有 input 帧。
 */
import { describe, it, expect, beforeAll, afterAll, vi } from 'vitest'
import { mount, unmount, flushSync } from 'svelte'
import { sessionRestore, terminalOpen } from '$lib/session-state.svelte.js'
import { invalidateRemoteEnvCache } from '$lib/remote-execution.svelte.js'

const CHILD_ENV = 'http://127.0.0.1:7999/v1/setup'
const SID = 's-sub-terminal'

let frames = []

class FakeWS {
  static OPEN = 1
  constructor(url) {
    this.url = url
    // Already open: the hub's first sendOpen goes straight out, which is what
    // a reconnect on a live socket does in the browser too.
    this.readyState = 1
    setTimeout(() => this.onopen?.(), 0)
  }
  send(text) {
    const msg = JSON.parse(text)
    frames.push(msg)
    if (msg.op === 'open') {
      // The parent answers an env channel as soon as its child hop is up.
      setTimeout(() => this.onmessage?.({ data: JSON.stringify({ op: 'opened', c: msg.c }) }), 0)
    }
  }
  close() { this.readyState = 3 }
}

function jsonResponse(payload) {
  return {
    ok: true, status: 200,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
    body: { getReader: () => ({ read: async () => ({ done: true, value: undefined }) }) },
  }
}

let host = null
let app = null

/** Real ticks, not microtasks: the channel is opened from a setTimeout(0). */
async function settle(ms = 40, rounds = 12) {
  for (let i = 0; i < rounds; i++) {
    await new Promise(r => setTimeout(r, Math.max(1, Math.floor(ms / rounds))))
    await flushSync()
  }
}

const markerText = () =>
  document.querySelector('.terminal-status')?.textContent.trim() ?? null
const closeBtn = () => document.querySelector('.terminal-close-btn')

beforeAll(async () => {
  window.matchMedia = window.matchMedia
    || (() => ({ matches: false, media: '', addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }))
  globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} disconnect() {} }
  invalidateRemoteEnvCache()
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    // The child env must resolve, otherwise restoring the session falls back
    // to local and the terminal would not carry an env_id at all.
    if (String(url).includes('/v1/remote-envs')) {
      return jsonResponse({ envs: [{ id: CHILD_ENV, url: CHILD_ENV, transport: 'http' }] })
    }
    if (String(url).includes('/v1/env')) return jsonResponse({ env: {} })
    return jsonResponse({})
  }))
  vi.stubGlobal('WebSocket', FakeWS)
  invalidateRemoteEnvCache()
})

afterAll(() => {
  vi.unstubAllGlobals()
  if (app) { unmount(app); app = null }
  host?.remove()
})

describe('会话面板进入嵌套子终端', () => {
  it('顶栏标记走到已连接，进入指令注入到该环境的通道', async () => {
    const ChatPage = (await import('./ChatPage.svelte')).default
    host = document.createElement('div')
    document.body.appendChild(host)
    app = mount(ChatPage, { target: host })
    await settle(30)

    // The session runs in a child environment; that is the case the report hits.
    sessionRestore.pending = {
      sessionId: SID,
      messages: [{ role: 'user', content: 'hi' }],
      meta: { remote_env: CHILD_ENV },
    }
    await settle(30)

    frames = []
    terminalOpen.sessionId = SID
    terminalOpen.remoteEnvId = CHILD_ENV
    terminalOpen.commands = ['docker exec -it cc bash']
    terminalOpen.token += 1

    await settle(120)
    const opens = frames.filter(f => f.op === 'open')
    expect(opens.length).toBeGreaterThan(0)
    expect(opens[0]).toMatchObject({ c: SID, env_id: CHILD_ENV })

    // 连接状态回到顶栏：不再卡在 Loading...，而是已连接的收起按钮。
    expect(markerText()).not.toBe('Loading...')
    expect(closeBtn()).toBeTruthy()

    // 进入该层的指令被注入（chain 首条在 delayFirst 后发出）。
    await settle(1200, 24)
    const injected = frames.filter(f => f.op === 'input' && f.c === SID)
    expect(injected.map(f => f.d)).toContain('docker exec -it cc bash\r')

    unmount(app)
    app = null
    host.remove()
    host = null
  }, 30_000)
})
