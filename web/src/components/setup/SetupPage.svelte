<script>
  import { router, getQueryParam } from '../../lib/router.svelte.js'
  import ThemeToggle from '../ThemeToggle.svelte'
  import AuthSettingsPage from './AuthSettingsPage.svelte'
  import { t, i18n, setLang } from '../../lib/i18n.svelte.js'
  import { catalog, loadPromptTemplates, refreshModels, refreshTools, refreshPromptTemplates, refreshAgents, refreshEnvVars } from '../../lib/catalog-state.svelte.js'
  import ModelsPage from '../models/ModelsPage.svelte'
  import ToolsPage from '../tools/ToolsPage.svelte'
  import PromptsPage from '../prompts/PromptsPage.svelte'
  import AgentsPage from '../agents/AgentsPage.svelte'
  import EnvPage from '../env/EnvPage.svelte'
  import ModelForm from '../models/ModelForm.svelte'
  import ToolForm from '../tools/ToolForm.svelte'
  import McpToolForm from '../tools/McpToolForm.svelte'
  import PromptForm from '../prompts/PromptForm.svelte'
  import AgentForm from '../agents/AgentForm.svelte'
  import { mcpServers } from '../../lib/api.js'

  const validTabs = ['models', 'tools', 'prompts', 'agents', 'env', 'auth', 'model-add', 'tool-add', 'prompt-add', 'agent-add', 'env-add', 'model-edit', 'tool-edit', 'mcp-tool-edit', 'prompt-edit', 'agent-edit']
  const initialTab = validTabs.includes(getQueryParam('tab')) ? getQueryParam('tab') : 'agents'
  let activeTab = $state(initialTab)
  let envDetectTrigger = $state(0)
  let authRefreshTrigger = $state(0)
  let refreshing = $state(false)

  let editingModel = $state(null)
  let editingTool = $state(null)
  let editingMcpTool = $state(null)
  let editingPrompt = $state(null)
  let editingAgent = $state(null)

  $effect(() => {
    const hash = router.current
    const tabParam = (() => {
      const q = hash.split('?')[1] || ''
      const p = new URLSearchParams(q)
      return p.get('tab') || ''
    })()
    if (validTabs.includes(tabParam)) {
      activeTab = tabParam
    }
  })

  $effect(() => {
    const tab = activeTab
    if (tab === 'prompt-edit') {
      const hash = router.current
      const params = new URLSearchParams(hash.split('?')[1] || '')
      const templateId = params.get('templateId') || ''
      if (templateId) {
        loadPromptTemplates().then(() => {
          const found = catalog.promptTemplates.items.find(t => t.template_id === templateId)
          if (found) editingPrompt = found
        })
      }
    }
  })

  function resetToDefaultTab() {
    activeTab = 'agents'
    editingModel = null
    editingTool = null
    editingPrompt = null
    editingAgent = null
  }

  $effect(() => {
    window.addEventListener('setup:reset', resetToDefaultTab)
    return () => window.removeEventListener('setup:reset', resetToDefaultTab)
  })

  const tabGroups = $derived.by(() => [
    {
      items: [
        { id: 'models', label: t('models'), icon: '📦' },
        { id: 'model-add', label: '✚', icon: '✚' },
      ]
    },
    {
      items: [
        { id: 'tools', label: t('tools'), icon: '🛠️' },
        { id: 'tool-add', label: '✚', icon: '✚' },
      ]
    },
    {
      items: [
        { id: 'prompts', label: t('prompts'), icon: '📝' },
        { id: 'prompt-add', label: '✚', icon: '✚' },
      ]
    },
    {
      items: [
        { id: 'agents', label: t('agents'), icon: '🤖' },
        { id: 'agent-add', label: '✚', icon: '✚' },
      ]
    },
    {
      items: [
        { id: 'env', label: t('nav_env'), icon: '🔑' },
        { id: 'env-add', label: '✚', icon: '✚' },
        { id: 'env-detect', label: '📡', icon: '📡', action: 'detect' },
      ]
    },
    {
      items: [
        { id: 'auth', label: t('nav_auth'), icon: '🔐' },
      ]
    },
  ])

  function handleTabClick(id) {
    activeTab = id
  }

  function getBaseCategory(tab) {
    if (tab.startsWith('model')) return 'models'
    if (tab.startsWith('tool')) return 'tools'
    if (tab.startsWith('prompt')) return 'prompts'
    if (tab.startsWith('agent')) return 'agents'
    if (tab.startsWith('env')) return 'env'
    return tab
  }

  async function handleRefresh() {
    if (refreshing) return
    refreshing = true
    const category = getBaseCategory(activeTab)
    try {
      if (category === 'models') await refreshModels()
      else if (category === 'tools') await refreshTools()
      else if (category === 'prompts') await refreshPromptTemplates()
      else if (category === 'agents') await refreshAgents()
      else if (category === 'env') await refreshEnvVars()
      else if (category === 'auth') authRefreshTrigger += 1
    } catch { /* errors are shown in each page */ }
    refreshing = false
  }

  function handleModelFormSuccess() {
    activeTab = 'models'
  }

  function handleToolFormSuccess() {
    activeTab = 'tools'
  }

  function handlePromptFormSuccess() {
    activeTab = 'prompts'
  }

  function handleAgentFormSuccess() {
    activeTab = 'agents'
  }

  function handleFormCancel() {
    if (activeTab === 'model-add' || activeTab === 'model-edit') activeTab = 'models'
    else if (activeTab === 'tool-add' || activeTab === 'tool-edit' || activeTab === 'mcp-tool-edit') activeTab = 'tools'
    else if (activeTab === 'prompt-add' || activeTab === 'prompt-edit') activeTab = 'prompts'
    else if (activeTab === 'agent-add' || activeTab === 'agent-edit') activeTab = 'agents'
    editingModel = null
    editingTool = null
    editingMcpTool = null
    editingPrompt = null
    editingAgent = null
  }

  function handleEditModel(model) {
    editingModel = model
    activeTab = 'model-edit'
  }

  function handleCopyModel(model) {
    const copied = { ...model, model_id: '' }
    editingModel = copied
    activeTab = 'model-edit'
  }

  function handleEditTool(tool, serverName) {
    if (tool.tool_type === 'mcp' && serverName) {
      mcpServers.list().then(data => {
        const config = data.mcpServers?.[serverName]
        if (config) {
          editingTool = { ...tool, mcpServerConfig: config, mcpServerName: serverName }
          activeTab = 'tool-edit'
        }
      }).catch(() => {
        editingTool = tool
        activeTab = 'tool-edit'
      })
    } else {
      editingTool = tool
      activeTab = 'tool-edit'
    }
  }

  function handleEditMcpTool(tool) {
    editingMcpTool = tool
    activeTab = 'mcp-tool-edit'
  }

  function handleEditPrompt(prompt) {
    editingPrompt = prompt
    activeTab = 'prompt-edit'
  }

  function handleCopyPrompt(prompt) {
    const copied = { ...prompt, template_id: '' }
    editingPrompt = copied
    activeTab = 'prompt-edit'
  }

  function handleEditAgent(agent) {
    editingAgent = agent
    activeTab = 'agent-edit'
  }

  function handleCopyAgent(agent) {
    const copied = { ...agent, agent_id: '' }
    editingAgent = copied
    activeTab = 'agent-edit'
  }
