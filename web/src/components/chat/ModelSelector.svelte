<script>
  import { models } from '../../lib/api.js'
  import { t } from '../../lib/i18n.svelte.js'

  let { selectedModelId = $bindable(''), onchange } = $props()

  let modelList = $state([])
  let loading = $state(true)
  let error = $state('')

  async function fetchModels() {
    loading = true
    error = ''
    try {
      const data = await models.list()
      modelList = data.models ?? []
      // 如果已保存的模型 ID 不在列表中，则清空选择
      if (selectedModelId && !modelList.some(m => m.model_id === selectedModelId)) {
        selectedModelId = ''
      }
    } catch (err) {
      error = err.message || t('fetchModelsFailed')
    } finally {
      loading = false
    }
  }

  function handleChange(e) {
    selectedModelId = e.target.value
    onchange?.(selectedModelId)
  }

  $effect(() => { fetchModels() })
</script>

<div class="model-selector">
  <label for="model-select">{t('modelLabel')}</label>
  {#if loading}
    <span class="hint">{t('loading')}</span>
  {:else if error}
    <span class="hint error">{error}</span>
  {:else}
    <select id="model-select" value={selectedModelId} onchange={handleChange}>
      <option value="">{t('selectModelPlaceholder')}</option>
      {#each modelList as m (m.model_id)}
        <option value={m.model_id}>{m.model_name} ({m.model_id})</option>
      {/each}
    </select>
    {#if !selectedModelId}
      <span class="hint">{t('selectModelHint')}</span>
    {/if}
  {/if}
</div>

<style>
  .model-selector { display: flex; align-items: center; gap: 8px; }
  label { font-size: 0.85rem; font-weight: 600; color: var(--text-secondary); white-space: nowrap; }
  select {
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    font-size: 0.9rem;
    min-width: 180px;
  }
  .hint { font-size: 0.8rem; color: var(--text-secondary); }
  .hint.error { color: var(--danger); }
</style>
