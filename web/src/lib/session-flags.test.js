/**
 * 会话旗标候选集回归测试。
 *
 * 旗标是「封闭枚举」：前端只提供这些按钮，后端 clean_flag 按同一候选集把关
 * （不在集合内 → 400）。两头的清单必须逐项一致，所以这里直接读
 * runtime/session_manager.py 里的 SESSION_FLAGS 字面量做比对，任何一侧偷偷
 * 加/删/改一个旗标都会红。
 *
 * 🏳️（U+1F3F3 U+FE0F）与 ⚠️（U+26A0 U+FE0F）带变体选择符 VS16，源码里很容易被
 * 编辑器"顺手"规范化掉，码点断言就是防这个的。
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { FLAG_NONE, SESSION_FLAGS, isKnownFlag, sameFlag, sessionFlagLabel } from './session-flags.js'

const PY_FLAGS = join(__dirname, '..', '..', '..', 'runtime', 'session_manager.py')

/** 从 session_manager.py 里抠出 SESSION_FLAGS 元组里的字面量。 */
function backendFlags() {
  const source = readFileSync(PY_FLAGS, 'utf8')
  const marker = 'SESSION_FLAGS: tuple[str, ...] = ('
  const start = source.indexOf(marker)
  if (start < 0) throw new Error('未找到 SESSION_FLAGS 定义 —— 前端候选集与后端可能已漂移')
  const end = source.indexOf(')', start)
  const block = source.slice(start + marker.length, end)
  return [...block.matchAll(/"([^"]*)"/g)].map(m => m[1])
}

describe('会话旗标候选集', () => {
  it('前端候选与后端 SESSION_FLAGS 逐项一致（含顺序与 VS16）', () => {
    expect(SESSION_FLAGS).toEqual(backendFlags())
  })

  it('需求里的 12 个旗标逐个按码点写死，防编辑器静默改写', () => {
    expect(SESSION_FLAGS).toEqual([
      '\u{1F6A9}',            // 🚩
      '\u{1F3F4}',            // 🏴
      '\u{1F3F3}\u{FE0F}',    // 🏳️（带 VS16）
      '\u{1F534}',            // 🔴
      '\u{1F7E0}',            // 🟠
      '\u{1F7E1}',            // 🟡
      '\u{1F7E2}',            // 🟢
      '\u{1F535}',            // 🔵
      '\u{1F7E3}',            // 🟣
      '\u{1F7E4}',            // 🟤
      '\u2705',               // ✅
      '\u26A0\uFE0F',         // ⚠️（带 VS16）
    ])
    expect(SESSION_FLAGS.length).toBe(12)
  })

  it('清除旗标就是空串（后端据此删掉 index.json 里的 flag 键）', () => {
    expect(FLAG_NONE).toBe('')
  })

  it('回显只承认候选集里的值，脏数据/旧会话一律空串', () => {
    expect(sessionFlagLabel('\u{1F6A9}')).toBe('\u{1F6A9}')
    expect(sessionFlagLabel('\u{1F3F3}')).toBe('\u{1F3F3}')   // 无 VS16 的历史写法照样认
    expect(sessionFlagLabel('')).toBe('')
    expect(sessionFlagLabel(undefined)).toBe('')
    expect(sessionFlagLabel('随便一个字符')).toBe('')
    expect(sessionFlagLabel(null)).toBe('')
  })

  it('同一旗标的 VS16 变体算同一个（老数据仍能在对话框里预选中）', () => {
    expect(sameFlag('\u{1F3F3}\u{FE0F}', '\u{1F3F3}')).toBe(true)
    expect(sameFlag('\u26A0\uFE0F', '\u26A0')).toBe(true)
    expect(sameFlag('', '')).toBe(true)
    expect(sameFlag('\u{1F6A9}', '\u{1F3F4}')).toBe(false)
    expect(sameFlag('\u{1F6A9}', '')).toBe(false)
    expect(isKnownFlag('\u{1F7E2}')).toBe(true)
    expect(isKnownFlag('🚩x')).toBe(false)
  })
})
