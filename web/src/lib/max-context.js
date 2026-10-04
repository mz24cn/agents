/**
 * ModelConfig.max_context 的显示/输入转换。
 *
 * 显示（后端数值 → 输入框文本）：
 *   1024 * 1024 的整数倍 → "NM"；否则 1024 的整数倍 → "NK"；否则原数值；
 *   0（未设置）→ 空字符串。
 *
 * 解析（输入框文本 → 提交给后端的纯数值 token 数）：
 *   末尾为 K/M（大小写不限，允许小数，如 1.5M）时按 1K = 1024、1M = 1024*1024
 *   换算；空输入等同 0；非法输入返回 null（由调用方提示错误）。
 */

const UNIT_K = 1024
const UNIT_M = 1024 * 1024

/**
 * 把后端保存的 max_context 数值格式化为输入框显示文本。
 *
 * @param {number|string|null|undefined} value - 后端数值（0 表示未设置）
 * @returns {string} 显示文本，未设置时为空字符串
 */
export function formatMaxContext(value) {
  const n = Number(value) || 0
  if (n <= 0) return ''
  if (n % UNIT_M === 0) return `${n / UNIT_M}M`
  if (n % UNIT_K === 0) return `${n / UNIT_K}K`
  return String(n)
}

/**
 * 把输入框文本解析为提交给后端的纯数值 token 数。
 *
 * @param {string|null|undefined} text - 用户输入，可带 K/M 单位
 * @returns {number|null} token 数；空输入为 0；无法解析时为 null
 */
export function parseMaxContext(text) {
  const raw = String(text ?? '').trim()
  if (!raw) return 0
  const unit = raw.slice(-1).toUpperCase()
  const hasUnit = unit === 'K' || unit === 'M'
  const digits = hasUnit ? raw.slice(0, -1).trim() : raw
  const num = Number(digits)
  if (!digits || !Number.isFinite(num) || num < 0) return null
  const factor = unit === 'M' ? UNIT_M : unit === 'K' ? UNIT_K : 1
  return Math.round(num * factor)
}
