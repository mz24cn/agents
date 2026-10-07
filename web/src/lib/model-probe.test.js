/**
 * Tests for model-probe.js — Setup 模型编辑页「模型检查」的选行/填值规则
 * 覆盖：单模型即当前模型、多模型按名称匹配（精确/包含/大小写）、匹配不到、
 * 选中行标记、以及"输入框已有值就不覆盖"。
 */
import { describe, it, expect } from 'vitest'
import {
  matchProbeIndex,
  markProbeRows,
  pickProbeIndex,
  probeColumns,
  probeFeatureText,
  filterProbeRows,
  shouldFillProbeValue,
} from './model-probe.js'

const rows = [
  { model_name: 'qwen3.8-flash-next-iq3_s', max_context: 131072 },
  { model_name: 'Qwen3-VL-32B', max_context: 32768 },
  { model_name: 'gpt-4o-mini', max_context: 0 },
]

describe('pickProbeIndex — 哪一行是当前模型', () => {
  it('只有一个模型时它就是当前模型', () => {
    expect(pickProbeIndex([{ model_name: 'only-one' }], '')).toBe(0)
    expect(pickProbeIndex([{ model_name: 'only-one' }], '完全不相关的名字')).toBe(0)
  })

  it('多个模型时按名称精确匹配（忽略大小写与首尾空白）', () => {
    expect(pickProbeIndex(rows, 'Qwen3-VL-32B')).toBe(1)
    expect(pickProbeIndex(rows, 'qwen3-vl-32b')).toBe(1)
    expect(pickProbeIndex(rows, '  gpt-4o-mini  ')).toBe(2)
  })

  it('多个模型时退化为包含匹配（别名 / 未解析的 {{ENV}} 占位符）', () => {
    expect(pickProbeIndex(rows, 'qwen3.8-flash')).toBe(0)          // 表单里是短名
    expect(pickProbeIndex(rows, 'qwen3.8-flash-next-iq3_s-gguf')).toBe(0)  // 表单里更长
  })

  it('匹配不到返回 -1（调用方提示用户手动点行）', () => {
    expect(pickProbeIndex(rows, 'deepseek-r1')).toBe(-1)
    expect(pickProbeIndex(rows, '')).toBe(-1)
    expect(pickProbeIndex(rows, null)).toBe(-1)
  })

  it('空列表 / 非数组返回 -1', () => {
    expect(pickProbeIndex([], 'x')).toBe(-1)
    expect(pickProbeIndex(undefined, 'x')).toBe(-1)
  })
})

describe('matchProbeIndex — 名称匹配细节', () => {
  it('缺 model_name 的行不参与包含匹配，也不会让查找崩掉', () => {
    const loose = [{ model_name: '' }, { model_name: 'qwen3-vl' }]
    expect(matchProbeIndex(loose, 'qwen')).toBe(1)
  })

  it('完全相同优先于包含', () => {
    const tricky = [{ model_name: 'qwen3-vl-max' }, { model_name: 'qwen3-vl' }]
    expect(matchProbeIndex(tricky, 'qwen3-vl')).toBe(1)
  })
})

describe('markProbeRows — 选中行标记', () => {
  it('只标记指定下标，且不改入参', () => {
    const marked = markProbeRows(rows, 1)
    expect(marked.map(r => r.selected)).toEqual([false, true, false])
    expect(rows.every(r => !('selected' in r))).toBe(true)
  })

  it('index 为 -1 时一行都不选', () => {
    expect(markProbeRows(rows, -1).every(r => !r.selected)).toBe(true)
  })
})

describe('shouldFillProbeValue — 不覆盖用户已填的值', () => {
  it('空 / 0 / 非法输入时自动填', () => {
    expect(shouldFillProbeValue('')).toBe(true)
    expect(shouldFillProbeValue('   ')).toBe(true)
    expect(shouldFillProbeValue('0')).toBe(true)
    expect(shouldFillProbeValue('abc')).toBe(true)
  })

  it('已有有效值（含 K/M 单位）时保持不动', () => {
    expect(shouldFillProbeValue('128K')).toBe(false)
    expect(shouldFillProbeValue('1M')).toBe(false)
    expect(shouldFillProbeValue('131072')).toBe(false)
  })
})

describe('probeColumns — 全表都没值的列不渲染', () => {
  it('只回 id 的网关：最大输出/能力/状态列都不出现', () => {
    expect(probeColumns([{ model_name: 'gpt-4o-mini', max_context: 0 }])).toEqual({
      output: false, features: false, status: false,
    })
  })

  it('任意一行有值就渲染该列', () => {
    expect(probeColumns([
      { model_name: 'a', max_output: 0 },
      { model_name: 'b', max_output: 393216, features: ['tools'], status: 'loaded' },
    ])).toEqual({ output: true, features: true, status: true })
  })

  it('只有 effort_levels 也算有能力列', () => {
    expect(probeColumns([{ model_name: 'a', effort_levels: ['low'] }]).features).toBe(true)
  })

  it('入参不是数组时不炸', () => {
    expect(probeColumns(undefined)).toEqual({ output: false, features: false, status: false })
  })
})

describe('filterProbeRows — 一个端点回 200 个模型时要能过滤', () => {
  const many = [
    { model_name: 'Qwen3-VL-32B' },
    { model_name: 'deepseek-r1' },
    { model_name: 'gpt-4o-mini' },
  ]

  it('空过滤词返回全部', () => {
    expect(filterProbeRows(many, '   ')).toHaveLength(3)
  })

  it('忽略大小写的子串匹配', () => {
    expect(filterProbeRows(many, 'qwen3').map(r => r.model_name)).toEqual(['Qwen3-VL-32B'])
    expect(filterProbeRows(many, 'VL').map(r => r.model_name)).toEqual(['Qwen3-VL-32B'])
  })

  it('没命中返回空数组（表格渲染占位行）', () => {
    expect(filterProbeRows(many, 'nope')).toEqual([])
    expect(filterProbeRows(null, 'x')).toEqual([])
  })
})

describe('probeFeatureText — 能力列文案', () => {
  it('features 与思考力度拼在一起', () => {
    expect(probeFeatureText({ features: ['tools', 'reasoning'], effort_levels: ['low', 'max'] }))
      .toBe('tools · reasoning · effort: low|max')
  })

  it('缺项只输出有的部分，全空返回空串', () => {
    expect(probeFeatureText({ effort_levels: ['low'] })).toBe('effort: low')
    expect(probeFeatureText({ features: [] })).toBe('')
    expect(probeFeatureText(undefined)).toBe('')
  })
})
