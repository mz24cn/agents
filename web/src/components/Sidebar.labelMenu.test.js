// @vitest-environment jsdom
/**
 * 标签节点右键菜单测试。
 *
 * 验证：标签模式下右键标签叶节点，弹出且仅弹出两个动作
 * （生成概要和记忆 / 生成新会话）；点“生成概要和记忆”调用
 * sessions.summarizeLabel；点“生成新会话”先取聚合文档（无则先生成），
 * 再把聚合内容写入 labelSessionRequest 并跳转 #/chat。
 */
import { describe, it, expect, beforeAll, afterAll, afterEach, vi } from 'vitest'
import { mount, unmount, flushSync } from 'svelte'
import Sidebar from './Sidebar.svelte'
import * as api from '$lib/api.js'
import { labelSessionRequest } from '$lib/session-state.svelte.js'

const TREE = [
  { id: 1, name: '主题', type: 'category', session_count: 1, children: [
    { id: 1, name: 'cat', type: 'category', session_count: 1, children: ['s1'] },
  ] },
  { id: 6, name: '标签', type: 'labels', session_count: 1, children: [
    { id: 1, name: 'TTS', type: 'label', session_count: 1, children: ['s1'], stats: {} },
  ], stats: { label_count: 1 } },
]

vi.mock('$lib/api.js', async () => {
  const actual = await vi.importActual('$lib/api.js')
  return {
    ...actual,
    sessions: {
      ...actual.sessions,
      list: vi.fn(async () => ({ sessions: [{ session_id: 's1', title: '会话一' }], has_more: false })),
      summarizeLabel: vi.fn(async () => ({ doc: { label: 'TTS', summary: 'S', memory: 'M', config: {} } })),
      labelDoc: vi.fn(async () => ({ doc: null })),
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

async function mountSidebarLabels() {
  host = document.createElement('div')
  document.body.appendChild(host)
  instance = mount(Sidebar, { target: host })
  await drain()
  const buttons = document.querySelectorAll('.session-panel-heading .mode-btn')
  const labelsButton = [...buttons].find(b => (b.getAttribute('aria-label') || '').includes('标签'))
  labelsButton.click()
  await drain()
}

beforeAll(() => {
  Element.prototype.getBoundingClientRect = () => ({
    width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0,
  })
  Object.defineProperty(window, 'innerHeight', { value: 800, configurable: true, writable: true })
  Object.defineProperty(window, 'innerWidth', { value: 1200, configurable: true, writable: true })
  vi.stubGlobal('fetch', vi.fn(async (path) => {
    const url = String(path)
    if (url.startsWith('/v1/sessions/tree')) return jsonResponse({ tree: TREE })
    return jsonResponse({ sessions: [], has_more: false })
  }))
})

afterEach(() => {
  if (instance) unmount(instance)
  instance = null
  host?.remove()
  host = null
  vi.clearAllMocks()
})

afterAll(() => {
  vi.unstubAllGlobals()
})

describe('标签节点右键菜单', () => {
  it('右键标签节点弹出两个动作；生成概要调用 summarizeLabel', async () => {
    await mountSidebarLabels()
    const labelRow = document.querySelector('[data-category-path="6/1"]')
    expect(labelRow).toBeTruthy()

    labelRow.dispatchEvent(new MouseEvent('contextmenu', {
      bubbles: true, cancelable: true, clientX: 30, clientY: 40,
    }))
    await drain()

    const items = [...document.querySelectorAll('.terminal-node-menu-item')]
    expect(items.length).toBe(2)
    expect(items[0].textContent).toContain('概要和记忆')
    expect(items[1].textContent).toContain('新会话')

    items[0].click()
    await drain()
    expect(api.sessions.summarizeLabel).toHaveBeenCalledWith('TTS')
    expect(document.querySelector('.label-action-status')).toBeTruthy()
  })

  it('生成新会话：无聚合文档时先生成，再写入 labelSessionRequest 并跳转', async () => {
    await mountSidebarLabels()
    const labelRow = document.querySelector('[data-category-path="6/1"]')
    labelRow.dispatchEvent(new MouseEvent('contextmenu', {
      bubbles: true, cancelable: true, clientX: 30, clientY: 40,
    }))
    await drain()

    const tokenBefore = labelSessionRequest.token
    const items = [...document.querySelectorAll('.terminal-node-menu-item')]
    items[1].click()
    await drain(20)

    // labelDoc 返回 null → 必须先 summarizeLabel
    expect(api.sessions.labelDoc).toHaveBeenCalledWith('TTS')
    expect(api.sessions.summarizeLabel).toHaveBeenCalledWith('TTS')
    expect(labelSessionRequest.token).toBe(tokenBefore + 1)
    expect(labelSessionRequest.label).toBe('TTS')
    expect(labelSessionRequest.summary).toBe('S')
    expect(labelSessionRequest.memory).toBe('M')
    expect(window.location.hash).toBe('#/chat')
  })
})
