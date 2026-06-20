<script>
  import { tools, mcpServers } from '../../lib/api.js'
  import { catalog, loadTools, refreshTools } from '../../lib/catalog-state.svelte.js'
  import ToolDetail from './ToolDetail.svelte'
  import ConfirmDialog from '../ConfirmDialog.svelte'
  import { t } from '../../lib/i18n.svelte.js'

  let { onEdit = null, onEditMcpTool = null } = $props()

  let toolList = $derived(catalog.tools.items)
  let loading = $derived(catalog.tools.loading && !catalog.tools.loaded)
  let error = $derived(catalog.tools.error)
  let deleteTarget = $state(null)
  let detailTool = $state(null)
  let expandedGroups = $state(new Set())
  let mcpServerConfigs = $state({})

  async function fetchTools({ force = false } = {}) {
    try {
      if (force) await refreshTools()
      else await loadTools()
    } catch {
      catalog.tools.error = catalog.tools.error || t('fetchToolListFailed')
    }
    // Also load MCP server configs to show connection type
    try {
      const data = await mcpServers.list()
      mcpServerConfigs = data.mcpServers ?? {}
    } catch { /* ignore */ }
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

  function mcpServerType(key) {
    if (!isMcpGroup(key)) return ''
    const serverName = key.slice(4)
    const cfg = mcpServerConfigs[serverName]
    if (cfg?.url) return cfg.url
    return 'stdio'
  }

  function toggleCollapse(key) {
    const next = new Set(expandedGroups)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    expandedGroups = next
  }

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
      await fetchTools({ force: true })
    } catch (err) {
      catalog.tools.error = err.message || t('deleteToolFailed')
    }
  }

  function handleDeleteCancel() { deleteTarget = null }
  function handleShowDetail(tool) { detailTool = tool }
  function handleCloseDetail() { detailTool = null }

  function handleEditGroupClick(key) {
    const serverName = key.slice(4)
    const firstTool = (groups.get(key) ?? [])[0]
    if (onEdit && firstTool) {
      onEdit(firstTool, serverName)
    }
  }

  function handleEditClick(tool) {
    if (onEdit) onEdit(tool, null)
  }

  function handleMcpToolEditClick(tool) {
    if (onEditMcpTool) onEditMcpTool(tool)
  }

  let confirmTitle = $derived(
    deleteTarget?.type === 'group'
      ? t('confirmDeleteMcpServer', { label: deleteTarget.label, count: deleteTarget.ids.length })
      : t('confirmDeleteTool', { id: deleteTarget?.tool?.tool_id ?? '' })
  )

  $effect(() => { fetchTools() })
</script>

