<script>
  import { models } from '../../lib/api.js'
  import ModelForm from './ModelForm.svelte'
  import ConfirmDialog from '../ConfirmDialog.svelte'
  import { t } from '../../lib/i18n.svelte.js'

  let modelList = $state([])
  let loading = $state(true)
  let error = $state('')
  let showForm = $state(false)
  let editingModel = $state(null)
  let deleteTarget = $state(null)

  async function fetchModels() {
    loading = true
    error = ''
    try {
      const data = await models.list()
      modelList = data.models ?? []
    } catch (err) {
      error = err.message || t('fetchModelListFailed')
    } finally {
      loading = false
    }
  }

  function handleCreate() { editingModel = null; showForm = true }
  function handleEdit(model) { editingModel = model; showForm = true }
  function handleFormSuccess() { showForm = false; editingModel = null; fetchModels() }
  function handleFormCancel() { showForm = false; editingModel = null }
  function handleDeleteClick(model) { deleteTarget = model }

  async function handleDeleteConfirm() {
    if (!deleteTarget) return
    const id = deleteTarget.model_id
    deleteTarget = null
    try {
      await models.delete(id)
      fetchModels()
    } catch (err) {
      error = err.message || t('deleteModelFailed')
    }
  }

  function handleDeleteCancel() { deleteTarget = null }

  $effect(() => { fetchModels() })
</script>

<div class="models-page">
  <div class="page-header">
    <h2>{t('modelsPageTitle')}</h2>
    {#if !showForm}
      <button class="btn btn-primary" onclick={handleCreate}>{t('registerModel')}</button>
    {/if}
  </div>

  {#if showForm}
    <ModelForm model={editingModel} onSuccess={handleFormSuccess} onCancel={handleFormCancel} />
  {/if}

  {#if error}
    <div class="error-msg">{error}</div>
  {/if}

  {#if loading}
    <div class="loading">{t('loading')}</div>
  {:else if modelList.length === 0 && !error}
    <div class="empty">{t('noModels')}</div>
  {:else if modelList.length > 0}
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>{t('modelIdHeader')}</th>
            <th>{t('modelNameHeader')}</th>
            <th>{t('apiBaseHeader')}</th>
            <th>{t('typeHeader')}</th>
            <th>{t('protocolHeader')}</th>
            <th>{t('actions')}</th>
          </tr>
        </thead>
        <tbody>
          {#each modelList as m (m.model_id)}
            <tr>
              <td>{m.model_id}</td>
              <td>{m.model_name}</td>
              <td>{m.api_base}</td>
              <td>{m.model_type}</td>
              <td>{m.api_protocol}</td>
              <td class="actions">
                <button class="btn btn-sm" onclick={() => handleEdit(m)}>{t('edit')}</button>
                <button class="btn btn-sm btn-danger" onclick={() => handleDeleteClick(m)}>{t('delete')}</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

<ConfirmDialog
  open={deleteTarget !== null}
  title={t('confirmDeleteModel', { id: deleteTarget?.model_id ?? '' })}
  onConfirm={handleDeleteConfirm}
  onCancel={handleDeleteCancel}
/>

<style>
  .models-page { padding: 20px; }
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
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--border); }
  th { background: var(--bg-secondary); color: var(--text-secondary); font-weight: 600; font-size: 0.85rem; }
  td { font-size: 0.9rem; color: var(--text); }
  .actions { display: flex; gap: 8px; }
</style>
