/**
 * 会话权威事实（session facts）的回归测试 —— 需求 5 / 6 的核心不变量。
 *
 * 这些规则是纯函数层的，故意不依赖 Svelte：Sidebar 只负责把结果放进响应式容器，
 * 「同一会话在任何分类下都一样」「晚到的响应不得覆盖新状态」「客户端工具等待不得被
 * 绿色成功冲掉」三条语义都在这里锁死，避免只在某一个分类里修好。
 */
import { describe, it, expect } from 'vitest'
import {
  CLIENT_TOOL_STATUS,
  applyLabelRename,
  carriesAuthoritativeTitle,
  emptyFacts,
  factsFromRow,
  isTerminalStatus,
  mergeFacts,
  projectRow,
} from './session-facts.js'

const T0 = 1_700_000_000_000

function merge(existing, patch, opts) {
  return mergeFacts(existing, patch, opts)
}

describe('跨分类共享同一份事实', () => {
  it('同一 session 在两个分类的行上显示同一标题 / 旗标 / 标签 / 状态', () => {
    const facts = emptyFacts('s1')
    facts.title = '统一的标题'
    facts.titleGiven = true
    facts.flag = '🟢'
    facts.labels = ['tts', '性能']
    facts.status = CLIENT_TOOL_STATUS

    const themeRow = { session_id: 's1', title: '旧标题', flag: '', labels: [] }
    const labelRow = { session_id: 's1', title: '另一个旧标题' }   // 标签分类的缓存行

    const a = projectRow(themeRow, facts)
    const b = projectRow(labelRow, facts)

    expect(a.title).toBe('统一的标题')
    expect(b.title).toBe('统一的标题')
    expect(a.flag).toBe('🟢')
    expect(b.flag).toBe('🟢')
    expect(b.labels).toEqual(['tts', '性能'])
    expect(a._status).toBe(CLIENT_TOOL_STATUS)
    expect(b._status).toBe(CLIENT_TOOL_STATUS)
  })

  it('事实层永远赢过分类缓存里的旧副本', () => {
    const stale = { session_id: 's1', title: '缓存里的旧名', flag: '🚩', _status: 'done_success_unread' }
    const facts = { ...emptyFacts('s1'), title: '刚改的名', titleGiven: true, flag: '', status: 'streaming' }
    const row = projectRow(stale, facts)
    expect(row.title).toBe('刚改的名')
    expect(row.flag).toBe('')          // 旗标已清除，不能被旧副本带回 🚩
    expect(row._status).toBe('streaming')
  })

  it('session_id 不同的行不会被串改（分类里同时有多个会话）', () => {
    const facts = { ...emptyFacts('s1'), title: 'A 的标题', flag: '🟡' }
    const other = projectRow({ session_id: 's2', title: 'B 的标题' }, facts)
    expect(other.title).toBe('B 的标题')
    expect(other.flag).toBeUndefined()
  })
})

describe('晚到的响应不得覆盖新状态', () => {
  it('列表响应用「请求发起时刻」写入，晚到时输给出请求期间到达的 SSE 事件', () => {
    const requestedAt = T0
    // 请求发出后、响应回来之前，SSE 先把状态推到了 streaming，并带来权威标题
    let facts = merge(null, factsFromRow({ session_id: 's1', title: '旧名', flag: '🚩' }),
      { session_id: 's1', at: requestedAt, kind: 'snapshot' })
    facts = merge(facts, { status: 'streaming' }, { session_id: 's1', at: requestedAt + 40, kind: 'sse' })
    facts = merge(facts, { title: '推理完成后生成', titleGiven: false },
      { session_id: 's1', at: requestedAt + 60, kind: 'sse' })

    // 晚到的列表响应：内容还是请求发起那一刻的旧快照（标题旧、状态是上一轮的 done）
    const late = factsFromRow({
      session_id: 's1', title: '旧名', flag: '🚩', _status: 'done_success_unread',
    })
    const next = merge(facts, late, { session_id: 's1', at: requestedAt, kind: 'snapshot' })

    expect(next).toBeNull()                                  // 没有任何可采纳的字段
    const projected = projectRow(
      { session_id: 's1', title: '旧名', _status: 'done_success_unread' }, facts)
    expect(projected._status).toBe('streaming')
    expect(projected.title).toBe('推理完成后生成')
  })

  it('快照不能把进行中的状态（streaming / 客户端工具等待）降级成终态', () => {
    let facts = merge(null, { status: 'streaming' }, { session_id: 's1', at: T0 + 10, kind: 'sse' })
    const downgraded = merge(facts, { status: 'done_success_unread' },
      { session_id: 's1', at: T0 + 900, kind: 'snapshot' })
    expect(downgraded).toBeNull()
    expect(facts.status).toBe('streaming')

    facts = merge(null, { status: CLIENT_TOOL_STATUS }, { session_id: 's1', at: T0 + 10, kind: 'sse' })
    const again = merge(facts, { status: 'done_success_unread' },
      { session_id: 's1', at: T0 + 900, kind: 'snapshot' })
    expect(again).toBeNull()
  })

  it('同为实时事件时按时间先后，更新的终态可以接管（真跑完还是要变绿/变红）', () => {
    let facts = merge(null, { status: 'streaming' }, { session_id: 's1', at: T0, kind: 'sse' })
    // 真跑完了：新一轮的 streaming 已经出现过，之后的终态才有效
    facts = merge(facts, { status: 'streaming' }, { session_id: 's1', at: T0 + 50, kind: 'sse' })
    const next = merge(facts, { status: 'done_error_unread' }, { session_id: 's1', at: T0 + 80, kind: 'sse' })
    expect(next?.status).toBe('done_error_unread')
  })

  it('本地改名不被稍后的旧列表响应回退', () => {
    let facts = merge(null, factsFromRow({ session_id: 's1', title: '旧名' }),
      { session_id: 's1', at: T0, kind: 'snapshot' })
    facts = merge(facts, { title: '用户改名', titleGiven: true }, { session_id: 's1', at: T0 + 10, kind: 'user' })
    const late = merge(facts, factsFromRow({ session_id: 's1', title: '旧名' }),
      { session_id: 's1', at: T0, kind: 'snapshot' })
    expect(late).toBeNull()
    expect(facts.title).toBe('用户改名')
  })

  it('用户手填的标题不被自动生成标题降级', () => {
    let facts = merge(null, { title: '我起的名字', titleGiven: true },
      { session_id: 's1', at: T0, kind: 'user' })
    const auto = merge(facts, { title: '模型生成的标题', titleGiven: false },
      { session_id: 's1', at: T0 + 10, kind: 'sse' })
    expect(auto).toBeNull()
    expect(facts.title).toBe('我起的名字')
    // 但用户自己的下一次改名可以覆盖
    const again = merge(facts, { title: '再改一次', titleGiven: true },
      { session_id: 's1', at: T0 + 20, kind: 'user' })
    expect(again?.title).toBe('再改一次')
  })
})

