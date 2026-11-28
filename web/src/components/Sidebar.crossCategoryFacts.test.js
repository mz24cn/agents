// @vitest-environment jsdom
/**
 * 需求 5 的侧边栏回归：同一个 session 在「主题分类 A / 主题分类 B」里有两份行副本，
 * 但标题、旗标、推理状态必须始终一致 —— 因为前端事实按 session_id 只有一份。
 *
 * 三个真实竞态：
 *   1. 两个分类都拉过一次列表（各自缓存），在 A 里改名 + 换旗标后切到 B（命中缓存、
 *      行副本还是旧值）必须立刻一致，不能「A 新 B 旧」；
 *   2. B 分类的列表响应在途时用户改名 / 后端状态变化，晚到的旧响应不得回退；
 *   3. 推理状态由 SSE 驱动，不分分类：在 B 里开始推理，切回 A 也是同一个状态。
 */
import { describe, it, expect, beforeAll, afterAll, afterEach, vi } from 'vitest'
import { mount, unmount, flushSync } from 'svelte'
import Sidebar from './Sidebar.svelte'
import * as api from '$lib/api.js'

const TREE = [
  {
    id: 1, name: '主题', type: 'category', session_count: 1, children: [
      { id: 1, name: '分类A', type: 'category', session_count: 1, children: ['s1'] },
      { id: 2, name: '分类B', type: 'category', session_count: 1, children: ['s1'] },
    ],
  },
  // 标签根的全部标签节点 = 「设置标签」项末尾 ··· 面板的候选
  {
    id: 2, name: '标签', type: 'labels', session_count: 1, children: [
      { id: 3, name: '旧标签', type: 'label', session_count: 1, children: ['s1'], stats: {} },
      { id: 4, name: '新标签', type: 'label', session_count: 3, children: [], stats: {} },
      { id: 5, name: '告警', type: 'label', session_count: 2, children: [], stats: {} },
    ],
  },
]

// 服务端只有一份权威元数据（index.json）：列表 / 改名 / 旗标读写的是同一个 state，
// 这样测试才能区分「服务端真的变了」和「前端自己把旧副本显示出来了」。
const server = vi.hoisted(() => ({ flag: '🚩', title: '会话原标题', labels: ['旧标签'] }))
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
      list: vi.fn(async (page, size, category) => ({
        // 分类不同，行副本不同（分页 / 排序各自独立），但元数据同源
        sessions: [{
          session_id: 's1',
          title: server.title,
          flag: server.flag,
          labels: [...server.labels],
          category: category || '',
        }],
        has_more: false,
      })),
      tree: vi.fn(async () => ({ tree: [] })),
      generateTitle: vi.fn(async (sessionId, title) => {
        if (title) server.title = title
        return {
          status: 'success', session_id: sessionId, title: server.title,
          title_given: !!title, labels: [...server.labels], flag: server.flag,
        }
      }),
      setFlag: vi.fn(async (sessionId, flag) => {
        server.flag = flag
        return {
          status: 'success', session_id: sessionId, flag: server.flag,
          title: server.title, title_given: true, labels: [...server.labels],
        }
      }),
      setLabels: vi.fn(async (sessionId, labels) => {
        server.labels = [...labels]
        return {
          status: 'success', session_id: sessionId, labels: [...server.labels],
          title: server.title, title_given: true, flag: server.flag,
        }
      }),
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
let releaseStalePage = null

async function drain(rounds = 12) {
  for (let i = 0; i < rounds; i++) await Promise.resolve()
  await flushSync()
}

async function mountSidebar() {
  host = document.createElement('div')
  document.body.appendChild(host)
  instance = mount(Sidebar, { target: host })
  api.sessions.tree.mockResolvedValue({ tree: TREE })
  await drain(20)
  // 默认模式是「最近」，树框不渲染；切到「主题」模式才有分类树
  const buttons = [...document.querySelectorAll('.session-panel-heading .mode-btn')]
  const categoryBtn = buttons.find(b => (b.getAttribute('aria-label') || '').includes('主题'))
  expect(categoryBtn, '未找到主题模式按钮').toBeTruthy()
  categoryBtn.click()
  await drain(20)
}

function categoryRow(path) {
  return document.querySelector(`.category-row[data-category-path="${path}"]`)
}

async function selectCategory(path) {
  let leaf = categoryRow(path)
  if (!leaf) {
    const parent = categoryRow(path.split('/').slice(0, -1).join('/'))
    if (parent) {
      parent.click()                 // 非末级节点：先展开
      await drain()
      leaf = categoryRow(path)
    }
  }
  expect(leaf, `分类节点 ${path} 未出现`).toBeTruthy()
  leaf.click()
  await drain(20)
}

function openMenuForRow(sessionId) {
  const row = document.querySelector(`.session-row[data-session-id="${sessionId}"]`)
  expect(row, `会话行 ${sessionId} 未出现`).toBeTruthy()
  row.querySelector('.session-menu-btn').click()
}

function menuItemByText(text) {
  return [...document.querySelectorAll('.session-dropdown-item')].find(b => b.textContent.includes(text))
}

