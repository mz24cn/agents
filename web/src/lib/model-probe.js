/**
 * "模型检查"（GET /v1/models/probe）中可单测的纯逻辑。
 *
 * pickProbeIndex —— 判断探测回来的列表里哪一行是"正在配置的模型"：
 *   - 只有一个模型 ⇒ 只能是它（index 0）；
 *   - 否则按表单里的 model_name 匹配：先忽略大小写完全相同，再退化为包含
 *     关系（表单里可能是别名，也可能是只有后端才能解析的 {{ENV}} 占位符）；
 *   - 匹配不到返回 -1，由调用方提示用户手动点表格行。
 *
 * markProbeRows —— 把选中行标出来（返回新数组，不改动入参）。
 *
 * shouldFillProbeValue —— 是否把该行的上下文长度自动写进输入框：只在输入框
 *   为空 / 为 0 / 当前值无法解析时写，避免覆盖用户手填的值。
 *
 * probeColumns / filterProbeRows / probeFeatureText —— 表格渲染：全表都没值的
 *   列不渲染、按名称过滤（有的网关一次回 200 个模型）、能力列的文案。
 */

import { parseMaxContext } from './max-context.js'

/** 按名称在探测结果里找当前模型；找不到返回 -1。 */
export function matchProbeIndex(rows, wanted) {
  const target = String(wanted ?? '').trim().toLowerCase()
  if (!target) return -1
  const names = rows.map(row => String(row?.model_name ?? '').toLowerCase())
  const exact = names.indexOf(target)
  if (exact >= 0) return exact
  return names.findIndex(name => name && (name.includes(target) || target.includes(name)))
}

/** 单模型即当前模型；多模型按名称匹配。 */
export function pickProbeIndex(list, modelName) {
  if (!Array.isArray(list) || list.length === 0) return -1
  if (list.length === 1) return 0
  return matchProbeIndex(list, modelName)
}

/** 复制列表并只把 selectedIndex 那一行标记为选中。 */
export function markProbeRows(list, selectedIndex) {
  return (Array.isArray(list) ? list : []).map((row, i) => ({ ...row, selected: i === selectedIndex }))
}

/** 输入框当前为空/0/非法时，才自动填入探测值。 */
export function shouldFillProbeValue(currentText) {
  return !parseMaxContext(currentText)
}

/** 某列全表都没有值就不渲染它（很多网关只给 id，别留一堆空列）。 */
export function probeColumns(rows) {
  const list = Array.isArray(rows) ? rows : []
  const any = key => list.some(row => {
    const value = row?.[key]
    return Array.isArray(value) ? value.length > 0 : Boolean(value)
  })
  return {
    output: any('max_output'),
    features: any('features') || any('effort_levels'),
    status: any('status'),
  }
}

/** 按名称过滤（忽略大小写）；空过滤词返回全部。200+ 模型的网关需要它。 */
export function filterProbeRows(rows, query) {
  const list = Array.isArray(rows) ? rows : []
  const needle = String(query ?? '').trim().toLowerCase()
  if (!needle) return list
  return list.filter(row => String(row?.model_name ?? '').toLowerCase().includes(needle))
}

/** 能力列：features + 思考力度，例如 ``tools · reasoning`` / ``effort: low|high``。 */
export function probeFeatureText(row) {
  const parts = []
  if (Array.isArray(row?.features) && row.features.length) parts.push(row.features.join(' · '))
  if (Array.isArray(row?.effort_levels) && row.effort_levels.length) {
    parts.push(`effort: ${row.effort_levels.join('|')}`)
  }
  return parts.join(' · ')
}
