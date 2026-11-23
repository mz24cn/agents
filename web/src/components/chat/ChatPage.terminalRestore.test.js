// @vitest-environment jsdom
/**
 * 刷新页面后重新打开会话：终端窗口要跟着回来（面板可以先收起）。
 *
 * 报告的现象：一条会话开着终端窗口用过，刷新页面再打开它，ChatPage 顶栏不再有
 * 终端图标 —— terminals 映射是一次页面加载的内存状态，刷新即空，窗口也没了。只
 * 能回侧栏的终端树里再双击一次，把已经存在（或已被回收）的终端重新点开一遍。
 *
 * 修法：后端在**终端被打开那一刻**把「这条会话有终端窗口」写进会话 meta
 * （terminal="enabled"，双击销毁时抹掉），会话恢复时 ChatPage 据此重建条目，
 * Terminal.svelte 一挂载就 attach。断言三件事：
 *   1. 图标回来了（💻 = .terminal-show-btn，而不是收起按钮：面板是收起的）；
 *   2. 窗口真的开着 —— open 帧已经发出去了，不用等用户点；
 *   3. 光有 exec_cli 在工具集里不算：终端没打开过的会话不恢复任何东西。
 */
import { describe, it, expect, beforeAll, afterAll, vi } from 'vitest'
import { mount, unmount, flushSync } from 'svelte'
import { sessionRestore } from '$lib/session-state.svelte.js'
import { invalidateRemoteEnvCache } from '$lib/remote-execution.svelte.js'

const SID = 's-terminal-restore'
const SID_NO_TERMINAL = 's-no-terminal'

let frames = []

class FakeWS {
  static OPEN = 1
  constructor(url) {
    this.url = url
    this.readyState = 1
    setTimeout(() => this.onopen?.(), 0)
  }
  send(text) {
    const msg = JSON.parse(text)
    frames.push(msg)
    if (msg.op === 'open') {
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

const showBtn = () => document.querySelector('.terminal-show-btn')
const closeBtn = () => document.querySelector('.terminal-close-btn')
const openFrames = () => frames.filter(f => f.op === 'open')

beforeAll(async () => {
  window.matchMedia = window.matchMedia
    || (() => ({ matches: false, media: '', addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }))
  globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} disconnect() {} }
  invalidateRemoteEnvCache()
  vi.stubGlobal('fetch', vi.fn(async (url) => {
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
  host = null
})

describe('刷新后恢复会话的终端窗口', () => {
  it('meta.terminal 让窗口开回来：图标可见、面板收起、通道已 attach', async () => {
    const ChatPage = (await import('./ChatPage.svelte')).default
    host = document.createElement('div')
    document.body.appendChild(host)
    app = mount(ChatPage, { target: host })
    await settle(30)

    frames = []
    sessionRestore.pending = {
      sessionId: SID,
      messages: [{ role: 'user', content: 'hi' }],
      meta: { terminal: 'enabled', tool_ids: ['read_file', 'exec_cli'] },
    }
    await settle(300)

    // 窗口是开着的，但没显示：顶栏上是「显示终端」那一枚，不是收起按钮。
    expect(showBtn()).toBeTruthy()
    expect(closeBtn()).toBeFalsy()
    //  attach 不需要用户先点一下：通道已经在这条会话上打开了。
    const opens = openFrames()
    expect(opens.length).toBeGreaterThan(0)
    expect(opens[0]).toMatchObject({ c: SID })

    // 点开只是把面板展开，不该再来一条通道。
    showBtn().click()
    await settle(120)
    expect(openFrames().length).toBe(opens.length)
    expect(closeBtn()).toBeTruthy()
    expect(showBtn()).toBeFalsy()

    unmount(app)
    app = null
    host.remove()
    host = null
  }, 30_000)

  it('工具集里有 exec_cli 但终端没打开过：什么都不恢复', async () => {
    const ChatPage = (await import('./ChatPage.svelte')).default
    host = document.createElement('div')
    document.body.appendChild(host)
    app = mount(ChatPage, { target: host })
    await settle(30)

    frames = []
    sessionRestore.pending = {
      sessionId: SID_NO_TERMINAL,
      messages: [{ role: 'user', content: 'hi' }],
      meta: { tool_ids: ['read_file', 'exec_cli'] },
    }
    await settle(300)

    // 模型一次没调 exec_cli，终端就没开过 —— 不能替用户起一个 shell。
    expect(showBtn()).toBeFalsy()
    expect(openFrames()).toEqual([])

    unmount(app)
    app = null
    host.remove()
    host = null
  }, 30_000)
})