function setInput(input, value) {
  const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(input), 'value').set
  setter.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

/** 「设置标题」主区域：打开对话框，改标题并确认（对话框不再搬旗标）。 */
async function setTitle(title) {
  openMenuForRow('s1')
  await drain()
  const item = [...document.querySelectorAll('.session-dropdown-item')]
    .find(b => b.textContent.includes('设置标题'))
  expect(item, '菜单里没有「设置标题」').toBeTruthy()
  item.click()
  await drain()
  // 对话框里没有旗标控件
  expect(document.querySelector('.title-dialog-flags')).toBeNull()
  setInput(document.querySelector('.title-dialog input'), title)
  document.querySelector('.title-dialog-confirm').click()
  await drain(20)
}

/** 某一行（主区域 + 末尾 ···） */
function submenuRow(text) {
  return [...document.querySelectorAll('.session-dropdown-submenu-row')]
    .find(r => r.querySelector('.session-dropdown-item')?.textContent.includes(text))
}
function moreBtnOf(text) {
  return submenuRow(text)?.querySelector('button.submenu-more-btn') || null
}
/** 悬停「设置标题」项末尾 ··· → 右侧旗标面板 */
async function openQuickFlagPanel() {
  openMenuForRow('s1')
  await drain()
  const btn = moreBtnOf('设置标题')
  expect(btn, '「设置标题」那行没有 ··· 入口').toBeTruthy()
  btn.dispatchEvent(new MouseEvent('mouseenter'))
  await drain()
  expect(document.querySelector('.session-flag-submenu'), '悬停 ··· 后没出现旗标面板').toBeTruthy()
}
/** 悬停「设置标签」项末尾 ··· → 右侧标签候选面板 */
async function openQuickLabelPanel() {
  openMenuForRow('s1')
  await drain()
  const btn = moreBtnOf('设置标签')
  expect(btn, '「设置标签」那行没有 ··· 入口').toBeTruthy()
  btn.dispatchEvent(new MouseEvent('mouseenter'))
  await drain()
  expect(document.querySelector('.session-label-submenu'), '悬停 ··· 后没出现标签面板').toBeTruthy()
}
function flagItemByChar(flag) {
  const items = [...document.querySelectorAll('.session-flag-submenu .session-flag-item')]
  if (flag === '') return items[0]                  // 竖向列表第一项 = 无旗标
  return items.find(b => b.querySelector('.session-flag-item-emoji')?.textContent.trim() === flag)
}
function labelItemByName(name) {
  return [...document.querySelectorAll('.session-label-submenu .session-label-item')]
    .find(b => b.querySelector('.session-label-name')?.textContent.trim() === name)
}
/** 点旗标面板里的某项（'' = 无旗标）：点选即落盘 */
async function setFlagViaQuickPanel(flag) {
  await openQuickFlagPanel()
  const item = flagItemByChar(flag)
  expect(item, `旗标面板里没有：${flag || '无旗标'}`).toBeTruthy()
  item.click()
  await drain(20)
}
/** 点标签候选面板里的某项：点选即追加并落盘（已持有则不变） */
async function appendLabelViaQuickPanel(label) {
  await openQuickLabelPanel()
  const item = labelItemByName(label)
  expect(item, `标签面板里没有：${label}`).toBeTruthy()
  item.click()
  await drain(20)
}

function itemNode(sessionId = 's1') {
  return document.querySelector(`.session-row[data-session-id="${sessionId}"] .session-item`)
}
function itemText(sessionId = 's1') { return itemNode(sessionId)?.textContent || '(no row)' }
function itemClass(sessionId = 's1') { return itemNode(sessionId)?.className || '(no row)' }
function rowFlag(sessionId = 's1') {
  return document.querySelector(`.session-row[data-session-id="${sessionId}"] .session-row-flag`)?.textContent || ''
}

function emit(frame) {
  expect(hub.push, 'SSE 订阅未建立').toBeTruthy()
  hub.push(frame)
}

beforeAll(() => {
  Element.prototype.getBoundingClientRect = () => ({
    width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0,
  })
  Object.defineProperty(window, 'innerHeight', { value: 800, configurable: true, writable: true })
  Object.defineProperty(window, 'innerWidth', { value: 1200, configurable: true, writable: true })
  vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ tree: [], sessions: [], has_more: false })))
})

function resetListMock() {
  api.sessions.list.mockReset()
  api.sessions.list.mockImplementation(async (page, size, category) => ({
    sessions: [{
      session_id: 's1', title: server.title, flag: server.flag,
      labels: [...server.labels], category: category || '',
    }],
    has_more: false,
  }))
}

afterEach(() => {
  if (instance) { unmount(instance); instance = null }
  if (host) { host.remove(); host = null }
  hub.push = null
  releaseStalePage = null
  server.flag = '🚩'
  server.title = '会话原标题'
  server.labels = ['旧标签']
  resetListMock()
  api.sessions.generateTitle.mockClear()
  api.sessions.setFlag.mockClear()
  api.sessions.setLabels.mockClear()
})

