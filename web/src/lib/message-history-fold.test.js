/**
 * Tests for message-history-fold.js — 长会话进入时「第一轮 + 横幅 + 最后两轮」的窗口计算
 * 覆盖：轮次边界扫描、阈值（用户消息 > 3 条才折叠）、保留头 1 轮尾 2 轮、横幅落点、
 *       隐藏轮次里的消息条数、点开后全展开、进入会话时的快照不被后续新轮次影响。
 */
import { describe, it, expect } from 'vitest'
import fc from 'fast-check'
import {
  computeHistoryFold,
  scanTurns,
  snapshotBoundaryStart,
  turnContaining,
  turnsInSnapshot,
  HISTORY_FOLD_USER_THRESHOLD,
} from './message-history-fold.js'

/** 一条问答轮：user 之后跟一条 assistant（可带中间 tool 消息）。 */
function turn(label, { toolRounds = 0 } = {}) {
  const msgs = [{ role: 'user', content: `问 ${label}` }]
  for (let i = 0; i < toolRounds; i++) {
    msgs.push({ role: 'assistant', content: '', tool_calls: [{ id: `c${label}${i}`, name: 'read_file' }] })
    msgs.push({ role: 'tool', content: 'ok', tool_use_id: `c${label}${i}` })
  }
  msgs.push({ role: 'assistant', content: `答 ${label}` })
  return msgs
}

function conversation(turnCount, opts) {
  return Array.from({ length: turnCount }, (_, i) => turn(i + 1, opts)).flat()
}

describe('scanTurns — 按 user 消息切轮次', () => {
  it('每条 user 消息连同其后的 assistant/tool 是一轮，下标保持原始位置', () => {
    const turns = scanTurns(conversation(3))
    expect(turns).toHaveLength(3)
    expect(turns.map(t => [t.startIndex, t.endIndex, t.lastAssistantIndex])).toEqual([
      [0, 1, 1],
      [2, 3, 3],
      [4, 5, 5],
    ])
    expect(turns.every(t => !t.ungrouped)).toBe(true)
  })

  it('带工具往返的轮次把中间消息也圈在同一轮里', () => {
    const turns = scanTurns(conversation(2, { toolRounds: 2 }))
    // 一轮 = user + (assistant+tool)*2 + assistant = 6 条
    expect(turns.map(t => [t.startIndex, t.endIndex, t.lastAssistantIndex])).toEqual([
      [0, 5, 5],
      [6, 11, 11],
    ])
  })

  it('开头的 system 之类非 user 消息单列为 ungrouped，不参与折叠', () => {
    const turns = scanTurns([{ role: 'system', content: 'x' }, ...conversation(1)])
    expect(turns[0]).toMatchObject({ startIndex: 0, endIndex: 0, ungrouped: true })
    expect(turns[1]).toMatchObject({ startIndex: 1, endIndex: 2, ungrouped: false })
  })

  it('空会话得到空数组', () => {
    expect(scanTurns([])).toEqual([])
    expect(scanTurns(undefined)).toEqual([])
  })
})

