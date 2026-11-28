// @vitest-environment jsdom
/**
 * 会话「…」菜单里「设置标题」「设置标签」的便捷交互回归测试。
 *
 * 两项都是一个 submenu-row：
 *   * 主区域（左边整块）点击 → 打开原来的对话框。标题对话框只有标题输入框（不再有
 *     旗标控件，保存也绝不写旗标）；标签对话框只有输入框 + 保存/取消（不再有候选列表，
 *     删除标签只能在输入框里改完再保存）。
 *   * 末尾的 ··· → 悬停/点击打开菜单右侧子面板（同「用户消息」「所属目录」那套）。
 *     旗标面板：竖向列表 = 「无旗标」+ 🚩🏴🏳️🔴🟠🟡🟢🔵🟣🟤✅⚠️，点选即调 /flag 落盘；
 *     标签面板：全部标签候选，点一下即在当前会话标签后面追加并调 /labels 落盘，
 *     已持有的标签点了不删除也不重复。
 *
 * 旗标存在 index.json 的 flag 字段里（会话级），旧会话没有该字段也能正常读取与打开。
 */
import { describe, it, expect, beforeAll, afterAll, afterEach, vi } from 'vitest'
import { mount, unmount, flushSync } from 'svelte'
import Sidebar from './Sidebar.svelte'
import * as api from '$lib/api.js'
import { SESSION_FLAGS } from '$lib/session-flags.js'

