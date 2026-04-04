<script>
  import { tools } from '../../lib/api.js'
  import { t } from '../../lib/i18n.svelte.js'

  let { selectedToolIds = $bindable([]), onchange } = $props()

  let toolList = $state([])
  let loading = $state(true)
  let error = $state('')
  let expanded = $state(false)
  let expandedGroups = $state(new Set())

  async function fetchTools() {
    loading = true
    error = ''
    try {
      const data = await tools.list()
      toolList = data.tools ?? []
      // 过滤掉已不存在的工具 ID，避免选中了已删除的工具
      const validIds = new Set(toolList.map(t => t.tool_id))
      selectedToolIds = selectedToolIds.filter(id => validIds.has(id))
    } catch (err) {
      error = err.message || t('fetchToolsFailed')
    } finally {
      loading = false
    }
  }

  let groups = $derived.by(() => {
    const map = new Map()
    for (const tool of toolList) {
      let key
      if (tool.tool_type === 'mcp' && tool.mcp_server_name) key = `mcp:${tool.mcp_server_name}`
      else if (tool.tool_type === 'function') key = 'function'
      else if (tool.tool_type === 'skill') key = 'skill'
      else key = tool.tool_type ?? 'other'
      if (!map.has(key)) map.set(key, [])
      map.get(key).push(tool)
    }
    return map
  })

  function groupLabel(key) {
    if (key.startsWith('mcp:')) return key.slice(4)
    if (key === 'function') return t('functionTools')
    if (key === 'skill') return t('skillTools')
    return key
  }

  function groupIcon(key) {
    if (key.startsWith('mcp:')) return '⚡'
    if (key === 'function') return '𝑓'
    if (key === 'skill') return '★'
    return '•'
  }

  function toggleTool(toolId) {
    if (selectedToolIds.includes(toolId)) selectedToolIds = selectedToolIds.filter(id => id !== toolId)
    else selectedToolIds = [...selectedToolIds, toolId]
    onchange?.(selectedToolIds)
  }

  function toggleGroup(key) {
    const ids = (groups.get(key) ?? []).map(t => t.tool_id)
    const allSelected = ids.every(id => selectedToolIds.includes(id))
    if (allSelected) selectedToolIds = selectedToolIds.filter(id => !ids.includes(id))
    else selectedToolIds = [...selectedToolIds, ...ids.filter(id => !selectedToolIds.includes(id))]
    onchange?.(selectedToolIds)
  }

  function groupSelectionState(key) {
    const ids = (groups.get(key) ?? []).map(t => t.tool_id)
    const selected = ids.filter(id => selectedToolIds.includes(id))
    if (selected.length === 0) return 'none'
    if (selected.length === ids.length) return 'all'
    return 'partial'
  }

  function toggleCollapse(key) {
    const next = new Set(expandedGroups)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    expandedGroups = next
  }

  $effect(() => { fetchTools() })
</script>

<div class="tool-selector">
  <button class="toggle-btn" onclick={() => expanded = !expanded}>
    {t('toolsButton', { count: selectedToolIds.length })}
    <span class="arrow">{expanded ? '▲' : '▼'}</span>
  </button>

  {#if expanded}
    <div class="tool-list">
      {#if loading}
        <span class="hint">{t('loading')}</span>
      {:else if error}
        <span class="hint error">{error}</span>
      {:else if toolList.length === 0}
        <span class="hint">{t('noTools')}</span>
      {:else}
        {#each [...groups.entries()] as [key, groupTools] (key)}
          {@const state = groupSelectionState(key)}
          {@const collapsed = !expandedGroups.has(key)}
          <div class="group">
            <div class="group-header">
              <button class="collapse-btn" onclick={() => toggleCollapse(key)} title={collapsed ? t('expand') : t('collapse')}>
                {collapsed ? '▶' : '▼'}
              </button>
              <label class="group-label">
                <input
                  type="checkbox"
                  checked={state === 'all'}
                  indeterminate={state === 'partial'}
                  onchange={() => toggleGroup(key)}
                />
                <span class="group-icon">{groupIcon(key)}</span>
                <span class="group-name">{groupLabel(key)}</span>
                <span class="group-count">({groupTools.length})</span>
              </label>
            </div>
            {#if !collapsed}
              <div class="group-tools">
                {#each groupTools as tool (tool.tool_id)}
                  <label class="tool-item">
                    <input type="checkbox" checked={selectedToolIds.includes(tool.tool_id)} onchange={() => toggleTool(tool.tool_id)} />
                    <span class="tool-name">{tool.name}</span>
                  </label>
                {/each}
              </div>
            {/if}
          </div>
        {/each}
      {/if}
    </div>
  {/if}
</div>

<style>
  .tool-selector { position: relative; }
  .toggle-btn {
    padding: 6px 12px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    font-size: 0.85rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .toggle-btn:hover { background: var(--bg-secondary); }
  .arrow { font-size: 0.7rem; }
  .tool-list {
    position: absolute;
    top: 100%;
    left: 0;
    margin-top: 4px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px;
    min-width: 240px;
    max-height: 320px;
    overflow-y: auto;
    z-index: 10;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
  }
  .group { margin-bottom: 2px; }
  .group-header { display: flex; align-items: center; gap: 4px; padding: 3px 2px; border-radius: 4px; }
  .group-header:hover { background: var(--bg-secondary); }
  .collapse-btn { background: none; border: none; cursor: pointer; color: var(--text-secondary); font-size: 0.65rem; padding: 0 2px; line-height: 1; flex-shrink: 0; }
  .group-label { display: flex; align-items: center; gap: 5px; cursor: pointer; font-size: 0.82rem; font-weight: 600; color: var(--text); flex: 1; }
  .group-icon { font-size: 0.75rem; color: var(--text-secondary); }
  .group-name { flex: 1; }
  .group-count { color: var(--text-secondary); font-weight: normal; font-size: 0.78rem; }
  .group-tools { padding-left: 28px; }
  .tool-item { display: flex; align-items: center; gap: 6px; padding: 3px 0; cursor: pointer; font-size: 0.82rem; color: var(--text); }
  .tool-item:hover { color: var(--primary, #4a9eff); }
  .hint { font-size: 0.8rem; color: var(--text-secondary); padding: 4px 0; }
  .hint.error { color: var(--danger); }
</style>
