/**
 * 标签"发起新会话"发出去的两样东西的单元测试：
 *   1. 首轮系统消息上的 label_origin 标记（后端据此把标签聚合与 AI 代理系统
 *      提示词合并进同一条 system，而不是相互覆盖）；
 *   2. 创建会话那次请求的 source_labels（来源标签跟着建档落 index.json /
 *      tree.json，而不是只在侧栏 UI 上闪一下）。
 *
 * 后端对应行为由 tests/test_label_source_session_infer.py 端到端验证。
 */
import { describe, it, expect } from 'vitest'
import {
  buildFirstTurnSystemMessages,
  isNewSessionRequest,
  sourceLabelsForRequest,
} from './label-session-request.js'

describe('isNewSessionRequest', () => {
  it('草稿态（null / undefined / new / __new__）都算"创建会话的那一次"', () => {
    for (const sid of [null, undefined, 'new', '__new__']) {
      expect(isNewSessionRequest(sid)).toBe(true)
    }
  })

  it('已有会话不算，来源标签不该被重复上报', () => {
    expect(isNewSessionRequest('261127_220924')).toBe(false)
    expect(isNewSessionRequest('')).toBe(false)
  })
})

describe('buildFirstTurnSystemMessages', () => {
  it('纯文本系统提示词 + 来源标签 → 一条带 label_origin 的 system', () => {
    const [msg] = buildFirstTurnSystemMessages({
      text: '【标签「TTS」聚合概要】\n旧会话要点',
      origin: 'TTS',
    })
    expect(msg).toEqual({
      role: 'system',
      content: '【标签「TTS」聚合概要】\n旧会话要点',
      label_origin: 'TTS',
    })
  })

  it('提示词模板路径同样带 label_origin（否则模板又被代理系统提示词覆盖）', () => {
    const [msg] = buildFirstTurnSystemMessages({
      template: { template_id: 'tpl-1', arguments: { tone: 'dry' } },
      origin: 'TTS',
    })
    expect(msg.role).toBe('system')
    expect(msg.content).toBe('')
    expect(msg.prompt_template).toBe('tpl-1')
    expect(msg.arguments).toEqual({ tone: 'dry' })
    expect(msg.label_origin).toBe('TTS')
  })

  it('模板无参数时不发 arguments 字段（与旧实现一致）', () => {
    const [msg] = buildFirstTurnSystemMessages({ template: { template_id: 'tpl-1' } })
    expect('arguments' in msg).toBe(false)
    expect('label_origin' in msg).toBe(false)
  })

  it('没有来源标签时不加 label_origin：用户自己贴的系统提示词仍按原语义被代理接管', () => {
    const [msg] = buildFirstTurnSystemMessages({ text: '用户自选提示词', origin: '' })
    expect('label_origin' in msg).toBe(false)
    expect(msg.content).toBe('用户自选提示词')
  })

  it('模板优先于纯文本；两者皆空则不产生系统消息', () => {
    expect(buildFirstTurnSystemMessages({
      text: 'A', template: { template_id: 'tpl-1' }, origin: 'TTS',
    })[0].prompt_template).toBe('tpl-1')
    expect(buildFirstTurnSystemMessages({})).toEqual([])
    expect(buildFirstTurnSystemMessages({ text: '' })).toEqual([])
    expect(buildFirstTurnSystemMessages({ template: null, origin: '' })).toEqual([])
  })

  it('与重构前的 ChatPage 内联实现保持逐字一致（不改语义，只挪位置）', () => {
    // 旧代码是 `if (systemPromptText) return [...]`：空白字符串也算"有内容"。
    expect(buildFirstTurnSystemMessages({ text: '   ' })).toEqual([
      { role: 'system', content: '   ' },
    ])
  })
})

describe('sourceLabelsForRequest', () => {
  it('创建会话时带上来源标签（数组，便于后端与显式 labels 合并去重）', () => {
    expect(sourceLabelsForRequest({ sessionId: null, origin: 'TTS' })).toEqual(['TTS'])
    expect(sourceLabelsForRequest({ sessionId: 'new', origin: '  TTS  ' })).toEqual(['TTS'])
  })

  it('已有会话或无来源标签时返回 null：调用方整个字段都不写', () => {
    expect(sourceLabelsForRequest({ sessionId: '261127_220924', origin: 'TTS' })).toBeNull()
    expect(sourceLabelsForRequest({ sessionId: null, origin: '' })).toBeNull()
    expect(sourceLabelsForRequest({ sessionId: null })).toBeNull()
  })
})
