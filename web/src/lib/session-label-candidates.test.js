/**
 * 「设置标签」候选标签与追加语义测试。
 *
 * 候选来自会话森林里的全部标签节点（type='label'，name=标签名，
 * children 是会话 ID 字符串数组），必须去重 + 稳定排序；标签叶子的 children
 * 不是节点，不能被当成节点遍历。
 *
 * 追加有两套语义，互不影响：
 *   * 「设置标签」对话框：输入框草稿串，由 parseLabels 负责拆分
 *     （半角逗号、全角逗号、空白都是分隔符）；
 *   * 会话菜单「设置标签」末尾 ··· 面板：直接往会话的 labels 数组后面加
 *     （appendLabelToList），已持有不重复也不删除，连续追加不丢已落盘的标签。
 */
import { describe, expect, it } from 'vitest'
import { appendLabelToList, collectLabelCandidates } from './session-label-candidates.js'

function labelNode(name, sessionCount = 1, sessions = ['s1']) {
  return { id: 1, name, type: 'label', session_count: sessionCount, children: sessions, stats: {} }
}

const TREE = [
  { id: 'category', name: '主题', type: 'category', children: [
    { id: 2, name: '子分类', type: 'category', children: [] },
  ] },
  { id: 'labels', name: '标签', type: 'labels', children: [
    labelNode('性能', 9, ['s1', 's2']),
    labelNode('tts', 3),
    labelNode('部署', 12),
    labelNode('TTS', 1),          // 大小写不同：是不同标签名，都保留
    { id: 7, name: '  带空格  ', type: 'label', session_count: 2, children: [] },
    { id: 8, name: '', type: 'label', session_count: 0, children: [] },
  ] },
]

describe('候选标签列表', () => {
  it('去重后按名称稳定排序，空名标签忽略', () => {
    const first = collectLabelCandidates(TREE)
    expect(first).toEqual(['部署', '带空格', '性能', 'tts', 'TTS'])
    // 同一棵树反复取，顺序不变（"稳定排序"的字面要求）
    expect(collectLabelCandidates(TREE)).toEqual(first)
  })

  it('标签叶子下的会话 ID 不会被当成候选标签', () => {
    expect(collectLabelCandidates(TREE)).not.toContain('s1')
    expect(collectLabelCandidates(TREE)).not.toContain('s2')
  })

  it('同名标签在树里重复出现时只留一份，会话数取较大值', () => {
    const dup = [
      { id: 'labels', type: 'labels', children: [labelNode('tts', 2), labelNode('tts', 5)] },
      { id: 'other', type: 'labels', children: [labelNode('tts', 1)] },
    ]
    expect(collectLabelCandidates(dup)).toEqual(['tts'])
  })

  it('没有标签节点时返回空数组（面板显示「暂无标签候选」）', () => {
    expect(collectLabelCandidates([])).toEqual([])
    expect(collectLabelCandidates(undefined)).toEqual([])
    expect(collectLabelCandidates([{ id: 1, name: 'x', type: 'category', children: null }])).toEqual([])
  })
})

describe('··· 面板：点候选往会话 labels 数组后面追加', () => {
  it('空标签会话：追加结果就是该标签', () => {
    expect(appendLabelToList([], '部署')).toEqual(['部署'])
    expect(appendLabelToList(undefined, '部署')).toEqual(['部署'])
    expect(appendLabelToList(['  ', ''], '部署')).toEqual(['部署'])   // 空项顺手清掉
  })

  it('已有标签保留原顺序，新标签追加在末尾', () => {
    expect(appendLabelToList(['tts', '性能'], '部署')).toEqual(['tts', '性能', '部署'])
  })

  it('已持有的标签：原样返回，既不重复也不删除（点第二次不变）', () => {
    const current = ['tts', '性能']
    expect(appendLabelToList(current, 'tts')).toEqual(['tts', '性能'])
    expect(appendLabelToList(current, '  tts ')).toEqual(['tts', '性能'])
    // 大小写不同是不同标签（与后端 labels 语义一致），照常追加
    expect(appendLabelToList(['TTS'], 'tts')).toEqual(['TTS', 'tts'])
  })

  it('空候选名不改动列表', () => {
    expect(appendLabelToList(['tts'], '')).toEqual(['tts'])
    expect(appendLabelToList(['tts'], null)).toEqual(['tts'])
  })

  it('连续追加：后一次基于前一次的结果，不会丢掉刚加上的标签', () => {
    let labels = ['tts', '性能']
    labels = appendLabelToList(labels, '部署')
    labels = appendLabelToList(labels, 'tts')          // 已持有 → 不变
    labels = appendLabelToList(labels, '告警')
    expect(labels).toEqual(['tts', '性能', '部署', '告警'])
  })

  it('标签名里的空格不会被拆开（列表版不经过草稿串）', () => {
    expect(appendLabelToList(['x'], 'two words')).toEqual(['x', 'two words'])
  })

  it('返回新数组，不改写入参（共享事实里的数组不会被就地改掉）', () => {
    const current = Object.freeze(['tts'])
    const next = appendLabelToList(current, '部署')
    expect(next).toEqual(['tts', '部署'])
    expect(current).toEqual(['tts'])
  })
})
