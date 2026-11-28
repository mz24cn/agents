// @vitest-environment jsdom
/**
 * 标签节点右键菜单测试。
 *
 * 验证：标签模式下右键标签叶节点，弹出且仅弹出三个动作
 * （生成概要和记忆 / 发起新会话 / 重命名标签）；点"生成概要和记忆"调用
 * sessions.summarizeLabel；点"发起新会话"先取聚合文档（无则先生成），
 * 再把聚合内容写入 labelSessionRequest 并跳转 #/chat；点"重命名标签"弹出
 * 改名对话框，确认后调用 sessions.renameLabel 并刷新分类树，非法新名
 * （含逗号/空格）不发请求。
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
      renameLabel: vi.fn(async () => ({ changed: true, from: 'TTS', to: '语音合成', session_count: 2 })),
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

/** 右键标签叶节点，让菜单出现。 */
async function openLabelMenu() {
  const labelRow = document.querySelector('[data-category-path="6/1"]')
  expect(labelRow).toBeTruthy()
  labelRow.dispatchEvent(new MouseEvent('contextmenu', {
    bubbles: true, cancelable: true, clientX: 30, clientY: 40,
  }))
  await drain()
  return labelRow
}

/** 通过原生 setter + input 事件驱动 Svelte 5 的 bind:value。 */
function setInput(input, value) {
  const proto = Object.getPrototypeOf(input)
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set
  setter.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
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
  it('右键标签节点弹出三个动作；生成概要调用 summarizeLabel', async () => {
    await mountSidebarLabels()
    await openLabelMenu()

    const items = [...document.querySelectorAll('.terminal-node-menu-item')]
    expect(items.length).toBe(3)
    expect(items[0].textContent).toContain('概要和记忆')
    // 措辞是"发起新会话"而不是"生成新会话"：新会话自动带来源标签，不是凭空生成
    expect(items[1].textContent).toContain('新会话')
    // 菜单项措辞精确为「重命名」（用户要求）：不带「标签」二字，标签只在对话框里说明
    const renameEmoji = items[2].querySelector('.menu-emoji')
    const renameItemText = (renameEmoji
      ? items[2].textContent.replace(renameEmoji.textContent, '')
      : items[2].textContent).trim()
    expect(renameItemText).toBe('重命名')

    items[0].click()
    await drain()
    expect(api.sessions.summarizeLabel).toHaveBeenCalledWith('TTS')
    expect(document.querySelector('.label-action-status')).toBeTruthy()
  })

  it('发起新会话：无聚合文档时先生成，再写入 labelSessionRequest 并跳转', async () => {
    await mountSidebarLabels()
    await openLabelMenu()

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

  it('重命名标签：确认后调用 renameLabel 并给出同步条数', async () => {
    await mountSidebarLabels()
    await openLabelMenu()

    const items = [...document.querySelectorAll('.terminal-node-menu-item')]
    items[2].click()
    await drain()

    const input = document.querySelector('.terminal-layer-dialog input')
    expect(input).toBeTruthy()
    expect(input.value).toBe('TTS')          // 预填当前标签名
    // 对话框可以称「重命名标签」，菜单项只称「重命名」
    expect(document.querySelector('.terminal-layer-dialog .terminal-layer-title')?.textContent)
      .toContain('重命名标签')

    setInput(input, '语音合成')
    await drain()
    document.querySelector('.terminal-layer-btn.primary').click()
    await drain(20)

    expect(api.sessions.renameLabel).toHaveBeenCalledWith('TTS', '语音合成')
    // 对话框关闭 + 结果提示（旧标签节点由分类树重建而消失）
    expect(document.querySelector('.terminal-layer-dialog')).toBeNull()
    const status = document.querySelector('.label-action-status')
    expect(status).toBeTruthy()
    expect(status.textContent).toContain('重命名')
    expect(status.textContent).toContain('语音合成')
  })

  it('重命名标签：新名含分隔符时前端拦住，不发请求', async () => {
    await mountSidebarLabels()
    await openLabelMenu()

    const items = [...document.querySelectorAll('.terminal-node-menu-item')]
    items[2].click()
    await drain()

    const input = document.querySelector('.terminal-layer-dialog input')
    setInput(input, '语音 合成,版')
    await drain()
    document.querySelector('.terminal-layer-btn.primary').click()
    await drain()

    expect(api.sessions.renameLabel).not.toHaveBeenCalled()
    const error = document.querySelector('.terminal-layer-error')
    expect(error).toBeTruthy()
    expect(error.textContent).toContain('不能包含')
  })

  it('重命名标签：改名失败时保留对话框并显示后端错误', async () => {
    api.sessions.renameLabel.mockRejectedValueOnce(new Error('label not found'))
    await mountSidebarLabels()
    await openLabelMenu()

    const items = [...document.querySelectorAll('.terminal-node-menu-item')]
    items[2].click()
    await drain()

    setInput(document.querySelector('.terminal-layer-dialog input'), '另一个名字')
    await drain()
    document.querySelector('.terminal-layer-btn.primary').click()
    await drain(20)

    expect(api.sessions.renameLabel).toHaveBeenCalledWith('TTS', '另一个名字')
    const error = document.querySelector('.terminal-layer-error')
    expect(error).toBeTruthy()
    expect(error.textContent).toContain('label not found')
  })
})
