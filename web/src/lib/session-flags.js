/**
 * 会话旗标（Sidebar「设置标题」对话框里的单选旗标字符）。
 *
 * 候选集必须与后端 ``runtime/session_manager.py`` 的 ``SESSION_FLAGS`` 逐项一致：
 * 后端按同一候选集把关（不在集合内 → 400），前端只提供这些按钮 + 「无旗标」。
 *
 * ``🏳️``（U+1F3F3 + U+FE0F）与 ``⚠️``（U+26A0 + U+FE0F）都带变体选择符 VS16，
 * 源码里很容易被编辑器"顺手"规范化掉，所以这里用码点转义写死，并由
 * ``session-flags.test.js`` 断言码点，避免前后端静默漂移。
 */

/** 无旗标：提交空串，后端清掉 index.json 里的 ``flag`` 键。 */
export const FLAG_NONE = ''

/** 单选旗标候选（顺序即对话框里的展示顺序）。 */
export const SESSION_FLAGS = [
  '\u{1F6A9}',   // 🚩
  '\u{1F3F4}',   // 🏴
  '\u{1F3F3}\u{FE0F}',  // 🏳️
  '\u{1F534}',   // 🔴
  '\u{1F7E0}',   // 🟠
  '\u{1F7E1}',   // 🟡
  '\u{1F7E2}',   // 🟢
  '\u{1F535}',   // 🔵
  '\u{1F7E3}',   // 🟣
  '\u{1F7E4}',   // 🟤
  '\u2705',      // ✅
  '\u26A0\uFE0F',       // ⚠️
]

/**
 * 旗标是否可识别（含去掉 VS16 的等价写法）。
 *
 * 只用于**读**：老数据或别的客户端可能写成 ``🏳``（无 VS16），展示时照原样显示，
 * 但对话框里要能把这一格预选中，不能因为写法差异显示成"无旗标"。
 */
export function isKnownFlag(flag) {
  if (typeof flag !== 'string' || !flag) return false
  const bare = flag.replace(/\uFE0F/g, '')
  return SESSION_FLAGS.some(f => f === flag || f.replace(/\uFE0F/g, '') === bare)
}

/**
 * 侧栏/对话框回显：只承认候选集里的旗标，其它值（脏数据、别的客户端写的自由文本、
 * 旧会话没有该字段）一律空串 —— 宁可少显示，也不渲染一个无法清掉的野值。
 */
export function sessionFlagLabel(flag) {
  return isKnownFlag(flag) ? flag : ''
}

/**
 * 两个旗标写法是否同一个（忽略 VS16）。
 *
 * 后端 ``clean_flag`` 会把变体归一成候选集里的规范写法，所以正常情况下比较
 * 原始字符串就够了；这里容错是为了「老数据里已经有 🏳（无 VS16）」这类情况
 * 在对话框里仍能选中对应按钮，而不是显示成「无旗标」让用户重复提交。
 */
export function sameFlag(a, b) {
  const norm = v => String(v ?? '').replace(/\uFE0F/g, '')
  return norm(a) === norm(b)
}