afterAll(() => {
  vi.unstubAllGlobals()
})

describe('同一会话跨分类的标题 / 旗标一致', () => {
  it('两个分类都加载过（各有缓存副本），在 A 里改名 / 换旗标 / 追加标签后切到 B 立刻一致', async () => {
    await mountSidebar()
    await selectCategory('1/1')
    expect(itemText()).toContain('会话原标题')
    expect(rowFlag()).toBe('🚩')
    await selectCategory('1/2')
    await selectCategory('1/1')          // B 的缓存副本留在原地

    // 标题走对话框（只改标题，不再写旗标），旗标走「设置标题」项末尾 ··· 的面板
    await setTitle('改一次，处处一致')
    await setFlagViaQuickPanel('🟡')
    expect(api.sessions.generateTitle).toHaveBeenCalledWith('s1', '改一次，处处一致', null)
    expect(api.sessions.setFlag).toHaveBeenCalledWith('s1', '🟡')
    expect(itemText()).toContain('改一次，处处一致')
    expect(rowFlag()).toBe('🟡')

    // 切到 B：命中缓存（行副本仍是改名前的旧值），事实层必须盖过它
    await selectCategory('1/2')
    expect(itemText()).toContain('改一次，处处一致')
    expect(rowFlag()).toBe('🟡')

    // 清除旗标也在 ··· 面板里做：写盘后 A / B 两个分类的行副本一起消失
    await setFlagViaQuickPanel('')
    expect(api.sessions.setFlag).toHaveBeenLastCalledWith('s1', '')
    expect(rowFlag()).toBe('')
    await selectCategory('1/1')
    expect(rowFlag()).toBe('')

    // 标签追加也在 ··· 面板里做：在 A 里追加 → 切到 B 也已经是「已持有」
    await appendLabelViaQuickPanel('新标签')
    expect(api.sessions.setLabels).toHaveBeenCalledWith('s1', ['旧标签', '新标签'])
    await selectCategory('1/2')
    await openQuickLabelPanel()
    expect(labelItemByName('新标签')?.getAttribute('aria-checked')).toBe('true')
    expect(labelItemByName('旧标签')?.getAttribute('aria-checked')).toBe('true')
    // 已持有的点一下不会被删掉
    const heldBefore = api.sessions.setLabels.mock.calls.length
    labelItemByName('旧标签').click()
    await drain(20)
    expect(api.sessions.setLabels.mock.calls.length).toBe(heldBefore)
    expect(labelItemByName('旧标签')?.getAttribute('aria-checked')).toBe('true')
  })
})

describe('晚到的列表响应不得覆盖新状态', () => {
  it('翻页响应在途时改名 / 换旗标 / 开始推理，旧页落地后不回退', async () => {
    // 第 1 页正常返回；第 2 页是「取数更早、落地更晚」的旧快照
    const staleRow = (category) => ({
      session_id: 's1', title: '会话原标题', flag: '🚩', labels: ['旧标签'],
      category, _status: 'done_success_unread',      // 上一轮的绿色
    })
    api.sessions.list.mockImplementation(async (page, size, category) => {
      if (page > 1) {
        return new Promise(resolve => {
          releaseStalePage = () => resolve({ sessions: [staleRow(category)], has_more: false })
        })
      }
      return { sessions: [staleRow(category)], has_more: true }
    })

    await mountSidebar()
    await selectCategory('1/1')
    expect(itemText()).toContain('会话原标题')

    // 触发翻页（jsdom 里列表高度为 0，滚动即视为触底）
    document.querySelector('.session-list').dispatchEvent(new Event('scroll'))
    await drain()
    expect(releaseStalePage, '翻页请求未发出').toBeTruthy()

    // 旧页还在途时：用户改名换旗标（写请求正常返回）+ 后端开始新一轮推理
    await setTitle('翻页期间改的名')
    await setFlagViaQuickPanel('🟠')
    emit({ event: 'message', session_id: 's1', status: 'streaming' })
    await drain()

    releaseStalePage?.()             // 晚到的旧快照此时才落地
    releaseStalePage = null
    await drain(24)

    expect(itemText()).toContain('翻页期间改的名')
    expect(rowFlag()).toBe('🟠')
    expect(itemClass()).toContain('status-streaming')
    expect(itemClass()).not.toContain('status-done-success-unread')
    // 用户自己定的标题不会被快照里的旧标题降级
    expect(api.sessions.generateTitle).toHaveBeenCalledTimes(1)
  })

  it('在 B 分类里开始推理，切回 A 分类也是同一个状态（状态不按分类各存一份）', async () => {
    await mountSidebar()
    await selectCategory('1/2')
    emit({ event: 'message', session_id: 's1', status: 'streaming' })
    await drain()
    expect(itemClass()).toContain('status-streaming')

    await selectCategory('1/1')
    expect(itemClass()).toContain('status-streaming')

    emit({ event: 'message', session_id: 's1', status: 'done_success_unread', title: '跑完了' })
    await drain()
    expect(itemClass()).toContain('status-done-success-unread')
    expect(itemText()).toContain('跑完了')
  })
})
