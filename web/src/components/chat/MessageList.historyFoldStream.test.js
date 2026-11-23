// @vitest-environment jsdom
/**
 * 推理中的会话（同一实例上流式追加消息）下，折叠窗口不能自己变大。
 *
 * 报告的现象：已经显示「为加速显示，已省略中间 N 轮」横幅时，最后一轮里再来一条工具调用
 * 结果消息，横幅后面会多出一轮 —— 尾部从「最后两轮」变成「最后三轮」。
 * 根因是快照按「进入时最后一轮的 endIndex」划界，而 endIndex 是扫描当刻的数组末尾，
 * 流式追加会让它越界，那一轮于是退出快照，尾窗整体往前挪一格。
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { mount, unmount, flushSync } from 'svelte'
import StreamHarness from '../../__fold_probe__/StreamHarness.svelte'
import { setLang } from '../../lib/i18n.svelte.js'

let host = null
let instance = null
let streamRef = null

/** 进入会话时的消息就是 initial；之后每一帧流式更新换一次 messages 引用。 */
function enter(messages) {
  streamRef = {}
  host = document.createElement('div')
  document.body.appendChild(host)
  instance = mount(StreamHarness, { target: host, props: { streamRef, initial: messages } })
  flushSync()
}

function streamFrame(messages) {
  streamRef.set(messages)
  flushSync()
}

afterEach(() => {
  instance && unmount(instance)
  instance = null
  streamRef = null
  host?.remove()
  host = null
  document.body.innerHTML = ''
})

function turn(n) {
  return [
    { role: 'user', content: `问题${n}`, timestamp: `2026-01-01T00:00:0${n}.000Z` },
    { role: 'assistant', content: `回答${n}`, timestamp: `2026-01-01T00:00:1${n}.000Z` },
  ]
}
const conversation = (n) => Array.from({ length: n }, (_, i) => turn(i + 1)).flat()

const visibleGroups = () => Array.from(document.querySelectorAll('.group')).map(el => Number(el.dataset.startIndex))
const banner = () => document.querySelector('button.history-fold')

describe('MessageList — 推理中的会话流式追加时折叠窗口保持稳定', () => {
  beforeEach(() => setLang('zh'))

  it('最后一轮里流进一次工具往返：仍是头 1 轮 + 尾 2 轮，横幅不多缩一轮', () => {
    const base = conversation(5)
    enter(base)
    const before = visibleGroups()
    expect(before).toEqual([0, 6, 8])
    const foldText = banner().textContent

    streamFrame([
      ...base,
      { role: 'assistant', content: '', tool_calls: [{ id: 'live1', name: 'read_file' }], timestamp: '2026-01-01T00:00:20.000Z' },
      { role: 'tool', content: 'ok', tool_use_id: 'live1', timestamp: '2026-01-01T00:00:21.000Z' },
    ])

    expect(visibleGroups()).toEqual(before)
    expect(document.body.textContent).not.toContain('问题3')
    expect(banner().textContent).toBe(foldText)
  })

  it('最后一轮连续来多条推理/工具帧，也不会一轮一轮地多吐出来', () => {
    const base = conversation(4)
    enter(base)
    const before = visibleGroups()
    expect(before).toEqual([0, 4, 6])

    let msgs = base
    for (let i = 0; i < 4; i++) {
      msgs = [...msgs,
        { role: 'assistant', content: '', tool_calls: [{ id: `c${i}`, name: 'exec_shell' }], timestamp: `2026-01-01T00:00:3${i}.000Z` },
        { role: 'tool', content: 'ok', tool_use_id: `c${i}`, timestamp: `2026-01-01T00:00:4${i}.000Z` }]
      streamFrame(msgs)
      expect(visibleGroups(), `第 ${i + 1} 次工具往返后`).toEqual(before)
    }
  })

  it('用户真的发起新一轮：新轮次渲染，进入时已可见的尾轮不被收走', () => {
    const base = conversation(5)
    enter(base)
    const foldText = banner().textContent
    streamFrame([...base, ...turn(6)])

    // 快照停在进入时的 5 轮：隐藏区仍是中间那两轮。新流进来的第 6 轮在快照之外一律渲染，
    // 而进入时就已经在看的尾轮不该被追溯收起 —— 否则用户刚聊完，上一轮回答就凭空消失。
    expect(visibleGroups()).toEqual([0, 6, 8, 10])
    expect(document.body.textContent).not.toContain('问题3')
    expect(document.body.textContent).toContain('问题6')
    expect(banner().textContent).toBe(foldText)
  })
})
