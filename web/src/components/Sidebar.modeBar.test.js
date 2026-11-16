// @vitest-environment jsdom
/**
 * 会话面板"模式图标行"测试（v7：森林根折成一行图标）。
 *
 * 验证：
 *   1. 标题行是 5 个纯图标按钮（最近/主题/历史/工作区 + 右对齐终端），无文字；
 *   2. recent 模式只有整体会话列表，没有树框；
 *   3. 分类模式（主题/工作区/历史）渲染"根的子级"——根节点本身不出现在树里，
 *      查询路径仍带根 id 前缀；分类框约占 1/3 高度（会话列表可见）；
 *   4. 终端模式渲染终端列表且 .category-tree 带 full 类（占满高度），无会话列表；
 *   5. 提为顶层的主题一级分类不再带根图标（category 节点一律无图标）。
 */
import { describe, it, expect, beforeAll, afterAll, afterEach, vi } from 'vitest'
import { mount, unmount, flushSync } from 'svelte'
import Sidebar from './Sidebar.svelte'

const TREE = [
  {
    id: 1, name: '主题', type: 'category', session_count: 1,
    children: [
      { id: 1, name: '网络调试', type: 'category', session_count: 1, children: ['s1'] },
    ],
  },
  {
    id: 2, name: '工作区', type: 'workspace', session_count: 1,
    children: [
      {
        id: 1, name: '本地', type: 'environment', environment: 'local', session_count: 1,
        children: [
          { id: 1, name: 'proj', type: 'workspace', workspace: '/x/proj', session_count: 1, children: ['s1'] },
        ],
      },
    ],
  },
  {
    id: 3, name: '终端', type: 'terminal', session_count: 0,
    children: [
      {
        id: 1, name: '本地', type: 'terminal', environment: 'local', env_id: '', session_count: 0,
        children: [
          { id: 1, uid: 'u1', name: '容器', type: 'terminal_layer', command: 'docker exec -it c sh', children: [] },
        ],
      },
      {
        id: 2, name: '10.0.0.1:7988', type: 'terminal', environment: '10.0.0.1:7988',
        env_id: 'http://10.0.0.1:7988', session_count: 0, children: [],
      },
    ],
  },
  {
    id: 5, name: '会话历史', type: 'history', session_count: 1,
    children: [
      { id: 1, name: '202611', type: 'month', session_count: 1, children: ['s1'] },
    ],
  },
]

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
  if (url.startsWith('/v1/sessions/tree')) return jsonResponse({ tree: TREE })
  if (/\/v1\/sessions\/[^/?]+/.test(url)) return jsonResponse({ messages: [], meta: null })
  if (url.startsWith('/v1/sessions')) {
    return jsonResponse({ sessions: [{ session_id: 's1', title: '会话一' }], has_more: false })
  }
  return jsonResponse({})
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
  return document.querySelectorAll('.session-panel-heading .mode-btn')
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

describe('会话面板模式图标行', () => {
  it('标题行是 5 个纯图标按钮，终端按钮右对齐', async () => {
    const buttons = await mountSidebar()
    expect(buttons.length).toBe(5)
    const last = buttons[4]
    expect(last.classList.contains('mode-btn-terminal')).toBe(true)
    // 不再显示模式文字（仅图标 glyph）
    for (const button of buttons) {
      expect(button.querySelector('.mode-glyph')?.textContent.trim().length).toBeGreaterThan(0)
    }
    // 默认 recent 模式：整体会话列表，无分类树框
    expect(buttons[0].classList.contains('active')).toBe(true)
    expect(document.querySelector('.category-tree')).toBeFalsy()
    expect(document.querySelector('.session-list')).toBeTruthy()
  })

  it('工作区模式只渲染根的子级，路径仍带根 id 前缀', async () => {
    const buttons = await mountSidebar()
    buttons[3].click()
    await drain()
    expect(document.querySelector('.category-tree')).toBeTruthy()
    // 工作区根本身不再出现
    const treeBox = document.querySelector('.category-tree')
    expect(treeBox.textContent).not.toContain('工作区')
    // 环境节点提为顶层：路径 "2/1"（根 id 前缀保留）
    const envRow = document.querySelector('[data-category-path="2/1"]')
    expect(envRow).toBeTruthy()
    expect(envRow.textContent).toContain('本地')
    // 未选末级分类：没有会话列表
    expect(document.querySelector('.session-list')).toBeFalsy()
    // 点开末级工作区分类 → 会话列表出现（分类框 + 列表共存）
    document.querySelector('[data-category-path="2/1"]').click()
    await drain()
    document.querySelector('[data-category-path="2/1/1"]').click()
    await drain()
    expect(document.querySelector('.session-list')).toBeTruthy()
    expect(document.querySelector('.session-list').textContent).toContain('会话一')
  })

  it('终端模式渲染终端列表、树框 full 占满高度且无会话列表', async () => {
    const buttons = await mountSidebar()
    buttons[4].click()
    await drain()
    const treeBox = document.querySelector('.category-tree')
    expect(treeBox).toBeTruthy()
    expect(treeBox.classList.contains('full')).toBe(true)
    expect(document.querySelector('[data-category-path="3/1"]')).toBeTruthy()
    expect(document.querySelector('[data-category-path="3/2"]')).toBeTruthy()
    // 终端节点没有会话列表语义
    expect(document.querySelector('.session-list')).toBeFalsy()
  })

  it('历史模式渲染月份节点；主题模式一级分类不带根图标', async () => {
    const buttons = await mountSidebar()
    buttons[2].click()
    await drain()
    const monthRow = document.querySelector('[data-category-path="5/1"]')
    expect(monthRow).toBeTruthy()
    expect(monthRow.textContent).toContain('202611')

    buttons[1].click()
    await drain()
    const topicRow = document.querySelector('[data-category-path="1/1"]')
    expect(topicRow).toBeTruthy()
    expect(topicRow.textContent).toContain('网络调试')
    // 提为顶层的普通分类节点不带 📚 根图标
    expect(topicRow.querySelector('.category-kind')).toBeFalsy()
    // 主题模式不是 full：分类框限高，选择末级后会话列表可见
    expect(document.querySelector('.category-tree').classList.contains('full')).toBe(false)
  })

  it('回到最近模式：树框消失，整体列表回来', async () => {
    const buttons = await mountSidebar()
    buttons[1].click()
    await drain()
    expect(document.querySelector('.category-tree')).toBeTruthy()
    buttons[0].click()
    await drain()
    expect(document.querySelector('.category-tree')).toBeFalsy()
    expect(document.querySelector('.session-list')).toBeTruthy()
  })
})
