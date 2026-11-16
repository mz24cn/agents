// @vitest-environment jsdom
/**
 * 侧边栏会话「···」弹出菜单的位置可见性测试。
 *
 * jsdom 不做布局，所以这里给 .session-dropdown / .session-menu-btn 打桩
 * getBoundingClientRect，模拟“会话条目贴近屏幕底部”的场景：
 *   1. 菜单本身必须被整体上移，底部条目（删除会话）留在视口内；
 *   2. ··· 子面板在下方空间不足时改为向上展开，底部同样不越界。
 */
import { describe, it, expect, beforeAll, afterAll, afterEach, vi } from 'vitest'
import { mount, unmount, flushSync } from 'svelte'
import Sidebar from './Sidebar.svelte'

const VIEWPORT = { width: 1200, height: 600 }
const EDGE_MARGIN = 8
const MENU_WIDTH = 200
// 两个用例按需改写：矮菜单用来触发子面板“向上展开”
let menuHeight = 420
// “···” 按钮：贴近屏幕底部的一行
const TRIGGER = { top: 520, right: 260 }

const zeroRect = {
  width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0,
}

const originalGetBoundingClientRect = Element.prototype.getBoundingClientRect

function stubRect(el) {
  if (el.classList?.contains('session-dropdown')) {
    return { ...zeroRect, width: MENU_WIDTH, height: menuHeight, right: MENU_WIDTH, bottom: menuHeight }
  }
  if (el.classList?.contains('session-menu-btn')) {
    return {
      ...zeroRect, width: 24, height: 24,
      top: TRIGGER.top, bottom: TRIGGER.top + 24,
      left: TRIGGER.right - 24, right: TRIGGER.right,
    }
  }
  return { ...zeroRect }
}

function jsonResponse(payload) {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
    body: { getReader: () => ({ read: async () => ({ done: true, value: undefined }) }) },
  }
}

function fakeFetch(path) {
  const url = String(path)
  if (url.startsWith('/v1/sessions/events')) return jsonResponse({})
  if (/\/v1\/sessions\/[^/]+/.test(url)) return jsonResponse({ messages: [], meta: null })
  if (url.startsWith('/v1/sessions')) {
    return jsonResponse({ sessions: [{ session_id: 's-bottom', title: '贴近底部的会话' }], has_more: false })
  }
  return jsonResponse({})
}

let host = null
let instance = null

async function drain(rounds = 6) {
  for (let i = 0; i < rounds; i++) await Promise.resolve()
  await flushSync()
}

async function openMenuOnBottomRow() {
  host = document.createElement('div')
  document.body.appendChild(host)
  instance = mount(Sidebar, { target: host })
  await flushSync()
  for (let i = 0; i < 10 && !document.querySelector('.session-menu-btn'); i++) await drain(1)

  const trigger = document.querySelector('.session-menu-btn')
  expect(trigger).toBeTruthy()
  trigger.dispatchEvent(new MouseEvent('mouseenter', { bubbles: false }))
  await drain()
  return document.querySelector('.session-dropdown')
}

beforeAll(() => {
  vi.useFakeTimers()
  Element.prototype.getBoundingClientRect = function () { return stubRect(this) }
  Object.defineProperty(window, 'innerHeight', { value: VIEWPORT.height, configurable: true, writable: true })
  Object.defineProperty(window, 'innerWidth', { value: VIEWPORT.width, configurable: true, writable: true })
  vi.stubGlobal('fetch', vi.fn(async (path) => fakeFetch(path)))
})

afterEach(() => {
  if (instance) unmount(instance)
  instance = null
  host?.remove()
  host = null
})

afterAll(() => {
  Element.prototype.getBoundingClientRect = originalGetBoundingClientRect
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('侧边栏会话菜单的可视区保护', () => {
  it('贴近底部的条目：菜单整体上移，底部条目仍在屏幕内', async () => {
    menuHeight = 420
    const menu = await openMenuOnBottomRow()
    expect(menu).toBeTruthy()

    const top = parseFloat(menu.style.getPropertyValue('top'))
    const left = parseFloat(menu.style.getPropertyValue('left'))
    // 触发点在 520，但菜单高 420 —— 会被抬到 600 - 420 - 8 = 172
    expect(top).toBe(VIEWPORT.height - menuHeight - EDGE_MARGIN)
    expect(top + menuHeight).toBeLessThanOrEqual(VIEWPORT.height)
    expect(left).toBe(TRIGGER.right + 4)
  })

  it('下方空间不足：··· 子面板向上展开，底部不越界', async () => {
    menuHeight = 120
    const menu = await openMenuOnBottomRow()
    expect(menu).toBeTruthy()

    const more = menu.querySelector('button.submenu-more-btn')
    expect(more).toBeTruthy()
    more.dispatchEvent(new MouseEvent('mouseenter', { bubbles: false }))
    await drain()

    const panel = document.querySelector('.user-message-submenu')
    expect(panel).toBeTruthy()
    const bottom = parseFloat(panel.style.getPropertyValue('bottom'))
    const maxHeight = parseFloat(panel.style.getPropertyValue('max-height'))
    // 与菜单底部对齐（菜单底部距屏幕底 8px），并压到剩余可用高度内
    expect(bottom).toBe(EDGE_MARGIN)
    expect(bottom + maxHeight).toBeLessThanOrEqual(VIEWPORT.height)
    // 向上展开：面板顶部同样在屏幕内
    expect(VIEWPORT.height - bottom - maxHeight).toBeGreaterThanOrEqual(0)
  })
})
