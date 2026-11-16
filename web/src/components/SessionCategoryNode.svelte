<script>
  // Svelte 5 no longer creates an implicit variable for a component to render
  // itself recursively. Import this file under a different name so expanding a
  // category can safely render its child categories in production builds.
  import CategoryNode from './SessionCategoryNode.svelte'

  let {
    node,
    depth = 0,
    expandedPaths,
    selectedCategory = '',
    onToggle,
    onSelect,
    onOpenTerminalNode,
    onNodeContextmenu,
  } = $props()

  let path = $derived(node.category || '')
  let expanded = $derived(expandedPaths.has(path))
  let categoryChildren = $derived((node.children || []).filter(child => child && typeof child === 'object'))
  // 节点类型：category（默认/旧数据）| workspace | environment | terminal |
  // recent（最近会话根，末级）| history（会话历史根）| month（月份末级）|
  // terminal_layer（终端环境下的嵌套终端层，command 为进入指令）。
  let nodeKind = $derived(node.type || 'category')
  let isTerminalAction = $derived(nodeKind === 'terminal' || nodeKind === 'terminal_layer')
  // 终端根（自身无 env_id/command）不显示右键菜单；环境节点（带 env_id）
  // 与终端层（带 command）提供"新建子终端/删除"。
  const hasEnvChildren = $derived(
    categoryChildren.some(child => (child.type || 'category') === 'terminal'))
  // 单环境形态里"本地终端"根自己就是环境节点，没有环境子级；旧数据的根上可能
  // 还没有 env/environment 字段，此时按"没有环境子级的 terminal 节点"识别它，
  // 否则最常见的单机部署里根本没有创建嵌套终端的入口。
  let isTerminalEnv = $derived(nodeKind === 'terminal'
    && ('env_id' in node || 'environment' in node || !hasEnvChildren))
  let isTerminalLayer = $derived(nodeKind === 'terminal_layer')
  let terminal = $derived(!isTerminalAction && Boolean(node.category) && categoryChildren.length === 0)

  // 根/特殊节点图标集中在这一张表：换风格只改这里并重新打包，不动 tree.json。
  // 追加变体选择符 U+FE0F 强制彩色呈现——U+1F5C2(🗂)/U+1F550(🕐)/U+1F5D3(🗓)
  // 等默认按文本绘制，Windows 上就是截图里那种灰色线稿。
  const kindIcons = {
    category: '\u{1F4DA}',            // 📚 主题（模式行图标；树内分类节点不带）
    workspace: '\u{1F5C2}\u{FE0F}',  // 🗂️
    environment: '\u{1F310}',        // 🌐
    terminal: '\u{1F4BB}',           // 💻
    terminal_layer: '\u{1F5A5}\u{FE0F}', // 🖥️ 嵌套终端层（自建的子终端）
    recent: '\u{1F550}\u{FE0F}',     // 🕐️
    history: '\u{1F4C5}',            // 📅
    month: '\u{1F4C6}',              // 📆
  }
  // 森林根已折叠进侧边栏的模式图标行、不再渲染为树节点：普通 category 节点
  // （含提为顶层的主题一级分类）一律不带图标，其余特殊类型按表带图标。
  const iconFor = $derived(
    nodeKind === 'category' ? '' : (kindIcons[nodeKind] || ''),
  )
  // 后端把 emoji 烘进根/特殊节点名字；显示时剥掉前缀，图标以本表为准。
  const displayName = $derived(
    nodeKind === 'category'
      ? node.name
      : (node.name.replace(/^[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE0F}\u{200D}\s]+/u, '').trim() || node.name),
  )

  function handleClick() {
    if (isTerminalAction) {
      // 终端节点的单击仅用于树形展开（ssh/docker 等子节点后续实现），
      // 不进入会话列表视图；打开终端用双击。
      if (categoryChildren.length) onToggle(path)
      return
    }
    if (terminal) onSelect(path)
    else onToggle(path)
  }

  function handleDblclick() {
    if (isTerminalAction && onOpenTerminalNode) onOpenTerminalNode(node, path)
  }

  function handleContextmenu(event) {
    if (!isTerminalAction || !(isTerminalEnv || isTerminalLayer) || !onNodeContextmenu) return
    event.preventDefault()
    event.stopPropagation()
    onNodeContextmenu(node, path, event)
  }
</script>

<div class="category-node">
  <button
    class="category-row"
    class:selected={!isTerminalAction && terminal && selectedCategory === path}
    data-category-path={path}
    style={`--category-depth: ${depth}`}
    onclick={handleClick}
    ondblclick={handleDblclick}
    oncontextmenu={handleContextmenu}
    title={node.command || node.workspace || node.environment || node.name}
  >
    <span class="category-arrow" class:leaf={terminal || (isTerminalAction && categoryChildren.length === 0)}>
      {isTerminalAction ? (categoryChildren.length ? (expanded ? '▾' : '▸') : '·') : (terminal ? '·' : (expanded ? '▾' : '▸'))}
    </span>
    {#if iconFor}
      <span class="category-kind" aria-hidden="true">{iconFor}</span>
    {/if}
    <span class="category-name">{displayName}</span>
    {#if !isTerminalAction}
      <span class="category-count">{node.session_count ?? 0}</span>
    {/if}
  </button>
  {#if expanded && categoryChildren.length}
    <div class="category-children">
      {#each categoryChildren as child (`${path}/${child.id}`)}
        <CategoryNode
          node={{ ...child, category: `${path}/${child.id}` }}
          depth={depth + 1}
          {expandedPaths}
          {selectedCategory}
          {onToggle}
          {onSelect}
          {onOpenTerminalNode}
          {onNodeContextmenu}
        />
      {/each}
    </div>
  {/if}
</div>

<style>
  .category-row {
    width: 100%;
    min-height: 30px;
    padding: 4px 8px 4px calc(8px + var(--category-depth) * 14px);
    border: none;
    background: transparent;
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    gap: 5px;
    font-size: 0.8rem;
    text-align: left;
    cursor: pointer;
  }
  .category-row:hover { background: var(--border); color: var(--text); }
  .category-row.selected { color: var(--primary); font-weight: 600; }
  .category-arrow { width: 12px; flex-shrink: 0; text-align: center; }
  .category-arrow.leaf { opacity: 0.45; }
  .category-kind { flex-shrink: 0; font-size: 0.85rem; line-height: 1; }
  .category-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .category-count { flex-shrink: 0; opacity: 0.5; font-size: 0.72rem; font-variant-numeric: tabular-nums; }
</style>
