// @vitest-environment jsdom
/**
 * 「点开一条会话，条目却从侧边栏消失了」的回归测试。
 *
 * loadSessionIntoChat 下载会话失败时，只有后端**明确**回答 404（这条会话不存在，
 * 后端已把它从 index 里清掉）才允许把条目从列表里移除。其余失败 —— 5xx/501、
 * 代理抖动、keep-alive 上前后两个请求串流错位 —— 只是这一次传输坏了，会话在磁盘
 * 上好好的，条目必须留在原处并给出错误提示。曾经一次 501 让条目当场消失、刷新
 * 页面又回来，就是因为 catch 里无条件 filter 掉了。
 */
import { describe, it, expect, beforeAll, afterEach, afterAll, vi } from 'vitest'
import { mount, unmount, flushSync } from 'svelte'
import Sidebar from './Sidebar.svelte'

let detailStatus = 200
let detailBody = { messages: [], meta: null }

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    body: { getReader: () => ({ read: async () => ({ done: true, value: undefined }) }) },
  }
}

function fakeFetch(path) {
  const url = String(path)
  if (url.startsWith('/v1/sessions/events')) return jsonResponse({})
  if (url.startsWith('/v1/sessions/tree')) return jsonResponse({ tree: [] })
  if (/^\/v1\/sessions\/s1(\?|$)/.test(url)) return jsonResponse(detailBody, detailStatus)
  if (url.startsWith('/v1/sessions')) {
    return jsonResponse({ sessions: [{ session_id: 's1', title: '会话一' }], has_more: false })
  }
  return jsonResponse({})
}

let host = null
let instance = null

async function drain(rounds = 20) {
  for (let i = 0; i < rounds; i++) await Promise.resolve()
  await flushSync()
}

async function mountSidebar({ status, body = { messages: [], meta: null }, error = '' }) {
  detailStatus = status
  detailBody = body ? { ...body } : { error }
  host = document.createElement('div')
  document.body.appendChild(host)
  instance = mount(Sidebar, { target: host })
  await drain()
  return document.querySelector('.session-item')
}

beforeAll(() => {
  Element.prototype.getBoundingClientRect = () => ({
    width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0,
  })
  Object.defineProperty(window, 'innerHeight', { value: 800, configurable: true, writable: true })
  Object.defineProperty(window, 'innerWidth', { value: 1200, configurable: true, writable: true })
  vi.stubGlobal('fetch', vi.fn(async (path) => fakeFetch(path)))
})

afterEach(() => {
  if (instance) unmount(instance)
  instance = null
  host?.remove()
  host = null
})

afterAll(() => {
  vi.unstubAllGlobals()
})

describe('下载失败时侧边栏条目的去留', () => {
  it('404（后端说会话不存在）→ 条目同步移除', async () => {
    const row = await mountSidebar({ status: 404, body: null, error: 'Session not found: s1' })
    expect(row).toBeTruthy()
    row.click()
    await drain()
    expect(document.querySelector('[data-session-id="s1"]')).toBeFalsy()
  })

  it('501（传输层坏了）→ 条目留在列表里，只报错', async () => {
    const row = await mountSidebar({ status: 501, body: null, error: '' })
    expect(row).toBeTruthy()
    row.click()
    await drain()
    // 会话还在磁盘上：不能凭一次失败的传输删除 UI 状态
    const kept = document.querySelector('[data-session-id="s1"]')
    expect(kept).toBeTruthy()
    expect(kept.textContent).toContain('会话一')
    // 失败仍然可见
    expect(document.querySelector('.session-error')?.textContent).toContain('501')
  })

  it('网络异常（fetch 直接 reject）→ 条目留在列表里', async () => {
    detailStatus = 0
    vi.stubGlobal('fetch', vi.fn(async (path) => {
      const url = String(path)
      if (/^\/v1\/sessions\/s1(\?|$)/.test(url)) return Promise.reject(new TypeError('Failed to fetch'))
      return fakeFetch(path)
    }))
    const row = await mountSidebar({ status: 200 })
    expect(row).toBeTruthy()
    row.click()
    await drain()
    expect(document.querySelector('[data-session-id="s1"]')).toBeTruthy()
    vi.stubGlobal('fetch', vi.fn(async (path) => fakeFetch(path)))
  })
})