describe('computeHistoryFold — 折叠窗口', () => {
  it('用户消息不超过 3 条时不折叠（阈值就是“大于 3 条”）', () => {
    for (let n = 0; n <= HISTORY_FOLD_USER_THRESHOLD; n++) {
      const fold = computeHistoryFold(scanTurns(conversation(n)))
      expect(fold.collapsed, `${n} 轮`).toBe(false)
      expect(fold.hiddenStarts.size).toBe(0)
      expect(fold.bannerAfterStart).toBeNull()
    }
  })

  it('4 轮：只省中间第 2 轮，第一轮和最后两轮照常显示', () => {
    const turns = scanTurns(conversation(4))
    const fold = computeHistoryFold(turns)
    expect(fold.collapsed).toBe(true)
    expect([...fold.hiddenStarts]).toEqual([turns[1].startIndex])
    expect(fold.hiddenTurns).toBe(1)
    expect(fold.hiddenMessages).toBe(2)             // 一轮 user + assistant
    expect(fold.bannerAfterStart).toBe(turns[0].startIndex)
  })

  it('6 轮：省中间 3 轮，横幅挂在第一轮之后', () => {
    const turns = scanTurns(conversation(6))
    const fold = computeHistoryFold(turns)
    expect([...fold.hiddenStarts]).toEqual([2, 4, 6])
    expect(fold.hiddenTurns).toBe(3)
    expect(fold.hiddenMessages).toBe(6)
    expect(fold.bannerAfterStart).toBe(0)
  })

  it('中间轮次里的工具往返也算「省略的消息」，条数如实报', () => {
    const turns = scanTurns(conversation(4, { toolRounds: 2 }))
    const fold = computeHistoryFold(turns)
    expect(fold.hiddenTurns).toBe(1)
    expect(fold.hiddenMessages).toBe(6)  // user + 2*(assistant+tool) + assistant
  })

  it('点过横幅（expanded）之后什么都不省', () => {
    const turns = scanTurns(conversation(9))
    const fold = computeHistoryFold(turns, { expanded: true })
    expect(fold.collapsed).toBe(false)
    expect(fold.hiddenStarts.size).toBe(0)
    expect(fold.bannerAfterStart).toBeNull()
  })

  it('快照之外的新轮次永不进隐藏区（会话中途继续聊不会把上一轮收走）', () => {
    const entered = scanTurns(conversation(5))
    const boundary = snapshotBoundaryStart(entered, null)
    expect(boundary).toBe(8)                       // 进入时最后一轮那条 user 的下标

    // 用户又发了一轮：进入时的快照不变，新轮次在快照之外。
    const grown = scanTurns(conversation(6))
    expect(snapshotBoundaryStart(grown, boundary)).toBe(boundary)
    expect(turnsInSnapshot(grown, boundary)).toHaveLength(5)

    const fold = computeHistoryFold(turnsInSnapshot(grown, boundary))
    // 快照里是 5 轮：省中间两轮（turn2、turn3），新流进来的第 6 轮不在快照里，照常渲染。
    expect([...fold.hiddenStarts]).toEqual([2, 4])
    expect(grown.at(-1).startIndex).toBe(10)
    expect(fold.hiddenStarts.has(10)).toBe(false)
  })

  it('快照只在第一次看见非空轮次时定下（会话消息是异步补进来的）', () => {
    let boundary = snapshotBoundaryStart([], null)
    expect(boundary).toBeNull()
    boundary = snapshotBoundaryStart(scanTurns(conversation(4)), boundary)
    expect(boundary).toBe(6)
    boundary = snapshotBoundaryStart(scanTurns(conversation(8)), boundary)
    expect(boundary).toBe(6)
    // 分界线还没定下来（消息没到）时没有快照轮次
    expect(turnsInSnapshot(scanTurns(conversation(3)), null)).toEqual([])
  })
})

describe('turnContaining — 某条消息落在哪一轮', () => {
  it('按轮的整区间命中（跳转/锚点用它先展开再滚）', () => {
    const turns = scanTurns(conversation(3, { toolRounds: 1 }))
    // 一轮 = user + assistant(tool_calls) + tool + assistant，共 4 条：[0..3] [4..7] [8..11]
    expect(turns.map(t => [t.startIndex, t.endIndex])).toEqual([[0, 3], [4, 7], [8, 11]])
    expect(turnContaining(turns, 3)?.startIndex).toBe(0)
    expect(turnContaining(turns, 9)?.startIndex).toBe(8)
    expect(turnContaining(turns, 99)).toBeNull()
  })
})

describe('折叠窗口不变量（随机会话）', () => {
  it('第一轮与最后两轮始终可见，隐藏轮数 = 总轮数 - 3', () => {
    fc.assert(
      fc.property(fc.integer({ min: 0, max: 40 }), (turnCount) => {
        const turns = scanTurns(conversation(turnCount))
        const fold = computeHistoryFold(turns)
        const visible = turns.filter(t => !t.ungrouped && !fold.hiddenStarts.has(t.startIndex))
        const hidden = turns.filter(t => fold.hiddenStarts.has(t.startIndex))

        // 头 1 轮、尾 2 轮一定在
        const head = turns.slice(0, 1)
        const tail = turns.slice(-2)
        for (const t of [...head, ...tail]) expect(fold.hiddenStarts.has(t.startIndex)).toBe(false)

        if (turnCount > HISTORY_FOLD_USER_THRESHOLD) {
          expect(fold.collapsed).toBe(true)
          expect(hidden).toHaveLength(turnCount - 3)
          expect(visible).toHaveLength(3)
          // 横幅只在第一轮之后出现一次
          expect(turns.filter(t => t.startIndex === fold.bannerAfterStart)).toHaveLength(1)
        } else {
          expect(fold.collapsed).toBe(false)
          expect(hidden).toHaveLength(0)
          expect(visible).toHaveLength(turnCount)
        }
      }),
      { numRuns: 60 },
    )
  })

  it('省略的消息条数等于被隐藏轮次覆盖的消息总数', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 4, max: 20 }),
        fc.integer({ min: 0, max: 3 }),
        (turnCount, toolRounds) => {
          const turns = scanTurns(conversation(turnCount, { toolRounds }))
          const fold = computeHistoryFold(turns)
          const covered = turns
            .filter(t => fold.hiddenStarts.has(t.startIndex))
            .reduce((sum, t) => sum + (t.endIndex - t.startIndex + 1), 0)
          expect(fold.hiddenMessages).toBe(covered)
          expect(fold.hiddenMessages).toBeGreaterThan(0)
        },
      ),
      { numRuns: 40 },
    )
  })
})