vi.mock('$lib/api.js', async () => {
  const actual = await vi.importActual('$lib/api.js')
  // 模拟后端的"权威元数据"：flag/labels 只有一份（index.json），谁写谁改，读接口回传
  // 同一份 —— 测试才能区分"服务端真的变了"和"前端自己乐观显示"。
  const state = { flag: '\u{1F7E2}', labels: ['tts', '性能'] }
  return {
    ...actual,
    sessions: {
      ...actual.sessions,
      // s1：有标签也有旗标；legacy1：旧会话，index 条目里没有 labels/flag 字段。
      // 每次挂载都会 list 一次，顺带把"磁盘状态"复位，避免测试之间互相污染。
      list: vi.fn(async () => {
        state.flag = '\u{1F7E2}'
        state.labels = ['tts', '性能']
        return {
          sessions: [
            { session_id: 's1', title: '旧标题', labels: [...state.labels], flag: state.flag },
            { session_id: 'legacy1', title: 'legacy1' },
          ],
          has_more: false,
        }
      }),
      // 森林里的标签根：与 runtime/category_tree_builder.py 的节点结构一致
      // （type='labels' 的根 + type='label' 的叶子，叶子 children 是会话 ID 字符串）
      tree: vi.fn(async () => ({
        tree: [
          { id: 1, name: '主题', type: 'category', session_count: 2, children: [
            { id: 11, name: '子分类', type: 'category', session_count: 2, children: ['s1', 'legacy1'] },
          ] },
          { id: 6, name: '标签', type: 'labels', session_count: 2, children: [
            { id: 3, name: 'tts', type: 'label', session_count: 2, children: ['s1', 'legacy1'], stats: {} },
            { id: 2, name: '部署', type: 'label', session_count: 5, children: ['s1'], stats: {} },
            { id: 4, name: '告警', type: 'label', session_count: 3, children: ['s1'], stats: {} },
            { id: 1, name: '性能', type: 'label', session_count: 1, children: ['s1'], stats: {} },
          ] },
        ],
      })),
      generateTitle: vi.fn(async (sessionId, title) => ({
        status: 'success',
        session_id: sessionId,
        title: title || '模型生成标题',
        title_given: !!title,
        labels: state.labels,
        // 成功响应回带 index 里当前的 flag（改标题不碰旗标）
        flag: state.flag,
      })),
      setFlag: vi.fn(async (sessionId, flag) => {
        state.flag = flag
        return {
          status: 'success',
          session_id: sessionId,
          flag: state.flag,
          title: '旧标题',
          title_given: true,
          labels: state.labels,
        }
      }),
      setLabels: vi.fn(async (sessionId, labels) => {
        state.labels = labels
        return {
          status: 'success',
          session_id: sessionId,
          labels: state.labels,
          title: '旧标题',
          title_given: true,
          flag: state.flag,
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

function openMenuForRow(sessionId) {
  const row = document.querySelector(`.session-row[data-session-id="${sessionId}"]`)
  expect(row).toBeTruthy()
  row.querySelector('.session-menu-btn').click()
}

function menuItemByText(text) {
  return [...document.querySelectorAll('.session-dropdown-item')].find(b => b.textContent.includes(text))
}

/** 「设置标题」/「设置标签」那一行（主区域 + 末尾 ···） */
function submenuRow(text) {
  return [...document.querySelectorAll('.session-dropdown-submenu-row')]
    .find(r => r.querySelector('.session-dropdown-item')?.textContent.includes(text))
}

function moreBtnOf(text) {
  return submenuRow(text)?.querySelector('button.submenu-more-btn') || null
}

/** 悬停 ···（onmouseenter）—— 主区域不受影响，面板应打开 */
async function hoverMore(text) {
  const btn = moreBtnOf(text)
  expect(btn, `${text} 那行没有 ··· 入口`).toBeTruthy()
  btn.dispatchEvent(new MouseEvent('mouseenter'))
  await drain()
  return btn
}

function flagItems() {
  return [...document.querySelectorAll('.session-flag-submenu .session-flag-item')]
}
function flagItemByChar(flag) {
  if (flag === '') return flagItems()[0]        // 竖向列表第一项 = 无旗标
  return flagItems().find(b => b.querySelector('.session-flag-item-emoji')?.textContent.trim() === flag)
}
function labelItems() {
  return [...document.querySelectorAll('.session-label-submenu .session-label-item')]
}
function labelItemByName(name) {
  return labelItems().find(b => b.querySelector('.session-label-name')?.textContent.trim() === name)
}
function rowFlag(sessionId = 's1') {
  return document.querySelector(`.session-row[data-session-id="${sessionId}"] .session-row-flag`)?.textContent || ''
}
function setInput(input, value) {
  const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(input), 'value').set
  setter.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

afterEach(() => {
  if (instance) unmount(instance)
  instance = null
  host?.remove()
  host = null
  vi.clearAllMocks()
})

beforeAll(() => {
  Element.prototype.getBoundingClientRect = () => ({
    width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0,
  })
  Object.defineProperty(window, 'innerHeight', { value: 800, configurable: true, writable: true })
  Object.defineProperty(window, 'innerWidth', { value: 1200, configurable: true, writable: true })
  vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ tree: [], sessions: [], has_more: false })))
})

afterAll(() => {
  vi.unstubAllGlobals()
})

describe('会话菜单：主区域 + 末尾 ··· 的两行结构', () => {
  it('「设置标题」「设置标签」各自独立成项、相邻，且各带一个 ··· 入口', async () => {
    await mountSidebar()
    await drain()

    openMenuForRow('s1')
    await drain()

    const items = [...document.querySelectorAll('.session-dropdown-item')].map(b => b.textContent.trim())
    expect(items.filter(x => x.includes('设置标题'))).toHaveLength(1)
    expect(items.filter(x => x.includes('设置标签'))).toHaveLength(1)
    expect(items.some(x => x.includes('设置标题与标签'))).toBe(false)
    const titleIdx = items.findIndex(x => x.includes('设置标题'))
    expect(items[titleIdx + 1]).toContain('设置标签')
    // 菜单里其它项不能被挤掉
    for (const label of ['所属目录', '重新生成概要和记忆', '打开终端']) {
      expect(items.filter(x => x.includes(label))).toHaveLength(1)
    }
    // 两行各自有一个 ··· 入口；面板没打开时不存在面板节点
    expect(moreBtnOf('设置标题')).toBeTruthy()
    expect(moreBtnOf('设置标签')).toBeTruthy()
    expect(document.querySelector('.session-flag-submenu')).toBeNull()
    expect(document.querySelector('.session-label-submenu')).toBeNull()
  })

  it('两行的图标沿用原有字符：设置标题 🖋️、设置标签 🏷️（与标签节点/标签模式同一枚）', async () => {
    await mountSidebar()
    await drain()

    openMenuForRow('s1')
    await drain()

    // 拆分菜单项时不要顺手换图标：🖋️ 是原来「设置标题与标签」那一项的图标，
    // 🏷️ 是标签根/标签叶子/标签模式一直在用的图标。
    expect(menuItemByText('设置标题')?.querySelector('.menu-emoji')?.textContent)
      .toBe('\u{1F58B}\u{FE0F}')
    expect(menuItemByText('设置标签')?.querySelector('.menu-emoji')?.textContent)
      .toBe('\u{1F3F7}\u{FE0F}')
  })

  it('会话行按 index 里的 flag 显示旗标；旧会话没有该字段就不显示', async () => {
    await mountSidebar()
    await drain()

    expect(document.querySelector('.session-row[data-session-id="s1"] .session-row-flag')?.textContent)
      .toBe('\u{1F7E2}')
    expect(document.querySelector('.session-row[data-session-id="legacy1"] .session-row-flag')).toBeNull()
  })
})

describe('「设置标题」对话框：只有标题输入，保存绝不写旗标', () => {
  it('对话框里没有旗标控件，也读不到旗标草稿', async () => {
    await mountSidebar()
    await drain()

    openMenuForRow('s1')
    await drain()
    menuItemByText('设置标题').click()
    await drain()

    const inputs = document.querySelectorAll('.title-dialog input[type="text"]')
    expect(inputs).toHaveLength(1)
    expect(inputs[0].value).toBe('旧标题')
    // 旗标控件已从对话框移除（老的 .session-flag-btn / .title-dialog-flags 都不该再出现）
    expect(document.querySelectorAll('.title-dialog .session-flag-btn')).toHaveLength(0)
    expect(document.querySelectorAll('.title-dialog .session-flag-item')).toHaveLength(0)
    expect(document.querySelector('.title-dialog-flags')).toBeNull()
    // 标签输入也不在这个对话框里
    expect(inputs).toHaveLength(1)
  })

  it('保存标题：generateTitle 带 labels=null，绝不调用 setFlag / setLabels', async () => {
    await mountSidebar()
    await drain()

    openMenuForRow('s1')
    await drain()
    menuItemByText('设置标题').click()
    await drain()

    setInput(document.querySelector('.title-dialog input[type="text"]'), '只改标题')
    document.querySelector('.title-dialog-confirm').click()
    await drain(20)

    expect(api.sessions.generateTitle).toHaveBeenCalledTimes(1)
    const [sid, title, labels] = api.sessions.generateTitle.mock.calls[0]
    expect(sid).toBe('s1')
    expect(title).toBe('只改标题')
    // labels 传 null = 请求体不带 labels 键
    expect(labels).toBeNull()
    expect(api.sessions.setFlag).not.toHaveBeenCalled()
    expect(api.sessions.setLabels).not.toHaveBeenCalled()
    // 标题改了，旗标仍是服务端那份（🟢），没有被对话框顺手写掉
    await drain()
    expect(document.querySelector('.session-row[data-session-id="s1"] .session-item')?.textContent)
      .toContain('只改标题')
    expect(rowFlag()).toBe('\u{1F7E2}')
  })

  it('标题留空确定 = 模型生成，同样不写旗标', async () => {
    await mountSidebar()
    await drain()

    openMenuForRow('s1')
    await drain()
    menuItemByText('设置标题').click()
    await drain()

    setInput(document.querySelector('.title-dialog input[type="text"]'), '')
    document.querySelector('.title-dialog-confirm').click()
    await drain(20)

    expect(api.sessions.setFlag).not.toHaveBeenCalled()
    expect(api.sessions.generateTitle).toHaveBeenCalledTimes(1)
    expect(api.sessions.generateTitle.mock.calls[0][1]).toBe('')   // 空标题 = 让模型生成
    expect(rowFlag()).toBe('\u{1F7E2}')
  })

  it('标题请求失败：错误可见、对话框留着，旗标不会被前端改动', async () => {
    api.sessions.generateTitle.mockRejectedValueOnce(new Error('标题模型超时'))

    await mountSidebar()
    await drain()
    openMenuForRow('s1')
    await drain()
    menuItemByText('设置标题').click()
    await drain()

    setInput(document.querySelector('.title-dialog input[type="text"]'), '改不掉的名')
    document.querySelector('.title-dialog-confirm').click()
    await drain(20)

    expect(api.sessions.setFlag).not.toHaveBeenCalled()
    expect(document.querySelector('.title-dialog-error')?.textContent).toContain('标题模型超时')
    expect(document.querySelector('.title-dialog')).toBeTruthy()
    const row = document.querySelector('.session-row[data-session-id="s1"] .session-item')
    expect(row?.textContent).toContain('旧标题')
    expect(row?.textContent).not.toContain('改不掉的名')
    expect(rowFlag()).toBe('\u{1F7E2}')
  })
})

describe('「设置标题」末尾 ··· 的旗标面板：竖向列表 + 点选即存', () => {
  it('悬停 ··· 打开面板：竖向列表 = 无旗标 + 12 个旗标，当前旗标有选中标记', async () => {
    await mountSidebar()
    await drain()

    openMenuForRow('s1')
    await drain()
    await hoverMore('设置标题')

    const panel = document.querySelector('.session-flag-submenu')
    expect(panel, '悬停 ··· 后没有出现旗标面板').toBeTruthy()
    // 竖向列表（不是横向 chips）
    const list = panel.querySelector('.session-flag-list')
    expect(list).toBeTruthy()
    const items = flagItems()
    expect(items).toHaveLength(SESSION_FLAGS.length + 1)
    expect(items[0].textContent).toContain('无旗标')
    expect(items.slice(1).map(b => b.querySelector('.session-flag-item-emoji')?.textContent))
      .toEqual(SESSION_FLAGS)
    // 当前旗标 🟢 选中，且只有一项选中
    const checked = items.filter(b => b.getAttribute('aria-checked') === 'true')
    expect(checked).toHaveLength(1)
    expect(checked[0]).toBe(flagItemByChar('\u{1F7E2}'))
    expect(checked[0].className).toContain('active')
    // 面板是菜单的子节点：鼠标从入口移到面板不会触发菜单 mouseleave，所以不会关掉
    moreBtnOf('设置标题')?.dispatchEvent(new MouseEvent('mouseleave'))
    panel.dispatchEvent(new MouseEvent('mouseenter'))
    await drain()
    expect(document.querySelector('.session-flag-submenu')).toBeTruthy()
  })

  it('主区域点击打开对话框、不打开面板；点 ··· 打开面板、不打开对话框', async () => {
    await mountSidebar()
    await drain()

    openMenuForRow('s1')
    await drain()
    menuItemByText('设置标题').click()
    await drain()
    expect(document.querySelector('.title-dialog')).toBeTruthy()
    expect(document.querySelector('.session-flag-submenu')).toBeNull()
    expect(api.sessions.setFlag).not.toHaveBeenCalled()
    document.querySelector('.title-dialog-cancel').click()
    await drain()

    openMenuForRow('s1')
    await drain()
    moreBtnOf('设置标题').click()          // ··· 走自己的 handler，不串到主区域
    await drain()
    expect(document.querySelector('.session-flag-submenu')).toBeTruthy()
    expect(document.querySelector('.title-dialog')).toBeNull()
    expect(api.sessions.generateTitle).not.toHaveBeenCalled()
  })

  it('点旗标立刻写 /flag，行内旗标与面板选中态同步；再点当前旗标不重复写盘', async () => {
    await mountSidebar()
    await drain()

    openMenuForRow('s1')
    await drain()
    await hoverMore('设置标题')

    flagItemByChar(SESSION_FLAGS[0]).click()      // 🚩
    await drain(20)

    expect(api.sessions.setFlag).toHaveBeenCalledTimes(1)
    expect(api.sessions.setFlag.mock.calls[0]).toEqual(['s1', SESSION_FLAGS[0]])
    // 行内旗标 = 服务端回传值；面板保持打开且选中态跟着共享事实走
    expect(rowFlag()).toBe(SESSION_FLAGS[0])
    expect(document.querySelector('.session-flag-submenu')).toBeTruthy()
    expect(flagItemByChar(SESSION_FLAGS[0]).getAttribute('aria-checked')).toBe('true')
    expect(flagItems()[0].getAttribute('aria-checked')).toBe('false')
    // 标题/标签两条路径一概没动
    expect(api.sessions.generateTitle).not.toHaveBeenCalled()
    expect(api.sessions.setLabels).not.toHaveBeenCalled()

    flagItemByChar(SESSION_FLAGS[0]).click()      // 已是当前旗标：幂等，不写盘
    await drain(20)
    expect(api.sessions.setFlag).toHaveBeenCalledTimes(1)
  })

  it('点「无旗标」＝清除（提交空串），行内旗标消失', async () => {
    await mountSidebar()
    await drain()

    openMenuForRow('s1')
    await drain()
    await hoverMore('设置标题')

    flagItems()[0].click()
    await drain(20)

    expect(api.sessions.setFlag).toHaveBeenCalledTimes(1)
    expect(api.sessions.setFlag.mock.calls[0]).toEqual(['s1', ''])
    expect(rowFlag()).toBe('')
    expect(document.querySelector(`.session-row[data-session-id="s1"] .session-row-flag`)).toBeNull()
    expect(flagItems()[0].getAttribute('aria-checked')).toBe('true')
  })

  it('旗标写盘失败：面板里报错，行内旗标保持服务端原值（不乐观显示）', async () => {
    api.sessions.setFlag.mockRejectedValueOnce(new Error('旗标接口挂了'))

    await mountSidebar()
    await drain()
    openMenuForRow('s1')
    await drain()
    await hoverMore('设置标题')

    flagItemByChar(SESSION_FLAGS[0]).click()
    await drain(20)

    expect(document.querySelector('.session-flag-submenu .user-message-submenu-status.error')?.textContent)
      .toContain('旗标接口挂了')
    // 失败不乐观：旗标还是服务端那份（🟢），面板里选中标记也没变
    expect(rowFlag()).toBe('\u{1F7E2}')
    expect(flagItemByChar('\u{1F7E2}').getAttribute('aria-checked')).toBe('true')
    expect(flagItemByChar(SESSION_FLAGS[0]).getAttribute('aria-checked')).toBe('false')
    expect(flagItems()[0].getAttribute('aria-checked')).toBe('false')
  })

  it('旧会话（index 条目里没有 flag）打开面板：选中「无旗标」，不报错', async () => {
    await mountSidebar()
    await drain()

    openMenuForRow('legacy1')
    await drain()
    await hoverMore('设置标题')

    expect(flagItems()).toHaveLength(SESSION_FLAGS.length + 1)
    expect(flagItems()[0].getAttribute('aria-checked')).toBe('true')
    expect(flagItems().some(b => b !== flagItems()[0] && b.getAttribute('aria-checked') === 'true'))
      .toBe(false)
    expect(document.querySelector('.session-flag-submenu .user-message-submenu-status.error')).toBeNull()
  })

  it('两个 ··· 面板互斥：悬停另一个入口时旧面板立刻卸载', async () => {
    await mountSidebar()
    await drain()

    openMenuForRow('s1')
    await drain()
    await hoverMore('设置标签')
    expect(document.querySelector('.session-label-submenu')).toBeTruthy()

    await hoverMore('设置标题')
    expect(document.querySelector('.session-flag-submenu')).toBeTruthy()
    expect(document.querySelector('.session-label-submenu')).toBeNull()
  })

  it('菜单被点外部关掉时，旗标面板一起卸载', async () => {
    await mountSidebar()
    await drain()

    openMenuForRow('s1')
    await drain()
    await hoverMore('设置标题')
    expect(document.querySelector('.session-flag-submenu')).toBeTruthy()

    // <svelte:window onclick> = 点菜单外面一律 closeMenu（菜单项自己 stopPropagation 才不会被关掉）
    document.body.click()
    await drain()
    expect(document.querySelector('.session-dropdown')).toBeNull()
    expect(document.querySelector('.session-flag-submenu')).toBeNull()
  })
})

describe('「设置标签」对话框：只有输入框 + 保存/取消', () => {
  it('对话框里没有候选列表，也没有旗标控件', async () => {
    await mountSidebar()
    await drain()

    openMenuForRow('s1')
    await drain()
    menuItemByText('设置标签').click()
    await drain()

    const inputs = document.querySelectorAll('.title-dialog input[type="text"]')
    expect(inputs).toHaveLength(1)
    expect(inputs[0].value).toBe('tts, 性能')
    expect(document.querySelector('.label-candidates')).toBeNull()
    expect(document.querySelectorAll('.label-candidate')).toHaveLength(0)
    expect(document.querySelectorAll('.session-flag-btn, .session-flag-item')).toHaveLength(0)
    // 对话框标题跟着菜单项走 🏷️（菜单图标不要顺手换掉）
    expect(document.querySelector('.title-dialog-title')?.textContent.trim())
      .toBe('\u{1F3F7}\u{FE0F} 设置标签')
    // 常规保存/取消按钮仍在
    expect(document.querySelector('.title-dialog-confirm')).toBeTruthy()
    expect(document.querySelector('.title-dialog-cancel')).toBeTruthy()
  })

  it('输入框保存 = 整组替换（半角/全角逗号与空白都算分隔），清空保存 = 清空标签', async () => {
    await mountSidebar()
    await drain()

    openMenuForRow('s1')
    await drain()
    menuItemByText('设置标签').click()
    await drain()

    setInput(document.querySelector('.title-dialog input[type="text"]'), 'tts, 部署,tts,, ，')
    document.querySelector('.title-dialog-confirm').click()
    await drain(20)
    expect(api.sessions.setLabels).toHaveBeenCalledTimes(1)
    expect(api.sessions.setLabels.mock.calls[0]).toEqual(['s1', ['tts', '部署', 'tts']])
    // 只改标签：标题与旗标两条路径都不被调用
    expect(api.sessions.generateTitle).not.toHaveBeenCalled()
    expect(api.sessions.setFlag).not.toHaveBeenCalled()

    // 删除标签只能在输入框里改：这里清空并保存
    openMenuForRow('s1')
    await drain()
    menuItemByText('设置标签').click()
    await drain()
    const again = document.querySelector('.title-dialog input[type="text"]')
    expect(again.value).toBe('tts, 部署, tts')
    setInput(again, '')
    document.querySelector('.title-dialog-confirm').click()
    await drain(20)
    expect(api.sessions.setLabels).toHaveBeenCalledTimes(2)
    expect(api.sessions.setLabels.mock.calls[1][1]).toEqual([])
  })

  it('旧会话没有标签：输入框为空，对话框里没有候选', async () => {
    await mountSidebar()
    await drain()

    openMenuForRow('legacy1')
    await drain()
    menuItemByText('设置标签').click()
    await drain()

    expect(document.querySelector('.title-dialog input[type="text"]').value).toBe('')
    expect(document.querySelectorAll('.label-candidate')).toHaveLength(0)
  })
})

describe('「设置标签」末尾 ··· 的标签候选面板：点选即追加', () => {
  it('悬停 ··· 打开面板：候选来自全部标签节点（去重 + 稳定排序），已持有的有选中标记', async () => {
    await mountSidebar()
    await drain()

    openMenuForRow('s1')
    await drain()
    await hoverMore('设置标签')

    const panel = document.querySelector('.session-label-submenu')
    expect(panel, '悬停 ··· 后没有出现标签候选面板').toBeTruthy()
    // 稳定排序（zh 拼音在前，拉丁在后），会话 ID 不会被当成候选
    expect(labelItems().map(b => b.querySelector('.session-label-name')?.textContent))
      .toEqual(['部署', '告警', '性能', 'tts'])
    // 已持有（tts / 性能）标出来，未持有（部署 / 告警）没标记
    expect(labelItems().filter(b => b.getAttribute('aria-checked') === 'true')
      .map(b => b.querySelector('.session-label-name')?.textContent)).toEqual(['性能', 'tts'])
    expect(labelItemByName('tts').className).toContain('active')
    expect(labelItemByName('部署').className).not.toContain('active')
    // 主区域与 ··· 不串事件：面板开着，对话框没开
    expect(document.querySelector('.title-dialog')).toBeNull()
    // 悬停入口 → 面板不会因鼠标移出入口而关掉
    moreBtnOf('设置标签')?.dispatchEvent(new MouseEvent('mouseleave'))
    await drain()
    expect(document.querySelector('.session-label-submenu')).toBeTruthy()
  })

  it('点候选：在已有标签后面追加并写 /labels，已有标签不被删除', async () => {
    await mountSidebar()
    await drain()

    openMenuForRow('s1')
    await drain()
    await hoverMore('设置标签')

    labelItemByName('部署').click()
    await drain(20)

    expect(api.sessions.setLabels).toHaveBeenCalledTimes(1)
    expect(api.sessions.setLabels.mock.calls[0]).toEqual(['s1', ['tts', '性能', '部署']])
    // 面板里立刻变成"已持有"（共享事实驱动的选中态）
    expect(labelItemByName('部署').getAttribute('aria-checked')).toBe('true')
    // 旗标与标题两条路径一概没动
    expect(api.sessions.setFlag).not.toHaveBeenCalled()
    expect(api.sessions.generateTitle).not.toHaveBeenCalled()
  })

  it('点已持有的标签：既不删除也不重复（不发请求）', async () => {
    await mountSidebar()
    await drain()

    openMenuForRow('s1')
    await drain()
    await hoverMore('设置标签')

    labelItemByName('tts').click()
    await drain(20)
    expect(api.sessions.setLabels).not.toHaveBeenCalled()
    expect(labelItemByName('tts').getAttribute('aria-checked')).toBe('true')
  })

  it('连续点选不同候选：后一次以前一次落盘后的标签为基准，不会丢掉刚加的', async () => {
    await mountSidebar()
    await drain()

    openMenuForRow('s1')
    await drain()
    await hoverMore('设置标签')

    labelItemByName('部署').click()
    await drain(20)
    labelItemByName('tts').click()          // 已持有 → 不发请求
    labelItemByName('告警').click()
    await drain(20)

    expect(api.sessions.setLabels).toHaveBeenCalledTimes(2)
    expect(api.sessions.setLabels.mock.calls[0][1]).toEqual(['tts', '性能', '部署'])
    expect(api.sessions.setLabels.mock.calls[1][1]).toEqual(['tts', '性能', '部署', '告警'])
    expect(labelItems().filter(b => b.getAttribute('aria-checked') === 'true')
      .map(b => b.querySelector('.session-label-name')?.textContent)).toEqual(['部署', '告警', '性能', 'tts'])
  })

  it('追加失败：面板里报错，候选不被乐观标成已持有', async () => {
    api.sessions.setLabels.mockRejectedValueOnce(new Error('标签接口挂了'))

    await mountSidebar()
    await drain()
    openMenuForRow('s1')
    await drain()
    await hoverMore('设置标签')

    labelItemByName('部署').click()
    await drain(20)

    expect(document.querySelector('.session-label-submenu .user-message-submenu-status.error')?.textContent)
      .toContain('标签接口挂了')
    expect(labelItemByName('部署').getAttribute('aria-checked')).toBe('false')
    expect(labelItemByName('部署').className).not.toContain('active')
    // 已持有的那些不受影响
    expect(labelItemByName('tts').getAttribute('aria-checked')).toBe('true')
  })

  it('候选缺数据时按需加载森林；加载失败在面板里反馈', async () => {
    // 「最近」模式下挂载不拉森林（没有标签节点）→ 打开面板才补一次
    api.sessions.tree.mockReset()
    api.sessions.tree.mockRejectedValue(new Error('森林接口挂了'))

    await mountSidebar()
    await drain()
    expect(api.sessions.tree).not.toHaveBeenCalled()

    openMenuForRow('s1')
    await drain()
    await hoverMore('设置标签')

    expect(api.sessions.tree.mock.calls.length).toBeGreaterThanOrEqual(1)
    const panel = document.querySelector('.session-label-submenu')
    expect(panel, '打开 ··· 后没有出现标签候选面板').toBeTruthy()
    const status = panel.querySelector('.user-message-submenu-status.error')?.textContent || ''
    expect(status).toContain('加载标签候选失败')
    expect(status).toContain('森林接口挂了')
    expect(labelItems()).toHaveLength(0)
  })

  it('森林里没有标签节点时面板给出空态提示，不报错', async () => {
    api.sessions.tree.mockReset()
    api.sessions.tree.mockResolvedValue({ tree: [] })

    await mountSidebar()
    await drain()
    openMenuForRow('s1')
    await drain()
    await hoverMore('设置标签')

    expect(document.querySelector('.session-label-submenu .user-message-submenu-status')?.textContent)
      .toContain('暂无标签候选')
    expect(labelItems()).toHaveLength(0)
    expect(document.querySelector('.session-label-submenu .user-message-submenu-status.error')).toBeNull()
  })
})
