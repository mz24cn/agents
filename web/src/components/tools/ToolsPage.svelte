<script>
  import { tools, mcpServers } from '../../lib/api.js'
  import ToolForm from './ToolForm.svelte'
  import ToolDetail from './ToolDetail.svelte'
  import ConfirmDialog from '../ConfirmDialog.svelte'
  import { t } from '../../lib/i18n.svelte.js'

  let toolList = $state([])
  let loading = $state(true)
  let error = $state('')
  let showForm = $state(false)
  let editingTool = $state(null)
  let deleteTarget = $state(null)
  let detailTool = $state(null)
  let expandedGroups = $state(new Set())

  async function fetchTools() {
    loading = true
    error = ''
    try {
      const data = await tools.list()
      toolList = data.tools ?? []
    } catch (err) {
      error = err.message || t('fetchToolListFailed')
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

  function isMcpGroup(key) { return key.startsWith('mcp:') }

  function toggleCollapse(key) {
    const next = new Set(expandedGroups)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    expandedGroups = next
  }

  function handleCreate() { editingTool = null; showForm = true }
  function handleEdit(tool) { editingTool = tool; showForm = true }

  let editingMcpServer = $state(null)

  async function handleEditMcpServer(key) {
    const serverName = key.slice(4)
    try {
      const data = await mcpServers.list()
      const serverCfg = data.mcpServers?.[serverName] ?? {}
      editingMcpServer = { serverName, config: { mcpServers: { [serverName]: serverCfg } } }
      editingTool = null
      showForm = true
    } catch (err) {
      error = err.message || t('fetchMcpConfigFailed')
    }
  }

  function handleFormSuccess() { showForm = false; editingTool = null; editingMcpServer = null; fetchTools() }
  function handleFormCancel() { showForm = false; editingTool = null; editingMcpServer = null }
  function handleDeleteClick(tool) { deleteTarget = { type: 'single', tool } }

  function handleDeleteGroupClick(key) {
    const serverName = key.slice(4)
    const ids = (groups.get(key) ?? []).map(t => t.tool_id)
    deleteTarget = { type: 'group', key, label: groupLabel(key), ids, serverName }
  }

  async function handleDeleteConfirm() {
    if (!deleteTarget) return
    const target = deleteTarget
    deleteTarget = null
    try {
      if (target.type === 'single') await tools.delete(target.tool.tool_id)
      else await mcpServers.delete(target.serverName)
      fetchTools()
    } catch (err) {
      error = err.message || t('deleteToolFailed')
    }
  }

  function handleDeleteCancel() { deleteTarget = null }
  function handleShowDetail(tool) { detailTool = tool }
  function handleCloseDetail() { detailTool = null }

  let confirmTitle = $derived(
    deleteTarget?.type === 'group'
      ? t('confirmDeleteMcpServer', { label: deleteTarget.label, count: deleteTarget.ids.length })
      : t('confirmDeleteTool', { id: deleteTarget?.tool?.tool_id ?? '' })
  )

  $effect(() => { fetchTools() })
</script>

<div class="tools-page">
  <div class="page-header">
    <h2>{t('toolsPageTitle')}</h2>
    {#if !showForm}
      <button class="btn btn-primary" onclick={handleCreate}>{t('registerTool')}</button>
    {/if}
  </div>

  {#if showForm}
    {#key editingTool?.tool_id ?? editingMcpServer?.serverName ?? '__new__'}
      <ToolForm tool={editingTool} mcpServer={editingMcpServer} onSuccess={handleFormSuccess} onCancel={handleFormCancel} />
    {/key}
  {/if}

  {#if detailTool}
    <ToolDetail tool={detailTool} onClose={handleCloseDetail} />
  {/if}

  {#if error}
    <div class="error-msg">{error}</div>
  {/if}

  <div class="page-content">
    {#if loading}
      <div class="loading">{t('loading')}</div>
    {:else if toolList.length === 0 && !error}
      <div class="empty">{t('noTools2')}</div>
    {:else if toolList.length > 0}
      <div class="groups-wrap">
        {#each [...groups.entries()] as [key, groupTools] (key)}
          {@const collapsed = !expandedGroups.has(key)}
          <div class="group-block">
            <div class="group-header">
              <button class="collapse-btn" onclick={() => toggleCollapse(key)}>
                {collapsed ? '▶' : '▼'}
              </button>
              <span class="group-title">
                {#if isMcpGroup(key)}
                  <span class="badge badge-mcp">MCP</span>
                {/if}
                {groupLabel(key)}
                <span class="group-count">{t('toolCount', { n: groupTools.length })}</span>
              </span>
              {#if isMcpGroup(key)}
                <button class="btn btn-sm" onclick={() => handleEditMcpServer(key)} title={t('editMcpServerTitle')}>{t('edit')}</button>
                <button class="btn btn-sm btn-danger" onclick={() => handleDeleteGroupClick(key)} title={t('deleteMcpServerTitle')}>{t('delete')}</button>
              {/if}
            </div>

            {#if !collapsed}
              <table>
                <thead>
                  <tr>
                    <th>{t('toolIdHeader')}</th>
                    <th>{t('toolNameHeader')}</th>
                    <th>{t('toolDescHeader')}</th>
                    <th>{t('actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {#each groupTools as tool (tool.tool_id)}
                    <tr>
                      <td class="id-cell">{tool.tool_id}</td>
                      <td class="name-cell">
                        <button class="link-btn" onclick={() => handleShowDetail(tool)}>{tool.name}</button>
                      </td>
                      <td class="desc-cell">{tool.description}</td>
                      <td class="actions">
                        {#if !isMcpGroup(key) && !tool.builtin}
                          <button class="btn btn-sm" onclick={() => handleEdit(tool)}>{t('edit')}</button>
                        {/if}
                        {#if !tool.builtin}
                          <button class="btn btn-sm btn-danger" onclick={() => handleDeleteClick(tool)}>{t('delete')}</button>
                        {:else}
                          <span class="badge badge-builtin">{t('builtin')}</span>
                        {/if}
                      </td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            {/if}
          </div>
        {/each}
      </div>
    {/if}
  </div>
</div>

<ConfirmDialog
  open={deleteTarget !== null}
  title={confirmTitle}
  onConfirm={handleDeleteConfirm}
  onCancel={handleDeleteCancel}
/>

<style>
  .tools-page {
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
  }
  .page-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 20px;
    flex-shrink: 0;
    border-bottom: 1px solid var(--border);
    background: var(--bg);
  }
  h2 { margin: 0; color: var(--text); }
  .btn { padding: 8px 18px; border-radius: 6px; border: none; cursor: pointer; font-size: 0.9rem; }
  .btn-primary { background: var(--primary); color: #fff; }
  .btn-primary:hover { background: var(--primary-hover); }
  .btn-sm { padding: 4px 10px; font-size: 0.82rem; background: var(--bg-secondary); color: var(--text); border: 1px solid var(--border); border-radius: 4px; cursor: pointer; }
  .btn-sm:hover { opacity: 0.8; }
  .btn-danger { background: var(--danger); color: #fff; border: none; }
  .btn-danger:hover { background: var(--danger-hover, #c0392b); }
  .link-btn { background: none; border: none; color: var(--primary); cursor: pointer; padding: 0; font-size: inherit; text-decoration: underline; }
  .link-btn:hover { opacity: 0.8; }
  .error-msg { background: var(--danger); color: #fff; padding: 10px 14px; border-radius: 6px; margin: 0 20px; font-size: 0.9rem; flex-shrink: 0; }
  .page-content {
    flex: 1;
    overflow-y: auto;
    min-height: 0;
    padding: 20px;
  }
  .loading, .empty { text-align: center; padding: 40px 0; color: var(--text-secondary); }
  .groups-wrap { display: flex; flex-direction: column; gap: 12px; }
  .group-block { border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  .group-header { display: flex; align-items: center; gap: 8px; padding: 8px 12px; background: var(--bg-secondary); border-bottom: 1px solid var(--border); }
  .collapse-btn { background: none; border: none; cursor: pointer; color: var(--text-secondary); font-size: 0.65rem; padding: 0 2px; flex-shrink: 0; }
  .group-title { flex: 1; display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 0.9rem; color: var(--text); }
  .badge { font-size: 0.7rem; padding: 1px 6px; border-radius: 4px; font-weight: 700; }
  .badge-mcp { background: #2563eb22; color: #2563eb; }
  .badge-builtin { font-size: 0.7rem; padding: 1px 6px; border-radius: 4px; font-weight: 700; background: #16a34a22; color: #16a34a; }
  .group-count { font-weight: normal; font-size: 0.8rem; color: var(--text-secondary); }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border); }
  th { background: var(--bg); color: var(--text-secondary); font-weight: 600; font-size: 0.82rem; }
  td { font-size: 0.88rem; color: var(--text); }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr:hover td { background: var(--bg-secondary); }
  .id-cell { font-family: monospace; font-size: 0.8rem; color: var(--text-secondary); white-space: nowrap; }
  .name-cell { white-space: nowrap; }
  .desc-cell { max-width: 0; width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .actions { display: flex; gap: 6px; white-space: nowrap; }
  
  /* 滚动条样式：默认隐藏，悬停时显示 */
  .page-content::-webkit-scrollbar {
    width: 8px;
  }
  .page-content::-webkit-scrollbar-track {
    background: transparent;
  }
  .page-content::-webkit-scrollbar-thumb {
    background: transparent;
    border-radius: 4px;
    transition: background 0.2s;
  }
  .page-content:hover::-webkit-scrollbar-thumb {
    background: var(--border);
  }
  .page-content:hover::-webkit-scrollbar-thumb:hover {
    background: var(--text-secondary);
  }
</style>
