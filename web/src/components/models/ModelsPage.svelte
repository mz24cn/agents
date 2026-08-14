<script>
  import { models } from '../../lib/api.js'
  import { catalog, loadModels, refreshModels } from '../../lib/catalog-state.svelte.js'
  import ConfirmDialog from '../ConfirmDialog.svelte'
  import { t } from '../../lib/i18n.svelte.js'

  let { onEdit = null, onCopy = null, sortByTimeDesc = false } = $props()

  let modelList = $derived.by(() => {
    const items = [...catalog.models.items]
    if (sortByTimeDesc) {
      items.sort((a, b) => {
        const timeDiff = (b.last_modified || '').localeCompare(a.last_modified || '')
        if (timeDiff !== 0) return timeDiff
        return (a.model_id || '').localeCompare(b.model_id || '')
      })
    } else {
      items.sort((a, b) => (a.model_id || '').localeCompare(b.model_id || ''))
    }
    return items
  })
  let loading = $derived(catalog.models.loading && !catalog.models.loaded)
  let error = $derived(catalog.models.error)
  let deleteTarget = $state(null)

  async function fetchModels({ force = false } = {}) {
    try {
      if (force) await refreshModels()
      else await loadModels()
    } catch {
      catalog.models.error = catalog.models.error || t('fetchModelListFailed')
    }
  }

  function handleDeleteClick(model) { deleteTarget = model }

  function handleEditClick(model) {
    if (onEdit) onEdit(model)
  }

  function handleCopyClick(model) {
    if (onCopy) onCopy(model)
  }

  async function handleDeleteConfirm() {
    if (!deleteTarget) return
    const id = deleteTarget.model_id
    deleteTarget = null
    try {
      await models.delete(id)
      await fetchModels({ force: true })
    } catch (err) {
      catalog.models.error = err.message || t('deleteModelFailed')
    }
  }

  function handleDeleteCancel() { deleteTarget = null }

  let mounted = $state(false)
  $effect(() => {
    if (!mounted) {
      mounted = true
      fetchModels()
    }
  })
</script>

<div class="models-page">
  {#if error}
    <div class="error-msg">{error}</div>
  {/if}

  <div class="page-content">
    {#if loading}
      <div class="loading">{t('loading')}</div>
    {:else if modelList.length === 0 && !error}
      <div class="empty">{t('noModels')}</div>
    {:else if modelList.length > 0}
      <table>
        <colgroup>
          <col style="width:15%" />
          <col style="width:12%" />
          <col style="width:28%" />
          <col style="width:8%" />
          <col style="width:18%" />
          <col style="width:19%" />
        </colgroup>
        <thead>
          <tr>
            <th>{t('modelIdHeader')}</th>
            <th>{t('modelNameHeader')}</th>
            <th>{t('apiBaseHeader')}</th>
            <th>{t('protocolHeader')}</th>
            <th>{t('labels')}</th>
            <th>{t('actions')}</th>
          </tr>
        </thead>
        <tbody>
          {#each modelList as m (m.model_id)}
            <tr>
              <td class="nowrap">{m.model_id}</td>
              <td class="nowrap">{m.model_name}</td>
              <td class="ellipsis">{m.api_base}</td>
              <td class="nowrap">{m.api_protocol}</td>
              <td class="labels-cell"><div class="labels-wrap">{#each m.labels ?? [] as label}<span class="label-tag">{label}</span>{/each}</div></td>
              <td class="nowrap">
                <span class="btn-group">
                  <button class="btn btn-sm" onclick={() => handleEditClick(m)}>{t('edit')}</button>
                  <button class="btn btn-sm btn-secondary" onclick={() => handleCopyClick(m)}>{t('copyModel')}</button>
                  <button class="btn btn-sm btn-danger" onclick={() => handleDeleteClick(m)}>{t('delete')}</button>
                </span>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
</div>

<ConfirmDialog
  open={deleteTarget !== null}
  title={t('confirmDeleteModel', { id: deleteTarget?.model_id ?? '' })}
  onConfirm={handleDeleteConfirm}
  onCancel={handleDeleteCancel}
/>

<style>
  .models-page {
    height: 100%;
    display: flex;
    flex-direction: column;
  }
  .btn-sm { padding: 4px 12px; font-size: 0.85rem; background: var(--bg-secondary); color: var(--text); border: 1px solid var(--border); border-radius: 4px; cursor: pointer; }
  .btn-sm:hover { opacity: 0.8; }
  .btn-secondary { background: var(--bg-secondary); color: var(--text); border: 1px solid var(--border); }
  .btn-secondary:hover { background: var(--primary); color: #fff; border-color: var(--primary); }
  .btn-danger { background: var(--danger); color: #fff; border: none; }
  .btn-danger:hover { background: var(--danger-hover); }
  .error-msg { background: var(--danger); color: #fff; padding: 10px 14px; border-radius: 6px; margin: 0 20px; font-size: 0.9rem; }
  .page-content {
    flex: 1;
    overflow-y: auto;
    padding: 20px;
  }
  .loading, .empty { text-align: center; padding: 40px 0; color: var(--text-secondary); font-size: 1rem; }

  table { width: 100%; border-collapse: collapse; table-layout: fixed; }
  th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
  th { background: var(--bg-secondary); color: var(--text-secondary); font-weight: 600; font-size: 0.85rem; }
  td { font-size: 0.9rem; color: var(--text); }
  .nowrap { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .ellipsis { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .btn-group { display: inline-flex; gap: 8px; }
  .labels-wrap { display: flex; flex-wrap: wrap; gap: 4px; }

  /* 滚动条样式 */
  .page-content::-webkit-scrollbar { width: 8px; }
  .page-content::-webkit-scrollbar-track { background: transparent; }
  .page-content::-webkit-scrollbar-thumb { background: transparent; border-radius: 4px; transition: background 0.2s; }
  .page-content:hover::-webkit-scrollbar-thumb { background: var(--border); }
  .page-content:hover::-webkit-scrollbar-thumb:hover { background: var(--text-secondary); }
</style>