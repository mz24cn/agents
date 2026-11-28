/**
 * 「设置标签」的候选标签：取自会话森林里的全部标签节点
 * （runtime/category_tree_builder.py 的 LABELS 根：type='labels'，
 * 其叶子是 type='label' 的节点，name 就是标签名）。
 *
 * 后端按「会话数倒序 + 标签名」排过，但同一标签在不同会话下会重复出现，
 * 而且标签根之外将来也可能挂标签叶子 —— 这里统一去重，并按名称稳定排序
 * （localeCompare + 定长会话数倒序兜底），保证两次打开看到的顺序一致。
 *
 * 候选只是"点一下就能用上"的入口，具体落到哪里由调用方决定：
 * 「设置标签」对话框里是输入框草稿串，由 parseLabels（半角/全角逗号、空白都是
 * 分隔符）清洗；会话菜单「设置标签」项末尾 ··· 面板的点选则直接往会话的 labels
 * 数组后面追加（见 appendLabelToList），两者互不影响。
 */

function walkLabelNodes(nodes, out) {
  const stack = Array.isArray(nodes) ? [...nodes] : []
  while (stack.length) {
    const node = stack.pop()
    if (!node || typeof node !== 'object') continue  // 标签叶子的 children 是会话 id 字符串
    if (node.type === 'label' && typeof node.name === 'string' && node.name.trim()) {
      out.push({ label: node.name.trim(), count: Number(node.session_count) || 0 })
      // 标签叶子下挂的是会话 id，不是子节点，不必再往下走
      continue
    }
    if (Array.isArray(node.children)) stack.push(...node.children)
  }
}

/**
 * 去重 + 稳定排序后的候选标签名数组。
 * 排序键：名称 localeCompare（不区分大小写）为主，同名不可能存在（已去重），
 * 因此结果只取决于标签名集合本身 —— 森林刷新前后顺序都不会变。
 */
export function collectLabelCandidates(treeNodes) {
  const found = []
  walkLabelNodes(treeNodes, found)
  const byLabel = new Map()
  for (const { label, count } of found) {
    const prev = byLabel.get(label)
    // 同一标签若在树里出现多次（理论不该），会话数取较大值，名称保持第一次看到的写法
    if (!prev || count > prev.count) byLabel.set(label, { label, count })
  }
  return [...byLabel.values()].sort((a, b) => {
    const cmp = String(a.label).localeCompare(String(b.label), 'zh-Hans-CN', { sensitivity: 'base' })
    if (cmp !== 0) return cmp
    return b.count - a.count
  }).map(x => x.label)
}

/**
 * 会话菜单「设置标签」项末尾 ··· 面板用的**列表版**追加（不经过输入框草稿串）。
 * 会话侧的标签是后端返回的数组，按数组追加才不会把标签名里的空格/分隔符
 * 再解析一遍（标签名允许含空格时草稿串语义会出错）。
 *
 * 幂等：已持有的标签原样复制返回 —— 既不会重复追加，也不会因为「再点一次」
 * 而被删除（删除只能在对话框输入框里改完再保存）。
 *
 * 连续追加的安全网：调用方每次都以「共享事实里的最新数组」为基准，所以后一次
 * 一定包含前一次已落盘的标签。
 */
export function appendLabelToList(labels, label) {
  const current = (Array.isArray(labels) ? labels : [])
    .map(x => String(x ?? '').trim())
    .filter(Boolean)
  const name = String(label ?? '').trim()
  if (!name || current.includes(name)) return current
  return [...current, name]
}
