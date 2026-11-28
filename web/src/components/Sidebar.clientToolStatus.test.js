// @vitest-environment jsdom
/**
 * 需求 6 的侧边栏回归：客户端工具调用后必须显示「等待客户端」的蓝色状态，
 * 且不能被 done_success_unread（绿色）覆盖；客户端回传后继续推理、真正完成时
 * 仍要正常回到绿色 / 红色。
 *
 * 覆盖的是「事件 → 事实层 → 任意分类的行」这条链路：SSE 不分分类，事实按
 * session_id 共享，所以蓝色状态对同一会话的所有分类副本同时生效。
 */
import { describe, it, expect, beforeAll, afterAll, afterEach, vi } from 'vitest'
import { mount, unmount, flushSync } from 'svelte'
import Sidebar from './Sidebar.svelte'
import * as api from '$lib/api.js'
import { CLIENT_TOOL_STATUS } from '$lib/session-facts.js'

// SSE 订阅改成可注入的假 hub：测试可以直接推后端帧
const hub = vi.hoisted(() => ({ push: null }))

vi.mock('$lib/api.js', async () => {
  const actual = await vi.importActual('$lib/api.js')
  return {
    ...actual,
    subscribeSessionEvents: vi.fn((cb) => {
      hub.push = cb
      return () => { hub.push = null }
    }),
    sessions: {
      ...actual.sessions,
      list: vi.fn(async () => ({
        sessions: [{ session_id: 's1', title: '等客户端工具的会话' }],
        has_more: false,
      })),
      tree: vi.fn(async () => ({ tree: [] })),
    },
  }
})

function jsonResponse(payload) {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
    body: { getReader: () => ({ read: async () => ({ done: true, value: undefined }) }) },
  }
}

let host = null
let instance = null

async function drain(rounds = 12) {
  for (let i = 0; i < rounds; i++) await Promise.resolve()
  await flushSync()
}

async function mountSidebar() {
  host = document.createElement('div')
  document.body.appendChild(host)
  instance = mount(Sidebar, { target: host })
  await drain()
  return host
}

function rowItem(sessionId = 's1') {
  return document.querySelector(`.session-row[data-session-id="${sessionId}"] .session-item`)
}

function statusClass(sessionId = 's1') {
  return rowItem(sessionId)?.className || '(no row)'
}

function emit(frame) {
  expect(hub.push, 'SSE 订阅未建立').toBeTruthy()
  hub.push(frame)
}

beforeAll(() => {
  vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({})))
})

afterEach(() => {
  if (instance) { unmount(instance); instance = null }
  if (host) { host.remove(); host = null }
  api.sessions.list.mockClear()
})

afterAll(() => {
  vi.unstubAllGlobals()
})

describe('客户端工具等待状态', () => {
  it('出现 client_tool_calls 后行显示蓝色，并带后端回传的权威标题', async () => {
    await mountSidebar()

    emit({
      event: 'message', session_id: 's1', status: CLIENT_TOOL_STATUS,
      title: '工具跑了一半的标题', title_given: false,
    })
    await drain()

    expect(statusClass()).toContain('status-client-tool')
    expect(statusClass()).not.toContain('status-done-success-unread')
    expect(statusClass()).not.toContain('status-streaming')
    expect(rowItem().textContent).toContain('工具跑了一半的标题')
  })

  it('done_success_unread（绿色）不得把蓝色覆盖掉', async () => {
    await mountSidebar()

    emit({ event: 'message', session_id: 's1', status: CLIENT_TOOL_STATUS })
    await drain()
    emit({ event: 'message', session_id: 's1', status: 'done_success_unread', title: '完成' })
    await drain()
    expect(statusClass()).toContain('status-client-tool')
    expect(statusClass()).not.toContain('status-done-success-unread')

    // 「已读」也不能把等待中的回合说成结束：mark-read 只清徽标，不清状态
    emit({ event: 'message', session_id: 's1', status: 'idle' })
    await drain()
    expect(statusClass()).toContain('status-client-tool')
  })

  it('客户端回传后继续推理 → 真正完成：蓝色 → 橙色 → 绿色', async () => {
    await mountSidebar()

    emit({ event: 'message', session_id: 's1', status: CLIENT_TOOL_STATUS })
    await drain()
    emit({ event: 'message', session_id: 's1', status: 'streaming' })
    await drain()
    expect(statusClass()).toContain('status-streaming')
    emit({ event: 'message', session_id: 's1', status: 'done_success_unread', title: '最终标题' })
    await drain()
    expect(statusClass()).toContain('status-done-success-unread')
    expect(rowItem().textContent).toContain('最终标题')
  })

  it('错误终态仍能把蓝色改成红色（成功语义之外的其它状态未被牵连）', async () => {
    await mountSidebar()
    emit({ event: 'message', session_id: 's1', status: CLIENT_TOOL_STATUS })
    await drain()
    emit({ event: 'message', session_id: 's1', status: 'done_error_unread', title: '出错' })
    await drain()
    expect(statusClass()).toContain('status-done-error-unread')
    expect(statusClass()).not.toContain('status-client-tool')
  })

  it('刷新页面后的 SSE init 快照里也是蓝色（等待态在快照里同样保留）', async () => {
    await mountSidebar()
    emit({
      event: 'init',
      sessions: { s1: CLIENT_TOOL_STATUS },
      titles: { s1: { title: '快照里的标题', title_given: false } },
    })
    await drain()
    expect(statusClass()).toContain('status-client-tool')
    expect(rowItem().textContent).toContain('快照里的标题')
  })
})
