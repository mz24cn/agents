<script>
  import { promptTemplates } from '../../lib/api.js'
  import PromptForm from './PromptForm.svelte'
  import ConfirmDialog from '../ConfirmDialog.svelte'
  import { extractPlaceholders } from '../../lib/placeholder.js'
  import { t } from '../../lib/i18n.svelte.js'

  let templateList = $state([])
  let loading = $state(true)
  let error = $state('')
  let showForm = $state(false)
  let editingTemplate = $state(null)
  let deleteTarget = $state(null)

  async function fetchTemplates() {
    loading = true
    error = ''
    try {
      const data = await promptTemplates.list()
      templateList = data.templates ?? []
    } catch (err) {
      error = err.message || t('fetchTemplateListFailed')
    } finally {
      loading = false
    }
  }

  function handleCreate() { editingTemplate = null; showForm = true }
  function handleEdit(tpl) { editingTemplate = tpl; showForm = true }
  function handleFormSuccess() { showForm = false; editingTemplate = null; fetchTemplates() }
  function handleFormCancel() { showForm = false; editingTemplate = null }
  function handleDeleteClick(tpl) { deleteTarget = tpl }

  async function handleDeleteConfirm() {
    if (!deleteTarget) return
    const id = deleteTarget.template_id
    deleteTarget = null
    try {
      await promptTemplates.delete(id)
      fetchTemplates()
    } catch (err) {
      error = err.message || t('deleteTemplateFailed')
    }
  }

  function handleDeleteCancel() { deleteTarget = null }

  function truncate(text, max = 80) {
    if (!text) return ''
    return text.length > max ? text.slice(0, max) + '...' : text
  }

  $effect(() => { fetchTemplates() })
</script>

<div class="prompts-page">
  <div class="page-header">
    <h2>{t('promptsPageTitle')}</h2>
    {#if !showForm}
      <button class="btn btn-primary" onclick={handleCreate}>{t('newTemplate')}</button>
    {/if}
  </div>

  {#if showForm}
    <PromptForm template={editingTemplate} onSuccess={handleFormSuccess} onCancel={handleFormCancel} />
  {/if}

  {#if error}
    <div class="error-msg">{error}</div>
  {/if}

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
              </div>
            </div>
            <div class="template-preview">{truncate(tpl.content)}</div>
          </div>
          <div class="template-actions">
            <button class="btn btn-sm" onclick={() => handleEdit(tpl)}>{t('edit')}</button>
            <button class="btn btn-sm btn-danger" onclick={() => handleDeleteClick(tpl)}>{t('delete')}</button>
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>

<ConfirmDialog
  open={deleteTarget !== null}
  title={t('confirmDeleteTemplate', { id: deleteTarget?.template_id ?? '' })}
  onConfirm={handleDeleteConfirm}
  onCancel={handleDeleteCancel}
/>

<style>
  .prompts-page { padding: 20px; }
  .page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
  h2 { margin: 0; color: var(--text); }
  .btn { padding: 8px 18px; border-radius: 6px; border: none; cursor: pointer; font-size: 0.9rem; }
  .btn-primary { background: var(--primary); color: #fff; }
  .btn-primary:hover { background: var(--primary-hover); }
  .btn-sm { padding: 4px 12px; font-size: 0.85rem; background: var(--bg-secondary); color: var(--text); border: 1px solid var(--border); border-radius: 4px; }
  .btn-sm:hover { opacity: 0.8; }
  .btn-danger { background: var(--danger); color: #fff; border: none; }
  .btn-danger:hover { background: var(--danger-hover); }
  .error-msg { background: var(--danger); color: #fff; padding: 10px 14px; border-radius: 6px; margin-bottom: 16px; font-size: 0.9rem; }
  .loading, .empty { text-align: center; padding: 40px 0; color: var(--text-secondary); font-size: 1rem; }
  .template-list { display: flex; flex-direction: column; gap: 12px; }
  .template-card { display: flex; justify-content: space-between; align-items: center; padding: 14px 16px; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; }
  .template-info { flex: 1; min-width: 0; }
  .template-name-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; gap: 8px; }
  .template-name { font-weight: 600; color: var(--text); font-size: 0.95rem; }
  .template-tags { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }
  .placeholder-tag { display: inline-block; padding: 1px 7px; background: var(--primary); color: #fff; border-radius: 4px; font-size: 0.78rem; font-family: monospace; }
  .template-preview { color: var(--text-secondary); font-size: 0.85rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .template-actions { display: flex; align-items: center; gap: 8px; margin-left: 16px; flex-shrink: 0; }
</style>
