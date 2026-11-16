// @vitest-environment jsdom
/**
 * SessionCategoryNode 森林交互测试（v2 多根结构）。
 *
 * 目录树现在混合三种节点类型：category（默认/旧数据）、workspace（工作区
 * 事实分组，点击行为与分类叶子一致）、terminal（"本地终端"动作节点）。
 * 终端节点的语义与其余节点相反：单击只保留给树展开（有子节点时），绝不
 * 进入会话列表视图；双击才触发打开终端窗口的回调。
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { mount, unmount, flushSync } from 'svelte'
import SessionCategoryNode from './SessionCategoryNode.svelte'

let host = null
let instance = null
let props = null

function open(node, extra = {}) {
  host = document.createElement('div')
  document.body.appendChild(host)
  props = {
    node,
    expandedPaths: new Set(),
    selectedCategory: '',
    onToggle: vi.fn(),
    onSelect: vi.fn(),
    onOpenTerminalNode: vi.fn(),
    ...extra,
  }
  instance = mount(SessionCategoryNode, { target: host, props })
  flushSync()
}

function row() {
  return document.querySelector('.category-row')
}

afterEach(() => {
  instance && unmount(instance)
  instance = null
  host?.remove()
  host = null
})

const CATEGORY_LEAF = {
  id: 1, name: '分类叶子', type: 'category', category: '1/1',
  children: ['s1'], session_count: 1,
}
const WORKSPACE_LEAF = {
  id: 1, name: 'agents', type: 'workspace', workspace: '/cc/agents', category: '2/1',
  children: ['s1'], session_count: 1,
}
const TERMINAL_ROOT = {
  id: 3, name: '本地终端', type: 'terminal', category: '3',
  children: [], session_count: 0,
}
const ENVIRONMENT_NODE = {
  id: 1, name: '本地', type: 'environment', environment: 'local', category: '2/1',
  children: [WORKSPACE_LEAF], session_count: 1,
}
const RECENT_ROOT = {
  id: 4, name: '最近会话', type: 'recent', category: '4',
  children: ['s1'], session_count: 1,
}
const MONTH_LEAF = {
  id: 2, name: '202608', type: 'month', category: '5/2',
  children: ['s1', 's2'], session_count: 2,
}

describe('SessionCategoryNode forest interactions', () => {
  it('clicking a category leaf selects it', () => {
    open(CATEGORY_LEAF)
    row().click()
    expect(props.onSelect).toHaveBeenCalledWith('1/1')
    expect(props.onToggle).not.toHaveBeenCalled()
  })

  it('workspace leaf behaves like a category leaf (loads its sessions)', () => {
    open(WORKSPACE_LEAF)
    row().click()
    expect(props.onSelect).toHaveBeenCalledWith('2/1')
    // 工作区节点带类型徽标，title 显示完整工作区路径
    expect(document.querySelector('.category-kind')).toBeTruthy()
    expect(row().getAttribute('title')).toBe('/cc/agents')
  })

  it('terminal node: single click never selects and never toggles when empty', () => {
    open(TERMINAL_ROOT)
    row().click()
    expect(props.onSelect).not.toHaveBeenCalled()
    expect(props.onToggle).not.toHaveBeenCalled()
    // 动作节点不显示会话计数
    expect(document.querySelector('.category-count')).toBeFalsy()
  })

  it('terminal node: double click opens the local terminal', () => {
    open(TERMINAL_ROOT)
    row().dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
    expect(props.onOpenTerminalNode).toHaveBeenCalledTimes(1)
    const [node, path] = props.onOpenTerminalNode.mock.calls[0]
    expect(node.name).toBe('本地终端')
    expect(path).toBe('3')
  })

  it('terminal node with children: single click expands the tree', () => {
    const withKids = {
      ...TERMINAL_ROOT,
      children: [{ id: 1, name: 'prod-server', type: 'terminal', children: [] }],
    }
    open(withKids)
    row().click()
    expect(props.onToggle).toHaveBeenCalledWith('3')
    expect(props.onSelect).not.toHaveBeenCalled()
  })

  it('recent root is a clickable leaf that loads the capped recent list', () => {
    open(RECENT_ROOT)
    row().click()
    expect(props.onSelect).toHaveBeenCalledWith('4')
    expect(props.onToggle).not.toHaveBeenCalled()
    // 最近会话根显示计数（≤ 上限）
    expect(document.querySelector('.category-count')?.textContent).toBe('1')
  })

  it('month leaf loads that month sessions; history root only expands', () => {
    open(MONTH_LEAF)
    row().click()
    expect(props.onSelect).toHaveBeenCalledWith('5/2')
    unmount(instance)
    instance = null
    open({
      id: 5, name: '会话历史', type: 'history', category: '5',
      children: [MONTH_LEAF], session_count: 2,
    })
    row().click()
    expect(props.onToggle).toHaveBeenCalledWith('5')
    expect(props.onSelect).not.toHaveBeenCalled()
  })

  it('environment node only expands and carries its env key in the tooltip', () => {
    open(ENVIRONMENT_NODE)
    row().click()
    expect(props.onToggle).toHaveBeenCalledWith('2/1')
    expect(props.onSelect).not.toHaveBeenCalled()
    expect(row().getAttribute('title')).toBe('local')
  })

  it('legacy nodes without an explicit type default to category', () => {
    const legacy = { id: 1, name: '旧数据', category: '1', children: ['s1'], session_count: 1 }
    open(legacy)
    row().click()
    expect(props.onSelect).toHaveBeenCalledWith('1')
    // 森林根已折进模式图标行：任何 category 节点（含提为顶层的一级分类）都不带图标
    expect(document.querySelector('.category-kind')).toBeFalsy()
    unmount(instance); instance = null; host?.remove()
    open(legacy, { depth: 1 })
    expect(document.querySelector('.category-kind')).toBeFalsy()
  })
})

describe('远程终端节点', () => {
  it('双击把 env_id 原样传给打开终端的回调（mux 按 env_id + session 路由）', () => {
    open({
      id: 2, name: '192.168.2.5:7989', type: 'terminal',
      environment: '192.168.2.5:7989', env_id: 'http://192.168.2.5:7989',
      category: '3/2', children: [], session_count: 0,
    })
    row().dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
    expect(props.onOpenTerminalNode).toHaveBeenCalledTimes(1)
    const [node] = props.onOpenTerminalNode.mock.calls[0]
    expect(node.env_id).toBe('http://192.168.2.5:7989')
  })

  it('本地终端节点（env_id 为空串）双击传空 env，落回本地通道', () => {
    open({
      id: 1, name: '本地', type: 'terminal', environment: 'local', env_id: '',
      category: '3/1', children: [], session_count: 0,
    })
    row().dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
    const [node] = props.onOpenTerminalNode.mock.calls[0]
    expect(node.env_id || '').toBe('')
  })
})

describe('嵌套终端层（terminal_layer）', () => {
  const LAYER = {
    id: 9, name: 'WSL', type: 'terminal_layer', uid: 'abc123', command: 'wsl',
    category: '3/2/9', children: [], session_count: 0,
  }
  const ENV = {
    id: 2, name: 'Office(Win)', type: 'terminal', environment: 'x:7989',
    env_id: 'http://x:7989', category: '3/2', children: [LAYER], session_count: 0,
  }
  const ROOT = {
    id: 3, name: '终端', type: 'terminal', category: '3',
    children: [ENV], session_count: 0,
  }

  it('终端层双击把节点与 id 路径传给打开终端回调（链由调用方按路径收集）', () => {
    open(LAYER)
    row().dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
    expect(props.onOpenTerminalNode).toHaveBeenCalledTimes(1)
    const [node, path] = props.onOpenTerminalNode.mock.calls[0]
    expect(node.command).toBe('wsl')
    expect(path).toBe('3/2/9')
  })

  it('终端层与环境的右键触发节点菜单回调（携带鼠标坐标）', () => {
    open(LAYER, { onNodeContextmenu: vi.fn() })
    row().dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, clientX: 40, clientY: 60 }))
    expect(props.onNodeContextmenu).toHaveBeenCalledTimes(1)
    const [node, path, event] = props.onNodeContextmenu.mock.calls[0]
    expect(node.uid).toBe('abc123')
    expect(path).toBe('3/2/9')
    expect(event.clientX).toBe(40)
    unmount(instance); instance = null; host?.remove()
    open(ENV, { onNodeContextmenu: vi.fn() })
    row().dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, clientX: 1, clientY: 2 }))
    expect(props.onNodeContextmenu).toHaveBeenCalledTimes(1)
  })

  it('终端根不触发节点菜单（环境节点来自注册配置，不可增删）', () => {
    open(ROOT, { onNodeContextmenu: vi.fn() })
    row().dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }))
    expect(props.onNodeContextmenu).not.toHaveBeenCalled()
  })

  it('单环境形态的"本地终端"根自己就是环境节点：右键给出节点菜单', () => {
    // 没有登记远程环境时树里只有一个根"本地终端"，终端层直接挂在它下面。
    // 它是这一形态下唯一的新建入口，右键必须可用（多环境形态的根仍不可）。
    const LOCAL_ROOT = {
      id: 3, name: '本地终端', type: 'terminal', category: '3',
      environment: 'local', env_id: '',
      children: [{ id: 9, name: 'WSL', type: 'terminal_layer', uid: 'l1', command: 'wsl', children: [] }],
      session_count: 0,
    }
    open(LOCAL_ROOT, { onNodeContextmenu: vi.fn(), expandedPaths: new Set(['3']) })
    row().dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, clientX: 5, clientY: 7 }))
    expect(props.onNodeContextmenu).toHaveBeenCalledTimes(1)
    const [node, path] = props.onNodeContextmenu.mock.calls[0]
    expect(node.type).toBe('terminal')
    expect(path).toBe('3')
    // 层级终端层挂在根下：路径 3/9
    expect(document.querySelector('[data-category-path="3/9"]')).toBeTruthy()

    // 旧数据的根上可能没有 env/environment 字段：没有环境子级同样按环境节点处理。
    unmount(instance); instance = null; host?.remove()
    const { environment: _env, env_id: _envId, ...legacy } = LOCAL_ROOT
    open(legacy, { onNodeContextmenu: vi.fn() })
    row().dispatchEvent(new MouseEvent('contextmenu', { bubbles: true }))
    expect(props.onNodeContextmenu).toHaveBeenCalledTimes(1)
  })

  it('终端层 tooltip 显示进入指令；终端根/环境保持原有 title 语义', () => {
    open(LAYER)
    expect(row().getAttribute('title')).toBe('wsl')
    unmount(instance); instance = null; host?.remove()
    open(ENV)
    expect(row().getAttribute('title')).toBe('x:7989')
  })
})