</script>

<div class="setup-page">
  <div class="setup-header">
    <div class="setup-header-row">
      <div class="tabs">
        {#each tabGroups as group}
          <div class="tab-group">
            {#each group.items as tab}
              <button
                class="tab-btn"
                class:active={activeTab === tab.id}
                onclick={() => {
                  if (tab.action === 'detect') {
                    activeTab = 'env'
                    envDetectTrigger += 1
                  } else {
                    handleTabClick(tab.id)
                  }
                }}
                title={tab.id.includes('-add') ? t('addNew') : (tab.action === 'detect' ? t('detectEnvVars') : tab.label)}
              >
                {tab.action === 'detect' ? tab.label : (tab.id.includes('-add') ? tab.label : tab.icon + ' ' + tab.label)}
              </button>
            {/each}
          </div>
        {/each}
      </div>
      <div class="setup-header-actions">
        <div class="setup-theme-wrap">
          <ThemeToggle />
        </div>
        <button
          class="setup-lang-btn"
          onclick={() => setLang(i18n.lang === 'zh' ? 'en' : 'zh')}
          title={t('languageSwitch')}
        >{i18n.lang === 'zh' ? '中' : 'En'}</button>
        <button
          class="refresh-btn"
          onclick={handleRefresh}
          disabled={refreshing}
          title={t('refreshCurrentTab')}
        >
          {refreshing ? '⏳' : '🔄'}
        </button>
      </div>
    </div>
  </div>

  <div class="setup-content">
    {#if activeTab === 'models'}
      <ModelsPage onEdit={handleEditModel} onCopy={handleCopyModel} />
    {:else if activeTab === 'model-add'}
      <div class="form-wrapper">
        <ModelForm onSuccess={handleModelFormSuccess} onCancel={handleFormCancel} />
      </div>
    {:else if activeTab === 'model-edit'}
      <div class="form-wrapper">
        <ModelForm model={editingModel} onSuccess={handleModelFormSuccess} onCancel={handleFormCancel} />
      </div>
    {:else if activeTab === 'tools'}
      <ToolsPage onEdit={handleEditTool} onEditMcpTool={handleEditMcpTool} />
    {:else if activeTab === 'tool-add'}
      <div class="form-wrapper">
        <ToolForm onSuccess={handleToolFormSuccess} onCancel={handleFormCancel} />
      </div>
    {:else if activeTab === 'tool-edit'}
      <div class="form-wrapper">
        <ToolForm tool={editingTool} onSuccess={handleToolFormSuccess} onCancel={handleFormCancel} />
      </div>
    {:else if activeTab === 'mcp-tool-edit'}
      <div class="form-wrapper">
        <McpToolForm tool={editingMcpTool} onSuccess={handleToolFormSuccess} onCancel={handleFormCancel} />
      </div>
    {:else if activeTab === 'prompts'}
      <PromptsPage onEdit={handleEditPrompt} onCopy={handleCopyPrompt} />
    {:else if activeTab === 'prompt-add'}
      <div class="form-wrapper">
        <PromptForm onSuccess={handlePromptFormSuccess} onCancel={handleFormCancel} />
      </div>
    {:else if activeTab === 'prompt-edit'}
      <div class="form-wrapper">
        <PromptForm template={editingPrompt} onSuccess={handlePromptFormSuccess} onCancel={handleFormCancel} />
      </div>
    {:else if activeTab === 'agents'}
      <AgentsPage onEdit={handleEditAgent} onCopy={handleCopyAgent} />
    {:else if activeTab === 'agent-add'}
      <div class="form-wrapper">
        <AgentForm onSuccess={handleAgentFormSuccess} onCancel={handleFormCancel} />
      </div>
    {:else if activeTab === 'agent-edit'}
      <div class="form-wrapper">
        <AgentForm agent={editingAgent} onSuccess={handleAgentFormSuccess} onCancel={handleFormCancel} />
      </div>
    {:else if activeTab === 'env'}
      <EnvPage triggerDetect={envDetectTrigger} />
    {:else if activeTab === 'env-add'}
      <div class="form-wrapper">
        <EnvPage showAddForm={true} />
      </div>
    {:else if activeTab === 'auth'}
      {#key authRefreshTrigger}
        <AuthSettingsPage />
      {/key}
    {/if}
  </div>
</div>

<style>
  .setup-page {
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
  }
  .setup-header {
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    padding: 12px 16px;
    flex-shrink: 0;
  }
  .setup-header-row {
    display: flex;
    align-items: flex-start;
    gap: 12px;
  }
  .tabs {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    flex: 1 1 auto;
    min-width: 0;
  }
  .setup-header-actions {
    margin-left: auto;
    display: flex;
    align-items: stretch;
    justify-content: flex-end;
    gap: 8px;
    flex: 0 0 auto;
  }
  .setup-theme-wrap {
    width: 38px;
    height: 36px;
    display: flex;
  }
  .setup-theme-wrap :global(.theme-toggle) {
    flex: 1;
    width: 100%;
    height: 100%;
    padding: 0;
    box-sizing: border-box;
    background: var(--bg-secondary);
    color: var(--text-secondary);
  }
  .setup-theme-wrap :global(.theme-toggle:hover) {
    background: var(--border);
    color: var(--text);
  }
  .setup-lang-btn {
    width: 38px;
    height: 36px;
    padding: 0;
    box-sizing: border-box;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg-secondary);
    color: var(--text-secondary);
    font-size: 0.88rem;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
  }
  .setup-lang-btn:hover {
    background: var(--border);
    color: var(--text);
  }
  .tab-group {
    display: flex;
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
  }
  .tab-btn {
    padding: 8px 14px;
    border-radius: 0;
    border: none;
    border-right: 1px solid var(--border);
    background: var(--bg-secondary);
    color: var(--text-secondary);
    font-size: 0.88rem;
    cursor: pointer;
    transition: all 0.15s;
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .tab-group .tab-btn:first-child {
    border-radius: 5px 0 0 5px;
  }
  .tab-group .tab-btn:last-child {
    border-radius: 0 5px 5px 0;
    border-right: none;
  }
  .tab-btn:hover {
    background: var(--border);
    color: var(--text);
  }
  .tab-btn.active {
    background: var(--primary);
    color: #fff;
  }
  .refresh-btn {
    width: 38px;
    height: 36px;
    padding: 0;
    box-sizing: border-box;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg-secondary);
    color: var(--text-secondary);
    font-size: 1rem;
    cursor: pointer;
    transition: all 0.15s;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .refresh-btn:hover:not(:disabled) {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary);
  }
  .refresh-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .tab-group:has(.tab-btn.active) {
    border-color: var(--primary);
  }
  .setup-content {
    flex: 1;
    overflow: hidden;
    min-height: 0;
  }
  .form-wrapper {
    height: 100%;
    padding: 20px;
    overflow-y: auto;
  }
  
  /* 滚动条样式：默认隐藏，悬停时显示 */
  .form-wrapper::-webkit-scrollbar {
    width: 8px;
  }
  .form-wrapper::-webkit-scrollbar-track {
    background: transparent;
  }
  .form-wrapper::-webkit-scrollbar-thumb {
    background: transparent;
    border-radius: 4px;
    transition: background 0.2s;
  }
  .form-wrapper:hover::-webkit-scrollbar-thumb {
    background: var(--border);
  }
  .form-wrapper:hover::-webkit-scrollbar-thumb:hover {
    background: var(--text-secondary);
  }
  </style>