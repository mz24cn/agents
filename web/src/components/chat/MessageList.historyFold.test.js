// @vitest-environment jsdom
/**
 * MessageList 的历史折叠窗口（lib/message-history-fold.js 的 DOM 侧行为）。
 *
 * 要守住的几点：
 *   - 进入一条用户消息多于 3 条的会话时，只渲染第一轮和最后两轮：中间轮次连 user 消息、
 *     紧凑/详细回复都根本不进 DOM —— 这才是「加速展示」，不是只把样式藏起来。
 *   - 第一轮问答之后插一条全宽横幅说明省略了多少轮/多少条，并给出「点击显示完整会话历史」。
 *   - 点横幅之后中间轮次全部补上，横幅退场。
 *   - 折叠只决定「渲不渲染」，messages 下标一个不动：fork / retry / 撤销 / 跳转锚点仍然
 *     按原始下标寻址（data-start-index、data-user-message-index）。
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { mount, unmount, flushSync } from 'svelte'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import MessageList from './MessageList.svelte'
import { setLang } from '../../lib/i18n.svelte.js'

let host = null
let instance = null

function render(messages, props = {}) {
  host = document.createElement('div')
  document.body.appendChild(host)
  instance = mount(MessageList, { target: host, props: { messages, ...props } })
  flushSync()
}

afterEach(() => {
  instance && unmount(instance)
  instance = null
  host?.remove()
  host = null
  document.body.innerHTML = ''
})

/** 一轮问答：一条 user + 一条 assistant，文本按轮次编号，便于断言渲没渲染。 */
function turn(n) {
  return [
    { role: 'user', content: `问题${n}`, timestamp: `2026-01-01T00:00:0${n}.000Z` },
    { role: 'assistant', content: `回答${n}`, timestamp: `2026-01-01T00:00:1${n}.000Z` },
  ]
}

function conversation(turnCount) {
  return Array.from({ length: turnCount }, (_, i) => turn(i + 1)).flat()
}

const visibleGroups = () => Array.from(document.querySelectorAll('.group')).map(el => Number(el.dataset.startIndex))
const banner = () => document.querySelector('button.history-fold')

describe('MessageList — 长会话默认只渲染第一轮和最后两轮', () => {
  beforeEach(() => setLang('zh'))

  it('4 条用户消息：中间那轮完全不进 DOM，由横幅代替', () => {
    render(conversation(4))
    expect(visibleGroups()).toEqual([0, 4, 6])
    const text = document.body.textContent
    expect(text).toContain('问题1')
    expect(text).toContain('回答4')
    expect(text).not.toContain('问题2')
    expect(text).not.toContain('回答2')
  })

  it('第一轮问答之后是一条全宽横幅，写清省略了多少轮/多少条', () => {
    render(conversation(6))
    const fold = banner()
    expect(fold).not.toBeNull()
    expect(fold.textContent).toContain('已省略中间 3 轮对话（6 条消息）')
    expect(fold.textContent).toContain('点击显示完整会话历史')
    const list = document.querySelector('.message-list')
    // 横幅挂在列表里、样式守在全宽单行那条规则上（见下面 describe）
    expect(fold.classList.contains('history-fold')).toBe(true)
    expect(list.contains(fold)).toBe(true)
  })

  it('横幅插在第一次问答之后，其余轮次保持原顺序', () => {
    render(conversation(5))
    const list = document.querySelector('.message-list')
    const seq = Array.from(list.children).map(el =>
      el.classList.contains('history-fold') ? 'fold' : `turn:${el.dataset.startIndex}`,
    )
    expect(seq).toEqual(['turn:0', 'fold', 'turn:6', 'turn:8'])
  })

  it('点击横幅：中间轮次的用户消息和回复全都补上，横幅退场', () => {
    render(conversation(4))
    banner().click()
    flushSync()
    expect(banner()).toBeNull()
    expect(visibleGroups()).toEqual([0, 2, 4, 6])
    const text = document.body.textContent
    expect(text).toContain('问题2')
    expect(text).toContain('回答2')
  })

  it('用户消息只有 3 条时不折叠（阈值是「大于 3 条」）', () => {
    render(conversation(3))
    expect(banner()).toBeNull()
    expect(visibleGroups()).toEqual([0, 2, 4])
  })

  it('详细模式同样省：中间轮次的 assistant 卡片也不渲染，点开后补上', () => {
    render(conversation(5), { displayMessageDetails: true })
    expect(visibleGroups()).toEqual([0, 6, 8])
    expect(document.body.textContent).not.toContain('回答3')
    banner().click()
    flushSync()
    expect(document.body.textContent).toContain('回答3')
    expect(document.body.textContent).toContain('问题3')
  })

  it('折叠不改下标：可见轮次仍按原始 messages 下标寻址', () => {
    render(conversation(4))
    // fork / retry / 撤销 / 跳转都靠这些原始下标锚点，过滤数组会把它们全部错位。
    expect(document.querySelector('.group[data-start-index="6"]')).not.toBeNull()
    expect(document.querySelector('[data-user-message-index="6"]')).not.toBeNull()
    expect(document.querySelector('[data-user-message-index="2"]')).toBeNull()
  })
})

describe('横幅样式：全宽单行（jsdom 不注入 scoped 样式，只能守源文件）', () => {
  const source = readFileSync(join(__dirname, 'MessageList.svelte'), 'utf8')
  const rule = source.match(/\.history-fold\s*\{([^}]*)\}/)

  it('.history-fold 铺满列表宽度且只有一行', () => {
    expect(rule, 'MessageList.svelte 需要保留 .history-fold 样式').toBeTruthy()
    expect(rule[1]).toMatch(/align-self:\s*stretch/)
    expect(rule[1]).toMatch(/width:\s*100%/)
    expect(rule[1]).toMatch(/white-space:\s*nowrap/)
  })
})
