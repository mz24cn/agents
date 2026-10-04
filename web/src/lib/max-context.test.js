/**
 * Tests for max-context.js — ModelConfig.max_context 的显示/输入换算
 * 覆盖：M/K/原数值 显示规则、K/M 单位解析、空输入等于 0、非法输入
 */
import { describe, it, expect } from 'vitest'
import { formatMaxContext, parseMaxContext } from './max-context.js'

describe('formatMaxContext — 后端数值 → 输入框文本', () => {
  it('未设置（0 / null / undefined）显示为空', () => {
    expect(formatMaxContext(0)).toBe('')
    expect(formatMaxContext(null)).toBe('')
    expect(formatMaxContext(undefined)).toBe('')
  })

  it('1024*1024 的整数倍按 M', () => {
    expect(formatMaxContext(1048576)).toBe('1M')
    expect(formatMaxContext(2097152)).toBe('2M')
    expect(formatMaxContext(131072 * 1024)).toBe('128M')
  })

  it('1024 的整数倍（非 M）按 K', () => {
    expect(formatMaxContext(1024)).toBe('1K')
    expect(formatMaxContext(131072)).toBe('128K')
    expect(formatMaxContext(393216)).toBe('384K')
  })

  it('其他按原数值', () => {
    expect(formatMaxContext(1000)).toBe('1000')
    expect(formatMaxContext(123456)).toBe('123456')
    expect(formatMaxContext(2049)).toBe('2049')
  })
})

describe('parseMaxContext — 输入框文本 → 纯数值 token', () => {
  it('空输入等同 0', () => {
    expect(parseMaxContext('')).toBe(0)
    expect(parseMaxContext('   ')).toBe(0)
    expect(parseMaxContext(null)).toBe(0)
    expect(parseMaxContext(undefined)).toBe(0)
  })

  it('纯数值原样返回', () => {
    expect(parseMaxContext('131072')).toBe(131072)
    expect(parseMaxContext('0')).toBe(0)
    expect(parseMaxContext(' 1000 ')).toBe(1000)
  })

  it('K 单位（大小写不限）', () => {
    expect(parseMaxContext('1K')).toBe(1024)
    expect(parseMaxContext('128k')).toBe(131072)
    expect(parseMaxContext('1.5K')).toBe(1536)
  })

  it('M 单位（大小写不限）', () => {
    expect(parseMaxContext('1M')).toBe(1048576)
    expect(parseMaxContext('1m')).toBe(1048576)
    expect(parseMaxContext('1.5M')).toBe(1572864)
  })

  it('1024K 优先按 K 换算', () => {
    expect(parseMaxContext('1024K')).toBe(1024 * 1024)
  })

  it('与 formatMaxContext 互为逆运算', () => {
    for (const n of [1024, 131072, 393216, 1048576, 1572864, 1000, 123456, 1]) {
      expect(parseMaxContext(formatMaxContext(n))).toBe(n)
    }
  })

  it('非法输入返回 null', () => {
    for (const bad of ['abc', 'K', 'M', '-1', '1,5M', 'NaN', 'K1', '1 2']) {
      expect(parseMaxContext(bad)).toBeNull()
    }
  })
})
