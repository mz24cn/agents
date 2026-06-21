<script>
  import { catalog, loadAgents } from '../../lib/catalog-state.svelte.js'
  import { t } from '../../lib/i18n.svelte.js'
  import IconDisplay from '../../lib/components/IconDisplay.svelte'

  let { selectedAgentIds = $bindable([]), onchange, disabled = false } = $props()

  let agentList = $derived(catalog.agents.items)
  let loading = $derived(catalog.agents.loading && !catalog.agents.loaded)
  let error = $derived(catalog.agents.error)
  let expanded = $state(false)
  let multiSelectMode = $state(false)

  // 按第一项标签分组
  let groupedAgents = $derived.by(() => {
    const groups = new Map()
    const noTagAgents = []
    
    for (const agent of agentList) {
      const firstTag = (agent.labels && agent.labels.length > 0) ? agent.labels[0] : ''
      if (!firstTag) {
        noTagAgents.push(agent)
      } else {
        if (!groups.has(firstTag)) groups.set(firstTag, [])
        groups.get(firstTag).push(agent)
      }
    }
    
    // 按标签名排序，空标签放在最后
    const sortedGroups = [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]))
    
    return { tagged: sortedGroups, untagged: noTagAgents }
  })

  // 获取选中的智能体列表
  let selectedAgents = $derived(
    agentList.filter(a => selectedAgentIds.includes(a.agent_id))
  )

  function handleSelect(agentId, event) {
    if (event.ctrlKey || event.metaKey) {
      // Ctrl/Cmd 点击：切换多选
      multiSelectMode = true
      if (selectedAgentIds.includes(agentId)) {
        selectedAgentIds = selectedAgentIds.filter(id => id !== agentId)
      } else {
        selectedAgentIds = [...selectedAgentIds, agentId]
      }
      // 多选模式下保持下拉框打开
      onchange?.(selectedAgentIds)
    } else {
      // 普通点击：单选并关闭
      if (multiSelectMode && selectedAgentIds.length > 0) {
        // 如果在多选模式中，单击切换该项但保持多选模式
        if (selectedAgentIds.includes(agentId)) {
          selectedAgentIds = selectedAgentIds.filter(id => id !== agentId)
        } else {
          selectedAgentIds = [...selectedAgentIds, agentId]
        }
      } else {
        // 普通单选模式
        selectedAgentIds = agentId ? [agentId] : []
        expanded = false
        multiSelectMode = false
      }
      onchange?.(selectedAgentIds)
    }
  }

  function handleClear() {
    selectedAgentIds = []
    multiSelectMode = false
    onchange?.([])
  }

  function handleToggle() {
    if (!disabled) {
      expanded = !expanded
      if (!expanded) {
        multiSelectMode = false
      }
    }
  }

  // 点击外部关闭下拉框
  function handleClickOutside(event) {
    if (!event.target.closest('.agent-selector')) {
      expanded = false
      multiSelectMode = false
    }
  }

  $effect(() => { loadAgents().catch(() => {}) })

  // 过滤掉已不存在的agent
  $effect(() => {
    if (!catalog.agents.loaded) return
    const validIds = new Set(agentList.map(a => a.agent_id))
    const validSelected = selectedAgentIds.filter(id => validIds.has(id))
    if (validSelected.length !== selectedAgentIds.length) {
      selectedAgentIds = validSelected
      onchange?.(selectedAgentIds)
    }
  })

  // 添加全局点击事件监听
  $effect(() => {
    document.addEventListener('click', handleClickOutside)
    return () => document.removeEventListener('click', handleClickOutside)
  })
</script>

