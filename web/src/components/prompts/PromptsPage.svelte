<script>
  import { promptTemplates } from '../../lib/api.js'
  import { catalog, loadPromptTemplates, refreshPromptTemplates } from '../../lib/catalog-state.svelte.js'
  import ConfirmDialog from '../ConfirmDialog.svelte'
  import { extractPlaceholders } from '../../lib/placeholder.js'
  import { t } from '../../lib/i18n.svelte.js'

  let { onEdit = null, onCopy = null, sortByTimeDesc = false } = $props()

  let templateList = $derived.by(() => {
    const items = [...catalog.promptTemplates.items]
    if (sortByTimeDesc) {
      items.sort((a, b) => {
        const timeDiff = (b.last_modified || '').localeCompare(a.last_modified || '')
        if (timeDiff !== 0) return timeDiff
        return (a.template_id || '').localeCompare(b.template_id || '')
      })
    } else {
      items.sort((a, b) => (a.template_id || '').localeCompare(b.template_id || ''))
    }
    return items
  })
  let loading = $derived(catalog.promptTemplates.loading && !catalog.promptTemplates.loaded)
  let error = $derived(catalog.promptTemplates.error)
  let deleteTarget = $state(null)

  async function fetchTemplates({ force = false } = {}) {
    try {
      if (force) await refreshPromptTemplates()
      else await loadPromptTemplates()
    } catch {
      catalog.promptTemplates.error = catalog.promptTemplates.error || t('fetchTemplateListFailed')
    }
  }

  function handleDeleteClick(tpl) { deleteTarget = tpl }

  function handleEditClick(tpl) {
    if (onEdit) onEdit(tpl)
  }

  function handleCopyClick(tpl) {
    if (onCopy) onCopy(tpl)
  }

  async function handleDeleteConfirm() {
    if (!deleteTarget) return
    const id = deleteTarget.template_id
    deleteTarget = null
    try {
      await promptTemplates.delete(id)
      await fetchTemplates({ force: true })
    } catch (err) {
      catalog.promptTemplates.error = err.message || t('deleteTemplateFailed')
    }
  }

  function handleDeleteCancel() { deleteTarget = null }

  let mounted = $state(false)
  $effect(() => {
    if (!mounted) {
      mounted = true
      fetchTemplates()
    }
  })
</script>

<div class="prompts-page">
  {#if error}
    <div class="error-msg">{error}</div>
  {/if}

  <div class="page-content">
    {#if loading}
      <div class="loading">{t('loading')}</div>
    {:else if templateList.length === 0 && !error}
      <div class="empty">{t('noTemplates')}</div>
    {:else if templateList.length > 0}
      <div class="template-list">
        {#each templateList as tpl (tpl.template_id)}
          <div class="template-card">
            <div class="template-info">
              <div class="template-name-row">
                <span class="template-name">{tpl.template_id}</span>
                <div class="template-tags">
                  {#each extractPlaceholders(tpl.content) as ph}
                    <span class="placeholder-tag">{ph}</span>
                  {/each}
                  {#each tpl.labels ?? [] as label}
                    <span class="label-tag">{label}</span>
                  {/each}
                </div>
              </div>
              <div class="template-preview">{tpl.content || ''}</div>
            </div>
            <div class="template-actions">
              <button class="btn btn-sm" onclick={() => handleEditClick(tpl)}>{t('edit')}</button>
              <button class="btn btn-sm btn-secondary" onclick={() => handleCopyClick(tpl)}>{t('copyPrompt')}</button>
              <button class="btn btn-sm btn-danger" onclick={() => handleDeleteClick(tpl)}>{t('delete')}</button>
            </div>
          </div>
        {/each}
      </div>
    {/if}
  </div>
</div>

<ConfirmDialog
  open={deleteTarget !== null}
  title={t('confirmDeleteTemplate', { id: deleteTarget?.template_id ?? '' })}
  onConfirm={handleDeleteConfirm}
  onCancel={handleDeleteCancel}
/>

<style>
  .prompts-page {
    height: 100%;
    display: flex;
    flex-direction: column;
  }
  .btn-sm { padding: 4px 12px; font-size: 0.85rem; background: var(--bg-secondary); color: var(--text); border: 1px solid var(--border); border-radius: 4px; }
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
  .template-list { display: flex; flex-direction: column; gap: 12px; }
  .template-card { display: flex; justify-content: space-between; align-items: center; padding: 14px 16px; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; }
  .template-info { flex: 1; min-width: 0; }
  .template-name-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; gap: 8px; }
  .template-name { font-weight: 600; color: var(--text); font-size: 0.95rem; }
  .template-tags { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }
  .placeholder-tag { display: inline-block; padding: 1px 7px; background: var(--primary); color: #fff; border-radius: 4px; font-size: 0.78rem; font-family: monospace; }
  .template-preview { 
    color: var(--text-secondary); 
    font-size: 0.85rem; 
    white-space: pre-wrap; 
    word-break: break-word;
    overflow-wrap: break-word;
    max-height: 4.5em;
    overflow-y: auto;
    line-height: 1.5;
  }
  .template-actions { display: flex; align-items: center; gap: 8px; margin-left: 16px; flex-shrink: 0; }
  
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
  .template-preview::-webkit-scrollbar {
    width: 4px;
  }
  .template-preview::-webkit-scrollbar-track {
    background: transparent;
  }
  .template-preview::-webkit-scrollbar-thumb {
    background: transparent;
    border-radius: 2px;
  }
  .template-preview:hover::-webkit-scrollbar-thumb {
    background: var(--border);
  }
</style>
