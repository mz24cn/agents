/**
 * 会话 meta → 「这条会话要不要把终端窗口恢复出来」。
 *
 * 判据是持久化在 conversation.json 里的 ``terminal: "enabled"``：后端在终端真的
 * 被打开那一刻写（runtime/server_state.py 的 mark_session_terminal），双击销毁时
 * 删掉。工具集里有没有 exec_cli 都不算 —— 模型可能一次都没调，终端就没开过。
 */
import { describe, it, expect } from 'vitest'
import {
  TERMINAL_META_ENABLED,
  TERMINAL_META_KEY,
  sessionHasTerminal,
  unconnectedTerminal,
} from './session-terminal.js'

describe('sessionHasTerminal', () => {
  it('没有 meta / 空 meta 都没有终端', () => {
    expect(sessionHasTerminal(null)).toBe(false)
    expect(sessionHasTerminal(undefined)).toBe(false)
    expect(sessionHasTerminal({})).toBe(false)
    expect(sessionHasTerminal('nope')).toBe(false)
  })

  it('terminal="enabled" 是判据', () => {
    expect(sessionHasTerminal({ terminal: 'enabled' })).toBe(true)
  })

  it('缺字段就是它的否定形式', () => {
    // 后端只在打开过时写这个字段，销毁时又删掉；早于这次改动存下的会话也没有，
    // 一律读成「这条会话没有终端窗口」，不去凭空起一个 shell。
    expect(sessionHasTerminal({ tool_ids: ['read_file'] })).toBe(false)
  })

  it('脏值不当真：只认那个字符串', () => {
    // meta 是磁盘上可读写手改的 JSON。
    expect(sessionHasTerminal({ terminal: true })).toBe(false)
    expect(sessionHasTerminal({ terminal: 'true' })).toBe(false)
    expect(sessionHasTerminal({ terminal: 'disabled' })).toBe(false)
    expect(sessionHasTerminal({ terminal: null })).toBe(false)
  })

  it('工具集里有 exec_cli 也不算：记的是终端打开过，不是允许用终端', () => {
    expect(sessionHasTerminal({ tool_ids: ['read_file', 'exec_cli'] })).toBe(false)
    // 反过来：标记说了就作数，哪怕这一轮的 tool_ids 里没有 exec_cli。
    expect(sessionHasTerminal({ terminal: 'enabled', tool_ids: ['read_file'] })).toBe(true)
  })

  it('键与值跟后端同名同值', () => {
    expect(TERMINAL_META_KEY).toBe('terminal')
    expect(TERMINAL_META_ENABLED).toBe('enabled')
  })
})

describe('unconnectedTerminal', () => {
  it('是一个「未连接、未在加载」的条目', () => {
    expect(unconnectedTerminal('s-1')).toEqual({
      ref: null,
      chain: [],
      status: { connected: false, error: null, loading: false, terminalId: 's-1' },
    })
  })
})
