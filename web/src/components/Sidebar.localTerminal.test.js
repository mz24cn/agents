// @vitest-environment jsdom
/**
 * 只有本地环境时的嵌套终端入口（单环境形态）。
 *
 * 没有登记任何远程环境时，森林里的终端根"本地终端"本身就是可连接的本地环境
 * 节点，用户自建的嵌套终端层（terminal_layer）直接挂在它下面。侧边栏必须：
 *   1. 在终端模式下渲染这一根行（否则连第一个嵌套终端都无处创建）；
 *   2. 对它右键给出"新建子终端"，并以 env_id='' 提交（后端据此定位到根）；
 *   3. 根下的层级终端层路径为 3/<id>，双击打开本地终端而不是会话列表。
 */
import { describe, it, expect, beforeAll, afterAll, afterEach, vi } from 'vitest'
import { mount, unmount, flushSync } from 'svelte'
import Sidebar from './Sidebar.svelte'

const LOCAL_TREE = [
  {
    id: 3, name: '本地终端', type: 'terminal', environment: 'local', env_id: '',
    session_count: 0,
    children: [
      {
        id: 9, uid: 'u9', name: '容器[cc]', type: 'terminal_layer',
        command: 'docker exec -it cc bash', children: [],
      },
    ],
  },
]

const posts = []

function jsonResponse(payload) {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
    body: { getReader: () => ({ read: async () => ({ done: true, value: undefined }) }) },
  }
}

function fakeFetch(path, opts = {}) {
  const url = String(path)
  if (opts.method === 'POST' && url.includes('/v1/sessions/tree/terminal/layer')) {
    const body = JSON.parse(opts.body || '{}')
    posts.push({ url, body })
    return jsonResponse({ ok: true, node: { id: 99, uid: 'u99', type: 'terminal_layer', name: 'WSL', command: body.command } })
  }
  if (url.startsWith('/v1/sessions/events')) return jsonResponse({})
  if (url.startsWith('/v1/sessions/tree')) return jsonResponse({ tree: LOCAL_TREE })
  if (/\/v1\/sessions\/[^/?]+/.test(url)) return jsonResponse({ messages: [], meta: null })
  return jsonResponse({})
}

let host = null
let instance = null

async function drain(rounds = 12) {
  for (let i = 0; i < rounds; i++) await Promise.resolve()
  await flushSync()
}

async function mountInTerminalMode() {
  host = document.createElement('div')
  document.body.appendChild(host)
  instance = mount(Sidebar, { target: host })
  await drain()
  const buttons = document.querySelectorAll('.session-panel-heading .mode-btn')
  buttons[4].click()   // 终端模式
  await drain()
  return buttons
}

beforeAll(() => {
  Element.prototype.getBoundingClientRect = () => ({
    width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0,
  })
  Object.defineProperty(window, 'innerHeight', { value: 800, configurable: true, writable: true })
  Object.defineProperty(window, 'innerWidth', { value: 1200, configurable: true, writable: true })
  vi.stubGlobal('fetch', vi.fn(async (path, opts) => fakeFetch(path, opts)))
})

afterEach(() => {
  if (instance) unmount(instance)
  instance = null
  host?.remove()
  host = null
  posts.length = 0
})

afterAll(() => {
  vi.unstubAllGlobals()
})

describe('单环境形态的本地嵌套终端', () => {
  it('终端模式渲染本地终端根行，右键"新建子终端"以 env_id="" 提交', async () => {
    await mountInTerminalMode()

    // 根行必须存在：它是这条形态下唯一的新建入口
    const rootRow = document.querySelector('[data-category-path="3"]')
    expect(rootRow).toBeTruthy()
    expect(rootRow.textContent).toContain('本地终端')

    rootRow.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, clientX: 20, clientY: 30 }))
    await drain()
    const menu = document.querySelector('.terminal-node-menu')
    expect(menu).toBeTruthy()
    const items = [...menu.querySelectorAll('.terminal-node-menu-item')]
    expect(items.length).toBe(1)                    // 根不是终端层：无编辑/删除
    items[0].click()
    await drain()

    const commandInput = document.querySelector('.terminal-layer-field input')
    expect(commandInput).toBeTruthy()
    commandInput.value = 'wsl'
    commandInput.dispatchEvent(new Event('input'))
    await drain()
    const primary = [...document.querySelectorAll('.terminal-layer-btn')]
      .find((b) => b.classList.contains('primary'))
    primary.click()
    await drain(20)

    expect(posts.length).toBe(1)
    expect(posts[0].url).toBe('/v1/sessions/tree/terminal/layer')
    expect(posts[0].body).toMatchObject({ env_id: '', command: 'wsl', parent_uid: '' })
    expect(document.querySelector('.terminal-layer-dialog')).toBeFalsy()
  })

  it('根下的层级终端层路径为 3/<id>，双击打开本地终端而不是会话列表', async () => {
    await mountInTerminalMode()
    // 展开根
    document.querySelector('[data-category-path="3"]').click()
    await drain()
    const layerRow = document.querySelector('[data-category-path="3/9"]')
    expect(layerRow).toBeTruthy()
    expect(layerRow.getAttribute('title')).toBe('docker exec -it cc bash')
    // 终端节点没有"末级分类 → 会话列表"语义
    expect(document.querySelector('.session-list')).toBeFalsy()
  })
})
