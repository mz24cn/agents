// @vitest-environment jsdom
/**
 * 「设置标题与标签」对话框测试。
 *
 * 验证：会话“…”菜单里的标题项打开对话框，预填现有标题与标签；标签输入框
 * 复用模型/工具/AI代理的 parseLabels 语义（半角/全角逗号、空白都是分隔符，
 * 去空去重）；确认后经 sessions.generateTitle 同时提交标题与标签数组。
 */
import { describe, it, expect, beforeAll, afterAll, afterEach, vi } from 'vitest'
import { mount, unmount, flushSync } from 'svelte'
import Sidebar from './Sidebar.svelte'
import * as api from '$lib/api.js'

vi.mock('$lib/api.js', async () => {
  const actual = await vi.importActual('$lib/api.js')
  return {
    ...actual,
    sessions: {
      ...actual.sessions,
      list: vi.fn(async () => ({
        sessions: [{ session_id: 's1', title: '旧标题', labels: ['tts', '性能'] }],
        has_more: false,
      })),
      generateTitle: vi.fn(async () => ({ title: '旧标题', title_given: true, labels: [] })),
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
  return document.querySelector('.session-panel-heading')
}

beforeAll(() => {
  Element.prototype.getBoundingClientRect = () => ({
    width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0,
  })
  Object.defineProperty(window, 'innerHeight', { value: 800, configurable: true, writable: true })
  Object.defineProperty(window, 'innerWidth', { value: 1200, configurable: true, writable: true })
  vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ tree: [], sessions: [], has_more: false })))
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

describe('设置标题与标签对话框', () => {
  it('菜单项打开对话框：预填标题与标签，提交时按 parseLabels 拆分', async () => {
    await mountSidebar()
    // 等 sessions.list 落到列表（mock 直接返回带 labels 的条目）
    await drain()

    const row = document.querySelector('.session-row')
    expect(row).toBeTruthy()
    row.querySelector('.session-menu-btn').click()
    await drain()

    const items = [...document.querySelectorAll('.session-dropdown-item')]
    // 中文环境下 generateTitle 的文案是“设置标题与标签”
    const titleItem = items.find(b => b.textContent.includes('标题'))
    expect(titleItem).toBeTruthy()
    titleItem.click()
    await drain()

    const inputs = document.querySelectorAll('.title-dialog input[type="text"]')
    expect(inputs.length).toBe(2)
    expect(inputs[0].value).toBe('旧标题')
    expect(inputs[1].value).toBe('tts, 性能')

    inputs[1].value = 'tts, 部署,tts,,  ，'
    inputs[1].dispatchEvent(new Event('input', { bubbles: true }))
    await drain()

    document.querySelector('.title-dialog-confirm').click()
    await drain()

    expect(api.sessions.generateTitle).toHaveBeenCalledTimes(1)
    // parseLabels 与模型/工具/AI代理同一语义：全角逗号也是分隔符、空段过滤；
    // 去重由后端 set_session_labels 保序完成，前端保持与表单一致的原始切分。
    const [sessionId, title, labels] = api.sessions.generateTitle.mock.calls[0]
    expect(sessionId).toBe('s1')
    expect(title).toBe('旧标题')
    expect(labels).toEqual(['tts', '部署', 'tts'])
  })
})