<div class="agent-selector">
  <button 
    type="button" 
    class="toggle-btn" 
    class:has-selection={selectedAgentIds.length > 0}
    onclick={handleToggle} 
    disabled={disabled}
  >
    {#if selectedAgentIds.length === 0}
      <span class="agent-name">—</span>
    {:else if selectedAgentIds.length === 1}
      {@const agent = selectedAgents[0]}
      {#if agent}
        <IconDisplay 
          value={agent.avatar} 
          alt={agent.nickname} 
          size={16} 
          class="agent-icon"
          fallback="🤖"
        />
        <span class="agent-name">{agent.nickname}{agent.myself_view ? ` (${agent.myself_view})` : ''}</span>
      {/if}
    {:else}
      <span class="agent-name">{selectedAgentIds.length} {t('agentsSelected') || 'agents selected'}</span>
    {/if}
    <span class="arrow">{expanded ? '▲' : '▼'}</span>
  </button>

  {#if expanded}
    <div class="agent-list">
      {#if loading}
        <span class="hint">{t('loading')}</span>
      {:else if error}
        <span class="hint error">{error}</span>
      {:else if agentList.length === 0}
        <span class="hint">{t('noAgents')}</span>
      {:else}
        <!-- 无标签的智能体（默认组） -->
        {#each groupedAgents.untagged as agent (agent.agent_id)}
          <button 
            type="button" 
            class="agent-item" 
            class:selected={selectedAgentIds.includes(agent.agent_id)}
            onclick={(e) => handleSelect(agent.agent_id, e)}
          >
            <IconDisplay 
              value={agent.avatar} 
              alt={agent.nickname} 
              size={16} 
              class="agent-icon"
              fallback="🤖"
            />
            <span class="agent-name">{agent.nickname}{agent.myself_view ? ` (${agent.myself_view})` : ''}</span>
          </button>
        {/each}
        
        <!-- 有标签的智能体分组 -->
        {#each groupedAgents.tagged as [tag, agents] (tag)}
          <div class="agent-group">
            <div class="group-header">
              <span class="group-label">{tag}</span>
            </div>
            {#each agents as agent (agent.agent_id)}
              <button 
                type="button" 
                class="agent-item" 
                class:selected={selectedAgentIds.includes(agent.agent_id)}
                onclick={(e) => handleSelect(agent.agent_id, e)}
              >
                <IconDisplay 
                  value={agent.avatar} 
                  alt={agent.nickname} 
                  size={16} 
                  class="agent-icon"
                  fallback="🤖"
                />
                <span class="agent-name">{agent.nickname}{agent.myself_view ? ` (${agent.myself_view})` : ''}</span>
              </button>
            {/each}
          </div>
        {/each}
      {/if}
      
      <!-- 底部操作栏 -->
      {#if selectedAgentIds.length > 0}
        <div class="bottom-bar">
          <button type="button" class="clear-btn" onclick={handleClear}>
            {t('clearSelection') || 'Clear'}
          </button>
        </div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .agent-selector { position: relative; }
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
    min-width: 180px;
    max-width: 250px;
    text-align: left;
    overflow: hidden;
  }
  .toggle-btn:hover { background: var(--bg-secondary); }
  .toggle-btn:disabled { opacity: 0.6; cursor: not-allowed; }
  .toggle-btn.has-selection {
    border-color: var(--primary, #4a9eff);
    background: color-mix(in srgb, var(--primary, #4a9eff) 5%, var(--bg));
  }
  .arrow { font-size: 0.7rem; margin-left: auto; flex-shrink: 0; }
  .agent-name { 
    flex: 1; 
    white-space: nowrap; 
    overflow: hidden; 
    text-overflow: ellipsis; 
  }
  .agent-list {
    position: absolute;
    top: 100%;
    left: 0;
    margin-top: 4px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 4px;
    min-width: 240px;
    max-height: 400px;
    overflow-y: auto;
    z-index: 10;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
  }
  .agent-group {
    margin-bottom: 2px;
  }
  .group-header {
    padding: 6px 8px 4px 8px;
    position: sticky;
    top: 0;
    background: var(--bg);
    z-index: 1;
  }
  .group-label {
    font-size: 0.75rem;
    font-weight: 600;
    color: #10b981;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .agent-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 8px;
    cursor: pointer;
    font-size: 0.82rem;
    color: var(--text);
    border: none;
    background: none;
    width: 100%;
    text-align: left;
    border-radius: 4px;
  }
  .agent-item:hover { background: var(--bg-secondary); color: var(--primary, #4a9eff); }
  .agent-item.selected { 
    background: var(--primary, #4a9eff);
    color: white;
  }
  .agent-item.selected:hover {
    background: color-mix(in srgb, var(--primary, #4a9eff) 80%, black);
    color: white;
  }
  .agent-icon {
    flex-shrink: 0;
    width: 16px;
    height: 16px;
  }
  .hint { font-size: 0.8rem; color: var(--text-secondary); padding: 4px 8px; }
  .hint.error { color: var(--danger); }
  .bottom-bar {
    padding: 6px 8px;
    border-top: 1px solid var(--border);
    margin-top: 4px;
    display: flex;
    justify-content: flex-end;
  }
  .clear-btn {
    padding: 4px 10px;
    font-size: 0.78rem;
    color: var(--text-secondary);
    background: none;
    border: 1px solid var(--border);
    border-radius: 4px;
    cursor: pointer;
  }
  .clear-btn:hover {
    background: var(--bg-secondary);
    color: var(--text);
  }
</style>
