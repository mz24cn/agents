import { describe, it, expect } from 'vitest'
import { buildCallTestSkeleton } from './tool-call-skeleton.js'

describe('buildCallTestSkeleton', () => {
  it('按类型生成默认值（含嵌套 object）', () => {
    const tool = {
      parameters: {
        type: 'object',
        properties: {
          name: { type: 'string' },
          count: { type: 'integer' },
          ratio: { type: 'number' },
          dry_run: { type: 'boolean' },
          tags: { type: 'array' },
          meta: { type: 'object', properties: { key: { type: 'string' }, depth: { type: 'integer' } } },
        },
      },
    }
    expect(JSON.parse(buildCallTestSkeleton(tool))).toEqual({
      name: '',
      count: 0,
      ratio: 0,
      dry_run: false,
      tags: [],
      meta: { key: '', depth: 0 },
    })
  })

  it('存在 enum 时取第一个候选值', () => {
    const tool = { parameters: { properties: { level: { type: 'string', enum: ['low', 'high'] } } } }
    expect(JSON.parse(buildCallTestSkeleton(tool))).toEqual({ level: 'low' })
  })

  it('列出全部 properties（含可选参数）', () => {
    const tool = {
      parameters: {
        type: 'object',
        required: ['a'],
        properties: { a: { type: 'string' }, b: { type: 'string' } },
      },
    }
    const parsed = JSON.parse(buildCallTestSkeleton(tool))
    expect(Object.keys(parsed).sort()).toEqual(['a', 'b'])
  })

  it('schema 缺失或非法时返回 {}', () => {
    expect(buildCallTestSkeleton({})).toBe('{}')
    expect(buildCallTestSkeleton(null)).toBe('{}')
    expect(buildCallTestSkeleton({ parameters: null })).toBe('{}')
    expect(buildCallTestSkeleton({ parameters: 'bad' })).toBe('{}')
    expect(buildCallTestSkeleton({ parameters: { type: 'object' } })).toBe('{}')
    expect(buildCallTestSkeleton({ parameters: { properties: 'bad' } })).toBe('{}')
  })

  it('无 type 但带 properties 的 schema 按 object 处理', () => {
    const tool = { parameters: { properties: { filter: { properties: { q: { type: 'string' } } } } } }
    expect(JSON.parse(buildCallTestSkeleton(tool))).toEqual({ filter: { q: '' } })
  })

  it('未知 type 返回空串占位', () => {
    const tool = { parameters: { properties: { weird: { type: 'something' } } } }
    expect(JSON.parse(buildCallTestSkeleton(tool))).toEqual({ weird: '' })
  })

  it('输出为 2 空格缩进的 JSON 字符串', () => {
    expect(buildCallTestSkeleton({ parameters: { properties: { a: { type: 'string' } } } }))
      .toBe('{\n  "a": ""\n}')
  })
})