describe('推理中的会话 — 往快照最后一轮里流式追加消息', () => {
  /** 进入会话之后，最后一轮里陆续到来的 assistant(带 tool_calls) + 工具结果帧。 */
  function toolFrames(count) {
    const frames = []
    for (let i = 0; i < count; i++) {
      frames.push({ role: 'assistant', content: '', tool_calls: [{ id: `live${i}`, name: 'read_file' }] })
      frames.push({ role: 'tool', content: 'ok', tool_use_id: `live${i}` })
    }
    return frames
  }

  it('追加一次工具往返：隐藏区一字不改（否则横幅前多显示一轮）', () => {
    const entered = conversation(5)
    const boundary = snapshotBoundaryStart(scanTurns(entered), null)
    const before = computeHistoryFold(turnsInSnapshot(scanTurns(entered), boundary))

    const turnList = scanTurns([...entered, ...toolFrames(1)])
    const after = computeHistoryFold(turnsInSnapshot(turnList, boundary))

    expect([...after.hiddenStarts]).toEqual([...before.hiddenStarts])
    expect(after.hiddenTurns).toBe(before.hiddenTurns)
    expect(after.hiddenMessages).toBe(before.hiddenMessages)
    expect(after.bannerAfterStart).toBe(before.bannerAfterStart)
    // 渲染出的轮次仍是头 1 轮 + 尾 2 轮，不会多出第三轮。
    expect(turnList.filter(t => !after.hiddenStarts.has(t.startIndex)).map(t => t.startIndex)).toEqual([0, 6, 8])
  })

  it('快照轮数不随流式帧增长：4 轮的会话不会因为一帧就整个不折叠', () => {
    const entered = conversation(4)
    const boundary = snapshotBoundaryStart(scanTurns(entered), null)
    for (let frames = 0; frames <= 6; frames++) {
      const turnList = scanTurns([...entered, ...toolFrames(frames)])
      expect(turnsInSnapshot(turnList, boundary), `${frames} 帧后快照轮数`).toHaveLength(4)
      const fold = computeHistoryFold(turnsInSnapshot(turnList, boundary))
      expect(fold.collapsed, `${frames} 帧后仍然折叠`).toBe(true)
      expect([...fold.hiddenStarts]).toEqual([2])
      expect(turnList.filter(t => !fold.hiddenStarts.has(t.startIndex))).toHaveLength(3)
    }
  })

  it('随机：往最后一轮追加任意多条非 user 帧，折叠窗口与横幅数字恒定', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 4, max: 20 }),
        fc.integer({ min: 0, max: 12 }),
        (turnCount, frames) => {
          const entered = conversation(turnCount)
          const boundary = snapshotBoundaryStart(scanTurns(entered), null)
          const before = computeHistoryFold(turnsInSnapshot(scanTurns(entered), boundary))

          const turnList = scanTurns([...entered, ...toolFrames(frames)])
          const after = computeHistoryFold(turnsInSnapshot(turnList, boundary))

          expect(turnsInSnapshot(turnList, boundary)).toHaveLength(turnCount)
          expect([...after.hiddenStarts]).toEqual([...before.hiddenStarts])
          expect(after.hiddenTurns).toBe(before.hiddenTurns)
          expect(after.hiddenMessages).toBe(before.hiddenMessages)
          // 可见轮次恒为 3（头 1 + 尾 2），不会因为流式帧变成 4。
          expect(turnList.filter(t => !after.hiddenStarts.has(t.startIndex))).toHaveLength(3)
        },
      ),
      { numRuns: 50 },
    )
  })
})
