<script>
  import { catalog, loadAgents } from '../../lib/catalog-state.svelte.js'
  import { t } from '../../lib/i18n.svelte.js'
  import IconDisplay from '../../lib/components/IconDisplay.svelte'

  let { selectedAgentId = $bindable(''), onchange, disabled = false } = $props()

  let agentList = $derived(catalog.agents.items)
  let loading = $derived(catalog.agents.loading && !catalog.agents.loaded)
  let error = $derived(catalog.agents.error)
  let expanded = $state(false)

  // 找到当前选中的agent
  let selectedAgent = $derived(agentList.find(a => a.agent_id === selectedAgentId))

  function handleSelect(agentId) {
    selectedAgentId = agentId
    expanded = false
    onchange?.(agentId)
  }

  function handleToggle() {
    if (!disabled) {
      expanded = !expanded
    }
  }

  // 点击外部关闭下拉框
  function handleClickOutside(event) {
    if (!event.target.closest('.agent-selector')) {
      expanded = false
    }
  }

  $effect(() => { loadAgents().catch(() => {}) })

  // 过滤掉已不存在的agent
  $effect(() => {
    if (!catalog.agents.loaded) return
    const validIds = new Set(agentList.map(a => a.agent_id))
    if (selectedAgentId && !validIds.has(selectedAgentId)) {
      selectedAgentId = ''
      onchange?.('')
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
    onclick={handleToggle} 
    disabled={disabled}
  >
    {#if selectedAgent}
      <IconDisplay 
        value={selectedAgent.avatar} 
        alt={selectedAgent.nickname} 
        size={16} 
        class="agent-icon"
        fallback="🤖"
      />
      <span class="agent-name">{selectedAgent.nickname}</span>
    {:else}
      <span class="agent-name">—</span>
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
        <button 
          type="button" 
          class="agent-item" 
          class:selected={selectedAgentId === ''}
          onclick={() => handleSelect('')}
        >
          <span class="agent-icon-placeholder"></span>
          <span class="agent-name">—</span>
        </button>
        {#each agentList as agent (agent.agent_id)}
          <button 
            type="button" 
            class="agent-item" 
            class:selected={selectedAgentId === agent.agent_id}
            onclick={() => handleSelect(agent.agent_id)}
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
    max-height: 320px;
    overflow-y: auto;
    z-index: 10;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
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
  .agent-item.selected { background: var(--bg-secondary); font-weight: 600; }
  .agent-icon-placeholder {
    width: 16px;
    height: 16px;
    flex-shrink: 0;
  }
  .agent-icon {
    flex-shrink: 0;
    width: 16px;
    height: 16px;
  }
  .hint { font-size: 0.8rem; color: var(--text-secondary); padding: 4px 8px; }
  .hint.error { color: var(--danger); }
</style>