describe('客户端工具等待（蓝色）不被绿色覆盖', () => {
  function waiting() {
    return merge(null, { status: CLIENT_TOOL_STATUS }, { session_id: 's1', at: T0, kind: 'sse' })
  }

  it('done_success_unread / idle / paused 事件不能把蓝色冲掉', () => {
    const facts = waiting()
    for (const status of ['done_success_unread', 'idle', 'paused']) {
      const next = merge(facts, { status }, { session_id: 's1', at: T0 + 1000, kind: 'sse' })
      expect(next, `${status} 不该覆盖等待态`).toBeNull()
    }
    expect(facts.status).toBe(CLIENT_TOOL_STATUS)
  })

  it('客户端回传后继续推理：streaming → done_success_unread 正常变绿', () => {
    let facts = waiting()
    let next = merge(facts, { status: 'streaming' }, { session_id: 's1', at: T0 + 10, kind: 'sse' })
    expect(next?.status).toBe('streaming')
    facts = next
    next = merge(facts, { status: 'done_success_unread' }, { session_id: 's1', at: T0 + 20, kind: 'sse' })
    expect(next?.status).toBe('done_success_unread')
  })

  it('错误终态仍能把蓝色改成红色（不被蓝色规则误挡）', () => {
    const facts = waiting()
    const next = merge(facts, { status: 'done_error_unread' }, { session_id: 's1', at: T0 + 30, kind: 'sse' })
    expect(next?.status).toBe('done_error_unread')
  })

  it('同类（都在等客户端工具）的更新快照按时间接管，旧的不覆盖新的', () => {
    let facts = merge(null, { status: CLIENT_TOOL_STATUS }, { session_id: 's1', at: T0 + 50, kind: 'sse' })
    const older = merge(facts, factsFromRow({ session_id: 's1', _status: CLIENT_TOOL_STATUS }),
      { session_id: 's1', at: T0, kind: 'snapshot' })
    expect(older).toBeNull()
    const same = merge(facts, factsFromRow({ session_id: 's1', _status: CLIENT_TOOL_STATUS }),
      { session_id: 's1', at: T0 + 60, kind: 'snapshot' })
    expect(same).toBeNull()      // 状态值相同，本来就没有变化
  })

  it('等待态带权威标题（与 done_* 一样，切分类后标题一致）', () => {
    expect(carriesAuthoritativeTitle(CLIENT_TOOL_STATUS)).toBe(true)
    expect(carriesAuthoritativeTitle('done_success_unread')).toBe(true)
    expect(carriesAuthoritativeTitle('streaming')).toBe(false)
    expect(isTerminalStatus(CLIENT_TOOL_STATUS)).toBe(false)
    expect(isTerminalStatus('done_error_unread')).toBe(true)
  })
})

describe('标签改名后的事实同步', () => {
  it('原地替换、保持顺序；目标标签已存在时去重', () => {
    expect(applyLabelRename(['tts', '性能', '部署'], 'tts', '语音合成'))
      .toEqual(['语音合成', '性能', '部署'])
    expect(applyLabelRename(['tts', '语音合成'], 'tts', '语音合成'))
      .toEqual(['语音合成'])
    expect(applyLabelRename(['其它'], 'tts', '语音合成')).toEqual(['其它'])
    expect(applyLabelRename([], 'tts', '语音合成')).toEqual([])
  })
})

describe('列表行 → 事实补丁', () => {
  it('_status 优先，没有 _status 时退回持久化的 paused', () => {
    expect(factsFromRow({ session_id: 's1', _status: 'streaming' }).status).toBe('streaming')
    expect(factsFromRow({ session_id: 's1', _status: CLIENT_TOOL_STATUS }).status).toBe(CLIENT_TOOL_STATUS)
    expect(factsFromRow({ session_id: 's1', status: 'paused' }).status).toBe('paused')
    expect(factsFromRow({ session_id: 's1', status: 'finished' }).status).toBeNull()
    expect(factsFromRow(null)).toBeNull()
    expect(factsFromRow({})).toBeNull()
  })

  it('缺失 flag / labels 的旧会话读出来是空值而不是 undefined', () => {
    const patch = factsFromRow({ session_id: 'legacy', title: 'legacy' })
    expect(patch.flag).toBe('')
    expect(patch.labels).toEqual([])
    expect(patch.titleGiven).toBeNull()
  })
})