<div class="tools-page">
  {#if error}
    <div class="error-msg">{error}</div>
  {/if}

  <div class="page-content">
    {#if detailTool}
      <ToolDetail tool={detailTool} onClose={handleCloseDetail} />
    {/if}

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
                <span class="mcp-server-type" title={mcpServerType(key)}>{mcpServerType(key)}</span>
                <button class="btn btn-sm" onclick={() => handleEditGroupClick(key)} title={t('editMcpServerTitle')}>{t('edit')}</button>
                <button class="btn btn-sm btn-danger" onclick={() => handleDeleteGroupClick(key)} title={t('deleteMcpServerTitle')}>{t('delete')}</button>
              {/if}
            </div>

            {#if !collapsed}
              <table>
                {#if isMcpGroup(key)}
                  <colgroup>
                    <col style="width:22%" />
                    <col style="width:14%" />
                    <col style="width:43%" />
                    <col style="width:15%" />
                    <col style="width:6%" />
                  </colgroup>
                {:else}
                  <colgroup>
                    <col style="width:19%" />
                    <col style="width:14%" />
                    <col style="width:40%" />
                    <col style="width:15%" />
                    <col style="width:12%" />
                  </colgroup>
                {/if}
                <thead>
                  <tr>
                    <th>{t('toolIdHeader')}</th>
                    <th>{t('toolNameHeader')}</th>
                    <th>{t('toolDescHeader')}</th>
                    <th>{t('labels')}</th>
                    <th>{t('actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {#each groupTools as tool (tool.tool_id)}
                    <tr>
                      <td class="nowrap">{tool.tool_id}</td>
                      <td class="nowrap">
                        <button class="link-btn" onclick={() => handleShowDetail(tool)}>{tool.name}</button>
                      </td>
                      <td class="ellipsis">{tool.description}</td>
                      <td class="labels-cell"><div class="labels-wrap">{#each tool.labels ?? [] as label}<span class="label-tag">{label}</span>{/each}</div></td>
                      <td class="nowrap">
                        <span class="btn-group">
                          {#if isMcpGroup(key)}
                            <button class="btn btn-sm" onclick={() => handleMcpToolEditClick(tool)}>{t('edit')}</button>
                          {:else if !tool.builtin}
                            <button class="btn btn-sm" onclick={() => handleEditClick(tool)}>{t('edit')}</button>
                            <button class="btn btn-sm btn-danger" onclick={() => handleDeleteClick(tool)}>{t('delete')}</button>
                          {:else}
                            <span class="badge badge-builtin">{t('builtin')}</span>
                          {/if}
                        </span>
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
    height: 100%;
    display: flex;
    flex-direction: column;
  }
  .btn-sm { padding: 4px 12px; font-size: 0.85rem; background: var(--bg-secondary); color: var(--text); border: 1px solid var(--border); border-radius: 4px; cursor: pointer; }
  .btn-sm:hover { opacity: 0.8; }
  .btn-danger { background: var(--danger); color: #fff; border: none; }
  .btn-danger:hover { background: var(--danger-hover); }
  .link-btn { background: none; border: none; color: var(--primary); cursor: pointer; padding: 0; font-size: inherit; text-decoration: underline; }
  .link-btn:hover { opacity: 0.8; }
  .error-msg { background: var(--danger); color: #fff; padding: 10px 14px; border-radius: 6px; margin: 0 20px; font-size: 0.9rem; }
  .page-content {
    flex: 1;
    overflow-y: auto;
    padding: 20px;
  }
  .loading, .empty { text-align: center; padding: 40px 0; color: var(--text-secondary); font-size: 1rem; }
  .groups-wrap { display: flex; flex-direction: column; gap: 12px; }
  .group-block { border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  .group-header { display: flex; align-items: center; gap: 8px; padding: 8px 12px; background: var(--bg-secondary); border-bottom: 1px solid var(--border); }
  .collapse-btn { background: none; border: none; cursor: pointer; color: var(--text-secondary); font-size: 0.65rem; padding: 0 2px; flex-shrink: 0; }
  .group-title { flex: 1; display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 0.9rem; color: var(--text); }
  .badge { font-size: 0.7rem; padding: 1px 6px; border-radius: 4px; font-weight: 700; }
  .badge-mcp { background: #2563eb22; color: #2563eb; }
  .mcp-server-type { color: var(--primary); font-size: 0.8rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 320px; margin-left: auto; }
  .badge-builtin { font-size: 0.7rem; padding: 1px 6px; border-radius: 4px; font-weight: 700; background: #16a34a22; color: #16a34a; }
  .group-count { font-weight: normal; font-size: 0.8rem; color: var(--text-secondary); }
  table { width: 100%; border-collapse: collapse; table-layout: fixed; }
  th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
  th { background: var(--bg-secondary); color: var(--text-secondary); font-weight: 600; font-size: 0.85rem; }
  td { font-size: 0.9rem; color: var(--text); }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr:hover td { background: var(--bg-secondary); }
  .nowrap { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .ellipsis { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .btn-group { display: inline-flex; gap: 8px; }
  .labels-wrap { display: flex; flex-wrap: wrap; gap: 4px; }
  .label-tag { display: inline-block; padding: 2px 7px; background: #10b981; color: #fff; border-radius: 10px; font-size: 0.78rem; width: fit-content; }
  
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
